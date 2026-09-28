"""Artwork of the four date-driven cards (typographic cards, art_logo.render_type). No network: fonts only.

    python -m unittest discover -s tests
"""
import json, sys, unittest
from pathlib import Path

GEN = Path(__file__).resolve().parents[1] / 'generator'
sys.path.insert(0, str(GEN))
import art_logo  # noqa: E402

CARDS = {c['slug']: c for c in json.loads((GEN / 'art.json').read_text(encoding='utf-8'))['cards']}
SPEC = {c['slug'] for c in json.loads((GEN / 'spec.json').read_text(encoding='utf-8'))['catalogs']}
NEW4 = ('just-hit-digital', 'theme-seasonal-now', 'trending-movies', 'trending-tv')


class TypeArt(unittest.TestCase):
    def test_every_catalog_has_art(self):
        self.assertEqual(SPEC, set(CARDS))

    def test_new_cards_pass_every_check_and_fit_one_line(self):
        for slug in NEW4:
            with self.subTest(slug):
                self.assertEqual(CARDS[slug]['style'], 'type')
                img, log, fails = art_logo.render_type(CARDS[slug])
                self.assertEqual(fails, [])
                self.assertEqual(img.size, (1280, 720))

    def test_no_new_badge_on_trending_or_seasonal(self):
        for slug in ('theme-seasonal-now', 'trending-movies', 'trending-tv'):
            _, log, _ = art_logo.render_type(CARDS[slug])
            self.assertNotEqual(log['mark'], 'NEW')
            self.assertNotEqual((log['badge'] or '').upper(), 'NEW')

    def test_genre_type_cards_keep_the_new_defaults(self):
        g = next(c for c in CARDS.values() if c.get('style') == 'type' and c['slug'].startswith('genre-'))
        _, log, fails = art_logo.render_type(g)
        self.assertEqual((log['mark'], log['badge'], fails), ('NEW', 'NEW', []))

    def test_seeded_controls_fail(self):
        base = CARDS['trending-tv']
        _, _, fails = art_logo.render_type(dict(base, mark_w=0.62))              # wide watermark under the pill
        self.assertTrue(any('overlaps: badge' in f for f in fails), fails)
        _, _, fails = art_logo.render_type(dict(base, badge='x' * 60))           # pill off the card
        self.assertTrue(any('safe area' in f for f in fails), fails)
        _, _, fails = art_logo.render_type(dict(base, field='#e8e8e8'))          # white type on a pale field
        self.assertTrue(any('contrast' in f for f in fails), fails)


if __name__ == '__main__':
    unittest.main()
