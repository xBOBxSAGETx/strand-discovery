"""Issue #35: every catalog meta carries `released` (full ISO date) so the AIOStreams fork's Jellyfin
'DateCreated,SortName' sort has a real date. Standard library only, no network:

    python -m unittest tests.test_released_date
"""
import json, os, sys, tempfile, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402


class ReleasedDate(unittest.TestCase):
    def test_preview_has_released_and_keeps_release_info(self):
        m = build.preview({'id': 1, 'title': 'A', 'release_date': '2024-05-17'}, 'movie')
        self.assertEqual(m['released'], '2024-05-17T00:00:00.000Z')
        self.assertEqual(m['releaseInfo'], '2024')
        s = build.preview({'id': 2, 'name': 'B', 'first_air_date': '2023-01-02'}, 'series')
        self.assertEqual(s['released'], '2023-01-02T00:00:00.000Z')

    def test_no_date_no_released(self):
        self.assertNotIn('released', build.preview({'id': 3, 'title': 'C'}, 'movie'))

    def test_old_cache_entry_gets_released_without_refetch(self):
        old = {'p': {'id': 'tmdb:77', 'type': 'movie', 'name': 'Old', 'releaseInfo': '2020', '_g': []}, 'd': '2020-03-04'}
        build._stable['movie:77'] = old
        try:
            m = build.details_preview('movie', 77)       # cached: must not call TMDB (the key is a dummy)
        finally:
            build._stable.pop('movie:77', None)
        self.assertEqual(m['released'], '2020-03-04T00:00:00.000Z')

    def test_rating_on_preview_and_none_when_unrated(self):
        m = build.preview({'id': 1, 'title': 'A', 'release_date': '2024-05-17', 'vote_average': 7.84, 'vote_count': 120}, 'movie')
        self.assertEqual(m['imdbRating'], '7.8')
        self.assertNotIn('imdbRating', build.preview({'id': 2, 'title': 'B', 'vote_average': 0, 'vote_count': 0}, 'movie'))
        self.assertNotIn('imdbRating', build.preview({'id': 3, 'title': 'C', 'vote_average': 9.0, 'vote_count': 0}, 'movie'))

    def _old(self, tid):
        return {'p': {'id': f'tmdb:{tid}', 'type': 'movie', 'name': 'Old', '_g': []}, 'd': '2020-03-04'}

    def test_old_cache_rating_filled_from_seen_row_without_refetch(self):
        build._stable['movie:78'] = self._old(78)
        build._keys[('movie', 78)] = {'pop': 1, 'vote': 6.66, 'votes': 50, 'date': '2020-03-04'}
        try:
            m = build.details_preview('movie', 78)       # no TMDB call: the key is a dummy
        finally:
            build._stable.pop('movie:78', None)
            build._keys.pop(('movie', 78), None)
        self.assertEqual(m['imdbRating'], '6.7')

    def test_old_cache_rating_refetch_is_capped(self):
        calls = []
        real_tmdb, real_cap, real_n = build.tmdb, build.RATING_BACKFILL_MAX, build._rating_backfill[0]
        build.tmdb = lambda path, **kw: calls.append(path) or {'id': int(path.rsplit('/', 1)[1]), 'title': 'N',
                                                              'release_date': '2020-03-04', 'vote_average': 8.0, 'vote_count': 9}
        build.RATING_BACKFILL_MAX, build._rating_backfill[0] = 1, 0
        for t in (9900080, 9900081):
            build._stable[f'movie:{t}'] = self._old(t)
        try:
            a, b = build.details_preview("movie", 9900080), build.details_preview("movie", 9900081)
        finally:
            build.tmdb, build.RATING_BACKFILL_MAX, build._rating_backfill[0] = real_tmdb, real_cap, real_n
            for t in (9900080, 9900081):
                build._stable.pop(f'movie:{t}', None)
        self.assertEqual(len(calls), 1)                  # one refetch only; the second waits for the next run
        self.assertEqual(a['imdbRating'], '8.0')
        self.assertNotIn('imdbRating', b)

    def _ago(self, n):
        return (__import__('datetime').date.fromisoformat(build.TODAY) - __import__('datetime').timedelta(days=n)).isoformat()

    def _run(self, tid, entry, seen=None):
        """details_preview on a cached entry with TMDB stubbed: -> (meta, number of TMDB calls)."""
        calls = []
        real = build.tmdb
        build.tmdb = lambda path, **kw: calls.append(path) or {'id': tid, 'title': 'N', 'release_date': entry['d'],
                                                              'vote_average': 8.0, 'vote_count': 900}
        build._stable[f'movie:{tid}'] = entry
        if seen:
            build._keys[('movie', tid)] = seen
        try:
            return build.details_preview('movie', tid), len(calls)
        finally:
            build.tmdb = real
            build._stable.pop(f'movie:{tid}', None)
            build._keys.pop(('movie', tid), None)

    def test_recent_title_rated_over_30_days_ago_is_refetched(self):
        e = {'p': {'id': 'tmdb:9900090', 'type': 'movie', 'name': 'R', '_g': [], 'imdbRating': '3.0'},
             'd': self._ago(60), 'v': self._ago(45)}
        m, n = self._run(9900090, e)
        self.assertEqual((n, m['imdbRating']), (1, '8.0'))

    def test_old_release_rated_long_ago_is_not_refetched(self):
        e = {'p': {'id': 'tmdb:9900091', 'type': 'movie', 'name': 'O', '_g': [], 'imdbRating': '7.0'},
             'd': self._ago(2000), 'v': self._ago(400)}
        m, n = self._run(9900091, e)
        self.assertEqual((n, m['imdbRating']), (0, '7.0'))

    def test_seen_row_overwrites_stale_cached_rating(self):
        e = {'p': {'id': 'tmdb:9900092', 'type': 'movie', 'name': 'S', '_g': [], 'imdbRating': '3.0'},
             'd': self._ago(2000), 'v': self._ago(400)}
        m, n = self._run(9900092, e, {'pop': 1, 'vote': 7.46, 'votes': 500, 'date': e['d']})
        self.assertEqual((n, m['imdbRating']), (0, '7.5'))
        e2 = {'p': {'id': 'tmdb:9900093', 'type': 'movie', 'name': 'S', '_g': [], 'imdbRating': '3.0'},
              'd': self._ago(2000), 'v': self._ago(400)}
        m2, _ = self._run(9900093, e2, {'pop': 1, 'vote': 0, 'votes': 0, 'date': e2['d']})
        self.assertNotIn('imdbRating', m2)               # the row now has no votes: the old score is dropped

    def test_written_page_keeps_released(self):
        m = build.preview({'id': 1, 'title': 'A', 'release_date': '2024-05-17', 'genre_ids': [18],
                          'vote_average': 7.84, 'vote_count': 120}, 'movie')
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            build.write_catalog(root, 'movie', 'sd-r', [m], 10)
            out = json.loads((root / 'catalog' / 'movie' / 'sd-r.json').read_text(encoding='utf-8'))['metas'][0]
        self.assertEqual(out['released'], '2024-05-17T00:00:00.000Z')
        self.assertEqual(out['releaseInfo'], '2024')
        self.assertEqual(out['imdbRating'], '7.8')


if __name__ == '__main__':
    unittest.main()
