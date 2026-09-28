"""No-flood test for first_seen's query re-baseline (channel provider ids added). Local, no TMDB, no network.

  python tests/no_flood_test.py [<strand-discovery checkout> [<work dir>]]
  (defaults: this repo and a fresh temp dir; run by .github/workflows/tests.yml on every PR)

Two providers, three simulated daily runs of generator/first_seen.py (enumeration faked, the rest real):
  day 1  X ids A        {1,2,3}      Y ids Y  {10,11}        -> baseline for both
  day 2  X ids A        {1,2,3,4}    Y ids Y  {10,11,12}     -> X +1 (4), Y +1 (12): genuine arrivals
  day 3  X ids A|B      {1..4, 5..40} (36 newly visible)  Y ids Y {10..13}
         -> X: 0 adds, 'rebaselined' = [X], its day-2 arrival (4) keeps its date; Y: +1 (13), history kept
Also: a legacy state without the `ids` field counts as changed once (no flood on the first run with the field).
Exit 1 on any failed assertion; prints every check.
"""
import json, os, subprocess, sys, tempfile, textwrap
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]).resolve()
WORK = Path(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix='sd-no-flood-')).resolve()
WORK.mkdir(parents=True, exist_ok=True)
problems = []


def check(ok, msg):
    print(('ok   ' if ok else 'FAIL ') + msg)
    if not ok:
        problems.append(msg)


RUNNER = textwrap.dedent('''
    import json, os, sys
    sys.path.insert(0, os.environ['GEN'])
    import first_seen as fs
    cfg = json.loads(os.environ['CFG'])
    def providers():
        return [{'slug': s, 'title': s, 'free': False,
                 'movie': {'with_watch_providers': c['ids'], 'with_watch_monetization_types': 'flatrate'},
                 'series': {'with_watch_providers': c['ids'], 'with_watch_monetization_types': 'flatrate'}}
                for s, c in cfg.items()]
    def enumerate_catalogue(p, media, dflt):
        ids = cfg[p['slug']]['titles'] if media == 'movie' else []
        return {i: {'id': i, 'title': f't{i}', 'popularity': i, 'release_date': '2020-01-01', 'genre_ids': [18]}
                for i in ids}, False
    fs.providers, fs.enumerate_catalogue = providers, enumerate_catalogue
    sys.argv = ['first_seen.py', os.environ['STATE']]
    fs.main()
''')


def run(day, cfg, state):
    env = dict(os.environ, SD_TODAY=day, CFG=json.dumps(cfg), STATE=str(state), GEN=str(REPO / 'generator'),
               TMDB_API_KEY='unused', SD_CACHE=str(WORK / 'cache'), PYTHONIOENCODING='utf-8')
    r = subprocess.run([sys.executable, '-c', RUNNER], env=env, capture_output=True, text=True, cwd=WORK)
    (WORK / f'run-{day}.log').write_text(r.stdout + r.stderr, encoding='utf-8')
    if r.returncode:
        print(r.stdout[-2000:], r.stderr[-2000:])
        sys.exit(f'run {day} failed')
    summary = json.loads((state / 'fs_summary.json').read_text(encoding='utf-8'))
    cands = json.loads((state / 'new_candidates.json').read_text(encoding='utf-8'))
    return summary, cands


state = WORK / 'state'
if state.exists():
    for f in state.iterdir():
        f.unlink()
s1, c1 = run('2026-10-01', {'x': {'ids': '1', 'titles': [1, 2, 3]}, 'y': {'ids': '9', 'titles': [10, 11]}}, state)
check(s1['providers']['x']['adds'] == 0 and s1['providers']['y']['adds'] == 0, 'day 1: baseline, 0 adds for x and y')
s2, c2 = run('2026-10-02', {'x': {'ids': '1', 'titles': [1, 2, 3, 4]}, 'y': {'ids': '9', 'titles': [10, 11, 12]}}, state)
check(s2['providers']['x']['adds'] == 1 and s2['providers']['y']['adds'] == 1, 'day 2: genuine arrivals x+1 (4), y+1 (12)')
check(not s2.get('rebaselined'), f"day 2: nothing re-baselined ({s2.get('rebaselined')})")
x3 = list(range(1, 41))
# stored arrival signals for x (as arrivals.merge writes them) must survive x's re-baseline untouched
import gzip
st = json.loads(gzip.decompress((state / 'first_seen.json.gz').read_bytes()))
sys.path.insert(0, str(REPO / 'generator'))
os.environ.setdefault('TMDB_API_KEY', 'unused')
import arrivals as _arr
V = _arr.PARSER_VERSION
st['providers']['x']['signals'] = {   # current observation format: [date, prec, source, season, future, version, url]
    'movie:30': {'o': [['2026-09-20', 'day', 'won', 0, 0, V, 'https://example.test/a']], 'p': 5.0, 'k': 'title', 's': 'announced'},
    'movie:5': {'o': [['2026-09-25', 'week', 'won', 0, 0, V, 'https://example.test/b']], 'p': 7.0, 'k': 'title', 's': 'announced'},
    # an observation from an older parser version (e.g. a departure row read as an arrival) must be purged
    'movie:31': {'o': [['2026-09-26', 'day', 'fb', 0, 0, V - 1, 'https://example.test/c']], 'p': 9.0, 'k': 'title', 's': 'announced'},
    'movie:32': {'o': [['2026-09-26', 'day', 'fb', 0, 0]], 'p': 9.0, 'k': 'title', 's': 'announced'}}
