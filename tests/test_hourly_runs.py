"""The scheduled build runs hourly (#76), not daily. Three daily assumptions, each replayed offline:

    python -m unittest tests.test_hourly_runs

1. first_seen: running its real main() several times on the SAME day (stubbed TMDB) adds nothing twice: a title that
   arrived in run 1 keeps its date and is not an "add" again in run 2; a glitch that hides a title for one run and
   brings it back the same day is not a new arrival; the runs list holds the day once.
2. health.py: one alert per problem streak. The same problems on the next hourly run add no comment (digits ignored);
   a changed set adds one; the first report opens the issue with a signature.
3. arrivals catch-up stall counter: CATCHUP_STALL_RUNS counts DAYS without progress, not runs.
"""
import json, os, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path

GEN = Path(__file__).resolve().parents[1] / 'generator'
SLUG = 'acorn-tv'

STUB = textwrap.dedent('''
    import json, os, sys
    sys.path.insert(0, os.environ['GEN'])
    import build, first_seen as fs
    ids = json.loads(os.environ['IDS'])                 # movie ids present on the service in this run

    def stub(path, **params):
        build._calls[0] += 1
        if not path.startswith('/discover/'):
            raise RuntimeError('stub: no network')
        rows = [{'id': i, 'title': f'T{i}', 'release_date': '2000-01-01', 'popularity': i, 'genre_ids': [18]}
                for i in ids] if path.endswith('movie') else []
        return {'page': 1, 'results': rows, 'total_results': len(rows), 'total_pages': 1 if rows else 0}

    build.tmdb = stub
    sys.argv = ['first_seen.py', os.environ['STATE']]
    fs.main()
''')


def run_logger(state, day, ids):
    env = dict(os.environ, SD_TODAY=day, SD_FS_ONLY=SLUG, IDS=json.dumps(ids), STATE=str(state), GEN=str(GEN),
               TMDB_API_KEY='unused-in-tests', SD_CACHE=str(state.parent / 'cache'), SD_BUILD_REQUESTS_EST='0',
               PYTHONIOENCODING='utf-8')
    r = subprocess.run([sys.executable, '-c', STUB], env=env, capture_output=True, text=True, cwd=state.parent)
    if r.returncode:
        raise AssertionError(f'first_seen run {day} failed:\n{r.stdout[-1500:]}\n{r.stderr[-3000:]}')
    import gzip
    st = json.loads(gzip.open(state / 'first_seen.json.gz', 'rt', encoding='utf-8').read())
    summ = json.loads((state / 'fs_summary.json').read_text(encoding='utf-8'))['providers'][SLUG]
    cands = json.loads((state / 'new_candidates.json').read_text(encoding='utf-8')).get(SLUG, [])
    return st, summ, cands


class FirstSeenSameDay(unittest.TestCase):
    def test_hourly_runs_on_one_day_add_nothing_twice(self):
        state = Path(tempfile.mkdtemp(prefix='sd-hourly-')) / 'state'
        run_logger(state, '2026-10-01', [1, 2, 3])                       # baseline day
        st, s, cands = run_logger(state, '2026-10-02', [1, 2, 3, 4])      # 00:xx run: title 4 arrives
        self.assertEqual((s['adds'], st['providers'][SLUG]['items']['m:4'][0]), (1, '2026-10-02'))
        self.assertEqual([c['id'] for c in cands], ['tmdb:4'])
        st, s, cands = run_logger(state, '2026-10-02', [1, 2, 3, 4])      # next hourly run, nothing changed
        self.assertEqual(s['adds'], 0)
        self.assertEqual([c['id'] for c in cands], ['tmdb:4'])            # listed once, same date
        st, s, cands = run_logger(state, '2026-10-02', [1, 3, 4, 5])      # 2 hidden by a glitch, 5 arrives
        self.assertEqual(s['adds'], 1)
        self.assertEqual(sorted(c['id'] for c in cands), ['tmdb:4', 'tmdb:5'])
        st, s, cands = run_logger(state, '2026-10-02', [1, 2, 3, 4, 5])   # 2 is back the same day
        items = st['providers'][SLUG]['items']
        self.assertEqual(s['adds'], 0)                                    # not a re-arrival
        self.assertEqual(items['m:2'][0], 'baseline')
        self.assertEqual(items['m:4'][0], '2026-10-02')
        self.assertEqual(items['m:5'][0], '2026-10-02')
        self.assertEqual(sorted(c['id'] for c in cands), ['tmdb:4', 'tmdb:5'])
        self.assertEqual(st['runs'], ['2026-10-01', '2026-10-02'])        # the day once, not once per run
        self.assertEqual(sum(1 for k in items), 5)


class HealthOncePerStreak(unittest.TestCase):
    def health(self, log_line, mock_issues):
        d = Path(tempfile.mkdtemp(prefix='sd-health-'))
        (d / 'report').mkdir()
        (d / 'report' / 'build.log').write_text(log_line + '\n', encoding='utf-8')
        mock = d / 'issues.json'
        mock.write_text(json.dumps({'health': mock_issues, 'reminder': []}), encoding='utf-8')
        env = dict(os.environ, HEALTH_DRY_RUN='1', HEALTH_MOCK_ISSUES=str(mock), GITHUB_REPOSITORY='o/r',
                   GITHUB_RUN_ID='1', PYTHONIOENCODING='utf-8')
        r = subprocess.run([sys.executable, str(GEN / 'health.py'), str(d / 'report'), 'failure', 'success', 'success'],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_streak(self):
        # first failure: opens the issue, body carries the signature
        out = self.health('GUARD: total items dropped 1000 -> 600 (>30%)', [])
        self.assertIn('gh issue create', out)
        sig = out.split('health-sig:')[1][:12]
        # next hourly run, same guard with other numbers: no comment
        issue = {'number': 7, 'body': f'first <!-- health-sig:{sig} -->', 'comments': []}
        out = self.health('GUARD: total items dropped 1000 -> 590 (>30%)', [issue])
        self.assertNotIn('WOULD RUN', out)
        self.assertIn('no new comment', out)
        # a different problem on the open issue: one comment
        out = self.health('GUARD: previously non-empty catalogs are now empty: [x]', [issue])
        self.assertIn('WOULD RUN: gh issue comment 7', out)
        # the newest comment's signature wins over an older one
        out2 = out.split('health-sig:')[1][:12]
        issue2 = {'number': 7, 'body': f'<!-- health-sig:{sig} -->', 'comments': [{'body': f'<!-- health-sig:{out2} -->'}]}
        out = self.health('GUARD: previously non-empty catalogs are now empty: [x]', [issue2])
        self.assertIn('no new comment', out)


class StallCountsDays(unittest.TestCase):
    def test_stall_per_day(self):
        os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
        os.environ.setdefault('SD_CACHE', str(Path(tempfile.mkdtemp(prefix='sd-tests-')) / 'sd_cache'))
        sys.path.insert(0, str(GEN))
        import arrivals
        st = {'stalled': 0}
        for _ in range(24):                                   # 24 hourly runs of one day
            arrivals._stall(st, '2026-10-02')
        self.assertEqual(st['stalled'], 1)
        arrivals._stall(st, '2026-10-03')
        self.assertEqual(st['stalled'], 2)
        self.assertLess(st['stalled'], arrivals.CATCHUP_STALL_RUNS)


if __name__ == '__main__':
    unittest.main()
