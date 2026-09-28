"""Tests for the date-driven cards in generator/build.py (Just Hit Digital, Seasonal · Now, Trending). Standard
library only; TMDB is stubbed (no network) and "today" is injected with SD_TODAY:

    python -m unittest discover -s tests
"""
import importlib, io, json, math, os, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-cards-tests-')
os.environ['TMDB_API_KEY'] = 'unused-in-tests'
for var in ('SD_CACHE', 'SD_STATE'):
    os.environ[var] = str(Path(_tmp) / var.lower())
GEN = Path(__file__).resolve().parents[1] / 'generator'
sys.path.insert(0, str(GEN))
SPEC = json.loads((GEN / 'spec.json').read_text(encoding='utf-8'))
BY_SLUG = {c['slug']: c for c in SPEC['catalogs']}
DFLT = SPEC['defaults']


def load_build(today):
    """build.py with SD_TODAY=today (module constants are computed at import, so reload)."""
    os.environ['SD_TODAY'] = today
    import build
    return importlib.reload(build)


def item(i, date, media='movie', **kw):
    it = {'id': i, 'genre_ids': [27, 35], 'popularity': 100 - i % 100, 'vote_count': 50, 'poster_path': f'/p{i}.jpg'}
    it.update({'title': f'Movie {i}', 'release_date': date} if media == 'movie' else
              {'name': f'Show {i}', 'first_air_date': date})
    it.update(kw)
    return it


class StubTMDB:
    """Records every call; serves 20 results per page from `pools` (a function (path, params) -> item list)."""

    def __init__(self, pools=None):
        self.calls = []
        self.pools = pools or (lambda path, params: [item(n, '2020-01-01', 'movie' if path.endswith('movie')
                                                          or '/movie/' in path else 'tv') for n in range(1, 31)])

    def __call__(self, path, **params):
        self.calls.append((path, params))
        if path.startswith('/keyword/'):
            return {'name': f"keyword {path.rsplit('/', 1)[1]}"}
        if path.startswith(('/discover/', '/trending/')):
            items = self.pools(path, params)
            page = params.get('page', 1)
            return {'results': items[(page - 1) * 20:page * 20], 'total_results': len(items),
                    'total_pages': max(1, math.ceil(len(items) / 20))}
        return {}

    def paths(self, prefix):
        return [(p, q) for p, q in self.calls if p.startswith(prefix)]


def run_main(b, cards, stub):
    """build.main() on a spec holding only `cards` (real addon/defaults/folders); returns (out dir, manifest)."""
    d = Path(tempfile.mkdtemp(prefix='sd-main-', dir=_tmp))
    (d / 'gen').mkdir()
    (d / 'gen' / 'spec.json').write_text(json.dumps({**SPEC, 'catalogs': cards}), encoding='utf-8')
    b.HERE, b.tmdb = d / 'gen', stub
    argv = sys.argv
    sys.argv = ['build.py', str(d / 'out')]
    try:
        with redirect_stdout(io.StringIO()):
            b.main()
    finally:
        sys.argv = argv
    return d / 'out', json.loads((d / 'out' / 'manifest.json').read_text(encoding='utf-8'))


class JustHitDigital(unittest.TestCase):
    TODAY = '2026-10-15'
    WINDOW = '2026-09-15'           # TODAY - 30 days

    def setUp(self):
        self.b = load_build(self.TODAY)
        self.card = BY_SLUG['just-hit-digital']

    def test_spec_card(self):
        c = self.card
        self.assertEqual((c['kind'], c['media'], c['folders']), ('discover', ['movie'], ['streaming-new']))
        self.assertEqual((c['release_types'], c['release_window_days'], c['newest_first']), ('4|6', 30, True))
        self.assertEqual(c['library'], 'Just Hit Digital')

    def test_query_is_narrowed_to_us_digital_in_window(self):
        stub = StubTMDB(lambda p, q: [item(1, '2026-10-01')])
        self.b.tmdb = stub
        self.b.build_one(self.card, DFLT)
        (path, q), = stub.paths('/discover/')
        self.assertEqual(path, '/discover/movie')
        self.assertEqual(q['with_release_type'], '4|6')
        self.assertEqual(q['region'], 'US')
        self.assertEqual(q['release_date.gte'], self.WINDOW)
        self.assertEqual(q['release_date.lte'], self.TODAY)
        self.assertEqual(q['vote_count.gte'], 5)

    def test_first_release_must_be_in_window_newest_first(self):
        pool = [item(1, '2026-09-20', popularity=90), item(2, '2026-10-10', popularity=10),
                item(3, '2026-08-01', popularity=99),   # went digital earlier; matched on a later entry -> dropped
                item(4, '2026-10-10', popularity=50), item(5, self.WINDOW, popularity=1)]
        self.b.tmdb = StubTMDB(lambda p, q: pool)
        metas, _ = self.b.build_one(self.card, DFLT)
        self.assertEqual([m['id'] for m in metas], ['tmdb:4', 'tmdb:2', 'tmdb:1', 'tmdb:5'])

    def test_other_movie_cards_unchanged(self):
        stub = StubTMDB()
        self.b.tmdb = stub
        self.b.build_one(BY_SLUG['genre-horror-new'], DFLT)
        q = stub.paths('/discover/movie')[0][1]
        self.assertEqual(q['with_release_type'], '4|5|6')
        self.assertNotIn('region', q)
        self.assertNotIn('release_date.gte', q)

    def test_builds_through_main(self):
        out, man = run_main(self.b, [self.card], StubTMDB(lambda p, q: [item(n, '2026-10-01') for n in range(1, 8)]))
        self.assertEqual([c['id'] for c in man['catalogs']], ['sd-just-hit-digital'])
        self.assertEqual(len(json.loads((out / 'catalog/movie/sd-just-hit-digital.json').read_text(encoding='utf-8'))['metas']), 7)


class SpecInvariants(unittest.TestCase):
    """What make_spec.py asserts, checked on the committed spec.json (make_spec needs the private people CSV)."""

    def test_ids_names_folders(self):
        import make_spec
        slugs = [c['slug'] for c in SPEC['catalogs']]
        libs = [c['library'] for c in SPEC['catalogs']]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(len(libs), len(set(libs)))
        for c in SPEC['catalogs']:
            self.assertRegex(c['slug'], r'^[a-z0-9-]+$')
            self.assertNotIn('collection', f"sd-{c['slug']} {c['library']}".lower())
        self.assertEqual(SPEC['folders'], make_spec.FOLDERS)
        keys = [f['key'] for f in SPEC['folders']]
        self.assertEqual(len(keys), 29, 'no new folders (owner rule)')
        for c in SPEC['catalogs']:
            self.assertLessEqual(set(c['folders']), set(keys))

    def test_catalogs_stay_in_folder_order(self):
        order = [f['key'] for f in SPEC['folders']]
        firsts = [order.index(c['folders'][0]) for c in SPEC['catalogs'] if c['kind'] not in ('director', 'actor')]
        self.assertEqual(firsts, sorted(firsts))

    def test_collection_guard_still_fires(self):
        b = load_build('2026-10-15')
        bad = dict(BY_SLUG['just-hit-digital'], slug='x-collection', library='X')
        with self.assertRaises(SystemExit) as e:
            run_main(b, [bad], StubTMDB(lambda p, q: [item(1, '2026-10-01')]))
        self.assertIn('collection', str(e.exception))


if __name__ == '__main__':
    unittest.main()
