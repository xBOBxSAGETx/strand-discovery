"""Watcher (issue #78): a cheap "anything new?" check against every source; the output is the list of cards to rebuild.

  python watch.py [--out changes.json] [--cache DIR] [--no-tmdb] [--dry]

Sources and what a change maps to:
  news feeds (5)        conditional GET (ETag / Last-Modified); 304 = nothing. 200 with a new body = the arrival
                        ("New") cards of the services that feed covers (arrivals.py's own service tables).
  MDBList exports (3)   same conditional GET; 200 with a new body = the card(s) built from that list.
  streaming catalogs    per provider x medium ONE discover call (page 1, newest first, the New card's filters): the
                        fingerprint is total_results + the first page's ids. A change = every card whose spec uses
                        that provider (New, Popular, Top, ...).
  TMDB /movie/changes   page 1 only while total_results is unchanged (TMDB refreshes the list ~8x a day); when it moves,
  and /tv/changes       all pages are read and the NEW ids map to the cards that contain them (id -> cards index).
The id -> cards index is built from the live site's catalog files (manifest + pages) and rebuilt only when the
deployed summary.json has a new generated_at (so once per deploy, ~2000 requests, kept in the cache).
More than MAX_CARDS (150) cards = "full": true and no card list (the caller does a full rebuild).

State (cache/watch_state.json, persisted by the workflow in the Actions cache) holds validators, body hashes, provider
fingerprints and the change ids already handled. The FIRST run of any source is a baseline: it records and reports
nothing. The state is written at the end of the run (use --dry to skip), so the workflow should save it only after it
has dispatched the rebuild; a failed dispatch would otherwise lose that change.

Env: TMDB_API_KEY (never printed; without it the TMDB parts are skipped, see --no-tmdb), SD_CACHE (default cache),
SD_WATCH_SITE (default the published Pages site), SD_WATCH_MAX_CARDS, SD_WATCH_MAX_CHANGE_PAGES.
"""
import datetime as dt, gzip, hashlib, json, os, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import arrivals  # noqa: E402  (service tables + the polite User-Agent)
import build  # noqa: E402  (throttled, key-safe tmdb())

