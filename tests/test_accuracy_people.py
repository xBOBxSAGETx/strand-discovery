"""Issue #93: Director / Actor accuracy against Wikidata (generator/accuracy_people.py). Stubbed TMDB and Wikidata, no network.
The truth-source mapping (Wikidata rows -> TMDB keys, P4947 movie / P4983 TV) is the unit under test; the card rules the
labels use are cross-checked against build.director / build.actor so they cannot drift apart.

    python -m unittest tests.test_accuracy_people
"""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402
import accuracy_people as ap  # noqa: E402

TODAY = '2026-10-15'
_saved = {}


def setUpModule():                      # pinned only while this module runs; other modules (test_watch) see the real clock
    _saved['v'] = (build.TODAY, build.YEAR_AGO)
    build.TODAY, build.YEAR_AGO = TODAY, '2025-10-15'


def tearDownModule():
    build.TODAY, build.YEAR_AGO = _saved['v']


def wd(q, m=None, t=None, d='2000-01-01T00:00:00Z', sh='0'):
    r = {'w': f'http://www.wikidata.org/entity/{q}', 'sh': sh}
    for k, v in (('m', m), ('t', t), ('d', d)):
        if v:
            r[k] = str(v)
    return r


class Mapping(unittest.TestCase):
    def test_wikidata_rows_become_tmdb_keys(self):
        rows = [wd('Q1', m=769), wd('Q2', t=1621, d=None), wd('Q3'),                 # film, series (no date), no TMDB id
                wd('Q4', m=5, d='2030-01-01T00:00:00Z'),                             # not released yet
                wd('Q5', m=6, sh='1')]                                               # a short film
        works, unmapped, unreleased = ap.parse_works(rows, TODAY)
        self.assertEqual(sorted(works), ['movie:6', 'movie:769', 'series:1621'])
        self.assertEqual((unmapped, unreleased), (['Q3'], 1))
        self.assertTrue(works['movie:6']['short'])
        self.assertFalse(works['movie:769']['short'])

    def test_sample_rows(self):
        c = lambda q: f'http://www.wikidata.org/entity/{q}'
        rows = [{'id': '10', 'c': c('Q7')}, {'id': '10', 'c': c('Q8')}, {'id': '11', 'c': c('Q8')}]
        self.assertEqual(ap.parse_sample(rows, 'series', 'Q7'), {'series:10': (2, True), 'series:11': (1, False)})
        q = ap.sample_query('actor', 'series', ['10', '11'])
        self.assertIn('wdt:P161', q)
        self.assertIn('VALUES ?id { "10" "11" }', q)
        self.assertIn('wdt:P4983', q)


def mc(i, title, job='Director', votes=900, date='2000-01-01'):
    return {'id': i, 'title': title, 'job': job, 'release_date': date, 'vote_count': votes, 'genre_ids': [18]}


def tc(i, name, job='Director', genres=(18,), date='2010-09-19'):
    return {'id': i, 'name': name, 'job': job, 'first_air_date': date, 'vote_count': 100, 'genre_ids': list(genres)}


def cast(i, title, character, media='movie', votes=500, order=0, eps=10, genres=(18,), date='2015-05-01'):
    c = {'id': i, 'media_type': media, 'character': character, 'vote_count': votes, 'genre_ids': list(genres)}
    c.update(title=title, release_date=date, order=order) if media == 'movie' else c.update(name=title, first_air_date=date, episode_count=eps)
    return c


