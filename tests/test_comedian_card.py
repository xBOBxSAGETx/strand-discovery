"""Comedian cards (issue #149): specials first, newest first, then the acting credits; and one card per listed comedian
right after the Stand-up card. Offline (TMDB stubbed):

    python -m unittest tests.test_comedian_card

Control: the actor card on the same credits drops every Self special (it drops every Self credit), so a comedian card
built by the actor rules fails the first test.
"""
import json, sys, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'generator'))
import build  # noqa: E402


def credit(i, title, date, character, order=0, genres=(35,), lang='en', media='movie', votes=100, **kw):
    return {'id': i, 'title': title, 'release_date': date, 'character': character, 'order': order, 'genre_ids': list(genres),
            'original_language': lang, 'media_type': media, 'vote_count': votes, 'popularity': 1, **kw}


CAST = [
    credit(1, 'Richard Pryor: Live in Concert', '1979-01-26', 'Himself', votes=300),
    credit(2, 'Richard Pryor: Live on the Sunset Strip', '1982-03-12', 'Self'),
    credit(3, 'Richard Pryor: Here and Now', '1983-10-28', '', votes=50),          # blank role, name in the title
    credit(4, 'Silver Streak', '1976-12-03', 'Grover Muldoon', order=1, genres=(35, 28), votes=900),
    credit(5, 'Stir Crazy', '1980-12-12', 'Harry Monroe', order=1, votes=700),
    credit(6, 'Some Comedy Roast', '1990-01-01', 'Himself', order=6),               # not headlining: not a special
    credit(7, 'Richard Pryor: Archive Reel', '2005-01-01', 'Himself (archive footage)'),
    credit(8, 'Richard Pryor: En Vivo', '1985-01-01', 'Himself', lang='es'),      # not English
    credit(9, 'Richard Pryor: Omit the Logic', '2013-05-31', 'Himself', genres=(99,)),   # a documentary, not Comedy
    credit(10, 'Late Show', '1999-01-01', 'Himself', media='tv', genres=(10767,), name='Late Show', first_air_date='1999-01-01'),
]
CARD = {'slug': 'comic-richard-pryor', 'title': 'Richard Pryor', 'kind': 'comedian', 'person_id': 1, 'media': ['movie', 'series']}


def built(card):
    with mock.patch.object(build, 'tmdb', return_value={'cast': CAST}):
        return [m['name'] for m in build.build_kind(card, {})[0]]


class ComedianCard(unittest.TestCase):
    def test_specials_newest_first_then_acting_best_known_first(self):
        self.assertEqual(built(CARD), ['Richard Pryor: Here and Now', 'Richard Pryor: Live on the Sunset Strip',
                                       'Richard Pryor: Live in Concert', 'Silver Streak', 'Stir Crazy'])

    def test_control_the_actor_card_keeps_no_special(self):
        got = built(dict(CARD, kind='actor'))
        self.assertEqual(got[:2], ['Silver Streak', 'Stir Crazy'])
        self.assertNotIn('Richard Pryor: Live in Concert', got)


class ComedianRow(unittest.TestCase):
    def test_one_card_per_listed_comedian_after_the_standup_card(self):
        spec = json.loads((ROOT / 'generator' / 'spec.json').read_text(encoding='utf-8'))
        listed = json.loads((ROOT / 'generator' / 'comedians.json').read_text(encoding='utf-8'))['comedians']
        row = [c for c in spec['catalogs'] if 'standup' in c['folders']]
        self.assertEqual(row[0]['slug'], 'standup')
        self.assertEqual([(c['kind'], c['person_id'], c['title']) for c in row[1:]],
                         [('comedian', p['id'], p['name']) for p in listed])
        self.assertEqual([c['ranks']['standup'] for c in row[1:]], list(range(1, len(listed) + 1)))
        self.assertGreaterEqual(len(listed), 90)


if __name__ == '__main__':
    unittest.main()
