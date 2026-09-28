"""Arrival signals for the "New on X" cards: when titles arrive on each service, from published schedules.

  python arrivals.py <out dir> [--backfill] [--today YYYY-MM-DD]     (daily, after the first_seen logger)

TMDB's watch-provider data says what is on a service, not when it arrived; the first_seen logger (our own daily
snapshots) needs 14 days of history. These sources publish the dates directly:
  won   What's on Netflix "whats-new" feed: weekly "New on Netflix This Week" roundups (no per-day dates -> dated at the
        week START, precision "week") and "Netflix Adds N ... for <Month> 1st" posts (day)
  vt    Vital Thrills monthly "<service>-<month>-<year>" schedule posts: daily = the streaming-schedule tag feed;
        backfill = its post sitemaps (never ?s= search URLs: robots.txt)
  wodp  whatsondisneyplus.com monthly "What's Coming To Disney+ (US) / Hulu / HBO Max In <Month>": daily = its feed;
        backfill = its post sitemaps
  fb    Film-Book streaming-schedule category feed -> each post (robots Crawl-delay 5 s)
  plex  plex.tv blog "New on Plex in <Month>" (month precision)
Politeness: identifiable User-Agent, <= 1 request/s per host (Crawl-delay honoured), on-disk HTTP cache with ETag /
Last-Modified conditional GET (cache/http/arrivals-v2: validators + parsed rows only, never page text). Only title /
date / service / TMDB id / source URL are kept - never the sources' text; nothing is republished except TMDB ids and
dates inside our catalogs. Private, non-commercial use.

Every row is resolved to TMDB by exact title (+ year / media / season, films >= 40 min, on-service tie-break); anything
not exact or ambiguous is a CHECK row and is not used. Later seasons of returning shows count (copilot ruling).
Signals are merged into the durable first_seen state (<SD_STATE>/first_seen.json.gz, per provider "signals"):
  key "movie:ID" / "series:ID" -> {"o": [[date, precision, source, season, recorded_before_date], ...],
                                     "p": popularity, "k": title|season, "s": status}
  - every distinct observation is kept (so the result never depends on the order rows arrived in); on each read the
    observations are grouped into arrival events: dates at most 14 days apart and not different seasons = one event
    (sources a few days / a week apart, weekly episode drops); a new season or a later re-arrival is a new event
  - the card shows the LATEST event that has started, dated at its best precision (day > week > month), earliest
    among equals; a scheduled (future) event never hides a past one
  - status is recomputed daily: "confirmed" when the logger saw the title on the service today, else "announced"
  - a signal first recorded with a future date is shown only once its date has passed AND it is confirmed
  - an unconfirmed past roundup item (week / month precision) is shown for its first 7 days only; day-dated schedule
    items are trusted for the whole window (F1); a Netflix-network series with no US
    provider data at all on TMDB counts as confirmed on Netflix (F2)
  - pruned after 180 days (TMDB terms)
<SD_STATE>/signal_candidates.json (per provider, the signals build.py may show today) is written by this step and,
from the stored signals, by the logger - so a failing source or step falls back to the stored signals, never empty.
Outputs in <out dir>: arrivals.json (today's merged signals), rows.csv, check_rows.csv, parse_stats.csv,
arrivals_summary.json (per-source posts / rows / accepted / errors + warnings, read by health.py).
Env: TMDB_API_KEY (never printed), SD_CACHE, SD_STATE (default state), SD_HTTP_CACHE (default cache/http).
"""
import csv, datetime as dt, gzip, hashlib, html, json, os, re, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build  # noqa: E402  (throttled, key-safe tmdb(); durable cache in SD_CACHE)

STATE_DIR = Path(os.environ.get('SD_STATE', 'state'))
WINDOW = 45                     # New on X: arrivals in the last 45 days
NEW_EVENT_GAP = 14              # signals further apart are separate arrival events (a new season, a re-arrival)
PRUNE_DAYS = 180
DAILY_LOOKBACK = 50             # daily run: posts from the last 50 days (the window + a margin)
BACKFILL_LOOKBACK = 92          # --backfill: the last 3 months
PREC = {'day': 0, 'week': 1, 'month': 2}

# ---------------------------------------------------------------- polite HTTP ------------------------------------
UA = 'strand-discovery/1.0 (personal non-commercial; +https://github.com/xBOBxSAGETx/strand-discovery)'
# The cache keeps NO source text: per (parser, URL) only the validators (ETag / Last-Modified), status, fetch time and
# the parser's output (feed item titles / links / dates, sitemap URL lists, parsed title rows). A 304 or a fresh
# record returns the stored parsed data; the parser only ever sees freshly fetched text, which is never written.
# 'arrivals-v2': the earlier namespace ('arrivals') held page bodies; it is deleted on start and never read again.
HTTP_ROOT = Path(os.environ.get('SD_HTTP_CACHE', 'cache/http'))
HTTP_CACHE = HTTP_ROOT / 'arrivals-v2'
PARSER_VERSION = 2              # bump on ANY parser change: stored parsed data from another version is refetched in full
DELAY = {'film-book.com': 5.0}          # robots.txt Crawl-delay
DEFAULT_DELAY = 1.1
_last = {}
fetch_stats = {'requests': 0, 'cache_fresh': 0, 'not_modified': 0, 'errors': 0, 'stale_fallback': 0,
               'scan_removed': 0}
TEXT_KEYS = {'body', 'text', 'content', 'html', 'xml', 'raw'}   # 'raw': source lines are not kept either
MARKUP = re.compile(r'<\s*/?\s*(html|head|body|rss|channel|item|feed|entry|article|div|p|span|script|style|loc|'
                    r'urlset|sitemapindex|\?xml|!\[CDATA)\b', re.I)
MAX_STR = 1000                  # parsed values are titles, dates, URLs, short title lines - never prose


