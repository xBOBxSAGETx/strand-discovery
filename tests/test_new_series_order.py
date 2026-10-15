"""Issue #45: a release-date "New" card's series must come newest first by first-air date, also with tv_air_window
(which used to force popularity order). Stubbed TMDB, no network."""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

DFLT = {'depth': {'new': 500}}
# the live BritBox series, in the popularity order that was served: 1985 1972 2021 2024 2002 ...
YEARS = [1985, 1972, 2021, 2024, 2002, 2006, 2023, 2018, 2013, 2022, 1979, 2009, 1999, 2026, 2025]
SHOWS = [{'id': i + 1, 'name': f'S{i}', 'first_air_date': f'{y}-03-01', 'popularity': 100.0 - i, 'vote_count': 9,
          'genre_ids': [18]} for i, y in enumerate(YEARS)]
CARD = {'slug': 'x-new', 'kind': 'discover', 'media': ['series'], 'sort': 'new', 'tv_air_window': 45,
        'series': {'with_watch_providers': '151'}}


def stub(path, **params):
    key = params['sort_by'].rpartition('.')[0]
    rows = sorted(SHOWS, key=lambda r: r[key], reverse=True)      # TMDB honours the sort_by it is given
    return {'page': 1, 'results': rows, 'total_results': len(rows), 'total_pages': 1}


class NewSeriesOrder(unittest.TestCase):
    def test_air_window_card_is_newest_first(self):
        real, build.tmdb = build.tmdb, stub
        try:
            out = build.discover(CARD, 'series', 500, DFLT)
        finally:
            build.tmdb = real
        years = [int(m['releaseInfo']) for m in out]
        self.assertEqual(len(years), len(YEARS))
        self.assertEqual(years, sorted(YEARS, reverse=True))


if __name__ == '__main__':
    unittest.main()
