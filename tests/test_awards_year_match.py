"""Issue #54: an Emmy title that won twice with different works (Shogun: the 1980 miniseries won 1981, the 2024 series
won 2024) must resolve the late win to the 2024 series, and the card must stay newest win first. Stubbed TMDB."""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import awards_wiki  # noqa: E402

SHOWS = {  # search query -> TMDB results
    'Shogun': [{'id': 13862, 'name': 'Shogun', 'first_air_date': '1980-09-15', 'vote_count': 400},
               {'id': 126308, 'name': 'Shogun', 'first_air_date': '2024-02-27', 'vote_count': 2000}],
    'Modern Family': [{'id': 1421, 'name': 'Modern Family', 'first_air_date': '2009-09-23', 'vote_count': 5000},
                      {'id': 99, 'name': 'Modern Family', 'first_air_date': '2014-01-01', 'vote_count': 5}],
    'The Pitt': [{'id': 250307, 'name': 'The Pitt', 'first_air_date': '2025-01-09', 'vote_count': 900}],
    'Hacks': [{'id': 120, 'name': 'Hacks', 'first_air_date': '2021-05-13', 'vote_count': 700}],
}
ENTRIES = [(1981, 'Shogun', 'p', None), (2024, 'Shogun', 'p', 'Shogun (2024 TV series)'),
           (2022, 'Hacks', 'p', None), (2026, 'The Pitt', 'p', None),
           (2010, 'Modern Family', 'p', None), (2014, 'Modern Family', 'p', None)]


def keys(ids):
    return [(e['type'], e['tmdb']) for e in ids]


def stub(path, **params):
    return {'results': SHOWS.get(params.get('query'), [])}


class AwardsYearMatch(unittest.TestCase):
    def run_list(self):
        real, awards_wiki.build.tmdb = awards_wiki.build.tmdb, stub
        try:
            return awards_wiki.build_list(ENTRIES, 'series')
        finally:
            awards_wiki.build.tmdb = real

    def test_late_win_resolves_to_the_work_of_that_year(self):
        ids, rows, _ = self.run_list()
        self.assertIn(('series', 126308), keys(ids), 'the 2024 Shogun series must be in the list')
        self.assertEqual([r[4] for r in rows if r[2] == 'Shogun' and r[0] == 2024], ['series:126308'])

    def test_long_runner_and_order(self):
        ids, rows, _ = self.run_list()
        self.assertIn(('series', 1421), keys(ids))         # Modern Family stays the 2009 show, not the 2014 remake
        self.assertNotIn(('series', 99), keys(ids))
        years = [r[0] for r in rows]
        self.assertEqual(years, sorted(years, reverse=True), 'newest win first')

    def test_entries_are_the_one_shape_with_a_wikipedia_source(self):      # issue #83
        ids, _, _ = self.run_list()
        shogun = next(e for e in ids if e['tmdb'] == 126308)
        self.assertEqual(shogun, {'type': 'series', 'tmdb': 126308, 'title': 'Shogun', 'year': 2024,
                                  'source': 'https://en.wikipedia.org/wiki/p'})


if __name__ == '__main__':
    unittest.main()