def _assert_no_text(obj, where):
    """Raise if a record about to be persisted (or found on disk) carries source text."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in TEXT_KEYS:
                raise AssertionError(f'{where}: text field {k!r} in the HTTP cache')
            _assert_no_text(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _assert_no_text(v, where)
    elif isinstance(obj, str) and (len(obj) > MAX_STR or MARKUP.search(obj)):
        raise AssertionError(f'{where}: raw markup / long text in the HTTP cache ({len(obj)} chars)')


def scan_http_cache():
    """On start: delete the old body-bearing namespace, and any record that is not clean parsed data."""
    old = HTTP_ROOT / 'arrivals'
    if old.is_dir():
        for f in old.iterdir():
            f.unlink(missing_ok=True)
            fetch_stats['scan_removed'] += 1
        old.rmdir()
    if not HTTP_CACHE.is_dir():
        return
    for f in HTTP_CACHE.iterdir():
        try:
            if f.suffix != '.json':
                raise AssertionError('not a parsed record')
            _assert_no_text(json.loads(f.read_text(encoding='utf-8')), f.name)
        except (AssertionError, OSError, ValueError):
            f.unlink(missing_ok=True)
            fetch_stats['scan_removed'] += 1


def _record_path(url, parser):
    return HTTP_CACHE / (hashlib.sha256(f'{parser}|{url}'.encode()).hexdigest()[:24] + '.json')


def get_parsed(url, parser, parse, max_age=20.0):
    """(status, data, from_cache). `parse(text)` -> JSON-serialisable data; `parser` names it (with PARSER_VERSION it
    keys the record). Records younger than max_age hours are reused; older ones are revalidated with ETag /
    Last-Modified (304 -> the stored data). A network error falls back to the stored data if there is any."""
    q = urllib.parse.urlparse(url)
    if re.search(r'(^|&)s=', q.query or ''):
        raise ValueError(f'search URLs are not allowed by robots.txt: {url}')
    rec_p = _record_path(url, parser)
    try:
        rec = json.loads(rec_p.read_text(encoding='utf-8')) if rec_p.exists() else None
    except (OSError, ValueError):
        rec = None
    usable = bool(rec) and rec.get('v') == PARSER_VERSION and 'data' in rec
    if usable and time.time() - rec['fetched'] < max_age * 3600 and os.environ.get('SD_HTTP_REVALIDATE') != '1':
        fetch_stats['cache_fresh'] += 1
        return rec['status'], rec['data'], True
    host = q.netloc.lower().removeprefix('www.')
    wait = _last.get(host, 0) + DELAY.get(host, DEFAULT_DELAY) - time.time()
    if wait > 0:
        time.sleep(wait)
    headers = {'User-Agent': UA, 'Accept-Encoding': 'identity'}
    if usable:                                 # another parser version: full refetch, no conditional headers
        if rec.get('etag'):
            headers['If-None-Match'] = rec['etag']
        if rec.get('last_modified'):
            headers['If-Modified-Since'] = rec['last_modified']
    fetch_stats['requests'] += 1
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
            status, text = r.status, r.read().decode('utf-8', errors='replace')
            etag, lm = r.headers.get('ETag'), r.headers.get('Last-Modified')
    except urllib.error.HTTPError as e:
        _last[host] = time.time()
        if e.code == 304 and usable:
            fetch_stats['not_modified'] += 1
            rec['fetched'] = time.time()
            _write(rec_p, rec)
            return 200, rec['data'], True
        fetch_stats['errors'] += 1
        return e.code, None, False
    except Exception:                          # network error: the stored parsed data beats nothing
        _last[host] = time.time()
        fetch_stats['errors'] += 1
        if usable:
            fetch_stats['stale_fallback'] += 1
            return rec['status'], rec['data'], True
        return 0, None, False
    _last[host] = time.time()
    data = parse(text)
    del text                                   # the source text goes no further than the parser
    _write(rec_p, {'url': url, 'parser': parser, 'v': PARSER_VERSION, 'status': status, 'etag': etag,
                   'last_modified': lm, 'fetched': time.time(), 'data': data})
    return status, data, False


def _write(path, rec):
    _assert_no_text(rec, path.name)            # every write is checked: parsed data only
    HTTP_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(rec, separators=(',', ':')), encoding='utf-8')
    tmp.replace(path)


# ---------------------------------------------------------------- text helpers -----------------------------------
MONTHS = {m: i for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                      'september', 'october', 'november', 'december'], 1)}
MON_RX = r'(january|february|march|april|may|june|july|august|september|october|november|december|sept?\.?|oct\.?|aug\.?|nov\.?|dec\.?|jan\.?|feb\.?|mar\.?|apr\.?|jun\.?|jul\.?)'
SHORT = {'sep': 9, 'sept': 9, 'oct': 10, 'aug': 8, 'nov': 11, 'dec': 12, 'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
         'jun': 6, 'jul': 7}
DROPPED = {'crunchyroll', 'rakuten-viki', 'the-roku-channel', 'kanopy', 'hoopla'}     # removed by the owner
# Vital Thrills URL slug prefix -> spec service slug
VT_SERVICES = {'netflix': 'netflix', 'prime-video': 'prime-video', 'hbo-max': 'hbo-max', 'paramount-plus': 'paramountplus',
               'peacock': 'peacock', 'apple-tv': 'apple-tv', 'starz': 'starz', 'britbox': 'britbox',
               'amc-plus': 'amcplus', 'shudder': 'shudder', 'tubi': 'tubi', 'terror-on-tubi': 'tubi',
               'acorn-tv': 'acorn-tv', 'pluto-tv': 'pluto-tv', 'disney-plus-and-hulu': 'disney+hulu'}
FB_SERVICES = {'netflix': 'netflix', 'disney+': 'disneyplus', 'hulu': 'hulu', 'hbo max': 'hbo-max',
               'paramount+': 'paramountplus', 'apple tv': 'apple-tv', 'britbox': 'britbox', 'shudder': 'shudder',
               'starz': 'starz', 'tubi': 'tubi', 'acorn tv': 'acorn-tv', 'mubi': 'mubi',
               'the criterion channel': 'criterion-channel', 'plex': 'plex', 'peacock': 'peacock',
               'prime video': 'prime-video'}
WODP = {'disney-in': 'disneyplus', 'hulu-hulu-on-disney-in': 'hulu', 'hbo-max-in': 'hbo-max'}
SKIP = re.compile(r'podcast|livestream|live stream|\blive\b|@ ?\d|\d ?(am|pm) (et|pt)|\bespn\b|'
                  r'\bnfl\b|wnba|nwsl|\bmlb\b|yankees|college football|volleyball|us open|premier league|nascar|'
                  r'\bufc\b|boxing|fast channel|\bvolume \d+, season|^\W*$|serie a\b|bundesliga|la liga|liga mx|uefa|'
                  r'champions league|\bwsl\b|\bpbr\b|big ten|game of the day|\bstage \d|la vuelta|tour de france|\bpga\b|'
                  r'lpga|\bnhl\b|\bnba\b|formula 1|motogp|indycar|supercross|sailgp|soccer|\bmls\b|wrestling|\bwwe\b|'
                  r'\baew\b|kentucky downs|horse racing|marathon|zuffa|fight night|\bmma\b|big3|\bsec network|'
                  r'thursday night football|sunday night|golf|grand prix|world cup|trailer|newsletter|'
                  r'\((daily|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)\b|'
                  r'\bthrough (friday|saturday|sunday)\b|regular season game', re.I)
NOISE = {'added', 'premiere', 'new', 'streaming now', 'date', 'show', 'category', 'status', 'related articles',
         'advertisement', 'movies', 'series', 'tv', 'films', 'coming soon', 'new episodes', 'returning',
         'follow on x', 'youtube', 'facebook', 'x', 'pinterest', 'instagram', 'tiktok', 'newsletter', 'threads',
         'bluesky', 'reddit', 'share', 'email', 'print', 'rss', 'mobile app', 'about us', 'contact us', 'jobs'}
STATUS = re.compile(r'^\s*(premiere|new episodes?|all episodes|streaming|season|tv-(y7?|g|pg|14|ma)|pg(-13)?|r|nr|g|'
                    r'n/a|netflix original|hulu original|documentary|stand-up|limited series|nc-17|unrated)', re.I)


def norm(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower().replace('&', 'and')
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def text_lines(fragment):
    b = re.sub(r'<figure.*?</figure>|<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>', '', fragment, flags=re.S)
    b = re.sub(r'<(h[1-6])[^>]*>', r'\n##\1 ', b)
    b = re.sub(r'<li[^>]*>', '\n- ', b)
    b = re.sub(r'<br ?/?>', '\n', b)
    b = re.sub(r'</(p|div|tr|h[1-6]|li)>', '\n', b)
    b = html.unescape(re.sub(r'<[^>]+>', '', b)).replace('’', "'").replace('‘', "'").replace('\xa0', ' ')
    return [x.strip() for x in b.splitlines() if x.strip() and x.strip() not in ('-', '–')]


def feed_items(xml):
    out = []
    for it in re.findall(r'<item>(.*?)</item>', xml, re.S):
        g = lambda tag: (re.search(rf'<{tag}>(.*?)</{tag}>', it, re.S) or [None, ''])[1]
        content = re.search(r'<content:encoded><!\[CDATA\[(.*?)\]\]></content:encoded>', it, re.S)
        pub = dt.datetime.strptime(g('pubDate')[:16], '%a, %d %b %Y').date() if g('pubDate') else None
        out.append({'title': html.unescape(re.sub(r'<!\[CDATA\[|\]\]>', '', g('title'))).replace('’', "'"),
                    'link': g('link').strip(), 'pub': pub, 'content': content.group(1) if content else ''})
    return out


def feed_links(xml):
    """Parser: feed items without their text - title, link, publication date (ISO) only."""
    return [{'title': it['title'], 'link': it['link'], 'pub': it['pub'].isoformat() if it['pub'] else None}
            for it in feed_items(xml)]


def loc_urls(xml):
    """Parser: the <loc> URLs of a sitemap / sitemap index."""
    return re.findall(r'<loc>(.*?)</loc>', xml)


def _dates(items):
    """Stored feed items -> the pub field back as a date."""
    return [{**it, 'pub': dt.date.fromisoformat(it['pub']) if it.get('pub') else None} for it in items]


def clean(raw):
    """-> dict(title, year, season, media) or None (skip). media: movie | series | None (unknown)."""
    s = raw.strip().lstrip('-*• ').replace('**', '').strip()
    s = re.sub(r'\s*\*+$', '', s)                                    # WoN "* not yet confirmed" marker
    full = s
    if re.search(r'(?i)(canada|uk|ireland|australia)(,| and)?\s*(premiere|only|exclusive)|now available in canada|'
                 r'available in canada', full) and not re.search(r'(?i)north america|united states|\bu\.?s\.?\b', full):
        return None                                     # BritBox rows for other regions (US lists only)
    if re.search(r'(?i)already available in (the )?(united states|u\.?s\.?)\b', full):
        return None                                     # not a new US arrival
    s = s.split(' | ')[0].replace('“', '').replace('”', '').replace('"', '').strip()
    if s.lower().strip(' :') in NOISE or re.fullmatch(r'leav(?:ing)( soon)?', s.lower().strip(' :')):
        return None                                     # section headings, incl. departure headings
    if re.search(r'(?i)new episodes?|finale|\bepisode\b|two-episode|weekly', s) and \
            not re.search(r'(?i)all episodes|episodes 1\s*[-–]', s):
        return None                                     # an episode of an ongoing season, not an arrival
    if SKIP.search(s) or len(s) > 140:
        return None
    year = re.search(r'\((19\d\d|20\d\d)\)', s)
    season = None
    m = re.search(r'\bseasons? (\d+)', full, re.I)
    if m:
        season = int(m.group(1))
    lim = bool(re.search(r'limited series|miniseries|docuseries|\bseries\)|\((19|20)\d\d[–-]\)|\|\s*(unscripted|scripted|'
                         r'docu|reality|animated)?\s*series', full, re.I))
    s = re.sub(r'\s*\((19|20)\d\d[–-]\)', '', s)
    cut = len(s)
    for rx in (r'\((19|20)\d\d\)', r'\(\s*(complete )?seasons?\b', r'\((limited series|stand-up|documentary|special)',
               r',?\s+(complete )?seasons? \d', r'\s+netflix original', r',\s+directed by', r'\s+\((hbo|hulu|disney|fx|netflix|apple|prime|'
               r'peacock|paramount|starz|amc|shudder|britbox|acorn|discovery|tlc|hgtv|id|cnn|tnt|own|dc|magnolia|food|'
               r'cartoon|abc|nbc|cbs|a&e|lifetime|bravo|history|nat geo|national geographic)[^)]*\)'):
        m = re.search(rx, s, re.I)
        if m:
            cut = min(cut, m.start())
    t = s[:cut]
    parts = re.split(r'\s+[–—-]\s+', t)                        # "Title - Premiere" / "Title - TV-14 - English"
    keep = [parts[0]]
    for p in parts[1:]:
        if STATUS.match(p):
            break
        keep.append(p)
    t = ' – '.join(keep)
    t = re.sub(r"^(FX's|FX|Marvel Studios'|Pixar's|Disney's|Tyler Perry's)\s+(?=\S)",
               lambda m: '' if m.group(1).startswith('FX') else m.group(0), t)
    t = re.sub(r'\s*\((hulu|disney\+?|netflix|hbo)[^)]*\)\s*$', '', t, flags=re.I).strip(' ,:;–-')
    inv = re.match(r'^(.*), (The|A|An)$', t)                          # Starz "Anomaly, The"
    if inv:
        t = f'{inv.group(2)} {inv.group(1)}'
    if not t or len(t) < 2 or t.lower() in NOISE:
        return None
    words = t.split()
    if len(words) > 12 or (len(words) > 7 and sum(w[:1].islower() for w in words) > len(words) / 2):
        return None                                     # a sentence, not a title
    media = 'series' if (season or lim) else ('movie' if year else None)
    return {'title': t, 'year': int(year.group(1)) if year else None, 'season': season, 'media': media}


def heading_date(line, year):
    """'AVAILABLE OCTOBER 1', 'TUESDAY, SEPTEMBER 1', 'October 1st, 2026', 'September 1' -> (date, 'day');
    month-only headings -> ('month', 'month')."""
    s = line.replace('**', '').replace('##h2', '').replace('##h3', '').replace('##h4', '').strip().rstrip(':').lower()
    if len(s) > 45:
        return None
    m = re.fullmatch(r'(?:available\s+|coming\s+|streaming\s+|premieres?\s+)?(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?'
                     + MON_RX + r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?', s)
    if m:
        mon = MONTHS.get(m.group(1).rstrip('.'), SHORT.get(m.group(1).rstrip('.')))
        try:
            return dt.date(int(m.group(3) or year), mon, int(m.group(2))), 'day'
        except ValueError:
            return None
    m = re.fullmatch(r'(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+' + MON_RX +
                     r'(?:,?\s+(20\d\d))?', s)
    if m:
        mon = MONTHS.get(m.group(2).rstrip('.'), SHORT.get(m.group(2).rstrip('.')))
        try:
            return dt.date(int(m.group(3) or year), mon, int(m.group(1))), 'day'
        except (ValueError, TypeError):
            return None
    if re.fullmatch(r'(?:available|coming|streaming)\s+(?:this month|in ' + MON_RX + r'|' + MON_RX + r'|tba|date tba)', s):
        return 'month', 'month'
    return None


def dated_rows(lines, service, year, month, source, url, start=None, stop=None, prefix=False, bullets_only=False,
               heading_marker=False, heading_year=None, default_date=None, h3_titles=False, drop_prefixes=(),
               strip_prefix=False, skip_lines=None):
    """Walk the lines after `start` (regex): date headings set the date, other lines are titles. heading_marker: a
    date heading must be a '##h' line; heading_year False: a heading carrying a year is ignored (Film-Book sidebar);
    default_date: the date before any heading; dates outside the post month +- 1 are ignored (sidebars / links)."""
    rows, date, prec, media, on = [], None, None, None, start is None
    if default_date:
        date, prec = default_date
    table, take_next = False, False
    lo, hi = dt.date(year, month, 1) - dt.timedelta(days=31), dt.date(year, month, 28) + dt.timedelta(days=35)
    for ln in lines:
        if not on:
            on = bool(re.search(start, ln, re.I))
            continue
        if stop and re.search(stop, ln, re.I):
            break
        if ln.lower() in ('date', 'show', 'title') and not table:
            table = True                                # Film-Book table: date / title / category / status cells
            continue
        if re.search(r'(?i)unless otherwise noted, all .* (on|begin) .*first (day|of the month)', ln):
            date, prec = dt.date(year, month, 1), 'day'
            continue
        h = heading_date(ln, year)
        if h and (not heading_marker or ln.startswith('##')) and not (heading_year is False and re.search(r'20\d\d', ln)):
            d, pr = (dt.date(year, month, 1), 'month') if h[0] == 'month' else h
            if lo <= d <= hi:
                date, prec, take_next = d, pr, table
            continue
        if h:
            continue
        m = re.match(MON_RX + r' (\d{1,2})(?:-\d{1,2})?\s*[–:-]\s*(.+)$', ln.replace('**', ''), re.I)
        if m and not prefix:                            # inline "September 4 - Title" / "September 1: Title"
            mon = MONTHS.get(m.group(1).lower().rstrip('.'), SHORT.get(m.group(1).lower().rstrip('.')))
            try:
                d = dt.date(year, mon, int(m.group(2)))
            except (ValueError, TypeError):
                continue
            if not lo <= d <= hi:
                continue
            ln_date, ln_prec, ln = d, 'day', m.group(3)
        else:
            ln_date, ln_prec = date, prec
            m = re.search(r'\s+[–-]\s+(\d{1,2})/(\d{1,2})$', ln)   # Tubi "Title - 9/11"
            if m:
                try:
                    ln_date, ln_prec, ln = dt.date(year, int(m.group(1)), int(m.group(2))), 'day', ln[:m.start()]
                except ValueError:
                    pass
        up = ln.replace('**', '').strip().upper()
        if up in ('MOVIES', 'FILMS', 'MOVIE', 'FEATURE FILMS'):
            media = 'movie'
            continue
        if up in ('SERIES', 'TV', 'TV SHOWS', 'TV SERIES', 'SHOWS', 'SERIES SPOTLIGHT'):
            media = 'series'
            continue
        if ln_date is None or ln.startswith('##') or re.fullmatch(MON_RX + r' \d{1,2}, 20\d\d', ln.lower()):
            continue
        if h3_titles and ln.startswith('##h3 ') and date:
            ln = '- ' + ln[5:]                          # wodp featured titles are h3 headings under the date
        if drop_prefixes and re.match(r'^-?\s*(' + '|'.join(drop_prefixes) + r')\s*:', ln, re.I):
            continue
        if drop_prefixes and re.search(r'\s[–-]\s(' + '|'.join(drop_prefixes) + r')\s*$', ln, re.I):
            continue
        if table:
            if not take_next:
                continue                                # category / status cells
            take_next = False
        if bullets_only and not ln.startswith('- '):
            continue
        plain = ln.replace('**', '').strip()
        if plain.isupper() and len(plain) < 32 and not re.search(r'\d', plain) and not prefix:
            media = None                                # genre / section headers (ACTION, THRILLER)
            continue
        svcs = [service]
        if prefix:                                     # "Hulu: **Title" / "Disney+ and Hulu: **Title"
            m = re.match(r'^(disney\+ and hulu|hulu|disney\+)\s*:\s*(.*)$', plain, re.I)
            if not m:
                continue
            svcs = {'hulu': ['hulu'], 'disney+': ['disneyplus'], 'disney+ and hulu': ['disneyplus', 'hulu']}[m.group(1).lower()]
            ln = m.group(2)
        elif len(ln) > 110 or '. ' in ln or ln.endswith('.'):
            continue                                    # description sentences, not titles
        if strip_prefix:                               # Film-Book Disney+ post: "Disney+ and Hulu: Title"
            m = re.match(r'^(disney\+ and hulu|hulu|disney\+|espn)\s*:\s*(.*)$', plain, re.I)
            if m:
                here = {'hulu': ['hulu'], 'disney+': ['disneyplus'], 'espn': [],
                        'disney+ and hulu': ['disneyplus', 'hulu']}[m.group(1).lower()]
                if service not in here:
                    continue
                ln = m.group(2)
        if skip_lines and re.search(skip_lines, ln[:120], re.I):
            continue
        c = clean(ln)
        if not c:
            continue
        if c['media'] is None and media:
            c['media'] = media
        for sv in svcs:
            # the source line itself is not kept: only what it yields (a "short" hint for the runtime rule)
            rows.append({**c, 'service': sv, 'date': ln_date.isoformat(), 'precision': ln_prec,
                         'source': source, 'url': url, **({'short': True} if re.search(r'short', ln[:120], re.I) else {})})
    return rows


# ---------------------------------------------------------------- sources -----------------------------------------
def _won_item_rows(it):
    """Rows of one What's on Netflix feed item (its content is in the feed), or None if it is not a new-arrivals post."""
    t = it['title']
    weekly = re.search(r'this week', t, re.I) and re.search(r'new', t, re.I) and 'UK' not in t
    first = re.search(r'(adds|added).*for (\w+) 1st', t, re.I)
    if not (weekly or first) or not it['pub']:
        return None
    rows, on, media = [], False, None
    for ln in text_lines(it['content']):
        if re.search(r'full list of new releases', ln, re.I):
            on = True
            continue
        if not on:
            continue
        if ln.startswith('##h3'):
            break                                       # next section (Top 10 ...)
        if ln.startswith('##h4'):
            media = 'series' if re.search(r'tv|series|shows', ln, re.I) else 'movie'
            continue
        if not ln.startswith('- '):
            continue
        c = clean(ln[2:])
        if not c:
            continue
        c['media'] = c['media'] if c['season'] else media
        if weekly:                                      # no per-day dates in the roundup: the week's START
            date, prec = it['pub'] - dt.timedelta(days=6), 'week'
        else:
            date, prec = dt.date(it['pub'].year, MONTHS[first.group(2).lower()], 1), 'day'
        rows.append({**c, 'service': 'netflix', 'date': date.isoformat(), 'precision': prec,
                     'source': 'won', 'url': it['link'],
                     **({'short': True} if re.search(r'short', ln[2:120], re.I) else {})})
    return rows


