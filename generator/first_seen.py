"""first_seen logger: which titles are on each streaming service today, and since when (our own "date added").

  python first_seen.py <state dir>

TMDB's watch-provider data (JustWatch) says what is on a service, not when it arrived. This enumerates every
provider catalogue daily and keeps, per (provider, title), the day we first saw it there. "New on X" cards are
then ordered by that date (build.py) once a provider has >= MIN_HISTORY_DAYS of history.

State (<state dir>/first_seen.json.gz, never committed; restored by the workflow from the Actions cache or, if the
cache is missing, from the previous run's artifact):
  {"providers": {slug: {"baseline": date, "last_add": date, "items": {"m:123": [first_seen, last_seen], ...}}},
   "runs": [dates]}
  - first run for a provider = BASELINE: everything present gets first_seen "baseline" and never counts as new
  - a title absent for > REARRIVAL_GAP days that comes back counts as a new arrival again (short data glitches don't)
  - entries not seen for > PRUNE_DAYS are dropped (TMDB terms: don't keep data longer than 6 months)
Outputs next to the state: new_candidates.json (per provider: previews of titles first seen within NEW_WINDOW days,
with popularity, for build.py) and fs_summary.json (per-provider size / adds / removals / churn + warnings).

Budget (copilot ruling): subscription services first, free services last; the free-service enumeration is skipped
(and reported) if the logger would push the whole run past MAX_REQUESTS or MAX_MINUTES.
Env: TMDB_API_KEY (never printed), SD_WORKERS, SD_FS_ONLY (comma list of slugs), SD_TODAY / SD_FS_CAP (tests only),
     SD_BUILD_REQUESTS_EST (requests the build step will need; default from the previous summary, else 14000).
"""
import datetime as dt, gzip, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402  (shared throttled, key-safe tmdb())

HERE = Path(__file__).resolve().parent
TODAY = dt.date.fromisoformat(os.environ['SD_TODAY']) if os.environ.get('SD_TODAY') else dt.date.today()  # tests only
MIN_HISTORY_DAYS = 14
NEW_WINDOW = 45
REARRIVAL_GAP = 7
PRUNE_DAYS = 180
CAP = int(os.environ.get('SD_FS_CAP') or 10000)   # TMDB discover returns at most 500 pages x 20 (env: tests only)
MAX_REQUESTS = 35000            # whole run (logger + build)
MAX_MINUTES = 30
PAGE_POOL = ThreadPoolExecutor(int(os.environ.get('SD_WORKERS') or 8))   # page fetches; the 20 req/s throttle is shared
FREE = {'tubi', 'pluto-tv', 'plex'}   # ad-supported services
MAX_DROP = 5          # at most this many services leave the state in one run (the 2026-09-28 drop was 5)


def providers():
    spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
    out = []
    for c in spec['catalogs']:
        if c['slug'].startswith('streaming-') and c['sort'] == 'popular':
            slug = c['slug'][len('streaming-'):]
            out.append({'slug': slug, 'title': c['title'], 'movie': c['movie'], 'series': c['series'],
                        'free': slug in FREE})
    only = {s for s in os.environ.get('SD_FS_ONLY', '').split(',') if s}
    out = [p for p in out if not only or p['slug'] in only]
    return sorted(out, key=lambda p: p['free'])            # subscription services first


def base_params(p, media, dflt):
    q = dict(p[media])                  # monetization comes from the spec card (make_spec: MONETIZATION / FREE_SERVICES)
    q.setdefault('with_watch_monetization_types', 'free|ads' if p['free'] else 'flatrate')
    q.update({'include_adult': 'false', 'vote_count.gte': 0,
              'sort_by': 'primary_release_date.asc' if media == 'movie' else 'first_air_date.asc'})
    excl = dflt.get('without_keywords', '')
    if media == 'series':
        q['without_genres'] = '10767|10763'
        if dflt.get('without_keywords_tv'):
            excl = f"{excl}|{dflt['without_keywords_tv']}"
    if excl:
        q['without_keywords'] = excl
    return q


