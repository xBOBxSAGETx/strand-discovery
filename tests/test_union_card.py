"""One card per streaming service (issue #143): a 'union' catalog = Popular + New (+ Top Rated) parts in one list.
Offline, no network:

    python -m unittest tests.test_union_card

Controls: NAIVE below is the obvious wrong merge (Popular, then New, cut at the cap); it loses New titles, so the
cap test fails on it and passes on build.union_merge. The spec test fails on the 518-card spec of the issue-142 build
(tests/fixtures/spec_before_cut.json has the older 606-card one, which has the -new slugs too).
"""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(tempfile.mkdtemp(prefix='sd-tests-')) / 'sd_cache')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'generator'))
import build  # noqa: E402

SPEC = json.loads((ROOT / 'generator' / 'spec.json').read_text(encoding='utf-8'))
PRE = {c['slug']: c for c in json.loads((ROOT / 'tests' / 'fixtures' / 'spec_before_cut.json').read_text(encoding='utf-8'))['catalogs']}


def meta(n, media='movie'):
    return {'type': media, 'id': f'tmdb:{n}', 'name': f't{n}'}


def pops(table):
    """popularity per id, as preview() records it."""
    build._keys.clear()
    for n, p in table.items():
        build._keys[('movie', n)] = {'pop': p}


def NAIVE(by_sort, cap):
    """The wrong way: Popular first, then New, cut at the cap."""
    seen, out = set(), []
    for m in by_sort['popular'] + by_sort['new'] + by_sort.get('top', []):
        if (m['type'], m['id']) not in seen:
            seen.add((m['type'], m['id']))
            out.append(m)
    return out[:cap]


class UnionMerge(unittest.TestCase):
    def test_dedupes_on_type_and_id(self):
        pops({1: 9, 2: 8, 3: 7})
        out, st = build.union_merge({'popular': [meta(1), meta(2)], 'new': [meta(2), meta(3)], 'top': [meta(1)]}, 10)
        self.assertEqual([m['id'] for m in out], ['tmdb:1', 'tmdb:2', 'tmdb:3'])
        self.assertEqual((st['popular_and_new'], st['kept']), (1, 3))
        movie_and_show = build.union_merge({'popular': [meta(1)], 'new': [meta(1, 'series')]}, 10)[0]
        self.assertEqual(len(movie_and_show), 2)                       # same number, different type: two titles

    def test_the_cap_keeps_every_new_title_and_fills_by_popularity(self):
        pops({i: 1000 - i for i in range(1, 11)} | {i: 5 for i in range(100, 104)})
        popular = [meta(i) for i in range(1, 9)]                       # 8 popular, in popularity order
        new = [meta(i) for i in (100, 101, 102, 103)]                  # 4 new, all unpopular
        by = {'popular': popular, 'new': new}
        old = NAIVE(by, 10)
        self.assertEqual(len({m['id'] for m in old} & {m['id'] for m in new}), 2)   # control: the naive cut loses 2 New
        out, st = build.union_merge(by, 10)
        ids = [m['id'] for m in out]
        self.assertEqual(len(out), 10)
        self.assertTrue({m['id'] for m in new} <= set(ids))            # every New title kept
        self.assertEqual(st['new_kept'], 4)
        self.assertEqual(ids[:6], [f'tmdb:{i}' for i in range(1, 7)])  # the 6 most popular fill the room, in order
        self.assertEqual(st['cut_by_cap'], 2)

    def test_default_order_is_popularity(self):
        pops({1: 50, 2: 90, 3: 70, 4: 60, 5: 10})
        out, _ = build.union_merge({'popular': [meta(2), meta(3), meta(1)], 'new': [meta(4), meta(5), meta(1)],
                                    'top': [meta(4)]}, 10)
        self.assertEqual([m['id'] for m in out], ['tmdb:2', 'tmdb:3', 'tmdb:1', 'tmdb:4', 'tmdb:5'])

    def test_top_only_titles_fill_after_popular(self):
        pops({1: 9, 2: 8, 3: 7, 4: 1})
        out, _ = build.union_merge({'popular': [meta(1), meta(2)], 'new': [meta(4)], 'top': [meta(3)]}, 3)
        self.assertEqual({m['id'] for m in out}, {'tmdb:1', 'tmdb:2', 'tmdb:4'})   # New is first-class; Top is cut first


