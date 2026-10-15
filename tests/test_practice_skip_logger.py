"""Practice builds skip the first_seen logger (#73). No network:

    python -m unittest tests.test_practice_skip_logger

health.py: a skipped logger is fine ONLY in a practice build (the decision step's output), otherwise still a problem.
build.yml: the logger step carries the skip condition, the decision step exists, and its script cannot skip on a
deploy, a schedule or main. Needs PyYAML (requirements-dev.txt).
"""
import os, sys, tempfile, unittest
from pathlib import Path

import yaml

os.environ.setdefault('GITHUB_REPOSITORY', 'owner/repo')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'generator'))
import health  # noqa: E402


class HealthSkippedLogger(unittest.TestCase):
    def found(self, logger, practice=None):
        with tempfile.TemporaryDirectory() as d:
            args = (Path(d), 'success', logger, 'success') + (() if practice is None else (practice,))
            return health.problems(*args)[0]

    def test_skipped_logger_in_practice_build_is_fine(self):
        self.assertFalse([p for p in self.found('skipped', True) if 'logger' in p])

    def test_skipped_logger_in_other_runs_is_still_a_problem(self):
        self.assertTrue([p for p in self.found('skipped') if 'logger' in p])
        self.assertTrue([p for p in self.found('skipped', False) if 'logger' in p])

    def test_failed_logger_is_a_problem_even_in_practice_build(self):
        self.assertTrue([p for p in self.found('failure', True) if 'logger' in p])

    def test_success_is_fine(self):
        self.assertFalse([p for p in self.found('success') if 'logger' in p])


class BuildYmlSkipCondition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = yaml.safe_load((ROOT / '.github' / 'workflows' / 'build.yml').read_text(encoding='utf-8'))
        cls.steps = cls.wf['jobs']['build']['steps']

    def step(self, step_id):
        return next(s for s in self.steps if s.get('id') == step_id)

    def test_logger_step_has_the_skip_condition(self):
        self.assertEqual(self.step('logger')['if'], "steps.practice.outputs.skip != 'true'")

    def test_decision_step_exists_before_the_logger(self):
        ids = [s.get('id') for s in self.steps]
        self.assertLess(ids.index('practice'), ids.index('logger'))

    def test_skip_needs_manual_run_deploy_off_not_main_and_a_cache_hit(self):
        run = self.step('practice')['run']
        gate = run.split('then', 1)[0]            # the outer `if [ ... ]; then` of the script
        for need in ('"$EVENT" = workflow_dispatch', '"$DEPLOY" = false', '"$REF" != refs/heads/main',
                     '-n "$CACHE_HIT"', '-f state/first_seen.json.gz'):
            self.assertIn(need, gate)
        self.assertEqual(run.count('skip=true'), 1)       # the only place that can skip is inside that gate
        self.assertGreater(run.index('skip=true'), run.index('then'))
        self.assertTrue(run.lstrip().startswith('skip=false'))   # default is "run the logger"
        env = self.step('practice')['env']
        self.assertEqual(env['EVENT'], '${{ github.event_name }}')
        self.assertEqual(env['DEPLOY'], '${{ inputs.deploy }}')
        self.assertEqual(env['REF'], '${{ github.ref }}')
        self.assertIn('cache-matched-key', env['CACHE_HIT'])

    def test_logger_code_changes_and_build_yml_changes_do_not_skip(self):
        run = self.step('practice')['run']
        pattern = run.split('grep -qxE', 1)[1].split('\n', 1)[0]
        for f in ('first_seen', 'arrivals'):
            self.assertIn(f, pattern)
        self.assertIn('build\\.yml', pattern)
        # shared build.py / spec.json / awards.json changes must NOT block the skip (they show in Generate catalogs)
        for f in ('build\\.py', 'spec', 'awards'):
            self.assertNotIn(f, pattern.replace('build\\.yml', ''))

    def test_full_logger_input_never_skips(self):
        inp = self.wf[True]['workflow_dispatch']['inputs']['full_logger']      # PyYAML reads `on:` as True
        self.assertEqual((inp['type'], inp['default']), ('boolean', False))
        run = self.step('practice')['run']
        self.assertIn('"$FULL_LOGGER" != true', run.split('then', 1)[0])
        self.assertEqual(self.step('practice')['env']['FULL_LOGGER'], '${{ inputs.full_logger }}')

    def test_pattern_matches_like_the_script(self):
        import re
        pattern = self.step('practice')['run'].split('grep -qxE', 1)[1].split('\n', 1)[0].split("'")[1]
        hit = lambda f: bool(re.fullmatch(pattern, f))
        for f in ('generator/first_seen.py', 'generator/arrivals.py', '.github/workflows/build.yml'):
            self.assertTrue(hit(f), f)
        for f in ('generator/build.py', 'generator/spec.json', 'generator/awards.json', 'README.md'):
            self.assertFalse(hit(f), f)

    def test_save_steps_still_need_logger_success(self):
        for name in ('Save first_seen state (cache)', 'Encrypt first_seen state for the artifact backup'):
            self.assertEqual(next(s for s in self.steps if s.get('name') == name)['if'],
                             "steps.logger.outcome == 'success'")

    def test_alert_passes_the_practice_flag_to_health(self):
        run = next(s for s in self.wf['jobs']['alert']['steps'] if 'health.py' in s.get('run', ''))['run']
        self.assertIn("needs.build.outputs.practice", run)


if __name__ == '__main__':
    unittest.main()
