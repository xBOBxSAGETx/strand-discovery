"""Generate the Strand Discovery static Stremio catalog addon from TMDB.

Output (default ./out): manifest.json, catalog/<type>/<id>.json, catalog/<type>/<id>/skip=<n>.json,
a terminal skip=<total>.json with {"metas": []}, summary.json and report.csv. Each catalog also declares a genre
extra (canonical genres present on the card, >= GENRE_MIN titles) with pre-filtered pages
catalog/<type>/<id>/genre=<G>.json and skip=<n>&genre=<G>.json - AIOStreams' Jellyfin server turns Strand's
genre filter into exactly these requests. No extra TMDB requests: genres come from the same responses.

Env:
  TMDB_API_KEY       required (GitHub Actions secret; never printed)
  PREV_SUMMARY_URL   optional: last deployed summary.json, used by the refresh guards
  FORCE_FAIL         optional: "1" fails the run after building (pilot test: last deploy must stay served)
  ALLOW_CATALOG_REMOVAL optional: "1" (manual runs only) accepts catalogs that were removed/renamed on purpose;
                     the >30% total-items guard still applies
  SD_KINDS_OFF       optional: comma list of catalog kinds to leave out of this deploy (staged rollout: Stage A
                     deploys with "director,actor" so AIOStreams can't surface people libraries before Stage B)
  SD_KEEP            optional: comma list of slugs kept despite SD_KINDS_OFF (the pilot's live library)
  SD_CACHE           optional: directory for the durable lookup cache (default ./cache). Holds only values that
                     don't change (movie runtimes, title previews by TMDB id); discover pages are never cached.
"""
import csv, datetime as dt, json, os, re, sys, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = 'https://api.themoviedb.org/3'
IMG = 'https://image.tmdb.org/t/p'
HERE = Path(__file__).resolve().parent
TODAY = dt.date.today().isoformat()
YEAR_AGO = (dt.date.today() - dt.timedelta(days=365)).isoformat()
MIN_INTERVAL = 1 / 20          # self-throttle: 20 requests/s (TMDB publishes ~40/s)
DROP_LIMIT = 0.30              # abort if total items fall by more than 30%
CARD_DROP = 0.70               # a catalog "dropped" if it lost more than 70% of its items (and had >= 20)
CARD_DROP_MAX = 10             # more than this many dropped catalogs = systemic -> abort
REQUEST_CAP = 40000            # runaway guard: abort before deploying anything
CACHE_FILE = Path(os.environ.get('SD_CACHE', 'cache')) / 'stable.json'

WORKERS = int(os.environ.get('SD_WORKERS', '6'))   # catalogs built in parallel; the 20 req/s throttle is shared

_last = [0.0]
_backfill = [0]
GENRE_BACKFILL_MAX = int(os.environ.get('SD_GENRE_BACKFILL_MAX', '3000'))   # per run; 0 = no refetch
_calls = [0]
_fill = [0, 0]                 # discover fill pass (issue #25): pages read, ids recovered
_lock = threading.Lock()


def tmdb(path, **params):
    key = os.environ.get('TMDB_API_KEY')
    if not key:
        sys.exit('TMDB_API_KEY is not set')
    url = f'{API}{path}?{urllib.parse.urlencode({**params, "api_key": key})}'
    for attempt in range(6):
        with _lock:                                # reserve the next send slot (shared across worker threads)
            slot = max(time.monotonic(), _last[0] + MIN_INTERVAL)
            _last[0] = slot
            _calls[0] += 1
            over = _calls[0] > REQUEST_CAP
        if over:
            sys.exit(f'GUARD: more than {REQUEST_CAP} TMDB requests in one run')
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        # The key rides in the URL, so no error path may surface the URL or the original exception:
        # only the API path and a status/exception class are ever reported, always with `from None`.
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'Accept-Encoding': 'identity'}), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            code = e.code
            retry_after = e.headers.get('Retry-After')
            if code == 429:                        # respect the limit: back off, no retry storm
                time.sleep(float(retry_after or 2 ** attempt))
                continue
            if code >= 500 and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f'TMDB HTTP {code} on {path}') from None
        except Exception as e:                     # URLError, timeouts, bad JSON — class name only
            kind = type(e).__name__
            if attempt < 5:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f'TMDB request failed on {path}: {kind}') from None
    raise RuntimeError(f'TMDB gave up on {path}')


# ---- durable cache: only values that never change (runtimes, previews by TMDB id) -------------------------------
try:
    _stable = json.loads(CACHE_FILE.read_text(encoding='utf-8'))
