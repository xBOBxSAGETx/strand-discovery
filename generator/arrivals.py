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
Last-Modified conditional GET (cache/http/arrivals). Only title / date / service / TMDB id / source URL are kept -
never the sources' text; nothing is republished except TMDB ids and dates inside our catalogs. Private,
non-commercial use.

Every row is resolved to TMDB by exact title (+ year / media / season, films >= 40 min, on-service tie-break); anything
not exact or ambiguous is a CHECK row and is not used. Later seasons of returning shows count (copilot ruling).
Signals are merged into the durable first_seen state (<SD_STATE>/first_seen.json.gz, per provider "signals"):
  key "movie:ID" / "series:ID" -> [date, precision, sources, status, was_future, popularity, kind]
  - one arrival event keeps its best-precision date (day > week > month), the earliest among equals; a signal more
    than 60 days after the stored one is a new arrival (the title left and came back) and replaces it
  - status is recomputed daily: "confirmed" when the logger saw the title on the service today, else "announced"
  - a signal first recorded with a future date is shown only once its date has passed AND it is confirmed
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
NEW_EVENT_GAP = 60              # a signal this much later than the stored one = a new arrival (re-arrival)
PRUNE_DAYS = 180
DAILY_LOOKBACK = 50             # daily run: posts from the last 50 days (the window + a margin)
BACKFILL_LOOKBACK = 92          # --backfill: the last 3 months
PREC = {'day': 0, 'week': 1, 'month': 2}

# ---------------------------------------------------------------- polite HTTP ------------------------------------
UA = 'strand-discovery/1.0 (personal non-commercial; +https://github.com/xBOBxSAGETx/strand-discovery)'
HTTP_CACHE = Path(os.environ.get('SD_HTTP_CACHE', 'cache/http')) / 'arrivals'
DELAY = {'film-book.com': 5.0}          # robots.txt Crawl-delay
DEFAULT_DELAY = 1.1
_last = {}
fetch_stats = {'requests': 0, 'cache_fresh': 0, 'not_modified': 0, 'errors': 0}


def _paths(url):
    h = hashlib.sha256(url.encode()).hexdigest()[:24]
    return HTTP_CACHE / f'{h}.body', HTTP_CACHE / f'{h}.meta.json'


def get(url, max_age=20.0):
    """(status, text, from_cache). Cached copies younger than max_age hours are reused; older ones are revalidated
    (304 -> the cached body). A network error falls back to a stale copy if there is one."""
    q = urllib.parse.urlparse(url)
    if re.search(r'(^|&)s=', q.query or ''):
        raise ValueError(f'search URLs are not allowed by robots.txt: {url}')
    body_p, meta_p = _paths(url)
    meta = json.loads(meta_p.read_text(encoding='utf-8')) if meta_p.exists() else None
    if meta and body_p.exists() and time.time() - meta['fetched'] < max_age * 3600:
        fetch_stats['cache_fresh'] += 1
        return meta['status'], body_p.read_text(encoding='utf-8'), True
    host = q.netloc.lower().removeprefix('www.')
    wait = _last.get(host, 0) + DELAY.get(host, DEFAULT_DELAY) - time.time()
    if wait > 0:
        time.sleep(wait)
    headers = {'User-Agent': UA, 'Accept-Encoding': 'identity'}
    if meta and body_p.exists():
        if meta.get('etag'):
            headers['If-None-Match'] = meta['etag']
        if meta.get('last_modified'):
            headers['If-Modified-Since'] = meta['last_modified']
    fetch_stats['requests'] += 1
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
            status, text = r.status, r.read().decode('utf-8', errors='replace')
            etag, lm = r.headers.get('ETag'), r.headers.get('Last-Modified')
    except urllib.error.HTTPError as e:
        _last[host] = time.time()
        if e.code == 304 and meta:
            fetch_stats['not_modified'] += 1
            meta['fetched'] = time.time()
            meta_p.write_text(json.dumps(meta), encoding='utf-8')
            return 200, body_p.read_text(encoding='utf-8'), True
        fetch_stats['errors'] += 1
        return e.code, '', False
    except Exception:                          # network error: a stale copy beats nothing
        _last[host] = time.time()
        fetch_stats['errors'] += 1
        if meta and body_p.exists():
            return meta['status'], body_p.read_text(encoding='utf-8'), True
        return 0, '', False
    _last[host] = time.time()
    HTTP_CACHE.mkdir(parents=True, exist_ok=True)
    body_p.write_text(text, encoding='utf-8')
    meta_p.write_text(json.dumps({'url': url, 'status': status, 'etag': etag, 'last_modified': lm,
                                  'fetched': time.time()}), encoding='utf-8')
    return status, text, False


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
               strip_prefix=False):
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
        c = clean(ln)
        if not c:
            continue
        if c['media'] is None and media:
            c['media'] = media
        for sv in svcs:
            rows.append({**c, 'service': sv, 'date': ln_date.isoformat(), 'precision': ln_prec,
                         'source': source, 'url': url, 'raw': ln[:120]})
    return rows


