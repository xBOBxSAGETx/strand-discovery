"""Leaving Soon: titles about to leave Netflix, Hulu, Prime Video, HBO Max and Disney+ (US) -> leaving.json.

  python leaving.py <out dir>            (daily; writes <out dir>/leaving.json + parse_stats.csv, check_rows.csv,
                                           netflix_crosscheck.csv)

Sources (read politely: identifiable User-Agent, <= 1 request/s per host, on-disk HTTP cache with conditional GET;
only title / date / service / TMDB id / source URL are kept - never the sources' text):
  won   What's on Netflix "What's Leaving Netflix in <Month> <Year>" posts (US only; UK posts skipped), from the
        https://www.whats-on-netflix.com/leaving-soon/feed/ RSS. Day headings ("... Leaving Netflix on October 1st")
        give the leave date; items marked * are not officially confirmed yet (kept, status "unconfirmed").
  wil   whatisleaving.com (robots: Allow /). The site is a single-page app; its titles and dates come from the site's
        own public read API (Supabase REST, the anonymous key the site ships in its JavaScript - read from the page
        at run time, never stored here). Platforms netflix, hulu, prime, max, disney.
Every title is resolved to TMDB. A whatisleaving row carries the site's TMDB id: it is accepted only if TMDB's own
title (or original title) and year agree; otherwise, and for every What's on Netflix row, the title is searched on
TMDB. Only an exact title + year match is accepted; anything else is a CHECK row and stays off the card, unless the
other source resolved the same service entry to the same TMDB id (cross-confirmed). Films need runtime >= 40 min.
A title is kept only while TMDB watch providers (JustWatch data, US, flatrate) still list it on the service, and
only while its leave date is after today. Card order: leave date ascending, then popularity.

leaving_card(service_slug, today, path) -> Stremio metas (same shape as build.preview, incl. the private '_g').
"""
import csv, datetime as dt, email.utils, hashlib, html, json, os, re, sys, threading, time, unicodedata, urllib.error
import urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = 'strand-discovery/1.0 (personal non-commercial; +https://github.com/xBOBxSAGETx/strand-discovery)'
HTTP_CACHE = Path(os.environ.get('SD_HTTP_CACHE', 'cache/http'))
SERVICES = ['netflix', 'hulu', 'prime-video', 'hbo-max', 'disneyplus']
WIL_PLATFORM = {'netflix': 'netflix', 'hulu': 'hulu', 'prime': 'prime-video', 'max': 'hbo-max', 'disney': 'disneyplus'}
WON_FEED = 'https://www.whats-on-netflix.com/leaving-soon/feed/'
WIL_HOME = 'https://whatisleaving.com/'
MIN_FILM_RUNTIME = 40
MONTHS = {m: i for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                      'september', 'october', 'november', 'december'], 1)}
_host_last, _host_lock = {}, threading.Lock()
_build = None


def build():
    """generator/build.py, imported lazily (its durable cache location comes from SD_CACHE at import time)."""
    global _build
    if _build is None:
        import __main__                      # called from the generator itself: reuse it (one cache, one throttle)
        if Path(getattr(__main__, '__file__', '') or '.').name == 'build.py':
            _build = __main__
        else:
            sys.path.insert(0, str(HERE))
            import build as b
            _build = b
    return _build


# ---- polite HTTP ---------------------------------------------------------------------------------------------------
def fetch(url, headers=None, cache=True):
    """GET with an identifiable UA, <= 1 request/s per host, and an on-disk cache revalidated with ETag /
    Last-Modified (a 304 reuses the stored body). Returns the body as text."""
    host = urllib.parse.urlsplit(url).netloc
    key = hashlib.sha256((url + json.dumps(headers or {}, sort_keys=True)).encode()).hexdigest()[:32]
    meta_f, body_f = HTTP_CACHE / f'{key}.json', HTTP_CACHE / f'{key}.body'
    meta = json.loads(meta_f.read_text(encoding='utf-8')) if cache and meta_f.exists() and body_f.exists() else {}
    h = {'User-Agent': UA, **(headers or {})}
    if meta.get('etag'):
        h['If-None-Match'] = meta['etag']
    if meta.get('last_modified'):
        h['If-Modified-Since'] = meta['last_modified']
    with _host_lock:
        wait = _host_last.get(host, 0) + 1.05 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
            body = r.read().decode('utf-8', errors='replace')
            meta = {'url': url, 'etag': r.headers.get('ETag'), 'last_modified': r.headers.get('Last-Modified'),
                    'fetched': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}
    except urllib.error.HTTPError as e:
        if e.code == 304 and meta:
            return body_f.read_text(encoding='utf-8')
        raise RuntimeError(f'HTTP {e.code} on {host}') from None
    if cache:
        HTTP_CACHE.mkdir(parents=True, exist_ok=True)
        body_f.write_text(body, encoding='utf-8')
        meta_f.write_text(json.dumps(meta), encoding='utf-8')
    return body


