"""W2-S1: an arrivals title resolve that hit a TMDB error or the request-cap exit is never cached as "no TMDB result".

    python -m unittest discover -s tests

The real build.tmdb() runs against a local 127.0.0.1 fake TMDB (no network, no key); only build.API and build's
time.sleep are patched (no 31 s of 5xx back-off). The durable cache is build._stable (saved as stable.json).
"""
import http.server, json, os, sys, tempfile, threading, unittest, urllib.parse
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-resolve-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
for var in ('SD_HTTP_CACHE', 'SD_CACHE', 'SD_STATE'):
    os.environ[var] = str(Path(_tmp) / var.lower())
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import arrivals as a  # noqa: E402

build = a.build


class FakeTMDB(http.server.BaseHTTPRequestHandler):
    """path -> (status, body) from the class-level ROUTES; every request is logged in HITS."""
    ROUTES, HITS = {}, []

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path.replace('/3', '', 1)
        FakeTMDB.HITS.append(path)
        status, body = FakeTMDB.ROUTES.get(path, (404, {'status_message': 'not found'}))
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def row(title, service='netflix', media='movie', year=2026):
    return {'title': title, 'service': service, 'media': media, 'year': year, 'season': None,
            'date': '2026-09-20', 'short': False}


def hit(rid, title, year='2026'):
    return {'id': rid, 'title': title, 'release_date': f'{year}-05-01', 'popularity': 10, 'vote_count': 5}


class ResolveNeverCachesATMDBError(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeTMDB)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api, cls.sleep = build.API, build.time.sleep
        build.API = f'http://127.0.0.1:{cls.srv.server_address[1]}/3'
        build.time.sleep = lambda s: None

    @classmethod
    def tearDownClass(cls):
        build.API, build.time.sleep = cls.api, cls.sleep
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        FakeTMDB.ROUTES, FakeTMDB.HITS = {}, []
        self.before = dict(build._stable)
        self.calls = build._calls[0]

    def tearDown(self):
        build._stable.clear()
        build._stable.update(self.before)
        build._calls[0] = self.calls

    def new_keys(self):
        return sorted(set(build._stable) - set(self.before))

    # ---- TMDB error ------------------------------------------------------------------------------------------
    def test_http_500_raises_and_caches_nothing(self):
        FakeTMDB.ROUTES = {'/search/movie': (500, {'status_message': 'boom'})}
        with self.assertRaises(RuntimeError):
            a._search('movie', 'Quiet Harbor')
        res = a.resolve(row('Quiet Harbor'), lambda *x: False)
        self.assertEqual(self.new_keys(), [])                         # cache untouched
        self.assertFalse(res[0])                                      # not accepted today
        self.assertNotEqual(res[6], 'CHECK: no TMDB result')          # never the "nothing found" answer
        self.assertEqual(res[6], a.TMDB_ERROR_HOW)

    def test_http_404_raises_and_caches_nothing(self):
        FakeTMDB.ROUTES = {}                                          # every path 404s (no retry on a 4xx)
        with self.assertRaises(RuntimeError):
            a._search('series', 'The Lantern Keeper')
        a.resolve(row('The Lantern Keeper', media=None), lambda *x: False)
        self.assertEqual(self.new_keys(), [])

    def test_retried_next_run_after_an_error(self):
        FakeTMDB.ROUTES = {'/search/movie': (500, {})}
        a.resolve(row('Quiet Harbor'), lambda *x: False)
        FakeTMDB.ROUTES = {'/search/movie': (200, {'results': [hit(501, 'Quiet Harbor')]}),
                           '/movie/501': (200, {'runtime': 95})}
        res = a.resolve(row('Quiet Harbor'), lambda *x: False)        # TMDB is back: resolved and cached now
        self.assertEqual(res[:3], (True, 'movie', 501))
        self.assertIn('arr2:movie|quiet harbor|2026|None|netflix', self.new_keys())

    def test_runtime_lookup_error_caches_nothing(self):
        FakeTMDB.ROUTES = {'/search/movie': (200, {'results': [hit(502, 'Quiet Harbor')]}),
                           '/movie/502': (500, {})}
        res = a.resolve(row('Quiet Harbor'), lambda *x: False)
        self.assertEqual(self.new_keys(), [])
        self.assertEqual(res[6], a.TMDB_ERROR_HOW)

    def test_presence_error_in_tie_break_caches_nothing(self):
        # two exact matches -> the on-service tie-break asks watch/providers, which errors
        two = [hit(503, 'Quiet Harbor'), hit(504, 'Quiet Harbor')]
        FakeTMDB.ROUTES = {'/search/movie': (200, {'results': two}), '/movie/503': (200, {'runtime': 100}),
                           '/movie/504': (200, {'runtime': 100}), '/movie/503/watch/providers': (500, {}),
                           '/movie/504/watch/providers': (500, {})}
        present = a.Presence(None, {'netflix': {'8'}})
        res = a.resolve(row('Quiet Harbor'), present)
        self.assertEqual([k for k in self.new_keys() if not k.startswith('runtime:')], [])
        self.assertEqual(res[6], a.TMDB_ERROR_HOW)
        self.assertFalse(present('netflix', 'movie', 503))            # plain calls keep answering False (unchanged)

    # ---- request cap -----------------------------------------------------------------------------------------
    def test_request_cap_raises_and_caches_nothing(self):
        FakeTMDB.ROUTES = {'/search/movie': (200, {'results': [hit(505, 'Quiet Harbor')]})}
        build._calls[0] = build.REQUEST_CAP                           # the next request is over the cap
        with self.assertRaises(SystemExit):
            a._search('movie', 'Quiet Harbor')
        build._calls[0] = build.REQUEST_CAP
        with self.assertRaises(SystemExit):                           # the runaway guard aborts the step
            a.resolve(row('Quiet Harbor'), lambda *x: False)
        self.assertEqual(self.new_keys(), [])
        self.assertEqual(FakeTMDB.HITS, [])                           # stopped before any request was sent

    # ---- success (unchanged behaviour) -----------------------------------------------------------------------
    def test_success_cached_as_before(self):
        FakeTMDB.ROUTES = {'/search/movie': (200, {'results': [hit(506, 'Quiet Harbor')]}),
                           '/movie/506': (200, {'runtime': 95})}
        res = a.resolve(row('Quiet Harbor'), lambda *x: False)
        self.assertEqual(res, (True, 'movie', 506, 'Quiet Harbor', 2026, 10, 'exact + year'))
        self.assertEqual(self.new_keys(), sorted(['runtime:506', 'arr2:movie|quiet harbor|2026|None|netflix']))
        n = len(FakeTMDB.HITS)
        self.assertEqual(a.resolve(row('Quiet Harbor'), lambda *x: False), res)   # from the cache
        self.assertEqual(len(FakeTMDB.HITS), n)

    def test_real_empty_result_is_cached(self):
        FakeTMDB.ROUTES = {'/search/movie': (200, {'results': []})}
        res = a.resolve(row('No Such Title'), lambda *x: False)
        self.assertEqual(res[6], 'CHECK: no TMDB result')             # a real "nothing found" is a real answer
        self.assertEqual(self.new_keys(), ['arr2:movie|no such title|2026|None|netflix'])


if __name__ == '__main__':
    unittest.main()
