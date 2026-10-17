"""Issue #92: the shared accuracy frame (generator/accuracy_frame.py) scores recall + precision of a Popular / Top card
and labels every gap. Stubbed TMDB (no network). Seeded controls: a clean card scores 100 / 100 with no gap; a card with
a title that breaks its own vote floor shows up as a 'code bug', a claim title cut by the vote floor as a 'deliberate
rule', a title cut at the depth cap as a 'cap', an unexplained missing title as a 'source limit'.

    python -m unittest tests.test_accuracy_frame
"""
import os, sys, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import accuracy_frame as af  # noqa: E402

TODAY = '2026-10-15'
SPEC = {'defaults': {'depth': {'popular': 3, 'top': 3}, 'without_keywords': '111|222', 'without_keywords_tv': '333', 'deny': []},
        'catalogs': []}
PROV = {'watch_region': 'US', 'with_watch_providers': '8', 'with_watch_monetization_types': 'flatrate'}
POP = {'slug': 'streaming-x', 'kind': 'discover', 'folders': ['streaming'], 'sort': 'popular', 'min_votes': 10,
       'movie': PROV, 'series': PROV}
TOP = {'slug': 'streaming-x-top', 'kind': 'discover', 'folders': ['streaming-top'], 'sort': 'top',
       'min_votes': {'movie': 300, 'series': 150}, 'min_age_days': 180, 'movie': PROV, 'series': PROV}


def movie(tid, votes=5000, runtime=100, date='2020-01-01', on=True, kw=(), types=(4,)):
    return {'id': tid, 'title': f'M{tid}', 'vote_count': votes, 'runtime': runtime, 'release_date': date,
            'genres': [], 'keywords': {'keywords': [{'id': k} for k in kw]},
            'release_dates': {'results': [{'release_dates': [{'type': t} for t in types]}]},
            'watch/providers': {'results': {'US': {'flatrate': [{'provider_id': 8 if on else 9}]}}}}


def series(tid, votes=5000, status='Ended', date='2015-01-01', on=True):
    return {'id': tid, 'name': f'S{tid}', 'vote_count': votes, 'status': status, 'first_air_date': date, 'genres': [],
            'keywords': {'results': []}, 'watch/providers': {'results': {'US': {'flatrate': [{'provider_id': 8 if on else 9}]}}}}


class Stub:
    """tmdb(path, **params): /discover/* returns the claim list, /movie|tv/{id} returns the details."""
    def __init__(self, truth_movies, truth_series, details):
        self.truth = {'movie': truth_movies, 'tv': truth_series}
        self.details = details

    def __call__(self, path, **params):
        parts = path.strip('/').split('/')
        if parts[0] == 'discover':
            return {'results': [{'id': i, 'title': f'M{i}'} for i in self.truth[parts[1]]] if params.get('page', 1) == 1 else []}
        return self.details[(parts[0], int(parts[1]))]


def run(card, order, stub, kind='popular', n=50):
    return af.measure(kind, [card], SPEC, lambda slug: order, stub, TODAY, n, 'seed', truth_k=20)


