"""Issue #67: director cards add the TV series they directed (tv_credits, crew job Director); actor cards drop
voice-only and cameo/self/archive roles unless the person is mainly a voice actor. Stubbed TMDB, no network."""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

OLD = '2015-05-01'


def movie_crew(i, title, job='Director', votes=900):
    return {'id': i, 'title': title, 'job': job, 'release_date': '2000-01-01', 'vote_count': votes, 'genre_ids': [18]}


def tv_crew(i, name, job='Director', genres=(18,), votes=100):
    return {'id': i, 'name': name, 'job': job, 'first_air_date': '2010-09-19', 'vote_count': votes,
            'genre_ids': list(genres)}


def cast(i, title, character, media='movie', votes=500, order=0, eps=10, genres=(18,)):
    c = {'id': i, 'media_type': media, 'character': character, 'vote_count': votes, 'genre_ids': list(genres)}
    if media == 'movie':
        c.update(title=title, release_date=OLD, order=order)
    else:
        c.update(name=title, first_air_date=OLD, episode_count=eps)
    return c


def run(fn, credits, card):
    def stub(path, **params):
        if path.endswith('/movie_credits'):
            return {'crew': credits.get('movie_crew', [])}
        if path.endswith('/tv_credits'):
            return {'crew': credits.get('tv_crew', [])}
        if path.endswith('/combined_credits'):
            return {'cast': credits.get('cast', [])}
        return {'runtime': 120}                       # /movie/{id}: a feature
    real, build.tmdb = build.tmdb, stub
    build._stable.clear()
    try:
        dropped = {} if fn is build.actor else []
        return [m['name'] for m in fn(card, dropped)]
    finally:
        build.tmdb = real


class Directors(unittest.TestCase):
    CARD = {'person_id': 1, 'slug': 'dir-x'}

    def test_scorsese_boardwalk_empire_once(self):
        names = run(build.director, {
            'movie_crew': [movie_crew(10, 'Goodfellas')],
            'tv_crew': [tv_crew(20, 'Boardwalk Empire'), tv_crew(20, 'Boardwalk Empire'), tv_crew(21, 'Vinyl')]},
            self.CARD)
        self.assertEqual(names.count('Boardwalk Empire'), 1)
        self.assertIn('Vinyl', names)
        self.assertEqual(names[0], 'Goodfellas')       # movies keep their place; series follow

    def test_tv_talk_news_reality_non_director_and_tv_movie_are_out(self):
        names = run(build.director, {
            'movie_crew': [movie_crew(10, 'Some TV Movie')],
            'tv_crew': [tv_crew(30, 'Late Show', genres=(10767,)), tv_crew(31, 'Evening News', genres=(10763,)),
                        tv_crew(32, 'Dating Island', genres=(10764,)), tv_crew(33, 'Producer Only', job='Producer'),
                        {**tv_crew(34, 'Some TV Movie'), 'first_air_date': '2000-03-01'}, tv_crew(35, 'The Office')]},
            self.CARD)
        self.assertEqual(names, ['Some TV Movie', 'The Office'])


class Actors(unittest.TestCase):
    CARD = {'person_id': 2, 'slug': 'actor-x'}

    def test_zendaya_voice_roles_dropped(self):
        names = run(build.actor, {'cast': [
            cast(1, 'Euphoria', 'Rue', 'tv'), cast(2, 'Dune', 'Chani'), cast(3, 'Spider-Man', 'MJ'),
            cast(4, 'Duck Duck Goose', 'Chi (voice)')]}, self.CARD)
        self.assertNotIn('Duck Duck Goose', names)
        self.assertEqual(sorted(names), ['Dune', 'Euphoria', 'Spider-Man'])

    def test_voice_actor_keeps_voice_roles(self):
        names = run(build.actor, {'cast': [
            cast(1, 'Toon A', 'Bob (voice)'), cast(2, 'Toon B', 'Bob (voice)', 'tv'),
            cast(3, 'Toon C', 'Rex (voice)'), cast(4, 'Live Film', 'Sam')]}, self.CARD)
        self.assertEqual(sorted(names), ['Live Film', 'Toon A', 'Toon B', 'Toon C'])

    def test_cameo_self_archive_uncredited_dropped(self):
        names = run(build.actor, {'cast': [
            cast(1, 'Real Film', 'Lead'), cast(2, 'The Nun', 'Cameo'), cast(3, 'Doc', 'Self'),
            cast(4, 'Doc2', 'Himself'), cast(5, 'Clips', 'Dr. X (archive footage)'),
            cast(6, 'Bit', 'Guard (uncredited)')]}, self.CARD)
        self.assertEqual(names, ['Real Film'])


if __name__ == '__main__':
    unittest.main()
