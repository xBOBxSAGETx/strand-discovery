"""Watcher (generator/watch.py, issue #78) with stubbed HTTP and TMDB (no network):

    python -m unittest tests.test_watch

A quiet run lists nothing; a changed feed / provider fingerprint (read twice in a row) lists exactly the cards it feeds;
--dispatch starts build.yml with the right command and saves the state only when that worked (#79).
"""
import copy, hashlib, io, json, os, sys, tempfile, threading, time, unittest
from contextlib import redirect_stdout
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'generator'))
import build, watch  # noqa: E402

SPEC = watch.load_spec()
SLUGS = [c['slug'] for c in SPEC['catalogs']]


class Net:
    """Stub HTTP: feeds / MDBList lists answer 200 with a body, or 304 when the request carries the validator they
    were last served with."""

    def __init__(self):
        self.bodies, self.force, self.calls = {}, {}, []

    def __call__(self, url, headers):
        self.calls.append(url)
        if url in self.force:
            return self.force[url]
        body = self.bodies.get(url, b'body-of-' + url.encode())
        etag = '"' + hashlib.md5(body).hexdigest() + '"'
        if headers.get('If-None-Match') == etag:
            return 304, {}, b''
        return 200, {'etag': etag}, body


class Tmdb:
    """Stub TMDB: discover page 1 per (path, watch provider ids)."""

    def __init__(self):
        self.disc, self.calls = {}, []

    def __call__(self, path, **p):
        self.calls.append(path)
        if path.startswith('/discover/'):
            return self.disc.get((path, p['with_watch_providers']), {'total_results': 500, 'results': [{'id': i} for i in range(1, 21)]})
        raise AssertionError(f'unexpected TMDB call {path}')


def make(net=None, tmdb=None, state=None, **kw):
    net, tmdb = net or Net(), tmdb or Tmdb()
    w = watch.Watch(SPEC, state if state is not None else {}, http=net, tmdb=tmdb, today='2026-10-15', **kw)
    return w, net, tmdb


def baseline(net, tmdb, **kw):
    """One first run (records every source, reports nothing); returns its state."""
    state = {}
    w, *_ = make(net, tmdb, state, **kw)
    res = w.run()
    assert res['cards'] == [] and not res['full'], res
    return state


