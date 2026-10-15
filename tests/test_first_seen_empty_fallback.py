"""A provider past the first-seen history window but with NO fresh candidates must not get an empty New card
(the 2026-10-12 BritBox break: `GUARD: empty catalogs: ['sd-streaming-britbox-new']`, deploy skipped). It falls back
to the release-date order. Stubbed discover(), no network:

    python -m unittest tests.test_first_seen_empty_fallback
"""
import os, sys, tempfile, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(tempfile.mkdtemp(prefix='sd-tests-')) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

DFLT = {'depth': {'popular': 1000, 'new': 500, 'top': 500, 'release_asc': 1000, 'votes': 1000}}
CARD = {'slug': 'sd-streaming-britbox-new', 'kind': 'discover', 'provider': 'britbox', 'media': ['movie'],
        'sort': 'new', 'movie': {}}
RELEASE_ORDER = [{'id': f'tmdb:{i}', 'type': 'movie', 'name': f'T{i}'} for i in (3, 2, 1)]


class FirstSeenEmptyFallback(unittest.TestCase):
    def test_no_fresh_candidates_falls_back_to_release_date(self):
        saved = build.FS_SUMMARY, build.FS_CANDIDATES, build.SIGNALS, build.discover
        # >14 days of history (mode first_seen) but zero new arrivals: the provider key is present, its list empty
        build.FS_SUMMARY = {'providers': {'britbox': {'mode': 'first_seen'}}}
        build.FS_CANDIDATES = {'britbox': []}
        build.SIGNALS = {}
        build.discover = lambda card, media, depth, dflt: [dict(m) for m in RELEASE_ORDER]
        try:
            self.assertFalse(build.first_seen_mode(CARD))
            metas, _ = build.build_one(CARD, DFLT)
        finally:
            build.FS_SUMMARY, build.FS_CANDIDATES, build.SIGNALS, build.discover = saved
        self.assertTrue(metas)                                    # not empty (the guard would skip the deploy)
        self.assertEqual([m['id'] for m in metas], [m['id'] for m in RELEASE_ORDER])   # release-date order


if __name__ == '__main__':
    unittest.main()