UA = arrivals.UA
SITE = os.environ.get('SD_WATCH_SITE', 'https://xbobxsagetx.github.io/strand-discovery').rstrip('/')
MAX_CARDS = int(os.environ.get('SD_WATCH_MAX_CARDS') or 150)
MAX_CHANGE_PAGES = int(os.environ.get('SD_WATCH_MAX_CHANGE_PAGES') or 100)   # per medium and run (100 ids a page)
SEEN_KEEP_DAYS = 2            # a change id is remembered this long (TMDB's list covers the last 24 h)
PAGE_SIZE = 100               # spec.json defaults.page_size (catalog pages)

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
    def __init__(self, spec, state, http=http_get, tmdb=None, site=SITE, today=None, max_cards=MAX_CARDS,
                 index=None, max_change_pages=MAX_CHANGE_PAGES, workers=6):
        self.spec, self.state, self.http, self.tmdb, self.site = spec, state, http, tmdb, site
        self.today = today or dt.date.today().isoformat()
        self.max_cards, self.max_change_pages, self.workers = max_cards, max_change_pages, workers
        self.index = index                       # {"generated_at", "cards": [slug], "ids": {"m1": [i, ...]}}
        self.cards = spec['catalogs']
        self.by_slug = {c['slug']: c for c in self.cards}
        self.reasons, self.errors, self.requests, self.index_requests = {}, [], 0, 0
        self.log = {}
        for k in ('http', 'fp', 'changes'):
            state.setdefault(k, {})

    # ---- bookkeeping
    def why(self, slugs, text):
        for s in slugs:
            if s in self.by_slug:
                self.reasons.setdefault(s, [])
                if text not in self.reasons[s]:
                    self.reasons[s].append(text)

    def get(self, url, headers=None, index=False):
        self.requests += 1
        if index:
            self.index_requests += 1
        return self.http(url, headers or {})

    def call_tmdb(self, path, **params):
        self.requests += 1
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
            return job, {'t': d.get('total_results', 0), 'ids': [r['id'] for r in d.get('results', [])], 'day': self.today}, None

        with ThreadPoolExecutor(self.workers) as pool:
            results = list(pool.map(one, jobs))
        changed, base, same = [], 0, 0
        for (slug, media, pop, _), fp, err in results:
            key = f'{slug}:{media}'
            if err:
                self.errors.append(f'discover {key}: {err}')
                continue
            old = self.state['fp'].get(key)
            self.state['fp'][key] = fp
            if old is None or old.get('day') != self.today:    # the query itself moves with the date: re-baseline
                base += 1
            elif (old['t'], old['ids']) != (fp['t'], fp['ids']):
                changed.append(key)
                self.why(self.provider_cards(prov_ids(pop)), f'catalog {key} changed ({old["t"]} -> {fp["t"]} titles)')
            else:
                same += 1
        self.log['providers'] = {'checked': len(jobs), 'changed': changed, 'baseline': base, 'same': same}

    # ---- 3. TMDB changes -> the cards that contain the ids
    def fetch_changes(self, kind):
        """-> 'error', or (ids or None when the list is unchanged, total_results). Page 1 first; the rest only when
        total_results moved."""
        path = f'/{kind}/changes'
        st = self.state['changes'].setdefault(kind, {})
        try:
            first = self.call_tmdb(path, page=1)
            total, pages = first.get('total_results', 0), first.get('total_pages', 1) or 1
            if st.get('total') == total and 'seen' in st:
                return None, total
            ids, limit = [r['id'] for r in first.get('results', [])], min(pages, self.max_change_pages)
            for pg in range(2, limit + 1):
                ids += [r['id'] for r in self.call_tmdb(path, page=pg).get('results', [])]
        except (RuntimeError, SystemExit) as e:
            self.errors.append(f'{path}: {e}')
            return 'error'
        if pages > limit:
            self.errors.append(f'{path}: {pages} pages, read {limit} (cap)')
        return ids, total

    def check_changes(self):
        out = {}
        cutoff = (dt.date.fromisoformat(self.today) - dt.timedelta(days=SEEN_KEEP_DAYS)).isoformat()
        hit = set()
        for kind, letter in (('movie', 'm'), ('tv', 't')):
            st = self.state['changes'].setdefault(kind, {})
            res = self.fetch_changes(kind)
            if res == 'error':
                out[kind] = 'error'
                continue
            ids, total = res
            if ids is None:
                out[kind] = 'same'
                continue
            seen = {k: v for k, v in st.get('seen', {}).items() if v >= cutoff}
            fresh = [f'{letter}{i}' for i in ids if f'{letter}{i}' not in seen]
            baseline = 'seen' not in st
            for k in fresh:
                seen[k] = self.today
            st.update(total=total, seen=seen)
            out[kind] = 'baseline' if baseline else f'{len(fresh)} new ids'
            if not baseline and fresh and self.index:
                for k in fresh:
                    hit.update(self.index['ids'].get(k, ()))
            elif not baseline and fresh:
                self.errors.append('changes: no id index, ids not mapped')
        if hit:
            self.why([self.index['cards'][i] for i in hit], 'TMDB changed one of its titles')
        self.log['changes'] = out

    # ---- the id -> cards index (live site)
    def site_json(self, path, index=True):
        st, _, body = self.get(f'{self.site}/{path}', index=index)
        if st != 200:
            return None
        try:
            return json.loads(body.decode('utf-8'))
        except ValueError:
            return None

    def refresh_index(self):
        """Rebuild the index when the deployed summary.json has a new generated_at (1 request while nothing deployed)."""
        summ = self.site_json('summary.json', index=False)
        if not summ:
            self.errors.append('index: summary.json unavailable')
            return
        gen = summ.get('generated_at')
        if self.index and self.index.get('generated_at') == gen:
            self.log['index'] = 'current'
            return
        man = self.site_json('manifest.json')
        if not man:
            self.errors.append('index: manifest.json unavailable')
            return
        slugs = [c['id'][3:] for c in man['catalogs'] if c['id'].startswith('sd-')]
        ids = {}

        def walk(n_slug):
            n, slug = n_slug
            found, skip = [], 0
            while True:
                d = self.site_json(f'catalog/movie/sd-{slug}.json' if skip == 0 else
                                   f'catalog/movie/sd-{slug}/skip={skip}.json')
                metas = (d or {}).get('metas') or []
                found += [('m' if m['type'] == 'movie' else 't') + m['id'].split(':', 1)[1] for m in metas]
                if len(metas) < PAGE_SIZE:
                    return n, found, d is not None or skip > 0
                skip += PAGE_SIZE

        with ThreadPoolExecutor(8) as pool:
            for n, found, ok in pool.map(walk, list(enumerate(slugs))):
                if not ok:
                    self.errors.append(f'index: catalog {slugs[n]} unreadable')
                for k in found:
                    ids.setdefault(k, []).append(n)
        self.index = {'generated_at': gen, 'cards': slugs, 'ids': ids}
        self.log['index'] = f'rebuilt ({len(slugs)} cards, {len(ids)} ids, {self.index_requests} requests)'

    # ---- run
    def run(self, tmdb_on=True):
        self.check_feeds()
        self.refresh_index()
        if tmdb_on and self.tmdb:
            self.check_providers()
            self.check_changes()
        else:
            self.log['tmdb'] = 'skipped'
        cards = sorted(self.reasons)
        full = len(cards) > self.max_cards
        reasons = self.reasons
        if full:
            reasons = {'*': [f'{len(cards)} cards would rebuild (limit {self.max_cards})']}
            cards = []
        self.state['checked'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        return {'cards': cards, 'reasons': reasons, 'full': full, 'requests': self.requests,
                'quiet_requests': self.requests - self.index_requests, 'errors': self.errors, 'sources': self.log}


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def load_index(cache):
    try:
        with gzip.open(cache / 'watch_index.json.gz', 'rt', encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_atomic(path, text_or_writer):
    tmp = path.with_name(path.name + '.tmp')
    text_or_writer(tmp)
    tmp.replace(path)


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    arg = lambda f, d: a[a.index(f) + 1] if f in a else d
    out = Path(arg('--out', 'changes.json'))
    cache = Path(arg('--cache', os.environ.get('SD_CACHE', 'cache')))
    cache.mkdir(parents=True, exist_ok=True)
    state = load_json(cache / 'watch_state.json', {})
    tmdb_on = '--no-tmdb' not in a and bool(os.environ.get('TMDB_API_KEY'))
    t0 = time.monotonic()
    w = Watch(load_spec(), state, tmdb=build.tmdb if tmdb_on else None, today=build.TODAY, index=load_index(cache))
    res = w.run(tmdb_on)
    if '--dry' not in a:
        save_atomic(cache / 'watch_state.json', lambda p: p.write_text(json.dumps(state, separators=(',', ':')), encoding='utf-8'))
        if w.index is not None:
            def wr(p):
                with gzip.open(p, 'wt', encoding='utf-8') as fh:
                    json.dump(w.index, fh, separators=(',', ':'))
            save_atomic(cache / 'watch_index.json.gz', wr)
    out.write_text(json.dumps(res, indent=1), encoding='utf-8')
    print(f"watch: {'FULL' if res['full'] else len(res['cards'])} cards to rebuild, {res['requests']} requests "
          f"({res['quiet_requests']} excluding the index), {len(res['errors'])} errors, {time.monotonic() - t0:.0f}s")
    return res


if __name__ == '__main__':
    main()
