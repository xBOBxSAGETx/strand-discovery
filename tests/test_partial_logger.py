"""A watcher-started partial rebuild of streaming New cards sees today's catalogue (#79, critic of aa0f705):

    python -m unittest tests.test_partial_logger

A title that just landed on a service must reach that service's New card in a PARTIAL rebuild, without losing the other
services' New cards. No network: first_seen.enumerate_catalogue is stubbed.

1. first_seen.py with SD_FS_ONLY=<service> adds the new title to that service's new_candidates.json entry, keeps the other
   services' entries (and fs_summary entries) as saved, and build.first_seen_card then shows the title.
2. build.yml: a step `partial_logger` runs before the arrival step in a partial, passes SD_FS_ONLY, takes the services from
   the streaming-<service>-new slugs only; the arrival step needs it; the three state-save steps need a full build.
"""
import datetime as dt, gzip, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

import yaml

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(tempfile.mkdtemp(prefix='sd-tests-')) / 'sd_cache')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'generator'))
import build  # noqa: E402
import first_seen  # noqa: E402

TODAY = dt.date(2026, 10, 15)
YDAY = (TODAY - dt.timedelta(days=1)).isoformat()


def row(i, name, pop):
    return {'id': i, 'title': name, 'popularity': pop, 'genre_ids': [], 'release_date': '2025-01-01', 'poster_path': None}


class PartialLogger(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='sd-state-'))
        self.saved = (first_seen.enumerate_catalogue, first_seen.TODAY, sys.argv[:], os.environ.get('SD_FS_ONLY'))
        first_seen.TODAY = TODAY
        first_seen.enumerate_catalogue = self.enumerate

    def tearDown(self):
        first_seen.enumerate_catalogue, first_seen.TODAY, sys.argv[:], old = self.saved
        sys.argv[:] = self.saved[2]
        os.environ.pop('SD_FS_ONLY', None)
        if old is not None:
            os.environ['SD_FS_ONLY'] = old

    def enumerate(self, p, media, dflt):
        if media == 'series':
            return {}, False
        # mubi today: the two titles it had last night plus one that just landed (id 3)
        return {1: row(1, 'Old One', 10), 2: row(2, 'Old Two', 20), 3: row(3, 'Just Landed', 5)}, False

    def state(self):
        os.environ['SD_FS_ONLY'] = 'mubi'
        sig = first_seen.query_signature(first_seen.providers()[0])
        old = (TODAY - dt.timedelta(days=40)).isoformat()
        mk = lambda items: {'baseline': old, 'last_add': None, 'last_run': YDAY, 'prev_run': YDAY, 'ids': sig, 'items': items}
        st = {'providers': {'mubi': mk({'m:1': ['baseline', YDAY], 'm:2': ['baseline', YDAY]}),
                            'netflix': mk({'m:9': ['baseline', YDAY]})}, 'runs': [YDAY]}
        with gzip.open(self.dir / 'first_seen.json.gz', 'wt', encoding='utf-8') as fh:
            json.dump(st, fh)
        (self.dir / 'new_candidates.json').write_text(json.dumps({
            'netflix': [{'id': 'tmdb:9', 'type': 'movie', 'name': 'Netflix Arrival', 'first_seen': YDAY, 'popularity': 7}],
            'mubi': []}), encoding='utf-8')
        (self.dir / 'fs_summary.json').write_text(json.dumps({'providers': {
            'netflix': {'size': 1, 'mode': 'first_seen'}, 'mubi': {'size': 2, 'mode': 'first_seen'}}, 'warnings': []}), encoding='utf-8')

    def test_new_title_reaches_the_new_card_and_other_services_keep_theirs(self):
        self.state()
        sys.argv[:] = ['first_seen.py', str(self.dir)]
        os.environ['SD_FS_ONLY'] = 'mubi'
        first_seen.main()
        cands = json.loads((self.dir / 'new_candidates.json').read_text(encoding='utf-8'))
        summary = json.loads((self.dir / 'fs_summary.json').read_text(encoding='utf-8'))
        self.assertEqual([c['id'] for c in cands['mubi']], ['tmdb:3'])
        self.assertEqual(cands['mubi'][0]['first_seen'], TODAY.isoformat())
        self.assertEqual([c['id'] for c in cands['netflix']], ['tmdb:9'])        # the other service keeps its candidates
        self.assertEqual(summary['providers']['netflix'], {'size': 1, 'mode': 'first_seen'})   # ...and its summary entry
        self.assertEqual(summary['providers']['mubi']['adds'], 1)
        # and that is what the New card is built from
        saved = build.FS_SUMMARY, build.FS_CANDIDATES, build.SIGNALS
        build.FS_SUMMARY, build.FS_CANDIDATES, build.SIGNALS = summary, cands, {}
        try:
            card = {'slug': 'streaming-mubi-new', 'provider': 'mubi'}
            self.assertTrue(build.first_seen_mode(card))
            ids = [m['id'] for m in build.first_seen_card(card, 50)]
            netflix = [m['id'] for m in build.first_seen_card({'slug': 'streaming-netflix-new', 'provider': 'netflix'}, 50)]
        finally:
            build.FS_SUMMARY, build.FS_CANDIDATES, build.SIGNALS = saved
        self.assertEqual(ids, ['tmdb:3'])
        self.assertEqual(netflix, ['tmdb:9'])

    def test_second_same_day_run_still_sees_the_change(self):
        # the nightly already ran today (last_run == today, prev_run == yesterday); a partial runs the logger again
        self.state()
        f = self.dir / 'first_seen.json.gz'
        st = json.load(gzip.open(f, 'rt', encoding='utf-8'))
        st['providers']['mubi']['last_run'] = TODAY.isoformat()
        st['providers']['mubi']['items'] = {'m:1': ['baseline', TODAY.isoformat()], 'm:2': ['baseline', TODAY.isoformat()]}
        with gzip.open(f, 'wt', encoding='utf-8') as fh:
            json.dump(st, fh)
        sys.argv[:] = ['first_seen.py', str(self.dir)]
        os.environ['SD_FS_ONLY'] = 'mubi'
        first_seen.main()
        cands = json.loads((self.dir / 'new_candidates.json').read_text(encoding='utf-8'))
        self.assertEqual([c['id'] for c in cands['mubi']], ['tmdb:3'])