NETFLIX = ['streaming-netflix']      # one card per service (#143): Popular + New (+ Top Rated where kept) are parts of it
FLIPPED = {'total_results': 501, 'results': [{'id': i} for i in range(1, 21)]}


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.net, self.tmdb = Net(), Tmdb()

    def run_again(self, state, **kw):
        w, *_ = make(self.net, self.tmdb, state, **kw)
        return w.run()

    def test_first_run_is_a_baseline(self):
        state = baseline(self.net, self.tmdb)
        self.assertTrue(state['fp'] and state['http'])

    def test_quiet_run_lists_nothing(self):
        state = baseline(self.net, self.tmdb)
        res = self.run_again(state)
        self.assertEqual((res['cards'], res['reasons'], res['full'], res['errors']), ([], {}, False, []))

    def test_feed_200_gives_that_services_new_card(self):
        state = baseline(self.net, self.tmdb)
        self.net.bodies[watch.FEED_URLS['won']] = b'a new post'
        res = self.run_again(state)
        self.assertEqual(res['cards'], ['streaming-netflix'])
        self.assertEqual(res['reasons'], {'streaming-netflix': ['feed won changed']})

    def test_feed_200_with_the_same_body_is_nothing(self):
        state = baseline(self.net, self.tmdb)
        url = watch.FEED_URLS['won']
        self.net.force[url] = (200, {}, b'body-of-' + url.encode())      # a server that ignores If-None-Match
        self.assertEqual(self.run_again(state)['cards'], [])

    def test_multi_service_feed_maps_to_each_service(self):
        state = baseline(self.net, self.tmdb)
        self.net.bodies[watch.FEED_URLS['wodp']] = b'new'
        self.assertEqual(self.run_again(state)['cards'],
                         ['streaming-disneyplus', 'streaming-hbo-max', 'streaming-hulu'])

    def test_mdblist_200_gives_its_card(self):
        state = baseline(self.net, self.tmdb)
        self.net.bodies[watch.MDBLIST_JSON.format(list='apg2886/oscar-best-picture-winners')] = b'[1]'
        self.assertEqual(self.run_again(state)['cards'], ['awards-oscar-best-picture'])

    # ---- provider fingerprints: a change counts when two runs in a row read it (#79: cache noise)
    def test_one_odd_provider_reading_is_not_a_change(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.disc[('/discover/movie', '8')] = FLIPPED
        self.assertEqual(self.run_again(state)['cards'], [])             # first sighting: held
        del self.tmdb.disc[('/discover/movie', '8')]                     # the cached page flips back
        self.assertEqual(self.run_again(state)['cards'], [])
        self.assertEqual(self.run_again(state)['cards'], [])
        self.assertNotIn('pend', state['fp']['netflix:movie'])           # the held reading was dropped

    def test_two_equal_provider_readings_in_a_row_give_that_providers_cards(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.disc[('/discover/movie', '8')] = FLIPPED
        self.assertEqual(self.run_again(state)['cards'], [])
        res = self.run_again(state)
        self.assertEqual(res['cards'], NETFLIX)
        self.assertNotIn('streaming-netflix-kids', res['cards'])
        self.assertEqual(self.run_again(state)['cards'], [])             # now the confirmed reading: quiet again

    def test_provider_page_in_a_different_order_is_not_a_change(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.disc[('/discover/movie', '8')] = {'total_results': 500, 'results': [{'id': i} for i in range(20, 0, -1)]}
        self.assertEqual(self.run_again(state)['cards'], [])
        self.assertEqual(self.run_again(state)['cards'], [])

    def test_a_new_day_rebaselines_instead_of_reporting(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.disc[('/discover/movie', '8')] = {'total_results': 501, 'results': []}
        for _ in range(2):
            w, *_ = make(self.net, self.tmdb, state)
            w.today = '2026-10-16'                   # the query's date filters moved; a count change proves nothing
            self.assertEqual(w.run()['cards'], [])

    def test_changed_first_page_ids_with_the_same_total_count(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.disc[('/discover/tv', '8')] = {'total_results': 500, 'results': [{'id': i} for i in range(2, 22)]}
        self.run_again(state)
        self.assertIn('streaming-netflix', self.run_again(state)['cards'])

    def test_too_many_cards_is_full(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.disc[('/discover/movie', '8')] = {'total_results': 501, 'results': []}
        self.run_again(state, max_cards=0)
        res = self.run_again(state, max_cards=0)
        self.assertTrue(res['full'])
        self.assertEqual(res['cards'], [])

    def test_a_failed_source_changes_nothing_and_is_reported(self):
        state = baseline(self.net, self.tmdb)
        self.net.force[watch.FEED_URLS['plex']] = (503, {}, b'')
        res = self.run_again(state)
        self.assertEqual(res['cards'], [])
        self.assertEqual(len(res['errors']), 1)

    def test_tmdb_changes_lists_are_not_read(self):
        """#79: /movie/changes and /tv/changes map to ~320 of the 606 cards on every refresh (always a full build)."""
        state = baseline(self.net, self.tmdb)
        self.run_again(state)
        self.assertTrue(self.tmdb.calls and all(c.startswith('/discover/') for c in self.tmdb.calls), set(self.tmdb.calls))

    def test_quiet_run_is_under_budget(self):
        state = baseline(self.net, self.tmdb)
        res = self.run_again(state)
        self.assertLessEqual(res['requests'], 250)
        print(f"  quiet run: {res['requests']} requests")

    def test_request_counter_is_locked(self):
        """The discover calls run on worker threads. A slow store (the sleep) makes an unlocked `+= 1` lose counts."""
        class Slow(watch.Watch):
            _n = 0
            requests = property(lambda self: self._n, lambda self, v: (time.sleep(0.0005), setattr(self, '_n', v))[1])

        w = Slow(SPEC, {}, http=Net(), tmdb=Tmdb(), today='2026-10-15')
        ts = [threading.Thread(target=lambda: [w.count() for _ in range(25)]) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(w.requests, 200)

    def test_urls_are_the_ones_the_generator_uses(self):
        src = (ROOT / 'generator' / 'arrivals.py').read_text(encoding='utf-8')
        for url in watch.FEED_URLS.values():
            self.assertIn(url, src)
        self.assertIn("MDBLIST_JSON = 'https://mdblist.com/lists/{list}/json'", (ROOT / 'generator' / 'build.py').read_text(encoding='utf-8'))

    def test_fingerprint_query_is_the_new_cards_discover_query(self, today='2026-10-15'):
        """fp_params must equal what build.discover sends for page 1 of the same card (drift guard). build.TODAY is
        pinned so the check never depends on the clock (the CI break: TODAY read at import before midnight UTC, the
        window read from dt.date.today() after it, one day apart)."""
        seen = []
        real, real_today = build.tmdb, build.TODAY
        build.TODAY = today
        build.tmdb = lambda path, **p: (seen.append((path, p)), {'results': [], 'total_results': 0, 'total_pages': 0})[1]
        try:
            for slug in ('streaming-netflix', 'streaming-tubi', 'streaming-mubi'):
                union = next(c for c in SPEC['catalogs'] if c['slug'] == slug)
                card = copy.deepcopy(next(p for p in build.union_parts(union) if p['sort'] == 'new'))   # #143: New is a part
                for media in ('movie', 'series'):
                    seen.clear()
                    build.discover(card, media, 5, SPEC['defaults'])
                    path, p = seen[0]
                    p = {k: v for k, v in p.items() if k != 'page'}
                    self.assertEqual(p, watch.fp_params(card, media, SPEC['defaults'], build.TODAY), (slug, media))
        finally:
            build.tmdb, build.TODAY = real, real_today

    def test_discover_dates_follow_build_today_not_the_live_clock(self):
        """Replay of the CI break: build.TODAY is a different day than the clock. A pinned 2020 date never equals the
        real one, so code that reads dt.date.today() for a window fails here on any clock."""
        self.test_fingerprint_query_is_the_new_cards_discover_query(today='2020-02-29')


# a stand-in for `gh`: logs its arguments, answers `run list` from STUB_RUNS, fails `workflow run` when STUB_FAIL=1
GH_STUB = '''
import json, os, sys
with open(os.environ['STUB_LOG'], 'a', encoding='utf-8') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['run', 'list']:
    print(os.environ.get('STUB_RUNS', '[]'))
elif sys.argv[1:3] == ['workflow', 'run'] and os.environ.get('STUB_FAIL') == '1':
    sys.stderr.write('HTTP 403: Resource not accessible by integration')
    sys.exit(1)
'''


class DispatchTests(unittest.TestCase):
    """watch.main() end to end with stub HTTP / TMDB / gh: the dispatch command, a quiet run, a failed dispatch."""

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix='sd-watch-'))
        self.cache, self.out, self.log = d / 'cache', d / 'changes.json', d / 'gh.log'
        (d / 'gh_stub.py').write_text(GH_STUB, encoding='utf-8')
        self.net, self.tmdb = Net(), Tmdb()
        self.saved = (watch.http_get, build.tmdb, dict(os.environ), watch.MAX_CARDS)
        watch.http_get, build.tmdb = self.net, self.tmdb
        os.environ.update(SD_GH=f'"{Path(sys.executable).as_posix()}" "{(d / "gh_stub.py").as_posix()}"', STUB_LOG=str(self.log))
        os.environ.pop('GITHUB_OUTPUT', None)
        self.main('--dispatch')                                   # first run: the baseline (saves the state)
        self.log.unlink(missing_ok=True)

    def tearDown(self):
        watch.http_get, build.tmdb, env, watch.MAX_CARDS = self.saved
        os.environ.clear()
        os.environ.update(env)

    def main(self, *flags):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                return watch.main(['--cache', str(self.cache), '--out', str(self.out), *flags])
        finally:
            self.stdout = buf.getvalue()

    def gh_calls(self):
        return [json.loads(x) for x in self.log.read_text(encoding='utf-8').splitlines()] if self.log.exists() else []

    def state_bytes(self):
        return (self.cache / 'watch_state.json').read_bytes()

    def flip_netflix(self):
        """Two runs in a row read a changed Netflix movie page; the first is held, the second is the change."""
        self.tmdb.disc[('/discover/movie', '8')] = FLIPPED
        self.assertEqual(self.main('--dispatch')['cards'], [])
        self.assertEqual(self.gh_calls(), [])                     # held: gh is not even asked

    def test_changed_fingerprint_dispatches_those_cards(self):
        self.flip_netflix()
        res = self.main('--dispatch')
        self.assertEqual(res['dispatch'], 'sent')
        calls = self.gh_calls()
        self.assertEqual(calls[0][:2], ['run', 'list'])
        self.assertEqual(calls[1], ['workflow', 'run', 'build.yml', '--ref', 'main', '-f', 'deploy=true',
                                    '-f', 'cards=' + ','.join(NETFLIX)])
        self.assertEqual(json.loads(self.state_bytes())['fp']['netflix:movie']['t'], 501)       # saved after the dispatch
        self.log.unlink()
        self.assertEqual(self.main('--dispatch')['dispatch'], 'none')                          # and it does not repeat
        self.assertEqual(self.gh_calls(), [])

    def test_quiet_run_dispatches_nothing_and_saves_nothing(self):
        before = self.state_bytes()
        res = self.main('--dispatch')
        self.assertEqual((res['cards'], res['dispatch']), ([], 'none'))
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.state_bytes(), before)
        self.assertIn('state unchanged', self.stdout)

    def test_failed_dispatch_leaves_the_state_unsaved_and_the_change_comes_back(self):
        self.flip_netflix()
        before = self.state_bytes()
        os.environ['STUB_FAIL'] = '1'
        with self.assertRaises(SystemExit) as cm:
            self.main('--dispatch')
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(self.state_bytes(), before)               # not saved
        self.assertIn('dispatch failed', self.stdout)
        os.environ['STUB_FAIL'] = '0'
        self.log.unlink()
        self.assertEqual(self.main('--dispatch')['dispatch'], 'sent')       # the same change, sent now
        self.assertIn('cards=' + ','.join(NETFLIX), self.gh_calls()[-1][-1])

    def test_a_build_of_main_already_queued_or_running_holds_the_dispatch(self):
        self.flip_netflix()
        before = self.state_bytes()
        os.environ['STUB_RUNS'] = json.dumps([{'status': 'completed'}, {'status': 'queued'}])
        res = self.main('--dispatch')
        self.assertEqual(res['dispatch'], 'held')
        self.assertEqual([c[:2] for c in self.gh_calls()], [['run', 'list']])       # never `workflow run`
        self.assertEqual(self.state_bytes(), before)
        os.environ['STUB_RUNS'] = json.dumps([{'status': 'completed'}])
        self.assertEqual(self.main('--dispatch')['dispatch'], 'sent')

    def test_too_many_cards_dispatches_a_full_build(self):
        watch.MAX_CARDS = 0
        self.tmdb.disc[('/discover/movie', '8')] = FLIPPED
        self.main('--dispatch')
        res = self.main('--dispatch')
        self.assertTrue(res['full'])
        self.assertEqual(self.gh_calls()[1], ['workflow', 'run', 'build.yml', '--ref', 'main', '-f', 'deploy=true'])

    def test_dry_run_reads_only(self):
        self.flip_netflix()
        before = self.state_bytes()
        res = self.main('--dry', '--dispatch')
        self.assertEqual((res['cards'], res['dispatch']), (NETFLIX, 'none'))
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.state_bytes(), before)

    def test_github_output_says_what_happened(self):
        outp = self.cache.parent / 'gh_output'
        os.environ['GITHUB_OUTPUT'] = str(outp)
        self.flip_netflix()
        self.main('--dispatch')
        lines = outp.read_text(encoding='utf-8').split()
        self.assertEqual(lines[-2:], ['dispatch=sent', 'dirty=true'])


if __name__ == '__main__':
    unittest.main()
