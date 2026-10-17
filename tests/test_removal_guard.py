"""Shelf cut guard (issue #139): a manual run with allow_catalog_removal may drop shelves on purpose. Before, the >30%
total-items check still counted the removed shelves, so a cut of ~50% of the items was always refused. Now only the
catalogs that remain are compared. No network:

    python -m unittest tests.test_removal_guard

OLD below is the control: the check as it was before #139. It refuses the cut that the new logic passes.
"""
import os, sys, tempfile, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(tempfile.mkdtemp(prefix='sd-tests-')) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402


def OLD(prev, counts, total, allow_removal):
    """The check before #139, verbatim: the allow flag never reached it."""
    if prev.get('total_items') and total < prev['total_items'] * (1 - build.DROP_LIMIT):
        return f"GUARD: total items dropped {prev['total_items']} -> {total} (>30%)"
    return None


PREV = {'total_items': 1000, 'catalogs': {'keep-a': 200, 'keep-b': 200, 'cut-a': 300, 'cut-b': 300}}


class RemovalGuard(unittest.TestCase):
    def test_cut_over_30_percent_with_allow_is_accepted_now_and_was_refused_before(self):
        counts = {'keep-a': 200, 'keep-b': 190}                        # total 1000 -> 390, a 61% drop that comes from the cut
        self.assertIn('GUARD', OLD(PREV, counts, sum(counts.values()), True))        # control: old logic refuses
        self.assertIsNone(build.total_drop_problem(PREV, counts, sum(counts.values()), True))

    def test_same_cut_without_allow_is_still_refused(self):
        counts = {'keep-a': 200, 'keep-b': 190}
        self.assertIn('GUARD', build.total_drop_problem(PREV, counts, sum(counts.values()), False))

    def test_kept_catalogs_dropping_too_much_is_still_refused_with_allow(self):
        counts = {'keep-a': 100, 'keep-b': 100}                        # the 2 kept shelves lost 50% (400 -> 200)
        self.assertIn('GUARD', build.total_drop_problem(PREV, counts, sum(counts.values()), True))

    def test_small_drop_passes_both_ways(self):
        counts = dict(PREV['catalogs'])
        self.assertIsNone(build.total_drop_problem(PREV, counts, 900, False))
        self.assertIsNone(build.total_drop_problem(PREV, counts, 900, True))


if __name__ == '__main__':
    unittest.main()
