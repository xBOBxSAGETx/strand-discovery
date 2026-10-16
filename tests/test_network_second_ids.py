"""Issue #120: a network card must list every TMDB id its network airs under, not only the first.
HBO Max = 3186 + 8304 + 6783 ("Max"), PBS = 14 + 122 (PBS Kids), Showtime = 67 + 6631. Offline: reads generator/spec.json
and generator/make_spec.py only (the replay of the old break: spec.json carried just the first id, so a show listed only
under the second id, e.g. The Pitt on 8304/6783, never reached the card).

    python -m unittest tests.test_network_second_ids
"""
import json, sys, unittest
from pathlib import Path

GEN = Path(__file__).resolve().parent.parent / 'generator'
SECOND = {'network-hbo-max': ('3186', ['8304', '6783']), 'network-pbs': ('14', ['122']), 'network-showtime': ('67', ['6631'])}


class NetworkSecondIds(unittest.TestCase):
    def cards(self):
        return {c['slug']: c for c in json.loads((GEN / 'spec.json').read_text(encoding='utf-8'))['catalogs']
                if c['slug'] in SECOND}

    def test_cards_carry_second_ids(self):
        cards = self.cards()
        self.assertEqual(set(cards), set(SECOND))
        for slug, (first, others) in SECOND.items():
            ids = cards[slug]['series']['with_networks'].split('|')
            self.assertEqual(ids[0], first, slug)
            for o in others:
                self.assertIn(o, ids, f'{slug} misses its network id {o}')
            self.assertEqual(cards[slug]['network_id'], int(first), slug)    # art / audit key off the first id only

    def test_spec_matches_make_spec(self):
        sys.path.insert(0, str(GEN))
        import make_spec
        want = {name: str(nid) for name, nid in make_spec.NETWORKS}
        for slug, c in self.cards().items():
            self.assertEqual(c['series']['with_networks'], want[c['title']], slug)


if __name__ == '__main__':
    unittest.main()