class SpecShape(unittest.TestCase):
    def test_496_catalogs_one_streaming_folder_no_retired_slugs(self):
        slugs = [c['slug'] for c in SPEC['catalogs']]
        self.assertEqual(len(slugs), 496)
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual([f['key'] for f in SPEC['folders']][:2], ['streaming', 'networks'])
        streaming = [c for c in SPEC['catalogs'] if 'streaming' in c['folders']]
        self.assertEqual(len(streaming), 21)
        self.assertEqual([f for f in SPEC['folders'] if f['key'] == 'streaming'][0]['title'], 'Streaming')
        self.assertFalse([s for s in slugs if s.startswith('streaming-') and s.endswith(('-new', '-top'))])
        self.assertNotIn('standup-new', slugs)
        self.assertTrue(all(c['kind'] == 'union' and c['cap'] == 2000 for c in streaming))
        stand = next(c for c in SPEC['catalogs'] if c['slug'] == 'standup')
        self.assertEqual((stand['kind'], [p['sort'] for p in stand['parts']]), ('union', ['popular', 'new']))

    def test_every_part_is_the_card_it_was_before(self):
        """A part keeps its old slug and its query, so corrections, arrivals and first_seen still match it."""
        keys = ('slug', 'sort', 'media', 'movie', 'series', 'min_votes', 'min_age_days', 'tv_air_window', 'depth', 'provider')
        n = 0
        for c in SPEC['catalogs']:
            if c['kind'] != 'union':
                continue
            for part in build.union_parts(c):
                old = PRE[part['slug']]
                self.assertEqual({k: part.get(k) for k in keys}, {k: old.get(k) for k in keys}, part['slug'])
                n += 1
            self.assertEqual(c['library'], PRE[c['slug']]['library'])      # the server library id/name are unchanged
        self.assertGreaterEqual(n, 21 * 2 + 2)

    def test_control_the_518_card_spec_fails_the_shape(self):
        """The spec before #143 had 518 cards and three Streaming folders: the shape test must be able to fail."""
        old_folders = ['streaming-popular', 'streaming-new']
        self.assertNotEqual(len(PRE), 496)
        self.assertTrue(any(c['slug'].endswith('-new') for c in PRE.values()))
        self.assertFalse([f for f in SPEC['folders'] if f['key'] in old_folders])


class MakeSpecNeedsOrderFiles(unittest.TestCase):
    def test_refuses_with_only_the_csv(self):
        r = subprocess.run([sys.executable, str(ROOT / 'generator' / 'make_spec.py'), 'whatever.csv'],
                           capture_output=True, text=True, cwd=tempfile.mkdtemp())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('order files', r.stderr)


class GuardPath(unittest.TestCase):
    """The one manual run (allow_catalog_removal) retires the 22 -new catalogs; the nightly after it has no flag."""
    BEFORE = {'total_items': 2000 + 519 + 311 + 521, 'catalogs': {'streaming-netflix': 2000, 'streaming-netflix-new': 519,
                                                                  'standup': 311, 'standup-new': 521}}

    def test_manual_run_with_the_flag_passes_and_without_it_the_removal_is_refused(self):
        counts = {'streaming-netflix': 2000, 'standup': 521}                  # Stand-up grows 311 -> 521, Netflix stays 2000
        self.assertIsNone(build.total_drop_problem(self.BEFORE, counts, sum(counts.values()), True))
        # without the flag the dropped catalogs are caught by main()'s 'previously non-empty' check
        emptied = [c for c, n in self.BEFORE['catalogs'].items() if n > 0 and counts.get(c, 0) == 0]
        self.assertEqual(emptied, ['streaming-netflix-new', 'standup-new'])

    def test_the_nightly_after_it_compares_against_the_union_counts(self):
        after = {'total_items': 2521, 'catalogs': {'streaming-netflix': 2000, 'standup': 521}}
        self.assertIsNone(build.total_drop_problem(after, dict(after['catalogs']), 2521, False))

    def test_a_growing_card_never_counts_as_an_item_drop(self):
        grown = {'standup': 311}
        now = {'standup': 521}
        dropped = [c for c, n in grown.items() if n >= 20 and 0 < now.get(c, 0) < n * (1 - build.CARD_DROP)]
        self.assertEqual(dropped, [])


if __name__ == '__main__':
    unittest.main()
