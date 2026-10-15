"""Partial rebuild (issue #77): SD_CARDS builds only the listed cards and copies every other file byte-for-byte from the
live site. Stubbed TMDB and a fake "live site" read from a local folder; no network:

    python -m unittest tests.test_partial_rebuild

The stub build_one() raises for any card that is not allowed to be built, so the old code (which ignores SD_CARDS and
builds everything) fails these tests.
"""
import csv, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(tempfile.mkdtemp(prefix='sd-tests-')) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

LIVE_URL = 'http://live.test/'
REAL = json.loads((Path(build.__file__).resolve().parent / 'spec.json').read_text(encoding='utf-8'))
SLUGS = list(dict.fromkeys(['studio-a24', 'genre-horror', 'genre-horror-new'] + [c['slug'] for c in REAL['catalogs'][:12]]))[:12]
SIZES = {'studio-a24': 250, 'genre-horror': 130, 'genre-horror-new': 40}      # others: 60


def items(slug, n, tag):
    return [{'id': f'tmdb:{abs(hash(slug)) % 100000 * 1000 + i}', 'type': 'movie', 'name': f'{tag} {slug} {i}',
             'releaseInfo': '2020', '_g': ['Drama' if i % 2 else 'Comedy']} for i in range(n)]


class Partial(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sd-partial-')
        t = Path(self.tmp.name)
        spec = {**REAL, 'catalogs': [c for c in REAL['catalogs'] if c['slug'] in SLUGS]}
        assert len(spec['catalogs']) == len(SLUGS), 'test cards missing from spec.json'
        self.spec = spec
        (t / 'gen').mkdir()
        (t / 'gen' / 'spec.json').write_text(json.dumps(spec, ensure_ascii=False), encoding='utf-8')
        self.live, self.out = t / 'live', t / 'out'
        self.patches = [mock.patch.object(build, 'HERE', t / 'gen'),
                        mock.patch.object(build, 'source_of', lambda card: ('x', 'x')),
                        mock.patch.object(build, 'fetch_bytes', self.fetch),
                        mock.patch.object(build, 'FS_SUMMARY', {'providers': {}}),
                        mock.patch.dict(os.environ, {'SD_SITE_URL': LIVE_URL, 'SD_CARDS': '', 'SD_BASE_DIR': str(t / 'live')})]
        for p in self.patches:
            p.start()
        self.run_build(self.live, tag='live', allowed=None)                    # the "live site": a normal full build

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def fetch(self, url):
        rel = url[len(LIVE_URL):]
        p = self.live / rel
        if not p.is_file():
            raise FileNotFoundError(url)
        return p.read_bytes()

    def run_build(self, root, tag, allowed, sizes=SIZES, cards=''):
        def build_one(card, dflt):
            if allowed is not None and card['slug'] not in allowed:
                raise AssertionError(f"card {card['slug']} must not be built")
            return items(card['slug'], sizes.get(card['slug'], 60), tag), ''
        root.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(build, 'build_one', build_one), mock.patch.dict(os.environ, {'SD_CARDS': cards}), \
                mock.patch.object(sys, 'argv', ['build.py', str(root)]):
            build.main()

    def files(self, root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def test_only_listed_cards_replaced(self):
        self.run_build(self.out, 'new', allowed={'studio-a24', 'genre-horror'}, cards='sd-studio-a24,genre-horror',
                       sizes={'studio-a24': 180, 'genre-horror': 130})        # a24 shrinks: 250 -> 180 (3 pages -> 2)
        live, new = self.files(self.live), self.files(self.out)
        listed = lambda f: any(f.startswith(f'catalog/movie/sd-{s}') and not f.startswith(f'catalog/movie/sd-{s}-')
                               for s in ('studio-a24', 'genre-horror'))
        unlisted = [f for f in live if f.startswith('catalog/') and not listed(f)]
        self.assertTrue(unlisted)
        for f in unlisted:                                                  # byte-identical, nothing missing
            self.assertEqual(new[f], live[f], f)
        self.assertEqual({f for f in new if f.startswith('catalog/') and not listed(f)}, set(unlisted))
        changed = [f for f in new if listed(f)]
        self.assertTrue(any(new[f] != live.get(f) for f in changed))
        self.assertNotIn('catalog/movie/sd-studio-a24/skip=200.json', new)  # old page 3 is gone, not left behind
        self.assertIn('catalog/movie/sd-studio-a24/skip=180.json', new)     # new terminal page
        self.assertIn(b'new studio-a24 0', new['catalog/movie/sd-studio-a24.json'])
        self.assertEqual(live['.nojekyll'], new['.nojekyll'])

    def test_manifest_and_summary_consistent(self):
        self.run_build(self.out, 'new', allowed={'studio-a24'}, cards='studio-a24', sizes={'studio-a24': 180})
        manifest = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        summary = json.loads((self.out / 'summary.json').read_text(encoding='utf-8'))
        live_m = json.loads((self.live / 'manifest.json').read_text(encoding='utf-8'))
        ids = [f"sd-{c['slug']}" for c in self.spec['catalogs']]
        self.assertEqual([e['id'] for e in manifest['catalogs']], ids)            # spec order, no loss, no dup
        for e, o in zip(manifest['catalogs'], live_m['catalogs']):
            if e['id'] != 'sd-studio-a24':
                self.assertEqual(e, o)
        expected = {i: SIZES.get(i[3:], 60) for i in ids}
        expected['sd-studio-a24'] = 180
        self.assertEqual(summary['catalogs'], expected)
        self.assertEqual(summary['total_items'], sum(expected.values()))
        self.assertEqual(summary['partial']['cards'], ['studio-a24'])
        # genre_pages are re-read from the merged site, so they match what is really there
        genre = [p for p in (self.out / 'catalog').rglob('*.json') if 'genre=' in p.name]
        self.assertEqual(summary['genre_pages']['files'], len(genre))
        self.assertEqual(summary['genre_pages']['bytes'], sum(p.stat().st_size for p in genre))
        self.assertEqual(summary['genre_pages']['options'],
                         sum(len(x['options']) for e in manifest['catalogs'] for x in e['extra'] if x['name'] == 'genre'))
        rows = list(csv.reader((self.out / 'report.csv').read_text(encoding='utf-8').splitlines(True)))
        self.assertEqual([r[7] for r in rows[1:]], ids)
        self.assertEqual(dict((r[7], r[9]) for r in rows[1:])['sd-studio-a24'], '180')
        self.assertEqual(dict((r[7], r[9]) for r in rows[1:])['sd-genre-horror'], '130')

    def test_forced_empty_card_blocks(self):
        with self.assertRaises(SystemExit) as e:
            self.run_build(self.out, 'new', allowed={'genre-horror-new'}, cards='genre-horror-new',
                           sizes={'genre-horror-new': 0})
        self.assertIn('empty catalogs', str(e.exception.code))

    def test_drop_over_70_percent_blocks(self):
        with self.assertRaises(SystemExit) as e:                    # 130 -> 20 (-85%)
            self.run_build(self.out, 'new', allowed={'genre-horror'}, cards='genre-horror', sizes={'genre-horror': 20})
        self.assertIn('lost >70%', str(e.exception.code))

    def test_small_partial_may_shrink_one_card_40_percent(self):
        # 2 listed cards: no total check, only the per-card ones. a24 250 -> 150 (-40%) is a legitimate shrink.
        self.run_build(self.out, 'new', allowed={'studio-a24', 'genre-horror-new'}, cards='studio-a24,genre-horror-new',
                       sizes={'studio-a24': 150, 'genre-horror-new': 40})      # listed total 290 -> 190 (-34%)
        summary = json.loads((self.out / 'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['catalogs']['sd-studio-a24'], 150)

    def test_twelve_card_partial_total_down_35_percent_blocks(self):
        self.assertEqual(len(SLUGS), 12)
        sizes = {s: 39 for s in SLUGS}                              # every card 60 -> 39 (-35%): under 70% per card
        sizes['studio-a24'], sizes['genre-horror'], sizes['genre-horror-new'] = 163, 85, 26   # 250/130/40 -35% too
        with self.assertRaises(SystemExit) as e:
            self.run_build(self.out, 'new', allowed=set(SLUGS), cards=','.join(SLUGS), sizes=sizes)
        self.assertIn('total items of the listed catalogs dropped', str(e.exception.code))
        sizes = {s: 48 for s in SLUGS}                              # -20%: fine
        sizes['studio-a24'], sizes['genre-horror'], sizes['genre-horror-new'] = 200, 104, 32
        self.run_build(self.out.parent / 'out2', 'new', allowed=set(SLUGS), cards=','.join(SLUGS), sizes=sizes)

    def test_unknown_card_fails(self):
        with self.assertRaises(SystemExit) as e:
            self.run_build(self.out, 'new', allowed=set(), cards='studio-a24x')
        self.assertIn('names no known card', str(e.exception.code))

    def test_parse_cards(self):
        cats = self.spec['catalogs']
        self.assertIsNone(build.parse_cards('', cats))
        self.assertIsNone(build.parse_cards('  ', cats))
        self.assertEqual(build.parse_cards('sd-studio-a24, genre-horror\nsd-genre-horror', cats),
                         {'studio-a24', 'genre-horror'})
        f = Path(self.tmp.name) / 'cards.txt'
        f.write_text('sd-studio-a24\ngenre-horror,', encoding='utf-8')
        self.assertEqual(build.parse_cards(str(f), cats), {'studio-a24', 'genre-horror'})

    def test_damaged_cache_blocks(self):
        genre = next(p for p in (self.live / 'catalog').rglob('*.json') if 'genre=' in p.name)
        genre.unlink()                                     # a half-restored cache: genre pages differ from its summary
        with self.assertRaises(SystemExit) as e:
            self.run_build(self.out, 'new', allowed={'studio-a24'}, cards='studio-a24', sizes={'studio-a24': 250})
        self.assertIn('cached site is damaged', str(e.exception.code))

    def test_stale_cache_blocks(self):
        real = self.fetch

        def fetch(url):                                    # live was deployed after the cache was saved
            if url.endswith('summary.json'):
                return json.dumps({**json.loads(real(url)), 'generated_at': 'newer-deploy'}).encode()
            return real(url)
        with mock.patch.object(build, 'fetch_bytes', fetch), self.assertRaises(SystemExit) as e:
            self.run_build(self.out, 'new', allowed={'studio-a24'}, cards='studio-a24', sizes={'studio-a24': 250})
        self.assertIn('is not the live one', str(e.exception.code))

    def test_no_cache_blocks(self):
        with mock.patch.dict(os.environ, {'SD_BASE_DIR': str(Path(self.tmp.name) / 'missing')}), \
                self.assertRaises(SystemExit) as e:
            self.run_build(self.out, 'new', allowed={'studio-a24'}, cards='studio-a24')
        self.assertIn('no cached copy', str(e.exception.code))

    def test_live_deploy_during_build_blocks(self):
        real, calls = self.fetch, []

        def fetch(url):                                   # the 1st summary read is real, later ones show a new deploy
            if url.endswith('summary.json'):
                calls.append(1)
                if len(calls) > 1:
                    return json.dumps({'generated_at': 'someone-deployed'}).encode()
            return real(url)
        with mock.patch.object(build, 'fetch_bytes', fetch), self.assertRaises(SystemExit) as e:
            self.run_build(self.out, 'new', allowed={'studio-a24'}, cards='studio-a24', sizes={'studio-a24': 250})
        self.assertIn('deployed while this partial build ran', str(e.exception.code))

    def test_new_card_without_state_blocks(self):
        for c in self.spec['catalogs']:
            if c['slug'] == 'studio-a24':
                c['provider'] = 'netflix'
        (Path(self.tmp.name) / 'gen' / 'spec.json').write_text(json.dumps(self.spec, ensure_ascii=False), encoding='utf-8')
        with mock.patch.object(build, 'FS_SUMMARY', None), self.assertRaises(SystemExit) as e:
            self.run_build(self.out, 'new', allowed={'studio-a24'}, cards='studio-a24')
        self.assertIn('first_seen state is missing', str(e.exception.code))

    def test_full_build_is_default(self):
        self.run_build(self.out, 'new', allowed=None, cards='')               # no list: everything is rebuilt
        summary = json.loads((self.out / 'summary.json').read_text(encoding='utf-8'))
        self.assertNotIn('partial', summary)
        self.assertIn(b'new genre-horror 0', (self.out / 'catalog/movie/sd-genre-horror.json').read_bytes())
        self.assertEqual(len(summary['catalogs']), len(SLUGS))


if __name__ == '__main__':
    unittest.main()
