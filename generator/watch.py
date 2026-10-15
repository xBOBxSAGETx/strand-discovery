"""Watcher (issues #78, #79): a cheap "anything new?" check against every source; the output is the list of cards to rebuild.

  python watch.py [--out changes.json] [--cache DIR] [--no-tmdb] [--dry] [--dispatch]

Sources and what a change maps to:
  news feeds (5)        conditional GET (ETag / Last-Modified); 304 = nothing. 200 with a new body = the arrival
                        ("New") cards of the services that feed covers (arrivals.py's own service tables).
  MDBList exports (3)   same conditional GET; 200 with a new body = the card(s) built from that list.
  streaming catalogs    per provider x medium ONE discover call (page 1, newest first, the New card's filters): the
                        fingerprint is total_results + the SORTED ids of page 1. A change = every card whose spec uses
                        that provider (New, Popular, Top, ...). A changed reading counts only when the NEXT run reads the
                        same thing (two in a row): TMDB's cached discover pages can show an old and a new page by turns.
NOT a source: TMDB /movie/changes and /tv/changes. Measured (issue #79): one TMDB refresh (about 8 a day) lists ~1,600
changed titles, which sit in ~320 of the 606 cards (always over the 150 limit, so every refresh would be a full build),
and almost all are edits (a poster, a translation) that do not move a title on or off a shelf. The nightly build picks
those edits up. Dropping it also drops the id -> cards index (~1,800 Pages requests after every deploy).

More than MAX_CARDS (150) cards = "full": true and no card list (the caller does a full rebuild).

State (cache/watch_state.json, kept by the workflow in the Actions cache) holds validators, body hashes and provider
fingerprints. The FIRST run of any source is a baseline: it records and reports nothing.

--dispatch (the workflow): when there is something to rebuild, start build.yml on main with `gh workflow run`
(deploy=true, cards=<list> or no cards for a full build). Only ONE build of main at a time: while a build of main is
queued or running, nothing is dispatched (GitHub keeps one pending run per concurrency group and cancels an older
pending one, which would lose its cards), the state is NOT saved, and the next run sees the same change again. The state
is saved only when nothing needed dispatching or the dispatch succeeded; a failed dispatch exits 1 and saves nothing.
--dry reads and reports but writes no state and dispatches nothing.

Env: TMDB_API_KEY (never printed; without it the TMDB parts are skipped, see --no-tmdb), SD_CACHE (default cache),
SD_WATCH_MAX_CARDS, SD_GH (the gh command, default "gh"), GITHUB_OUTPUT (when set: dispatch=, dirty=).
"""
import datetime as dt, hashlib, json, os, shlex, subprocess, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import arrivals  # noqa: E402  (service tables + the polite User-Agent)
import build  # noqa: E402  (throttled, key-safe tmdb())

UA = arrivals.UA
MAX_CARDS = int(os.environ.get('SD_WATCH_MAX_CARDS') or 150)

# The URLs arrivals.py and build.py fetch (tests/test_watch.py checks each one is still in their source).
FEED_URLS = {
    'won': 'https://www.whats-on-netflix.com/whats-new/feed/',
    'vt': 'https://www.vitalthrills.com/tag/streaming-schedule/feed/',
    'wodp': 'https://whatsondisneyplus.com/feed/',
    'fb': 'https://film-book.com/category/streaming-schedule/feed/',
    'plex': 'https://www.plex.tv/blog/feed/',
}
MDBLIST_JSON = build.MDBLIST_JSON


def feed_services():
    """Feed -> the spec service slugs its posts can carry (arrivals.py's tables; 'disney+hulu' is one post for two)."""
    vt = set()
    for s in arrivals.VT_SERVICES.values():
        vt |= {'disneyplus', 'hulu'} if s == 'disney+hulu' else {s}
    out = {'won': {'netflix'}, 'vt': vt, 'wodp': set(arrivals.WODP.values()),
           'fb': set(arrivals.FB_SERVICES.values()), 'plex': {'plex'}}
    return {k: v - arrivals.DROPPED for k, v in out.items()}


