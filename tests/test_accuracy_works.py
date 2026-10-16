"""Issue #94: Studios + Movie series accuracy (generator/accuracy_works.py), truth = Wikidata. Stubbed Wikidata and TMDB
(no network). The truth mapping (Wikidata rows -> 'movie:id' keys, released-only, shorts-free ids only) and the gap labels
are checked with seeded controls: a clean card scores 100 / 100; a film cut by the vote floor is a 'deliberate rule', a film
TMDB credits to no studio company is a 'source limit', a title on the card that breaks its own floor is a 'code bug', a
card at its depth is a 'cap'.

    python -m unittest tests.test_accuracy_works
"""
import json, os, sys, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import accuracy_works as aw  # noqa: E402

TODAY = '2026-10-15'
SPEC = {'defaults': {'depth': {'popular': 3, 'release_asc': 3}, 'without_keywords': '111', 'deny': []}, 'catalogs': []}
STUDIO = {'slug': 'studio-x', 'kind': 'discover', 'folders': ['studios'], 'media': ['movie'], 'sort': 'popular',
          'min_votes': 10, 'movie': {'with_companies': '7'}}


def row(tid, title, date='2015-01-01', links=10, kind=None):
    r = {'f': {'value': f'http://www.wikidata.org/entity/Q{tid}'}, 'title': {'value': title}, 'tmdbid': {'value': str(tid)},
         'sitelinks': {'value': str(links)}}
    if date:
        r['released'] = {'value': date + 'T00:00:00Z'}
    if kind:
        r['kind'] = {'value': kind}
    return r


def det(tid, votes=500, date='2015-01-01', companies=(7,), types=(4,), name=None):
    return {'id': tid, 'title': name or f'F{tid}', 'vote_count': votes, 'runtime': 100, 'release_date': date, 'genres': [],
            'keywords': {'keywords': []}, 'production_companies': [{'id': c} for c in companies],
            'release_dates': {'results': [{'release_dates': [{'type': t} for t in types]}]}}


class TruthMapping(unittest.TestCase):
    def test_rows_become_keys_released_only(self):
        rows = [row(1, 'A', links=5), row(2, 'Future', date='2027-01-01'), {**row(3, 'NoId'), 'tmdbid': {'value': 'abc'}},
                row(1, 'A again', links=9), row(4, 'NoDate', date=None), row(5, 'Show', kind='series')]
        t = aw.parse_truth(rows, TODAY)
        self.assertEqual(sorted(t), ['movie:1', 'movie:4', 'series:5'])      # future + non-numeric id dropped
        self.assertEqual(t['movie:1']['name'], 'A again')                    # the most-linked row of a repeated id wins
        self.assertEqual(t['movie:1']['qid'], 'Q1')
        self.assertEqual(aw.top_k(t, 2), ['movie:4', 'series:5'])           # most linked first, ties by key

    def test_lookup_parse(self):
        rows = [{'id': {'value': '9'}, 'f': {'value': 'http://x/Q99'}, 'v0': {'value': 'Acme; Beta'}},
                {'id': {'value': '8'}, 'f': {'value': 'http://x/Q88'}}]
        got = aw.parse_lookup(rows, ('P272', 'P750'))
        self.assertEqual((got[9]['qid'], got[9]['statements'], got[9]['by']['P272']), ('Q99', 'Acme; Beta', ('Acme; Beta', set())))
        self.assertEqual((got[8]['qid'], got[8]['statements']), ('Q88', ''))

    def test_distributor_only_reason(self):
        wd = {'qid': 'Q9', 'statements': 'Other; Studio X', 'by': {'P272': ('Other', {'Q5'}), 'P750': ('Studio X', {'Q7'})}}
        lab, why = aw.classify_studio_extra(aw.detail_facts(lambda *a, **k: det(9), 'movie', 9), aw.af.card_rules(
            'popular', STUDIO, 'movie', SPEC), TODAY, wd, ('Q7',))
        self.assertEqual(lab, 'source limit')
        self.assertIn('distributor only (production: Other)', why)

    def test_every_card_has_a_wikidata_item(self):
        spec = json.loads((Path(aw.HERE) / 'spec.json').read_text(encoding='utf-8'))
        ind, listed = aw.pick_studio_cards(spec)
        self.assertEqual((len(ind), len(listed)), (13, 5))                    # the 18 studio cards, split by how they are built
        ser = aw.pick_series_cards(spec)
        self.assertEqual(len(ser), 13)
        self.assertEqual(ser, aw.pick_series_cards(spec))                     # seeded: stable
        for c in ind + listed + ser:
            self.assertIn(c['slug'], {**aw.STUDIOS, **aw.LISTED, **aw.SERIES})
        self.assertEqual(sum(1 for c in ser if c['kind'] == 'discover'), 3)  # the three universe cards are always in


class Stub:
    def __init__(self, details):
        self.details = details

    def tmdb(self, path, **params):
        return self.details[int(path.rsplit('/', 1)[1])]


