"""Issue #96: Network + rule-based card accuracy (generator/accuracy_rules.py). Stubbed TMDB, Wikidata and IMDb, no network.
The truth mapping (card -> Wikidata broadcaster items, Wikidata / IMDb rows -> TMDB keys) is the unit under test; the card
rules the labels use are checked against the real spec.json cards.

    python -m unittest tests.test_accuracy_rules
"""
import json, os, re, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import accuracy_rules as ar  # noqa: E402

TODAY = '2026-10-15'
# the full 606-card spec from before the #139 cut: the generator still supports every card type, and these tests cover them
SPEC = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'spec_before_cut.json').read_text(encoding='utf-8'))
CARDS = {c['slug']: c for c in SPEC['catalogs']}


def det(tid, date='1995-06-01', votes=500, rating=8.0, runtime=100, status='Ended', genres=(18,), kw=(), nets=(), movie=True):
    d = {'id': tid, 'vote_count': votes, 'vote_average': rating, 'genres': [{'id': g} for g in genres],
         'networks': [{'id': n} for n in nets]}
    if movie:
        d.update({'title': f'M{tid}', 'release_date': date, 'runtime': runtime,
                  'release_dates': {'results': [{'release_dates': [{'type': 4}]}]}, 'keywords': {'keywords': [{'id': k} for k in kw]}})
    else:
        d.update({'name': f'S{tid}', 'first_air_date': date, 'status': status, 'keywords': {'results': [{'id': k} for k in kw]}})
    return d


class Mapping(unittest.TestCase):
    def test_every_network_card_has_a_wikidata_entry(self):
        nets = [c for c in SPEC['catalogs'] if 'networks' in c.get('folders', [])]
        self.assertEqual(len(nets), 25)
        for c in nets:
            self.assertIn(c['network_id'], ar.NETWORKS, c['slug'])
            for q in ar.NETWORKS[c['network_id']] or []:
                self.assertRegex(q, r'^Q\d+$')
        self.assertEqual(ar.network_qs(CARDS['network-hbo'])[0], 'Q23633')
        self.assertIn('Q9531', ar.network_qs(CARDS['network-bbc-one']))              # BBC shows are filed under the BBC itself
        self.assertEqual(ar.network_qs(CARDS['network-peacock']), [])              # no Wikidata item: no truth, listed

    def test_pick_cards_kinds_and_counts(self):
        got = {k: len(ar.pick_cards(SPEC, k, 99, 's')) for k in ar.KINDS}
        self.assertEqual(got, {'network': 25, 'decade': 7, 'acclaimed': 2, 'hidden': 2, 'standup': 3})
        self.assertEqual(sum(got.values()) - got['network'], 14)
        a = ar.pick_cards(SPEC, 'network', 12, 'audit-96')
        self.assertEqual(len(a), 12)
        self.assertEqual(a, ar.pick_cards(SPEC, 'network', 12, 'audit-96'))         # stable

    def test_broadcaster_rows_become_series_keys_and_drop_unreleased(self):
        q = ar.broadcaster_truth_query(['Q23633'])
        self.assertIn('wdt:P449', q)
        self.assertIn('wdt:P4983', q)
        rows = [{'id': '1399', 'sl': '90', 'd': '2011-04-17T00:00:00Z'}, {'id': '777', 'sl': '5', 'd': '2031-01-01T00:00:00Z'},
                {'id': '55', 'sl': '3'}]
        self.assertEqual(ar.parse_truth(rows, 'series', TODAY), ['series:1399', 'series:55'])

    def test_imdb_weighted_top_and_mapping(self):
        lines = ['tconst\taverageRating\tnumVotes', 'tt1\t9.0\t30000', 'tt2\t9.3\t26000', 'tt3\t9.9\t100', 'tt4\t8.0\t900000']
        self.assertEqual(ar.imdb_top(lines, 'movie'), ['tt2', 'tt1', 'tt4'])        # 100 votes is under the 25,000 floor
        rows = {'tt2': [{'i': 'tt2', 'm': '238'}], 'tt1': [{'i': 'tt1', 't': '5'}], 'tt4': []}
        got = ar.imdb_truth(lines, lambda q: [r for k, v in rows.items() if f'"{k}"' in q for r in v])
        self.assertEqual(got['movie'], ['movie:238'])                                # tt1 is a series: not a movie key
        self.assertEqual(got['series'], ['series:5'])


