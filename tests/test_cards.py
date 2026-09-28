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
        self.pools = pools or (lambda path, params: [item(n, '2020-01-01', 'movie' if '/movie' in path else 'tv')
                                                     for n in range(1, 31)])

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


class SeasonalNow(unittest.TestCase):
    card = BY_SLUG['theme-seasonal-now']

    def build(self, today, pools=None):
        b = load_build(today)
        stub = StubTMDB(pools)
        b.tmdb = stub
        metas, notes = b.build_one(self.card, DFLT)
        return b, stub, metas, notes

    def queries(self, stub, media):
        return [q for p, q in stub.paths(f"/discover/{'movie' if media == 'movie' else 'tv'}")]

    def test_spec_card(self):
        c = self.card
        self.assertEqual((c['kind'], c['folders'], c['library'], c['title']),
                         ('seasonal', ['themes'], 'Theme · Seasonal · Now', 'Seasonal · Now'))
        names = [s['name'] for s in c['seasons']]
        self.assertEqual(names, ['Summer', 'Halloween', 'Thanksgiving', 'Christmas', 'Best of the Past Year'])
        self.assertEqual(sum(1 for s in c['seasons'] if not s.get('start')), 1, 'exactly one fallback')

    def test_october_is_halloween(self):
        for day in ('2026-10-01', '2026-10-15', '2026-10-31'):
            b, stub, metas, notes = self.build(day)
            self.assertEqual(notes, 'season: Halloween', day)
            self.assertEqual(self.queries(stub, 'movie')[0]['with_keywords'].split('|')[:2], ['3335', '232795'])
            self.assertTrue(self.queries(stub, 'series')[0]['with_keywords'].startswith('3335'))
            self.assertEqual(self.queries(stub, 'movie')[0]['with_runtime.gte'], 20)   # half-hour specials stay
            self.assertEqual(self.queries(stub, 'movie')[0]['sort_by'], 'vote_count.desc')
            self.assertEqual({m['type'] for m in metas}, {'movie', 'series'})

    def test_december_is_christmas(self):
        for day in ('2026-11-29', '2026-12-10', '2026-12-31'):
            _, stub, _, notes = self.build(day)
            self.assertEqual(notes, 'season: Christmas', day)
            self.assertTrue(self.queries(stub, 'movie')[0]['with_keywords'].startswith('207317'))
            self.assertEqual(self.queries(stub, 'series')[0]['with_keywords'].split('|')[0], '207317')

    def test_thanksgiving_and_summer(self):
        self.assertEqual(self.build('2026-11-26')[3], 'season: Thanksgiving')
        self.assertEqual(self.build('2026-11-28')[3], 'season: Thanksgiving')
        self.assertEqual(self.build('2026-07-04')[3], 'season: Summer')

    def test_no_season_date_uses_fallback(self):
        for day, since in (('2026-09-28', '2025-09-28'), ('2027-01-15', '2026-01-15'), ('2026-05-31', '2025-05-31')):
            _, stub, metas, notes = self.build(day)
            self.assertEqual(notes, 'season: Best of the Past Year', day)
            mq, tq = self.queries(stub, 'movie')[0], self.queries(stub, 'series')[0]
            self.assertNotIn('with_keywords', mq)
            self.assertEqual(mq['primary_release_date.gte'], since)
            self.assertEqual(tq['first_air_date.gte'], since)
            self.assertTrue(metas)

    def test_thin_season_falls_back(self):
        def pools(path, q):                  # a keyword season with only 3 titles per medium
            media = 'movie' if path.endswith('movie') else 'tv'
            n = 3 if 'with_keywords' in q else 30
            return [item(i, '2020-01-01', media) for i in range(1, n + 1)]
        _, stub, metas, notes = self.build('2026-10-15', pools)
        self.assertEqual(notes, 'season Halloween had only 6 titles: fallback Best of the Past Year')
        self.assertEqual(len(metas), 60)

    def test_wrapping_window(self):
        import datetime as dt
        b = load_build('2026-10-15')
        card = {'seasons': [{'name': 'Winter', 'start': '12-15', 'end': '01-10'}, {'name': 'Rest'}]}
        pick = lambda d: b.season_of(card, dt.date.fromisoformat(d))['name']
        self.assertEqual([pick(d) for d in ('2026-12-20', '2027-01-10', '2027-01-11', '2026-12-14')],
                         ['Winter', 'Winter', 'Rest', 'Rest'])

    def test_builds_through_main(self):
        b = load_build('2026-10-15')
        out, man = run_main(b, [self.card], StubTMDB())
        self.assertEqual([(c['id'], c['name']) for c in man['catalogs']],
                         [('sd-theme-seasonal-now', 'Theme · Seasonal · Now')])
        report = (out / 'report.csv').read_text(encoding='utf-8')
        self.assertIn('season Halloween (10-01..10-31)', report)
        self.assertIn('keyword 3335', report)
        self.assertIn('season: Halloween', report)


