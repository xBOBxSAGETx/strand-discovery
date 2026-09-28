"""first_seen's own /discover pager vs TMDB's per-page caching (issue #25), stubbed TMDB (no network):

    python -m unittest discover -s tests

TMDB caches every /discover page on its own, so the pages of one query can come from different snapshots: an id
repeats on two pages and another is never returned. For first_seen that means a title "absent" one day and back the
next - a false removal, or a false ARRIVAL when the hidden day was the baseline. The stub serves such stale pages for
first_seen's sort (release date asc) and consistent pages for the reverse sort. The real enumerate_catalogue(),
build.fill_reverse() and main() run; only build.tmdb() is replaced. Runs of main() go through a subprocess (TODAY,
SD_FS_ONLY and the module state are per process), like tests/no_flood_test.py.
"""
import json, os, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path

GEN = Path(__file__).resolve().parents[1] / 'generator'
SLUG = 'acorn-tv'                       # any subscription provider of the spec; its real query params are used

STUB = textwrap.dedent('''
    import json, os, sys
    sys.path.insert(0, os.environ['GEN'])
    import build, first_seen as fs
    cfg = json.loads(os.environ['CFG'])      # {media: {"n": pool size, "hidden": [...]}}, "nofill": bool
    calls = []

    def pool(media):
        n = cfg[media]['n']
        key = 'release_date' if media == 'movie' else 'first_air_date'
        return [{'id': (i if media == 'movie' else 10000 + i), 'title': f'T{i}', 'name': f'T{i}',
                 key: f'{1990 + i // 12:04d}-{i % 12 + 1:02d}-{i % 27 + 1:02d}', 'popularity': i,
                 'genre_ids': [18]} for i in range(1, n + 1)]

    def stub(path, **params):
        build._calls[0] += 1
        calls.append({'path': path, **params})
        if not path.startswith('/discover/'):
            raise RuntimeError('stub: no network')
        media = 'movie' if path.endswith('movie') else 'series'
        rows = pool(media)                               # true order = release date asc
        field, _, way = params['sort_by'].rpartition('.')
        hidden = set(cfg[media]['hidden'])
        if way == 'desc':
            rows = rows[::-1]                            # the reverse pass: fresh, consistent
        else:                                            # stale pages: a hidden id's slot repeats the id before it
            # (so two hidden ids must not be adjacent)
            rows = [rows[n - 1] if r['id'] in hidden and n else r for n, r in enumerate(rows)]
        pg, total = params['page'], len(rows)
        return {'page': pg, 'results': [dict(r) for r in rows[(pg - 1) * 20:pg * 20]],
                'total_results': total, 'total_pages': -(-total // 20)}

    build.tmdb = stub
    if cfg.get('nofill'):                                # seeded control: the pre-fix pager (no reverse pass)
        build.fill_reverse = lambda *a, **k: (0, 0)
    sys.argv = ['first_seen.py', os.environ['STATE']]
    fs.main()
    json.dump(calls, open(os.path.join(os.environ['STATE'], 'calls.json'), 'w'))
''')


def run(state, day, cfg):
    env = dict(os.environ, SD_TODAY=day, SD_FS_ONLY=SLUG, CFG=json.dumps(cfg), STATE=str(state), GEN=str(GEN),
               TMDB_API_KEY='unused-in-tests', SD_CACHE=str(state.parent / 'cache'), SD_BUILD_REQUESTS_EST='0',
               PYTHONIOENCODING='utf-8')
    r = subprocess.run([sys.executable, '-c', STUB], env=env, capture_output=True, text=True, cwd=state.parent)
    if r.returncode:
        raise AssertionError(f'first_seen run {day} failed:\n{r.stdout[-1500:]}\n{r.stderr[-3000:]}')
    s = json.loads((state / 'fs_summary.json').read_text(encoding='utf-8'))
    cands = json.loads((state / 'new_candidates.json').read_text(encoding='utf-8')).get(SLUG, [])
    calls = json.loads((state / 'calls.json').read_text(encoding='utf-8'))
    return s, s['providers'][SLUG], cands, calls


