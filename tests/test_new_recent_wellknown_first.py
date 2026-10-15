"""Issue #49: on a "New on X" card, arrivals of the last RECENT_DAYS come most-popular first (obscure pushed down),
older arrivals follow in date order. Both the arrival card and the first-seen card. Stubbed, no network."""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

TODAY = '2026-10-15'
CARD = {'slug': 'x-new', 'provider': 'x', 'kind': 'discover', 'media': ['movie'], 'sort': 'new'}
# (tmdb id, arrival date, popularity): the newest day is an obscure title, the same week a well-known one
ROWS = [(1, '2026-10-14', 2.0), (2, '2026-10-13', 900.0), (3, '2026-10-10', 40.0),
        (4, '2026-09-01', 800.0), (5, '2026-08-20', 700.0)]
WANT = ['tmdb:2', 'tmdb:3', 'tmdb:1', 'tmdb:4', 'tmdb:5']   # recent window by popularity, then older by date


def meta(i):
    return {'id': f'tmdb:{i}', 'type': 'movie', 'name': f'T{i}'}


class RecentWellKnownFirst(unittest.TestCase):
    def setUp(self):
        self.saved = (build.TODAY, build.SIGNALS, build.FS_SUMMARY, build.FS_CANDIDATES, build.details_preview, build.discover)
        build.TODAY = TODAY
        build.details_preview = lambda media, tid: meta(tid)
        build.discover = lambda *a, **k: []

    def tearDown(self):
        (build.TODAY, build.SIGNALS, build.FS_SUMMARY, build.FS_CANDIDATES, build.details_preview, build.discover) = self.saved

    def test_arrival_card(self):
        build.FS_SUMMARY, build.FS_CANDIDATES = None, {}
        build.SIGNALS = {'x': [{'date': d, 'precision': 'day', 'popularity': p, 'key': f'movie:{i}'} for i, d, p in ROWS]}
        out, _ = build.arrival_card(CARD, 100, {})
        self.assertEqual([m['id'] for m in out], WANT)

    def test_first_seen_card(self):
        build.FS_CANDIDATES = {'x': [{**meta(i), 'first_seen': d, 'popularity': p} for i, d, p in ROWS]}
        out = build.first_seen_card(CARD, 100)
        self.assertEqual([m['id'] for m in out], WANT)


if __name__ == '__main__':
    unittest.main()
