"""Issue #129: the weekly accuracy check honours our corrections list (generator/accuracy_corrections.py), stubbed TMDB.
keep resolves a flag (listed apart as "checked and kept"); an added title on its card is "filled by our list", never TMDB
recall; an add that is not on its card is a deliberate rule (the card's own rules drop it) or a code bug; a removed title
still on its card is a code bug and never a precision gap. Each test has a seeded control with no entry: the flag stays.

    python -m unittest tests.test_accuracy_corrections
"""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import accuracy_corrections as ac  # noqa: E402
import accuracy_frame as af  # noqa: E402

TODAY = '2026-10-15'
SPEC = {'defaults': {'depth': {'popular': 50}, 'without_keywords': '111', 'deny': []}, 'catalogs': []}
PROV = {'watch_region': 'US', 'with_watch_providers': '8', 'with_watch_monetization_types': 'flatrate'}
CARD = {'slug': 'streaming-x', 'kind': 'discover', 'folders': ['streaming-popular'], 'sort': 'popular', 'min_votes': 10,
        'movie': PROV, 'series': PROV}


def movie(tid, votes=5000):
    return {'id': tid, 'title': f'M{tid}', 'vote_count': votes, 'runtime': 100, 'release_date': '2020-01-01', 'genres': [],
            'keywords': {'keywords': []}, 'release_dates': {'results': [{'release_dates': [{'type': 4}]}]},
            'watch/providers': {'results': {'US': {'flatrate': [{'provider_id': 8}]}}}}


def tmdb_stub(truth, details):
    def tmdb(path, **params):
        parts = path.strip('/').split('/')
        if parts[0] == 'discover':
            return {'results': [{'id': i, 'title': f'M{i}'} for i in truth] if params.get('page', 1) == 1 and parts[1] == 'movie' else []}
        return details[(parts[0], int(parts[1]))]
    return tmdb


def entry(tid, side, why=None, **kw):
    return {'type': 'movie', 'tmdb': tid, 'title': f'M{tid}', 'year': 2020, 'source': 'https://x', 'added': '2026-10-16', 'side': side,
            **({'why': why} if why else {}), **kw}


def measure(order, truth, details):
    return af.measure('popular', [CARD], SPEC, lambda slug: order, tmdb_stub(truth, details), TODAY, 50, 'seed', truth_k=20)


def settle(row, gaps, entries, order=(), audited=('streaming-x',), reason=None):
    return ac.settle([row], gaps, entries, set(audited), lambda slug: list(order), reason or (lambda slug, key: None))


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = ac.ENTRIES

    def tearDown(self):
        ac.ENTRIES = self._saved


class Keep(Base):
    def test_keep_resolves_a_precision_flag_and_is_listed_apart(self):
        ac.ENTRIES = {}
        order, details = ['movie:1', 'movie:2'], {('movie', 1): movie(1), ('movie', 2): movie(2, votes=3)}   # 2 breaks its own vote floor
        row, gaps = measure(order, [], details)
        out = settle(row, gaps, {})                                                    # CONTROL: no entry -> the flag stays open
        self.assertEqual((row['open'], len(row['kept']), row['code_bugs']), (1, 0, 1))
        row, gaps = measure(order, [], details)
        out = settle(row, gaps, {('streaming-x', 'movie:2'): entry(2, 'keep', 'Wikipedia lists it under this theme')})
        self.assertEqual((row['open'], len(row['kept']), row['code_bugs'], gaps), (0, 1, 0, []))
        self.assertEqual(row['precision_gaps']['code bug'], 0)
        self.assertEqual(out['kept'][0]['why'], 'Wikipedia lists it under this theme')
        text = '\n'.join(ac.lines([row], out['kept']))
        self.assertIn('checked and kept: 1', text)
        self.assertIn('Wikipedia lists it under this theme', text)

    def test_keep_resolves_a_recall_flag(self):
        row, gaps = measure(['movie:1'], [1, 4], {('movie', 1): movie(1), ('movie', 4): movie(4)})     # 4 passes every rule: source limit
        self.assertEqual(len(gaps), 1)
        out = settle(row, gaps, {('streaming-x', 'movie:4'): entry(4, 'keep', 'TMDB paging snapshot, left as is')})
        self.assertEqual((gaps, row['open'], row['recall_gaps']['source limit']), ([], 0, 0))
        self.assertEqual(len(out['kept']), 1)

    def test_keep_on_another_card_does_not_resolve(self):
        row, gaps = measure(['movie:1', 'movie:2'], [], {('movie', 1): movie(1), ('movie', 2): movie(2, votes=3)})
        settle(row, gaps, {('streaming-other', 'movie:2'): entry(2, 'keep')})
        self.assertEqual(row['open'], 1)