class Standup(unittest.TestCase):
    def test_no_query_mixes_a_path_with_a_class_union(self):
        for q in (ar.standup_truth_query('movie', ['Q1', 'Q2'], 'P31'), ar.judge_query('standup', 'movie', ['1'], ar.STANDUP_CLASSES)):
            self.assertNotIn('P279', q)                       # WDQS drops rows when a path meets a class union
        self.assertIn('P279*', ar.class_closure_query(ar.STANDUP_CLASSES))

    def test_subclass_counts_and_truth_merges_both_properties(self):
        E = 'http://www.wikidata.org/entity/'
        rows = [{'id': '1', 'g': E + 'Q106633193', 't': E + 'Q11424'}, {'id': '1', 'g': E + 'Q5', 't': E + 'Q11424'},
                {'id': '2', 'g': E + 'Q5'}, {'id': '3', 't': E + 'Q11424'}, {'id': '4', 't': E + 'Q145806'}]
        got = ar.parse_judge_classes(rows, 'movie', ['Q145806', 'Q106633193'])
        # 1: stand-up subclass genre = ok; 2: another genre = wrong; 3: no genre statement at all = unjudged (not wrong);
        # 4: no genre but a stand-up type = ok
        self.assertEqual(got, {'movie:1': (2, True), 'movie:2': (1, False), 'movie:3': (0, False), 'movie:4': (1, True)})

        def sparql(q):
            return [{'id': '9', 'sl': '5'}] if 'wdt:P31 ' in q else [{'id': '8', 'sl': '7'}, {'id': '9', 'sl': '5'}]
        self.assertEqual(ar.standup_truth(sparql, 'movie', ['Q145806'], TODAY, 10), ['movie:8', 'movie:9'])


class Rules(unittest.TestCase):
    def r(self, slug, media):
        return CARDS[slug], ar.rules_of('x', CARDS[slug], media, SPEC)

    def test_decade_window_and_vote_floor(self):
        card, r = self.r('decade-1990s', 'movie')
        good = ar.facts(det(1, date='1995-06-01'), 'movie')
        old = ar.facts(det(2, date='1989-12-31'), 'movie')
        self.assertIsNone(ar.classify_extra(good, 'movie', card, r, TODAY))
        lab, why = ar.classify_extra(old, 'movie', card, r, TODAY)
        self.assertEqual(lab, 'code bug')
        self.assertIn('before the card', why)

    def test_hidden_gem_ceiling_drift_is_not_a_code_bug(self):
        card, r = self.r('acclaimed-hidden-gem-movies', 'movie')
        near = ar.facts(det(3, date='2015-01-01', votes=1520, rating=7.6), 'movie')       # 1.3% over 1500
        far = ar.facts(det(4, date='2015-01-01', votes=5000, rating=7.6), 'movie')
        self.assertEqual(ar.classify_extra(near, 'movie', card, r, TODAY)[0], 'source limit')
        self.assertEqual(ar.classify_extra(far, 'movie', card, r, TODAY)[0], 'code bug')

    def test_standup_movie_floor_is_20_minutes_and_keyword_is_a_tag(self):
        card, r = self.r('standup-top', 'movie')
        self.assertEqual(r['min_runtime'], 20)
        short = ar.facts(det(5, date='2015-01-01', votes=900, runtime=25, kw=(9716,)), 'movie')
        self.assertIsNone(ar.classify_extra(short, 'movie', card, r, TODAY))
        blank = ar.facts(det(8, date='2015-01-01', votes=900, runtime=0, kw=(9716,)), 'movie')       # TMDB blanked it since the build
        self.assertEqual(ar.classify_extra(blank, 'movie', card, r, TODAY)[0], 'source limit')
        nokw = ar.facts(det(6, date='2015-01-01', votes=900, runtime=60), 'movie')
        self.assertEqual(ar.classify_extra(nokw, 'movie', card, r, TODAY)[0], 'source limit')

    def test_second_tmdb_id_of_the_cards_own_network_is_a_code_bug(self):
        """The Pitt (HBO Max, TMDB lists 8304 / 6783 not 3186), Dinosaur Train (PBS Kids 122), Dexter: Original Sin (6631)."""
        for slug, nets in (('network-hbo-max', (8304, 6783)), ('network-pbs', (122,)), ('network-showtime', (6631,))):
            card, r = self.r(slug, 'series')
            f = ar.facts(det(9, movie=False, nets=nets), 'series')
            # the card as it was before #120 (first id only): the miss is a code bug
            old = {**card, 'series': {**card['series'], 'with_networks': str(card['network_id'])}}
            lab, why = ar.classify_missing(f, 'series', old, r, TODAY, 10)
            self.assertEqual(lab, 'code bug', slug)
            self.assertIn('second TMDB id', why)
            # the card now (spec.json carries the second ids): no longer a code bug
            lab, _ = ar.classify_missing(f, 'series', card, r, TODAY, 10)
            self.assertNotEqual(lab, 'code bug', slug)

    def test_parent_organisation_only_truth_is_left_out_and_counted(self):
        rows = [{'id': '1', 'spec': '1'}, {'id': '2', 'spec': '0'}, {'id': '3'}]
        keep, wide = ar.split_wide(rows)
        self.assertEqual([r['id'] for r in keep], ['1', '3'])
        self.assertEqual([r['id'] for r in wide], ['2'])
        self.assertIn('?spec', ar.broadcaster_truth_query(['Q191472', 'Q9531']))
        self.assertNotIn('?spec', ar.broadcaster_truth_query(['Q215616']))     # PBS: the extra column timed Wikidata out (HTTP 500)
        E = 'http://www.wikidata.org/entity/'
        jr = [{'id': '5', 'x': E + 'Q9531'}, {'id': '6', 'x': E + 'Q191472'}, {'id': '7', 'x': E + 'Q9531'}, {'id': '7', 'x': E + 'Q191472'},
              {'id': '8', 'x': E + 'Q99'}, {'id': '9'}]
        self.assertEqual(ar.parse_judge(jr, 'series', ['Q191472', 'Q9531']),
                         {'series:5': (0, False),       # only the parent organisation: unjudged
                          'series:6': (1, True), 'series:7': (2, True),
                          'series:8': (1, False),       # another broadcaster: wrong
                          'series:9': (0, False)})      # no broadcaster statement: unjudged

    def test_network_membership_is_a_tag_not_a_rule(self):
        card, r = self.r('network-hbo', 'series')
        other = ar.facts(det(7, movie=False, nets=(213,)), 'series')
        self.assertEqual(ar.classify_missing(other, 'series', card, r, TODAY, 10)[0], 'source limit')