def cfg(hm=(), hs=(), nofill=False):
    return {'movie': {'n': 95, 'hidden': list(hm)}, 'series': {'n': 61, 'hidden': [10000 + i for i in hs]},
            'nofill': nofill}


FULL = 95 + 61


class FirstSeenFill(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix='sd-fs-fill-')) / 'state'

    def test_hidden_ids_recovered_full_pool(self):
        s, p, _, calls = run(self.state, '2026-10-01', cfg(hm=(21, 40, 77), hs=(22, 50)))
        self.assertEqual(p['size'], FULL)                        # every title, despite 5 hidden ids
        self.assertEqual(p['fill_recovered'], 5)
        self.assertEqual(s['discover_fill'], {'pages': p['fill_pages'], 'recovered': 5})
        rev = [c for c in calls if c['sort_by'].endswith('.desc')]
        self.assertEqual(len(rev), p['fill_pages'])
        self.assertLessEqual(len(rev), 5 + 4)                    # bounded by the pool's pages (5 movie + 4 tv)
        fwd = [c for c in calls if c['sort_by'].endswith('.asc')]
        # the fill keeps first_seen's own query: same params apart from the sort direction
        strip = lambda c: {k: v for k, v in c.items() if k not in ('page', 'sort_by')}
        for media in ('movie', 'tv'):
            f = {json.dumps(strip(c), sort_keys=True) for c in fwd if c['path'].endswith(media)}
            r = {json.dumps(strip(c), sort_keys=True) for c in rev if c['path'].endswith(media)}
            self.assertEqual(len(f), 1)
            if r:
                self.assertEqual(r, f)

    def test_no_holes_no_extra_requests(self):
        _, p, _, calls = run(self.state, '2026-10-01', cfg())
        self.assertEqual(p['size'], FULL)
        self.assertEqual(p['fill_pages'], 0)
        self.assertEqual(len(calls), 5 + 4)                      # the forward pages only
        self.assertFalse([c for c in calls if c['sort_by'].endswith('.desc')])

    def test_two_runs_no_false_absence_or_arrival(self):
        # day 1 = baseline with some ids hidden; day 2 hides DIFFERENT ids; day 3 clean
        _, p1, _, _ = run(self.state, '2026-10-01', cfg(hm=(21, 40), hs=(22,)))
        s2, p2, c2, _ = run(self.state, '2026-10-02', cfg(hm=(60,), hs=(41, 45)))
        _, p3, c3, _ = run(self.state, '2026-10-03', cfg())
        self.assertEqual((p1['size'], p2['size'], p3['size']), (FULL, FULL, FULL))
        self.assertEqual((p2['adds'], p2['removals']), (0, 0))
        self.assertEqual((p3['adds'], p3['removals']), (0, 0))
        self.assertEqual(c2 + c3, [])                            # nothing dated as a new arrival
        self.assertFalse(s2.get('rebaselined'))                  # the query signature is unchanged by the fill

    def test_control_prefix_pager_shows_the_bug(self):
        # seeded negative control: the same days with the reverse pass disabled must show the false signals
        _, p1, _, _ = run(self.state, '2026-10-01', cfg(hm=(21, 40), hs=(22,), nofill=True))
        _, p2, c2, _ = run(self.state, '2026-10-02', cfg(hm=(60,), hs=(41, 45), nofill=True))
        self.assertEqual(p1['size'], FULL - 3)
        self.assertEqual(p2['removals'], 3)                      # 60, 41, 45 "left" the service
        self.assertEqual(p2['adds'], 3)                          # 21, 40, 22 hidden on the baseline -> "arrivals"
        self.assertEqual(sorted(c['id'] for c in c2), ['tmdb:10022', 'tmdb:21', 'tmdb:40'])


if __name__ == '__main__':
    unittest.main()