# ---- What's on Netflix -----------------------------------------------------------------------------------------------
MONTHLY = re.compile(r"^(?:What.s|Everything) Leaving Netflix in (\w+) (\d{4})$", re.I)
DAY_HEAD = re.compile(r"Leaving Netflix on (\w+) (\d{1,2})(?:st|nd|rd|th)?\b", re.I)


def text(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s)).replace('\xa0', ' ').strip()


def parse_won_item(raw, section):
    """'Title (2013)', 'Show (Seasons 1-5)*', 'X (2021) – Netflix Original Removal' ->
    (title, year, media hint, confirmed)."""
    s = raw.strip()
    confirmed = not s.endswith('*')
    s = s.rstrip('*').strip()
    s = re.sub(r'\s+[–-]\s+[^()]*Removal$', '', s).strip()
    s = re.sub(r'\s*\(Multiple Versions\)', '', s, flags=re.I).strip()
    media = {'movies': 'movie', 'series': 'series'}.get(section)
    if re.search(r'\((?:Seasons?\s[\d\s,&-]+|Multiple Seasons|Limited Series|Volumes?\s[\d\s-]+)\)\s*$', s, re.I):
        s = re.sub(r'\s*\((?:Seasons?\s[\d\s,&-]+|Multiple Seasons|Limited Series|Volumes?\s[\d\s-]+)\)\s*$', '', s,
                   flags=re.I)
        media = 'series'
    year = None
    m = re.search(r'\s*\(((?:19|20)\d\d)\)\s*$', s)
    if m:
        year, s = int(m.group(1)), s[:m.start()].strip()
    return s.strip(' *'), year, media, confirmed


def won_rows(stats):
    feed = fetch(WON_FEED)
    rows = []
    for item in re.findall(r'<item>(.*?)</item>', feed, re.S):
        title = text(re.search(r'<title>(.*?)</title>', item, re.S).group(1))
        link = text(re.search(r'<link>(.*?)</link>', item, re.S).group(1))
        m = MONTHLY.match(title)
        if not m or 'uk' in title.lower().split():
            continue
        year = int(m.group(2))
        body = re.search(r'<content:encoded><!\[CDATA\[(.*?)\]\]></content:encoded>', item, re.S)
        if not body:
            stats['errors'].append(f'no body: {link}')
            continue
        stats['posts'] += 1
        date, section = None, None
        for tok in re.finditer(r'<h[1-6][^>]*>(.*?)</h[1-6]>|<li[^>]*>(.*?)</li>', body.group(1), re.S):
            if tok.group(1) is not None:
                head = text(tok.group(1))
                d = DAY_HEAD.search(head)
                if d and d.group(1).lower() in MONTHS:
                    date = dt.date(year, MONTHS[d.group(1).lower()], int(d.group(2))).isoformat()
                    section = 'movies' if head.lower().startswith('movies') else \
                        'series' if head.lower().startswith(('series', 'tv')) else None
                else:
                    date = None                 # a heading without a date (e.g. "Best of ...") ends the dated lists
                continue
            if not date:
                continue
            t, y, media, confirmed = parse_won_item(text(tok.group(2)), section)
            if t:
                rows.append({'service': 'netflix', 'title': t, 'year': y, 'media': media, 'leave_date': date,
                             'precision': 'day', 'confirmed': confirmed, 'source': 'won', 'url': link})
    stats['rows'] = len(rows)
    return rows