(state / 'first_seen.json.gz').write_bytes(gzip.compress(json.dumps(st).encode()))
before_sig = {k: v['o'] for k, v in st['providers']['x']['signals'].items() if k in ('movie:30', 'movie:5')}
s3, c3 = run('2026-10-03', {'x': {'ids': '1|2', 'titles': x3}, 'y': {'ids': '9', 'titles': [10, 11, 12, 13]}}, state)
st3 = json.loads(gzip.decompress((state / 'first_seen.json.gz').read_bytes()))
after_sig = {k: v['o'] for k, v in st3['providers']['x'].get('signals', {}).items()}
check(after_sig == before_sig, f'day 3: x current-version arrival signals untouched by the re-baseline ({sorted(after_sig)})')
check('movie:31' not in after_sig and 'movie:32' not in after_sig,
      'day 3: observations from an older parser version (and unversioned ones) purged from the state')
sc = json.loads((state / 'signal_candidates.json').read_text(encoding='utf-8'))['providers'].get('x', [])
check(sorted(c['key'] for c in sc) == ['movie:30', 'movie:5'],
      f"day 3: x dated arrivals still offered to its New card ({[(c['key'], c['date']) for c in sc]})")
check(s3.get('rebaselined') == ['x'], f"day 3: only x re-baselined ({s3.get('rebaselined')})")
check(s3['providers']['x']['adds'] == 0, f"day 3: x adds 0 despite 36 newly visible titles ({s3['providers']['x']['adds']})")
check(s3['providers']['y']['adds'] == 1, f"day 3: y keeps counting genuine arrivals (+1 = 13) ({s3['providers']['y']['adds']})")
xc = sorted(int(m['id'].split(':')[1]) for m in c3.get('x', []))
yc = sorted(int(m['id'].split(':')[1]) for m in c3.get('y', []))
check(xc == [4], f'day 3: x New candidates = only its genuine day-2 arrival [4], no flood ({xc})')
check(yc == [12, 13], f'day 3: y New candidates = [12, 13] ({yc})')
check(s3['providers']['x']['history_days'] == 2 and s3['providers']['y']['history_days'] == 2,
      'day 3: both histories kept (2 days since baseline)')

# legacy state: drop the ids field, change nothing else -> counts as changed once, no flood
import gzip
st = json.loads(gzip.decompress((state / 'first_seen.json.gz').read_bytes()))
for p in st['providers'].values():
    p.pop('ids', None)
(state / 'first_seen.json.gz').write_bytes(gzip.compress(json.dumps(st).encode()))
s4, c4 = run('2026-10-04', {'x': {'ids': '1|2', 'titles': x3 + [41]}, 'y': {'ids': '9', 'titles': [10, 11, 12, 13]}}, state)
check(sorted(s4.get('rebaselined', [])) == ['x', 'y'], f"legacy state: both counted as changed once ({s4.get('rebaselined')})")
check(s4['providers']['x']['adds'] == 0, 'legacy state: no adds on that run (41 recorded as baseline)')
s5, c5 = run('2026-10-05', {'x': {'ids': '1|2', 'titles': x3 + [41, 42]}, 'y': {'ids': '9', 'titles': [10, 11, 12, 13]}}, state)
check(not s5.get('rebaselined') and s5['providers']['x']['adds'] == 1, f"next run: normal again, x +1 (42) ({s5.get('rebaselined')}, {s5['providers']['x']['adds']})")
print('RESULT:', 'PASS' if not problems else f'FAIL ({len(problems)})')
sys.exit(1 if problems else 0)