def enumerate_catalogue(p, media, dflt):
    """All titles of one provider x medium. Catalogues over TMDB's 10k discover cap are sliced by date range
    (titles without any date can't be reached by a date slice - a known, small gap for capped catalogues only)."""
    path = f"/discover/{'movie' if media == 'movie' else 'tv'}"
    field = 'primary_release_date' if media == 'movie' else 'first_air_date'
    q = base_params(p, media, dflt)
    items = {}

    def take(results):
        for r in results:
            items[r['id']] = r

    def pages(params, total_pages):             # pages 2..N in parallel (latency, not the throttle, was the limit)
        for d in PAGE_POOL.map(lambda pg: build.tmdb(path, page=pg, **params), range(2, min(total_pages, 500) + 1)):
            take(d.get('results', []))

    first = build.tmdb(path, page=1, **q)
    if first.get('total_results', 0) <= CAP:
        take(first.get('results', []))
        pages(q, first.get('total_pages', 0))
        return items, False

    def slice_range(lo, hi):
        params = {**q, f'{field}.gte': lo.isoformat(), f'{field}.lte': hi.isoformat()}
        d = build.tmdb(path, page=1, **params)
        if d.get('total_results', 0) > CAP and (hi - lo).days > 0:
            mid = lo + (hi - lo) // 2
            slice_range(lo, mid)
            slice_range(mid + dt.timedelta(days=1), hi)
            return
        take(d.get('results', []))
        pages(params, d.get('total_pages', 0))

    slice_range(dt.date(1874, 1, 1), TODAY + dt.timedelta(days=730))
    return items, True


def load_state(state_dir):
    f = state_dir / 'first_seen.json.gz'
    if f.exists():
        with gzip.open(f, 'rt', encoding='utf-8') as fh:
            return json.load(fh)
    return {'providers': {}, 'runs': []}


def save_state(state_dir, state):
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / 'first_seen.json.gz.tmp'
    with gzip.open(tmp, 'wt', encoding='utf-8') as fh:
        json.dump(state, fh, separators=(',', ':'))
    tmp.replace(state_dir / 'first_seen.json.gz')           # never leave a half-written state behind