class WorkflowSteps(unittest.TestCase):
    wf = yaml.safe_load((ROOT / '.github' / 'workflows' / 'build.yml').read_text(encoding='utf-8'))
    steps = wf['jobs']['build']['steps']

    def ids(self):
        return [s.get('id') for s in self.steps]

    def test_partial_logger_runs_first_and_feeds_the_arrival_step(self):
        i = self.ids()
        self.assertLess(i.index('logger'), i.index('partial_logger'))
        self.assertLess(i.index('partial_logger'), i.index('arrivals'))
        step = self.steps[i.index('partial_logger')]
        self.assertEqual(step['if'], "env.SD_CARDS != ''")
        self.assertIn('SD_FS_ONLY="$providers" python generator/first_seen.py state', step['run'])
        self.assertIn("^streaming-.+-new$", step['run'])                   # streaming New cards only
        self.assertEqual(self.steps[i.index('arrivals')]['if'], "env.SD_CARDS == '' || steps.partial_logger.outputs.providers != ''")

    def test_services_are_taken_from_the_streaming_new_slugs_only(self):
        step = next(s for s in self.steps if s.get('id') == 'partial_logger')
        script = step['run'].replace('if [ -n "$providers" ]; then SD_FS_ONLY="$providers" python generator/first_seen.py state; fi', ':')
        bash = next((b for b in ('bash', 'C:/Program Files/Git/usr/bin/bash.exe')
                     if subprocess.run([b, '-c', 'true'], capture_output=True).returncode == 0), None)
        if bash is None:
            self.skipTest('no working bash')
        gh_out = Path(tempfile.mkdtemp(prefix='sd-gh-')) / 'out'

        def out(cards):
            gh_out.write_text('', encoding='utf-8')
            r = subprocess.run([bash, '-c', script], env={**os.environ, 'SD_CARDS': cards, 'GITHUB_OUTPUT': str(gh_out)},
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return gh_out.read_text(encoding='utf-8').strip().replace('providers=', '')
        self.assertEqual(out('streaming-prime-video-new, sd-streaming-mubi-new,streaming-netflix,streaming-netflix-top,genre-horror-new'),
                         'mubi,prime-video')
        self.assertEqual(out('genre-horror-new,studio-a24'), '')

    def test_a_partial_never_saves_state(self):
        saves = [s for s in self.steps if s.get('name') in (
            'Save first_seen state (cache)', 'Encrypt first_seen state for the artifact backup',
            'Save first_seen state (artifact backup)')]
        self.assertEqual(len(saves), 3)
        for s in saves:
            self.assertIn("env.SD_CARDS == ''", s['if'], s['name'])


if __name__ == '__main__':
    unittest.main()
