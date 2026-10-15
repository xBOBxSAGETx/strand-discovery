"""Practice builds skip the first_seen logger (#73). No network:

    python -m unittest tests.test_practice_skip_logger

health.py: a skipped logger is fine ONLY in a practice build (the decision step's output), otherwise still a problem.
build.yml: the logger step carries the skip condition, the decision steps exist, a skip needs the EXACT-key restore of
main's newest state to have hit, and only main saves state. Needs PyYAML (requirements-dev.txt).
"""
import os, re, sys, tempfile, unittest
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

    def ids(self):
        return [s.get('id') for s in self.steps]

    def test_logger_step_has_the_skip_condition(self):
        # #77: a partial rebuild (non-empty `cards` -> SD_CARDS) never runs the logger either
        self.assertEqual(self.step('logger')['if'], "steps.practice.outputs.skip != 'true' && env.SD_CARDS == ''")
        self.assertEqual(self.step('arrivals')['if'], "env.SD_CARDS == '' || contains(env.SD_CARDS, '-new')")   # #79
        self.assertEqual(self.wf['jobs']['build']['env']['SD_CARDS'], '${{ inputs.cards }}')

    def test_decision_steps_run_in_order_before_the_logger(self):
        i = self.ids()
        self.assertLess(i.index('state_cache'), i.index('practice_candidate'))
        self.assertLess(i.index('practice_candidate'), i.index('main_state'))
        self.assertLess(i.index('main_state'), i.index('practice'))
        self.assertLess(i.index('practice'), i.index('logger'))

    def test_candidate_needs_manual_run_deploy_off_not_main_and_no_full_logger(self):
        step = self.step('practice_candidate')
        run = step['run']
        gate = run.split('then', 1)[0]            # the outer `if [ ... ]; then` of the script
        for need in ('"$EVENT" = workflow_dispatch', '"$DEPLOY" = false', '"$REF" != refs/heads/main',
                     '"$FULL_LOGGER" != true'):
            self.assertIn(need, gate)
        self.assertEqual(run.count('candidate=true'), 1)       # the only place that can set it is inside that gate
        self.assertGreater(run.index('candidate=true'), run.index('then'))
        self.assertTrue(run.lstrip().startswith('candidate=false'))   # default is "run the logger"
        env = step['env']
        self.assertEqual(env['EVENT'], '${{ github.event_name }}')
        self.assertEqual(env['DEPLOY'], '${{ inputs.deploy }}')
        self.assertEqual(env['REF'], '${{ github.ref }}')
        self.assertEqual(env['FULL_LOGGER'], '${{ inputs.full_logger }}')
        self.assertIn('actions/caches?ref=refs/heads/main&key=sd-state-', run)   # main's newest state key
        self.assertIn('sort=created_at&direction=desc&per_page=1', run)
        self.assertEqual(self.wf['jobs']['build']['permissions']['actions'], 'read')   # the caches list API

    def test_exact_key_restore_of_mains_state_has_no_restore_keys(self):
        step = self.step('main_state')
        self.assertEqual(step['if'], "steps.practice_candidate.outputs.candidate == 'true'")
        self.assertTrue(step['uses'].startswith('actions/cache/restore@'))
        self.assertEqual(step['uses'], self.step('state_cache')['uses'])          # same pinned sha
        self.assertEqual(step['with']['key'], '${{ steps.practice_candidate.outputs.main_key }}')
        self.assertEqual(step['with']['path'], 'state')
        self.assertNotIn('restore-keys', step['with'])

    def test_skip_needs_the_exact_restore_to_hit_and_the_state_file(self):
        step = self.step('practice')
        run = step['run']
        gate = run.split('then', 1)[0]
        for need in ('"$CANDIDATE" = true', '"$MAIN_HIT" = true', '-f state/first_seen.json.gz'):
            self.assertIn(need, gate)
        self.assertEqual(step['env']['MAIN_HIT'], '${{ steps.main_state.outputs.cache-hit }}')
        self.assertEqual(step['env']['CANDIDATE'], '${{ steps.practice_candidate.outputs.candidate }}')
        self.assertEqual(run.count('skip=true'), 1)
        self.assertGreater(run.index('skip=true'), run.index('then'))
        self.assertTrue(run.lstrip().startswith('skip=false'))
        self.assertIn('using main\'s saved state $MAIN_KEY', run)              # prints the exact key restored
        self.assertNotIn('cache-matched-key', run)                              # never trusts the first restore

    def test_logger_code_changes_and_build_yml_changes_do_not_skip(self):
        pattern = self.step('practice_candidate')['run'].split('grep -qxE', 1)[1].split('\n', 1)[0].split("'")[1]
        hit = lambda f: bool(re.fullmatch(pattern, f))
        for f in ('generator/first_seen.py', 'generator/arrivals.py', '.github/workflows/build.yml'):
            self.assertTrue(hit(f), f)
        # shared build.py / spec.json / awards.json changes must NOT block the skip (they show in Generate catalogs)
        for f in ('generator/build.py', 'generator/spec.json', 'generator/awards.json', 'README.md'):
            self.assertFalse(hit(f), f)

    def test_full_logger_input_exists(self):
        inp = self.wf[True]['workflow_dispatch']['inputs']['full_logger']      # PyYAML reads `on:` as True
        self.assertEqual((inp['type'], inp['default']), ('boolean', False))

    def test_only_main_saves_state(self):
        for name in ('Save first_seen state (cache)', 'Encrypt first_seen state for the artifact backup',
                     'Save first_seen state (artifact backup)'):
            cond = next(s for s in self.steps if s.get('name') == name)['if']
            self.assertIn("github.ref == 'refs/heads/main'", cond)
            self.assertIn("steps.logger.outcome == 'success'", cond)

    def test_alert_passes_the_practice_flag_to_health(self):
        run = next(s for s in self.wf['jobs']['alert']['steps'] if 'health.py' in s.get('run', ''))['run']
        self.assertIn("needs.build.outputs.practice", run)


if __name__ == '__main__':
    unittest.main()