def main():
    state_dir = Path(sys.argv[1])
    t0 = time.monotonic()
    dflt = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))['defaults']
    state = load_state(state_dir)
    today = TODAY.isoformat()
    build_est = int(os.environ.get('SD_BUILD_REQUESTS_EST') or 14000)
    summary = {'date': today, 'state_source': os.environ.get('SD_STATE_SOURCE', 'unknown'), 'providers': {},
               'warnings': [], 'skipped': []}
    candidates = {}
    wanted = providers()
    gone = sorted(set(state['providers']) - {p['slug'] for p in wanted})
    if gone and not os.environ.get('SD_FS_ONLY'):          # a service dropped from the spec leaves the state too...
        if len(gone) <= MAX_DROP and len(gone) <= 0.3 * len(state['providers']):
            for g in gone:
                del state['providers'][g]
            summary['dropped'] = gone
        else:                                                # ...but never a mass drop (a broken or trimmed spec)
            summary['warnings'].append(f"{len(gone)} services missing from the spec were KEPT in the state "
                                       f"(more than {MAX_DROP} or 30%): {gone[:8]}")
    for p in wanted:
        elapsed = (time.monotonic() - t0) / 60
        if p['free'] and (build._calls[0] + build_est > MAX_REQUESTS * 0.8 or elapsed > MAX_MINUTES * 0.5):
            summary['skipped'].append(f"{p['slug']}: budget (requests so far {build._calls[0]}, build est {build_est}, "
                                      f"{elapsed:.1f} min)")
            continue
        calls_before = build._calls[0]
        with ThreadPoolExecutor(2) as pool:                  # movie + tv in parallel; the throttle is shared
            (movies, sliced_m), (shows, sliced_t) = pool.map(lambda m: enumerate_catalogue(p, m, dflt), ('movie', 'series'))
        present = {f'm:{i}': r for i, r in movies.items()} | {f't:{i}': r for i, r in shows.items()}
        prov = state['providers'].setdefault(p['slug'], {})
        for k, v in {'baseline': today, 'last_add': None, 'last_run': None, 'items': {}}.items():
            prov.setdefault(k, v)                        # a provider may exist with arrival signals only
        items = prov['items']
        is_baseline = prov['baseline'] == today
        # presence is judged against this provider's previous RUN, not the calendar: a gap in our own runs
        # (workflow outage) must not turn a whole catalogue into "new arrivals"
        last_run = prov.get('last_run') if prov.get('last_run') != today else prov.get('prev_run')
        prev_present = {k for k, (fs, ls) in items.items() if last_run and ls >= last_run}
        adds = 0
        for k in present:
            if k not in items:
                items[k] = ['baseline' if is_baseline else today, today]
                adds += 0 if is_baseline else 1
            else:
                fs, ls = items[k]
                absent_since = dt.date.fromisoformat(ls)
                came_back = last_run and ls < last_run and (TODAY - absent_since).days > REARRIVAL_GAP
                if came_back and not is_baseline:            # missing from runs for > 7 days, now back: new again
                    items[k] = [today, today]
                    adds += 1
                else:
                    items[k][1] = today
        removals = len(prev_present - set(present)) if not is_baseline else 0
        cutoff = (TODAY - dt.timedelta(days=PRUNE_DAYS)).isoformat()
        for k in [k for k, (fs, ls) in items.items() if ls < cutoff]:
            del items[k]
        if adds:
            prov['last_add'] = today
        if prov.get('last_run') != today:
            prov['prev_run'] = prov.get('last_run')
        prov['last_run'] = today
        history = (TODAY - dt.date.fromisoformat(prov['baseline'])).days
        size_prev = len(prev_present)
        churn = (adds + removals) / size_prev if size_prev else 0.0
        s = {'size': len(present), 'adds': adds, 'removals': removals, 'churn': round(churn, 4),
             'history_days': history, 'baseline': prov['baseline'], 'requests': build._calls[0] - calls_before,
             'sliced': sliced_m or sliced_t, 'mode': 'first_seen' if history >= MIN_HISTORY_DAYS else 'release_date'}
        summary['providers'][p['slug']] = s
        if size_prev and len(present) < size_prev * 0.8:
            summary['warnings'].append(f"{p['slug']}: catalogue shrank {size_prev} -> {len(present)} (>20%)")
        if size_prev and churn > 0.15:
            summary['warnings'].append(f"{p['slug']}: churn {churn:.0%} (>15%)")
        if history >= 7 and (not prov['last_add'] or (TODAY - dt.date.fromisoformat(prov['last_add'])).days >= 7):
            summary['warnings'].append(f"{p['slug']}: no new arrivals for 7+ days")
        window = (TODAY - dt.timedelta(days=NEW_WINDOW)).isoformat()
        fresh = [(k, present[k]) for k in present if items[k][0] != 'baseline' and items[k][0] >= window]
        candidates[p['slug']] = [{'id': f"tmdb:{r['id']}", 'type': 'movie' if k[0] == 'm' else 'series',
                                  'name': r.get('title') or r.get('name'), 'first_seen': items[k][0],
                                  'popularity': r.get('popularity') or 0,
                                  '_g': build.canon_genres(r.get('genre_ids')),
                                  **({'poster': f"{build.IMG}/w342{r['poster_path']}"} if r.get('poster_path') else {}),
                                  **({'releaseInfo': (r.get('release_date') or r.get('first_air_date'))[:4]}
                                     if (r.get('release_date') or r.get('first_air_date')) else {})}
                                 for k, r in fresh]
        print(f"  {p['slug']:<18} size {len(present):>6} adds {adds:>4} removals {removals:>4} history {history:>3}d "
              f"requests {s['requests']:>5}{' sliced' if s['sliced'] else ''}", flush=True)
    if today not in state['runs']:
        state['runs'].append(today)
    try:                                                 # the stored arrival signals with today's presence: the
        import arrivals                                  # fallback for build.py if today's arrivals step fails
        arrivals.write_candidates(state, state_dir, today, arrivals.Presence(state, {}))
    except Exception as e:                               # never fail the logger over it
        summary['warnings'].append(f'signal candidates not written: {type(e).__name__}: {str(e)[:200]}')
    save_state(state_dir, state)
    summary['requests'] = build._calls[0]
    summary['seconds'] = round(time.monotonic() - t0)
    (state_dir / 'new_candidates.json').write_text(json.dumps(candidates, separators=(',', ':')), encoding='utf-8')
    (state_dir / 'fs_summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k != 'providers'}))
    for w in summary['warnings']:
        print('WARNING', w)


if __name__ == '__main__':
    main()