# ---------------------------------------------------------------- sources -----------------------------------------
def src_won(since, backfill, stats):
    rows = []
    st, xml, _ = get('https://www.whats-on-netflix.com/whats-new/feed/')
    stats['won'] = {'posts': 0, 'status': st, 'errors': [] if st == 200 else [f'feed HTTP {st}']}
    for it in (feed_items(xml) if st == 200 else []):
        if not it['pub'] or it['pub'] < since:
            continue
        t = it['title']
        weekly = re.search(r'this week', t, re.I) and re.search(r'new', t, re.I) and 'UK' not in t
        first = re.search(r'(adds|added).*for (\w+) 1st', t, re.I)
        if not (weekly or first):
            continue
        stats['won']['posts'] += 1
        lines, on, media = text_lines(it['content']), False, None
        for ln in lines:
            if re.search(r'full list of new releases', ln, re.I):
                on = True
                continue
            if not on:
                continue
            if ln.startswith('##h3'):
                break                                   # next section (Top 10 ...)
            if ln.startswith('##h4'):
                media = 'series' if re.search(r'tv|series|shows', ln, re.I) else 'movie'
                continue
            if not ln.startswith('- '):
                continue
            c = clean(ln[2:])
            if not c:
                continue
            c['media'] = c['media'] if c['season'] else media
            if weekly:                                  # no per-day dates in the roundup: the week's START
                date, prec = it['pub'] - dt.timedelta(days=6), 'week'
            else:
                date, prec = dt.date(it['pub'].year, MONTHS[first.group(2).lower()], 1), 'day'
            rows.append({**c, 'service': 'netflix', 'date': date.isoformat(), 'precision': prec,
                         'source': 'won', 'url': it['link'], 'raw': ln[2:120]})
    return rows


def post_body(t):
    i = t.find('entry-content')
    if i < 0:
        i = t.find('<article')
    j = t.find('</article>', i)
    return t[i:j if j > 0 else len(t)]


def sitemap_posts(site, index, n=3):
    """Post URLs from the newest n post sitemaps listed in a WordPress sitemap index."""
    st, xml, _ = get(f'{site}/{index}', max_age=20)
    maps = [u for u in re.findall(r'<loc>(.*?)</loc>', xml) if re.search(r'post-sitemap\d*\.xml', u)] if st == 200 else []
    maps.sort(key=lambda u: int((re.search(r'post-sitemap(\d*)\.xml', u).group(1) or 1)))
    urls = []
    for u in maps[-n:]:
        s, x, _ = get(u, max_age=20)
        urls += re.findall(r'<loc>(.*?)</loc>', x) if s == 200 else []
    return urls


def src_vt(months, backfill, stats):
    rows, posts, errs = [], 0, []
    if backfill:
        urls = sitemap_posts('https://www.vitalthrills.com', 'sitemap_index.xml')
    else:
        st, xml, _ = get('https://www.vitalthrills.com/tag/streaming-schedule/feed/')
        urls = [it['link'] for it in feed_items(xml)] if st == 200 else []
        if st != 200:
            errs.append(f'tag feed HTTP {st}')
    for u in dict.fromkeys(urls):
        m = re.search(r'vitalthrills\.com/([a-z-]+?)-(' + '|'.join(months) + r')-(20\d\d)/?$', u)
        if not m or m.group(1) not in VT_SERVICES:
            continue
        svc, mon, year = VT_SERVICES[m.group(1)], MONTHS[m.group(2)], int(m.group(3))
        if not in_window(year, mon):
            continue
        s, t, _ = get(u, max_age=72)
        if s != 200:
            errs.append(f'{u}: HTTP {s}')
            continue
        posts += 1
        r = dated_rows(text_lines(post_body(t)), svc, year, mon, 'vt', u, start=r'^(##h2 )?.*(schedules?|titles)$',
                       stop=r'^#\w|^tags?:|related posts|^##h2 (sports|live|fast channels|espn)', prefix=svc == 'disney+hulu')
        if not r:
            errs.append(f'{u}: 0 rows (format change?)')
        rows += r
    stats['vt'] = {'posts': posts, 'errors': errs}
    return rows