except (OSError, ValueError):
    _stable = {}
_names = {}                    # per-run memo for source names (collections, keywords, people, providers, …)


def stable(key, fetch):
    if key not in _stable:
        _stable[key] = fetch()
    return _stable[key]


def save_cache():
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(_stable, separators=(',', ':')), encoding='utf-8')


def named(path, field='name', **params):
    key = (path, tuple(sorted(params.items())))
    if key not in _names:
        _names[key] = tmdb(path, **params)
    return _names[key] if field is None else _names[key].get(field, '?')


# TMDB movie + TV genre ids -> one canonical genre list for the per-catalog genre filter (Jellyfin "Genres").
# Names stay [A-Za-z-] so the page file names need no percent-encoding. TV Movie, News and Talk are not offered.
CANON_GENRES = {28: ['Action'], 10759: ['Action', 'Adventure'], 12: ['Adventure'], 16: ['Animation'],
                35: ['Comedy'], 80: ['Crime'], 99: ['Documentary'], 18: ['Drama'], 10751: ['Family'],
                14: ['Fantasy'], 10765: ['Sci-Fi', 'Fantasy'], 36: ['History'], 27: ['Horror'], 10762: ['Kids'],
                10402: ['Music'], 9648: ['Mystery'], 10749: ['Romance'], 878: ['Sci-Fi'], 53: ['Thriller'],
                10752: ['War'], 10768: ['War'], 37: ['Western'], 10764: ['Reality'], 10766: ['Soap']}
GENRE_MIN = 5          # a genre is offered on a card only with at least this many titles (and not all of them)


def canon_genres(ids):
    return sorted({g for i in ids or [] for g in CANON_GENRES.get(i, [])})


def preview(item, media):
    title = item.get('title') or item.get('name')
    date = item.get('release_date') or item.get('first_air_date') or ''
    meta = {'id': f"tmdb:{item['id']}", 'type': media, 'name': title}
    if item.get('poster_path'):
        meta['poster'] = f"{IMG}/w342{item['poster_path']}"
    if date[:4]:
        meta['releaseInfo'] = date[:4]
    # private: canonical genres for the genre pages, stripped before anything is written
    meta['_g'] = canon_genres(item.get('genre_ids') or [g['id'] for g in item.get('genres') or []])
    return meta


def released(item, media):
    date = item.get('release_date') if media == 'movie' else item.get('first_air_date')
    return bool(date) and date <= TODAY


def details_preview(media, tid):
    """Preview for one TMDB id (cached durably); None if unknown or not released yet."""
    key = f'{media}:{tid}'
    old = _stable.get(key)
    # cached before genres were kept: refetch once (one-time migration); on failure the old entry is kept
    with _lock:
        stale = bool(old) and '_g' not in old['p'] and _backfill[0] < GENRE_BACKFILL_MAX
        if stale:
            _backfill[0] += 1
    if key not in _stable or stale:
        try:
            d = tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}")
            _stable[key] = ({'p': preview(d, media), 'd': d.get('release_date') or d.get('first_air_date') or ''}
                            if d.get('id') else None)
        except RuntimeError:          # a transient failure must not stick
            if not stale:
                return None           # not cached: try again next run
            _stable[key] = old        # keep the pre-genre entry (no genre pages for it this run)
    hit = _stable[key]
    return hit['p'] if hit and hit['d'] and hit['d'] <= TODAY else None


SORTS = {
    'popular': ('popularity.desc', 'popularity.desc'),
    'new': ('primary_release_date.desc', 'first_air_date.desc'),
    'top': ('vote_average.desc', 'vote_average.desc'),
    'release_asc': ('primary_release_date.asc', 'first_air_date.asc'),
    'votes': ('vote_count.desc', 'vote_count.desc'),
}


def depth_of(card, dflt):
    return card.get('depth') or dflt['depth'][card['sort']]


_excluded = {}                 # catalog id -> {rule: count}, filled when SD_MEASURE_EXCLUSIONS=1 (report only)


