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

    def test_written_page_keeps_released(self):
        m = build.preview({'id': 1, 'title': 'A', 'release_date': '2024-05-17', 'genre_ids': [18]}, 'movie')
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            build.write_catalog(root, 'movie', 'sd-r', [m], 10)
            out = json.loads((root / 'catalog' / 'movie' / 'sd-r.json').read_text(encoding='utf-8'))['metas'][0]
        self.assertEqual(out['released'], '2024-05-17T00:00:00.000Z')
        self.assertEqual(out['releaseInfo'], '2024')


if __name__ == '__main__':
    unittest.main()