def won_feed(xml):
    """Parser: the What's on Netflix feed -> items with their parsed rows (the post text itself is not kept)."""
    return [{'title': it['title'], 'link': it['link'], 'pub': it['pub'].isoformat() if it['pub'] else None,
             'rows': _won_item_rows(it)} for it in feed_items(xml)]


def src_won(since, backfill, stats):
    rows = []
    st, data, _ = get_parsed('https://www.whats-on-netflix.com/whats-new/feed/', 'won-feed', won_feed)
    items = _dates(data) if st == 200 else []
    stats['won'] = {'posts': 0, 'items': len(items), 'errors': [] if st == 200 else [f'feed HTTP {st}'], 'notes': []}
    for it in items:
        if not it['pub'] or it['pub'] < since or it['rows'] is None:
            continue
        stats['won']['posts'] += 1
        rows += it['rows']
    return rows


def post_body(t):
    i = t.find('entry-content')
    if i < 0:
        i = t.find('<article')
    j = t.find('</article>', i)
    return t[i:j if j > 0 else len(t)]


def sitemap_posts(site, index, n=3):
    """Post URLs from the newest n post sitemaps listed in a WordPress sitemap index."""
    st, locs, _ = get_parsed(f'{site}/{index}', 'sitemap', loc_urls, max_age=20)
    maps = [u for u in locs if re.search(r'post-sitemap\d*\.xml', u)] if st == 200 else []
    maps.sort(key=lambda u: int((re.search(r'post-sitemap(\d*)\.xml', u).group(1) or 1)))
    urls = []
    for u in maps[-n:]:
        s, x, _ = get_parsed(u, 'sitemap', loc_urls, max_age=20)
        urls += x if s == 200 else []
    return urls