def discover(card, media, depth, dflt):
    movie = media == 'movie'
    sort = SORTS[card['sort']][0 if movie else 1]
    votes = card.get('min_votes', 10)
    if isinstance(votes, dict):
        votes = votes[media]
    params = {'sort_by': sort, 'include_adult': 'false', 'vote_count.gte': votes, **card.get(media, {})}
    cutoff = TODAY
    if card.get('min_age_days'):              # e.g. hidden gems: at least a year old
        cutoff = (dt.date.today() - dt.timedelta(days=card['min_age_days'])).isoformat()
    if not movie and card.get('tv_air_window'):   # any episode aired recently (new seasons of old shows count)
        params.pop('first_air_date.lte', None)
        params.update({'air_date.gte': (dt.date.today() - dt.timedelta(days=card['tv_air_window'])).isoformat(),
                       'air_date.lte': TODAY, 'sort_by': 'popularity.desc'})
    if movie:
        params.update({'with_release_type': '4|5|6', 'release_date.lte': cutoff})
        if card['sort'] in ('top', 'votes'):  # shorts/music videos rise to the top of rating order (smoke test)
            params.setdefault('with_runtime.gte', 40)
    else:
        # released-only without widening a card's own window (decades set their own first_air_date.lte)
        params.update({'with_status': '0|3|4|5', 'first_air_date.lte': min(cutoff, params.get('first_air_date.lte', cutoff))})
        params.setdefault('without_genres', '10767|10763')    # no talk shows or news on any TV card
    if dflt.get('without_keywords'):            # adult-adjacent tags (ecchi/hentai/softcore/…) on every discover card
        own = params.get('without_keywords')
        excl = dflt['without_keywords'] + ('' if movie or not dflt.get('without_keywords_tv')
                                           else '|' + dflt['without_keywords_tv'])
        params['without_keywords'] = f"{own}|{excl}" if own else excl
        if os.environ.get('SD_MEASURE_EXCLUSIONS') == '1':   # how many titles in this card's pool the rule removes
            # TMDB can't group AND/OR, so cards that already filter by keyword aren't measurable this way
            n = 'n/a (keyword card)'
            if 'with_keywords' not in params:
                probe = {k: v for k, v in params.items() if k != 'without_keywords'}
                probe['with_keywords'] = dflt['without_keywords']
                n = tmdb(f"/discover/{'movie' if movie else 'tv'}", page=1, **probe).get('total_results', 0)
            _excluded.setdefault(f"sd-{card['slug']}", {})[f'keywords:{media}'] = n
    path = f"/discover/{'movie' if movie else 'tv'}"
    out, seen, page, raw = [], set(), 1, {}
    while len(out) < depth:
        data = tmdb(path, page=page, **params)
        for item in data.get('results', []):
            if item['id'] not in seen:
                seen.add(item['id'])
                raw[item['id']] = item
                out.append(preview(item, media))
        if page >= min(data.get('total_pages', 0), 500):
            break
        page += 1
    # TMDB caches every /discover page separately (for hours), so the pages of one query can come from different
    # ranking snapshots: ids repeat on two pages and others are never returned (issue #25). When the whole pool was
    # read but ids are missing, read it once more in the REVERSE order (different cache entries) and merge by the
    # sort key. Bounded by total_pages; only runs on a short card with holes.
    total, pages = data.get('total_results', 0), data.get('total_pages', 0)
    if len(out) < depth and len(seen) < total and 0 < pages <= 500:
        sort_by = params['sort_by']
        field, _, way = sort_by.rpartition('.')
        field = {'primary_release_date': 'release_date'}.get(field, field)
        fill = {**params, 'sort_by': f"{sort_by.rpartition('.')[0]}.{'asc' if way == 'desc' else 'desc'}"}
        n0, used = len(seen), 0
        for pg in range(1, pages + 1):
            used += 1
            for item in tmdb(path, page=pg, **fill).get('results', []):
                if item['id'] not in seen:
                    seen.add(item['id'])
                    raw[item['id']] = item
            if len(seen) >= total:
                break
        with _lock:
            _fill[0] += used
            _fill[1] += len(seen) - n0
        print(f"  discover fill {card.get('slug')} {media}: +{len(seen) - n0} ids ({used} pages)", flush=True)
        # one order from the current values (the stale pages' order was the problem); ties keep first-read order
        order = {i: n for n, i in enumerate(raw)}
        have = [i for i in raw if raw[i].get(field) not in (None, '')]
        have.sort(key=lambda i: (raw[i][field], -order[i]) if way == 'desc' else (raw[i][field], order[i]),
                  reverse=way == 'desc')
        rest = [i for i in raw if raw[i].get(field) in (None, '')]
        out = [preview(raw[i], media) for i in have + rest]
    return out[:depth]


