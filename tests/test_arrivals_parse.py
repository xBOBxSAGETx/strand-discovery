"""Parser and baseline-guard tests for generator/arrivals.py (standard library only; no network, no TMDB):

    python -m unittest discover -s tests

The page excerpts are synthetic (invented titles in the sources' layouts); no source text is stored in this repo.
"""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
for var in ('SD_HTTP_CACHE', 'SD_CACHE', 'SD_STATE'):
    os.environ[var] = str(Path(_tmp) / var.lower())
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import arrivals as a  # noqa: E402

FB_URL = 'https://film-book.com/example-october-2026-schedule/'
VT_URL = 'https://www.vitalthrills.com/example-october-2026/'


def fb_page(body):
    return f'<html><body><nav>sidebar</nav><article>{body}</article></body></html>'


def vt_page(body):
    return f'<html><body><div class="entry-content">{body}</div></article></body></html>'


def titles(rows):
    return [r['title'] for r in rows]


class FilmBookMonthLevel(unittest.TestCase):
    def test_month_heading_rows_dated_the_first(self):
        rows = a.fb_rows(fb_page(
            '<h2>New Movies Coming to Mubi in October 2026</h2><p>Intro text.</p>'
            '<h2>Mubi October 2026 Schedule</h2>'
            '<h3>Available in October</h3><ul><li>Quiet Harbor (2025)</li><li>The Lantern Keeper (2024)</li></ul>'),
            'mubi', 2026, 10, FB_URL)
        self.assertEqual(titles(rows), ['Quiet Harbor', 'The Lantern Keeper'])
        self.assertEqual({(r['date'], r['precision'], r['service']) for r in rows}, {('2026-10-01', 'month', 'mubi')})
        self.assertEqual([r['year'] for r in rows], [2025, 2024])

    def test_new_on_service_in_month_heading(self):
        rows = a.fb_rows(fb_page('<h3>Plex September 2026 Schedule</h3><h3>New on Plex in September</h3>'
                                 '<ul><li>Paper Comets (2019)</li></ul>'), 'plex', 2026, 9, FB_URL)
        self.assertEqual([(r['title'], r['date'], r['precision']) for r in rows], [('Paper Comets', '2026-09-01', 'month')])

    def test_departures_heading_stops_the_month_list(self):
        rows = a.fb_rows(fb_page(
            '<h2>Mubi October 2026 Schedule</h2><h3>Available in October</h3><ul><li>Quiet Harbor (2025)</li></ul>'
            '<h3>Leaving Mubi in October</h3><ul><li>Old Reel (1999)</li></ul>'), 'mubi', 2026, 10, FB_URL)
        self.assertEqual(titles(rows), ['Quiet Harbor'])

    def test_day_headings_still_day_precision(self):
        rows = a.fb_rows(fb_page('<h3>Starz July 2026 Schedule</h3><h3>July 1</h3><ul><li>Iron Orchard (2012)</li></ul>'
                                 '<h3>July 10</h3><ul><li>Salt Road, Season 2</li></ul>'), 'starz', 2026, 7, FB_URL)
        self.assertEqual([(r['title'], r['date'], r['precision']) for r in rows],
                         [('Iron Orchard', '2026-07-01', 'day'), ('Salt Road', '2026-07-10', 'day')])

    def test_table_date_cells(self):
        rows = a.fb_rows(fb_page('<h3>Hulu June 2026 Schedule</h3><table>\n<tr>\n<td>date</td>\n<td>show</td>\n'
                                 '<td>category</td>\n<td>status</td>\n</tr>\n<tr>\n<td>6/4/26</td>\n<td>Glass Tide</td>\n'
                                 '<td>Hulu Original</td>\n<td>Added</td>\n</tr>\n</table>'),
                         'hulu', 2026, 6, FB_URL)
        self.assertEqual([(r['title'], r['date'], r['precision']) for r in rows], [('Glass Tide', '2026-06-04', 'day')])

    def test_bbc_select_section_is_not_britbox(self):
        rows = a.fb_rows(fb_page('<h3>BritBox June 2026 Schedule</h3><h3>June 3</h3><ul><li>Moor Lane, Season 1</li></ul>'
                                 '<p>BBC SELECT MONTHLY LISTINGS</p><h3>June 9</h3><ul><li>Rivers of Stone</li></ul>'),
                         'britbox', 2026, 6, FB_URL)
        self.assertEqual(titles(rows), ['Moor Lane'])

    def test_leaving_list_without_heading_is_flagged(self):
        leaving = ''.join(f'<li>Faded Title {i}</li>' for i in range(24))
        rows = a.fb_rows(fb_page(f'<h2>Starz October 2026 Schedule</h2><h3>October 1</h3><ul><li>Lone Arrival</li></ul>'
                                 f'<h3>October 31</h3><ul>{leaving}</ul>'), 'starz', 2026, 10, FB_URL)
        self.assertEqual(len(rows), 25)
        self.assertEqual(a.departures_signature(rows), '24 of 25 rows dated a month end')

    def test_arrivals_on_the_first_are_not_flagged(self):
        rows = [{'date': '2026-10-01'}] * 30 + [{'date': '2026-10-31'}] * 5
        self.assertEqual(a.departures_signature(rows), '')
        self.assertEqual(a.departures_signature([{'date': '2026-10-31'}] * 19), '')     # too few rows to judge


