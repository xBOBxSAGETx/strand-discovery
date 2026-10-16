"""#85: only a FAILED build or deploy emails (a new comment, or a new issue). Routine warnings go to the run summary and the
issue body (an edit); a successful run closes the issue without a comment. Offline: health.py dry-run + mock mode.

    python -m unittest tests.test_health_failure_only
"""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

GEN = Path(__file__).resolve().parents[1] / 'generator'
WARN = {'warnings': ['britbox: no new arrivals for 7+ days'], 'skipped': []}
OPEN = {'number': 38, 'body': 'old body <!-- health-sig:000000000000 -->', 'comments': []}


class HealthEmailsOnlyOnFailure(unittest.TestCase):
    def run_health(self, build, deploy='skipped', warnings=True, issues=(OPEN,)):
        d = Path(tempfile.mkdtemp(prefix='sd-health-'))
        (d / 'report' / 'state').mkdir(parents=True)
        (d / 'report' / 'build.log').write_text('GUARD: total items dropped 1000 -> 600\n', encoding='utf-8')
        (d / 'report' / 'summary.json').write_text('{}', encoding='utf-8')
        if warnings:
            (d / 'report' / 'state' / 'fs_summary.json').write_text(json.dumps(WARN), encoding='utf-8')
        mock = d / 'issues.json'
        mock.write_text(json.dumps({'health': list(issues), 'reminder': []}), encoding='utf-8')
        summary = d / 'step_summary.md'
        env = dict(os.environ, HEALTH_DRY_RUN='1', HEALTH_MOCK_ISSUES=str(mock), GITHUB_REPOSITORY='o/r',
                   GITHUB_RUN_ID='1', GITHUB_STEP_SUMMARY=str(summary), PYTHONIOENCODING='utf-8')
        r = subprocess.run([sys.executable, str(GEN / 'health.py'), str(d / 'report'), build, 'success', 'success',
                            'false', deploy], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = summary.read_text(encoding='utf-8') if summary.exists() else ''
        return r.stdout, text

    def test_build_failure_posts_exactly_one_comment(self):
        out, summ = self.run_health('failure')
        self.assertEqual(out.count('WOULD RUN: gh issue comment 38'), 1, out)
        self.assertNotIn('gh issue close', out)
        self.assertIn('Build job ended', summ)

    def test_deploy_failure_posts_exactly_one_comment(self):
        out, _ = self.run_health('success', deploy='failure')
        self.assertEqual(out.count('WOULD RUN: gh issue comment 38'), 1, out)
        self.assertIn('Deploy job ended', out)

    def test_failure_with_no_open_issue_opens_one(self):
        out, _ = self.run_health('failure', issues=())
        self.assertEqual(out.count('WOULD RUN: gh issue create'), 1, out)

    def test_warnings_only_no_comment_body_edited_and_closed(self):
        out, summ = self.run_health('success')
        self.assertNotIn('gh issue comment', out)
        self.assertNotIn('--comment', out)
        self.assertNotIn('gh issue create', out)
        self.assertEqual(out.count('WOULD RUN: gh issue edit 38 --body'), 1, out)
        self.assertIn('britbox: no new arrivals', out)            # warning kept in the body
        self.assertIn('WOULD RUN: gh issue close 38', out)
        self.assertIn('britbox: no new arrivals', summ)            # and in the run summary

    def test_warnings_only_without_open_issue_posts_nothing(self):
        out, summ = self.run_health('success', issues=())
        self.assertNotIn('WOULD RUN', out)
        self.assertIn('britbox: no new arrivals', summ)

    def test_clean_run_closes_without_comment(self):
        out, _ = self.run_health('success', warnings=False)
        self.assertNotIn('gh issue comment', out)
        self.assertNotIn('--comment', out)
        self.assertNotIn('gh issue edit', out)
        self.assertEqual(out.count('WOULD RUN: gh issue close 38'), 1, out)


if __name__ == '__main__':
    unittest.main()
