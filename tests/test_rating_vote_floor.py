"""Issue #138: imdbRating is written only for titles with at least the Top Rated vote floor (movie 300 / series 150),
so the fork's rating sort is not led by low-vote titles. Standard library only, no network:

    python -m unittest tests.test_rating_vote_floor
"""
import datetime as dt, os, sys, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402


def item(votes, avg=9.4, tid=1):
    return {'id': tid, 'title': 'T', 'name': 'T', 'release_date': '2020-03-04', 'first_air_date': '2020-03-04',
            'vote_average': avg, 'vote_count': votes}


class VoteFloor(unittest.TestCase):
    def test_movie_floor(self):
        self.assertNotIn('imdbRating', build.preview(item(299), 'movie'))
        self.assertEqual(build.preview(item(300), 'movie')['imdbRating'], '9.4')

    def test_series_floor(self):
        self.assertNotIn('imdbRating', build.preview(item(149), 'series'))
        self.assertEqual(build.preview(item(150), 'series')['imdbRating'], '9.4')

    def test_first_seen_candidate_helper_uses_media_floor(self):
        self.assertIsNone(build.rating_str(item(200), 'movie'))
        self.assertEqual(build.rating_str(item(200), 'series'), '9.4')

    def _run(self, tid, entry, seen=None, fetched=None):
        """details_preview on a cached entry with TMDB stubbed: -> (meta, TMDB calls)."""
        calls = []
        real = build.tmdb
        build.tmdb = lambda path, **kw: calls.append(path) or dict(fetched or item(900, 8.0, tid), id=tid)
        build._stable[f'movie:{tid}'] = entry
        if seen:
            build._keys[('movie', tid)] = seen
        try:
            return build.details_preview('movie', tid), calls
        finally:
            build.tmdb = real
            build._stable.pop(f'movie:{tid}', None)
            build._keys.pop(('movie', tid), None)

    def _entry(self, tid, **extra):
        return {'p': {'id': f'tmdb:{tid}', 'type': 'movie', 'name': 'C', '_g': [], 'imdbRating': '9.6'},
                'd': '2020-03-04', 'v': build.TODAY, **extra}

    def test_cached_entry_without_vote_count_is_due(self):
        self.assertTrue(build.rating_due(self._entry(1)))
        self.assertFalse(build.rating_due(self._entry(1, vc=500)))

    def test_cached_entry_without_vote_count_is_refetched_and_stores_vc(self):
        m, calls = self._run(9900101, self._entry(9900101))
        self.assertEqual(len(calls), 1)
        self.assertEqual(m['imdbRating'], '8.0')
        self.assertEqual(build._rating_backfill[0] >= 1, True)

    def test_unknown_vote_count_beyond_cap_keeps_no_rating(self):
        real_cap = build.RATING_BACKFILL_MAX
        build.RATING_BACKFILL_MAX = 0                     # nothing may be refetched this run
        try:
            m, calls = self._run(9900102, self._entry(9900102))
        finally:
            build.RATING_BACKFILL_MAX = real_cap
        self.assertEqual(calls, [])
        self.assertNotIn('imdbRating', m)                 # the cached 9.6 has an unknown vote count: dropped, not kept

    def test_cached_entry_with_vote_count_keeps_rating_without_refetch(self):
        m, calls = self._run(9900103, self._entry(9900103, vc=500))
        self.assertEqual((calls, m['imdbRating']), ([], '9.6'))

    def test_seen_row_overwrite_respects_floor(self):
        low = self._run(9900104, self._entry(9900104, vc=500),
                        {'pop': 1, 'vote': 9.6, 'votes': 12, 'date': '2020-03-04'})[0]
        self.assertNotIn('imdbRating', low)               # 12 votes: the old rating is dropped
        ok = self._run(9900105, self._entry(9900105, vc=500),
                       {'pop': 1, 'vote': 7.46, 'votes': 300, 'date': '2020-03-04'})
        self.assertEqual((ok[1], ok[0]['imdbRating']), ([], '7.5'))

    def test_fetch_below_floor_has_no_rating(self):
        m, calls = self._run(9900106, self._entry(9900106), fetched=item(12, 9.6, 9900106))
        self.assertEqual(len(calls), 1)
        self.assertNotIn('imdbRating', m)


if __name__ == '__main__':
    unittest.main()