class VitalThrillsTrailingDates(unittest.TestCase):
    def test_trailing_date_lines(self):
        rows = a.vt_rows(vt_page(
            '<h2>SHUDDER OCTOBER 2026 HIGHLIGHTS</h2>'
            '<p>Night Orchard (Shudder Original Film) &#8211; New Film Premieres Friday, October 2 '
            '(Available in the US and CA)</p><p>A description sentence about the film. It is long.</p>'
            '<p>HOLLOW PINES 2 &#8211; Premieres Oct. 9</p>'
            '<p>Grey Coast (Shudder Exclusive Film) &#8211; New Film Premieres Friday, October 16 '
            '(Available in Canada)</p>'
            '<p>Marsh Lights Season 2 (Shudder Original Series) &#8211; New Episodes Continue Weekly Through October 30</p>'
            '<p>LEAVING OCTOBER 31</p><p>Old Fog &#8211; Available until October 31</p>'),
            'shudder', 'shudder', 2026, 10, VT_URL)
        self.assertEqual([(r['title'], r['date'], r['precision'], r['service']) for r in rows],
                         [('Night Orchard', '2026-10-02', 'day', 'shudder'), ('HOLLOW PINES 2', '2026-10-09', 'day', 'shudder')])

    def test_amc_plus_sections(self):
        rows = a.vt_rows(vt_page(
            '<h2>AMC+ OCTOBER 2026 HIGHLIGHTS</h2>'
            '<p>AMC+</p><p>Ridge County Season 3 (AMC+ Original Series) &#8211; Season Premieres Sunday, October 4</p>'
            '<p>SHUDDER (also available on AMC+)</p>'
            '<p>Night Orchard (Shudder Original Film) &#8211; New Film Premieres Friday, October 2</p>'
            '<p>SUNDANCE NOW (also available on AMC+)</p>'
            '<p>Cold Ledger (Sundance Now Documentary) &#8211; Documentary Series Premieres Thursday, October 8</p>'
            '<p>HIDIVE</p><p>Star Lantern &#8211; Premieres October 3</p>'),
            'amcplus', 'amc-plus', 2026, 10, VT_URL)
        self.assertEqual([(r['title'], r['service'], r['date']) for r in rows],
                         [('Ridge County', 'amcplus', '2026-10-04'), ('Night Orchard', 'shudder', '2026-10-02'),
                          ('Cold Ledger', 'amcplus', '2026-10-08')])

    def test_bbc_select_stops_vital_thrills_britbox(self):
        rows = a.vt_rows(vt_page('<h2>BRITBOX JUNE 2026 SCHEDULE</h2><p>AVAILABLE JUNE 3</p>'
                                 '<p>Moor Lane Season 1 | New to BritBox | 6 x 60\'</p>'
                                 '<h2>BBC SELECT MONTHLY LISTINGS</h2><p>AVAILABLE JUNE 9</p>'
                                 '<p>Rivers of Stone | Available in North America | 1 x 60\'</p>'),
                         'britbox', 'britbox', 2026, 6, VT_URL)
        self.assertEqual(titles(rows), ['Moor Lane'])