def franchise(card):
    """Collections (+ explicit TMDB ids for titles TMDB doesn't group), each medium in release order."""
    movies = []
    for cid in card['collection_ids']:
        movies += [p for p in named(f'/collection/{cid}', None).get('parts', []) if released(p, 'movie')]
    movies = [preview(p, 'movie') for p in sorted(movies, key=lambda p: p['release_date'])]
    extra = {'movie': [], 'series': []}
    for media, tid in card.get('ids', []):
        p = details_preview(media, tid)
        if p:
            extra[media].append(p)
    seen = {m['id'] for m in movies}
    movies += [p for p in extra['movie'] if p['id'] not in seen]
    by_year = lambda p: p.get('releaseInfo', '9999')
    return [sorted(movies, key=by_year), sorted(extra['series'], key=by_year)]


MIN_RUNTIME = 40      # director cards: drop shorts; unknown runtime kept only for well-known titles
KNOWN_VOTES = 500


def director(card, dropped):
    crew = tmdb(f"/person/{card['person_id']}/movie_credits").get('crew', [])
    films = {c['id']: c for c in crew if c.get('job') in ('Director', 'Co-Director') and released(c, 'movie')}
    kept = []
    # best-known first (vote count): release order put restorations/compilations on top (Wyler, Leone - report.csv)
    for c in sorted(films.values(), key=lambda c: (-(c.get('vote_count') or 0), c['release_date'])):
        runtime = stable(f"runtime:{c['id']}", lambda: tmdb(f"/movie/{c['id']}").get('runtime') or 0)
        if runtime >= MIN_RUNTIME or (runtime == 0 and c.get('vote_count', 0) >= KNOWN_VOTES):
            kept.append(preview(c, 'movie'))
        else:
            dropped.append(f"{c.get('title')} ({c['release_date'][:4]}, {runtime or 'no'} min)")
    return kept


SELF = re.compile(r'\b(self|himself|herself|themselves|themself)\b|\(uncredited\)', re.I)
NOT_ACTING = {10767, 10763, 10764}     # Talk, News, Reality
MIN_EPISODES = 5                       # TV: a real role, not a guest arc
MAX_BILLING = 15                       # movies: cast order (0 = top billed)


def actor(card, dropped):
    """Acting credits, movies + TV, best-known first (vote count). Drops talk/news/reality, self/uncredited
    appearances, TV roles under 5 episodes, movie roles billed below #15, unreleased titles, and near-unknown titles
    older than a year (< 10 votes). Popularity order put small recurring TV roles first (report.csv spot check)."""
    cast = tmdb(f"/person/{card['person_id']}/combined_credits").get('cast', [])
    ranked, seen = [], set()
    for c in cast:
        media = {'movie': 'movie', 'tv': 'series'}.get(c.get('media_type'))
        if not media or not released(c, media):
            continue
        date = c.get('release_date') or c.get('first_air_date')
        why = ('not acting' if set(c.get('genre_ids') or []) & NOT_ACTING else
               'self/uncredited' if SELF.search(c.get('character') or '') else
               'minor TV role' if media == 'series' and (c.get('episode_count') or 0) < MIN_EPISODES else
               'bit part' if media == 'movie' and (c.get('order') or 0) > MAX_BILLING else
               'obscure' if (c.get('vote_count') or 0) < 10 and date < YEAR_AGO else None)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        if (media, c['id']) not in seen:
            seen.add((media, c['id']))
            ranked.append((c.get('vote_count') or 0, preview(c, media)))
    return [p for _, p in sorted(ranked, key=lambda x: -x[0])]


MDBLIST_JSON = 'https://mdblist.com/lists/{list}/json'   # public export, no key needed


def mdblist(card, media):
    """Items of a public MDBList list as tmdb-id previews (MDBList `id` is the TMDB id); newest first if asked."""
    with urllib.request.urlopen(urllib.request.Request(MDBLIST_JSON.format(list=card['list']),
                                                       headers={'User-Agent': 'strand-discovery'}), timeout=60) as r:
        items = json.load(r)
    want = 'movie' if media == 'movie' else 'show'
    items = [it for it in items if it.get('mediatype') == want]
    if card.get('order') == 'year_desc':
        items.sort(key=lambda it: -(it.get('release_year') or 0))    # stable: list order within a year
    out = []
    for it in items:
        tid = it.get('id')
        if not tid and it.get('imdb_id'):          # fallback: resolve via TMDB /find
            found = tmdb(f"/find/{it['imdb_id']}", external_source='imdb_id')
            hits = found.get('movie_results' if media == 'movie' else 'tv_results') or []
            tid = hits[0]['id'] if hits else None
        p = details_preview(media, tid) if tid else None
        if p:
            out.append(p)
    return out