def src_vt(months, backfill, stats):
    rows, posts, errs, notes = [], 0, [], []
    if backfill:
        urls = sitemap_posts('https://www.vitalthrills.com', 'sitemap_index.xml')
    else:
        st, items, _ = get_parsed('https://www.vitalthrills.com/tag/streaming-schedule/feed/', 'feed-links', feed_links)
        urls = [it['link'] for it in items] if st == 200 else []
        if st != 200:
            errs.append(f'tag feed HTTP {st}')
    for u in dict.fromkeys(urls):
        m = re.search(r'vitalthrills\.com/([a-z-]+?)-(' + '|'.join(months) + r')-(20\d\d)/?$', u)
        if not m or m.group(1) not in VT_SERVICES:
            continue
        svc, mon, year = VT_SERVICES[m.group(1)], MONTHS[m.group(2)], int(m.group(3))
        if not in_window(year, mon):
            continue
        s, r, _ = get_parsed(u, 'vt-post', lambda t, svc=svc, year=year, mon=mon, u=u: dated_rows(
            text_lines(post_body(t)), svc, year, mon, 'vt', u, start=r'^(##h2 )?.*(schedules?|titles)$',
            stop=r'^#\w|^tags?:|related posts|^##h2 (sports|live|fast channels|espn)', prefix=svc == 'disney+hulu'),
            max_age=72)
        if s != 200:
            errs.append(f'{u}: HTTP {s}')
            continue
        posts += 1
        if not r:
            notes.append(f'{u}: 0 rows')
        rows += r
    stats['vt'] = {'posts': posts, 'items': len(urls), 'errors': errs, 'notes': notes}
    return rows


