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


def settle(row, gaps, entries, order=(), audited=('streaming-x',), reason=None, built=None):
    return ac.settle([row], gaps, entries, set(audited), lambda slug: list(order), reason or (lambda slug, key: None), built=built)


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


class Report(Base):
    def test_recall_line_for_a_non_theme_row_with_filled_does_not_crash(self):   # critic: KeyError 'corrections' on any non-theme kind
        row = af.summarize('studio', [CARD], 9, 10, 5, 5, [], 1)               # as accuracy_works builds it: filled=1, no 'corrections'
        text = '\n'.join(ac.recall_lines([row]))
        self.assertIn('studio recall: 90.0 with our own lists, 80.0 from TMDB alone', text)
        self.assertNotIn('corrections applied', text)
        theme = dict(row, kind='theme', corrections={'found': 2, 'total': 3})
        self.assertIn('corrections applied 2/3', '\n'.join(ac.recall_lines([theme])))
        self.assertEqual(ac.recall_lines([af.summarize('studio', [CARD], 9, 10, 5, 5, [], 0)]), [])     # nothing filled: no line

    def test_pending_add_that_is_also_a_claim_gap_counts_once(self):
        row, gaps = measure(['movie:1'], [1, 4], {('movie', 1): movie(1), ('movie', 4): movie(4)})        # 4: a claim gap AND an add
        out = ac.settle([row], gaps, {('streaming-x', 'movie:4'): entry(4, 'add')}, {'streaming-x'}, lambda s: ['movie:1'],
                        lambda s, k: None, '2026-10-16')
        self.assertEqual(out['pending'], [{'card': 'streaming-x', 'key': 'movie:4'}])
        row, gaps = measure(['movie:1'], [1], {('movie', 1): movie(1)})                                    # an add outside the claim list: still once
        out = ac.settle([row], gaps, {('streaming-x', 'movie:77'): entry(77, 'add')}, {'streaming-x'}, lambda s: ['movie:1'],
                        lambda s, k: None, '2026-10-16')
        self.assertEqual(len(out['pending']), 1)


class BuildRule(Base):
    def test_deliberate_rule_comes_from_the_builds_own_check(self):
        import build
        spec = {'defaults': {'deny': []}, 'catalogs': [CARD]}
        det = {'id': 4, 'title': 'M4', 'release_date': '2020-01-01', 'vote_count': 3, 'genres': [], 'runtime': 100,
               'release_dates': {'results': [{'release_dates': [{'type': 4}]}]}, 'keywords': {'keywords': []}}
        on_service = {'results': {'US': {'flatrate': [{'provider_id': 8}]}}}

        def fake(extra=None):                       # like TMDB: the providers block comes back only when it was asked for
            def get(path, **kw):
                d = dict(det, **(extra or {}))
                return dict(d, **{'watch/providers': on_service}) if 'watch/providers' in kw.get('append_to_response', '') else d
            return get
        reason = ac.build_rule_reason(spec, fake())
        self.assertEqual(reason('streaming-x', 'movie:4'),
                         build.correction_rule(CARD, spec['defaults'], 'movie', build.correction_details(CARD, 'movie', 4, fetch=fake())))
        self.assertEqual(reason('streaming-x', 'movie:4'), 'min_votes')
        self.assertIsNone(ac.build_rule_reason(spec, fake({'vote_count': 900}))('streaming-x', 'movie:4'))

    def test_check_asks_tmdb_for_what_the_build_asks(self):
        """#131 x #133: the check fetched without watch/providers, so every add on a service card would read
        'no watch/providers data' (deliberate rule) while the build, which asks for it, took the title."""
        import build
        calls = []
        ac.build_rule_reason({'defaults': {}, 'catalogs': [CARD]}, lambda path, **kw: calls.append((path, kw)) or {'id': 0})('streaming-x', 'movie:4')
        build_calls = []
        build.correction_details(CARD, 'movie', 4, fetch=lambda path, **kw: build_calls.append((path, kw)) or {})
        self.assertEqual(calls, build_calls)
        self.assertIn('watch/providers', calls[0][1]['append_to_response'])


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


