"""Our own corrections list for theme cards (issue #117), with a stubbed TMDB (no network):

    python -m unittest tests.test_corrections

A theme shelf that lacks a Wikidata-confirmed title gets it from generator/corrections.json; a title already on the
shelf is not added twice or fetched again; the shelf cap holds.
"""
import json, os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

HERE = Path(build.HERE)
FIXED = {'movie': 3933, 'series': 50241}      # Corpse Bride (zombies card), Lorong Waktu (time-travel card): corrections, not must-haves
DFLT = {'depth': {'popular': 1000}}
CARD = {'slug': 'theme-zombies', 'kind': 'discover', 'media': ['movie'], 'sort': 'popular', 'movie': {'with_keywords': '12377'}}
CORR_MOVIES = [e['tmdb'] for e in json.loads((HERE / 'corrections.json').read_text(encoding='utf-8'))['theme-zombies']
               if e['type'] == 'movie']
POOL_SIZE = 4


def row(i, name):
    return {'id': i, 'title': name, 'popularity': 100.0 - i % 50, 'release_date': '2014-01-01',
            'poster_path': f'/p{i}.jpg', 'genre_ids': [27]}


class Stub:
    """/discover returns `pool` on page 1; /movie/<id> returns the title for any id (counted)."""

    def __init__(self, pool):
        self.pool, self.details = pool, []

    def __call__(self, path, **params):
        if path.startswith('/discover'):
            return {'page': 1, 'results': [dict(r) for r in self.pool], 'total_results': len(self.pool), 'total_pages': 1}
        self.details.append(path)
        return row(int(path.rsplit('/', 1)[1]), f'Detail {path}')


def build_card(stub, dflt=DFLT, card=CARD):
    real = build.tmdb
    build.tmdb = stub
    build._stable.clear()
    try:
        return build.build_one(card, dflt)[0]
    finally:
        build.tmdb = real


def ids_of(metas):
    return [m['id'] for m in metas]


class Corrections(unittest.TestCase):
    def pool(self, *extra):
        return [row(9000 + n, f'Other {n}') for n in range(POOL_SIZE)] + [row(i, f'T{i}') for i in extra]

    def test_missing_title_is_added_and_present_one_is_not_duplicated(self):
        have = CORR_MOVIES[0]
        missing = [i for i in CORR_MOVIES if i != have]
        stub = Stub(self.pool(have))
        ids = ids_of(build_card(stub))
        for i in missing:
            self.assertEqual(ids.count(f'tmdb:{i}'), 1, i)               # old code: absent
        self.assertEqual(ids.count(f'tmdb:{have}'), 1)                   # already there: no duplicate
        self.assertNotIn(f'/movie/{have}', stub.details)                 # and no extra request for it
        self.assertEqual(len(ids), len(set(ids)))

    def test_shelf_order_is_kept_corrections_go_after_it(self):
        ids = ids_of(build_card(Stub(self.pool())))
        sorted_part = [f'tmdb:{9000 + n}' for n in range(POOL_SIZE)]
        at = [ids.index(i) for i in sorted_part]
        self.assertEqual(at, sorted(at))                                 # discover order untouched
        self.assertTrue(all(ids.index(f'tmdb:{c}') > max(at) for c in CORR_MOVIES))

    def test_cap_holds(self):
        stub = Stub(self.pool())
        must = build.must_haves(CARD, 'movie')
        dflt = {'depth': {'popular': len(must) + len(CORR_MOVIES) + 2}}  # room for the must-haves, the corrections and 2 of the 4 sorted titles
        ids = ids_of(build_card(stub, dflt))
        self.assertEqual(len(ids), len(must) + len(CORR_MOVIES) + 2)     # the cap holds
        self.assertEqual([i for i in ids if i.startswith('tmdb:90')], ['tmdb:9000', 'tmdb:9001'])   # the sorted tail made room
        self.assertTrue(all(f'tmdb:{i}' in ids for i in must) and all(f'tmdb:{c}' in ids for c in CORR_MOVIES))

    def test_other_cards_untouched(self):
        ids = ids_of(build_card(Stub(self.pool()), card={**CARD, 'slug': 'studio-test'}))
        self.assertEqual(ids, [f'tmdb:{9000 + n}' for n in range(POOL_SIZE)])

    def test_only_side_add_is_built(self):
        real = build.load_hand_list
        e = build.hand_entry('movie', 777, 'X', 2000, 's')
        build.load_hand_list = lambda name, *a, **k: {'theme-zombies': [{**e, 'side': 'add'}, {**e, 'tmdb': 778, 'side': 'remove'},
                                                                       {**e, 'tmdb': 779, 'side': 'keep'}]}
        try:
            self.assertEqual(build.corrections(CARD, 'movie'), [777])
        finally:
            build.load_hand_list = real

    def test_not_released_is_skipped(self):
        class Future(Stub):
            def __call__(self, path, **params):
                r = super().__call__(path, **params)
                if not path.startswith('/discover'):
                    r['release_date'] = '2999-01-01'
                return r
        ids = ids_of(build_card(Future(self.pool())))
        self.assertTrue(all(f'tmdb:{c}' not in ids for c in CORR_MOVIES))


class CorrectionData(unittest.TestCase):
    def test_file_is_well_formed(self):
        lists = build.load_hand_list('corrections')
        must = build.load_hand_list('theme_must_haves')
        spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
        slugs = {c['slug'] for c in spec['catalogs']}
        self.assertTrue(set(lists) <= slugs)
        self.assertIn(FIXED['movie'], [e['tmdb'] for e in lists['theme-zombies']])
        for slug, entries in lists.items():
            for e in entries:
                self.assertTrue(e['title'] and e['wikidata'].startswith('Q') and e['added'], f"{slug}: {e['title']}")
                self.assertTrue(e['source'].startswith('https://en.wikipedia.org/wiki/') and e['why'] and e['side'] == 'add')   # two sources: Wikidata id + Wikipedia category
            held = {(m['type'], m['tmdb']) for m in must.get(slug, [])}
            self.assertFalse(held & {(e['type'], e['tmdb']) for e in entries}, f'{slug}: a correction repeats a must-have')


if __name__ == '__main__':
    unittest.main()
