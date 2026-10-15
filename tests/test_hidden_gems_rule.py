"""Issue #53: the two Hidden Gem cards must carry the "well rated but lesser known" rule, not just a low vote count.
Offline, reads generator/spec.json only.

    python -m unittest tests.test_hidden_gems_rule
"""
import json, unittest
from pathlib import Path

SPEC = Path(__file__).resolve().parent.parent / 'generator' / 'spec.json'
NO_GENRES = {'80', '99', '10762', '10763', '10764', '10766', '10767'}   # crime, doc, kids, news, reality, soap, talk


def problems(card):
    """What is wrong with a hidden-gem card's rule (empty list = fine)."""
    out, media = [], card['media'][0]
    p = card[media if media == 'movie' else 'series']
    if p.get('vote_average.gte', 0) < 7.5:
        out.append('rating floor under 7.5')
    if not 0 < p.get('vote_count.lte', 10**9) <= 1500 or card['min_votes'] < 100:
        out.append('vote band missing or too loose')
    if media == 'series':
        if not NO_GENRES <= set(p.get('without_genres', '').split('|')):
            out.append('soap/crime/documentary/talk/news/reality/kids not excluded')
        if p.get('first_air_date.gte', '0') < '2000':
            out.append('no first_air_date window (1950s-90s staples)')
    elif card.get('min_age_days', 0) < 730:
        out.append('this and last year releases not excluded')
    return out


class HiddenGemsRule(unittest.TestCase):
    def cards(self):
        return {c['slug']: c for c in json.loads(SPEC.read_text(encoding='utf-8'))['catalogs']
                if c['slug'].startswith('acclaimed-hidden-gem')}

    def test_new_rule_in_spec(self):
        cards = self.cards()
        self.assertEqual(set(cards), {'acclaimed-hidden-gem-movies', 'acclaimed-hidden-gem-tv'})
        for slug, c in cards.items():
            self.assertEqual(problems(c), [], slug)

    def test_old_rule_fails(self):   # the pre-#53 cards, as they were in spec.json
        old_tv = {'media': ['series'], 'min_votes': 80, 'series': {
            'vote_average.gte': 7.3, 'vote_count.lte': 600, 'without_genres': '10767|10763|10764|10762'}}
        old_movie = {'media': ['movie'], 'min_votes': 150, 'min_age_days': 365,
                     'movie': {'vote_average.gte': 7.3, 'vote_count.lte': 1500}}
        self.assertGreaterEqual(len(problems(old_tv)), 3)
        self.assertGreaterEqual(len(problems(old_movie)), 2)


if __name__ == '__main__':
    unittest.main()