class SourceSides(Base):
    """The stand-up / network check flags titles on two more sides: 'outside_recall' (a second source lists it, TMDB does not
    tag it) and 'truth_precision' (Wikidata disagrees with the card). They settle like recall / precision."""
    LABS = {l: 0 for l in af.LABELS}

    def row(self, side, lab='source limit'):
        key = {'outside_recall': 'outside', 'truth_precision': 'truth_precision'}[side]
        gaps = {**self.LABS, lab: 1}
        r = {'kind': 'standup', 'filled': 0, 'recall_gaps': dict(self.LABS), 'precision_gaps': dict(self.LABS), 'code_bugs': 0,
             key: {'gaps': gaps}}
        g = [{'kind': 'standup', 'card': 'streaming-x', 'side': side, 'key': 'movie:9', 'name': 'M9', 'label': lab, 'reason': 'r'}]
        return r, g

    def test_outside_recall_keep_is_kept(self):
        row, gaps = self.row('outside_recall')
        out = settle(row, gaps, {('streaming-x', 'movie:9'): entry(9, 'keep', 'TMDB does not tag it')})
        self.assertEqual((gaps, row['open'], len(row['kept']), row['outside']['gaps']['source limit']), ([], 0, 1, 0))
        self.assertEqual(out['kept'][0]['side'], 'outside_recall')

    def test_outside_recall_without_an_entry_stays_open(self):                          # CONTROL
        row, gaps = self.row('outside_recall')
        settle(row, gaps, {})
        self.assertEqual((len(gaps), row['open'], row['outside']['gaps']['source limit']), (1, 1, 1))

    def test_outside_recall_add_is_relabelled_and_bumps_the_outside_counter(self):
        row, gaps = self.row('outside_recall')
        settle(row, gaps, {('streaming-x', 'movie:9'): entry(9, 'add', added='2026-01-01')}, built='2026-10-01')
        self.assertEqual(gaps[0]['label'], 'code bug')
        self.assertEqual((row['outside']['gaps']['source limit'], row['outside']['gaps']['code bug']), (0, 1))
        self.assertEqual(row['recall_gaps']['code bug'], 0)

    def test_outside_recall_add_newer_than_the_build_is_pending(self):
        row, gaps = self.row('outside_recall')
        out = settle(row, gaps, {('streaming-x', 'movie:9'): entry(9, 'add')}, built='2026-10-16')
        self.assertEqual((gaps, row['open']), ([], 0))
        self.assertEqual(out['pending'], [{'card': 'streaming-x', 'key': 'movie:9'}])

    def test_truth_precision_remove_is_fixed(self):
        row, gaps = self.row('truth_precision')
        settle(row, gaps, {('streaming-x', 'movie:9'): entry(9, 'remove')})
        self.assertEqual((gaps, row['open'], row['fixed_by_list'], row['truth_precision']['gaps']['source limit']), ([], 0, 1, 0))

    def test_truth_precision_add_is_fixed_and_keep_is_kept(self):
        row, gaps = self.row('truth_precision')
        settle(row, gaps, {('streaming-x', 'movie:9'): entry(9, 'add')})
        self.assertEqual((gaps, row['fixed_by_list']), ([], 1))
        row, gaps = self.row('truth_precision')
        settle(row, gaps, {('streaming-x', 'movie:9'): entry(9, 'keep', 'w')})
        self.assertEqual((gaps, len(row['kept'])), ([], 1))

    def test_other_sides_are_left_alone(self):
        row, gaps = self.row('truth_precision')
        gaps[0]['side'] = 'must_have'
        settle(row, gaps, {('streaming-x', 'movie:9'): entry(9, 'keep')})
        self.assertEqual(len(gaps), 1)


if __name__ == '__main__':
    unittest.main()
