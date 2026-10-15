"""accuracy_check.report() acts only on its OWN issue (title == TITLE), not on other issues that carry the
'accuracy' label (#30, #33, #35, #37). Offline: health.py's dry-run + mock mode stands in for gh.

    python -m unittest tests.test_accuracy_report
"""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

GEN = Path(__file__).resolve().parent.parent / 'generator'
OTHER = {'number': 37, 'title': 'arrivals: something else'}
OWN = {'number': 99, 'title': 'Weekly accuracy check'}


def run(issues, problems):
    with tempfile.TemporaryDirectory() as d:
        mock = Path(d) / 'mock.json'
        mock.write_text(json.dumps({'accuracy': issues}), encoding='utf-8')
        env = dict(os.environ, HEALTH_DRY_RUN='1', HEALTH_MOCK_ISSUES=str(mock), GITHUB_REPOSITORY='o/r',
                   PYTHONIOENCODING='utf-8')
        code = f"import accuracy_check as a; a.report({problems!r}, ['row'], 'http://run')"
        r = subprocess.run([sys.executable, '-c', code], cwd=GEN, env=env, capture_output=True, text=True, encoding='utf-8')
    assert r.returncode == 0, r.stderr
    return r.stdout


class AccuracyReportOwnIssue(unittest.TestCase):
    def test_clean_run_closes_only_own_issue(self):
        out = run([OTHER, OWN], [])
        self.assertIn('gh issue close 99', out)
        self.assertNotIn('close 37', out)

    def test_problem_run_comments_on_own_issue(self):
        out = run([OTHER, OWN], ['p1'])
        self.assertIn('gh issue comment 99', out)
        self.assertNotIn('comment 37', out)

    def test_only_foreign_issue_open_creates_new(self):
        out = run([OTHER], ['p1'])
        self.assertIn('gh issue create --title Weekly accuracy check', out)
        self.assertNotIn('comment 37', out)

    def test_clean_with_only_foreign_issue_closes_nothing(self):
        out = run([OTHER], [])
        self.assertNotIn('gh issue close', out)
        self.assertIn('clean, no open issue', out)


if __name__ == '__main__':
    unittest.main()