class Add(Base):
    def test_added_title_counts_as_filled_not_tmdb_recall(self):
        details = {('movie', 1): movie(1), ('movie', 2): movie(2)}
        ac.ENTRIES = {}                                                                # CONTROL: no entry -> all of it is TMDB's
        row, _ = measure(['movie:1', 'movie:2'], [1, 2], details)
        self.assertEqual((row['recall'], row['filled'], row['tmdb_recall']), (100.0, 0, 100.0))
        ac.ENTRIES = {('streaming-x', 'movie:2'): entry(2, 'add')}
        row, _ = measure(['movie:1', 'movie:2'], [1, 2], details)
        self.assertEqual((row['recall'], row['filled'], row['tmdb_recall']), (100.0, 1, 50.0))
        settle(row, [], ac.ENTRIES)
        self.assertEqual(row['fixed_by_list'], 1)

    def test_add_missing_from_its_card_is_a_deliberate_rule_when_the_rules_drop_it(self):
        ac.ENTRIES = {}
        row, gaps = measure(['movie:1'], [1, 4], {('movie', 1): movie(1), ('movie', 4): movie(4)})
        out = settle(row, gaps, {('streaming-x', 'movie:4'): entry(4, 'add')}, reason=lambda slug, key: '2 votes, floor 10')
        self.assertEqual([(g['key'], g['label']) for g in gaps], [('movie:4', 'deliberate rule')])
        self.assertIn('2 votes, floor 10', gaps[0]['reason'])
        self.assertEqual((row['recall_gaps']['deliberate rule'], row['recall_gaps']['source limit'], row['code_bugs']), (1, 0, 0))
        self.assertEqual(out['problems'], [])

    def test_add_missing_from_its_card_with_no_rule_to_blame_is_a_code_bug(self):
        row, gaps = measure(['movie:1'], [1, 4], {('movie', 1): movie(1), ('movie', 4): movie(4)})
        settle(row, gaps, {('streaming-x', 'movie:4'): entry(4, 'add')})               # reason_of -> None
        self.assertEqual([(g['key'], g['label']) for g in gaps], [('movie:4', 'code bug')])
        self.assertEqual(row['code_bugs'], 1)

    def test_add_newer_than_the_live_build_is_pending_not_a_flag(self):
        for built, flagged in (('2026-10-16', False), ('2026-10-17', True)):         # the second is the CONTROL: build is newer -> a real flag
            row, gaps = measure(['movie:1'], [1, 4], {('movie', 1): movie(1), ('movie', 4): movie(4)})
            out = ac.settle([row], gaps, {('streaming-x', 'movie:4'): entry(4, 'add')}, {'streaming-x'}, lambda s: ['movie:1'],
                            lambda s, k: None, built)
            self.assertEqual((bool(gaps), row['code_bugs']), (flagged, int(flagged)))
            self.assertEqual(bool(out['pending']), not flagged)

    def test_add_outside_the_claim_list_is_checked_too(self):
        row, gaps = measure(['movie:1'], [1], {('movie', 1): movie(1)})
        settle(row, gaps, {('streaming-x', 'movie:77'): entry(77, 'add')}, order=['movie:1'])
        self.assertEqual([(g['kind'], g['key'], g['label']) for g in gaps], [('corrections', 'movie:77', 'code bug')])

    def test_add_flagged_on_precision_side_is_fixed_by_our_list(self):
        row, gaps = measure(['movie:1', 'movie:2'], [], {('movie', 1): movie(1), ('movie', 2): movie(2, votes=3)})
        settle(row, gaps, {('streaming-x', 'movie:2'): entry(2, 'add')})
        self.assertEqual((gaps, row['open'], row['fixed_by_list']), ([], 0, 1))


class Remove(Base):
    def test_removed_title_still_on_its_card_is_a_code_bug_and_a_problem(self):
        row, gaps = measure(['movie:1', 'movie:5'], [], {('movie', 1): movie(1), ('movie', 5): movie(5)})
        out = settle(row, gaps, {('streaming-x', 'movie:5'): entry(5, 'remove')}, order=['movie:1', 'movie:5'])
        self.assertEqual([(g['key'], g['side'], g['label']) for g in gaps], [('movie:5', 'precision', 'code bug')])
        self.assertEqual(len(out['problems']), 1)
        self.assertIn('movie:5', out['problems'][0])

    def test_removed_title_that_is_gone_is_clean(self):                                 # CONTROL for the above
        row, gaps = measure(['movie:1'], [], {('movie', 1): movie(1)})
        out = settle(row, gaps, {('streaming-x', 'movie:5'): entry(5, 'remove')}, order=['movie:1'])
        self.assertEqual((gaps, out['problems']), ([], []))

    def test_removed_title_is_never_a_precision_gap_nor_a_recall_gap(self):
        row, gaps = measure(['movie:1', 'movie:5'], [1, 6], {('movie', 1): movie(1), ('movie', 5): movie(5, votes=2), ('movie', 6): movie(6)})
        self.assertEqual({(g['side'], g['key']) for g in gaps}, {('precision', 'movie:5'), ('recall', 'movie:6')})
        settle(row, gaps, {('streaming-x', 'movie:5'): entry(5, 'remove'), ('streaming-x', 'movie:6'): entry(6, 'remove')},
               order=['movie:1', 'movie:5'])
        self.assertEqual([(g['side'], g['key'], g['label']) for g in gaps], [('precision', 'movie:5', 'code bug')])   # only the one still there


class Loader(unittest.TestCase):
    def test_real_list_loads_by_card_and_key(self):
        got = ac.load()
        self.assertTrue(got)
        (slug, key), e = next(iter(got.items()))
        self.assertEqual(key, f"{e['type']}:{e['tmdb']}")
        self.assertIn(e['side'], ac.SIDES)

    def test_filled_counts_only_adds_on_the_card(self):
        ents = {('c', 'movie:1'): entry(1, 'add'), ('c', 'movie:2'): entry(2, 'keep'), ('c', 'movie:3'): entry(3, 'add')}
        self.assertEqual(ac.filled('c', ['movie:1', 'movie:2', 'movie:3'], {'movie:1', 'movie:2'}, ents), 1)


if __name__ == '__main__':
    unittest.main()
