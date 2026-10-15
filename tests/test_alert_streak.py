"""Builds can run many times a day now (the watcher starts partial rebuilds, #79). Two assumptions that used to be daily,
each replayed offline (salvaged from branch hourly-builds, #76):

    python -m unittest tests.test_alert_streak

1. health.py: one alert per problem streak. The same problems on the next run add no comment (digits ignored); a changed
   set adds one; the first report opens the issue with a signature.
2. arrivals catch-up stall counter: CATCHUP_STALL_RUNS counts DAYS without progress, not runs.
"""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

GEN = Path(__file__).resolve().parents[1] / 'generator'


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
        # next run, same guard with other numbers: no comment
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
        for _ in range(24):                                   # 24 runs of one day
            arrivals._stall(st, '2026-10-02')
        self.assertEqual(st['stalled'], 1)
        arrivals._stall(st, '2026-10-03')
        self.assertEqual(st['stalled'], 2)
        self.assertLess(st['stalled'], arrivals.CATCHUP_STALL_RUNS)


if __name__ == '__main__':
    unittest.main()
