"""Weekly accuracy check of the arrival-ordered "New on X" cards: PRECISION (what the cards show is on the service)
and RECALL (the services' own published arrival lists are on the cards). Keeps ONE GitHub issue labelled "accuracy".

  python accuracy_check.py --pages DIR --state DIR [--out DIR] [--report] [--n 20] [--seed N]
  python accuracy_check.py --fetch-site URL --pages DIR ...     # download the needed pages of the live site first

--pages = a generator output (summary.json + catalog/); --state = the first_seen state dir the build read
(first_seen.json.gz; signal_candidates.json if present, else recomputed from the stored signals for the build's date).

PRECISION: for every New card built in 'arrivals' mode (summary.json new_card_modes), a seeded random N of the card's
DATED items (placed by arrival date) is checked on TMDB watch/providers (US) under the provider's ids and monetization
from spec.json. Netflix / HBO Max: a series of the service's own network with no US provider data counts as on the
service (rules F2 / F3 of arrivals.py). A miss is (i) LAG = a day-dated item from a published schedule, <= 14 days old;
(iii) OFFICIAL = on the service's official arrival list (official_arrivals.csv), a JustWatch gap; DOCUMENTED = listed in
precision_unknowns.json; else (ii) ERROR. The gate fails on any undocumented (ii).
RECALL: per service in official_arrivals.csv, the official arrivals dated inside the 45-day New window (at least
MIN_OFFICIAL of them, else skipped with a note - no current list, nothing measured) that are anywhere on the service's
New card. A drop of more than MAX_DROP points below accuracy_baseline.json is a problem.
--report: open / comment on / close the "accuracy" issue (gh CLI, GITHUB_TOKEN with issues: write). Test mode as in
health.py: HEALTH_DRY_RUN=1 prints the writes; HEALTH_MOCK_ISSUES=<json> stands in for `gh issue list`.
TMDB_API_KEY from the environment (never printed). Reads only; never writes state.
"""
import argparse, csv, datetime as dt, gzip, json, os, random, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SCHEDULES = {'vt', 'fb', 'wodp'}
ORPHAN_NETWORKS = {'netflix': {213}, 'hbo-max': {49, 3186, 8304}}      # the sets arrivals.py uses (F2 / F3)
LAG_DAYS = 14
WINDOW = 45                     # the New card's arrival window (arrivals.WINDOW)
MIN_OFFICIAL = 10               # fewer official arrivals inside the window = no current list: recall skipped
MAX_DROP = 3.0                  # recall points below the baseline that open the issue
UA = 'strand-discovery/1.0 (accuracy check; +https://github.com/xBOBxSAGETx/strand-discovery)'
TITLE = 'Weekly accuracy check'


def tmdb(path):
    key = os.environ.get('TMDB_API_KEY')
    if not key:
        sys.exit('TMDB_API_KEY is not set')
    time.sleep(0.25)
    try:
        with urllib.request.urlopen(f'https://api.themoviedb.org/3{path}?{urllib.parse.urlencode({"api_key": key})}',
                                    timeout=30) as r:
            return json.load(r)
    except Exception as e:                     # never surface the URL (it carries the key)
        raise RuntimeError(f'TMDB {type(e).__name__} on {path}') from None


def fetch_site(site, pages):
    """The live site's summary.json + the New cards' pages (terminal page included), politely."""
    site = site.rstrip('/')

    def get(rel):
        time.sleep(1.0)
        with urllib.request.urlopen(urllib.request.Request(f'{site}/{rel}', headers={'User-Agent': UA}), timeout=60) as r:
            return r.read()
    pages.mkdir(parents=True, exist_ok=True)
    (pages / 'summary.json').write_bytes(get('summary.json'))
    summary = json.loads((pages / 'summary.json').read_text(encoding='utf-8'))
    d = pages / 'catalog' / 'movie'
    for slug in summary.get('new_card_modes', {}):
        cid = f'sd-{slug}'
        (d / cid).mkdir(parents=True, exist_ok=True)
        first = get(f'catalog/movie/{cid}.json')
        (d / f'{cid}.json').write_bytes(first)
        n, skip = len(json.loads(first)['metas']), len(json.loads(first)['metas'])
        while n:
            body = get(f'catalog/movie/{cid}/skip={skip}.json')
            (d / cid / f'skip={skip}.json').write_bytes(body)
            n = len(json.loads(body)['metas'])
            skip += n


def card_order(pages, slug):
    d, cid = pages / 'catalog' / 'movie', f'sd-{slug}'
    files = [d / f'{cid}.json'] + sorted((p for p in (d / cid).glob('skip=*.json') if '&' not in p.name),
                                         key=lambda p: int(p.stem.split('=')[1]))
    return [f"{m['type']}:{m['id'].split(':')[1]}" for f in files if f.exists()
            for m in json.loads(f.read_text(encoding='utf-8'))['metas']]