class Trending(unittest.TestCase):
    TODAY = '2026-10-15'

    def setUp(self):
        self.b = load_build(self.TODAY)

    def test_spec_cards(self):
        for slug, lib, media in (('trending-movies', 'Trending · Movies', ['movie']),
                                 ('trending-tv', 'Trending · TV', ['series'])):
            c = BY_SLUG[slug]
            self.assertEqual((c['kind'], c['library'], c['media'], c['folders'], c['window'], c['depth']),
                             ('trending', lib, media, ['streaming-popular'], 'week', 200))

    def test_movies_in_tmdb_order_released_only(self):
        pool = [item(5, '2026-10-01'), item(9, '2026-11-20'),            # 9 not released yet -> dropped
                item(2, '2025-01-01', adult=True), item(7, '2026-10-15'), item(5, '2026-10-01'), item(3, '')]
        stub = StubTMDB(lambda p, q: pool)
        self.b.tmdb = stub
        metas, _ = self.b.build_one(BY_SLUG['trending-movies'], DFLT)
        self.assertEqual([m['id'] for m in metas], ['tmdb:5', 'tmdb:7'])
        self.assertEqual({p for p, _ in stub.calls}, {'/trending/movie/week'})
        self.assertEqual(metas[0]['type'], 'movie')
        self.assertEqual(metas[0]['_g'], ['Comedy', 'Horror'])              # genre pages work from genre_ids

    def test_tv_drops_talk_and_news(self):
        pool = [item(1, '2026-01-01', 'tv'), item(2, '2026-01-01', 'tv', genre_ids=[10767]),
                item(3, '2026-01-01', 'tv', genre_ids=[18, 10763]), item(4, '2026-01-01', 'tv')]
        stub = StubTMDB(lambda p, q: pool)
        self.b.tmdb = stub
        metas, _ = self.b.build_one(BY_SLUG['trending-tv'], DFLT)
        self.assertEqual([(m['id'], m['type']) for m in metas], [('tmdb:1', 'series'), ('tmdb:4', 'series')])
        self.assertEqual({p for p, _ in stub.calls}, {'/trending/tv/week'})

    def test_depth_and_page_cap(self):
        many = [item(i, '2026-01-01') for i in range(1, 1001)]
        self.b.tmdb = stub = StubTMDB(lambda p, q: many)
        self.assertEqual(len(self.b.build_one(BY_SLUG['trending-movies'], DFLT)[0]), 200)
        self.assertEqual(len(stub.calls), 10)
        unreleased = [item(i, '2027-01-01') for i in range(1, 1001)]
        self.b.tmdb = stub = StubTMDB(lambda p, q: unreleased)
        self.assertEqual(self.b.build_one(BY_SLUG['trending-movies'], DFLT)[0], [])
        self.assertEqual(len(stub.calls), 15, 'page cap = depth/20 + 5')


class AllNewCardsTogether(unittest.TestCase):
    def test_main_builds_the_four_cards_in_spec_order(self):
        slugs = [c['slug'] for c in SPEC['catalogs']
                 if c['slug'] in ('trending-movies', 'trending-tv', 'just-hit-digital', 'theme-seasonal-now')]
        self.assertEqual(slugs, ['trending-movies', 'trending-tv', 'just-hit-digital', 'theme-seasonal-now'])
        b = load_build('2026-12-10')
        out, man = run_main(b, [BY_SLUG[s] for s in slugs],
                            StubTMDB(lambda p, q: [item(n, '2026-12-01', 'movie' if '/movie' in p else 'tv')
                                                   for n in range(1, 31)]))
        self.assertEqual([c['id'] for c in man['catalogs']], [f'sd-{s}' for s in slugs])
        summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
        self.assertTrue(all(n > 0 for n in summary['catalogs'].values()), summary['catalogs'])
        self.assertIn('season: Christmas', (out / 'report.csv').read_text(encoding='utf-8'))
        # genre extra offered (>= 5 titles, not all of them): every stub title is Comedy + Horror -> none offered
        self.assertTrue(all(c['extra'][-1] == {'name': 'skip'} for c in man['catalogs']))


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