def http_get(url, headers):
    """(status, lower-cased response headers, body). Status 0 = network error. A 304 comes back as 304."""
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'identity', **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, b''
    except Exception:
        return 0, {}, b''


def load_spec():
    return json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))


def prov_ids(card):
    """The TMDB watch-provider ids a card's spec filters on (either medium)."""
    out = set()
    for m in ('movie', 'series'):
        v = (card.get(m) or {}).get('with_watch_providers')
        if v:
            out |= {x for x in str(v).replace(',', '|').split('|') if x}
    return out


def fp_params(card, media, dflt, today):
    """discover() params for page 1 of a New card (build.discover, sort 'new'): same filters, newest first by the date
    field the card uses. tests/test_watch.py replays build.discover and compares."""
    movie = media == 'movie'
    votes = card.get('min_votes', 10)
    if isinstance(votes, dict):
        votes = votes[media]
    p = {'sort_by': build.SORTS['new'][0 if movie else 1], 'include_adult': 'false', 'vote_count.gte': votes,
         **card.get(media, {})}
    if movie:
        p.update({'with_release_type': '4|5|6', 'release_date.lte': today})
    else:
        if card.get('tv_air_window'):      # any episode aired recently: the window picks the pool (build.discover)
            p.pop('first_air_date.lte', None)
            p.update({'air_date.gte': (dt.date.fromisoformat(today) - dt.timedelta(days=card['tv_air_window'])).isoformat(),
                      'air_date.lte': today})
        p.update({'with_status': '0|3|4|5', 'first_air_date.lte': min(today, p.get('first_air_date.lte', today))})
        p.setdefault('without_genres', '10767|10763')
    if dflt.get('without_keywords'):
        own = p.get('without_keywords')
        excl = dflt['without_keywords'] + ('' if movie or not dflt.get('without_keywords_tv')
                                           else '|' + dflt['without_keywords_tv'])
        p['without_keywords'] = f'{own}|{excl}' if own else excl
    return p


