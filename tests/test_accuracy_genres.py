"""Issue #95: Genre / Theme accuracy against Wikidata (generator/accuracy_genres.py). Stubbed TMDB and Wikidata, no network.
The truth-source mapping (card -> Wikidata genre classes, Wikidata rows -> TMDB keys, P4947 movie / P4983 TV) is the unit
under test; the card rules the labels use are the ones of build.py (build.genre_fits / main_genres), not copies.
Nothing here pins build.TODAY or any other module global (the date is passed in).

    python -m unittest tests.test_accuracy_genres
"""
import json, os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402
import accuracy_genres as ag  # noqa: E402

TODAY = '2026-10-15'
SPEC = json.loads((Path(build.HERE) / 'spec.json').read_text(encoding='utf-8'))
CARDS = {c['slug']: c for c in SPEC['catalogs']}


class Mapping(unittest.TestCase):
    def test_every_card_has_a_mapping_or_is_listed_as_no_truth(self):
        genre_cards = [c for c in SPEC['catalogs'] if any(f.startswith('genres-') for f in c.get('folders', []))]
        self.assertEqual(len(genre_cards), 63)
        for c in genre_cards:
            self.assertIn(ag.base_slug(c['slug']), ag.GENRES, c['slug'])
            for media in c['media']:                      # a Wikidata class for every medium the card holds
                self.assertTrue(ag.classes_for(c, media), (c['slug'], media))
        themes = ag.pick_cards(SPEC, 'theme', 0, 's')
        self.assertEqual(sorted(c['slug'] for c in themes), sorted(ag.THEMES))
        self.assertEqual(sorted(s for s, q in ag.THEMES.items() if not q), ['theme-myths-and-legends', 'theme-robots-and-ai'])

    def test_sci_fi_series_maps_to_the_tv_class_and_movie_to_the_film_class(self):
        card = CARDS['genre-sci-fi-top']                  # base slug drops the -top / -new suffix
        self.assertEqual(ag.base_slug(card['slug']), 'sci-fi')
        self.assertEqual(ag.classes_for(card, 'movie'), ['Q471839'])
        self.assertEqual(ag.classes_for(card, 'series'), ['Q140472311'])
        self.assertEqual(ag.card_tag(card, 'series'), ('with_genres', {10765}))
        self.assertEqual(ag.card_tag(CARDS['genre-anime'], 'movie'), ('with_keywords', {210024}))
        self.assertEqual(ag.card_tag(CARDS['theme-creature-features'], 'movie')[0], 'with_keywords')

    def test_pick_is_a_third_of_each_sort_and_stable(self):
        a = ag.pick_cards(SPEC, 'genre', 12, 'audit-95')
        self.assertEqual(a, ag.pick_cards(SPEC, 'genre', 12, 'audit-95'))
        self.assertEqual(len(a), 12)
        self.assertEqual(sorted(sum(1 for c in a if c['sort'] == s) for s in ('popular', 'new', 'top')), [4, 4, 4])

    def test_wikidata_rows_become_tmdb_keys(self):
        rows = [{'id': '603', 'sl': '120', 'd': '1999-03-31T00:00:00Z'}, {'id': '9', 'sl': '50'},     # no date: kept
                {'id': '7', 'sl': '40', 'd': '2030-01-01T00:00:00Z'}]                                  # unreleased: dropped
        self.assertEqual(ag.parse_truth(rows, 'movie', TODAY), [('movie:603', 120, '1999-03-31'), ('movie:9', 50, '')])
        j = [{'id': '603', 'hit': '1', 'n': '9'}, {'id': '10', 'hit': '0', 'n': '3'}, {'id': '11', 'hit': '0', 'n': '0'}]
        self.assertEqual(ag.parse_judge(j, 'series'), {'series:603': (9, True), 'series:10': (3, False), 'series:11': (0, False)})
        q = ag.truth_query('series', ['Q343782', 'Q188473'], since='2026-04-18')
        self.assertIn('wdt:P4983', q)
        self.assertIn('VALUES ?G { wd:Q343782 wd:Q188473 }', q)
        self.assertIn('2026-04-18', q)
        self.assertIn('wdt:P4947', ag.judge_query('movie', ['Q188473'], ['1', '2']))
        self.assertIn('VALUES ?id { "1" "2" }', ag.judge_query('movie', ['Q188473'], ['1', '2']))


def det(i, name, genres, votes=900, date='2000-01-01', runtime=120, kw=()):
    return {'id': i, 'title': name, 'genres': [{'id': g} for g in genres], 'vote_count': votes, 'vote_average': 8.2,
            'release_date': date, 'first_air_date': date, 'runtime': runtime, 'keywords': {'keywords': [{'id': k} for k in kw]},
            'release_dates': {'results': [{'release_dates': [{'type': 4}]}]}}