def src_wodp(months, backfill, stats):
    rows, posts, errs = [], 0, []
    if backfill:
        urls = sitemap_posts('https://whatsondisneyplus.com', 'sitemap.xml')          # its index (no sitemap_index)
    else:
        st, xml, _ = get('https://whatsondisneyplus.com/feed/')
        urls = [it['link'] for it in feed_items(xml)] if st == 200 else []
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
        s, t, _ = get(u, max_age=72)
        if s != 200:
            errs.append(f'{u}: HTTP {s}')
            continue
        posts += 1
        r = dated_rows(text_lines(post_body(t)), svc, year, mon, 'wodp', u, start=None,
                       stop=r'looking forward to|for the latest|let me know', bullets_only=True, heading_marker=True,
                       h3_titles=True, drop_prefixes=('hulu', 'espn') if svc == 'disneyplus' else ('espn',))
        if not r:
            errs.append(f'{u}: 0 rows (format change?)')
        rows += r
    stats['wodp'] = {'posts': posts, 'errors': errs}
    return rows


def src_fb(months, backfill, stats):
    rows, posts, errs = [], 0, []
    feeds = ['https://film-book.com/category/streaming-schedule/feed/']        # ~120 posts: 3+ months (no paging)
    items = []
    for f in feeds:
        st, xml, _ = get(f)
        if st != 200:
            errs.append(f'feed HTTP {st}' if f == feeds[0] else f'{f}: HTTP {st}')
            continue
        items += feed_items(xml)
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
        s, t, _ = get(it['link'], max_age=72)
        if s != 200:
            errs.append(f"{it['link']}: HTTP {s}")
            continue
        posts += 1
        i = t.find('<article')
        r = dated_rows(text_lines(t[i:]), svc, int(m.group(3)), MONTHS[m.group(2).lower()], 'fb', it['link'],
                       start=r'^##h3 .*schedule$', stop=r'^(more .* streaming|view all streaming|share this|about the author|'
                                                      r'tags:|leave a (reply|comment)|##h[23] .*(leav(?:ing)|coming soon)|'
                                                      r'back to top|you may also like|recent posts|popular posts|'
                                                      r'movie trailer|^contest$|newsletter|trending on filmbook|'
                                                      r'latest video|^flickr$|^tags$|^subscribe$|delivered to your inbox)',
                       strip_prefix=svc in ('disneyplus', 'hulu'), heading_year=False)
        r = [x for x in r if not re.search(r'schedule:|streaming release|^advertisement', x['raw'], re.I)]
        if not r:
            errs.append(f"{it['link']}: 0 dated rows (month-level format, not parsed)")
        rows += r
    stats['fb'] = {'posts': posts, 'errors': errs}
    return rows


def src_plex(months, backfill, stats):
    rows, posts, errs = [], 0, []
    st, xml, _ = get('https://www.plex.tv/blog/feed/')
    if st != 200:
        errs.append(f'feed HTTP {st}')
    for it in (feed_items(xml) if st == 200 else []):
        m = re.match(r'new on plex in (\w+)', it['title'], re.I)
        if not m or m.group(1).lower() not in months:
            continue
        mon = MONTHS[m.group(1).lower()]
        year = it['pub'].year + (1 if mon < it['pub'].month - 6 else 0)
        body = it['content'] or get(it['link'], max_age=72)[1]
        posts += 1
        r = dated_rows(text_lines(body), 'plex', year, mon, 'plex', it['link'], start=r'^##h\d new on plex in',
                       stop=r'^##h\d', default_date=(dt.date(year, mon, 1), 'month'))
        if not r:
            errs.append(f"{it['link']}: 0 rows (format change?)")
        rows += r
    stats['plex'] = {'posts': posts, 'errors': errs}
    return rows