def run(order, truth, details, wd=None, card=STUDIO, listed=False, hand=(), excluded=None, kind='studio', n=20):
    s = Stub(details)
    return aw.measure(kind, [card], SPEC, lambda slug: order, s.tmdb, TODAY, n, 'seed', truth_for=lambda c: truth,
                      lookup=lambda media, ids: {i: (wd or {})[i] for i in ids if i in (wd or {})},
                      excluded=excluded or {}, hand_for=lambda slug: set(hand))


class StudioLabels(unittest.TestCase):
    def test_clean_card_scores_100(self):
        truth = aw.parse_truth([row(1, 'A'), row(2, 'B')], TODAY)
        r, gaps = run(['movie:1', 'movie:2'], truth, {})
        self.assertEqual((r['recall'], r['precision'], gaps), (100.0, 100.0, []))

    def test_each_gap_gets_its_label(self):
        truth = aw.parse_truth([row(1, 'OnCard'), row(2, 'FewVotes'), row(3, 'NoCredit'), row(4, 'Paged'), row(5, 'Cut')], TODAY)
        details = {2: det(2, votes=3), 3: det(3, companies=(99,)), 4: det(4), 5: det(5),
                   9: det(9, votes=2), 10: det(10), 11: det(11)}                                     # 9: on the card, under its own floor
        wd = {9: {'qid': 'Q9', 'statements': '', 'by': {}}}
        r, gaps = run(['movie:1', 'movie:9'], truth, details, wd=wd)
        by = {g['key']: g['label'] for g in gaps}
        self.assertEqual(by['movie:2'], 'deliberate rule')                  # vote floor
        self.assertEqual(by['movie:3'], 'source limit')                     # TMDB has no company credit
        self.assertEqual(by['movie:4'], 'source limit')                     # passes every rule, under cap: paging
        self.assertEqual(by['movie:9'], 'code bug')                         # on the card, breaks its own floor
        self.assertEqual((r['found'], r['truth']), (1, 5))
        # cap: same card at its depth of 3 -> the passing film is cut
        r2, g2 = run(['movie:1', 'movie:10', 'movie:11'], aw.parse_truth([row(1, 'A'), row(4, 'Paged')], TODAY),
                     {4: det(4), 10: det(10), 11: det(11)}, wd={10: {'qid': 'Q', 'statements': 'Acme', 'by': {'P272': ('Acme', {'Q1'})}}, 11: {'qid': 'Q', 'statements': 'Acme', 'by': {'P272': ('Acme', {'Q1'})}}})
        self.assertEqual({g['key']: g['label'] for g in g2 if g['side'] == 'recall'}, {'movie:4': 'cap'})
        self.assertTrue(all('disagrees' in g['reason'] for g in g2 if g['side'] == 'precision'))

    def test_dead_tmdb_id_is_a_source_limit_not_a_crash(self):
        truth = aw.parse_truth([row(1, 'A'), row(2, 'Dead')], TODAY)

        def tmdb(path, **p):
            if path.endswith('/2'):
                raise RuntimeError('TMDB HTTPError on /movie/2')
            return det(1)
        orig, aw.time.sleep = aw.time.sleep, lambda s: None
        try:
            r, gaps = aw.measure('studio', [STUDIO], SPEC, lambda slug: ['movie:1'], tmdb, TODAY, 20, 'seed',
                                 truth_for=lambda c: truth, lookup=lambda m, ids: {}, excluded={}, hand_for=lambda s: set())
        finally:
            aw.time.sleep = orig
        self.assertEqual([(g['key'], g['label']) for g in gaps], [('movie:2', 'source limit')])

    def test_listed_card_hand_list_and_exclude(self):
        truth = aw.parse_truth([row(1, 'Stale'), row(2, 'Excluded'), row(3, 'Listed')], TODAY)
        details = {1: det(1, companies=(99,)), 2: det(2, companies=(99,)), 3: det(3, companies=(99,))}
        r, gaps = run(['movie:3'], truth, details, listed=True, kind='studio_listed', hand={3}, excluded={'studio-x': {2}})
        by = {g['key']: g['label'] for g in gaps}
        self.assertEqual(by, {'movie:1': 'code bug', 'movie:2': 'deliberate rule'})   # stale hand list / exclude list


class SeriesLabels(unittest.TestCase):
    FR = {'slug': 'series-x', 'kind': 'franchise', 'folders': ['series'], 'media': ['movie'], 'collection_ids': [55], 'ids': []}

    def test_collection_card(self):
        truth = aw.parse_truth([row(1, 'One'), row(2, 'InCollection'), row(3, 'Spinoff'), row(4, 'Recent', date='2026-01-01')], TODAY)
        d2 = {**det(2), 'belongs_to_collection': {'id': 55}}
        d3 = {**det(3), 'belongs_to_collection': None}
        d4 = det(4, date='2026-01-01')
        r, gaps = run(['movie:1'], truth, {2: d2, 3: d3, 4: d4}, card=self.FR, kind='series')
        by = {g['key']: g['label'] for g in gaps}
        self.assertEqual(by, {'movie:2': 'code bug', 'movie:3': 'source limit', 'movie:4': 'source limit'})
        self.assertEqual((r['found'], r['truth']), (1, 4))


if __name__ == '__main__':
    unittest.main()
