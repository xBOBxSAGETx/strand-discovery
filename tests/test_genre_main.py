"""Issue #52: a genre card counts a title only when the card's genre is among its first K tags (build.genre_fits;
K is 3, Animation any, Horror 1, Action 2).
Stubbed TMDB (no network); the genre_ids are TMDB's real order, taken from the build's pool dump (run 38807142043).
Replays the live break (Horror Top Rated held Come and See, Coraline and Ghosts) and the recall the first
"first tag only" version lost (Schindler's List is [Drama, History, War] and was dropped from History and War).

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
# (tmdb id, title, genre_ids in TMDB order)  18 Drama, 36 History, 10752 War, 27 Horror, 9648 Mystery, 35 Comedy, 80 Crime
ROWS = [(7340, 'Carrie', [27, 53]),
        (25237, 'Come and See', [18, 10752, 27, 36]),
        (14836, 'Coraline', [16, 10751, 14, 27]),
        (37793, 'Ghosts', [10402, 27]),
        (424, "Schindler's List", [18, 36, 10752]),
        (857, 'Saving Private Ryan', [10752, 18, 36]),
        (546554, 'Knives Out', [35, 80, 9648]),
        (1084242, 'Zootopia 2', [16, 12, 35, 9648, 10751]),
        (354912, 'Coco', [10751, 16, 10402, 12]),
        (803796, 'KPop Demon Hunters', [14, 10402, 35, 16]),
        (129, 'Spirited Away', [16, 10751, 14]),
        (120, 'The Lord of the Rings: The Fellowship of the Ring', [12, 14, 28]),
        (671, "Harry Potter and the Philosopher's Stone", [12, 14]),
        (19404, 'Dilwale Dulhania Le Jayenge', [35, 18, 10749]),
        (372058, 'Your Name.', [16, 10749, 18]),
        (31132, 'Regular Show', [16, 35, 10759, 10765])]


def item(i, title, genres, n):
    return {'id': i, 'title': title, 'popularity': 100.0 - n, 'release_date': '2001-01-01', 'poster_path': f'/p{i}.jpg',
            'genre_ids': genres}


def stub(path, **params):
    rows = [item(*r, n) for n, r in enumerate(ROWS)]
    return {'page': params['page'], 'results': rows if params['page'] == 1 else [], 'total_results': len(rows),
            'total_pages': 1}


def names(card):
    real, build.tmdb = build.tmdb, stub
    try:
        return [m['name'] for m in build.discover(card, 'movie', 100, DFLT)]
    finally:
        build.tmdb = real


def genre_card(genre, slug='genre-x'):
    return {'slug': slug, 'kind': 'discover', 'media': ['movie'], 'sort': 'popular', 'folders': ['genres-top'],
            'movie': {'with_genres': genre}}


class MainGenre(unittest.TestCase):
    def test_horror_drops_the_bad_examples(self):
        # Come and See (Horror 3rd), Coraline (4th) and Ghosts (a music video, Horror 2nd) are gone
        self.assertEqual(names(genre_card('27')), ['Carrie'])

    def test_history_keeps_schindlers_list_and_drops_a_far_tag(self):
        # Schindler's List has History 2nd (after Drama), Saving Private Ryan 3rd; Come and See has it 4th
        self.assertEqual(names(genre_card('36')), ["Schindler's List", 'Saving Private Ryan'])

    def test_war_keeps_schindlers_list_with_war_third(self):
        self.assertEqual(names(genre_card('10752')),
                         ['Come and See', "Schindler's List", 'Saving Private Ryan'])

    def test_mystery_keeps_knives_out_and_drops_zootopia(self):
        self.assertEqual(names(genre_card('9648')), ['Knives Out'])

    def test_animation_counts_at_any_position(self):
        got = names(genre_card('16'))
        for t in ('Coco', 'KPop Demon Hunters', 'Spirited Away', 'Coraline', 'Your Name.', 'Zootopia 2'):
            self.assertIn(t, got)

    def test_fantasy_keeps_lord_of_the_rings_harry_potter_and_spirited_away(self):
        got = names(genre_card('14'))
        for t in ('The Lord of the Rings: The Fellowship of the Ring', "Harry Potter and the Philosopher's Stone",
                  'Spirited Away', 'KPop Demon Hunters', 'Coraline'):
            self.assertIn(t, got)
        self.assertNotIn('Carrie', got)

    def test_romance_keeps_comedy_drama_romance_films(self):
        self.assertEqual(names(genre_card('10749')), ['Dilwale Dulhania Le Jayenge', 'Your Name.'])

    def test_action_keeps_first_two_tags_only(self):
        # Regular Show lists Action & Adventure 3rd; The Lord of the Rings has Action 3rd too
        self.assertEqual(names(genre_card('10759')), [])
        self.assertEqual(names(genre_card('28')), [])

    def test_non_genre_card_is_unaffected(self):
        card = {**genre_card('27'), 'slug': 'studio-x', 'folders': ['studios']}
        self.assertEqual(names(card), [r[1] for r in ROWS])

    def test_card_without_with_genres_is_unaffected(self):
        card = {**genre_card('27'), 'movie': {'with_keywords': '210024'}}
        self.assertEqual(len(names(card)), len(ROWS))


if __name__ == '__main__':
    unittest.main()