class Measure(unittest.TestCase):
    def test_network_card_end_to_end(self):
        """Control: 1399 is on the card and Wikidata says HBO (ok); 22 is on the card but Wikidata says another broadcaster
        while TMDB lists HBO (deliberate rule); 99 is Wikidata truth, missing from the card, TMDB lists no HBO (source limit)."""
        card = CARDS['network-hbo']
        dets = {1399: det(1399, movie=False, nets=(49,), votes=900), 22: det(22, movie=False, nets=(49, 4), votes=900),
                99: det(99, movie=False, nets=(4,), votes=900)}

        def tmdb(path, **p):
            return dets[int(path.rsplit('/', 1)[1])]

        def sparql(q):
            if 'ORDER BY DESC(?sl)' in q:
                return [{'id': '1399', 'sl': '9'}, {'id': '99', 'sl': '3'}]
            return [{'id': '1399', 'x': 'http://www.wikidata.org/entity/Q23633'}, {'id': '22', 'x': 'http://www.wikidata.org/entity/Q3'}]
        row, gaps = ar.measure_rules('network', [card], SPEC, lambda s: ['series:1399', 'series:22'], tmdb, sparql, TODAY, 20, 'x')
        self.assertEqual((row['found'], row['truth'], row['recall']), (1, 2, 50.0))
        miss = [g for g in gaps if g['side'] == 'recall']
        self.assertEqual([(g['key'], g['label']) for g in miss], [('series:99', 'source limit')])
        tp = row['truth_precision']
        self.assertEqual((tp['ok'], tp['judged'], tp['gaps']['deliberate rule']), (1, 2, 1))
        self.assertEqual(row['precision'], 100.0)                                     # both pass the card's own rules
        # Wikidata timing out on one card (PBS, HTTP 500 in the 10/16 runs) must not lose the kind: the card is listed, not scored
        def down(q):
            raise RuntimeError('Wikidata HTTP 500')
        row3, _ = ar.measure_rules('network', [card], SPEC, lambda s: ['series:1399'], tmdb, down, TODAY, 5, 'x')
        self.assertEqual((row3['wd_failed'], row3['truth']), (['network-hbo'], 0))
        # seeded control: a network with no Wikidata item is listed and not scored
        row2, _ = ar.measure_rules('network', [CARDS['network-peacock']], SPEC, lambda s: ['series:1'], tmdb, sparql, TODAY, 0, 'x')
        self.assertEqual((row2['truth'], row2['no_truth']), (0, ['network-peacock']))


if __name__ == '__main__':
    unittest.main()