def interleave(lists):
    """a0, b0, a1, b1, … so page 1 always holds both kinds (Jellyfin samples the first 20 entries)."""
    out, seen = [], set()
    for row in range(max((len(l) for l in lists), default=0)):
        for lst in lists:
            if row < len(lst) and (lst[row]['id'], lst[row]['type']) not in seen:
                seen.add((lst[row]['id'], lst[row]['type']))
                out.append(lst[row])
    return out


def idlist_ids(card):
    """[[media, id], ...] of an idlist card: its awards.json list (read at build time, so the yearly award refresh
    only has to update awards.json), or ids written into the spec."""
    if 'awards_key' in card:
        return json.loads((HERE / 'awards.json').read_text(encoding='utf-8'))[card['awards_key']]['ids']
    return card['ids']


def source_of(card):
    """(source id, human-readable source name) for report.csv."""
    k = card['kind']
    if k == 'mdblist':
        return card['list'], f"MDBList {card['list']}"
    if k == 'idlist':
        return card.get('awards_key', card['slug']), f"committed id list ({len(idlist_ids(card))} ids, generator/awards.json from Wikipedia)"
    if k == 'franchise':
        names = [named(f'/collection/{c}') for c in card['collection_ids']]
        ids = card.get('ids') or []
        return card['collection_ids'], ' + '.join(names) + (f' + {len(ids)} listed titles' if ids else '')
    if k in ('director', 'actor'):
        return card['person_id'], named(f"/person/{card['person_id']}") + (' (directing)' if k == 'director' else ' (acting)')
    parts, ids = [], []
    for media in card['media']:
        p = card.get(media, {})
        tag = 'movie' if media == 'movie' else 'tv'
        if 'with_watch_providers' in p:
            provs = named(f'/watch/providers/{tag}', 'results', watch_region=p.get('watch_region', 'US'))
            names = {str(x['provider_id']): x['provider_name'] for x in provs}
            ids.append(p['with_watch_providers'])
            parts.append(f"{tag}: " + ' | '.join(names.get(i, i) for i in p['with_watch_providers'].split('|')))
        elif 'with_networks' in p:
            ids.append(p['with_networks'])
            parts.append(f"{tag}: network " + named(f"/network/{p['with_networks']}"))
        elif 'with_companies' in p:
            ids.append(p['with_companies'])
            parts.append(f"{tag}: " + ' | '.join(named(f'/company/{c}') for c in p['with_companies'].split('|')))
        elif 'with_keywords' in p:
            ids.append(p['with_keywords'])
            parts.append(f"{tag}: kw " + ' | '.join(named(f'/keyword/{kw}') for kw in p['with_keywords'].split('|')))
        elif 'with_genres' in p:
            ids.append(p['with_genres'])
            genres = {str(g['id']): g['name'] for g in named(f'/genre/{tag}/list', 'genres')}
            parts.append(f"{tag}: genre {genres.get(p['with_genres'], p['with_genres'])}")
        else:
            parts.append(f"{tag}: {json.dumps(p, sort_keys=True)}")
    votes = card.get('min_votes')
    return ' / '.join(dict.fromkeys(ids)) or '-', '; '.join(parts) + f" [{card['sort']}, votes>={votes}]"


def write_pages(base, cid, metas, page_size, genre=None):
    """First page + skip pages + an empty terminal page. File names are the extras string AIOStreams sends upstream
    (core ExtrasParser, checked on the deployed image): 'genre=G' for the first page, then 'skip=N&genre=G'."""
    tag = f'&genre={genre}' if genre else ''
    d = base / cid
    d.mkdir(parents=True, exist_ok=True)
    first = d / f'genre={genre}.json' if genre else base / f'{cid}.json'
    pages = [(first, metas[:page_size])]
    pages += [(d / f'skip={skip}{tag}.json', metas[skip:skip + page_size]) for skip in range(page_size, len(metas), page_size)]
    pages.append((d / f'skip={len(metas)}{tag}.json', []))          # terminal page: no 404 at the end
    size = 0
    for path, page in pages:
        text = json.dumps({'metas': page}, separators=(',', ':'))
        path.write_text(text, encoding='utf-8')
        size += len(text)
    return len(pages), size


