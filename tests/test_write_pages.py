"""write_pages() normalises the poster size at emit time (generator/build.py). Standard library only, no network:

    python -m unittest discover tests

Why: details_preview() caches whole previews (poster URL included) durably in the lookup cache, and first_seen stores
New-card metas with their poster URLs, so previews fetched before the w780 change kept w342 indefinitely. The size is
therefore rewritten in ONE place, when the pages are written, from copies of the metas.
"""
import copy, json, os, sys, tempfile, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

IMG = 'https://image.tmdb.org/t/p'


class WritePagesPosterSize(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='sd-pages-')
        self.base = Path(self._tmp.name) / 'catalog' / 'movie'
        self.base.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def pages(self, cid):
        files = [self.base / f'{cid}.json'] + sorted((self.base / cid).glob('skip=*.json'))
        return [m for f in files for m in json.loads(f.read_text(encoding='utf-8'))['metas']]

    def test_w342_becomes_w780(self):
        metas = [{'id': 'tmdb:1', 'type': 'movie', 'name': 'A', 'poster': f'{IMG}/w342/abc.jpg'},
                 {'id': 'tmdb:2', 'type': 'movie', 'name': 'B', 'poster': f'{IMG}/w185/def.jpg'},
                 {'id': 'tmdb:3', 'type': 'movie', 'name': 'C', 'poster': f'{IMG}/w780/ghi.jpg'}]
        build.write_pages(self.base, 'sd-x', metas, page_size=2)
        out = self.pages('sd-x')
        self.assertEqual(build.POSTER_SIZE, 'w780')
        self.assertEqual([m['poster'] for m in out],
                         [f'{IMG}/w780/abc.jpg', f'{IMG}/w780/def.jpg', f'{IMG}/w780/ghi.jpg'])
        self.assertEqual([m['id'] for m in out], ['tmdb:1', 'tmdb:2', 'tmdb:3'])
        for f in [self.base / 'sd-x.json', *(self.base / 'sd-x').glob('*.json')]:
            self.assertNotIn('/t/p/w342/', f.read_text(encoding='utf-8'))

    def test_genre_pages_too(self):
        metas = [{'id': 'tmdb:1', 'type': 'movie', 'name': 'A', 'poster': f'{IMG}/w342/abc.jpg'}]
        build.write_pages(self.base, 'sd-g', metas, page_size=10, genre='Drama')
        text = (self.base / 'sd-g' / 'genre=Drama.json').read_text(encoding='utf-8')
        self.assertIn('/t/p/w780/abc.jpg', text)
        self.assertNotIn('/w342/', text)

    def test_meta_without_poster_untouched(self):
        metas = [{'id': 'tmdb:9', 'type': 'movie', 'name': 'No art', 'releaseInfo': '2020'}]
        build.write_pages(self.base, 'sd-np', metas, page_size=10)
        self.assertEqual(self.pages('sd-np'), metas)

    def test_input_not_mutated(self):
        cached = {'id': 'tmdb:1', 'type': 'movie', 'name': 'A', 'poster': f'{IMG}/w342/abc.jpg', 'releaseInfo': '1999'}
        metas = [cached, {'id': 'tmdb:9', 'type': 'movie', 'name': 'N'}]
        before = copy.deepcopy(metas)
        build.write_pages(self.base, 'sd-m', metas, page_size=10)
        self.assertEqual(metas, before)
        self.assertEqual(cached['poster'], f'{IMG}/w342/abc.jpg')      # the cache object behind the meta is kept

    def test_write_catalog_end_to_end(self):
        metas = [{'id': f'tmdb:{i}', 'type': 'movie', 'name': str(i), 'poster': f'{IMG}/w342/p{i}.jpg',
                  '_g': ['Drama']} for i in range(12)]
        before = copy.deepcopy(metas)
        root = Path(self._tmp.name) / 'root'
        build.write_catalog(root, 'movie', 'sd-c', metas, 5)
        self.assertEqual(metas, before)
        texts = [f.read_text(encoding='utf-8') for f in (root / 'catalog').rglob('*.json')]
        self.assertTrue(texts)
        self.assertFalse(any('/w342/' in t for t in texts))
        self.assertFalse(any('"_g"' in t for t in texts))


if __name__ == '__main__':
    unittest.main()