def src_wodp(months, backfill, stats):
    rows, posts, errs, notes = [], 0, [], []
    if backfill:
        urls = sitemap_posts('https://whatsondisneyplus.com', 'sitemap.xml')          # its index (no sitemap_index)
    else:
        st, items, _ = get_parsed('https://whatsondisneyplus.com/feed/', 'feed-links', feed_links)
        urls = [it['link'] for it in items] if st == 200 else []
        if st != 200:
            errs.append(f'feed HTTP {st}')
    for u in dict.fromkeys(urls):
        m = re.search(r"whats-coming-to-(disney-in|hulu-hulu-on-disney-in|hbo-max-in)-(" + '|'.join(months) +
                      r")-(20\d\d)(-us)?/?$", u)
        if not m or (m.group(1) == 'disney-in' and not m.group(4)):
            continue                                    # Disney+: the US edition only
        svc, mon, year = WODP[m.group(1)], MONTHS[m.group(2)], int(m.group(3))
        if not in_window(year, mon):
            continue
        s, r, _ = get_parsed(u, 'wodp-post', lambda t, svc=svc, year=year, mon=mon, u=u: dated_rows(
            text_lines(post_body(t)), svc, year, mon, 'wodp', u, start=None,
            stop=r'looking forward to|for the latest|let me know', bullets_only=True, heading_marker=True,
            h3_titles=True, drop_prefixes=('hulu', 'espn') if svc == 'disneyplus' else ('espn',)), max_age=72)
        if s != 200:
            errs.append(f'{u}: HTTP {s}')
            continue
        posts += 1
        if not r:
            notes.append(f'{u}: 0 rows')
        rows += r
    stats['wodp'] = {'posts': posts, 'items': len(urls), 'errors': errs, 'notes': notes}
    return rows


def src_fb(months, backfill, stats):
    rows, posts, errs, notes = [], 0, [], []
    feeds = ['https://film-book.com/category/streaming-schedule/feed/']        # ~120 posts: 3+ months (no paging)
    items = []
    for f in feeds:
        st, data, _ = get_parsed(f, 'feed-links', feed_links)
        if st != 200:
            errs.append(f'feed HTTP {st}' if f == feeds[0] else f'{f}: HTTP {st}')
            continue
        items += data
    seen = set()
    for it in items:
        if it['link'] in seen:
            continue
        seen.add(it['link'])
        m = re.match(r'(.+?) (' + '|'.join(m.capitalize() for m in months) + r') (20\d\d)', it['title'])
        if not m:
            continue
        svc = FB_SERVICES.get(m.group(1).strip().lower())
        if not svc or svc in DROPPED or not in_window(int(m.group(3)), MONTHS[m.group(2).lower()]):
            continue
        year, mon, link = int(m.group(3)), MONTHS[m.group(2).lower()], it['link']
        s, r, _ = get_parsed(link, 'fb-post', lambda t, svc=svc, year=year, mon=mon, link=link: dated_rows(
            text_lines(t[t.find('<article'):]), svc, year, mon, 'fb', link,
            start=r'^##h3 .*schedule$', stop=r'^(more .* streaming|view all streaming|share this|about the author|'
                                           r'tags:|leave a (reply|comment)|##h[23] .*(leav(?:ing)|coming soon)|'
                                           r'back to top|you may also like|recent posts|popular posts|'
                                           r'movie trailer|^contest$|newsletter|trending on filmbook|'
                                           r'latest video|^flickr$|^tags$|^subscribe$|delivered to your inbox)',
            strip_prefix=svc in ('disneyplus', 'hulu'), heading_year=False,
            skip_lines=r'schedule:|streaming release|^advertisement'), max_age=72)
        if s != 200:
            errs.append(f"{it['link']}: HTTP {s}")
            continue
        posts += 1
        if not r:
            notes.append(f"{it['link']}: 0 dated rows (month-level format, not parsed)")
        rows += r
    stats['fb'] = {'posts': posts, 'items': len(items), 'errors': errs, 'notes': notes}
    return rows