def write_catalog(root, media, cid, metas, page_size):
    """The catalog's pages plus one filtered set per offered genre. Returns (genre options, genre files, bytes)."""
    base = root / 'catalog' / media
    genres = {}
    for m in metas:
        for g in m.get('_g', []):
            genres[g] = genres.get(g, 0) + 1
    public = [{k: v for k, v in m.items() if k != '_g'} for m in metas]   # copies: cached previews keep '_g'
    write_pages(base, cid, public, page_size)
    options = sorted(g for g, n in genres.items() if GENRE_MIN <= n < len(metas))
    files = size = 0
    for g in options:
        f, b = write_pages(base, cid, [p for p, m in zip(public, metas) if g in m.get('_g', [])], page_size, g)
        files, size = files + f, size + b
    return options, files, size


def previous_summary():
    url = os.environ.get('PREV_SUMMARY_URL')
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except Exception as e:  # first run / not deployed yet
        print(f'no previous summary ({e.__class__.__name__}); guards skipped')
        return None


STATE_DIR = Path(os.environ.get('SD_STATE', 'state'))


def _read_json(p, default):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


FS_SUMMARY = _read_json(STATE_DIR / 'fs_summary.json', None)
FS_CANDIDATES = _read_json(STATE_DIR / 'new_candidates.json', {})
# arrival signals (arrivals.py, or the logger's fallback from the stored signals): per provider, date-desc
SIGNALS = _read_json(STATE_DIR / 'signal_candidates.json', {}).get('providers', {})
MIN_SIGNALS = 5                # a New card switches to arrival order with at least this many dated arrivals (was 10;
#                                5 evaluated 2026-09-28: Apple TV and Shudder switch, precision gate 13/13 on service)


def first_seen_mode(card):
    prov = card.get('provider')
    info = (FS_SUMMARY or {}).get('providers', {}).get(prov) if prov else None
    return bool(info and info.get('mode') == 'first_seen' and prov in FS_CANDIDATES)


def first_seen_card(card, depth):
    """New on X = titles first seen on the service within the window, newest first, popularity tiebreak,
    no vote floor (arrivals are often obscure); movies and TV interleaved."""
    c = sorted(FS_CANDIDATES[card['provider']], key=lambda m: (m['first_seen'], m['popularity']), reverse=True)
    strip = lambda m: {k: v for k, v in m.items() if k not in ('first_seen', 'popularity')}
    return interleave([[strip(m) for m in c if m['type'] == t][:depth] for t in ('movie', 'series')])


def arrival_mode(card):
    return len(SIGNALS.get(card.get('provider') or '', [])) >= MIN_SIGNALS


def both_kinds_early(metas, n=20):
    """Keep date order, but make sure the first n entries hold both kinds when the card has both (Jellyfin samples
    the first entries for the library type): the newest entry of a missing kind moves up to position n - 1."""
    head = {m['type'] for m in metas[:n]}
    for t in ('movie', 'series'):
        if t not in head:
            i = next((i for i, m in enumerate(metas) if m['type'] == t), None)
            if i is not None and i >= n:
                metas.insert(n - 1, metas.pop(i))
    return metas


def arrival_card(card, depth, dflt):
    """New on X by ARRIVAL date: dated arrivals from published schedules (arrivals.py; confirmed or announced, last
    45 days) merged with the titles our own daily snapshots first saw (when that history is on), newest first,
    popularity tiebreak; then filled up with the release-date card. Returns (metas, counts)."""
    prov = card['provider']
    timeline, keys = [], set()
    for c in SIGNALS.get(prov, []):
        timeline.append((c['date'], -{'day': 0, 'week': 1, 'month': 2}[c['precision']], c['popularity'], c['key'], None))
        keys.add(c['key'])
    if first_seen_mode(card):
        for m in FS_CANDIDATES.get(prov, []):
            k = f"{m['type']}:{m['id'].split(':', 1)[1]}"
            if k not in keys:
                meta = {kk: v for kk, v in m.items() if kk not in ('first_seen', 'popularity')}
                timeline.append((m['first_seen'], 0, m['popularity'], k, meta))
                keys.add(k)
    timeline.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    cap = depth * len(card['media'])
    out, seen, n_sig = [], set(), 0
    for date, _, _, k, meta in timeline:
        if len(out) >= cap:
            break
        if meta is None:
            media, tid = k.split(':')
            meta = details_preview(media, int(tid))          # durably cached; None = unknown / not released
            n_sig += meta is not None
        if meta and meta['id'] not in seen and meta['type'] in card['media']:
            seen.add(meta['id'])
            out.append(meta)
    n_arr = len(out)
    fill = interleave([discover(card, m, depth, dflt) for m in card['media']])
    out += [m for m in fill if m['id'] not in seen][:max(0, cap - len(out))]
    return both_kinds_early(out), {'signals': n_sig, 'arrivals': n_arr, 'filled': len(out) - n_arr}