class Watch:
    def __init__(self, spec, state, http=http_get, tmdb=None, today=None, max_cards=MAX_CARDS, workers=6):
        self.spec, self.state, self.http, self.tmdb = spec, state, http, tmdb
        self.today = today or dt.date.today().isoformat()
        self.max_cards, self.workers = max_cards, workers
        self.cards = spec['catalogs']
        self.by_slug = {c['slug']: c for c in self.cards}
        self.reasons, self.errors, self.requests = {}, [], 0
        self._lock = threading.Lock()            # the discover calls run on worker threads: `+= 1` alone can lose counts
        self.log = {}
        for k in ('http', 'fp'):
            state.setdefault(k, {})

    # ---- bookkeeping
    def why(self, slugs, text):
        for s in slugs:
            if s in self.by_slug:
                self.reasons.setdefault(s, [])
                if text not in self.reasons[s]:
                    self.reasons[s].append(text)

    def count(self):
        with self._lock:
            self.requests += 1

    def get(self, url, headers=None):
        self.count()
        return self.http(url, headers or {})

    def call_tmdb(self, path, **params):
        self.count()
        return self.tmdb(path, **params)

    # ---- source -> cards
    def new_cards(self, services):
        return [c['slug'] for c in self.cards if c.get('sort') == 'new' and c.get('provider') in services]

    def provider_cards(self, ids):
        return [c['slug'] for c in self.cards if prov_ids(c) & ids]

    # ---- 1. feeds and MDBList: conditional GET
    def conditional(self, url):
        """'changed' | 'same' | 'baseline' | 'error'. Records validators + a body hash (a server that ignores the
        conditional headers and answers 200 with the same body is still 'same')."""
        rec = self.state['http'].get(url)
        h = {}
        if rec and rec.get('etag'):
            h['If-None-Match'] = rec['etag']
        if rec and rec.get('lm'):
            h['If-Modified-Since'] = rec['lm']
        st, hd, body = self.get(url, h)
        if st == 304:
            return 'same'
        if st != 200:
            self.errors.append(f'{url}: HTTP {st}')
            return 'error'
        new = {'etag': hd.get('etag'), 'lm': hd.get('last-modified'), 'sha': hashlib.sha256(body).hexdigest()}
        self.state['http'][url] = new
        if not rec:
            return 'baseline'
        return 'same' if rec.get('sha') == new['sha'] else 'changed'

    def check_feeds(self):
        svc = feed_services()
        out = {}
        for name, url in FEED_URLS.items():
            out[name] = r = self.conditional(url)
            if r == 'changed':
                self.why(self.new_cards(svc[name]), f'feed {name} changed')
        self.log['feeds'] = out
        lists = {}
        for c in self.cards:
            if c.get('kind') == 'mdblist':
                lists.setdefault(c['list'], []).append(c['slug'])
        out = {}
        for lst, slugs in lists.items():
            out[lst] = r = self.conditional(MDBLIST_JSON.format(list=lst))
            if r == 'changed':
                self.why(slugs, f'mdblist {lst} changed')
        self.log['mdblist'] = out

    # ---- 2. streaming catalogs: one discover call per provider x medium
    def judge(self, key, fp):
        """'baseline' | 'same' | 'pending' | ('changed', old total). Two readings in a row that differ from the confirmed
        one and agree with each other = a change; one odd reading (cache noise) is held as `pend` and forgotten when the
        next reading goes back."""
        old = self.state['fp'].get(key)
        if old is None or old.get('day') != self.today:        # the query itself moves with the date: re-baseline
            self.state['fp'][key] = fp
            return 'baseline'
        if (old['t'], sorted(old['ids'])) == (fp['t'], fp['ids']):
            old.pop('pend', None)
            return 'same'
        pend = old.get('pend')
        if pend and (pend['t'], pend['ids']) == (fp['t'], fp['ids']):
            self.state['fp'][key] = fp
            return 'changed', old['t']
        old['pend'] = {'t': fp['t'], 'ids': fp['ids']}
        return 'pending'

    def check_providers(self):
        dflt = self.spec['defaults']
        jobs = []
        for pop in self.cards:
            if pop['slug'].startswith('streaming-') and pop.get('sort') == 'popular':
                slug = pop['slug'][len('streaming-'):]
                new = self.by_slug.get(f'streaming-{slug}-new', pop)
                for media in ('movie', 'series'):
                    if media in new.get('media', []) and new.get(media):
                        jobs.append((slug, media, pop, new))

        def one(job):
            slug, media, pop, new = job
            path = f"/discover/{'movie' if media == 'movie' else 'tv'}"
            try:
                d = self.call_tmdb(path, page=1, **fp_params(new, media, dflt, self.today))
            except (RuntimeError, SystemExit) as e:
                return job, None, str(e)
            return job, {'t': d.get('total_results', 0), 'ids': sorted(r['id'] for r in d.get('results', [])),
                         'day': self.today}, None

        with ThreadPoolExecutor(self.workers) as pool:
            results = list(pool.map(one, jobs))
        n = {'baseline': 0, 'same': 0, 'pending': 0}
        changed = []
        for (slug, media, pop, _), fp, err in results:
            key = f'{slug}:{media}'
            if err:
                self.errors.append(f'discover {key}: {err}')
                continue
            r = self.judge(key, fp)
            if isinstance(r, tuple):
                changed.append(key)
                self.why(self.provider_cards(prov_ids(pop)), f'catalog {key} changed ({r[1]} -> {fp["t"]} titles)')
            else:
                n[r] += 1
        self.log['providers'] = {'checked': len(jobs), 'changed': changed, **n}

    # ---- run
    def run(self, tmdb_on=True):
        self.check_feeds()
        if tmdb_on and self.tmdb:
            self.check_providers()
        else:
            self.log['tmdb'] = 'skipped'
        cards = sorted(self.reasons)
        full = len(cards) > self.max_cards
        reasons = self.reasons
        if full:
            reasons = {'*': [f'{len(cards)} cards would rebuild (limit {self.max_cards})']}
            cards = []
        return {'cards': cards, 'reasons': reasons, 'full': full, 'requests': self.requests, 'errors': self.errors,
                'sources': self.log}


