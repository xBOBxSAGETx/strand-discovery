"""Movie Series title logos (issue #15): selection (art_plan) and rendering asserts (art.render). No network, no TMDB:
TMDB answers are small fixtures and images are synthetic PIL images.

    python -m unittest discover -s tests

Seeded negative controls: an oversized logo, an offset logo and an unreadable logo must each FAIL render()'s asserts.
"""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
for var in ('SD_HTTP_CACHE', 'SD_CACHE', 'SD_STATE'):
    os.environ[var] = str(Path(_tmp) / var.lower())
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
from PIL import Image, ImageDraw  # noqa: E402
import art, art_plan  # noqa: E402


def logo(path, lang='en', width=800, height=300, avg=5.0, count=2):
    return {'file_path': path, 'iso_639_1': lang, 'width': width, 'height': height,
            'vote_average': avg, 'vote_count': count}


COLLECTIONS = [{'id': 1, 'parts': [{'id': 11, 'title': 'The Terminator', 'release_date': '1984-10-26'},
                                   {'id': 12, 'title': 'Terminator 2: Judgment Day', 'release_date': '1991-07-03'}]}]
IMAGES = {
    ('collection', 1): [],
    ('movie', 11): [logo('/svg.svg', avg=9.9), logo('/small.png', width=400, avg=9.0), logo('/fr.png', lang='fr', avg=8),
                    logo('/null.png', lang=None, avg=7), logo('/box.png', avg=6), logo('/en-low.png', avg=5.2),
                    logo('/en-best.png', avg=5.5, count=3)],
    ('movie', 99): [logo('/pinned.png', avg=1)],
}
SHAPES = {'/box.png': (0.95, 2.0), '/strip.png': (0.2, 17.6)}


def images_of(kind, tid):
    return IMAGES.get((kind, tid), [])


def shape_of(path):
    return SHAPES.get(path, (0.3, 2.7))


class Selection(unittest.TestCase):
    def test_rank_filters_language_width_and_svg(self):
        ranked = [l['file_path'] for l in art_plan.rank_title_logos(IMAGES[('movie', 11)])]
        self.assertEqual(ranked, ['/box.png', '/en-best.png', '/en-low.png', '/null.png'])  # box is dropped later

    def test_sources_only_a_film_titled_like_the_card(self):
        self.assertEqual(art_plan.title_logo_sources('Terminator', COLLECTIONS), [('collection', 1), ('movie', 11)])
        bond = [{'id': 645, 'parts': [{'id': 646, 'title': 'Dr. No', 'release_date': '1962-10-07'}]}]
        self.assertEqual(art_plan.title_logo_sources('007', bond), [('collection', 645)])
        ff = [{'id': 9, 'parts': [{'id': 9, 'title': 'The Fast and the Furious', 'release_date': '2001'}]}]
        self.assertEqual(art_plan.title_logo_sources('Fast & Furious', ff), [('collection', 9)])

    def test_pick_highest_voted_english_not_boxy(self):
        got = art_plan.pick_title_logo('Terminator', art_plan.title_logo_sources('Terminator', COLLECTIONS),
                                       images_of, shape_of)
        self.assertEqual((got['path'], got['lang'], got['source'], got['pinned']), ('/en-best.png', 'en', 'movie 11', False))

    def test_untagged_when_no_english(self):
        IMAGES[('movie', 50)] = [logo('/n.png', lang=None), logo('/de.png', lang='de', avg=10)]
        got = art_plan.pick_title_logo('X', [('movie', 50)], images_of, shape_of)
        self.assertEqual((got['path'], got['lang']), ('/n.png', None))

    def test_fallbacks(self):
        self.assertIn('keyword franchise', art_plan.pick_title_logo('MCU', [], images_of, shape_of)['fallback'])
        self.assertIn('no film titled', art_plan.pick_title_logo('007', [('collection', 1)], images_of, shape_of)['fallback'])
        IMAGES[('movie', 60)] = [logo('/box.png'), logo('/strip.png', width=1920, height=109)]
        self.assertIn('not boxy', art_plan.pick_title_logo('Y', [('movie', 60)], images_of, shape_of)['fallback'])
        self.assertEqual(art_plan.pick_title_logo('Y', [('movie', 11)], images_of, shape_of, pin=False),
                         {'fallback': 'override: typeset title'})

    def test_pin_used_even_if_low_voted_and_must_still_be_listed(self):
        got = art_plan.pick_title_logo('Z', [], images_of, shape_of, pin={'kind': 'movie', 'id': 99, 'path': '/pinned.png'})
        self.assertEqual((got['path'], got['pinned']), ('/pinned.png', True))
        gone = art_plan.pick_title_logo('Z', [], images_of, shape_of, pin={'kind': 'movie', 'id': 99, 'path': '/gone.png'})
        self.assertTrue(gone['fallback'].startswith('PIN MISSING'))