def _plex_month(it):
    m = re.match(r'new on plex in (\w+)', it['title'], re.I)
    if not m or m.group(1).lower() not in MONTHS or not it['pub']:
        return None
    mon = MONTHS[m.group(1).lower()]
    return it['pub'].year + (1 if mon < it['pub'].month - 6 else 0), mon


def _plex_rows(text, year, mon, link):
    return dated_rows(text_lines(text), 'plex', year, mon, 'plex', link, start=r'^##h\d new on plex in',
                      stop=r'^##h\d', default_date=(dt.date(year, mon, 1), 'month'))


def plex_feed(xml):
    """Parser: the Plex blog feed -> items; a "New on Plex in <Month>" item carries its parsed rows when the feed
    holds the post (rows None = fetch the post page)."""
    out = []
    for it in feed_items(xml):
        ym = _plex_month(it)
        out.append({'title': it['title'], 'link': it['link'], 'pub': it['pub'].isoformat() if it['pub'] else None,
                    'rows': _plex_rows(it['content'], *ym, it['link']) if ym and it['content'] else None})
    return out


def src_plex(months, backfill, stats):
    rows, posts, errs, notes = [], 0, [], []
    st, data, _ = get_parsed('https://www.plex.tv/blog/feed/', 'plex-feed', plex_feed)
    items = _dates(data) if st == 200 else []
    if st != 200:
        errs.append(f'feed HTTP {st}')
    for it in items:
        m = re.match(r'new on plex in (\w+)', it['title'], re.I)
        if not m or m.group(1).lower() not in months:
            continue
        year, mon = _plex_month(it)
        r = it['rows']
        if r is None:                                   # the feed did not carry the post: its page
            s, r, _ = get_parsed(it['link'], 'plex-post', lambda t, year=year, mon=mon, link=it['link']:
                                 _plex_rows(t, year, mon, link), max_age=72)
            r = r if s == 200 else []
        posts += 1
        if not r:
            notes.append(f"{it['link']}: 0 rows")
        rows += r
    stats['plex'] = {'posts': posts, 'items': len(items), 'errors': errs, 'notes': notes}
    return rows


def prune_http_cache(days=120):
    """Drop records not fetched for `days` (the durable cache is saved every run; posts that old are unused)."""
    cut = time.time() - days * 86400
    for rec_p in HTTP_CACHE.glob('*.json'):
        try:
            if json.loads(rec_p.read_text(encoding='utf-8'))['fetched'] < cut:
                rec_p.unlink()
        except (OSError, ValueError, KeyError):
            pass


_WIN = {}


def in_window(year, mon):
    """A monthly post is read only if its month overlaps the run's window (older posts of other years match the URL
    patterns too)."""
    lo, hi = _WIN.get('lo'), _WIN.get('hi')
    first = dt.date(year, mon, 1)
    return not lo or (lo - dt.timedelta(days=31) <= first <= hi)


SOURCES = {'won': src_won, 'vt': src_vt, 'wodp': src_wodp, 'fb': src_fb, 'plex': src_plex}


# ---------------------------------------------------------------- TMDB resolution ---------------------------------
def providers():
    """spec service slug -> set of TMDB provider ids (the Popular streaming cards)."""
    spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
    out = {}
    for c in spec['catalogs']:
        if c['slug'].startswith('streaming-') and c.get('sort') == 'popular':
            out[c['slug'][len('streaming-'):]] = set(c['movie']['with_watch_providers'].split('|'))
    return out


def fs_key(key):
    """'movie:123' -> the first_seen state's 'm:123'."""
    media, tid = key.split(':')
    return ('m:' if media == 'movie' else 't:') + tid


class Presence:
    """Is a title on a service today? The first_seen state (the logger ran minutes ago: last_seen == its last run)
    answers for free; TMDB watch/providers only for services the logger has no state for."""

    def __init__(self, state, pids):
        self.state, self.pids, self.memo = state, pids, {}

    def __call__(self, svc, media, tid):
        prov = (self.state or {}).get('providers', {}).get(svc)
        if prov and prov.get('last_run'):
            it = prov['items'].get(fs_key(f'{media}:{tid}'))
            return bool(it and it[1] >= prov['last_run'])
        if svc not in self.pids:
            return False
        k = (svc, media, tid)
        if k not in self.memo:
            try:
                wp = build.tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}/watch/providers")['results'].get('US', {})
            except (RuntimeError, SystemExit):
                wp = {}
            self.memo[k] = bool({str(p['provider_id']) for kind in ('flatrate', 'free', 'ads', 'rent')
                                 for p in wp.get(kind, [])} & self.pids.get(svc, set()))
        return self.memo[k]


FRANCHISE = re.compile(r"^(star wars|marvel studios'|marvel's|marvel|disney's|pixar's|dc's|dc)\s*:?\s+(?=\S)", re.I)


def _year(r):
    d = r.get('release_date') or r.get('first_air_date') or ''
    return int(d[:4]) if d[:4].isdigit() else None


def resolve(row, present):
    """-> (accepted, media, id, tmdb_title, tmdb_year, popularity, how); durably cached per title/service."""
    ver = 'arr3' if FRANCHISE.match(row['title']) else 'arr2'
    key = f"{ver}:{row.get('media')}|{norm(row['title'])}|{row.get('year')}|{row.get('season')}|{row['service']}"
    return tuple(build.stable(key, lambda: list(_resolve(row, present))))


def _search(media, query):
    try:
        return build.tmdb('/search/movie' if media == 'movie' else '/search/tv', query=query,
                          include_adult='false').get('results', [])
    except (RuntimeError, SystemExit):
        return []


