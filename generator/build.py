"""Generate the Strand Discovery static Stremio catalog addon from TMDB.

Output (default ./out): manifest.json, catalog/<type>/<id>.json, catalog/<type>/<id>/skip=<n>.json,
a terminal skip=<total>.json with {"metas": []}, summary.json and report.csv.

Env:
  TMDB_API_KEY       required (GitHub Actions secret; never printed)
  PREV_SUMMARY_URL   optional: last deployed summary.json, used by the refresh guards
  FORCE_FAIL         optional: "1" fails the run after building (pilot test: last deploy must stay served)
  ALLOW_CATALOG_REMOVAL optional: "1" (manual runs only) accepts catalogs that were removed/renamed on purpose;
                     the >30% total-items guard still applies
"""
import csv, datetime as dt, json, os, sys, time, urllib.parse, urllib.request
from pathlib import Path

API = 'https://api.themoviedb.org/3'
IMG = 'https://image.tmdb.org/t/p'
HERE = Path(__file__).resolve().parent
TODAY = dt.date.today().isoformat()
MIN_INTERVAL = 1 / 20          # self-throttle: 20 requests/s (TMDB publishes ~40/s)
DROP_LIMIT = 0.30              # abort if total items fall by more than 30%

_last = [0.0]
_calls = [0]


def tmdb(path, **params):
    key = os.environ.get('TMDB_API_KEY')
    if not key:
        sys.exit('TMDB_API_KEY is not set')
    url = f'{API}{path}?{urllib.parse.urlencode({**params, "api_key": key})}'
    for attempt in range(6):
        wait = _last[0] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
        _calls[0] += 1
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


def preview(item, media):
    title = item.get('title') or item.get('name')
    date = item.get('release_date') or item.get('first_air_date') or ''
    meta = {'id': f"tmdb:{item['id']}", 'type': media, 'name': title}
    if item.get('poster_path'):
        meta['poster'] = f"{IMG}/w342{item['poster_path']}"
    if date[:4]:
        meta['releaseInfo'] = date[:4]
    return meta


SORTS = {
    'popular': ('popularity.desc', 'popularity.desc'),
    'new': ('primary_release_date.desc', 'first_air_date.desc'),
    'top': ('vote_average.desc', 'vote_average.desc'),
    'release_asc': ('primary_release_date.asc', 'first_air_date.asc'),
}


def discover(card, media, depth, min_votes):
    movie = media == 'movie'
    sort = SORTS[card.get('sort', 'popular')][0 if movie else 1]
    params = {'sort_by': sort, 'include_adult': 'false', 'vote_count.gte': card.get('min_votes', min_votes), **card['params']}
    if movie:
        params.update({'with_release_type': '4|5|6', 'release_date.lte': TODAY})
    else:
        params.update({'with_status': '0|3|4|5', 'first_air_date.lte': TODAY})
    out, seen, page = [], set(), 1
    while len(out) < depth:
        data = tmdb(f"/discover/{'movie' if movie else 'tv'}", page=page, **params)
        for item in data.get('results', []):
            if item['id'] not in seen:
                seen.add(item['id'])
                out.append(preview(item, media))
        if page >= min(data.get('total_pages', 0), 500):
            break
        page += 1
    return out[:depth]


def collection(card):
    out = []
    for cid in card['collection_ids']:
        parts = tmdb(f'/collection/{cid}').get('parts', [])
        released = [p for p in parts if p.get('release_date') and p['release_date'] <= TODAY]
        out += [preview(p, 'movie') for p in sorted(released, key=lambda p: p['release_date'])]
    return out


MIN_RUNTIME = 40      # director cards: drop shorts; unknown runtime kept only for well-known titles
KNOWN_VOTES = 500


def director(card, dropped):
    crew = tmdb(f"/person/{card['person_id']}/movie_credits").get('crew', [])
    films = {c['id']: c for c in crew if c.get('job') in ('Director', 'Co-Director')
             and c.get('release_date') and c['release_date'] <= TODAY}
    kept = []
    for c in sorted(films.values(), key=lambda c: c['release_date'], reverse=True):
        runtime = tmdb(f"/movie/{c['id']}").get('runtime') or 0
        if runtime >= MIN_RUNTIME or (runtime == 0 and c.get('vote_count', 0) >= KNOWN_VOTES):
            kept.append(preview(c, 'movie'))
        else:
            dropped.append(f"{c.get('title')} ({c['release_date'][:4]}, {runtime or 'no'} min)")
    return kept


MDBLIST_JSON = 'https://mdblist.com/lists/{list}/json'   # public export, no key needed


def mdblist(card, media):
    """Items of a public MDBList list, in list order, as tmdb-id previews (MDBList `id` is the TMDB id)."""
    with urllib.request.urlopen(urllib.request.Request(MDBLIST_JSON.format(list=card['list']),
                                                       headers={'User-Agent': 'strand-discovery'}), timeout=60) as r:
        items = json.load(r)
    want = 'movie' if media == 'movie' else 'show'
    out = []
    for it in items:
        if it.get('mediatype') != want:
            continue
        tid = it.get('id')
        if not tid and it.get('imdb_id'):          # fallback: resolve via TMDB /find
            found = tmdb(f"/find/{it['imdb_id']}", external_source='imdb_id')
            hits = found.get('movie_results' if media == 'movie' else 'tv_results') or []
            tid = hits[0]['id'] if hits else None
        if not tid:
            continue
        details = tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}")
        if details.get('id'):
            out.append(preview(details, media))
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