def wordmark_logo(rgb=(255, 255, 255), size=(900, 300)):
    """Transparent PNG-like logo: three solid bars (a stand-in for letters) with transparent margins."""
    img = Image.new('RGBA', size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i in range(3):
        d.rectangle((40 + i * 290, 40, 40 + i * 290 + 220, size[1] - 40), fill=rgb + (255,))
    return img


class Render(unittest.TestCase):
    def setUp(self):
        self.saved = (art.fetch_image, art.background_path, art.OUT, art.TITLE_LOGO_BOX, art.title_logo_xy, art.gradient,
                      art.TITLE_LOGO_MAX_AREA)
        art.OUT = Path(_tmp) / 'cards'
        art.background_path = lambda bg: '/bg.jpg'
        self.logo_img = wordmark_logo()
        self.bg_img = Image.new('RGBA', (1920, 1080), (120, 130, 140, 255))
        art.fetch_image = lambda path, size='original': (self.bg_img if path == '/bg.jpg' else self.logo_img).copy()

    def tearDown(self):
        (art.fetch_image, art.background_path, art.OUT, art.TITLE_LOGO_BOX, art.title_logo_xy, art.gradient,
         art.TITLE_LOGO_MAX_AREA) = self.saved

    def card(self, **tl):
        return {'slug': 'series-test', 'shape': 'wide', 'eyebrow': 'Movie Series', 'title': 'Test',
                'bg': {'kind': 'collection', 'id': 1},
                'title_logo': tl or {'path': '/logo.png', 'lang': 'en', 'source': 'movie 11'}}

    def test_logo_card_passes_all_asserts(self):
        _, log, fails = art.render(self.card())
        self.assertEqual(fails, [])
        self.assertEqual(log['safe_area'], 'ok')
        self.assertIn('movie 11 /logo.png (en)', log['title_logo'])
        self.assertGreaterEqual(log['title_logo_legible'], art.MIN_LEGIBLE_SHARE)
        self.assertNotIn('title_px', log)                        # no typeset title drawn
        self.assertEqual(log['hero_band'], 'ok')
        self.assertNotIn('title_logo_capped', log)

    def test_dark_logo_is_recoloured_and_legible(self):
        self.logo_img = wordmark_logo((20, 20, 20))
        _, log, fails = art.render(self.card())
        self.assertEqual(fails, [])
        self.assertEqual(log['title_logo_recoloured'], '#ffffff')

    def two_tone(self, face, edge, edge_px):
        img = wordmark_logo(face)
        d = ImageDraw.Draw(img)
        for i in range(3):                           # a thick outline around each bar
            x0 = 40 + i * 290
            d.rectangle((x0, 40, x0 + 220, 260), outline=edge + (255,), width=edge_px)
        return img

    def test_multicolour_logo_keeps_its_colours(self):
        self.logo_img = self.two_tone((255, 220, 0), (10, 20, 90), 20)    # yellow face, dark-blue outline (Toy Story)
        _, log, fails = art.render(self.card())
        self.assertEqual(fails, [])
        self.assertEqual(log['title_logo_kind'], 'multi-colour')
        self.assertNotIn('title_logo_recoloured', log)

    def test_control_mostly_dark_multicolour_logo_fails(self):
        self.logo_img = self.two_tone((255, 220, 0), (10, 20, 90), 90)    # outline swallows most of the face
        _, log, fails = art.render(self.card())
        self.assertNotIn('title_logo_recoloured', log)
        self.assertTrue(any(f.startswith('title logo contrast') and 'multi-colour' in f for f in fails), (fails, log))

    def test_fallback_keeps_the_typeset_title(self):
        _, log, fails = art.render(self.card(fallback='no film titled like the card'))
        self.assertEqual(fails, [])
        self.assertEqual(log['title_lines'], 1)
        self.assertEqual(log['title_logo'], 'fallback: no film titled like the card')

    # ---- seeded negative controls: each MUST fail ------------------------------------------------------------
    def test_control_oversized_logo_fails_safe_area(self):
        art.TITLE_LOGO_BOX = {'wide': (1.10, 1.10), 'poster': (1.1, 1.1)}
        art.TITLE_LOGO_MAX_AREA = 10.0                                               # the cap would shrink it back
        _, log, fails = art.render(self.card())
        self.assertEqual(log['safe_area'], 'FAIL')
        self.assertTrue(any('outside 4% safe area' in f and 'title logo' in f for f in fails), fails)

    def test_control_offset_logo_fails_safe_area(self):
        art.title_logo_xy = lambda size, lw, lh, margin: (10, size[1] - lh - 10)     # inside the canvas, not the 4%
        _, log, fails = art.render(self.card())
        self.assertEqual(log['safe_area'], 'FAIL')
        self.assertTrue(any('title logo 10,' in f for f in fails), fails)

    def test_control_unreadable_logo_fails_contrast(self):
        # black/white horizontal stripes under the logo, no darkening shade: any single colour vanishes on half of
        # them, and so does the original white logo (a mean-vs-mean check would call this grey and pass it)
        art.gradient = lambda size, shape: Image.new('L', size, 0)
        self.bg_img = Image.new('RGBA', (1920, 1080), (0, 0, 0, 255))
        d = ImageDraw.Draw(self.bg_img)
        for top in range(0, 1080, 120):
            d.rectangle((0, top, 1919, top + 59), fill=(255, 255, 255, 255))
        _, log, fails = art.render(self.card())
        self.assertTrue(any(f.startswith('title logo contrast') for f in fails), (fails, log))
        self.assertLess(log['title_logo_legible'], art.MIN_LEGIBLE_SHARE)

    def solid(self, w, h):
        img = Image.new('RGBA', (w + 20, h + 20), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle((10, 10, 9 + w, 9 + h), fill=(255, 255, 255, 255))
        return img

    def test_area_cap_shrinks_only_logos_that_fill_both_directions(self):
        cases = {'alien 788x299': ((788, 299), True), 'toy story 324x244': ((1413, 1064), False),
                 'dark knight 1278x361': ((1278, 361), False), 'wide strip 4:1': ((800, 200), False)}
        for name, ((w, h), capped) in cases.items():
            self.logo_img = self.solid(w, h)
            _, log, fails = art.render(self.card())
            self.assertEqual(fails, [], name)
            self.assertEqual(log.get('title_logo_capped', False), capped, (name, log['title_logo_px']))
            lw, lh = map(int, log['title_logo_px'].split('x'))
            self.assertLessEqual(lw * lh, art.TITLE_LOGO_MAX_AREA * 1280 * 720 + 1300, name)

    def test_samples_toy_story_and_dark_knight_keep_their_fitted_size(self):
        for (w, h), px in (((1413, 1064), '325x245'), ((1278, 361), '640x181')):
            self.logo_img = self.solid(w, h)
            self.assertEqual(art.render(self.card())[1]['title_logo_px'], px)

    def test_control_logo_below_hero_band_fails(self):
        art.title_logo_xy = lambda size, lw, lh, margin: (margin, 600 - lh)          # bottom y600 > 543, inside 4%
        _, log, fails = art.render(self.card())
        self.assertEqual(log['safe_area'], 'ok')
        self.assertEqual(log['hero_band'], 'FAIL')
        self.assertTrue(any(f.startswith('outside hero band') and 'title logo' in f for f in fails), fails)

    def test_control_thin_strip_fails_min_size(self):
        self.logo_img = wordmark_logo(size=(1920, 109))                              # the ALIEN strip: 640x36
        _, log, fails = art.render(self.card())
        self.assertTrue(any(f.startswith('title logo too small') for f in fails), fails)

    def test_legible_share_counts_pixels_not_means(self):
        white = wordmark_logo()
        self.assertEqual(art.legible_share(white, Image.new('RGB', white.size, (0, 0, 0))), 1.0)
        self.assertEqual(art.legible_share(white, Image.new('RGB', white.size, (255, 255, 255))), 0.0)


if __name__ == '__main__':
    unittest.main()