class Labels(unittest.TestCase):
    def setUp(self):
        self.card = CARDS['genre-action']
        self.r = ag.af.card_rules('genre', self.card, 'movie', SPEC)

    def label(self, d):
        return ag.classify_missing(ag.title_facts(d, 'movie'), 'movie', self.card, self.r, TODAY, 100)[0]

    def test_missing_title_labels(self):
        self.assertEqual(self.label(det(1, 'Not tagged', [18])), 'source limit')                 # TMDB has no Action tag
        self.assertEqual(self.label(det(2, 'Few votes', [28], votes=3)), 'deliberate rule')       # under the 10-vote floor
        self.assertEqual(self.label(det(3, 'Third tag', [18, 12, 28])), 'deliberate rule')        # Action must be in the first 2 tags
        self.assertEqual(self.label(det(4, 'Fine', [28, 12])), 'source limit')                    # passes everything, under the cap
        full = ag.classify_missing(ag.title_facts(det(4, 'Fine', [28]), 'movie'), 'movie', self.card, self.r, TODAY, 1000)
        self.assertEqual(full[0], 'cap')

    def test_main_genre_rule_is_the_builds(self):             # the label and the build cannot drift apart
        for tags in ([28], [12, 28], [18, 12, 28], [18]):
            f = ag.title_facts(det(5, 'x', tags), 'movie')
            ours = not any(b.startswith('main-genre') for b in ag.rule_breaks(f, 'movie', self.card, self.r, TODAY))
            self.assertEqual(ours, build.genre_fits(tags, {28}), tags)

    def test_sampled_title_that_breaks_its_own_rule_is_a_code_bug(self):
        f = ag.title_facts(det(6, 'Short', [28], runtime=10), 'movie')
        top = CARDS['genre-action-top']                                                            # Top Rated: 40-minute floor
        r = ag.af.card_rules('genre', top, 'movie', SPEC)
        self.assertEqual(ag.classify_extra(f, 'movie', top, r, TODAY)[0], 'code bug')
        self.assertIsNone(ag.classify_extra(ag.title_facts(det(7, 'Long', [28], runtime=100, date='2020-01-01'), 'movie'), 'movie', top, r, TODAY))


class Measure(unittest.TestCase):
    def test_genre_card_end_to_end(self):
        card = CARDS['genre-horror']                       # movies only
        order = ['movie:1', 'movie:2']
        details = {1: det(1, 'On card', [27]), 2: det(2, 'Disagree', [27]), 3: det(3, 'Missing', [27], votes=2)}

        def tmdb(path, **kw):
            return details[int(path.rsplit('/', 1)[1])]

        def sparql(q):
            if 'SELECT ?id (MAX(?sl0)' in q:
                return [{'id': '1', 'sl': '90'}, {'id': '3', 'sl': '80'}]
            return [{'id': '1', 'hit': '1', 'n': '3'}, {'id': '2', 'hit': '0', 'n': '2'}]
        row, gaps = ag.measure_cards('genre', [card], SPEC, lambda s: order, tmdb, sparql, TODAY, 20, 's')
        self.assertEqual((row['found'], row['truth'], row['recall']), (1, 2, 50.0))
        self.assertEqual((row['sample'], row['ok'], row['precision']), (2, 1, 50.0))
        self.assertEqual(sorted((g['side'], g['label']) for g in gaps), [('precision', 'source limit'), ('recall', 'deliberate rule')])

    def test_title_unknown_to_tmdb_is_a_gap_not_a_crash(self):
        card = CARDS['genre-horror']                       # the first run crashed on a Wikidata id TMDB answers 404 for

        def tmdb(path, **kw):
            raise RuntimeError('TMDB HTTPError on ' + path)

        def sparql(q):
            return [{'id': '3', 'sl': '80'}] if 'SELECT ?id (MAX(?sl0)' in q else []
        row, gaps = ag.measure_cards('genre', [card], SPEC, lambda s: ['movie:1'], tmdb, sparql, TODAY, 20, 's')
        self.assertEqual([(g['side'], g['label']) for g in gaps], [('recall', 'source limit')])
        self.assertEqual(row['unjudged'], 1)

    def test_theme_has_no_precision_truth(self):
        card = CARDS['theme-heist']
        details = {1: det(1, 'Heist', [80], kw=[10051]), 2: det(2, 'No keyword', [80])}

        def tmdb(path, **kw):
            i = int(path.rsplit('/', 1)[1])
            return details.get(i) or det(i, f'Must-have {i}', [80])        # the real must-have ids of the card: released, absent

        def sparql(q):
            if 'SELECT ?id (MAX(?sl0)' in q:
                return [{'id': '1', 'sl': '90'}]
            return [{'id': '1', 'hit': '1', 'n': '2'}, {'id': '2', 'hit': '0', 'n': '2'}]
        row, gaps = ag.measure_cards('theme', [card], SPEC, lambda s: ['movie:1', 'movie:2'], tmdb, sparql, TODAY, 20, 's')
        self.assertIsNone(row['precision'])
        self.assertEqual((row['confirmed'], row['not_confirmed'], row['own_rule']), (1, 1, {'ok': 1, 'n': 2}))
        self.assertEqual([g['label'] for g in gaps if g['side'] == 'precision'], ['code bug'])     # movie 2 lacks the keyword
        n = len([e for e in build.load_hand_list('theme_must_haves')['theme-heist']])
        self.assertEqual((row['must_have']['found'], row['must_have']['total']), (0, n))           # none of them on the stub card
        self.assertTrue(all(g['label'] == 'code bug' for g in gaps if g['side'] == 'must_have'))


if __name__ == '__main__':
    unittest.main()
