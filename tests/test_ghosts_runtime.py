"""Issue #118: a Top Rated / Most Voted movie card promises a 40-minute floor (no shorts). The build asks TMDB's
/discover for that (with_runtime.gte=40), but TMDB's discover let Ghosts (Michael Jackson, 1997) through while its own
/movie/37793 says runtime 39 (run 38872930504: discover found it at gte 40, 39 and 38 alike). The build now checks the
runtime of each title from /movie/{id} too (durable cache key runtime:{id}, shared with the director and awards cards).
Stubbed TMDB (no network).

    python -m unittest tests.test_ghosts_runtime
"""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

DFLT = {'depth': {'popular': 1000, 'new': 500, 'top': 500, 'release_asc': 1000, 'votes': 1000}}
# (tmdb id, title, runtime TMDB's /movie/{id} gives); every one of them passed TMDB's own discover runtime filter
ROWS = [(1, 'Feature', 120), (37793, 'Ghosts', 39), (2, 'Exactly forty', 40), (3, 'Unknown runtime', 0), (4, 'Detail fails', None)]


def stub(path, **params):
    if path == '/discover/movie':
        rows = [{'id': i, 'title': t, 'popularity': 50.0 - n, 'release_date': '1997-09-04', 'poster_path': f'/p{i}.jpg',
                 'genre_ids': [10402]} for n, (i, t, _) in enumerate(ROWS)]
        return {'page': 1, 'results': rows, 'total_results': len(rows), 'total_pages': 1}
    tid = int(path.rsplit('/', 1)[1])
    runtime = {i: r for i, _, r in ROWS}[tid]
    if runtime is None:
        raise RuntimeError(f'TMDB HTTP 500 on {path}')
    return {'id': tid, 'runtime': runtime}


def names(sort):
    card = {'slug': 'genre-music-' + sort, 'kind': 'discover', 'media': ['movie'], 'sort': sort,
            'folders': ['genres-top'], 'movie': {'with_genres': '10402'}}
    real, build.tmdb = build.tmdb, stub
    build._stable.clear()
    try:
        return [m['name'] for m in build.discover(card, 'movie', 100, DFLT)]
    finally:
        build.tmdb = real


class GhostsRuntime(unittest.TestCase):
    def test_top_card_drops_ghosts_at_39_minutes(self):
        got = names('top')
        self.assertNotIn('Ghosts', got)
        self.assertEqual(got, ['Feature', 'Exactly forty', 'Unknown runtime', 'Detail fails'])  # 40 stays, 0 and an error stay

    def test_most_voted_card_has_the_same_floor(self):
        self.assertNotIn('Ghosts', names('votes'))

    def test_popular_card_has_no_floor(self):
        self.assertIn('Ghosts', names('popular'))


if __name__ == '__main__':
    unittest.main()
