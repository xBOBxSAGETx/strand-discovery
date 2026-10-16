"""Issue #129: a Wikidata item that carries several TMDB ids (one stale) counts as found when ANY of its ids is on the card.
The old queries kept ONE id per item (MAX in accuracy_people.works_query, MIN in accuracy_works' truth queries), so the stale
id was flagged as a missing title. Stubbed Wikidata and TMDB (no network).

    python -m unittest tests.test_accuracy_ids
"""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import accuracy_people as ap  # noqa: E402
import accuracy_works as aw  # noqa: E402

TODAY = '2026-10-15'


def person_item(ids, q='Q1'):
    """What the fixed works_query returns for one item: ALL its ids in one space-separated cell."""
    return {'w': f'http://www.wikidata.org/entity/{q}', 'sh': '0', 'm': ' '.join(map(str, ids)), 'd': '2000-01-01T00:00:00Z'}


def run_people(rows, order):
    card = {'slug': 'director-x', 'kind': 'director', 'person_id': 77}
    credits = {'crew': [{'id': i, 'title': f'F{i}', 'job': 'Director', 'release_date': '2000-01-01'} for i in (111, 222)]}

    def sparql(q):
        if q.startswith('SELECT ?p WHERE'):
            return [{'p': 'http://www.wikidata.org/entity/Q77'}]
        return rows if 'GROUP BY ?w' in q else []
    tmdb = lambda path, **p: credits if path.endswith('movie_credits') else {'crew': []}
    return ap.measure_people('director', [card], {}, lambda slug: order, tmdb, sparql, TODAY, 5, 20, 'seed')


class PeopleTwoIds(unittest.TestCase):
    def test_query_returns_every_id_not_the_max(self):
        q = ap.works_query('director', 77)
        self.assertNotIn('MAX(?m0)', q)
        self.assertIn('GROUP_CONCAT(DISTINCT STR(?m0)', q)

    def test_item_with_a_stale_id_is_found_by_the_live_one(self):
        row, gaps = run_people([person_item([111, 222])], ['movie:111'])          # 222 is the stale id, 111 is on the card
        self.assertEqual((row['found'], row['truth'], row['recall']), (1, 1, 100.0))
        self.assertEqual([g for g in gaps if g['side'] == 'recall'], [])

    def test_found_by_either_id(self):
        row, _ = run_people([person_item([111, 222])], ['movie:222'])
        self.assertEqual((row['found'], row['truth']), (1, 1))

    def test_control_item_on_the_card_under_neither_id_is_one_missing_title(self):
        row, gaps = run_people([person_item([111, 222])], ['movie:999'])
        self.assertEqual((row['found'], row['truth']), (0, 1))                    # ONE item, counted once, not once per id
        self.assertEqual(len([g for g in gaps if g['side'] == 'recall']), 1)

    def test_parse_keeps_all_ids_of_an_item(self):
        works, unmapped, _ = ap.parse_works([person_item([111, 222])], TODAY)
        self.assertEqual(sorted(works), ['movie:111', 'movie:222'])
        self.assertEqual(unmapped, [])


class WorksTwoIds(unittest.TestCase):
    @staticmethod
    def row(ids, title='A', links=5):
        return {'f': {'value': 'http://www.wikidata.org/entity/Q5'}, 'title': {'value': title}, 'tmdbids': {'value': ' '.join(map(str, ids))},
                'sitelinks': {'value': str(links)}, 'released': {'value': '2015-01-01T00:00:00Z'}}

    def test_queries_return_every_id_not_the_min(self):
        for q in (aw.studio_truth_query(['Q1'], ['P272']), aw.series_truth_query(['Q1'])):
            self.assertNotIn('MIN(?tmdb)', q)
            self.assertNotIn('MIN(?id)', q)
            self.assertIn('GROUP_CONCAT(DISTINCT', q)

    def test_item_is_found_when_any_id_is_on_the_card(self):
        truth = aw.parse_truth([self.row([10, 20])], TODAY)
        self.assertEqual(list(truth), ['movie:10'])
        self.assertEqual(truth['movie:10']['alts'], ['movie:20'])
        res = aw.resolve_alts(truth, {'movie:20'})
        self.assertEqual(list(res), ['movie:20'])                                  # found under the id on the card
        self.assertEqual(aw.af.score(list(res), {'movie:20'})[:2], (1, 1))

    def test_control_neither_id_on_the_card_is_one_missing_item(self):
        res = aw.resolve_alts(aw.parse_truth([self.row([10, 20])], TODAY), {'movie:99'})
        self.assertEqual(aw.af.score(list(res), {'movie:99'})[:2], (0, 1))

    def test_old_single_id_rows_still_parse(self):
        r = dict(self.row([7]), tmdbid={'value': '7'})
        del r['tmdbids']
        self.assertEqual(list(aw.parse_truth([r], TODAY)), ['movie:7'])


if __name__ == '__main__':
    unittest.main()
