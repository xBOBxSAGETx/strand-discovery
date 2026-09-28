"""Fake-server test of arrivals.get_parsed's 429/503 circuit breaker. Local only (127.0.0.1), no real sites.

  python tests/circuit_breaker_test.py [<strand-discovery checkout> [<work dir>]]
  (defaults: this repo and a fresh temp dir; run by .github/workflows/tests.yml on every PR)

Host A always answers 429 with Retry-After: 2; host B answers 200. Expected:
  - A url1: request, 429 -> waits Retry-After (~2 s) -> request, 429 -> host A stopped, BLOCKED, 2 requests to A
  - A url2 (same run): BLOCKED immediately, 0 further requests to A
  - B: 200 and parsed data (other hosts keep going)
  - one stop reason recorded for A (-> ONE warning per host in arrivals_summary.json)
  - a stopped host with stored parsed data serves that data (stale fallback), like a network error
"""
import http.server, json, os, sys, tempfile, threading, time
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]).resolve()
WORK = Path(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix='sd-breaker-')).resolve()
WORK.mkdir(parents=True, exist_ok=True)
os.environ['SD_HTTP_CACHE'] = str(WORK / 'http')
os.environ.setdefault('TMDB_API_KEY', 'unused')
os.environ['SD_CACHE'] = str(WORK / 'cache')
sys.path.insert(0, str(REPO / 'generator'))
import arrivals as a  # noqa: E402

hits = {'A': 0, 'B': 0}


def handler(name, code):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits[name] += 1
            self.send_response(code)
            if code == 429:
                self.send_header('Retry-After', '2')
            self.end_headers()
            if code == 200:
                self.wfile.write(b'hello')

        def log_message(self, *args):
            pass
    return H


servers = []
for name, code in (('A', 429), ('B', 200)):
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler(name, code))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers.append(srv)
A = f'http://127.0.0.1:{servers[0].server_address[1]}'
B = f'http://localhost:{servers[1].server_address[1]}'
a.DEFAULT_DELAY = 0.1
problems = []


def check(ok, msg):
    print(('ok   ' if ok else 'FAIL ') + msg)
    if not ok:
        problems.append(msg)


parse = lambda t: {'len': len(t)}
t0 = time.time()
s1, d1, _ = a.get_parsed(A + '/one', 't', parse)
dt1 = time.time() - t0
check(s1 == a.BLOCKED and d1 is None, f'A url1: BLOCKED after a second 429 (status {s1})')
check(hits['A'] == 2, f'A url1: exactly 2 requests (got {hits["A"]})')
check(1.8 <= dt1 < 10, f'A url1: waited Retry-After (~2 s) before the retry ({dt1:.1f} s)')
s2, d2, _ = a.get_parsed(A + '/two', 't', parse)
check(s2 == a.BLOCKED and hits['A'] == 2, f'A url2: BLOCKED without another request (requests to A: {hits["A"]})')
s3, d3, _ = a.get_parsed(B + '/ok', 't', parse)
check(s3 == 200 and d3 == {'len': 5} and hits['B'] == 1, f'B keeps going: {s3} {d3}')
stopped = dict(a._stopped)
check(list(stopped) == [A.split('//')[1]], f'one stop reason, for A only: {stopped}')
stats = {'vt': {'posts': 0, 'errors': [f'{A}/one: HTTP {a.BLOCKED}', f'{A}/two: HTTP {a.BLOCKED}']}}
warnings = [f'{s}: {e}' for s, st in stats.items() for e in st.get('errors', []) if f'HTTP {a.BLOCKED}' not in e]
warnings += [f'{h}: {why}' for h, why in a._stopped.items()]
check(len(warnings) == 1 and '429 twice' in warnings[0], f'ONE warning for the stopped host: {warnings}')
# a stopped host with stored parsed data: serve the stored data
rec_p = a._record_path(A + '/three', 't')
a._write(rec_p, {'url': A + '/three', 'parser': 't', 'v': a.PARSER_VERSION, 'status': 200, 'etag': None,
                 'last_modified': None, 'fetched': 0, 'data': {'len': 9}})
s4, d4, c4 = a.get_parsed(A + '/three', 't', parse)
check(s4 == 200 and d4 == {'len': 9} and c4 and hits['A'] == 2, f'stopped host + stored data -> stored data ({s4}, {d4})')
print('RESULT:', 'PASS' if not problems else f'FAIL ({len(problems)})')
sys.exit(1 if problems else 0)