# ---- whatisleaving.com -----------------------------------------------------------------------------------------------
def wil_rows(stats, today):
    home = fetch(WIL_HOME, cache=False)
    js = re.search(r'src="(/assets/index-[^"]+\.js)"', home)
    if not js:
        raise RuntimeError('whatisleaving: app bundle not found (page format changed)')
    # the bundle carries the anonymous key: never cached, so the key is never written anywhere
    bundle = fetch(urllib.parse.urljoin(WIL_HOME, js.group(1)), cache=False)
    m = re.search(r'"(https://[a-z0-9]+\.supabase\.co)",\w+="(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)"', bundle)
    if not m:
        raise RuntimeError('whatisleaving: data endpoint not found (app format changed)')
    base, anon = m.groups()
    rows, offset = [], 0
    while True:
        q = urllib.parse.urlencode({'select': 'platform,leaving_date,verified,titles(title,type,year,tmdb_id)',
                                    'leaving_date': f'gt.{today}', 'order': 'leaving_date.asc,id.asc',
                                    'limit': 1000, 'offset': offset})
        page = json.loads(fetch(f'{base}/rest/v1/expirations?{q}', {'apikey': anon, 'Authorization': f'Bearer {anon}'},
                                cache=False))
        for r in page:
            svc, t = WIL_PLATFORM.get(r.get('platform')), r.get('titles') or {}
            stats['platforms'][r.get('platform')] = stats['platforms'].get(r.get('platform'), 0) + 1
            if not svc or not t.get('title'):
                continue
            rows.append({'service': svc, 'title': t['title'], 'year': t.get('year'),
                         'media': {'movie': 'movie', 'tv': 'series', 'series': 'series', 'show': 'series'}.get(t.get('type')),
                         'leave_date': r['leaving_date'], 'precision': 'day', 'confirmed': bool(r.get('verified', True)),
                         'source': 'wil', 'url': f"https://whatisleaving.com/{r['platform']}", 'src_tmdb': t.get('tmdb_id')})
        if len(page) < 1000:
            break
        offset += 1000
    stats['posts'] = 1
    stats['rows'] = len(rows)
    return rows


# ---- TMDB ----------------------------------------------------------------------------------------------------------
def norm(s):
    s = re.sub(r"['’‘`]", '', s or '')              # Dawson’s = Dawson's = Dawsons
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower().replace('&', 'and')
    s = s.replace('vv', 'w')                          # stylised W ("The VVitch"); applied to both sides of a compare
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def details(media, tid):
    """Durably cached TMDB details subset (title, original title, date, poster, genres, runtime, popularity)."""
    b = build()

    def get():
        d = b.tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}")
        keep = ('id', 'title', 'name', 'original_title', 'original_name', 'release_date', 'first_air_date',
                'poster_path', 'genres', 'runtime', 'popularity')
        return {k: d.get(k) for k in keep} if d.get('id') else None
    try:
        return b.stable(f'lv:{media}:{tid}', get)
    except RuntimeError:
        return None


def year_of(d):
    s = (d or {}).get('release_date') or (d or {}).get('first_air_date') or ''
    return int(s[:4]) if s[:4].isdigit() else None


def feature(media, d):
    return media != 'movie' or not d.get('runtime') or d['runtime'] >= MIN_FILM_RUNTIME


def title_ok(title, d):
    return norm(title) in {norm(d.get('title') or d.get('name')), norm(d.get('original_title') or d.get('original_name'))}


def search(title, year, media):
    """[(media, id, exact?)] candidates, most votes first; exact = same title and |year - hint| <= 1."""
    b, out = build(), []
    for m in ([media] if media else ['movie', 'series']):
        res = b.tmdb('/search/movie' if m == 'movie' else '/search/tv', query=title, include_adult='false').get('results', [])
        for r in sorted(res, key=lambda r: -(r.get('vote_count') or 0))[:8]:
            y = year_of(r)
            if year and y and abs(y - year) > 1:
                continue
            exact = norm(r.get('title') or r.get('name')) == norm(title) or \
                norm(r.get('original_title') or r.get('original_name')) == norm(title)
            out.append((m, r['id'], exact, y, r.get('title') or r.get('name')))
    return out


# CHECK rows whose correct match is unambiguous, checked by hand on TMDB (title, year, runtime) 2026-09-28:
# the source drops a subtitle or uses another English title. Key: (normalised source title, source year or None).
OVERRIDES = {('quiet victory', 1988): ('movie', 203552),         # Quiet Victory: The Charlie Wedemeyer Story
             ('triumph of the heart', 1991): ('movie', 208377),  # A Triumph of the Heart: The Ricky Bell Story
             ('forest of piano', None): ('series', 78471)}       # The Piano Forest (anime, 2018)