def _resolve(row, present):
    medias = [row['media']] if row.get('media') else ['movie', 'series']
    on = lambda m, r: present(row['service'], m, r['id'])
    cands = []
    for media in medias:
        for r in _search(media, row['title'])[:20]:
            names = {norm(r.get('title') or r.get('name') or ''), norm(r.get('original_title') or r.get('original_name') or '')}
            cands.append((media, r, norm(row['title']) in names))
    exact = [(m, r) for m, r, e in cands if e]
    fp = FRANCHISE.match(row['title'])
    if not exact and fp:
        # "Star Wars: The Mandalorian and Grogu" -> TMDB "The Mandalorian and Grogu": accepted only as the single exact
        # match that is also on the service
        short = row['title'][fp.end():]
        alt = [(media, r) for media in medias for r in _search(media, short)[:20]
               if norm(r.get('title') or r.get('name') or '') == norm(short)]
        onsv = [(m, r) for m, r in alt[:6] if on(m, r)]
        if len(onsv) == 1:
            m, r = onsv[0]
            return (True, m, r['id'], r.get('title') or r.get('name'), _year(r), r.get('popularity') or 0,
                    f'exact without franchise prefix "{fp.group(0).strip()}" + on service')
    y = row.get('year')
    if y:
        exact = [(m, r) for m, r in exact if _year(r) is None or abs(_year(r) - y) <= 1] or \
                ([] if any(_year(r) for _, r in exact) else exact)
    if row.get('season') and row['season'] > 1:        # an existing show: it aired before this arrival
        exact = [(m, r) for m, r in exact if m == 'series' and (r.get('first_air_date') or '9') < row['date']]
    feats = []
    for m, r in exact:
        if m == 'movie':
            try:
                rt = build.stable(f"runtime:{r['id']}", lambda: build.tmdb(f"/movie/{r['id']}").get('runtime') or 0)
            except RuntimeError:
                rt = 0
            if rt and rt < 40 and not row.get('short'):
                continue
        feats.append((m, r))
    exact = feats
    if not exact:
        best = max(cands, key=lambda c: c[1].get('popularity') or 0, default=None)
        if best:
            m, r, _ = best
            return (False, m, r['id'], r.get('title') or r.get('name'), _year(r), r.get('popularity') or 0,
                    'CHECK: no exact title' + (' within year' if y else ''))
        return (False, row.get('media'), None, '', None, 0, 'CHECK: no TMDB result')
    if len(exact) == 1:
        m, r = exact[0]
        return (True, m, r['id'], r.get('title') or r.get('name'), _year(r), r.get('popularity') or 0,
                'exact' + (' + year' if y else ''))
    onsv = [(m, r) for m, r in exact[:6] if on(m, r)]
    if len(onsv) == 1:
        m, r = onsv[0]
        return (True, m, r['id'], r.get('title') or r.get('name'), _year(r), r.get('popularity') or 0,
                f'exact + on service ({len(exact)} candidates)')
    pool = sorted(onsv or exact, key=lambda c: -(c[1].get('vote_count') or 0))
    m, r = pool[0]
    return (False, m, r['id'], r.get('title') or r.get('name'), _year(r), r.get('popularity') or 0,
            'CHECK: ' + '; '.join(f"{mm}:{rr['id']} {_year(rr)} v{rr.get('vote_count', 0)}" for mm, rr in pool[:5])
            + f' ({len(onsv)} on service)')


# ---------------------------------------------------------------- state merge -------------------------------------
def load_state(state_dir):
    f = Path(state_dir) / 'first_seen.json.gz'
    if f.exists():
        with gzip.open(f, 'rt', encoding='utf-8') as fh:
            return json.load(fh)
    return None


def merge(signals, key, rec, today):
    """Record one accepted row as an observation of the title's arrivals (see module doc)."""
    date, prec, src, pop, kind, season = rec
    v = signals.get(key)
    if not isinstance(v, dict):                     # new key (or an old-format entry: rebuilt from today's rows)
        v = signals[key] = {'o': [], 'p': 0, 'k': kind, 's': 'announced'}
    ob = [date, prec, src, season or 0]
    if not any(o[:4] == ob for o in v['o']):
        v['o'].append(ob + [int(date > today)])
    v['p'] = max(v['p'], round(pop, 3))
    if kind == 'title':
        v['k'] = 'title'


def events(obs):
    """Group observations into arrival events (date order): <= NEW_EVENT_GAP days apart and not different seasons."""
    out = []
    for o in sorted(obs):
        cur = out[-1] if out else None
        if cur and (dt.date.fromisoformat(o[0]) - dt.date.fromisoformat(cur[-1][0])).days <= NEW_EVENT_GAP and                 not (o[3] and cur[-1][3] and o[3] != cur[-1][3]):
            cur.append(o)
        else:
            out.append([o])
    return out


def current_event(v, today):
    """(date, precision, sources, needs_confirmation, future) of the latest event that has started; the earliest
    scheduled one when none has started yet."""
    evs = events(v['o'])
    started = [e for e in evs if e[0][0] <= today]
    ev = started[-1] if started else evs[0]
    best = min(ev, key=lambda o: (PREC[o[1]], o[0]))
    return (best[0], best[1], sorted({o[2] for o in ev}), all(o[4] for o in ev), not started)


ANNOUNCED_DAYS = 7              # F1: a week/month-precision (roundup) arrival not seen on the service: first 7 days only;
#                                 day-dated items from a published schedule are trusted for the whole window
NETFLIX_NETWORK = 213
_orphan_memo = {}


def netflix_orphan(media, tid):
    """F2 (narrow): a series whose TMDB networks include Netflix and for which TMDB has NO US watch-provider data at all
    counts as on Netflix - JustWatch never lists some Netflix originals (STEEL BALL RUN, Unveil &TEAM), so the logger
    can never confirm them. Titles with US provider data that lacks Netflix stay unconfirmed. Networks are durably
    cached (1 request per title, ever); provider data is checked once per run, only for titles F1 would drop."""
    if media != 'series':
        return False
    if tid not in _orphan_memo:
        try:
            nets = build.stable(f'tvnet:{tid}', lambda: [n['id'] for n in build.tmdb(f'/tv/{tid}').get('networks') or []])
            _orphan_memo[tid] = NETFLIX_NETWORK in nets and not \
                build.tmdb(f'/tv/{tid}/watch/providers').get('results', {}).get('US')
        except RuntimeError:                           # TMDB error: unknown = not confirmed (the guards still apply)
            _orphan_memo[tid] = False
    return _orphan_memo[tid]


def refresh(state, today, present=None):
    """Recompute every stored signal's status from today's presence and prune old observations; returns
    {provider: [candidate dicts]} = the arrivals build.py may show today: the current event dated in the last WINDOW
    days, and an event known only from schedules published before its date only once confirmed on the service."""
    lo = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW)).isoformat()
    cut = (dt.date.fromisoformat(today) - dt.timedelta(days=PRUNE_DAYS)).isoformat()
    out = {}
    for svc, prov in (state or {}).get('providers', {}).items():
        sig = prov.get('signals') or {}
        for k in list(sig):
            v = sig[k]
            if not isinstance(v, dict):
                del sig[k]                          # old format: re-recorded from the sources
                continue
            v['o'] = [o for o in v['o'] if o[0] >= cut]
            if not v['o']:
                del sig[k]
        cands = []
        for k, v in sig.items():
            media, tid = k.split(':')
            if present:
                v['s'] = 'confirmed' if present(svc, media, int(tid)) else 'announced'
            date, prec, srcs, needs_conf, future = current_event(v, today)
            if future or not lo <= date <= today or (needs_conf and v['s'] != 'confirmed'):
                continue
            status = v['s']
            if status != 'confirmed' and prec != 'day' and                     (dt.date.fromisoformat(today) - dt.date.fromisoformat(date)).days > ANNOUNCED_DAYS:
                if not (svc == 'netflix' and netflix_orphan(media, int(tid))):
                    continue                        # F1: a roundup item never confirmed after a week -> not shown
                status = 'confirmed (Netflix network, no provider data)'                  # F2
            cands.append({'key': k, 'date': date, 'precision': prec, 'sources': srcs, 'status': status,
                          'popularity': v['p'], 'kind': v['k']})
        cands.sort(key=lambda c: (c['date'], -PREC[c['precision']], c['popularity']), reverse=True)
        out[svc] = cands
    return out


