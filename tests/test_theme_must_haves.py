"""Theme cards always hold their hand-picked must-have titles (issue #64), with a stubbed TMDB (no network):

    python -m unittest tests.test_theme_must_haves
"""
import json, os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

DFLT = {'depth': {'popular': 1000, 'new': 500, 'top': 500, 'release_asc': 1000, 'votes': 1000}}
LISTS = json.loads((Path(build.HERE) / 'theme_must_haves.json').read_text(encoding='utf-8'))
CARD = {'slug': 'theme-space-epics', 'kind': 'discover', 'media': ['movie'], 'sort': 'popular',
        'movie': {'with_keywords': '161176'}}
INTERSTELLAR, GRAVITY = 157336, 49047        # both movie must-haves of theme-space-epics (ids from Wikidata)


def row(i, name):
    return {'id': i, 'title': name, 'popularity': 100.0 - i % 50, 'release_date': '2014-01-01',
            'poster_path': f'/p{i}.jpg', 'genre_ids': [878]}


class Stub:
    """/discover returns `pool` on page 1; /movie/<id> returns the title for any id (counted)."""

    def __init__(self, pool):
        self.pool, self.details = pool, []

    def __call__(self, path, **params):
        if path.startswith('/discover'):
            return {'page': 1, 'results': [dict(r) for r in self.pool], 'total_results': len(self.pool), 'total_pages': 1}
        self.details.append(path)
        return row(int(path.rsplit('/', 1)[1]), f'Detail {path}')


def build_card(stub):
    real = build.tmdb
    build.tmdb = stub
    build._stable.clear()
    try:
        return build.build_one(CARD, DFLT)[0]
    finally:
        build.tmdb = real


def ids_of(metas):
    return [m['id'] for m in metas]


class MustHaves(unittest.TestCase):
    def setUp(self):                      # these tests are about the must-have list alone; corrections (#117) have their own test
        self._corr, build.corrections_of = build.corrections_of, lambda card: []

    def tearDown(self):
        build.corrections_of = self._corr

    def test_missing_must_have_is_added_once(self):
        stub = Stub([row(9001, 'Other A'), row(GRAVITY, 'Gravity'), row(9002, 'Other B')])    # no Interstellar
        ids = ids_of(build_card(stub))
        self.assertEqual(ids.count(f'tmdb:{INTERSTELLAR}'), 1)               # old code: absent
        self.assertEqual(ids.count(f'tmdb:{GRAVITY}'), 1)                    # already there: no duplicate
        self.assertIn('tmdb:9001', ids)
        self.assertNotIn(f'/movie/{GRAVITY}', stub.details)                  # present ones cost no request
        self.assertIn(f'/movie/{INTERSTELLAR}', stub.details)
        self.assertEqual(len(ids), len(set(ids)))

    def test_must_haves_first_in_list_order_then_discover_order(self):
        movie_ids = [e['tmdb'] for e in LISTS[CARD['slug']] if e['type'] == 'movie']
        ids = ids_of(build_card(Stub([row(9001, 'Other A'), row(9002, 'Other B')])))
        self.assertEqual(ids[:len(movie_ids)], [f'tmdb:{i}' for i in movie_ids])
        self.assertEqual(ids[len(movie_ids):], ['tmdb:9001', 'tmdb:9002'])

    def test_no_extra_request_when_discover_has_them_all(self):
        movie_ids = [e['tmdb'] for e in LISTS[CARD['slug']] if e['type'] == 'movie']
        stub = Stub([row(i, f'M{i}') for i in movie_ids] + [row(9001, 'Other')])
        ids = ids_of(build_card(stub))
        self.assertEqual(stub.details, [])
        self.assertEqual(len(ids), len(movie_ids) + 1)

    def test_not_a_theme_card_is_untouched(self):
        card = {**CARD, 'slug': 'studio-test'}
        real, stub = build.tmdb, Stub([row(9001, 'Other')])
        build.tmdb = stub
        try:
            ids = ids_of(build.build_one(card, DFLT)[0])
        finally:
            build.tmdb = real
        self.assertEqual(ids, ['tmdb:9001'])
        self.assertEqual(stub.details, [])


class MustHaveData(unittest.TestCase):
    def test_file_is_well_formed(self):
        spec = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'spec_before_cut.json').read_text(encoding='utf-8'))   # pre-#139-cut: themes are cut
        themes = {c['slug'] for c in spec['catalogs'] if 'themes' in c.get('folders', [])}
        self.assertEqual(set(LISTS), themes)                                  # every theme card, nothing else
        for slug, entries in LISTS.items():
            self.assertTrue(10 <= len(entries) <= 20, slug)
            for e in entries:        # type / int id / duplicates / source: tests/test_hand_lists.py checks every hand list
                self.assertTrue(e['title'] and 1900 < e['year'] < 2100, slug)
                self.assertTrue(str(e.get('source', '')).startswith('https://'), f"{slug}: {e['title']} has no source")


if __name__ == '__main__':
    unittest.main()