def resolve(row, provider_ids, present_cache):
    """(media, id, how, ok) - ok=False is a CHECK row."""
    hit = OVERRIDES.get((norm(row['title']), row['year'] or None))
    if hit:
        return hit[0], hit[1], 'OVERRIDE (checked by hand)', True
    if row.get('src_tmdb') and row['media']:
        d = details(row['media'], row['src_tmdb'])
        # the source named this exact id: a short film / stand-up special is accepted when title + year agree
        # (the runtime rule only stops the SEARCH from picking a short over a feature)
        if d and title_ok(row['title'], d) and (not row['year'] or not year_of(d) or abs(year_of(d) - row['year']) <= 1):
            return row['media'], row['src_tmdb'], 'source TMDB id verified (title + year)', True
    cands = search(row['title'], row['year'], row['media'])
    exact = [c for c in cands if c[2] and feature(c[0], details(c[0], c[1]) or {})]
    if len(exact) > 1:                                 # same-name titles: the one actually on the service wins
        on = [c for c in exact if present(c[0], c[1], provider_ids, present_cache)]
        if len(on) == 1:
            return on[0][0], on[0][1], f'exact title + year, {len(exact)} candidates -> the one on the service', True
        return exact[0][0], exact[0][1], 'CHECK: several exact candidates ' + '; '.join(
            f'{c[0]}:{c[1]} {c[3]}' for c in exact), False
    if exact:
        how = 'exact title + year' if row['year'] else 'exact title (no year given)'
        ok = bool(row['year'])
        if not ok:                                     # no year: accepted only when the title is actually on the service
            ok = present(exact[0][0], exact[0][1], provider_ids, present_cache)
            how += ', on the service' if ok else ', NOT on the service -> CHECK'
        if row.get('src_tmdb'):
            how += f" (source id {row['src_tmdb']} rejected)"
        return exact[0][0], exact[0][1], how, ok
    if cands:
        c = cands[0]
        return c[0], c[1], f'CHECK: no exact title, best candidate "{c[4]}" ({c[3]})', False
    return None, None, 'UNRESOLVED', False


def present(media, tid, provider_ids, cache):
    """Still on the service now: TMDB watch providers (JustWatch), US, flatrate."""
    key = (media, tid)
    if key not in cache:
        try:
            r = build().tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}/watch/providers")
            us = (r.get('results') or {}).get('US') or {}
            cache[key] = {p['provider_id'] for p in us.get('flatrate') or []}
        except RuntimeError:
            cache[key] = None                          # unknown: not counted as present
    return bool(cache[key] and cache[key] & provider_ids)


def provider_ids():
    spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
    out = {}
    for c in spec['catalogs']:
        slug = c['slug'][len('streaming-'):] if c['slug'].startswith('streaming-') and c.get('sort') == 'popular' else None
        if slug in SERVICES:
            out[slug] = {int(i) for i in c['movie']['with_watch_providers'].split('|')}
    return out