def write_candidates(state, state_dir, today, present=None):
    cands = refresh(state, today, present)
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    (Path(state_dir) / 'signal_candidates.json').write_text(json.dumps({'date': today, 'providers': cands},
                                                                       separators=(',', ':')), encoding='utf-8')
    return cands


# ---------------------------------------------------------------- main --------------------------------------------
def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else 'arrivals')
    out.mkdir(parents=True, exist_ok=True)
    backfill = '--backfill' in sys.argv
    today_d = (dt.date.fromisoformat(sys.argv[sys.argv.index('--today') + 1]) if '--today' in sys.argv else
               dt.date.fromisoformat(os.environ['SD_TODAY']) if os.environ.get('SD_TODAY') else dt.date.today())
    today = today_d.isoformat()
    since = today_d - dt.timedelta(days=BACKFILL_LOOKBACK if backfill else DAILY_LOOKBACK)
    months = sorted({(since + dt.timedelta(days=d)).strftime('%B').lower() for d in range((today_d - since).days + 40)},
                    key=lambda m: MONTHS[m])
    _WIN.update(lo=since, hi=today_d + dt.timedelta(days=45))
    t0 = time.monotonic()
    scan_http_cache()                                   # old body-bearing cache out; only clean parsed records stay
    state = load_state(STATE_DIR)
    pids = {s: p for s, p in providers().items() if s not in DROPPED}
    present = Presence(state, pids)
    stats, rows = {}, []
    for name, fn in SOURCES.items():
        try:
            r = fn(since, backfill, stats) if name == 'won' else fn(months, backfill, stats)
        except Exception as e:                          # a broken source is a health warning, never a crash
            stats.setdefault(name, {'posts': 0, 'errors': []})['errors'].append(f'{type(e).__name__}: {str(e)[:200]}')
            r = []
        rows += r
        print(f"{name}: {stats.get(name, {}).get('posts', 0)} posts, {len(r)} rows", flush=True)
    rows = [r for r in rows if r['service'] not in DROPPED]
    todo = [r for r in rows if r['service'] in pids]
    for r in rows:
        if r['service'] not in pids:
            r.update(accepted=False, how='service not in spec')
    with ThreadPoolExecutor(6) as pool:
        res = list(pool.map(lambda r: resolve(r, present), todo))
    for r, (ok, m, i, tt, ty, pop, how) in zip(todo, res):
        r.update(accepted=ok, tmdb_media=m, tmdb_id=i, tmdb_title=tt, tmdb_year=ty, popularity=pop or 0, how=how)
    build.save_cache()
    prune_http_cache()
    acc = [r for r in rows if r.get('accepted')]
    # merge into the durable state (per provider); a missing state (logger failed) = today's signals only
    work = state if state is not None else {'providers': {}}
    for r in sorted(acc, key=lambda r: (PREC[r['precision']], r['date'])):
        prov = work['providers'].setdefault(r['service'], {'items': {}})
        merge(prov.setdefault('signals', {}), f"{r['tmdb_media']}:{r['tmdb_id']}",
              (r['date'], r['precision'], r['source'], r['popularity'],
               'season' if (r.get('season') or 0) > 1 else 'title', r.get('season')), today)
    cands = refresh(work, today, present)
    for r in acc:
        r['on_service'] = present(r['service'], r['tmdb_media'], r['tmdb_id'])
    if state is not None:
        tmp = STATE_DIR / 'first_seen.json.gz.tmp'
        with gzip.open(tmp, 'wt', encoding='utf-8') as fh:
            json.dump(state, fh, separators=(',', ':'))
        tmp.replace(STATE_DIR / 'first_seen.json.gz')   # never a half-written state
        (STATE_DIR / 'signal_candidates.json').write_text(json.dumps({'date': today, 'providers': cands},
                                                                     separators=(',', ':')), encoding='utf-8')
    # today's merged signals, in the shape of the accuracy re-score (services -> key -> record)
    arrivals = {}
    for svc, prov in work['providers'].items():
        for k, v in (prov.get('signals') or {}).items():
            date, prec, srcs, needs_conf, future = current_event(v, today)
            arrivals.setdefault(svc, {})[k] = {'date': date, 'precision': prec, 'source': srcs[0], 'sources': srcs,
                                              'status': v['s'], 'popularity': v['p'], 'kind': v['k'],
                                              **({'future': True} if future else {})}
    (out / 'arrivals.json').write_text(json.dumps({'generated': today, 'since': since.isoformat(),
                                                   'services': arrivals}, indent=1), encoding='utf-8')
    cols = ['source', 'service', 'date', 'precision', 'title', 'year', 'season', 'media', 'accepted', 'how',
            'tmdb_media', 'tmdb_id', 'tmdb_title', 'tmdb_year', 'on_service', 'url']
    for name, sel in (('rows.csv', rows), ('check_rows.csv', [r for r in rows if not r.get('accepted')])):
        with open(out / name, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            w.writeheader()
            w.writerows(sel)
    per = {s: {'rows': 0, 'accepted': 0, 'check': 0, 'confirmed': 0} for s in SOURCES}
    for r in rows:
        p = per[r['source']]
        p['rows'] += 1
        p['accepted' if r.get('accepted') else 'check'] += 1
        p['confirmed'] += 1 if r.get('on_service') else 0
    with open(out / 'parse_stats.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['source', 'posts', 'rows', 'accepted', 'resolved_pct', 'check', 'confirmed_on_service', 'errors'])
        for s, p in per.items():
            st = stats.get(s, {})
            w.writerow([s, st.get('posts', 0), p['rows'], p['accepted'], f"{p['accepted'] / max(1, p['rows']):.0%}",
                        p['check'], p['confirmed'], ' | '.join(st.get('errors', []) + st.get('notes', []))[:500]])
    # warnings = real failures only (they open the health issue); known per-post parse gaps are notes
    warnings = [f'{s}: {e}' for s, st in stats.items() for e in st.get('errors', [])]
    warnings += [f'{s}: the feed / sitemap returned no items (source down or format changed)' for s in SOURCES
                 if stats.get(s, {}).get('items', 0) == 0 and not stats.get(s, {}).get('errors')]
    warnings += [f'{s}: {stats[s]["posts"]} posts but 0 rows (format changed?)' for s in SOURCES
                 if stats.get(s, {}).get('posts', 0) > 0 and per[s]['rows'] == 0]
    summary = {'date': today, 'mode': 'backfill' if backfill else 'daily', 'since': since.isoformat(),
               'state': 'merged' if state is not None else 'missing (logger failed): signals not stored',
               'sources': {s: {'items': stats.get(s, {}).get('items', 0), 'posts': stats.get(s, {}).get('posts', 0),
                               **per[s], 'notes': stats.get(s, {}).get('notes', [])} for s in SOURCES},
               'candidates': {s: len(c) for s, c in cands.items()}, 'tmdb_requests': build._calls[0],
               'fetch': fetch_stats, 'seconds': round(time.monotonic() - t0), 'warnings': warnings}
    (out / 'arrivals_summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ('mode', 'state', 'tmdb_requests', 'seconds', 'candidates')}), flush=True)
    for w in warnings:
        print('WARNING', w)


if __name__ == '__main__':
    main()