def build_one(card, dflt):
    """Returns (metas, notes)."""
    kind, notes = card['kind'], ''
    if kind == 'discover' and arrival_mode(card):
        metas, n = arrival_card(card, depth_of(card, dflt), dflt)
        return metas, (f"ordered by arrival date: {n['signals']} dated arrivals + {n['arrivals'] - n['signals']} "
                       f"first seen, then {n['filled']} by release date")
    if kind == 'discover' and first_seen_mode(card):
        return first_seen_card(card, depth_of(card, dflt)), 'ordered by date first seen on the service'
    if kind == 'discover':
        return interleave([discover(card, m, depth_of(card, dflt), dflt) for m in card['media']]), notes
    if kind == 'franchise':
        return interleave(franchise(card)), notes
    if kind == 'director':
        dropped = []
        metas = director(card, dropped)
        return metas, (f"dropped {len(dropped)} short: " + '; '.join(dropped)) if dropped else ''
    if kind == 'actor':
        dropped = {}
        metas = actor(card, dropped)
        return metas, ('dropped ' + ', '.join(f'{k} {v}' for k, v in sorted(dropped.items()))) if dropped else ''
    if kind == 'mdblist':
        return interleave([mdblist(card, m) for m in card['media']]), notes
    if kind == 'idlist':                       # committed id list (awards.json from awards_wiki.py), in its own order
        metas = [details_preview(m, i) for m, i in idlist_ids(card)]
        return [m for m in metas if m], notes
    raise SystemExit(f'unknown kind {kind}')


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else 'out')
    spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
    off = {k for k in os.environ.get('SD_KINDS_OFF', '').split(',') if k}
    keep = {k for k in os.environ.get('SD_KEEP', '').split(',') if k}
    spec['catalogs'] = [c for c in spec['catalogs'] if c['kind'] not in off or c['slug'] in keep]
    dflt = spec['defaults']
    folder_title = {f['key']: f['title'] for f in spec['folders']}
    manifest = {**spec['addon'], 'resources': ['catalog'], 'types': ['movie', 'series'], 'catalogs': [],
                'behaviorHints': {'configurable': False}}
    counts, rows, t0 = {}, [], time.monotonic()
    genre_stats = {}
    deny = {(m, int(i)) for m, i, _ in dflt.get('deny', [])}
    def work(card):
        metas, notes = build_one(card, dflt)
        return metas, notes, source_of(card)

    try:
        results = {}
        with ThreadPoolExecutor(WORKERS) as pool:
            futs = {pool.submit(work, c): c['slug'] for c in spec['catalogs']}
            for done in as_completed(futs):
                results[futs[done]] = done.result()       # re-raises a worker's error / guard exit here
                if len(results) % 25 == 0:
                    print(f"  {len(results)}/{len(spec['catalogs'])} catalogs, {_calls[0]} requests, "
                          f"{time.monotonic() - t0:.0f}s", flush=True)
        for card in spec['catalogs']:                      # write in spec (= folder) order
            # One catalog per card, used directly as a Jellyfin library (merged catalogs lose deep pages when cold).
            cid = f"sd-{card['slug']}"
            if 'collection' in f"{cid} {card['library']}".lower():   # AIOStreams would make it a BoxSet library
                raise SystemExit(f"catalog {card['slug']!r}: id/name must not contain 'collection'")
            metas, notes, (src_id, src_name) = results[card['slug']]
            before = len(metas)
            metas = [m for m in metas if (m['type'], int(m['id'].split(':', 1)[1])) not in deny]
            if len(metas) < before:
                _excluded.setdefault(cid, {})['deny'] = before - len(metas)
                notes = f"{notes}; deny-list removed {before - len(metas)}".lstrip('; ')
            card_ex = {(m, int(i)) for m, i in card.get('exclude', [])}       # this card only (e.g. Oscar 1927/28)
            if card_ex:
                n = len(metas)
                metas = [m for m in metas if (m['type'], int(m['id'].split(':', 1)[1])) not in card_ex]
                _excluded.setdefault(cid, {})['card_exclude'] = n - len(metas)
                notes = f"{notes}; card exclusions removed {n - len(metas)}".lstrip('; ')
            options, gfiles, gbytes = write_catalog(root, 'movie', cid, metas, dflt['page_size'])
            genre_stats[cid] = {'options': len(options), 'files': gfiles, 'bytes': gbytes,
                                'untagged': sum(1 for m in metas if not m.get('_g'))}
            extra = ([{'name': 'genre', 'options': options, 'isRequired': False}] if options else []) + [{'name': 'skip'}]
            manifest['catalogs'].append({'type': 'movie', 'id': cid, 'name': card['library'], 'extra': extra})
            counts[cid] = len(metas)
            kinds = {t: sum(1 for m in metas if m['type'] == t) for t in ('movie', 'series')}
            rows.append([card['title'], card['library'], ' + '.join(folder_title[f] for f in card['folders']),
                         card.get('sort', card.get('order', 'default')), card['kind'], json.dumps(src_id) if not
                         isinstance(src_id, str) else src_id, src_name, cid,
                         f"movie={kinds['movie']} series={kinds['series']}", len(metas),
                         ' | '.join(f"{m['name']} ({m.get('releaseInfo', '?')})" for m in metas[:5]), notes])
    finally:
        save_cache()
    ids = [c['id'] for c in manifest['catalogs']]
    if len(ids) != len(spec['catalogs']) or len(set(ids)) != len(ids):
        sys.exit(f"GUARD: manifest has {len(ids)} catalogs ({len(set(ids))} unique), spec has {len(spec['catalogs'])}")
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding='utf-8')
    (root / '.nojekyll').write_text('', encoding='utf-8')
    summary = {'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'tmdb_requests': _calls[0],
               'seconds': round(time.monotonic() - t0), 'total_items': sum(counts.values()), 'catalogs': counts}
    summary['genre_pages'] = {'files': sum(g['files'] for g in genre_stats.values()),
                              'bytes': sum(g['bytes'] for g in genre_stats.values()),
                              'options': sum(g['options'] for g in genre_stats.values()),
                              'untagged_items': sum(g['untagged'] for g in genre_stats.values()),
                              'backfilled': _backfill[0], 'min_titles': GENRE_MIN}
    summary['discover_fill'] = {'pages': _fill[0], 'recovered': _fill[1]}
    fs = FS_SUMMARY or {'status': 'missing (logger did not run or failed)'}
    summary['first_seen'] = {k: v for k, v in fs.items() if k != 'providers'}
    summary['first_seen']['providers'] = {p: {k: v for k, v in i.items() if k in ('size', 'adds', 'removals', 'churn',
                                                                                  'history_days', 'mode', 'sliced')}
                                          for p, i in fs.get('providers', {}).items()}
    summary['new_card_modes'] = {c['slug']: ('arrivals' if arrival_mode(c) else 'first_seen' if first_seen_mode(c)
                                             else 'release_date')
                                 for c in spec['catalogs'] if c.get('provider')}
    (root / 'summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    if _excluded:
        (root / 'exclusions.json').write_text(json.dumps(_excluded, indent=1), encoding='utf-8')
    with open(root / 'report.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['card', 'library', 'folders', 'sort', 'source_type', 'source_id', 'source_name', 'catalog_id',
                    'media', 'item_count', 'top5', 'notes'])
        w.writerows(rows)
    print(json.dumps({k: v for k, v in summary.items() if k != 'catalogs'}), '| catalogs:', len(counts))

    empty = [c for c, n in counts.items() if n == 0]
    if empty:
        sys.exit(f'GUARD: empty catalogs: {empty}')
    prev = previous_summary()
    if prev:
        emptied = [c for c, n in prev.get('catalogs', {}).items() if n > 0 and counts.get(c, 0) == 0]
        if emptied and os.environ.get('ALLOW_CATALOG_REMOVAL') == '1':
            print(f'catalogs removed/renamed on purpose (manual run): {emptied}')
        elif emptied:
            sys.exit(f'GUARD: previously non-empty catalogs are now empty: {emptied}')
        if prev.get('total_items') and summary['total_items'] < prev['total_items'] * (1 - DROP_LIMIT):
            sys.exit(f"GUARD: total items dropped {prev['total_items']} -> {summary['total_items']} (>30%)")
        dropped = [f'{c} {n}->{counts[c]}' for c, n in prev.get('catalogs', {}).items()
                   if n >= 20 and 0 < counts.get(c, 0) < n * (1 - CARD_DROP)]
        if len(dropped) > CARD_DROP_MAX:
            sys.exit(f'GUARD: {len(dropped)} catalogs lost >70% of their items: {dropped}')
        if dropped:
            print(f'WARNING: catalogs that lost >70% of their items: {dropped}')
    if os.environ.get('FORCE_FAIL') == '1':
        sys.exit('FORCE_FAIL=1: failing on purpose (pilot test)')


if __name__ == '__main__':
    main()