class Rules(unittest.TestCase):
    def test_actor_rules_match_build_actor(self):
        credits = [cast(1, 'Lead', 'Hero'), cast(2, 'Bit', 'Guard', order=20), cast(3, 'Cameo', 'Himself'),
                   cast(4, 'Guest', 'Bob', media='tv', eps=2), cast(5, 'Regular', 'Bob', media='tv', eps=30),
                   cast(6, 'Cartoon', 'Rex (voice)', votes=300), cast(7, 'BigCartoon', 'Rex (voice)', votes=5000, order=1),
                   cast(8, 'Talk', 'Host', media='tv', genres=(10767,)), cast(9, 'Unknown', 'Hero', votes=3),
                   cast(10, 'Future', 'Hero', date='2030-01-01')]
        real, build.tmdb = build.tmdb, (lambda path, **p: {'cast': credits})
        try:
            kept = {(m['type'], int(m['id'].split(':')[1])) for m in build.actor({'person_id': 1}, {})}
        finally:
            build.tmdb = real
        ctx = ap.actor_ctx({'cast': credits}, TODAY)
        mine = {(media, c['id']) for media, c in (cs[0] for cs in ctx['by'].values())
                if ap.actor_status(media, c['id'], ctx, TODAY) is None}
        self.assertEqual(mine, kept)
        self.assertEqual(kept, {('movie', 1), ('series', 5), ('movie', 7)})

    def test_director_rules_match_build_director(self):
        movies = [mc(1, 'Feature'), mc(2, 'Short'), mc(3, 'Segment', job='Writer'), mc(4, 'Unknown runtime', votes=900),
                  mc(5, 'Unknown runtime obscure', votes=20), mc(6, 'Future', date='2030-01-01')]
        shows = [tc(20, 'Show'), tc(21, 'Late night', genres=(10767,)), tc(22, 'Feature', date='2000-03-03'), tc(23, 'Writer only', job='Writer')]
        runtimes = {1: 120, 2: 15, 4: 0, 5: 0}

        def stub(path, **params):
            if path.endswith('movie_credits'):
                return {'crew': movies}
            if path.endswith('tv_credits'):
                return {'crew': shows}
            return {'runtime': runtimes[int(path.rsplit('/', 1)[1])]}
        real, build.tmdb = build.tmdb, stub
        build._stable.clear()
        try:
            kept = {(m['type'], int(m['id'].split(':')[1])) for m in build.director({'person_id': 1}, [])}
        finally:
            build.tmdb = real
        ctx = ap.director_ctx({'crew': movies}, {'crew': shows})
        mine = {(media, tid) for media in ('movie', 'series') for tid in ctx[media]
                if ap.director_status(media, tid, ctx, TODAY, runtimes.get) is None}
        self.assertEqual(mine, kept)
        self.assertEqual(kept, {('movie', 1), ('movie', 4), ('series', 20)})
        self.assertIn('short film', ap.director_status('movie', 2, ctx, TODAY, runtimes.get)[1])


class Measure(unittest.TestCase):
    def run_card(self, kind, rows, order, tmdb, sample_rows=()):
        card = {'slug': f'{kind}-x', 'kind': kind, 'person_id': 77}

        def sparql(q):
            if q.startswith('SELECT ?p WHERE'):
                return [{'p': 'http://www.wikidata.org/entity/Q77'}]
            if 'GROUP BY ?w' in q:
                return rows
            return list(sample_rows)
        return ap.measure_people(kind, [card], {}, lambda slug: order, tmdb, sparql, TODAY, 5, 20, 'seed')

    def test_director_recall_labels_and_shorts_both_ways(self):
        movies = [mc(1, 'Feature'), mc(2, 'Short'), mc(4, 'Dropped feature')]
        runtimes = {1: 120, 2: 15, 4: 100}
        tmdb = lambda path, **p: ({'crew': movies} if path.endswith('movie_credits') else {'crew': []} if path.endswith('tv_credits')
                                  else {'runtime': runtimes[int(path.rsplit('/', 1)[1])]})
        rows = [wd('Q1', m=1), wd('Q2', m=2, sh='1'), wd('Q3', m=3), wd('Q4', m=4), wd('Q5')]
        row, gaps = self.run_card('director', rows, ['movie:1'], tmdb)
        got = {g['key']: g['label'] for g in gaps if g['side'] == 'recall'}
        self.assertEqual(got, {'movie:2': 'deliberate rule', 'movie:3': 'source limit', 'movie:4': 'code bug'})
        self.assertEqual((row['found'], row['truth'], row['recall']), (1, 4, 25.0))
        self.assertEqual(row['unmapped'], 1)
        self.assertEqual(row['shorts'], {'missing_as_shorts': 1, 'recall_shorts_in_claim': 25.0, 'recall_shorts_out_of_claim': 33.3})
        self.assertEqual(row['wikidata_short_films'], {'on_card': 0, 'total': 1})

    def test_actor_precision_judged_only(self):
        credits = {'cast': [cast(1, 'A', 'Hero'), cast(2, 'B', 'Hero'), cast(3, 'C', 'Hero'), cast(9, 'Bit', 'Guard', order=20)]}
        tmdb = lambda path, **p: credits
        E = 'http://www.wikidata.org/entity/'
        sample = [{'id': '2', 'c': E + 'Q5'}]
        row, gaps = self.run_card('actor', [wd('Q1', m=1)], ['movie:1', 'movie:2', 'movie:3', 'movie:9'], tmdb, sample)
        prec = {g['key']: g['label'] for g in gaps if g['side'] == 'precision'}
        self.assertEqual(prec, {'movie:2': 'source limit', 'movie:9': 'code bug'})    # 3 has no Wikidata cast list: unjudged
        self.assertEqual((row['ok'], row['sample'], row['unjudged'], row['precision']), (1, 3, 1, 33.3))
        self.assertEqual(row['recall'], 100.0)

    def test_card_unknown_to_wikidata_is_skipped(self):
        card = {'slug': 'a', 'kind': 'actor', 'person_id': 1}
        row, gaps = ap.measure_people('actor', [card], {}, lambda s: [], lambda q: [], lambda *a, **k: {}, TODAY, 5, 20, 's')
        self.assertEqual((row['cards'], row['no_wikidata'], gaps), (0, ['a'], []))


if __name__ == '__main__':
    unittest.main()
