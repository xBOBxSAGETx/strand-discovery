"""Issue #97: Streaming New accuracy (generator/accuracy_new.py): the label of the dated-precision errors, the claim-only
window truth and the per-card measure. Stubbed TMDB (no network).

    python -m unittest tests.test_accuracy_new
"""
import gzip, json, os, sys, tempfile, unittest
from pathlib import Path

os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import accuracy_new as an  # noqa: E402

TODAY = '2026-10-15'
SPEC = {'defaults': {'depth': {'new': 500, 'popular': 1000}, 'without_keywords': '111'}, 'catalogs': []}
NF = {'slug': 'streaming-netflix-new', 'kind': 'discover', 'sort': 'new', 'media': ['movie', 'series'],
      'movie': {'watch_region': 'US', 'with_watch_providers': '8', 'with_watch_monetization_types': 'flatrate'},
      'series': {'watch_region': 'US', 'with_watch_providers': '8', 'with_watch_monetization_types': 'flatrate'}}
UNION_NF = dict(NF, slug='streaming-netflix', kind='union', sort='popular', provider='netflix', min_votes=10, cap=2000, folders=['streaming'],
                parts=[{'slug': 'streaming-netflix', 'sort': 'popular', 'min_votes': 10},
                       {'slug': 'streaming-netflix-new', 'sort': 'new', 'min_votes': 5, 'provider': 'netflix'},
                       {'slug': 'streaming-netflix-top', 'sort': 'top', 'min_votes': {'movie': 300, 'series': 150}, 'min_age_days': 180}])


def det(tid, date='2026-10-01', on=True, kw=(), title=None):
    wp = {'US': {'flatrate': [{'provider_id': 8}] if on else [{'provider_id': 337}]}}
    d = {'id': tid, 'title': title or f't{tid}', 'name': title or f't{tid}', 'release_date': date, 'first_air_date': date,
         'genres': [], 'watch/providers': {'results': wp}, 'keywords': {'keywords': [{'id': k} for k in kw], 'results': [{'id': k} for k in kw]},
         'networks': []}
    return d


def fake_tmdb(details, window):
    def tmdb(path, **p):
        if path.startswith('/discover'):
            ids = window.get('movie' if path.endswith('movie') else 'series', [])
            return {'results': [{'id': i, 'title': f't{i}', 'name': f't{i}'} for i in ids], 'total_pages': 1}
        tid = int(path.rsplit('/', 1)[1])
        if tid not in details:
            raise RuntimeError('no such')
        return details[tid]
    return tmdb


class DatedErrorLabel(unittest.TestCase):
    CAND = {'date': '2026-09-01', 'precision': 'day', 'sources': ['x']}

    def test_service_dropped_it_is_rule_f1(self):
        # the 14 errors of run 38855194911: a Sept 1 day-dated item the service has since dropped
        lab, why = an.label_dated_error(['2026-09-01', '2026-09-20'], self.CAND, '2026-10-14', TODAY)
        self.assertEqual(lab, 'deliberate rule')
        self.assertIn('F1', why)

    def test_crawl_still_saw_it_is_a_source_limit(self):
        self.assertEqual(an.label_dated_error(['2026-09-01', '2026-10-14'], self.CAND, '2026-10-14', TODAY)[0], 'source limit')

    def test_crawl_never_saw_it_is_a_source_limit(self):
        self.assertEqual(an.label_dated_error(None, self.CAND, '2026-10-14', TODAY)[0], 'source limit')


class WindowTruth(unittest.TestCase):
    def test_window_is_the_providers_last_45_days(self):
        p = an.window_params(NF, 'movie', TODAY)
        self.assertEqual((p['primary_release_date.gte'], p['primary_release_date.lte']), ('2026-08-31', TODAY))
        self.assertEqual(p['with_watch_providers'], '8')
        self.assertEqual(an.window_params(NF, 'series', TODAY)['first_air_date.gte'], '2026-08-31')


class Measure(unittest.TestCase):
    def run_card(self, order, window, details, mode='release_date', items=None, cands=None):
        return an.measure(NF, order, SPEC, fake_tmdb(details, window), TODAY, mode, items or {}, cands or {}, 'seed', n_sample=20, truth_k=20)

    def test_clean_card(self):
        row, gaps = self.run_card(['movie:1', 'series:2'], {'movie': [1], 'series': [2]}, {1: det(1), 2: det(2)})
        self.assertEqual((row['recall'], row['precision'], gaps), (100.0, 100.0, []))
        self.assertTrue(row['circular'])

    def test_missing_truth_title_labels(self):
        details = {1: det(1), 3: det(3, kw=[111]), 4: det(4, on=False), 5: det(5)}
        row, gaps = self.run_card(['movie:1'], {'movie': [1, 3, 4, 5]}, details, items={'m:5': ['2026-08-01', '2026-10-14']}, mode='first_seen')
        got = {g['key']: g['label'] for g in gaps if g['side'] == 'recall'}
        self.assertEqual(got, {'movie:3': 'deliberate rule', 'movie:4': 'source limit', 'movie:5': 'deliberate rule'})

    def test_card_title_breaking_its_own_rule_is_a_code_bug(self):
        # CONTROL: an unreleased title on the card is a code bug, found by the precision sample
        row, gaps = self.run_card(['movie:1', 'movie:9'], {'movie': [1]}, {1: det(1), 9: det(9, date='2027-01-01')})
        self.assertEqual([(g['key'], g['label']) for g in gaps if g['side'] == 'precision'], [('movie:9', 'code bug')])
        self.assertEqual(row['code_bugs'], 1)

    def test_dated_item_dropped_by_the_service_is_a_deliberate_rule_not_a_bug(self):
        cands = {'movie:7': {'date': '2026-09-01', 'precision': 'day', 'sources': ['x']}}
        row, gaps = self.run_card(['movie:7'], {'movie': []}, {7: det(7, on=False)}, mode='arrivals', cands=cands, items={'m:7': ['2026-09-01', '2026-09-30']})
        self.assertEqual([(g['side'], g['label']) for g in gaps], [('precision', 'deliberate rule')])


