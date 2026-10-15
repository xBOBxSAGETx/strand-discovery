"""A watcher-started partial rebuild that includes New cards reads the feeds again first (#79):

    python -m unittest tests.test_partial_arrivals

1. build.yml: the Arrival signals step runs for a full build AND for a partial one that names a -new card, and the partial
   sets SD_ARRIVALS_LISTING_MAX_AGE=0 (a full build leaves it empty = the 20 h default).
2. arrivals.py: with that variable at 0 a feed listing read 1 hour ago is revalidated (a conditional GET) instead of reused;
   without it the same record is reused with no request at all (the old behaviour).
"""
import json, os, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent('''
    import json, os, sys, time, urllib.error, urllib.request
    sys.path.insert(0, os.environ['GEN'])
    import arrivals
    url = 'https://example.test/feed/'
    p = arrivals._record_path(url, 'feed-links')
    arrivals._write(p, {'url': url, 'parser': 'feed-links', 'v': arrivals.PARSER_VERSION, 'status': 200, 'etag': '"e1"',
                        'last_modified': None, 'fetched': time.time() - 3600, 'data': [{'title': 'old'}]})
    seen = []
    def fake(req, timeout=0):
        seen.append(req.get_header('If-none-match'))
        raise urllib.error.HTTPError(req.full_url, 304, 'Not Modified', {}, None)
    urllib.request.urlopen = fake
    st, data, cached = arrivals.get_parsed(url, 'feed-links', lambda t: [])
    print(json.dumps({'listing_max_age': arrivals.LISTING_MAX_AGE, 'requests': len(seen), 'etag_sent': seen, 'data': data}))
''')


def probe(listing_max_age):
    d = Path(tempfile.mkdtemp(prefix='sd-arr-'))
    env = {k: v for k, v in os.environ.items() if k != 'SD_ARRIVALS_LISTING_MAX_AGE'}
    env.update(GEN=str(ROOT / 'generator'), SD_HTTP_CACHE=str(d / 'http'), SD_CACHE=str(d / 'cache'),
               TMDB_API_KEY='unused-in-tests', PYTHONIOENCODING='utf-8')
    if listing_max_age is not None:
        env['SD_ARRIVALS_LISTING_MAX_AGE'] = listing_max_age
    r = subprocess.run([sys.executable, '-c', PROBE], env=env, capture_output=True, text=True, cwd=d)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


class PartialArrivals(unittest.TestCase):
    def test_workflow_runs_arrivals_for_a_partial_with_new_cards(self):
        wf = yaml.safe_load((ROOT / '.github' / 'workflows' / 'build.yml').read_text(encoding='utf-8'))
        step = next(s for s in wf['jobs']['build']['steps'] if s.get('id') == 'arrivals')
        self.assertEqual(step['if'], "env.SD_CARDS == '' || contains(env.SD_CARDS, '-new')")
        self.assertEqual(step['env']['SD_ARRIVALS_LISTING_MAX_AGE'], "${{ env.SD_CARDS != '' && '0' || '' }}")

    def test_listing_max_age_zero_revalidates_a_fresh_listing(self):
        r = probe('0')
        self.assertEqual((r['listing_max_age'], r['requests'], r['etag_sent'], r['data']), (0.0, 1, ['"e1"'], [{'title': 'old'}]))

    def test_default_reuses_a_fresh_listing_without_a_request(self):
        for unset in (None, ''):
            r = probe(unset)
            self.assertEqual((r['listing_max_age'], r['requests']), (20.0, 0))


if __name__ == '__main__':
    unittest.main()