class BaselineGuard(unittest.TestCase):
    """refresh(): an arrival dated after the logger baseline, for a title present at the baseline and never removed,
    is dropped (pre-listing / rotation); past-dated events and titles that arrived after the baseline are kept; a new
    season (>= 2, higher than any season the show had at the baseline) is kept, a re-listed old season is dropped."""
    TOP = {4: 2, 6: 4}                                  # highest season aired at the baseline (stubbed TMDB lookup)

    def setUp(self):
        self._lookup = a.highest_season_at
        a.highest_season_at = lambda tid, base: self.TOP.get(tid)

    def tearDown(self):
        a.highest_season_at = self._lookup

    def state(self):
        sig = {}
        for key, date, kind, season in (('movie:1', '2026-10-01', 'title', None),     # present at baseline -> dropped
                                        ('movie:2', '2026-10-01', 'title', None),     # first seen after baseline
                                        ('movie:3', '2026-09-20', 'title', None),     # past-dated
                                        ('series:4', '2026-10-02', 'season', 3),      # S3 > S2 at baseline: new
                                        ('movie:5', '2026-10-01', 'title', None),     # present at baseline, then left
                                        ('series:6', '2026-10-03', 'season', 3)):     # S3 re-listed, S4 out: old
            a.merge(sig, key, (date, 'day', 'vt', 1.0, kind, season, 'https://example.test/guard'), '2026-09-15')
        items = {'m:1': ['baseline', '2026-10-10'], 'm:2': ['2026-10-01', '2026-10-10'],
                 'm:3': ['baseline', '2026-10-10'], 't:4': ['baseline', '2026-10-10'], 'm:5': ['baseline', '2026-09-30'],
                 't:6': ['baseline', '2026-10-10']}
        return {'providers': {'x': {'baseline': '2026-09-28', 'last_run': '2026-10-10', 'items': items, 'signals': sig}}}

    def keys(self, today='2026-10-10'):
        return sorted(c['key'] for c in a.refresh(self.state(), today, lambda s, m, i: True)['x'])

    def test_present_at_baseline_and_dated_after_is_dropped(self):
        self.assertEqual(self.keys(), ['movie:2', 'movie:3', 'movie:5', 'series:4'])
        self.assertEqual(sorted(a.GUARD_DROPS['x']), ['movie:1', 'series:6'])

    def test_not_present_at_baseline_is_kept(self):
        self.assertIn('movie:2', self.keys())
        self.assertIn('movie:5', self.keys())               # left the service after the baseline: a real re-arrival

    def test_past_dated_is_untouched(self):
        self.assertIn('movie:3', self.keys())

    def test_future_item_is_dropped_before_its_date_too(self):
        self.keys('2026-09-29')
        self.assertIn('movie:1', a.GUARD_DROPS['x'])

    def test_new_season_switch(self):
        try:
            a.BASELINE_GUARD_KEEP_NEW_SEASONS = False
            self.assertNotIn('series:4', self.keys())
            self.assertEqual(sorted(a.GUARD_DROPS['x']), ['movie:1', 'series:4', 'series:6'])
        finally:
            a.BASELINE_GUARD_KEEP_NEW_SEASONS = True

    def test_relisted_old_season_is_dropped(self):
        self.assertNotIn('series:6', self.keys())           # S3 while S4 had aired at the baseline
        self.assertIn('series:4', self.keys())              # S3 while S2 was the latest: a new season

    def test_unknown_seasons_drop(self):
        self.TOP = {6: 4}                                   # series:4 lookup failed (None): not shown to be new
        self.assertNotIn('series:4', self.keys())

    def test_highest_season_at_edge_cases(self):
        seasons = [{'season_number': 0, 'air_date': '2030-01-01'},        # specials: never counted
                   {'season_number': 1, 'air_date': '2020-05-01'},
                   {'season_number': 2, 'air_date': '2026-09-28'},        # aired on the baseline day: counts
                   {'season_number': 3, 'air_date': None},                # no air_date: not aired
                   {'season_number': 4, 'air_date': '2026-11-01'}]        # after the baseline
        tmdb, a.build.tmdb = a.build.tmdb, lambda path, **kw: {'seasons': seasons}
        try:
            self.assertEqual(self._lookup(990001, '2026-09-28'), 2)
            self.assertEqual(self._lookup(990002, '2019-01-01'), 0)
            a.build.tmdb = lambda path, **kw: (_ for _ in ()).throw(RuntimeError('TMDB HTTP 500'))
            self.assertIsNone(self._lookup(990003, '2026-09-28'))
            self.assertNotIn('tvseasons-at:990003:2026-09-28', a.build._stable)   # an error is not cached
        finally:
            a.build.tmdb = tmdb
            for k in [k for k in a.build._stable if k.startswith('tvseasons-at:99000')]:
                del a.build._stable[k]


if __name__ == '__main__':
    unittest.main()