def source_name(card):
    k = card['kind']
    if k == 'mdblist':
        return f"MDBList {card['list']}"
    if k == 'collection':
        return ' + '.join(tmdb(f'/collection/{c}').get('name', '?') for c in card['collection_ids'])
    if k == 'director':
        return tmdb(f"/person/{card['person_id']}").get('name', '?') + ' (Director credits)'
    p = card['params']
    if 'with_watch_providers' in p:
        provs = tmdb('/watch/providers/movie', watch_region=p.get('watch_region', 'US')).get('results', [])
        names = {str(x['provider_id']): x['provider_name'] for x in provs}
        return ' | '.join(names.get(i, i) for i in p['with_watch_providers'].split('|'))
    if 'with_keywords' in p:
        return ' | '.join(tmdb(f'/keyword/{k}').get('name', k) for k in p['with_keywords'].split('|'))
    return json.dumps(p)


def write_catalog(root, media, cid, metas, page_size):
    base = root / 'catalog' / media
    base.mkdir(parents=True, exist_ok=True)
    (base / f'{cid}.json').write_text(json.dumps({'metas': metas[:page_size]}, separators=(',', ':')), encoding='utf-8')
    for skip in range(page_size, len(metas), page_size):
        d = base / cid
        d.mkdir(exist_ok=True)
        (d / f'skip={skip}.json').write_text(json.dumps({'metas': metas[skip:skip + page_size]}, separators=(',', ':')), encoding='utf-8')
    d = base / cid
    d.mkdir(exist_ok=True)
    (d / f'skip={len(metas)}.json').write_text('{"metas":[]}', encoding='utf-8')   # terminal page: no 404 at the end


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


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else 'out')
    spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
    dflt = spec['defaults']
    manifest = {**spec['addon'], 'resources': ['catalog'], 'types': ['movie', 'series'], 'catalogs': [],
                'behaviorHints': {'configurable': False}}
    counts, rows = {}, []
    for card in spec['cards']:
        # One catalog per card, used directly as a Jellyfin library (merged catalogs lose deep pages when cold).
        cid = f"sd-{card['slug']}"
        if 'collection' in f"{cid} {card['title']}".lower():   # AIOStreams would turn it into a BoxSet library
            raise SystemExit(f"card {card['slug']!r}: id/name must not contain 'collection'")
        name = source_name(card)
        per_media = []
        for media in card['media']:
            if card['kind'] == 'discover':
                per_media.append(discover(card, media, card.get('depth', dflt['depth']), dflt['min_votes']))
            elif card['kind'] == 'collection':
                per_media.append(collection(card))
            elif card['kind'] == 'director':
                dropped = []
                per_media.append(director(card, dropped))
                if dropped:
                    print(f"{card['title']}: dropped {len(dropped)} short/unknown-runtime titles: {'; '.join(dropped)}")
            elif card['kind'] == 'mdblist':
                per_media.append(mdblist(card, media))
            else:
                raise SystemExit(f"unknown kind {card['kind']}")
        metas = interleave(per_media)
        write_catalog(root, 'movie', cid, metas, dflt['page_size'])
        manifest['catalogs'].append({'type': 'movie', 'id': cid, 'name': card['title'], 'extra': [{'name': 'skip'}]})
        counts[cid] = len(metas)
        kinds = {t: sum(1 for m in metas if m['type'] == t) for t in ('movie', 'series')}
        src_id = card.get('params', {}).get('with_watch_providers') or card.get('params', {}).get('with_keywords') \
            or card.get('collection_ids') or card.get('person_id') or card.get('list')
        rows.append([card['title'], card['shelf'], card.get('sort', 'default'), card['kind'], src_id, name, cid,
                     f"movie={kinds['movie']} series={kinds['series']}", len(metas), ' | '.join(m['name'] for m in metas[:3])])
    (root / 'manifest.json').write_text(json.dumps(manifest, indent=1), encoding='utf-8')
    (root / '.nojekyll').write_text('', encoding='utf-8')
    summary = {'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'tmdb_requests': _calls[0],
               'total_items': sum(counts.values()), 'catalogs': counts}
    (root / 'summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    with open(root / 'report.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['card', 'shelf', 'sort', 'source_type', 'source_id', 'source_name', 'catalog_id', 'media', 'item_count', 'top3'])
        w.writerows(rows)
    print(json.dumps({k: v for k, v in summary.items() if k != 'catalogs'}), '| catalogs:', len(counts))

    prev = previous_summary()
    if prev:
        emptied = [c for c, n in prev.get('catalogs', {}).items() if n > 0 and counts.get(c, 0) == 0]
        if emptied and os.environ.get('ALLOW_CATALOG_REMOVAL') == '1':
            print(f'catalogs removed/renamed on purpose (manual run): {emptied}')
        elif emptied:
            sys.exit(f'GUARD: previously non-empty catalogs are now empty: {emptied}')
        if prev.get('total_items') and summary['total_items'] < prev['total_items'] * (1 - DROP_LIMIT):
            sys.exit(f"GUARD: total items dropped {prev['total_items']} -> {summary['total_items']} (>30%)")
    if os.environ.get('FORCE_FAIL') == '1':
        sys.exit('FORCE_FAIL=1: failing on purpose (pilot test)')


if __name__ == '__main__':
    main()
