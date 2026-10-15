"""A sliced provider (catalogue read by date slices: Prime Video, Tubi, Plex) must not fill its New card with
first-seen items: slice drift logs library titles as arrivals and crowds out the real dated signals
(2026-10-15 Prime New: 2 dated arrivals + 998 first seen). Signals first, then release date. Stubbed, no network:

    python -m unittest tests.test_sliced_no_first_seen
"""
import os, sys, tempfile, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(tempfile.mkdtemp(prefix='sd-tests-')) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

DFLT = {'depth': {'popular': 1000, 'new': 500, 'top': 500, 'release_asc': 1000, 'votes': 1000}}
CARD = {'slug': 'sd-streaming-prime-video-new', 'kind': 'discover', 'provider': 'prime-video', 'media': ['movie'],
        'sort': 'new', 'movie': {}}
SIGNALS = [{'key': f'movie:{i}', 'date': f'2026-10-0{i}', 'precision': 'day', 'popularity': 1.0} for i in range(1, 7)]
DRIFT = [{'id': f'tmdb:{1000 + i}', 'type': 'movie', 'name': f'Drift{i}', 'first_seen': '2026-10-14', 'popularity': 1.0}
         for i in range(50)]
RELEASE = [{'id': f'tmdb:{2000 + i}', 'type': 'movie', 'name': f'Rel{i}'} for i in range(3)]


class SlicedNoFirstSeen(unittest.TestCase):
    def run_card(self, sliced):
        saved = build.FS_SUMMARY, build.FS_CANDIDATES, build.SIGNALS, build.discover, build.details_preview
        build.FS_SUMMARY = {'providers': {'prime-video': {'mode': 'first_seen', 'sliced': sliced}}}
        build.FS_CANDIDATES = {'prime-video': DRIFT}
        build.SIGNALS = {'prime-video': SIGNALS}
        build.discover = lambda card, media, depth, dflt: [dict(m) for m in RELEASE]
        build.details_preview = lambda media, tid: {'id': f'tmdb:{tid}', 'type': media, 'name': f'Sig{tid}'}
        try:
            return build.first_seen_mode(CARD), build.build_one(CARD, {'depth': {'new': 8}, **DFLT})
        finally:
            build.FS_SUMMARY, build.FS_CANDIDATES, build.SIGNALS, build.discover, build.details_preview = saved

    def test_sliced_signals_first_no_first_seen(self):
        mode, (metas, note) = self.run_card(sliced=True)
        self.assertFalse(mode)
        ids = [m['id'] for m in metas]
        self.assertEqual({i for i in ids if i.startswith('tmdb:1')} & {m['id'] for m in DRIFT}, set())  # no drift
        self.assertEqual(len([i for i in ids if i.startswith('tmdb:') and int(i[5:]) < 10]), 6)          # all signals
        self.assertIn('0 first seen', note)

    def test_unsliced_still_first_seen(self):                         # control: the non-sliced path is unchanged
        mode, _ = self.run_card(sliced=False)
        self.assertTrue(mode)


if __name__ == '__main__':
    unittest.main()
