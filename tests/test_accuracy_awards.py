"""Issue #97: award-card accuracy (generator/accuracy_awards.py), truth = Wikidata P166 (and the Wikipedia list for MDBList
cards). Stubbed Wikidata (no network). Lessons of the earlier audits are pinned: the truth is a P279* scope, one property per
query with no union, the label service "en,mul", a title Wikidata has no statement for is UNJUDGED (never wrong), and only a
positive contradiction (a NOMINEE-only statement confirmed on the title's own entity) is a precision gap.

    python -m unittest tests.test_accuracy_awards
"""
import os, sys, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import accuracy_awards as aa  # noqa: E402

SLUG = 'awards-golden-globe-best-picture'
CARD = {'slug': SLUG, 'media': ['movie'], 'kind': 'idlist'}
SPEC = {'defaults': {'deny': []}, 'catalogs': [CARD]}
SCOPE = {'Q1011509', 'Q670282'}
LISTS = {'golden-globe': {'ids': [], 'unresolved': []}}


def entry(name, keys, awards=('Q1011509',)):
    return {'name': name, 'keys': set(keys), 'awards': set(awards)}


def fake_wd(truth, lookup, entities=None):
    """A Wikidata stand-in: truth = {Q: entry}, lookup = {tmdb id: parse_lookup entry}, entities = {Q: P166 award Qs}."""
    entities = entities or {}
    return {'scope': lambda roots: set(SCOPE), 'truth': lambda cfg, scope, medias: truth,
            'lookup': lambda cfg, media, ids: {i: lookup[i] for i in ids if i in lookup},
            'entity': lambda q: {'entities': {q: {'claims': {'P166': [{'mainsnak': {'datavalue': {'value': {'id': a}}}}
                                                                      for a in entities.get(q, ())]}}}},
            'wikitext': lambda page: ''}


def run(order, truth, lookup, entities=None):
    saved = dict(aa.CARDS)                       # restore the patched module global
    try:
        aa.CARDS[SLUG] = {'roots': ['Q1011509'], 'props': ('P166',), 'list': 'golden-globe'}
        return aa.measure_card(CARD, order, SPEC, LISTS, fake_wd(truth, lookup, entities), {})
    finally:
        aa.CARDS.clear()
        aa.CARDS.update(saved)


class Queries(unittest.TestCase):
    def test_scope_is_a_subclass_path_alone(self):
        q = aa.scope_query(['Q1011509'])
        self.assertIn('wdt:P279*', q)
        self.assertNotIn('UNION', q)

    def test_truth_query_one_property_no_union_label_en_mul(self):
        for via in (None, 'P179'):
            q = aa.truth_query({'Q1'}, 'P166', 'series', via)
            self.assertNotIn('UNION', q)
            self.assertIn('"en,mul"', q)
            self.assertNotIn('FILTER(lang', q)           # no filter on English labels

    def test_parse_truth_keeps_both_tmdb_ids_of_one_item(self):
        rows = [{'f': {'value': 'http://x/entity/Q5'}, 'title': {'value': 'Push'}, 'ids': {'value': '1 2'}, 'awards': {'value': 'Q9'}}]
        self.assertEqual(aa.parse_truth(rows, 'movie')['Q5']['keys'], {'movie:1', 'movie:2'})


class Measure(unittest.TestCase):
    def test_clean_card_is_100_100(self):
        (row, gaps), = run(['movie:1'], {'Q1': entry('A', ['movie:1'])}, {})
        self.assertEqual((row['recall'], row['precision'], gaps), (100.0, 100.0, []))

    def test_winner_missing_from_the_card_is_a_recall_gap(self):
        (row, gaps), = run(['movie:1'], {'Q1': entry('A', ['movie:1']), 'Q2': entry('B', ['movie:2'])}, {})
        self.assertEqual(row['recall'], 50.0)
        self.assertEqual([(g['side'], g['key']) for g in gaps], [('recall', 'movie:2')])

    def test_no_statement_is_unjudged_never_wrong(self):
        # CONTROL for the first run of this audit (Golden Globe 27%): a title with only OTHER awards, one with none and one
        # with no item must all be unjudged, not precision gaps.
        lookup = {2: {'qid': 'Q20', 'win': {'Q99'}, 'nom': set(), 'awards': {'Q99'}},
                  3: {'qid': 'Q30', 'win': set(), 'nom': set(), 'awards': set()}}
        (row, gaps), = run(['movie:1', 'movie:2', 'movie:3', 'movie:4'], {'Q1': entry('A', ['movie:1'])}, lookup)
        self.assertEqual((row['unjudged'], row['sample'], row['precision'], gaps), (3, 1, 100.0, []))

    def test_nominee_only_is_a_gap(self):
        lookup = {2: {'qid': 'Q20', 'win': set(), 'nom': {'Q1011509'}, 'awards': {'Q1011509'}}}
        (row, gaps), = run(['movie:1', 'movie:2'], {'Q1': entry('A', ['movie:1'])}, lookup, entities={'Q20': []})
        self.assertEqual((row['precision_disagrees'], row['precision']), (1, 50.0))
        self.assertTrue(gaps[0]['reason'].startswith('disagrees'))

    def test_nominee_only_is_cleared_when_the_entity_has_a_winner_statement(self):
        # the winner statement sits at a non-best rank: the wdt: lookup missed it, the entity read finds it
        lookup = {2: {'qid': 'Q20', 'win': set(), 'nom': {'Q1011509'}, 'awards': {'Q1011509'}}}
        (row, gaps), = run(['movie:1', 'movie:2'], {'Q1': entry('A', ['movie:1'])}, lookup, entities={'Q20': ['Q1011509']})
        self.assertEqual((row['precision_disagrees'], row['precision'], gaps), (0, 100.0, []))


class Labels(unittest.TestCase):
    CTX = {'excluded': set(), 'out': {'Q1321674'}, 'ref': None, 'unresolved': [], 'wiki': ''}

    def test_out_of_scope_award_is_a_deliberate_rule(self):
        self.assertEqual(aa.classify_missing(entry('H', ['movie:5'], ['Q1321674']), self.CTX)[0], 'deliberate rule')

    def test_wikipedia_lists_it_but_awards_json_lacks_it_is_a_code_bug(self):
        ctx = dict(self.CTX, wiki="| ''[[Blood Brother (film)|Blood Brother]]'' || x")
        self.assertEqual(aa.classify_missing(entry('Blood Brother', ['movie:155556']), ctx)[0], 'code bug')

    def test_unlisted_title_is_a_source_limit(self):
        self.assertEqual(aa.classify_missing(entry('Push', ['movie:13455']), self.CTX)[0], 'source limit')

    def test_listed_in_ignores_running_text_and_longer_titles(self):
        self.assertFalse(aa.listed_in('the film Push was shown', 'Push'))
        self.assertFalse(aa.listed_in("''Pushing Daisies''", 'Push'))
        self.assertTrue(aa.listed_in("''Push'' (2009)", 'Push'))


if __name__ == '__main__':
    unittest.main()