def prune_http_cache(days=120):
    """Drop cached pages not fetched for `days` (the durable cache is saved every run; posts that old are unused)."""
    cut = time.time() - days * 86400
    for meta_p in HTTP_CACHE.glob('*.meta.json'):
        try:
            if json.loads(meta_p.read_text(encoding='utf-8'))['fetched'] < cut:
                meta_p.with_name(meta_p.name.replace('.meta.json', '.body')).unlink(missing_ok=True)
                meta_p.unlink()
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
            if rt and rt < 40 and not re.search(r'short', row.get('raw', ''), re.I):
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
    """Merge one accepted row into a provider's stored signals (see module doc)."""
    date, prec, src, pop, kind = rec
    cur = signals.get(key)
    if cur and dt.date.fromisoformat(date) - dt.date.fromisoformat(cur[0]) > dt.timedelta(days=NEW_EVENT_GAP):
        cur = None                                  # a re-arrival: a new event replaces the old one
    if not cur:
        signals[key] = [date, prec, [src], 'announced', int(date > today), round(pop, 3), kind]
        return
    if PREC[prec] < PREC[cur[1]] or (PREC[prec] == PREC[cur[1]] and date < cur[0]):
        cur[0], cur[1] = date, prec
        cur[4] = cur[4] and int(date > today)
    if src not in cur[2]:
        cur[2] = sorted(cur[2] + [src])
    cur[5] = max(cur[5], round(pop, 3))
    if kind == 'title':
        cur[6] = 'title'


def refresh(state, today, present=None):
    """Recompute every stored signal's status from today's presence and prune old ones; returns
    {provider: [candidate dicts]} = the signals build.py may show today (dated in the last WINDOW days, and a signal
    first recorded as future only once confirmed)."""
    lo = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW)).isoformat()
    cut = (dt.date.fromisoformat(today) - dt.timedelta(days=PRUNE_DAYS)).isoformat()
    out = {}
    for svc, prov in (state or {}).get('providers', {}).items():
        sig = prov.get('signals') or {}
        for k in [k for k, v in sig.items() if v[0] < cut]:
            del sig[k]
        cands = []
        for k, v in sig.items():
            media, tid = k.split(':')
            if present:
                v[3] = 'confirmed' if present(svc, media, int(tid)) else 'announced'
            if lo <= v[0] <= today and (not v[4] or v[3] == 'confirmed'):
                cands.append({'key': k, 'date': v[0], 'precision': v[1], 'sources': v[2], 'status': v[3],
                              'popularity': v[5], 'kind': v[6]})
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
               'season' if (r.get('season') or 0) > 1 else 'title'), today)
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
            arrivals.setdefault(svc, {})[k] = {'date': v[0], 'precision': v[1], 'source': v[2][0], 'sources': v[2],
                                              'status': v[3], 'popularity': v[5], 'kind': v[6],
                                              **({'future': True} if v[0] > today else {})}
    (out / 'arrivals.json').write_text(json.dumps({'generated': today, 'since': since.isoformat(),
                                                   'services': arrivals}, indent=1), encoding='utf-8')
    cols = ['source', 'service', 'date', 'precision', 'raw', 'title', 'year', 'season', 'media', 'accepted', 'how',
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
                        p['check'], p['confirmed'], ' | '.join(st.get('errors', []))[:500]])
    warnings = [f'{s}: {e}' for s, st in stats.items() for e in st.get('errors', [])]
    # a normally productive source with nothing today = a format change or an outage (the feeds always hold posts)
    warnings += [f'{s}: 0 posts parsed (source down or format changed)' for s in SOURCES
                 if stats.get(s, {}).get('posts', 0) == 0]
    warnings += [f'{s}: {per[s]["rows"]} rows from {stats[s]["posts"]} posts (format changed?)' for s in SOURCES
                 if stats.get(s, {}).get('posts', 0) > 0 and per[s]['rows'] == 0]
    summary = {'date': today, 'mode': 'backfill' if backfill else 'daily', 'since': since.isoformat(),
               'state': 'merged' if state is not None else 'missing (logger failed): signals not stored',
               'sources': {s: {'posts': stats.get(s, {}).get('posts', 0), **per[s]} for s in SOURCES},
               'candidates': {s: len(c) for s, c in cands.items()}, 'tmdb_requests': build._calls[0],
               'fetch': fetch_stats, 'seconds': round(time.monotonic() - t0), 'warnings': warnings}
    (out / 'arrivals_summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ('mode', 'state', 'tmdb_requests', 'seconds', 'candidates')}), flush=True)
    for w in warnings:
        print('WARNING', w)


if __name__ == '__main__':
    main()
