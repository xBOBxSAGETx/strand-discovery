"""Weekly shelf compare (accuracy_check.snapshot / compare_weeks, DuckDB): two tiny fake weeks. A title that left a card
is flagged (and flagged again if last week's audit marked it correct); an unchanged week is clean; no last week passes.
Skips when duckdb isn't installed (CI installs the pin from requirements-dev.txt).

    python -m unittest tests.test_weekly_compare
"""
import json, sys, tempfile, unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'generator'))
try:
    import duckdb  # noqa: F401
    import accuracy_check as ac
except ImportError:
    ac = None


def site(root, shelves):
    d = Path(root) / 'catalog' / 'movie'
    d.mkdir(parents=True)
    for cid, items in shelves.items():
        (d / f'{cid}.json').write_text(json.dumps({'metas': [
            {'id': f'tmdb:{i}', 'type': t, 'name': f'T{i}'} for t, i in items]}), encoding='utf-8')
    return Path(root)


WEEK1 = {'sd-streaming-netflix-new': [('movie', 1), ('series', 2), ('movie', 3)], 'sd-streaming-hulu-new': [('movie', 9)],
         'sd-genre-empty': []}
OK = {('sd-streaming-netflix-new', 'movie:1')}


@unittest.skipIf(ac is None, 'duckdb not installed')
class WeeklyCompare(unittest.TestCase):
    def snap(self, tmp, name, shelves, ok=()):
        out = Path(tmp) / f'snap-{name}'
        files, titles = ac.snapshot(site(Path(tmp) / f'site-{name}', shelves), out, ok)
        return out, files, titles

    def test_reads_every_shelf_file_even_an_empty_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, files, titles = self.snap(tmp, 'a', WEEK1)
        self.assertEqual((files, titles), (3, 4))

    def test_lost_title_is_flagged_and_audit_ok_title_called_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev, _, _ = self.snap(tmp, 'a', WEEK1, OK)
            week2 = dict(WEEK1, **{'sd-streaming-netflix-new': [('series', 2), ('movie', 3)]})     # movie:1 left
            cur, _, _ = self.snap(tmp, 'b', week2)
            cmp = ac.compare_weeks(prev, cur)
        self.assertEqual(cmp['lost_titles'], 1)
        self.assertEqual(cmp['top_cards'], [('sd-streaming-netflix-new', 1)])
        self.assertEqual(cmp['lost_audited_ok'], [('sd-streaming-netflix-new', 'movie:1', 'T1')])
        self.assertTrue(any('marked correct' in n and 'movie:1' in n for n in ac.week_notes(cmp)))

    def test_lost_title_not_marked_ok_is_counted_but_not_called_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev, _, _ = self.snap(tmp, 'a', WEEK1, OK)
            week2 = dict(WEEK1, **{'sd-streaming-netflix-new': [('movie', 1), ('series', 2)]})     # movie:3 left
            cur, _, _ = self.snap(tmp, 'b', week2)
            cmp = ac.compare_weeks(prev, cur)
        self.assertEqual(cmp['lost_titles'], 1)
        self.assertEqual(cmp['lost_audited_ok'], [])

    def test_unchanged_week_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev, _, _ = self.snap(tmp, 'a', WEEK1, OK)
            cur, _, _ = self.snap(tmp, 'b', WEEK1)
            cmp = ac.compare_weeks(prev, cur)
        self.assertEqual((cmp['lost_titles'], cmp['cards_with_losses'], cmp['lost_audited_ok']), (0, 0, []))

    def test_new_titles_are_not_losses(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev, _, _ = self.snap(tmp, 'a', WEEK1)
            cur, _, _ = self.snap(tmp, 'b', dict(WEEK1, **{'sd-streaming-hulu-new': [('movie', 9), ('movie', 10)]}))
            self.assertEqual(ac.compare_weeks(prev, cur)['lost_titles'], 0)

    def test_no_last_week_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cur, _, _ = self.snap(tmp, 'b', WEEK1)
            empty = Path(tmp) / 'empty'
            empty.mkdir()
            self.assertIsNone(ac.compare_weeks(empty, cur))
        self.assertEqual(ac.week_notes(None), ['weekly compare: no last week yet'])


@unittest.skipIf(ac is None, 'duckdb not installed')
class FetchSiteFallback(unittest.TestCase):
    def test_fetch_site_into_empty_pages_writes_every_first_page(self):
        """The live-site fallback starts from an empty pages dir (rm -rf pages && mkdir pages)."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'site'
            (src / 'catalog' / 'movie').mkdir(parents=True)
            page = json.dumps({'metas': []})
            for cid in ('sd-a', 'sd-b'):
                (src / 'catalog' / 'movie' / f'{cid}.json').write_text(page)
            (src / 'summary.json').write_text(json.dumps({'catalogs': {'sd-a': 0, 'sd-b': 0}, 'new_card_modes': {}}))
            pages = Path(tmp) / 'pages'
            pages.mkdir()
            with mock.patch.object(ac.time, 'sleep'):
                ac.fetch_site(src.as_uri(), pages)
            got = sorted(p.name for p in (pages / 'catalog' / 'movie').glob('sd-*.json'))
        self.assertEqual(got, ['sd-a.json', 'sd-b.json'])


if __name__ == '__main__':
    unittest.main()