def load_candidates(state_dir, today):
    f = state_dir / 'signal_candidates.json'
    if f.exists():
        return json.loads(f.read_text(encoding='utf-8'))
    import arrivals                              # recompute what the build saw from the stored signals
    state = json.loads(gzip.decompress((state_dir / 'first_seen.json.gz').read_bytes()))
    return {'date': today, 'providers': arrivals.refresh(state, today)}


def precision(pages, state_dir, spec, summary, cands, n, seed, official, unknowns):
    state = json.loads(gzip.decompress((state_dir / 'first_seen.json.gz').read_bytes()))
    today = dt.date.fromisoformat(cands['date'])
    by_slug = {c['slug']: c for c in spec['catalogs']}
    rows, table = [], []
    for slug, mode in sorted(summary.get('new_card_modes', {}).items()):
        if mode != 'arrivals':
            continue
        svc = slug[len('streaming-'):-len('-new')]
        card, order = by_slug[slug], card_order(pages, slug)
        cmap = {c['key']: c for c in cands['providers'].get(svc, [])}
        dated = [k for k in order if k in cmap]
        random.seed(f'{seed}-{svc}')
        sample = random.sample(dated, min(n, len(dated)))
        sig = state['providers'].get(svc, {}).get('signals', {})
        t = {'provider': svc, 'dated_items': len(dated), 'sample': len(sample), 'on_service': 0, 'lag_i': 0,
             'official_iii': 0, 'documented_ii': 0, 'errors_ii': 0}
        for k in sample:
            media, tid = k.split(':')
            c = cmap[k]
            params = card[media if media in card else 'movie']
            ids = {int(i) for i in str(params['with_watch_providers']).split('|')}
            kinds = str(params['with_watch_monetization_types']).split('|')
            wp = tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}/watch/providers").get('results', {}).get('US', {})
            on = bool({p['provider_id'] for kd in kinds for p in wp.get(kd, [])} & ids)
            if not on and svc in ORPHAN_NETWORKS and media == 'series' and not wp:
                on = bool(ORPHAN_NETWORKS[svc] & {x['id'] for x in tmdb(f'/tv/{tid}').get('networks', [])})
            age = (today - dt.date.fromisoformat(c['date'])).days
            urls = sorted({o[6] for o in sig.get(k, {}).get('o', []) if len(o) >= 7 and o[0] == c['date'] and o[6]})
            if on:
                cls = 'ok'
                t['on_service'] += 1
            elif c['precision'] == 'day' and set(c['sources']) & SCHEDULES and 0 <= age <= LAG_DAYS:
                cls = 'lag (i)'
                t['lag_i'] += 1
            elif k in official.get(svc, set()):
                cls = 'official (iii)'
                t['official_iii'] += 1
            elif (svc, k) in unknowns:
                cls = 'documented (ii)'
                t['documented_ii'] += 1
            else:
                cls = 'ERROR (ii)'
                t['errors_ii'] += 1
            rows.append({'provider': svc, 'key': k, 'class': cls, 'event_date': c['date'], 'precision': c['precision'],
                         'sources': '|'.join(c['sources']), 'status': c['status'], 'urls': ' '.join(urls),
                         'card_rank': order.index(k) + 1})
        table.append(t)
    return table, rows


def recall(pages, summary, official_rows, today, baseline):
    lo = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW)).isoformat()
    out = []
    for svc in sorted({r['service'] for r in official_rows}):
        slug = f'streaming-{svc}-new'
        inwin = {f"{r['media']}:{r['tmdb_id']}" for r in official_rows
                 if r['service'] == svc and lo <= r['date'] <= today}
        base = baseline.get('services', {}).get(svc)
        if len(inwin) < MIN_OFFICIAL:
            out.append({'service': svc, 'status': 'skipped', 'official_in_window': len(inwin),
                        'note': f'fewer than {MIN_OFFICIAL} official arrivals dated {lo}..{today}: no current list'})
            continue
        order = set(card_order(pages, slug))
        found = len(inwin & order)
        pct = round(100 * found / len(inwin), 1)
        drop = round(base - pct, 1) if base is not None else None
        out.append({'service': svc, 'status': 'measured', 'official_in_window': len(inwin), 'found': found,
                    'recall': pct, 'baseline': base, 'drop': drop, 'mode': summary.get('new_card_modes', {}).get(slug),
                    'problem': drop is not None and drop > MAX_DROP})
    return out


