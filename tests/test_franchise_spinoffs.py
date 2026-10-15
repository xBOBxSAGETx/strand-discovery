"""Issue #69: a Movie Series card holds every theatrical feature of the franchise, spin-offs included. TMDB's collection
omits spin-offs, so they are listed as explicit ids on the card (generator/make_spec.py SERIES -> spec.json). Offline.

    python -m unittest tests.test_franchise_spinoffs
"""
import json, os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

SPEC = Path(__file__).resolve().parent.parent / 'generator' / 'spec.json'
# card title -> TMDB movie ids added by #69 (each checked on Wikidata, P4947, label + date)
ADDED = {
    'X-Men': [76170, 2080, 263115, 293660, 383498, 533535, 340102],
    'Alien': [70981, 126889, 945961],
    'Transformers': [424783, 667538, 698687],
    'Shrek': [417859, 315162],
    'Madagascar': [270946],
    'Men in Black': [479455],
    "Ocean's": [402900],
    'Ghostbusters': [43074],
    'Toy Story': [718789],
    'Halloween': [2082, 24150],
    'Planet of the Apes': [869],
    'Lord of the Rings': [839033],
    'Predator': [1376434],
}
NOT_ON_CARD = {'Rocky': [], 'Creed': []}   # Creed has its own card: its films stay off Rocky


class FranchiseSpinoffs(unittest.TestCase):
    def cards(self):
        return {c['title']: c for c in json.loads(SPEC.read_text(encoding='utf-8'))['catalogs']
                if c['folders'] == ['series'] and c['kind'] == 'franchise'}

    def test_cards_carry_their_added_ids_once(self):
        cards = self.cards()
        for title, ids in ADDED.items():
            listed = [i for m, i in cards[title]['ids'] if m == 'movie']
            self.assertEqual(sorted(listed), sorted(ids), title)
            self.assertEqual(len(listed), len(set(listed)), f'{title}: duplicate id')

    def test_rocky_has_no_creed_ids_and_creed_keeps_its_card(self):
        cards = self.cards()
        self.assertEqual(cards['Rocky']['ids'], [])
        self.assertIn('Creed', cards)

    def test_movie_only_ids_keep_a_movie_card(self):
        cards = self.cards()
        self.assertEqual(cards['X-Men']['media'], ['movie'])
        self.assertEqual(cards['Star Wars']['media'], ['movie', 'series'])

    def test_franchise_merges_in_release_order_and_dedupes(self):
        # stub: collection 1 = films A (1990), C (2010), D (2016-05-18); explicit ids = B (2000), C again, unreleased F, and E (2016-02-09,
        # same year as D: the full date decides, not the year)
        rows = {1: ('A', '1990-01-01'), 2: ('B', '2000-01-01'), 3: ('C', '2010-01-01'), 4: ('F', '2999-01-01'),
                5: ('D', '2016-05-18'), 6: ('E', '2016-02-09')}   # D collection, E added id, same year

        def row(i):
            t, d = rows[i]
            return {'id': i, 'title': t, 'release_date': d, 'vote_count': 9, 'genre_ids': [28]}

        def stub(path, *a, **k):
            if path == '/collection/1':
                return {'name': 'X', 'parts': [row(3), row(1), row(5)]}
            return row(int(path.rsplit('/', 1)[1]))

        real, build.tmdb = build.tmdb, stub
        old_stable = dict(build._stable)
        build._stable.clear()
        try:
            card = {'collection_ids': [1], 'ids': [['movie', 2], ['movie', 3], ['movie', 4], ['movie', 6]]}
            movies, series = build.franchise(card)
        finally:
            build.tmdb = real
            build._stable.clear()
            build._stable.update(old_stable)
        self.assertEqual([m['name'] for m in movies], ['A', 'B', 'C', 'E', 'D'])   # release order, C once, F dropped, Feb 2016 E before May 2016 D
        self.assertEqual(series, [])


if __name__ == '__main__':
    unittest.main()