class UnionCard(unittest.TestCase):
    """#143: the New measures run on the service's one union card (svc = its provider), none is skipped."""
    def setUp(self):
        import accuracy_check as ck
        self.ck = ck
        self.pages = Path(tempfile.mkdtemp(prefix='sd-acc-'))
        d = self.pages / 'catalog' / 'movie'
        d.mkdir(parents=True)
        self.keys = [f'movie:{i}' for i in range(1, 11)]
        (d / 'sd-streaming-netflix.json').write_text(json.dumps({'metas': [{'type': 'movie', 'id': f'tmdb:{i}'} for i in range(1, 11)]}), encoding='utf-8')
        self.summary = {'new_card_modes': {'streaming-netflix': 'arrivals'}}

    def test_measure_runs_on_the_union_card_with_its_provider(self):
        row, gaps = an.measure(UNION_NF, ['movie:1', 'series:2'], SPEC, fake_tmdb({1: det(1), 2: det(2)}, {'movie': [1], 'series': [2]}),
                               TODAY, 'release_date', {}, {}, 'seed', n_sample=20, truth_k=20)
        self.assertEqual((an.svc_of(UNION_NF), row['kind'], row['recall'], row['precision'], gaps), ('netflix', 'new-netflix', 100.0, 100.0, []))

    def test_measure_does_not_mistake_the_union_cap_for_the_new_cap(self):
        order = [f'movie:{i}' for i in range(1, 601)]
        row, _ = an.measure(UNION_NF, order, SPEC, fake_tmdb({1: det(1)}, {'movie': [1]}), TODAY, 'release_date', {}, {}, 'seed', n_sample=0, truth_k=20)
        self.assertEqual(row['at_cap'], [])

    def test_recall_is_measured_on_the_union_card(self):
        rows = [{'service': 'netflix', 'date': '2026-10-10', 'media': 'movie', 'tmdb_id': str(i)} for i in range(1, 11)]
        out = self.ck.recall(self.pages, self.summary, rows, TODAY, {})
        self.assertEqual([(r['service'], r['status'], r['found'], r['recall']) for r in out], [('netflix', 'measured', 10, 100.0)])

    def test_precision_samples_the_union_card(self):
        state = {'providers': {'netflix': {'signals': {}, 'items': {}, 'last_run': TODAY}}}
        sd = self.pages / 'state'
        sd.mkdir()
        (sd / 'first_seen.json.gz').write_bytes(gzip.compress(json.dumps(state).encode()))
        cands = {'date': TODAY, 'providers': {'netflix': [{'key': k, 'date': '2026-10-10', 'precision': 'day', 'sources': ['vt'], 'status': 'x'}
                                                          for k in self.keys[:3]]}}
        saved = self.ck.tmdb
        self.ck.tmdb = lambda path, **p: {'results': {'US': {'flatrate': [{'provider_id': 8}]}}}
        try:
            table, rows = self.ck.precision(self.pages, sd, {'catalogs': [UNION_NF]}, self.summary, cands, 5, 'seed', {}, set())
        finally:
            self.ck.tmdb = saved
        self.assertEqual([(t['provider'], t['dated_items'], t['sample'], t['on_service']) for t in table], [('netflix', 3, 3, 3)])
        self.assertEqual({r['class'] for r in rows}, {'ok'})


class NewRowsNeverAlarmOnNumbers(unittest.TestCase):
    """Critic of PR #109: New rows are circular and use a weekly-reseeded sample, so their numbers are shown, never alarmed."""
    BASE = json.loads((Path(__file__).resolve().parents[1] / 'generator' / 'accuracy_baseline.json').read_text(encoding='utf-8'))['frame']

    @staticmethod
    def row(kind, recall, precision, bugs=0):
        return {'kind': kind, 'recall': recall, 'precision': precision, 'code_bugs': bugs}

    def test_committed_baseline_holds_no_numbers_for_new_rows(self):
        news = {k: v for k, v in self.BASE.items() if k.startswith('new-')}
        self.assertEqual(len(news), 21)
        self.assertTrue(all(set(v) == {'code_bugs'} for v in news.values()))

    def test_lower_recall_and_precision_than_last_week_raise_nothing(self):
        import accuracy_check as ac
        rows = [self.row(k, 10.0, 50.0) for k in self.BASE if k.startswith('new-')]
        self.assertEqual(ac.frame_problems(rows, self.BASE), [])

    def test_control_a_numeric_baseline_would_alarm_and_a_code_bug_still_does(self):
        import accuracy_check as ac
        self.assertEqual(len(ac.frame_problems([self.row('new-x', 80.0, 90.0)], {'new-x': {'recall': 96.0, 'precision': 100.0, 'code_bugs': 0}})), 2)
        self.assertEqual(len(ac.frame_problems([self.row('new-netflix', 96.0, 100.0, 1)], self.BASE)), 1)


if __name__ == '__main__':
    unittest.main()