def report(problems, lines, run_url):
    import health                                # reuse the gh helpers + dry-run / mock mode of the health issue
    health.ensure_label('accuracy', '1d76db', 'Weekly accuracy check (opened/closed automatically)')
    existing = json.loads(health.gh('issue', 'list', '--label', 'accuracy', '--state', 'open', '--json', 'number',
                                    '--limit', '5') or '[]')
    if problems:
        body = (f'Latest run: {run_url}\n\n' + '\n'.join(f'- {p}' for p in problems) + '\n\n' + '\n'.join(lines) +
                '\n\nThis issue closes itself after the next clean weekly check. What it means: README → Operations.')
        if existing:
            health.gh('issue', 'comment', str(existing[0]['number']), '--body', body)
            print(f"accuracy: updated issue #{existing[0]['number']}")
        else:
            print('accuracy:', health.gh('issue', 'create', '--title', TITLE, '--label', 'accuracy', '--body', body).strip())
    else:
        for i in existing:
            health.gh('issue', 'close', str(i['number']), '--comment', f'Clean weekly check: {run_url}')
            print(f"accuracy: closed issue #{i['number']}")
        if not existing:
            print('accuracy: clean, no open issue')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pages', required=True)
    ap.add_argument('--state', required=True)
    ap.add_argument('--out', default='accuracy')
    ap.add_argument('--fetch-site')
    ap.add_argument('--report', action='store_true')
    ap.add_argument('--n', type=int, default=20)
    ap.add_argument('--seed', default=None)
    ap.add_argument('--official', default=str(HERE / 'official_arrivals.csv'))
    ap.add_argument('--unknowns', default=str(HERE / 'precision_unknowns.json'))
    ap.add_argument('--baseline', default=str(HERE / 'accuracy_baseline.json'))
    ap.add_argument('--spec', default=str(HERE / 'spec.json'))
    a = ap.parse_args()
    pages, state_dir, out = Path(a.pages), Path(a.state), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.fetch_site:
        fetch_site(a.fetch_site, pages)
    summary = json.loads((pages / 'summary.json').read_text(encoding='utf-8'))
    spec = json.loads(Path(a.spec).read_text(encoding='utf-8'))
    cands = load_candidates(state_dir, summary['generated_at'][:10])
    today = cands['date']                        # the date the build ordered the cards with
    official_rows = list(csv.DictReader(open(a.official, encoding='utf-8')))
    official = {}
    for r in official_rows:
        official.setdefault(r['service'], set()).add(f"{r['media']}:{r['tmdb_id']}")
    unknowns = {(u['provider'], u['key']): u['reason'] for u in json.loads(Path(a.unknowns).read_text(encoding='utf-8'))}
    baseline = json.loads(Path(a.baseline).read_text(encoding='utf-8')) if Path(a.baseline).exists() else {}
    seed = a.seed or dt.date.fromisoformat(today).strftime('%G-W%V')          # a new sample every week
    table, rows = precision(pages, state_dir, spec, summary, cands, a.n, seed, official, unknowns)
    rec = recall(pages, summary, official_rows, today, baseline)
    with open(out / 'precision.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ['provider'])
        w.writeheader()
        w.writerows(rows)
    errors = [r for r in rows if r['class'] == 'ERROR (ii)']
    (out / 'accuracy.json').write_text(json.dumps({'date': today, 'seed': seed, 'precision': table, 'recall': rec,
                                                   'errors': errors}, indent=1), encoding='utf-8')
    lines = ['| provider | dated | sample | on service | lag (i) | official (iii) | documented | errors (ii) |',
             '|---|---|---|---|---|---|---|---|']
    lines += [f"| {t['provider']} | {t['dated_items']} | {t['sample']} | {t['on_service']} | {t['lag_i']} | "
              f"{t['official_iii']} | {t['documented_ii']} | {t['errors_ii']} |" for t in table]
    lines += ['', '| service | recall | baseline | official arrivals in window | note |', '|---|---|---|---|---|']
    lines += [f"| {r['service']} | {r.get('recall', '-')} | {r.get('baseline', '-')} | {r['official_in_window']} | "
              f"{r.get('note', '')} |" for r in rec]
    print('\n'.join(lines))
    problems = [f"precision: {e['provider']} {e['key']} not on the service ({e['precision']} precision, event "
                f"{e['event_date']}, sources {e['sources']}, status {e['status']}) {e['urls'][:200]}" for e in errors]
    problems += [f"recall: {r['service']} {r['recall']}% is {r['drop']} points below the baseline {r['baseline']}%"
                 for r in rec if r.get('problem')]
    print('RESULT:', 'CLEAN' if not problems else f'{len(problems)} problem(s)')
    for p in problems:
        print(' -', p)
    if a.report:
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}"
                   f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
        report(problems, lines, run_url)
    return 1 if problems and not a.report else 0      # with --report the issue carries the signal (like health.py)


if __name__ == '__main__':
    sys.exit(main())
