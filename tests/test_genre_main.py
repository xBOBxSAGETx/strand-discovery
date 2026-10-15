"""Issue #52: genre cards count a title only when the card's genre is its MAIN (first-listed) TMDB genre.
Stubbed TMDB (no network). Replays the live break: Horror Top Rated held a war film (Come and See) tagged
[10752, 27] next to real horror [27, 53].

    python -m unittest tests.test_genre_main
"""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

DFLT = {'depth': {'popular': 1000, 'new': 500, 'top': 500, 'release_asc': 1000, 'votes': 1000}}
ROWS = [(1, 'Real Horror', [27, 53]), (2, 'Come and See', [10752, 27]), (3, 'Coraline', [16, 27]),
        (4, 'Second Horror', [27]), (5, 'Third Horror', [27, 9648])]


def item(i, title, genres):
    return {'id': i, 'title': title, 'popularity': 100.0 - i, 'release_date': '2001-01-01', 'poster_path': f'/p{i}.jpg',
            'genre_ids': genres}


def stub(path, **params):
    rows = [item(*r) for r in ROWS]
    return {'page': params['page'], 'results': rows if params['page'] == 1 else [], 'total_results': len(rows),
            'total_pages': 1}


def names(card):
    real, build.tmdb = build.tmdb, stub
    try:
        return [m['name'] for m in build.discover(card, 'movie', 100, DFLT)]
    finally:
        build.tmdb = real


GENRE = {'slug': 'genre-horror-top', 'kind': 'discover', 'media': ['movie'], 'sort': 'popular',
         'folders': ['genres-top'], 'movie': {'with_genres': '27'}}
OTHER = {**GENRE, 'slug': 'studio-x', 'folders': ['studios']}


class MainGenreOnly(unittest.TestCase):
    def test_genre_card_keeps_only_main_genre_titles(self):
        self.assertEqual(names(GENRE), ['Real Horror', 'Second Horror', 'Third Horror'])

    def test_non_genre_card_is_unaffected(self):
        self.assertEqual(names(OTHER), [r[1] for r in ROWS])

    def test_several_ids_any_is_main(self):
        card = {**GENRE, 'movie': {'with_genres': '10752|27'}}
        self.assertEqual(names(card), ['Real Horror', 'Come and See', 'Second Horror', 'Third Horror'])

    def test_card_without_with_genres_is_unaffected(self):
        card = {**GENRE, 'movie': {'with_keywords': '210024'}}
        self.assertEqual(len(names(card)), len(ROWS))


if __name__ == '__main__':
    unittest.main()