class Frame(unittest.TestCase):
    def test_clean_card_is_100_100(self):
        stub = Stub([1, 2], [10], {('movie', 1): movie(1), ('movie', 2): movie(2), ('tv', 10): series(10)})
        row, gaps = run(POP, ['movie:1', 'series:10', 'movie:2'], stub)
        self.assertEqual((row['recall'], row['precision'], gaps), (100.0, 100.0, []))

    def test_missing_titles_are_labelled(self):
        stub = Stub([1, 2, 3, 4], [], {('movie', 1): movie(1),
                                        ('movie', 2): movie(2, votes=4),                  # under the 10-vote floor
                                        ('movie', 3): movie(3, types=(1, 2)),             # theatrical only
                                        ('movie', 4): movie(4)})                          # passes every rule
        row, gaps = run(POP, ['movie:1'], stub)
        got = {g['key']: g['label'] for g in gaps if g['side'] == 'recall'}
        self.assertEqual(got, {'movie:2': 'deliberate rule', 'movie:3': 'deliberate rule', 'movie:4': 'source limit'})
        self.assertEqual((row['found'], row['truth'], row['recall']), (1, 4, 25.0))

    def test_cap_label(self):
        det = {('movie', i): movie(i) for i in (1, 2, 3, 4, 9)}
        row, gaps = run(POP, ['movie:1', 'movie:2', 'movie:3'], Stub([1, 2, 3, 4], [], det))   # depth 3 reached, rank 4 is beyond it
        self.assertEqual([g['label'] for g in gaps if g['side'] == 'recall'], ['cap'])
        self.assertEqual(row['at_cap'], ['streaming-x:movie'])
        _, gaps = run(POP, ['movie:1', 'movie:2', 'movie:3'], Stub([9], [], det))             # rank 1 cannot be cut by a cap of 3
        self.assertEqual([g['label'] for g in gaps if g['side'] == 'recall'], ['source limit'])

    def test_precision_breaks_are_labelled(self):
        stub = Stub([], [], {('movie', 1): movie(1),
                              ('movie', 2): movie(2, votes=3),                # on the card against its own floor
                              ('movie', 3): movie(3, on=False)})              # discover said yes, watch/providers says no
        row, gaps = run(POP, ['movie:1', 'movie:2', 'movie:3'], stub)
        got = {g['key']: g['label'] for g in gaps if g['side'] == 'precision'}
        self.assertEqual(got, {'movie:2': 'code bug', 'movie:3': 'source limit'})
        self.assertEqual((row['ok'], row['sample'], row['precision'], row['code_bugs']), (1, 3, 33.3, 1))

    def test_top_rules(self):
        stub = Stub([1, 2, 3], [], {('movie', 1): movie(1, votes=200),                       # 100..299 votes: floor 300
                                     ('movie', 2): movie(2, date='2026-09-01'),             # under 180 days old
                                     ('movie', 3): movie(3, runtime=12)})                   # a short
        _, gaps = run(TOP, [], stub, kind='top')
        self.assertEqual({g['key']: g['label'] for g in gaps}, {'movie:1': 'deliberate rule', 'movie:2': 'deliberate rule',
                                                                'movie:3': 'deliberate rule'})
        self.assertIn('floor 300', gaps[0]['reason'])
        self.assertIn('180 days', gaps[1]['reason'])
        self.assertIn('runtime 12', gaps[2]['reason'])

    def test_keyword_and_tv_status_rules(self):
        r = af.card_rules('popular', POP, 'series', SPEC)
        self.assertEqual(r['keywords_out'], {111, 222, 333})
        self.assertEqual(af.card_rules('popular', POP, 'movie', SPEC)['keywords_out'], {111, 222})
        f = af.facts(series(5, status='In Production'), 'series')
        self.assertTrue(any('status' in b for b in af.rule_breaks(f, 'series', r, TODAY)))

    def test_pick_cards_is_seeded_and_filtered(self):
        spec = {'catalogs': [dict(POP, slug=f'p{i}') for i in range(9)] + [dict(TOP, slug='t0')]}
        a, b = af.pick_cards(spec, 'popular', 4, 's1'), af.pick_cards(spec, 'popular', 4, 's1')
        self.assertEqual([c['slug'] for c in a], [c['slug'] for c in b])
        self.assertEqual(len(a), 4)
        self.assertNotIn('t0', [c['slug'] for c in a])


UNION = {'slug': 'streaming-x', 'kind': 'union', 'folders': ['streaming'], 'sort': 'popular', 'min_votes': 10, 'cap': 2000,
         'provider': 'x', 'media': ['movie', 'series'], 'movie': PROV, 'series': PROV,
         'parts': [{'slug': 'streaming-x', 'sort': 'popular', 'min_votes': 10},
                   {'slug': 'streaming-x-new', 'sort': 'new', 'min_votes': 5, 'provider': 'x'},
                   {'slug': 'streaming-x-top', 'sort': 'top', 'min_votes': {'movie': 300, 'series': 150}, 'min_age_days': 180}]}


class UnionCard(unittest.TestCase):
    """#143: the weekly accuracy judges a union card's title by the loosest of its parts (Popular + New + Top Rated)."""
    def judge(self, card, det, media='movie'):
        return af.classify_extra(af.facts(det, media), media, card, af.card_rules('popular', card, media, SPEC), TODAY)

    def test_a_3_vote_new_arrival_on_a_union_card_is_not_a_code_bug(self):
        self.assertIsNone(self.judge(UNION, movie(1, votes=3, date='2026-10-10')))        # new, 5 days old, 3 votes
        self.assertIsNone(self.judge(UNION, series(2, votes=3, date='2026-10-10'), 'series'))

    def test_control_the_same_title_on_a_plain_popular_card_is_a_code_bug(self):
        lab, why = self.judge(POP, movie(1, votes=3, date='2026-10-10'))
        self.assertEqual(lab, 'code bug')
        self.assertIn('floor 10', why)

    def test_the_loosest_part_sets_the_rules_and_real_breaks_still_show(self):
        r = af.card_rules('popular', UNION, 'movie', SPEC)
        self.assertEqual((r['min_votes'], r['min_age_days']), (0, 0))
        self.assertEqual(af.card_rules('popular', dict(UNION, parts=[UNION['parts'][0], UNION['parts'][2]]), 'movie', SPEC)['min_votes'], 10)
        self.assertEqual(self.judge(UNION, movie(3, date='2027-01-01'))[0], 'code bug')       # unreleased stays a bug


if __name__ == '__main__':
    unittest.main()