# ---- main ------------------------------------------------------------------------------------------------------------
def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else 'out')
    out.mkdir(parents=True, exist_ok=True)
    today = os.environ.get('SD_TODAY') or dt.date.today().isoformat()
    pids = provider_ids()
    stats = {s: {'posts': 0, 'rows': 0, 'errors': [], 'platforms': {}} for s in ('won', 'wil')}
    rows = []
    for name, fn in (('won', lambda st: won_rows(st)), ('wil', lambda st: wil_rows(st, today))):
        # OFF by default: whatisleaving.com's Terms of Service forbid automated bulk extraction and republishing its
        # lists (found 2026-09-28); only an explicit SD_LEAVING_WIL=1 turns it on, pending the owner's decision
        if name == 'wil' and os.environ.get('SD_LEAVING_WIL', '0') != '1':
            stats[name]['errors'].append('disabled (SD_LEAVING_WIL=0)')
            continue
        try:
            rows += fn(stats[name])
        except Exception as e:                         # a failing source is reported, never fatal
            stats[name]['errors'].append(f'{type(e).__name__}: {e}'[:300])
    rows = [r for r in rows if r['leave_date'] > today]
    cache = {}
    for r in rows:
        r['media_res'], r['tmdb'], r['how'], r['ok'] = resolve(r, pids[r['service']], cache)
    # cross-confirmation: a CHECK row that the other source resolved to the same id for the same service
    ok_ids = {(r['service'], r['media_res'], r['tmdb'], r['source']) for r in rows if r['ok']}
    for r in rows:
        other = 'wil' if r['source'] == 'won' else 'won'
        if not r['ok'] and r['tmdb'] and (r['service'], r['media_res'], r['tmdb'], other) in ok_ids:
            r['ok'], r['how'] = True, r['how'] + ' -> cross-confirmed by the other source'
    for r in rows:
        r['present'] = bool(r['ok'] and present(r['media_res'], r['tmdb'], pids[r['service']], cache))
    # merge per (service, title): earliest leave date wins; sources listed
    merged = {}
    for r in rows:
        if not (r['ok'] and r['present']):
            continue
        k = (r['service'], r['media_res'], r['tmdb'])
        e = merged.setdefault(k, {'id': f"tmdb:{r['tmdb']}", 'type': r['media_res'], 'name': r['title'],
                                  'leave_date': r['leave_date'], 'precision': r['precision'], 'sources': [],
                                  'dates': {}, 'status': 'confirmed'})
        e['leave_date'] = min(e['leave_date'], r['leave_date'])
        if r['source'] not in e['sources']:
            e['sources'].append(r['source'])
        e['dates'][r['source']] = min(e['dates'].get(r['source'], r['leave_date']), r['leave_date'])
        if not r['confirmed']:
            e['status'] = 'unconfirmed' if e['status'] == 'confirmed' and len(e['sources']) == 1 else e['status']
    data = {'generated': today, 'services': {s: [] for s in SERVICES}}
    for (svc, media, tid), e in merged.items():
        d = details(media, tid) or {}
        e['popularity'] = d.get('popularity') or 0
        data['services'][svc].append(e)
    for svc in SERVICES:
        data['services'][svc].sort(key=lambda e: (e['leave_date'], -e['popularity']))
    (out / 'leaving.json').write_text(json.dumps(data, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    write_reports(out, rows, stats, data)
    summary = {'date': today, 'errors': {s: stats[s]['errors'] for s in stats},
               'cards': {s: len(v) for s, v in data['services'].items()},
               'rows': {s: sum(1 for r in rows if r['source'] == s) for s in stats}}
    (out / 'leaving_summary.json').write_text(json.dumps(summary, indent=1) + '\n', encoding='utf-8')
    print(json.dumps(summary), flush=True)
    build().save_cache()
    return rows, stats, data


def write_reports(out, rows, stats, data):
    with open(out / 'check_rows.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['source', 'service', 'title', 'year', 'media_hint', 'leave_date', 'tmdb', 'how', 'url'])
        for r in rows:
            if not r['ok']:
                w.writerow([r['source'], r['service'], r['title'], r['year'] or '', r['media'] or '', r['leave_date'],
                            f"{r['media_res']}:{r['tmdb']}" if r['tmdb'] else '', r['how'], r['url']])
    with open(out / 'parse_stats.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['source', 'service', 'posts', 'rows', 'resolved', 'resolved_pct', 'check', 'still_present',
                    'on_card', 'errors'])
        for src in ('won', 'wil'):
            for svc in SERVICES:
                rs = [r for r in rows if r['source'] == src and r['service'] == svc]
                if not rs and not (src == 'wil'):
                    continue
                ok = sum(r['ok'] for r in rs)
                card = sum(1 for e in data['services'][svc] if src in e['sources'])
                w.writerow([src, svc, stats[src]['posts'], len(rs), ok, f'{100 * ok / len(rs):.1f}' if rs else '',
                            len(rs) - ok, sum(r['present'] for r in rs), card, ' | '.join(stats[src]['errors'])])
    # Netflix cross-check: same TMDB id in both sources, date agreement
    won = {(r['media_res'], r['tmdb']): r for r in rows if r['source'] == 'won' and r['service'] == 'netflix' and r['ok']}
    wil = {(r['media_res'], r['tmdb']): r for r in rows if r['source'] == 'wil' and r['service'] == 'netflix' and r['ok']}
    with open(out / 'netflix_crosscheck.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['status', 'tmdb', 'title', 'won_date', 'wil_date', 'date_agrees', 'present'])
        for k in sorted(set(won) | set(wil), key=lambda k: (won.get(k) or wil.get(k))['leave_date']):
            a, b = won.get(k), wil.get(k)
            st = 'both' if a and b else 'won_only' if a else 'wil_only'
            r = a or b
            w.writerow([st, f'{k[0]}:{k[1]}', r['title'], a['leave_date'] if a else '', b['leave_date'] if b else '',
                        (a['leave_date'] == b['leave_date']) if a and b else '', r['present']])


def leaving_card(service_slug, today, path):
    """Metas for the "Leaving Soon · <service>" card: leave date after `today`, soonest first (leaving.json order),
    each a build.preview meta (poster, year, '_g' genres) from the durably cached TMDB details."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    b, metas = build(), []
    for e in data.get('services', {}).get(service_slug, []):
        if e['leave_date'] <= today:
            continue
        media, tid = e['type'], int(e['id'].split(':', 1)[1])
        d = details(media, tid)
        if d:
            m = b.preview(d, media)
            m.setdefault('_g', b.canon_genres([g['id'] for g in d.get('genres') or []])
                         if hasattr(b, 'canon_genres') else [])
            metas.append(m)
    return metas


if __name__ == '__main__':
    main()