# ---- dispatch: start build.yml on main (gh workflow run; the workflow's GITHUB_TOKEN needs `actions: write`)
def gh_cmd():
    return shlex.split(os.environ.get('SD_GH') or 'gh')


def dispatch_cmd(res):
    """The gh command for this result, or None when nothing needs rebuilding."""
    base = gh_cmd() + ['workflow', 'run', 'build.yml', '--ref', 'main', '-f', 'deploy=true']
    if res['full']:
        return base
    if res['cards']:
        return base + ['-f', 'cards=' + ','.join(res['cards'])]
    return None


def build_busy(run=subprocess.run):
    """True while a build of main is queued or running (raises on a gh failure)."""
    r = run(gh_cmd() + ['run', 'list', '--workflow', 'build.yml', '--branch', 'main', '--limit', '30', '--json', 'status'],
            capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f'gh run list failed (exit {r.returncode}): {r.stderr.strip()[:300]}')
    return any(x.get('status') != 'completed' for x in json.loads(r.stdout or '[]'))


def dispatch(res, run=subprocess.run):
    """'none' (nothing to rebuild) | 'sent' | 'held' (a build of main is already queued / running) | 'failed'."""
    cmd = dispatch_cmd(res)
    if cmd is None:
        return 'none'
    try:
        if build_busy(run):
            return 'held'
        r = run(cmd, capture_output=True, text=True, timeout=120)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        print(f'watch: dispatch failed: {e}')
        return 'failed'
    if r.returncode:
        print(f'watch: dispatch failed (exit {r.returncode}): {r.stderr.strip()[:300]}')
        return 'failed'
    return 'sent'


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def save_atomic(path, text):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(path)


def main(argv=None, run=subprocess.run):
    a = sys.argv[1:] if argv is None else argv
    arg = lambda f, d: a[a.index(f) + 1] if f in a else d
    out = Path(arg('--out', 'changes.json'))
    cache = Path(arg('--cache', os.environ.get('SD_CACHE', 'cache')))
    cache.mkdir(parents=True, exist_ok=True)
    state_file = cache / 'watch_state.json'
    state = load_json(state_file, {})
    before = json.dumps(state, sort_keys=True, separators=(',', ':'))
    tmdb_on = '--no-tmdb' not in a and bool(os.environ.get('TMDB_API_KEY'))
    t0 = time.monotonic()
    w = Watch(load_spec(), state, http=http_get, tmdb=build.tmdb if tmdb_on else None, today=build.TODAY,
              max_cards=MAX_CARDS)
    res = w.run(tmdb_on)
    dry = '--dry' in a
    res['dispatch'] = dispatch(res, run) if '--dispatch' in a and not dry else 'none'
    out.write_text(json.dumps(res, indent=1), encoding='utf-8')
    dirty = False
    if not dry and res['dispatch'] in ('none', 'sent'):          # never after a held or failed dispatch: that change must come back
        after = json.dumps(state, sort_keys=True, separators=(',', ':'))
        dirty = after != before
        if dirty:
            save_atomic(state_file, json.dumps(state, separators=(',', ':')))
    print(f"watch: {'FULL' if res['full'] else len(res['cards'])} cards to rebuild, {res['requests']} requests, "
          f"{len(res['errors'])} errors, dispatch {res['dispatch']}, state {'saved' if dirty else 'unchanged'}, "
          f"{time.monotonic() - t0:.0f}s")
    for text in sorted({t for ts in res['reasons'].values() for t in ts}):
        print(f"  why: {text} ({sum(text in ts for ts in res['reasons'].values())} cards)")
    for e in res['errors']:
        print(f'  error: {e}')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as fh:
            fh.write(f"dispatch={res['dispatch']}\ndirty={'true' if dirty else 'false'}\n")
    if res['dispatch'] == 'failed':
        sys.exit(1)
    return res


if __name__ == '__main__':
    main()
