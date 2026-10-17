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
                     the >30% total-items guard then compares only the catalogs that remain
  SD_KINDS_OFF       optional: comma list of catalog kinds to leave out of this deploy (staged rollout: Stage A
                     deploys with "director,actor" so AIOStreams can't surface people libraries before Stage B)
  SD_KEEP            optional: comma list of slugs kept despite SD_KINDS_OFF (the pilot's live library)
  SD_CACHE           optional: directory for the durable lookup cache (default ./cache). Holds only values that
                     don't change (movie runtimes, title previews by TMDB id); discover pages are never cached.
  SD_CARDS           optional: PARTIAL REBUILD (issue #77). Comma-separated card slugs, with or without the "sd-"
                     prefix (studio-a24 or sd-studio-a24), or the path of a file holding them (commas/newlines).
                     Only these cards are built (the rest of the run is skipped); every other file is restored
                     byte-for-byte from the cached copy of the last deployed site (SD_BASE_DIR, restored by the
                     workflow from the Actions cache sd-site-*), after one live summary.json read proves the copy is
                     what is live. out/ is again a complete site. The guards run on the listed cards only (see partial_guards).
                     Unset/empty = the normal full build.
  SD_BASE_DIR        the cached site a partial rebuild starts from.
  SD_SITE_URL        optional: base URL of the live site (its summary.json is read twice: start check, end race check).
"""
import csv, datetime as dt, json, os, re, shutil, sys, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = 'https://api.themoviedb.org/3'
IMG = 'https://image.tmdb.org/t/p'
POSTER_SIZE = 'w780'   # item posters (Strand tiles on a 4K TV; w342 looked soft - measured ~2.6x sharper, 0 extra requests)
POSTER_RE = re.compile(r'/t/p/w\d+/')   # the size segment of a TMDB image URL; write_pages() rewrites it
HERE = Path(__file__).resolve().parent
TODAY = dt.date.today().isoformat()   # read once: every date window below derives from it (watch.py fingerprints against the same value)
YEAR_AGO = (dt.date.fromisoformat(TODAY) - dt.timedelta(days=365)).isoformat()
MIN_INTERVAL = 1 / 20          # self-throttle: 20 requests/s (TMDB publishes ~40/s)
DROP_LIMIT = 0.30              # abort if total items fall by more than 30%
PARTIAL_TOTAL_MIN_CARDS = 10   # partial rebuild: the total check needs at least this many listed cards (fewer = per-card guards only)
CARD_DROP = 0.70               # a catalog "dropped" if it lost more than 70% of its items (and had >= 20)
CARD_DROP_MAX = 10             # more than this many dropped catalogs = systemic -> abort
REQUEST_CAP = 40000            # runaway guard: abort before deploying anything
CACHE_FILE = Path(os.environ.get('SD_CACHE', 'cache')) / 'stable.json'

WORKERS = int(os.environ.get('SD_WORKERS', '6'))   # catalogs built in parallel; the 20 req/s throttle is shared

HAND_LISTS = ('theme_must_haves', 'corrections', 'studio_distributed', 'awards', 'card_ids', 'deny', 'exclude',
              'official_arrivals', 'precision_unknowns')


HAND_FIELDS = ('type', 'tmdb', 'title', 'year', 'source')


def hand_entry(type=None, tmdb=None, title=None, year=None, source=None, **extra):
    """The one shape of a hand-list entry (issues #72, #83): {"type": "movie"|"series", "tmdb": int, "title", "year",
    "source"} (+ extra fields a list keeps: date, wikidata/qid, why, added, provider)."""
    return {'type': type, 'tmdb': tmdb, 'title': title, 'year': year, 'source': source, **extra}


def as_entry(e):
    """A dict read from a hand-list file -> entry. A missing field stays None (tests/test_hand_lists.py flags it)."""
    return hand_entry(*(e.get(k) for k in HAND_FIELDS), **{k: v for k, v in e.items() if k not in HAND_FIELDS})


def load_hand_list(name, spec=None, path=None):
    """THE loader of every hand-made list (issues #72, #83): -> {group: [entry, ...]} in file order, each entry
    {"type","tmdb","title","year","source"} (+ the extra fields the file keeps, e.g. "date" for official_arrivals).
    Values are NOT coerced (a string id stays a string) so tests/test_hand_lists.py can catch it; readers int() them.
    name: one of HAND_LISTS. group = card slug (theme_must_haves, corrections, studio_distributed, card_ids, exclude), award key
    (awards), 'deny', service (official_arrivals), provider (precision_unknowns). deny/exclude/card_ids read `spec`
    (a loaded spec.json; read from HERE when omitted). `path` overrides the file (accuracy_check's --official/--unknowns)."""
    if name not in HAND_LISTS:
        raise KeyError(name)

    def read(fname):
        return json.loads(Path(path or HERE / fname).read_text(encoding='utf-8'))

    if name in ('theme_must_haves', 'corrections', 'studio_distributed'):
        return {g: [as_entry(e) for e in es] for g, es in read(name + '.json').items()}
    if name == 'awards':
        return {g: [as_entry(e) for e in v['ids']] for g, v in read('awards.json').items()}
    if name == 'precision_unknowns':
        out = {}
        for u in read('precision_unknowns.json'):
            out.setdefault(u['provider'], []).append(as_entry({k: v for k, v in u.items() if k != 'provider'}))
        return out
    if name == 'official_arrivals':
        out = {}
        with open(path or HERE / 'official_arrivals.csv', encoding='utf-8', newline='') as f:
            for r in csv.DictReader(f):
                n, y = r['tmdb'], r['year']
                out.setdefault(r['service'], []).append(
                    hand_entry(r['type'], int(n) if n.isdigit() else n, r['title'], int(y) if y.isdigit() else None,
                               r['source'], date=r['date']))
        return out
    if spec is None:
        spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
    if name == 'deny':                    # spec.json defaults.deny: entries
        return {'deny': [as_entry(e) for e in spec['defaults'].get('deny', [])]}
    key = 'exclude' if name == 'exclude' else 'ids'          # per-card entries: card exclusions / franchise ids
    return {c['slug']: [as_entry(e) for e in c[key]] for c in spec['catalogs'] if c.get(key)}


_last = [0.0]
_backfill = [0]
_rating_backfill = [0]
RATING_BACKFILL_MAX = int(os.environ.get('SD_RATING_BACKFILL_MAX', '3000'))   # per run: cached previews refetched for a rating (same cap as the genre backfill)
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


_keys = {}                     # (medium, tmdb id) -> sort values seen on TMDB's own rows (issue #129: where a correction lands)
_new_keys = {}                 # (card slug, medium, tmdb id) -> "new" card order key (arrival / first_seen date + popularity)


def released_iso(date):
    """TMDB 'YYYY-MM-DD' -> the full ISO date the Stremio meta 'released' field carries (the AIOStreams fork's Jellyfin
    sort parses it with Date().toISOString()); None when TMDB has no full date. Issue #35."""
    return f'{date[:10]}T00:00:00.000Z' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', (date or '')[:10]) else None


# Issue #138: imdbRating only for titles with at least the vote floor of the streaming Top Rated cards (make_spec.py
# VOTES['top'], spec min_votes movie 300 / series 150), so the fork's rating sort is not led by a 9.6 from a handful of votes.
RATING_MIN_VOTES = {'movie': 300, 'series': 150}


def rating_value(media, vote, votes):
    """TMDB vote_average as the Stremio imdbRating string ("7.8"); None when the title has fewer votes than the Top Rated
    floor (or none), so it has no rating (the fork's imdbRatingOf reads a missing field as undefined) and sorts last."""
    return f"{vote:.1f}" if vote and (votes or 0) >= RATING_MIN_VOTES.get(media, 300) else None


def rating_str(item, media):
    return rating_value(media, item.get('vote_average'), item.get('vote_count'))


def preview(item, media):
    title = item.get('title') or item.get('name')
    date = item.get('release_date') or item.get('first_air_date') or ''
    _keys[(media, item['id'])] = {'pop': item.get('popularity') or 0, 'vote': item.get('vote_average') or 0,
                                  'votes': item.get('vote_count') or 0, 'date': date}
    meta = {'id': f"tmdb:{item['id']}", 'type': media, 'name': title}
    if item.get('poster_path'):
        meta['poster'] = f"{IMG}/{POSTER_SIZE}{item['poster_path']}"
    if date[:4]:
        meta['releaseInfo'] = date[:4]
    if released_iso(date):
        meta['released'] = released_iso(date)
    if rating_str(item, media):
        meta['imdbRating'] = rating_str(item, media)
    # private: canonical genres for the genre pages, stripped before anything is written
    meta['_g'] = canon_genres(item.get('genre_ids') or [g['id'] for g in item.get('genres') or []])
    return meta


def released(item, media):
    date = item.get('release_date') if media == 'movie' else item.get('first_air_date')
    return bool(date) and date <= TODAY


def rating_due(entry):
    """A cached preview needs a rating refetch when it never had one fetched ('v' = the fetch date, absent on old entries),
    or when it was released within the last 365 days and its rating is over 30 days old (a new title's score moves)."""
    v = entry.get('v')
    if not isinstance(v, str) or 'vc' not in entry:      # 'vc' = the vote count at the fetch (#138); older entries are refetched
        return True
    day = lambda n: (dt.date.fromisoformat(TODAY) - dt.timedelta(days=n)).isoformat()
    return v < day(30) and (entry.get('d') or '') >= day(365)


def details_preview(media, tid):
    """Preview for one TMDB id (cached durably); None if unknown or not released yet."""
    key = f'{media}:{tid}'
    old = _stable.get(key)
    # cached before genres were kept: refetch once (one-time migration); on failure the old entry is kept
    with _lock:
        stale = bool(old) and '_g' not in old['p'] and _backfill[0] < GENRE_BACKFILL_MAX
        if stale:
            _backfill[0] += 1
        # cached before ratings were kept (#35): fill from a TMDB row seen this run, else refetch up to a per-run cap
        if old:
            seen = _keys.get((media, tid))
            if seen:                  # this run's TMDB row is current: it always wins over the cached rating
                r = rating_value(media, seen['vote'], seen['votes'])
                if r:
                    old['p']['imdbRating'] = r
                else:
                    old['p'].pop('imdbRating', None)
                old['v'], old['vc'] = TODAY, seen['votes']
            elif not stale and _rating_backfill[0] < RATING_BACKFILL_MAX and rating_due(old):
                _rating_backfill[0] += 1
                stale = 'rating'
    if key not in _stable or stale:
        try:
            d = tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}")
            _stable[key] = ({'p': preview(d, media), 'd': d.get('release_date') or d.get('first_air_date') or '', 'v': TODAY,
                             'vc': d.get('vote_count') or 0}
                            if d.get('id') else None)
        except RuntimeError:          # a transient failure must not stick
            if not stale:
                return None           # not cached: try again next run
            _stable[key] = old        # keep the pre-genre entry (no genre pages for it this run)
    hit = _stable[key]
    if hit and 'vc' not in hit:           # vote count unknown (cached before #138, not refetched yet): no rating beats a low-vote one
        hit['p'].pop('imdbRating', None)
    if hit and hit['d'] and 'released' not in hit['p'] and released_iso(hit['d']):
        hit['p']['released'] = released_iso(hit['d'])      # cached before #35: filled from the cached date, no refetch
    return hit['p'] if hit and hit['d'] and hit['d'] <= TODAY else None


def studio_distributed(card):
    """Issue #66: TMDB ids of the films a studio card's company distributed (generator/studio_distributed.json, made by
    studio_distributed.py from Wikidata P750/P4947; never fetched at build time). [] for any non-studio card."""
    if 'studios' not in card.get('folders', []):
        return []
    try:
        lists = load_hand_list('studio_distributed')
    except OSError:
        return []
    return [int(e['tmdb']) for e in lists.get(card.get('slug'), [])]


def add_studio_distributed(card, metas):
    """A studio card also holds the films its studio distributed. The data file's list (most-linked on Wikidata first)
    is merged 1:1 with the popularity-ordered discover list by interleave(), which drops repeats, so well-known
    distributed films sit near the top. A distributed film already in the discover list is taken from it; the rest are
    fetched with details_preview (cached; None = not released / unknown, skipped). Not cut by depth: the file caps it."""
    ids = studio_distributed(card)
    if not ids:
        return metas
    have = {m['id']: m for m in metas}
    dist = []
    for tid in ids:
        m = have.get(f'tmdb:{tid}') or details_preview('movie', tid)
        if m:
            dist.append(m)
    return interleave([metas, dist])


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


def fill_reverse(path, params, raw, total, pages, pool=None):
    """Issue #25: read a /discover pool once more in the REVERSE sort order (separate cache entries) and add every id
    the forward pages hid to `raw` ({id: item}, updated in place). Stops once `raw` holds `total` ids; bounded by
    `pages`. With `pool` (an executor) pages are read in parallel batches of its size. -> (pages read, ids added)."""
    field, _, way = params['sort_by'].rpartition('.')
    fill = {**params, 'sort_by': f"{field}.{'asc' if way == 'desc' else 'desc'}"}
    n0, used, step = len(raw), 0, (pool._max_workers if pool else 1)
    for start in range(1, pages + 1, step):
        batch = range(start, min(start + step, pages + 1))
        fetch = (lambda pg: tmdb(path, page=pg, **fill))
        for data in (pool.map(fetch, batch) if pool else map(fetch, batch)):
            used += 1
            for item in data.get('results', []):
                raw.setdefault(item['id'], item)
        if len(raw) >= total:
            break
    return used, len(raw) - n0


MAIN_GENRE_PAGES = 200         # genre cards read up to this many pages per medium (a main-genre filter needs more pages)


# Issue #52: a genre card keeps a title when the card's genre is among its first K tags (TMDB lists a title's main
# genre first). K was picked per genre from TMDB's real tag order in the build's pools: most films carry their real
# genre 2nd or 3rd (Schindler's List is [Drama, History, War]; Lord of the Rings [Adventure, Fantasy, Action]).
GENRE_K_DEFAULT = 3
GENRE_K = {16: 99,             # Animation: any position (Coco, KPop Demon Hunters list it 2nd/4th; it is a format, not a topic)
           27: 2,              # Horror: first two (Come and See [Drama, War, Horror] is not horror), see GENRE_NOT_AFTER
           28: 2, 10759: 2}    # Action (and TV Action & Adventure): first two (Regular Show lists it 3rd)


# A genre that does not count when it is not the first tag and the first tag is one of these (Ghosts [Music, Horror]
# is a music video; Coraline-style Animation and Documentary are formats). Used only by Horror.
GENRE_NOT_AFTER = {27: {10402, 99, 16}}


def genre_fits(genre_ids, card_ids):
    """True when one of the card's genre ids is within the first K tags of the title (K by genre, see GENRE_K);
    a card with several ids uses the largest K. A hit past the first tag is ignored when the first tag is in
    GENRE_NOT_AFTER for that genre."""
    k = max(GENRE_K.get(i, GENRE_K_DEFAULT) for i in card_ids)
    tags = list(genre_ids or [])
    for pos, t in enumerate(tags[:k]):
        if t in card_ids and (pos == 0 or tags[0] not in GENRE_NOT_AFTER.get(t, ())):
            return True
    return False


def main_genres(card, media):
    """Issue #52: a genre card (folder genres-*) counts a title only when the card's genre is among its first tags (genre_fits).
    -> set of the card's genre ids for this medium (with_genres may list several, '|' or ','), or None when the
    filter does not apply (not a genre card, or no with_genres: Anime)."""
    if not any(f.startswith('genres-') for f in card.get('folders', [])):
        return None
    ids = re.split(r'[|,]', str(card.get(media, {}).get('with_genres', '')))
    return {int(i) for i in ids if i.strip().isdigit()} or None


def not_short(item):
    """Issue #118: the 40-minute floor of a Top / Most Voted movie card, checked on the title's own /movie runtime.
    /discover with_runtime.gte=40 still returned Ghosts (1997), whose /movie/37793 says 39 min. Runtime unknown (0) or a
    TMDB error keeps the title (as the director and award cards do; an error is not cached). Durable cache runtime:{id}."""
    try:
        rt = stable(f"runtime:{item['id']}", lambda: tmdb(f"/movie/{item['id']}").get('runtime') or 0)
    except RuntimeError:
        return True
    if rt == 0 or rt >= MIN_RUNTIME:
        return True
    print(f"  runtime floor: {item.get('title')} ({(item.get('release_date') or '')[:4]}, tmdb:{item['id']}, {rt} min) left off", flush=True)
    return False


def discover(card, media, depth, dflt):
    movie = media == 'movie'
    sort = SORTS[card['sort']][0 if movie else 1]
    votes = card.get('min_votes', 10)
    if isinstance(votes, dict):
        votes = votes[media]
    params = {'sort_by': sort, 'include_adult': 'false', 'vote_count.gte': votes, **card.get(media, {})}
    cutoff = TODAY
    if card.get('min_age_days'):              # e.g. hidden gems: at least a year old
        cutoff = (dt.date.fromisoformat(TODAY) - dt.timedelta(days=card['min_age_days'])).isoformat()
    if not movie and card.get('tv_air_window'):   # any episode aired recently (new seasons of old shows count)
        params.pop('first_air_date.lte', None)
        params.update({'air_date.gte': (dt.date.fromisoformat(TODAY) - dt.timedelta(days=card['tv_air_window'])).isoformat(),
                       'air_date.lte': TODAY})      # the window picks the pool; the card's own sort orders it (issue #45)
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
    mains = main_genres(card, media)
    ok = (lambda it: genre_fits(it.get('genre_ids'), mains)) if mains else (lambda it: True)
    if movie and card['sort'] in ('top', 'votes'):   # issue #118: TMDB's discover runtime filter lets some shorts through
        genre_ok = ok
        ok = lambda it: genre_ok(it) and not_short(it)
    last = MAIN_GENRE_PAGES if mains else 500
    out, seen, page, raw = [], set(), 1, {}
    while len(out) < depth:
        data = tmdb(path, page=page, **params)
        for item in data.get('results', []):
            if item['id'] not in seen:
                seen.add(item['id'])
                raw[item['id']] = item
                if ok(item):
                    out.append(preview(item, media))
        if page >= min(data.get('total_pages', 0), last):
            break
        page += 1
    # TMDB caches every /discover page separately (for hours), so the pages of one query can come from different
    # ranking snapshots: ids repeat on two pages and others are never returned (issue #25). When the whole pool was
    # read but ids are missing, read it once more in the REVERSE order (different cache entries) and merge by the
    # sort key. Bounded by total_pages; only runs on a short card with holes.
    total, pages = data.get('total_results', 0), data.get('total_pages', 0)
    if len(out) < depth and len(seen) < total and 0 < pages <= last:
        sort_by = params['sort_by']
        field, _, way = sort_by.rpartition('.')
        field = {'primary_release_date': 'release_date'}.get(field, field)
        used, got = fill_reverse(path, params, raw, total, pages)
        with _lock:
            _fill[0] += used
            _fill[1] += got
        print(f"  discover fill {card.get('slug')} {media}: +{got} ids ({used} pages)", flush=True)
        # one order from the current values (the stale pages' order was the problem); ties keep first-read order
        order = {i: n for n, i in enumerate(raw)}
        have = [i for i in raw if raw[i].get(field) not in (None, '')]
        have.sort(key=lambda i: (raw[i][field], -order[i]) if way == 'desc' else (raw[i][field], order[i]),
                  reverse=way == 'desc')
        rest = [i for i in raw if raw[i].get(field) in (None, '')]
        out = [preview(raw[i], media) for i in have + rest if ok(raw[i])]
    return out[:depth]


def must_haves(card, media):
    """Hand-picked must-have titles of a theme card (generator/theme_must_haves.json, issue #64): [tmdb id, ...]."""
    try:
        lists = load_hand_list('theme_must_haves')
    except OSError:
        return []
    return [int(e['tmdb']) for e in lists.get(card.get('slug'), []) if e['type'] == media]


def add_must_haves(card, media, metas):
    """Issue #64: a theme card always holds its hand-picked titles. Each is taken from the discover list when it is
    already there, else fetched with details_preview (one cached request each; None = not released / unknown, skipped).
    Previews carry no popularity, so the must-haves go FIRST, in list order, then the discover order. Never cut by depth."""
    ids = must_haves(card, media)
    if not ids:
        return metas
    have = {m['id']: m for m in metas}
    first = []
    for tid in ids:
        m = have.get(f'tmdb:{tid}') or details_preview(media, tid)
        if m and m not in first:
            first.append(m)
    return first + [m for m in metas if m not in first]


def corrections_of(card):
    """Issue #117/#129: every entry of OUR corrections list for this card (any side), in file order."""
    try:
        return load_hand_list('corrections').get(card.get('slug'), [])
    except OSError:
        return []


def corrections(card, media):
    """Issue #117: the tmdb ids the list adds to this card for one medium (side add): titles TMDB lacks the keyword,
    credit or grouping for but two sources (Wikidata and the English Wikipedia) file under it."""
    return [int(e['tmdb']) for e in corrections_of(card) if e['type'] == media and e.get('side') == 'add']


def _split(rule):
    return [x for x in re.split(r'[|,]', str(rule or '')) if x.strip()]


def _blocked_by_keywords(rule, kw_ids):
    """TMDB without_keywords syntax: '|' = any of, ',' = all of (a part between '|' may hold ',' groups)."""
    return any(all(x in kw_ids for x in part.split(',')) for part in str(rule or '').split('|') if part.strip())


def correction_details(card, media, tid, fetch=None):
    """THE TMDB fetch of a correction add (the build and the accuracy check both use it, so they judge the same data):
    details + what correction_rule reads (release types, keywords, and watch providers on a service card).
    fetch: the caller's TMDB getter (path, **params) -> dict; default this module's tmdb()."""
    return (fetch or tmdb)(f"/{'movie' if media == 'movie' else 'tv'}/{tid}",
                append_to_response=('release_dates,keywords' if media == 'movie' else 'keywords')
                + (',watch/providers' if card.get(media, {}).get('with_watch_providers') else ''))


def correction_rule(card, dflt, media, d):
    """Issue #129: the first of the card's OWN rules a hand-added title fails, from its TMDB details (+ release_dates and
    keywords); None = it fits. An add only fixes the source's missing tag/credit/grouping, never the card's other rules.
    Every card: released, not adult, not on the deny / card exclusion lists. A discover card that is not in a New mode
    also: min_age_days, min_votes, movie release type 4|5|6 and the runtime floor, series without_genres, the defaults'
    without_keywords (+ the card's own), the main-genre rule of a genre card, and every other discover() filter the card
    uses: vote_average.gte, vote_count.lte, with_original_language, the release / first-air date window, series status
    (with_status 0|3|4|5), the tv_air_window. A card with with_watch_providers also needs the title on that service in
    its region (and monetization types), New modes included (TMDB watch/providers, appended to the same request)."""
    tid = d['id']
    date = d.get('release_date') or d.get('first_air_date') or ''
    if not date or date > TODAY:
        return 'not released'
    if d.get('adult'):
        return 'adult'
    dflt = dflt or {}
    if (media, tid) in {(e['type'], int(e['tmdb'])) for e in dflt.get('deny', [])}:
        return 'deny list'
    if (media, tid) in {(e['type'], int(e['tmdb'])) for e in card.get('exclude', [])}:
        return 'card exclusion list'
    if card['kind'] != 'discover':
        return None
    own = card.get(media, {})
    movie = media == 'movie'
    if own.get('with_watch_providers'):     # a streaming card: on the card's service, in its region; no provider data = skip (fail closed)
        if not d.get('watch/providers'):
            return 'no watch/providers data'
        res = d['watch/providers'].get('results', {}).get(own.get('watch_region') or 'US', {})
        kinds = _split(own.get('with_watch_monetization_types')) or ['flatrate', 'free', 'ads', 'rent', 'buy']
        if not {int(x['provider_id']) for k in kinds for x in res.get(k, [])} & {int(x) for x in _split(own['with_watch_providers'])}:
            return 'not on the card\'s service'
    if arrival_mode(card) or first_seen_mode(card):
        return None
    if card.get('min_age_days') and date > (dt.date.fromisoformat(TODAY) - dt.timedelta(days=card['min_age_days'])).isoformat():
        return 'min_age_days'
    votes = card.get('min_votes', 10)
    if isinstance(votes, dict):
        votes = votes[media]
    if (d.get('vote_count') or 0) < int(votes):
        return 'min_votes'
    if (d.get('vote_average') or 0) < float(own.get('vote_average.gte') or 0):
        return 'vote_average'
    if 'vote_count.lte' in own and (d.get('vote_count') or 0) > int(own['vote_count.lte']):
        return 'vote_count.lte'
    if own.get('with_original_language') and d.get('original_language') not in _split(own['with_original_language']):
        return 'language'
    lo, hi = (own.get('primary_release_date.gte'), own.get('primary_release_date.lte')) if movie else \
             (own.get('first_air_date.gte'), own.get('first_air_date.lte'))
    if (lo and date < lo) or (hi and date > hi):
        return 'date window'
    if not movie:
        if d.get('status') in ('Planned', 'In Production'):                # with_status 0|3|4|5: returning, ended, cancelled, pilot
            return 'status'
        if card.get('tv_air_window') and (d.get('last_air_date') or '') < \
                (dt.date.fromisoformat(TODAY) - dt.timedelta(days=card['tv_air_window'])).isoformat():
            return 'air window'                                            # no episode aired in the last tv_air_window days
    gids = [g['id'] if isinstance(g, dict) else g for g in d.get('genres') or d.get('genre_ids') or []]
    if movie:
        types = {r.get('type') for c in (d.get('release_dates') or {}).get('results', []) for r in c.get('release_dates', [])}
        if 'release_dates' in d and not {4, 5, 6} & types:
            return 'release type 4|5|6'
        floor = own.get('with_runtime.gte') or (MIN_RUNTIME if card['sort'] in ('top', 'votes') else 0)
        if floor and 0 < (d.get('runtime') or 0) < int(floor):
            return 'runtime'
    elif set(gids) & {int(x) for x in _split(own.get('without_genres', '10767|10763'))}:
        return 'without_genres'
    if dflt.get('without_keywords'):
        excl = dflt['without_keywords'] + ('' if movie or not dflt.get('without_keywords_tv') else '|' + dflt['without_keywords_tv'])
        rule = f"{own['without_keywords']}|{excl}" if own.get('without_keywords') else excl
        kws = (d.get('keywords') or {}).get('keywords' if movie else 'results', [])
        if _blocked_by_keywords(rule, {str(k['id']) for k in kws}):
            return 'without_keywords'
    mains = main_genres(card, media)
    if mains and not genre_fits(gids, mains):
        return 'main genre'
    return None


def placement(card):
    """How this card orders its list -> (key of a meta, highest first?, name) or None (= appended). Reuses the key each
    kind sorts by today: popular -> popularity; top -> rating; votes (Most Voted, director, actor) -> vote count;
    new / release_asc / franchise -> release date; arrival and first_seen New cards -> recent_first_key of the arrival
    date (the entry's "date"); mdblist year_desc -> release date. Idlist and plain mdblist keep list order: appended."""
    def val(field):
        return lambda m: (_keys.get((m['type'], int(m['id'].split(':', 1)[1]))) or {}).get(field) or None

    def date_of(m):
        k = int(m['id'].split(':', 1)[1])
        return (_keys.get((m['type'], k)) or {}).get('date') or (_stable.get(f"{m['type']}:{k}") or {}).get('d') \
            or m.get('releaseInfo') or None

    kind = card['kind']
    if kind == 'discover':
        if 'studios' not in card.get('folders', []) and (arrival_mode(card) or first_seen_mode(card)):
            return (lambda m: _new_keys.get((card['slug'], m['type'], int(m['id'].split(':', 1)[1])))), True, 'arrival'
        return {'popular': (val('pop'), True), 'top': (val('vote'), True), 'votes': (val('votes'), True),
                'new': (date_of, True), 'release_asc': (date_of, False)}[card['sort']] + (card['sort'],)
    if kind == 'franchise':
        return date_of, False, 'release date'
    if kind in ('director', 'actor'):
        return val('votes'), True, 'vote count'
    if kind == 'mdblist' and card.get('order') == 'year_desc':
        return date_of, True, 'release date'
    return None


def _insert(metas, new, key, desc):
    """Put `new` among the same-medium entries by key: right after the last one that ranks at or above it (before the
    first keyed one when it outranks them all; appended when none has a key). Entries without a key are skipped."""
    k_new = key(new)
    keyed = [(i, key(m)) for i, m in enumerate(metas) if m['type'] == new['type'] and key(m) is not None]
    if k_new is None or not keyed:
        metas.append(new)
        return
    above = [i for i, k in keyed if (k >= k_new if desc else k <= k_new)]
    metas.insert(above[-1] + 1 if above else keyed[0][0], new)


def apply_corrections(card, metas, depth, dflt=None):
    """Issue #129 (part of #117): OUR OWN corrections list (generator/corrections.json), one step at the end of build_one
    for every card kind. side remove: the title leaves the card. side add: fetched like any title, not duplicated,
    checked against the card's own other rules (correction_rule; a failing add is skipped and named), and PLACED BY THE
    CARD'S OWN ORDER (placement()). The card keeps its depth: the tail of the list makes room, never the must-haves nor
    the additions (a kind with no depth is not cut). side keep: no effect here. -> (metas, [skip notes])."""
    entries = corrections_of(card)
    metas, skipped = list(metas), []
    if not entries:
        return metas, skipped
    holds = card.get('media') or ('movie', 'series')
    gone = {(e['type'], f"tmdb:{int(e['tmdb'])}") for e in entries if e.get('side') == 'remove'}
    metas = [m for m in metas if (m['type'], m['id']) not in gone]
    order = placement(card)
    have = {(m['type'], m['id']) for m in metas}
    added = []
    for e in (e for e in entries if e.get('side') == 'add'):
        media, tid = e['type'], int(e['tmdb'])
        if (media, f'tmdb:{tid}') in have:
            continue
        d, why = None, None
        if media not in holds:
            why = f'card holds no {media}'
        elif order and order[2] == 'arrival' and not e.get('date'):
            why = 'no date for a New card'
        else:
            try:
                d = correction_details(card, media, tid)
                why = correction_rule(card, dflt, media, d) if d.get('id') else 'unknown to TMDB'
            except RuntimeError:
                why = 'TMDB lookup failed'
        if why:
            skipped.append(f"correction skipped: {e.get('title') or tid} ({why})")
            continue
        m = preview(d, media)
        twin = next((o for o in metas if o['type'] != media and (o.get('name') or '').casefold() == (m.get('name') or '').casefold()
                     and o.get('releaseInfo') == m.get('releaseInfo')), None)
        if twin:           # a TV film that Wikidata also lists as a movie: the shelf must not show one film twice
            skipped.append(f"correction skipped: {e.get('title') or tid} (same title and year already on the card as a {twin['type']})")
            continue
        have.add((media, m['id']))
        added.append(m['id'])
        if order and order[2] == 'arrival':
            _new_keys[(card['slug'], media, tid)] = recent_first_key(e['date'], d.get('popularity') or 0, 0)
        if order:
            _insert(metas, m, order[0], order[1])
        else:
            metas.append(m)
    if depth and added:
        cap = depth * len(card.get('media') or ('movie',))
        listed = {f"tmdb:{int(e['tmdb'])}" for e in entries if e.get('side') == 'add'}    # corrections already in the sorted list too
        pinned = set(added) | listed | {f'tmdb:{i}' for media in holds for i in must_haves(card, media)}
        over = len(metas) - cap
        if 'studios' in card.get('folders', []):      # a studio card is longer than its depth by design (distributed films): only make room for the additions
            over = len(added) - max(0, cap - (len(metas) - len(added)))
        for n in range(len(metas) - 1, -1, -1):
            if over <= 0:
                break
            if metas[n]['id'] not in pinned:
                del metas[n]
                over -= 1
    return metas, skipped


def franchise(card):
    """Collections (+ explicit TMDB ids for titles TMDB doesn't group), each medium in release order."""
    parts, dates = [], {}                       # dates: preview id -> full release date, for same-year ties
    for cid in card['collection_ids']:
        parts += [p for p in named(f'/collection/{cid}', None).get('parts', []) if released(p, 'movie')]
    movies = [preview(p, 'movie') for p in sorted(parts, key=lambda p: p['release_date'])]
    dates.update({f"tmdb:{p['id']}": p['release_date'] for p in parts})
    extra = {'movie': [], 'series': []}
    for e in card.get('ids', []):
        media, tid = e['type'], e['tmdb']
        p = details_preview(media, tid)
        if p:
            extra[media].append(p)
            dates.setdefault(p['id'], (_stable.get(f'{media}:{tid}') or {}).get('d') or '')
    seen = {m['id'] for m in movies}
    movies += [p for p in extra['movie'] if p['id'] not in seen]
    # full date first (a spin-off released in February comes before a May film of the same year), then year, then last
    by_date = lambda p: dates.get(p['id']) or p.get('releaseInfo', '9999')
    return [sorted(movies, key=by_date), sorted(extra['series'], key=by_date)]


MIN_RUNTIME = 40      # Top / Most Voted movie cards only (not_short); director cards keep shorts (issue #30)


def director(card, dropped):
    """Movies they directed (shorts included, issue #30, best-known first), then the TV series they directed episodes of
    (TMDB tv_credits, crew job Director; one episode counts; talk/news/reality shows and TV movies already listed
    as movies are left out), best-known first."""
    pid = card['person_id']
    crew = tmdb(f"/person/{pid}/movie_credits").get('crew', [])
    films = {c['id']: c for c in crew if c.get('job') in ('Director', 'Co-Director') and released(c, 'movie')}
    # best-known first (vote count): release order put restorations/compilations on top (Wyler, Leone - report.csv)
    kept = [preview(c, 'movie')
            for c in sorted(films.values(), key=lambda c: (-(c.get('vote_count') or 0), c['release_date']))]
    # TV movies come back from tv_credits under a different (tv) id: match them to the movie list by title + year
    film_keys = {((c.get('title') or '').casefold(), c['release_date'][:4]) for c in films.values()}
    shows = {}
    for c in tmdb(f"/person/{pid}/tv_credits").get('crew', []):
        if (c.get('job') != 'Director' or not released(c, 'series') or c['id'] in shows
                or set(c.get('genre_ids') or []) & NOT_ACTING
                or ((c.get('name') or '').casefold(), c['first_air_date'][:4]) in film_keys):
            continue
        shows[c['id']] = c
    kept += [preview(c, 'series')
             for c in sorted(shows.values(), key=lambda c: (-(c.get('vote_count') or 0), c['first_air_date']))]
    return kept


SELF = re.compile(r'\b(self|himself|herself|themselves|themself|cameo)\b|\(uncredited\)|\barchive\b', re.I)
VOICE = re.compile(r'\(voice\)', re.I)
LEAD_ORDER, LEAD_VOTES = 2, 1000       # a voice role this high up in a well-known film is a lead, not a bit
NOT_ACTING = {10767, 10763, 10764}     # Talk, News, Reality
MIN_EPISODES = 5                       # TV: a real role, not a guest arc
MAX_BILLING = 15                       # movies: cast order (0 = top billed)


def actor(card, dropped):
    """Acting credits, movies + TV, best-known first (vote count). Drops talk/news/reality, self/cameo/archive/
    uncredited appearances, TV roles under 5 episodes, movie roles billed below #15, unreleased titles, and
    near-unknown titles older than a year (< 10 votes). Voice roles ('(voice)') are dropped too, unless the film is
    well known (>= 1000 votes) and the role is a lead (cast order <= 2), or more than half of the credits left are
    voice roles (a voice actor's card keeps them all). Live credits are deduped first, so a dropped voice credit
    never blocks a live-action credit of the same title. Popularity order put small recurring
    TV roles first (report.csv spot check)."""
    cast = tmdb(f"/person/{card['person_id']}/combined_credits").get('cast', [])
    ranked, voices, seen = [], [], set()
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
        votes = c.get('vote_count') or 0
        if VOICE.search(c.get('character') or ''):
            lead = c.get('order') is not None and c['order'] <= LEAD_ORDER and votes >= LEAD_VOTES
            voices.append((votes, preview(c, media), (media, c['id']), lead))
        elif (media, c['id']) not in seen:
            seen.add((media, c['id']))
            ranked.append((votes, preview(c, media)))
    voice_actor = len(voices) * 2 > len(ranked) + len(voices)
    for votes, p, key, lead in voices:         # after the live credits, so a live role of the same title wins
        if (voice_actor or lead) and key not in seen:
            seen.add(key)
            ranked.append((votes, p))
        elif not (voice_actor or lead):
            dropped['voice'] = dropped.get('voice', 0) + 1
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
    """[(media, id), ...] of an idlist card: its awards.json list (read at build time, so the yearly award refresh
    only has to update awards.json), or ids written into the spec."""
    entries = load_hand_list('awards')[card['awards_key']] if 'awards_key' in card else card['ids']
    return [(e['type'], e['tmdb']) for e in entries]


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
            parts.append(f"{tag}: network " + ' | '.join(named(f'/network/{n}') for n in p['with_networks'].split('|')))
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
    # Poster size is normalised here, at emit time, in ONE place: cached previews (_stable, durable) and stored
    # first-seen metas keep the size they were fetched with, so rewriting at the source alone never reaches them.
    # Copies only: the caller's metas (and the cache objects behind them) are never mutated. 0 extra TMDB requests.
    metas = [{**m, 'poster': POSTER_RE.sub(f'/t/p/{POSTER_SIZE}/', m['poster'])} if m.get('poster') else dict(m)
             for m in metas]
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


SITE_URL_DEFAULT = 'https://xbobxsagetx.github.io/strand-discovery/'


def site_url():
    """Base URL (trailing slash) of the live site a partial rebuild starts from."""
    u = os.environ.get('SD_SITE_URL') or ''
    if not u:
        prev = os.environ.get('PREV_SUMMARY_URL') or ''
        u = prev.rsplit('/', 1)[0] if prev else SITE_URL_DEFAULT
    return u if u.endswith('/') else u + '/'


def fetch_bytes(url, attempts=8):
    """GET `url` -> bytes. 404 -> FileNotFoundError. 429 (GitHub Pages rate limit, seen at ~150 requests/s) and other
    failures back off (Retry-After when sent), then RuntimeError (class/status only)."""
    for a in range(attempts):
        wait = min(2 ** (a + 1), 30)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise FileNotFoundError(url) from None
            err = f'HTTP {e.code}'
            if e.code == 429 and (e.headers.get('Retry-After') or '').isdigit():
                wait = max(wait, min(int(e.headers['Retry-After']), 60))
        except Exception as e:
            err = type(e).__name__
        if a == attempts - 1:
            raise RuntimeError(f'{err} on {url}') from None
        time.sleep(wait)


# ---- partial rebuild (issue #77) --------------------------------------------------------------------------------
def parse_cards(raw, catalogs):
    """SD_CARDS -> set of spec slugs, or None for a full build. Accepts slugs with or without the "sd-" catalog-id
    prefix, or the path of a file of them. An unknown slug fails the run (a typo must not silently build nothing)."""
    raw = (raw or '').strip()
    if not raw:
        return None
    if os.path.isfile(raw):
        raw = Path(raw).read_text(encoding='utf-8')
    slugs = {c['slug'] for c in catalogs}
    want, bad = set(), []
    for t in re.split(r'[,\s]+', raw):
        if not t:
            continue
        t = t if t in slugs else t[3:] if t.startswith('sd-') and t[3:] in slugs else t
        if t in slugs:
            want.add(t)
        else:
            bad.append(t)
    if bad or not want:
        sys.exit(f'GUARD: SD_CARDS names no known card: {bad or raw!r}')
    return want


def partial_guards(prev_counts, new_counts):
    """The safety checks of a partial rebuild, on the LISTED cards only (prev_counts = their counts on the live site;
    a card absent there is new and has nothing to compare). Unlike the full build, a card that lost >70% BLOCKS.
    The total check runs only with PARTIAL_TOTAL_MIN_CARDS (10) or more listed cards; fewer rely on the per-card
    checks, so one shelf may legitimately shrink 30-70%. It compares the listed cards with the same cards live (like
    with like): the unlisted ones are byte-identical by construction, so comparing whole-site totals would hide a real drop."""
    empty = sorted(c for c, n in new_counts.items() if n == 0)
    if empty:
        sys.exit(f'GUARD: empty catalogs: {empty}')
    dropped = [f'{c} {prev_counts[c]}->{n}' for c, n in new_counts.items()
               if prev_counts.get(c, 0) >= 20 and n < prev_counts[c] * (1 - CARD_DROP)]
    if dropped:
        sys.exit(f'GUARD: listed catalogs lost >70% of their items: {dropped}')
    old_total = sum(prev_counts[c] for c in new_counts if c in prev_counts)
    new_total = sum(n for c, n in new_counts.items() if c in prev_counts)
    if len(new_counts) >= PARTIAL_TOTAL_MIN_CARDS and old_total and new_total < old_total * (1 - DROP_LIMIT):
        sys.exit(f'GUARD: total items of the listed catalogs dropped {old_total} -> {new_total} (>30%)')


def load_base(base_dir, base_url):
    """The cached copy of the last deployed site (SD_BASE_DIR: restored from the Actions cache `sd-site-*` by the
    workflow) - its manifest.json and summary.json. It is the start of a partial rebuild only if the live site's
    summary.json (ONE request) says the same: same generated_at and same counts, i.e. the cache is what is live now.
    Anything else stops the run: a partial build on a stale base would deploy old shelves over new ones."""
    if not base_dir or not (Path(base_dir) / 'summary.json').is_file():
        sys.exit('GUARD: no cached copy of the live site (SD_BASE_DIR); run a full build instead')
    d = Path(base_dir)
    try:
        manifest, summary = (json.loads((d / n).read_text(encoding='utf-8')) for n in ('manifest.json', 'summary.json'))
        live = json.loads(fetch_bytes(base_url + 'summary.json'))
    except (OSError, RuntimeError, ValueError) as e:
        sys.exit(f'GUARD: cannot read the cached or live site summary ({e.__class__.__name__}); run a full build instead')
    if live.get('generated_at') != summary.get('generated_at') or live.get('catalogs') != summary.get('catalogs'):
        sys.exit(f"GUARD: the cached site ({summary.get('generated_at')}) is not the live one "
                 f"({live.get('generated_at')}); run a full build instead")
    gp = summary.get('genre_pages') or {}
    if (gp.get('files'), gp.get('bytes')) != tree_genre_stats(d):
        sys.exit('GUARD: the cached site is damaged (genre pages differ from its summary); run a full build instead')
    return manifest, summary


def restore_site(root, base_dir, skip):
    """Copy every catalog file of the cached site NOT belonging to a card in `skip` into root, byte for byte
    (<cid>.json and the <cid>/ folder, per-genre pages included). -> number of files written."""
    n = 0
    for media in (Path(base_dir) / 'catalog').iterdir():
        for e in media.iterdir():
            if (e.stem if e.is_file() else e.name) in skip:
                continue
            dest = root / 'catalog' / media.name / e.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if e.is_dir():
                shutil.copytree(e, dest)
                n += sum(1 for f in e.rglob('*') if f.is_file())
            else:
                shutil.copy2(e, dest)
                n += 1
    return n


def fetch_summary_stamp(base_url):
    """generated_at of the live summary.json right now (None if unreadable): a partial build compares it with the one
    it started from, so a deploy that landed in between is caught instead of overwritten with stale shelves."""
    try:
        return json.loads(fetch_bytes(base_url + 'summary.json')).get('generated_at')
    except (OSError, RuntimeError, ValueError):
        return None


def merge_report(base_dir, rows, cids):
    """report.csv of a partial rebuild: the cached rows of the unlisted cards + the new rows of the listed ones, in spec
    order (cids). Cached report missing/unreadable -> only the listed cards' rows (the file is informational)."""
    live = {}
    try:
        lines = list(csv.reader((Path(base_dir) / 'report.csv').read_text(encoding='utf-8').splitlines(True)))
        live = {r[7]: r for r in lines[1:] if len(r) > 7}
    except (OSError, ValueError):
        print('WARNING: cached report.csv unreadable; the report holds only the rebuilt cards')
    new = {r[7]: r for r in rows}
    return [new.get(c) or live[c] for c in cids if c in new or c in live]


def tree_genre_stats(root):
    """(files, bytes) of the genre pages under root/catalog - what write_catalog counts in a full build."""
    n = size = 0
    for p in (root / 'catalog').rglob('*.json'):
        if 'genre=' in p.name:
            n += 1
            size += p.stat().st_size
    return n, size


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


RECENT_DAYS = 30               # on a New card, arrivals this recent are ordered by popularity (well-known first); older ones stay by date


def recent_first_key(date, popularity, rank=0):
    """Sort key (use with reverse=True): arrivals dated within RECENT_DAYS first, most popular first; then the older
    ones newest first (rank, then popularity, break date ties). Issue #49."""
    if date >= (dt.date.fromisoformat(TODAY) - dt.timedelta(days=RECENT_DAYS)).isoformat():
        return (1, '', popularity, date, rank)
    return (0, date, rank, popularity, '')


def first_seen_mode(card):
    prov = card.get('provider')
    info = (FS_SUMMARY or {}).get('providers', {}).get(prov) if prov else None
    # sliced providers (catalogue > 10,000, read by date slices) drift: library titles log as "first seen" and crowd out
    # the real dated arrivals (Prime New, 2026-10-15: 2 arrivals + 998 drift). They use dated signals, then release date.
    return bool(info and info.get('mode') == 'first_seen' and not info.get('sliced') and bool(FS_CANDIDATES.get(prov)))


def first_seen_card(card, depth):
    """New on X = titles first seen on the service within the window: the last RECENT_DAYS most popular first, then
    older ones newest first; no vote floor (arrivals are often obscure); movies and TV interleaved."""
    c = sorted(FS_CANDIDATES[card['provider']], key=lambda m: recent_first_key(m['first_seen'], m['popularity']), reverse=True)
    strip = lambda m: {k: v for k, v in m.items() if k not in ('first_seen', 'popularity')}
    for m in c:                                  # issue #129: the order key a hand-added title is placed by
        _new_keys[(card['slug'], m['type'], int(m['id'].split(':', 1)[1]))] = recent_first_key(m['first_seen'], m['popularity'])
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
    45 days) merged with the titles our own daily snapshots first saw (when that history is on): the last RECENT_DAYS
    most popular first, then older ones newest first; then filled up with the release-date card. Returns (metas, counts)."""
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
    timeline.sort(key=lambda t: recent_first_key(t[0], t[2], t[1]), reverse=True)
    cap = depth * len(card['media'])
    out, seen, n_sig = [], set(), 0
    for date, rank, pop, k, meta in timeline:
        if len(out) >= cap:
            break
        if meta is None:
            media, tid = k.split(':')
            meta = details_preview(media, int(tid))          # durably cached; None = unknown / not released
            n_sig += meta is not None
        if meta and meta['id'] not in seen and meta['type'] in card['media']:
            seen.add(meta['id'])
            _new_keys[(card['slug'], meta['type'], int(meta['id'].split(':', 1)[1]))] = recent_first_key(date, pop, rank)
            out.append(meta)
    n_arr = len(out)
    fill = interleave([discover(card, m, depth, dflt) for m in card['media']])
    out += [m for m in fill if m['id'] not in seen][:max(0, cap - len(out))]
    return both_kinds_early(out), {'signals': n_sig, 'arrivals': n_arr, 'filled': len(out) - n_arr}


def build_one(card, dflt):
    """Returns (metas, notes). The card is built by its kind, then OUR corrections list is applied to it (issue #129)."""
    metas, notes = build_kind(card, dflt)
    depth = depth_of(card, dflt) if 'sort' in card or card.get('depth') else None    # franchise / people / id lists have none
    metas, skipped = apply_corrections(card, metas, depth, dflt)
    return metas, '; '.join(x for x in [notes] + skipped if x)


def build_kind(card, dflt):
    kind, notes = card['kind'], ''
    if kind == 'discover' and 'studios' in card.get('folders', []):      # issue #66: + the films the studio distributed
        return interleave([add_studio_distributed(card, discover(card, m, depth_of(card, dflt), dflt))
                           for m in card['media']]), notes
    if kind == 'discover' and arrival_mode(card):
        metas, n = arrival_card(card, depth_of(card, dflt), dflt)
        return metas, (f"ordered by arrival date: {n['signals']} dated arrivals + {n['arrivals'] - n['signals']} "
                       f"first seen, then {n['filled']} by release date")
    if kind == 'discover' and first_seen_mode(card):
        return first_seen_card(card, depth_of(card, dflt)), 'ordered by date first seen on the service'
    if kind == 'discover':
        depth = depth_of(card, dflt)
        return interleave([add_must_haves(card, m, discover(card, m, depth, dflt)) for m in card['media']]), notes
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


def total_drop_problem(prev, counts, total, allow_removal):
    """-> the GUARD message when total items fell more than DROP_LIMIT against the last run, else None. On a manual run
    with allow_catalog_removal (#139: one deliberate cut of shelves) only the catalogs that remain are compared, so
    shelves removed on purpose do not count as a drop; each kept catalog is still guarded one by one afterwards.
    Nightly runs never set it and compare the whole total."""
    old = prev.get('total_items')
    if allow_removal:
        old = sum(n for c, n in prev.get('catalogs', {}).items() if c in counts)
        total = sum(counts.values())
    if old and total < old * (1 - DROP_LIMIT):
        return f'GUARD: total items dropped {old} -> {total} (>30%)'
    return None


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
    deny = {(e['type'], int(e['tmdb'])) for e in load_hand_list('deny', spec)['deny']}
    card_exclude = load_hand_list('exclude', spec)
    # Partial rebuild (issue #77): only the listed cards are built; the rest of the site is copied from the live one.
    selected = parse_cards(os.environ.get('SD_CARDS'), spec['catalogs'])
    todo = spec['catalogs'] if selected is None else [c for c in spec['catalogs'] if c['slug'] in selected]
    entries, row_of, restore = {}, {}, None
    base_manifest = base_summary = None
    if selected is not None:
        # arrival (New) cards read the first_seen state restored by the workflow (cache / artifact); no state = they would
        # silently fall back to release order and could still pass the count guards, so refuse
        if FS_SUMMARY is None and any(c.get('provider') for c in todo):
            sys.exit('GUARD: first_seen state is missing; New cards need it - run a full build')
        base_url = site_url()
        base_dir = os.environ.get('SD_BASE_DIR') or ''
        base_manifest, base_summary = load_base(base_dir, base_url)
        live_ids = [e['id'] for e in base_manifest.get('catalogs', [])]
        want_ids = {f"sd-{c['slug']}" for c in spec['catalogs']}
        changed_ids = {f"sd-{c['slug']}" for c in todo}
        if set(live_ids) - want_ids or (want_ids - changed_ids) - set(live_ids):
            sys.exit('GUARD: the card list of the live site differs from the spec beyond the listed cards '
                     f'(live-only {sorted(set(live_ids) - want_ids)[:5]}, spec-only {sorted((want_ids - changed_ids) - set(live_ids))[:5]}); '
                     'run a full build')
        entries = {e['id']: e for e in base_manifest['catalogs'] if e['id'] not in changed_ids}
        print(f'partial rebuild of {len(todo)} cards: {sorted(selected)}', flush=True)
        restore = restore_site(root, base_dir, changed_ids)
        print(f'cached site copied: {restore} files', flush=True)

    def work(card):
        metas, notes = build_one(card, dflt)
        return metas, notes, source_of(card)

    try:
        results = {}
        with ThreadPoolExecutor(WORKERS) as pool:
            futs = {pool.submit(work, c): c['slug'] for c in todo}
            for done in as_completed(futs):
                results[futs[done]] = done.result()       # re-raises a worker's error / guard exit here
                if len(results) % 25 == 0:
                    print(f"  {len(results)}/{len(todo)} catalogs, {_calls[0]} requests, "
                          f"{time.monotonic() - t0:.0f}s", flush=True)
        for card in todo:                                  # write in spec (= folder) order
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
            card_ex = {(e['type'], int(e['tmdb'])) for e in card_exclude.get(card['slug'], [])}       # this card only (e.g. Oscar 1927/28)
            if card_ex:
                n = len(metas)
                metas = [m for m in metas if (m['type'], int(m['id'].split(':', 1)[1])) not in card_ex]
                _excluded.setdefault(cid, {})['card_exclude'] = n - len(metas)
                notes = f"{notes}; card exclusions removed {n - len(metas)}".lstrip('; ')
            options, gfiles, gbytes = write_catalog(root, 'movie', cid, metas, dflt['page_size'])
            genre_stats[cid] = {'options': len(options), 'files': gfiles, 'bytes': gbytes,
                                'untagged': sum(1 for m in metas if not m.get('_g'))}
            extra = ([{'name': 'genre', 'options': options, 'isRequired': False}] if options else []) + [{'name': 'skip'}]
            entries[cid] = {'type': 'movie', 'id': cid, 'name': card['library'], 'extra': extra}
            counts[cid] = len(metas)
            kinds = {t: sum(1 for m in metas if m['type'] == t) for t in ('movie', 'series')}
            rows.append([card['title'], card['library'], ' + '.join(folder_title[f] for f in card['folders']),
                         card.get('sort', card.get('order', 'default')), card['kind'], json.dumps(src_id) if not
                         isinstance(src_id, str) else src_id, src_name, cid,
                         f"movie={kinds['movie']} series={kinds['series']}", len(metas),
                         ' | '.join(f"{m['name']} ({m.get('releaseInfo', '?')})" for m in metas[:5]), notes])
    finally:
        save_cache()
    new_counts = dict(counts)
    if selected is not None:
        if fetch_summary_stamp(site_url()) != base_summary.get('generated_at'):
            sys.exit('GUARD: the live site was deployed while this partial build ran; run it again')
        counts = {f"sd-{c['slug']}": counts.get(f"sd-{c['slug']}", base_summary['catalogs'].get(f"sd-{c['slug']}"))
                  for c in spec['catalogs']}
        rows = merge_report(base_dir, rows, [f"sd-{c['slug']}" for c in spec['catalogs']])
    manifest['catalogs'] = [entries[f"sd-{c['slug']}"] for c in spec['catalogs']]
    ids = [c['id'] for c in manifest['catalogs']]
    if len(ids) != len(spec['catalogs']) or len(set(ids)) != len(ids):
        sys.exit(f"GUARD: manifest has {len(ids)} catalogs ({len(set(ids))} unique), spec has {len(spec['catalogs'])}")
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding='utf-8')
    (root / '.nojekyll').write_text('', encoding='utf-8')
    summary = {'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'tmdb_requests': _calls[0],
               'seconds': round(time.monotonic() - t0), 'total_items': sum(counts.values()), 'catalogs': counts}
    summary['rating_backfilled'] = _rating_backfill[0]     # #35: cached previews refetched this run for a rating
    modes = lambda c: 'arrivals' if arrival_mode(c) else 'first_seen' if first_seen_mode(c) else 'release_date'
    if selected is None:
        summary['genre_pages'] = {'files': sum(g['files'] for g in genre_stats.values()),
                                  'bytes': sum(g['bytes'] for g in genre_stats.values()),
                                  'options': sum(g['options'] for g in genre_stats.values()),
                                  'untagged_items': sum(g['untagged'] for g in genre_stats.values()),
                                  'backfilled': _backfill[0], 'min_titles': GENRE_MIN}
    else:
        # files / bytes / options are read back from the merged site, so they stay exact. untagged_items (titles with no
        # genre) cannot be recovered from written pages: carried from the last full build.
        gfiles, gbytes = tree_genre_stats(root)
        summary['genre_pages'] = {'files': gfiles, 'bytes': gbytes,
                                  'options': sum(len(x['options']) for e in manifest['catalogs']
                                                 for x in e['extra'] if x['name'] == 'genre'),
                                  'untagged_items': base_summary.get('genre_pages', {}).get('untagged_items', 0),
                                  'backfilled': _backfill[0], 'min_titles': GENRE_MIN}
    summary['discover_fill'] = {'pages': _fill[0], 'recovered': _fill[1]}
    if selected is None:
        fs = FS_SUMMARY or {'status': 'missing (logger did not run or failed)'}
        summary['first_seen'] = {k: v for k, v in fs.items() if k != 'providers'}
        summary['first_seen']['providers'] = {p: {k: v for k, v in i.items() if k in ('size', 'adds', 'removals', 'churn',
                                                                                      'history_days', 'mode', 'sliced')}
                                              for p, i in fs.get('providers', {}).items()}
        summary['new_card_modes'] = {c['slug']: modes(c) for c in spec['catalogs'] if c.get('provider')}
    else:
        # the logger does not run in a partial rebuild: its block is the live one (describes the last full run); the
        # listed New cards are re-evaluated against the stored state, the others keep their live mode
        summary['first_seen'] = base_summary.get('first_seen', {'status': 'missing (carried from live)'})
        live_modes = base_summary.get('new_card_modes', {})
        summary['new_card_modes'] = {c['slug']: (modes(c) if c['slug'] in selected else live_modes.get(c['slug'], 'release_date'))
                                     for c in spec['catalogs'] if c.get('provider')}
        summary['partial'] = {'cards': sorted(selected), 'base_generated_at': base_summary.get('generated_at')}
    (root / 'summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    if selected is not None and base_summary is not None:
        try:                                               # keep the live exclusions report, update the listed cards
            live_ex = json.loads((Path(base_dir) / 'exclusions.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            live_ex = {}
        merged_ex = {**{k: v for k, v in live_ex.items() if k not in new_counts}, **_excluded}
        if merged_ex:
            (root / 'exclusions.json').write_text(json.dumps(merged_ex, indent=1), encoding='utf-8')
    elif _excluded:
        (root / 'exclusions.json').write_text(json.dumps(_excluded, indent=1), encoding='utf-8')
    with open(root / 'report.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['card', 'library', 'folders', 'sort', 'source_type', 'source_id', 'source_name', 'catalog_id',
                    'media', 'item_count', 'top5', 'notes'])
        w.writerows(rows)
    print(json.dumps({k: v for k, v in summary.items() if k != 'catalogs'}), '| catalogs:', len(counts))

    if selected is not None:
        partial_guards(base_summary.get('catalogs', {}), new_counts)
        prev = None
    else:
        prev = previous_summary()
    empty = [c for c, n in counts.items() if n == 0]
    if empty:
        sys.exit(f'GUARD: empty catalogs: {empty}')
    if prev:
        emptied = [c for c, n in prev.get('catalogs', {}).items() if n > 0 and counts.get(c, 0) == 0]
        if emptied and os.environ.get('ALLOW_CATALOG_REMOVAL') == '1':
            print(f'catalogs removed/renamed on purpose (manual run): {emptied}')
        elif emptied:
            sys.exit(f'GUARD: previously non-empty catalogs are now empty: {emptied}')
        problem = total_drop_problem(prev, counts, summary['total_items'], os.environ.get('ALLOW_CATALOG_REMOVAL') == '1')
        if problem:
            sys.exit(problem)
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
