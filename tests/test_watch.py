"""Watcher (generator/watch.py, issue #78) with stubbed HTTP and TMDB (no network):

    python -m unittest tests.test_watch

A quiet run lists nothing; a changed feed / provider fingerprint / TMDB change id lists exactly the cards it feeds.
"""
import copy, hashlib, json, os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'generator'))
import build, watch  # noqa: E402

SPEC = watch.load_spec()
SLUGS = [c['slug'] for c in SPEC['catalogs']]
GEN = '2026-10-15T00:00:00+00:00'


class Net:
    """Stub HTTP: feeds / MDBList lists answer 200 with a body, or 304 when the request carries the validator they
    were last served with. Also serves the site's summary.json."""

    def __init__(self):
        self.bodies, self.force, self.calls = {}, {}, []

    def __call__(self, url, headers):
        self.calls.append(url)
        if url in self.force:
            return self.force[url]
        if url.endswith('/summary.json'):
            return 200, {}, json.dumps({'generated_at': GEN}).encode()
        body = self.bodies.get(url, b'body-of-' + url.encode())
        etag = '"' + hashlib.md5(body).hexdigest() + '"'
        if headers.get('If-None-Match') == etag:
            return 304, {}, b''
        return 200, {'etag': etag}, body


class Tmdb:
    """Stub TMDB: discover page 1 per (path, watch provider ids); /changes lists."""

    def __init__(self):
        self.disc, self.changes, self.calls = {}, {'movie': [], 'tv': []}, []

    def __call__(self, path, **p):
        self.calls.append(path)
        if path.startswith('/discover/'):
            return self.disc.get((path, p['with_watch_providers']), {'total_results': 500, 'results': [{'id': i} for i in range(1, 21)]})
        kind = path.split('/')[1]
        ids = self.changes[kind]
        pg = p.get('page', 1)
        return {'total_results': len(ids), 'total_pages': max(1, -(-len(ids) // 100)),
                'results': [{'id': i} for i in ids[(pg - 1) * 100: pg * 100]]}


def make(net=None, tmdb=None, state=None, index=None, **kw):
    net, tmdb = net or Net(), tmdb or Tmdb()
    index = index if index is not None else {'generated_at': GEN, 'cards': SLUGS, 'ids': {}}
    w = watch.Watch(SPEC, state if state is not None else {}, http=net, tmdb=tmdb, site='https://site.test',
                    today='2026-10-15', index=index, **kw)
    return w, net, tmdb


def baseline(net, tmdb, index=None, **kw):
    """One first run (records every source, reports nothing); returns its state."""
    state = {}
    w, *_ = make(net, tmdb, state, index, **kw)
    res = w.run()
    assert res['cards'] == [] and not res['full'], res
    return state


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.net, self.tmdb = Net(), Tmdb()
        self.tmdb.changes = {'movie': [11, 12], 'tv': [21]}

    def run_again(self, state, index=None, **kw):
        w, *_ = make(self.net, self.tmdb, state, index, **kw)
        return w.run()

    def test_first_run_is_a_baseline(self):
        state = baseline(self.net, self.tmdb)
        self.assertTrue(state['fp'] and state['http'] and 'seen' in state['changes']['movie'])

    def test_quiet_run_lists_nothing(self):
        state = baseline(self.net, self.tmdb)
        res = self.run_again(state)
        self.assertEqual((res['cards'], res['reasons'], res['full'], res['errors']), ([], {}, False, []))

    def test_feed_200_gives_that_services_new_card(self):
        state = baseline(self.net, self.tmdb)
        self.net.bodies[watch.FEED_URLS['won']] = b'a new post'
        res = self.run_again(state)
        self.assertEqual(res['cards'], ['streaming-netflix-new'])
        self.assertEqual(res['reasons'], {'streaming-netflix-new': ['feed won changed']})

    def test_feed_200_with_the_same_body_is_nothing(self):
        state = baseline(self.net, self.tmdb)
        url = watch.FEED_URLS['won']
        self.net.force[url] = (200, {}, b'body-of-' + url.encode())      # a server that ignores If-None-Match
        self.assertEqual(self.run_again(state)['cards'], [])

    def test_multi_service_feed_maps_to_each_service(self):
        state = baseline(self.net, self.tmdb)
        self.net.bodies[watch.FEED_URLS['wodp']] = b'new'
        self.assertEqual(self.run_again(state)['cards'],
                         ['streaming-disneyplus-new', 'streaming-hbo-max-new', 'streaming-hulu-new'])

    def test_mdblist_200_gives_its_card(self):
        state = baseline(self.net, self.tmdb)
        self.net.bodies[watch.MDBLIST_JSON.format(list='apg2886/oscar-best-picture-winners')] = b'[1]'
        self.assertEqual(self.run_again(state)['cards'], ['awards-oscar-best-picture'])

    def test_changed_provider_fingerprint_gives_that_providers_cards(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.disc[('/discover/movie', '8')] = {'total_results': 501, 'results': [{'id': i} for i in range(1, 21)]}
        res = self.run_again(state)
        self.assertEqual(res['cards'], ['streaming-netflix', 'streaming-netflix-new', 'streaming-netflix-top'])
        self.assertNotIn('streaming-netflix-kids', res['cards'])

    def test_a_new_day_rebaselines_instead_of_reporting(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.disc[('/discover/movie', '8')] = {'total_results': 501, 'results': []}
        w, *_ = make(self.net, self.tmdb, state)
        w.today = '2026-10-16'                       # the query's date filters moved; a count change proves nothing
        self.assertEqual(w.run()['cards'], [])

    def test_changed_first_page_ids_with_the_same_total_count(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.disc[('/discover/tv', '8')] = {'total_results': 500, 'results': [{'id': i} for i in range(2, 22)]}
        self.assertIn('streaming-netflix-new', self.run_again(state)['cards'])

    def test_changed_id_gives_the_cards_that_contain_it(self):
        index = {'generated_at': GEN, 'cards': SLUGS, 'ids': {'m555': [SLUGS.index('genre-horror'), SLUGS.index('streaming-hulu')],
                                                              't777': [SLUGS.index('genre-drama')]}}
        state = baseline(self.net, self.tmdb, index)
        self.tmdb.changes['movie'] = [11, 12, 555]
        res = self.run_again(state, index)
        self.assertEqual(res['cards'], ['genre-horror', 'streaming-hulu'])
        self.tmdb.changes['tv'] = [21, 777]
        res = self.run_again(state, index)
        self.assertEqual(res['cards'], ['genre-drama'])          # 555 is already handled

    def test_unchanged_changes_list_costs_one_request_each(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.calls.clear()
        self.run_again(state)
        self.assertEqual([c for c in self.tmdb.calls if 'changes' in c], ['/movie/changes', '/tv/changes'])

    def test_too_many_cards_is_full(self):
        state = baseline(self.net, self.tmdb)
        self.tmdb.disc[('/discover/movie', '8')] = {'total_results': 501, 'results': []}
        res = self.run_again(state, max_cards=2)
        self.assertTrue(res['full'])
        self.assertEqual(res['cards'], [])

    def test_a_failed_source_changes_nothing_and_is_reported(self):
        state = baseline(self.net, self.tmdb)
        self.net.force[watch.FEED_URLS['plex']] = (503, {}, b'')
        res = self.run_again(state)
        self.assertEqual(res['cards'], [])
        self.assertEqual(len(res['errors']), 1)

    def test_quiet_run_is_under_budget(self):
        state = baseline(self.net, self.tmdb)
        res = self.run_again(state)
        self.assertLessEqual(res['requests'], 250)
        print(f"  quiet run: {res['requests']} requests")

    def test_index_is_rebuilt_only_when_the_deploy_changed(self):
        w, net, _ = make()
        w.index = None
        # a two-card site: cards 'a' (150 titles over two pages) and 'b'
        pages = {'manifest.json': {'catalogs': [{'id': 'sd-a'}, {'id': 'sd-b'}]},
                 'catalog/movie/sd-a.json': {'metas': [{'id': f'tmdb:{i}', 'type': 'movie'} for i in range(100)]},
                 'catalog/movie/sd-a/skip=100.json': {'metas': [{'id': 'tmdb:500', 'type': 'series'}]},
                 'catalog/movie/sd-b.json': {'metas': [{'id': 'tmdb:5', 'type': 'movie'}]}}
        base = net.__call__
        w.http = lambda url, h: (200, {}, json.dumps(pages[url.split('site.test/')[1]]).encode()) if url.split('site.test/')[1] in pages else base(url, h)
        w.refresh_index()
        self.assertEqual(w.index['cards'], ['a', 'b'])
        self.assertEqual((w.index['ids']['m5'], w.index['ids']['t500']), ([0, 1], [0]))
        n = w.requests
        w.refresh_index()
        self.assertEqual(w.requests, n + 1)                     # unchanged deploy: only summary.json

    def test_urls_are_the_ones_the_generator_uses(self):
        src = (ROOT / 'generator' / 'arrivals.py').read_text(encoding='utf-8')
        for url in watch.FEED_URLS.values():
            self.assertIn(url, src)
        self.assertIn("MDBLIST_JSON = 'https://mdblist.com/lists/{list}/json'", (ROOT / 'generator' / 'build.py').read_text(encoding='utf-8'))

    def test_fingerprint_query_is_the_new_cards_discover_query(self):
        """fp_params must equal what build.discover sends for page 1 of the same card (drift guard)."""
        seen = []
        real = build.tmdb
        build.tmdb = lambda path, **p: (seen.append((path, p)), {'results': [], 'total_results': 0, 'total_pages': 0})[1]
        try:
            for slug in ('streaming-netflix-new', 'streaming-tubi-new', 'streaming-mubi-new'):
                card = copy.deepcopy(next(c for c in SPEC['catalogs'] if c['slug'] == slug))
                for media in ('movie', 'series'):
                    seen.clear()
                    build.discover(card, media, 5, SPEC['defaults'])
                    path, p = seen[0]
                    p = {k: v for k, v in p.items() if k != 'page'}
                    self.assertEqual(p, watch.fp_params(card, media, SPEC['defaults'], build.TODAY), (slug, media))
        finally:
            build.tmdb = real


if __name__ == '__main__':
    unittest.main()
