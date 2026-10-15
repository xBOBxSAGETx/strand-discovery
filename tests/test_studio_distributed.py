"""Studio cards also hold the films the studio distributed (issue #66), with a stubbed TMDB (no network):

    python -m unittest tests.test_studio_distributed
"""
import json, os, re, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
GEN = Path(__file__).resolve().parents[1] / 'generator'
sys.path.insert(0, str(GEN))
import build  # noqa: E402
import studio_distributed  # noqa: E402

DFLT = {'depth': {'popular': 1000, 'new': 500, 'top': 500, 'release_asc': 1000, 'votes': 1000}}
LADY_BIRD, HEREDITARY, UNRELEASED = 391713, 493922, 999001


def movie(i, date='2017-11-03'):
    return {'id': i, 'title': f'T{i}', 'popularity': 1000.0 - i, 'vote_count': 500, 'release_date': date,
            'poster_path': f'/p{i}.jpg', 'genre_ids': [18]}


def card(slug, folders):
    return {'slug': slug, 'kind': 'discover', 'media': ['movie'], 'sort': 'popular', 'folders': folders,
            'movie': {'with_companies': '41077'}}


class Stub:
    """/discover/movie returns `pool` (one page); /movie/<id> returns the film or 404-like RuntimeError."""

    def __init__(self, pool, extra):
        self.pool, self.extra, self.details = pool, {m['id']: m for m in extra}, []

    def __call__(self, path, **params):
        if path == '/discover/movie':
            return {'page': 1, 'results': [dict(r) for r in self.pool], 'total_results': len(self.pool), 'total_pages': 1}
        tid = int(path.rsplit('/', 1)[1])
        self.details.append(tid)
        return dict(self.extra[tid]) if tid in self.extra else {}


def build_card(c, stub, data):
    d = Path(tempfile.mkdtemp(prefix='sd-here-', dir=_tmp))
    (d / 'studio_distributed.json').write_text(json.dumps(data), encoding='utf-8')
    real_tmdb, real_here = build.tmdb, build.HERE
    build.tmdb, build.HERE = stub, d
    build._stable.clear()
    try:
        metas, _ = build.build_one(c, DFLT)
        return [m['id'] for m in metas]
    finally:
        build.tmdb, build.HERE = real_tmdb, real_here


def entry(tid, qid):
    return {'tmdb': tid, 'title': f'T{tid}', 'year': 2017, 'qid': qid}


DATA = {'studio-a24': [entry(LADY_BIRD, 'Q27480769'), entry(HEREDITARY, 'Q47524071'), entry(UNRELEASED, 'Q1')]}


class StudioDistributed(unittest.TestCase):
    def test_missing_distributed_film_is_added_once_near_the_top(self):
        pool = [movie(i) for i in range(1, 11)]
        stub = Stub(pool, [movie(LADY_BIRD), movie(HEREDITARY), movie(UNRELEASED, '2999-01-01')])
        ids = build_card(card('studio-a24', ['studios']), stub, DATA)
        self.assertEqual(ids[:4], ['tmdb:1', f'tmdb:{LADY_BIRD}', 'tmdb:2', f'tmdb:{HEREDITARY}'])   # 1:1 merge
        self.assertEqual(ids.count(f'tmdb:{LADY_BIRD}'), 1)
        self.assertNotIn(f'tmdb:{UNRELEASED}', ids)                                                  # unreleased skipped
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 12)

    def test_no_duplicate_when_already_in_discover(self):
        stub = Stub([movie(1), movie(LADY_BIRD)], [movie(HEREDITARY)])
        ids = build_card(card('studio-a24', ['studios']), stub, DATA)
        self.assertEqual(ids.count(f'tmdb:{LADY_BIRD}'), 1)
        self.assertEqual(set(ids), {'tmdb:1', f'tmdb:{LADY_BIRD}', f'tmdb:{HEREDITARY}'})
        self.assertNotIn(LADY_BIRD, stub.details)                  # not even fetched

    def test_non_studio_card_untouched(self):
        stub = Stub([movie(1), movie(2)], [movie(LADY_BIRD), movie(HEREDITARY)])
        ids = build_card(card('studio-a24', ['genres-popular']), stub, DATA)     # same slug, not in the studios folder
        self.assertEqual(ids, ['tmdb:1', 'tmdb:2'])
        self.assertEqual(stub.details, [])

    def test_production_studio_gets_nothing_added(self):
        stub = Stub([movie(1), movie(2)], [movie(LADY_BIRD), movie(HEREDITARY)])
        ids = build_card(card('studio-pixar', ['studios']), stub, DATA)          # a studios card with no distributed list
        self.assertEqual(ids, ['tmdb:1', 'tmdb:2'])
        self.assertEqual(stub.details, [])


class DataFile(unittest.TestCase):
    def test_data_file_sane(self):
        data = json.loads((GEN / 'studio_distributed.json').read_text(encoding='utf-8'))
        spec = json.loads((GEN / 'spec.json').read_text(encoding='utf-8'))
        studios = {c['slug'] for c in spec['catalogs'] if 'studios' in c.get('folders', [])}
        self.assertEqual(set(data), set(studio_distributed.STUDIO_ITEMS))        # no strays
        self.assertEqual(set(data), {'studio-a24', 'studio-neon', 'studio-lionsgate', 'studio-focus-features',
                                     'studio-searchlight'})                      # distribution-identity studios only
        self.assertTrue(set(data) <= studios)
        for slug, rows in data.items():
            ids = [r['tmdb'] for r in rows]
            self.assertTrue(all(type(i) is int and i > 0 for i in ids), slug)
            self.assertEqual(len(ids), len(set(ids)), f'{slug}: duplicate tmdb ids')
            self.assertLessEqual(len(ids), studio_distributed.CAP, slug)
            for r in rows:
                self.assertRegex(r['qid'], r'^Q\d+$')
                self.assertTrue(r['title'] and type(r['year']) is int, r)

    def test_the_issue_examples_are_in(self):
        data = json.loads((GEN / 'studio_distributed.json').read_text(encoding='utf-8'))
        self.assertTrue({LADY_BIRD, HEREDITARY} <= {r['tmdb'] for r in data['studio-a24']})
        self.assertIn(496243, {r['tmdb'] for r in data['studio-neon']})         # Parasite

    def test_territory_only_releases_out_real_ones_in(self):
        data = json.loads((GEN / 'studio_distributed.json').read_text(encoding='utf-8'))
        titles = {r['title'] for rows in data.values() for r in rows}
        for gone in ('Absolutely Anything', 'Shivers'):                          # UK-only / old Canadian (Lionsgate)
            self.assertNotIn(gone, titles)
        for kept in ('Lady Bird', 'Parasite'):
            self.assertIn(kept, titles)


if __name__ == '__main__':
    unittest.main()
