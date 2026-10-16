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
RECALL: per service, on the LATEST month that has an official list in official_arrivals.csv: its official arrivals
dated inside the 45-day New window that are anywhere on the service's New card. A drop of more than MAX_DROP points
below accuracy_baseline.json is a problem. NOTES (shown in the log and in the issue body when an issue is open anyway,
never a reason to open one): the lists are older than the run's month ("stale"), or fewer than MIN_OFFICIAL arrivals
of that month fall in the window (not measured). The lists are an optional manual refresh.
--report: open / comment on / close the "accuracy" issue (gh CLI, GITHUB_TOKEN with issues: write). Test mode as in
health.py: HEALTH_DRY_RUN=1 prints the writes; HEALTH_MOCK_ISSUES=<json> stands in for `gh issue list`.
TMDB_API_KEY from the environment (never printed). Reads only; never writes state.
"""
import argparse, csv, datetime as dt, gzip, json, os, random, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build                    # the one loader of the hand lists (build.load_hand_list); no network at import
import accuracy_frame           # the shared frame for the Popular / Top cards (issue #92)
import accuracy_people          # Director / Actor cards against Wikidata (issue #93)

SCHEDULES = {'vt', 'fb', 'wodp'}
ORPHAN_NETWORKS = {'netflix': {213}, 'hbo-max': {49, 3186, 8304}}      # the sets arrivals.py uses (F2 / F3)
LAG_DAYS = 14
WINDOW = 45                     # the New card's arrival window (arrivals.WINDOW)
MIN_OFFICIAL = 10               # fewer official arrivals of the measured month in the window: not measured (a note)
MAX_DROP = 3.0                  # recall points below the baseline that open the issue
UA = 'strand-discovery/1.0 (accuracy check; +https://github.com/xBOBxSAGETx/strand-discovery)'
TITLE = 'Weekly accuracy check'
FRAME_CARDS = 12                # Popular / Top frame (accuracy_frame.py): seeded cards per kind
FRAME_SAMPLE = 20               # card titles checked per card (precision)
FRAME_SEED = 'audit-92'         # fixed, so the baseline compares like with like
FRAME_MAX_DROP = 5.0            # points below accuracy_baseline.json "frame" that open the issue
PEOPLE_CARDS = 12               # Director / Actor frame (accuracy_people.py): seeded cards per kind that Wikidata knows
PEOPLE_SPARE = 3                # extra cards of the seeded order fetched from the live site, for cards Wikidata does not know
PEOPLE_SEED = 'audit-93'


def tmdb(path, **params):
    key = os.environ.get('TMDB_API_KEY')
    if not key:
        sys.exit('TMDB_API_KEY is not set')
    time.sleep(0.25)
    try:
        with urllib.request.urlopen(f'https://api.themoviedb.org/3{path}?{urllib.parse.urlencode({**params, "api_key": key})}',
                                    timeout=30) as r:
            return json.load(r)
    except Exception as e:                     # never surface the URL (it carries the key)
        raise RuntimeError(f'TMDB {type(e).__name__} on {path}') from None


def fetch_site(site, pages, audit_slugs=()):
    """The live site's summary.json + every shelf's first page + the New cards' further pages (terminal page included),
    politely: one request a second, one at a time."""
    site = site.rstrip('/')

    def get(rel):
        time.sleep(1.0)
        with urllib.request.urlopen(urllib.request.Request(f'{site}/{rel}', headers={'User-Agent': UA}), timeout=60) as r:
            return r.read()
    pages.mkdir(parents=True, exist_ok=True)
    (pages / 'summary.json').write_bytes(get('summary.json'))
    summary = json.loads((pages / 'summary.json').read_text(encoding='utf-8'))
    d = pages / 'catalog' / 'movie'
    d.mkdir(parents=True, exist_ok=True)
    for cid in summary.get('catalogs', {}):          # every shelf's first page (the weekly DuckDB read + week compare)
        (d / f'{cid}.json').write_bytes(get(f'catalog/movie/{cid}.json'))
    for slug in list(summary.get('new_card_modes', {})) + list(audit_slugs):   # these cards also page through
        cid = f'sd-{slug}'
        (d / cid).mkdir(parents=True, exist_ok=True)
        first = (d / f'{cid}.json').read_bytes()
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
        # measured on the LATEST month that has an official list for this service (lists are a manual refresh)
        month = max(r['date'][:7] for r in official_rows if r['service'] == svc)
        inwin = {f"{r['media']}:{r['tmdb_id']}" for r in official_rows
                 if r['service'] == svc and r['date'][:7] == month and lo <= r['date'] <= today}
        base = baseline.get('services', {}).get(svc)
        stale = month < today[:7]
        if len(inwin) < MIN_OFFICIAL:                        # a note, never a problem
            out.append({'service': svc, 'status': 'skipped', 'official_in_window': len(inwin), 'month': month,
                        'stale': stale, 'note': f'only {len(inwin)} official arrivals of {month} dated {lo}..{today} '
                                                f'(need {MIN_OFFICIAL}): not measured'})
            continue
        order = set(card_order(pages, slug))
        found = len(inwin & order)
        pct = round(100 * found / len(inwin), 1)
        drop = round(base - pct, 1) if base is not None else None
        out.append({'service': svc, 'status': 'measured', 'official_in_window': len(inwin), 'found': found,
                    'recall': pct, 'baseline': base, 'drop': drop, 'mode': summary.get('new_card_modes', {}).get(slug),
                    'month': month, 'stale': stale, 'note': f'measured on {month}',
                    'problem': drop is not None and drop > MAX_DROP})
    return out


SHELF_COLS = "{'metas': 'STRUCT(id VARCHAR, type VARCHAR, name VARCHAR)[]'}"


def snapshot(pages, snap_dir, ok_keys):
    """DuckDB reads EVERY shelf file (catalog/*/sd-*.json) of the site copy and saves a compact table
    (card, position, key, name) as shelves.parquet, plus ok.csv = the titles the precision audit marked correct this
    week (class 'ok'). Returns (files read, titles). The weekly artifact is just this folder; no database file is kept."""
    import duckdb
    snap_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    src = f"read_json('{(pages / 'catalog').as_posix()}/*/sd-*.json', columns={SHELF_COLS}, filename=true)"
    files = con.execute(f'select count(*) from {src}').fetchone()[0]
    con.execute(f"create table shelf as select parse_filename(filename, true) as card, "
                f"generate_subscripts(metas, 1) as position, unnest(metas) as m from {src}")
    out = (snap_dir / 'shelves.parquet').as_posix()
    con.execute("copy (select card, position, m.type || ':' || split_part(m.id, ':', 2) as key, m.name as name "
                f"from shelf order by card, position) to '{out}' (format parquet)")
    titles = con.execute('select count(*) from shelf').fetchone()[0]
    with open(snap_dir / 'ok.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['card', 'key'])
        w.writerows(sorted(ok_keys))
    return files, titles


def compare_weeks(prev_dir, cur_dir):
    """Last week's snapshot vs this week's: every title that left a card, and those of them the audit marked correct
    last week (ok.csv). None when there is no last week. DuckDB does the joins."""
    import duckdb
    prev, cur = Path(prev_dir) / 'shelves.parquet', Path(cur_dir) / 'shelves.parquet'
    if not prev.exists():
        return None
    con = duckdb.connect()
    con.execute(f"create table prev as select * from read_parquet('{prev.as_posix()}')")
    con.execute(f"create table cur as select * from read_parquet('{cur.as_posix()}')")
    ok = Path(prev_dir) / 'ok.csv'
    con.execute(f"create table okp as select * from read_csv('{ok.as_posix()}', header=true, all_varchar=true)"
                if ok.exists() else 'create table okp(card varchar, key varchar)')
    con.execute("create table lost as select p.card, p.key, p.name, p.position, "
                "exists(select 1 from okp o where o.card = p.card and o.key = p.key) as was_ok "
                "from prev p where not exists (select 1 from cur c where c.card = p.card and c.key = p.key)")
    per_card = con.execute('select card, count(*) n from lost group by card order by n desc, card').fetchall()
    lost_ok = con.execute('select card, key, name from lost where was_ok order by card, position').fetchall()
    return {'prev_titles': con.execute('select count(*) from prev').fetchone()[0],
            'lost_titles': sum(n for _, n in per_card), 'cards_with_losses': len(per_card),
            'top_cards': per_card[:10], 'lost_audited_ok': lost_ok}


def week_notes(cmp):
    if cmp is None:
        return ['weekly compare: no last week yet']
    notes = [f"weekly compare: {cmp['lost_titles']} of last week's {cmp['prev_titles']} shelf titles left their card "
             f"({cmp['cards_with_losses']} cards)"]
    if cmp['top_cards']:
        notes.append('weekly compare: most losses: ' + ', '.join(f'{c} {n}' for c, n in cmp['top_cards']))
    notes += [f'weekly compare: {k} "{name}" was marked correct by last week\'s audit and is gone from {card}'
              for card, k, name in cmp['lost_audited_ok']]
    return notes


def report(problems, lines, run_url):
    import health                                # reuse the gh helpers + dry-run / mock mode of the health issue
    health.ensure_label('accuracy', '1d76db', 'Weekly accuracy check (opened/closed automatically)')
    existing = json.loads(health.gh('issue', 'list', '--label', 'accuracy', '--state', 'open', '--json',
                                    'number,title', '--limit', '30') or '[]')
    existing = [i for i in existing if i.get('title') == TITLE]    # other issues carry the label too: only our own
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
    ap.add_argument('--snapshot', help="dir to write this week's shelf snapshot (shelves.parquet + ok.csv)")
    ap.add_argument('--prev', help="dir holding last week's snapshot (missing / empty: 'no last week yet')")
    ap.add_argument('--n', type=int, default=20)
    ap.add_argument('--seed', default=None)
    ap.add_argument('--official', default=str(HERE / 'official_arrivals.csv'))
    ap.add_argument('--unknowns', default=str(HERE / 'precision_unknowns.json'))
    ap.add_argument('--baseline', default=str(HERE / 'accuracy_baseline.json'))
    ap.add_argument('--spec', default=str(HERE / 'spec.json'))
    ap.add_argument('--cards', type=int, default=FRAME_CARDS, help='Popular / Top frame: seeded cards per kind (0 = off)')
    ap.add_argument('--frame-sample', type=int, default=FRAME_SAMPLE, help='Popular / Top frame: card titles checked per card')
    ap.add_argument('--frame-seed', default=FRAME_SEED, help='fixed seed of the card + title sample (a stable baseline)')
    ap.add_argument('--people-cards', type=int, default=PEOPLE_CARDS, help='Director / Actor frame: seeded cards per kind (0 = off)')
    ap.add_argument('--people-seed', default=PEOPLE_SEED, help='fixed seed of the Director / Actor card + title sample')
    ap.add_argument('--today', help='tests only: the date to judge against (default: the build date)')
    a = ap.parse_args()
    pages, state_dir, out = Path(a.pages), Path(a.state), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(a.spec).read_text(encoding='utf-8'))
    frame_cards = {k: accuracy_frame.pick_cards(spec, k, a.cards, a.frame_seed) for k in accuracy_frame.KINDS} if a.cards else {}
    people_pool = {k: accuracy_people.pick_people(spec, k, a.people_seed) for k in accuracy_people.PROP} if a.people_cards else {}
    if a.fetch_site:
        fetch_site(a.fetch_site, pages, [c['slug'] for cs in frame_cards.values() for c in cs] +
                   [c['slug'] for cs in people_pool.values() for c in cs[:a.people_cards + PEOPLE_SPARE]])
    summary = json.loads((pages / 'summary.json').read_text(encoding='utf-8'))
    cands = load_candidates(state_dir, summary['generated_at'][:10])
    if a.today:                                  # tests only: judge the recall window / list month as of this date
        cands = dict(cands, date=a.today)
    today = cands['date']                        # the date the build ordered the cards with
    official_rows = [{'service': svc, 'date': e['date'], 'media': e['type'], 'tmdb_id': str(e['tmdb'])}
                     for svc, es in build.load_hand_list('official_arrivals', path=a.official).items() for e in es]
    official = {}
    for r in official_rows:
        official.setdefault(r['service'], set()).add(f"{r['media']}:{r['tmdb_id']}")
    unknowns = {(prov, f"{e['type']}:{e['tmdb']}"): e['source']
                for prov, es in build.load_hand_list('precision_unknowns', path=a.unknowns).items() for e in es}
    baseline = json.loads(Path(a.baseline).read_text(encoding='utf-8')) if Path(a.baseline).exists() else {}
    seed = a.seed or dt.date.fromisoformat(today).strftime('%G-W%V')          # a new sample every week
    table, rows = precision(pages, state_dir, spec, summary, cands, a.n, seed, official, unknowns)
    rec = recall(pages, summary, official_rows, today, baseline)
    with open(out / 'precision.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ['provider'])
        w.writeheader()
        w.writerows(rows)
    errors = [r for r in rows if r['class'] == 'ERROR (ii)']
    frame_sum, frame_gaps = [], []
    for kind, cs in frame_cards.items():          # Popular / Top: recall + precision + labelled gaps (accuracy_frame.py)
        row, gaps = accuracy_frame.measure(kind, cs, spec, lambda slug: card_order(pages, slug), tmdb, dt.date.today().isoformat(),
                                           a.frame_sample, a.frame_seed)
        frame_sum.append(row)
        frame_gaps += gaps
    people_notes = []
    for kind, pool in people_pool.items():        # Director / Actor: against Wikidata (accuracy_people.py)
        try:
            row, gaps = accuracy_people.measure_people(kind, pool, spec, lambda slug: card_order(pages, slug), tmdb,
                                                       accuracy_people.wikidata, dt.date.today().isoformat(), a.people_cards,
                                                       a.frame_sample, a.people_seed)
        except RuntimeError as e:                 # Wikidata / TMDB down: a note, never a reason to open the issue
            people_notes.append(f'{kind} cards not measured this week: {e}')
            continue
        frame_sum.append(row)
        frame_gaps += gaps
    if frame_gaps:
        with open(out / 'frame_gaps.csv', 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(frame_gaps[0]))
            w.writeheader()
            w.writerows(frame_gaps)
    lines = ['| provider | dated | sample | on service | lag (i) | official (iii) | documented | errors (ii) |',
             '|---|---|---|---|---|---|---|---|']
    lines += [f"| {t['provider']} | {t['dated_items']} | {t['sample']} | {t['on_service']} | {t['lag_i']} | "
              f"{t['official_iii']} | {t['documented_ii']} | {t['errors_ii']} |" for t in table]
    lines += ['', '| service | recall | baseline | official arrivals in window | note |', '|---|---|---|---|---|']
    lines += [f"| {r['service']} | {r.get('recall', '-')} | {r.get('baseline', '-')} | {r['official_in_window']} | "
              f"{r.get('note', '')} |" for r in rec]
    fbase = baseline.get('frame', {})
    if frame_sum:
        lines += ['', '| kind | cards | recall | baseline | claim titles found | precision | baseline | sample ok | recall gaps (code bug / '
                  'deliberate / cap / source) | precision gaps (same order) |', '|---|---|---|---|---|---|---|---|---|---|']
        for r in frame_sum:
            b = fbase.get(r['kind'], {})
            g = lambda d: ' / '.join(str(d[x]) for x in accuracy_frame.LABELS)
            lines.append(f"| {r['kind']} | {r['cards']} | {r['recall']} | {b.get('recall', '-')} | {r['found']}/{r['truth']} | "
                         f"{r['precision']} | {b.get('precision', '-')} | {r['ok']}/{r['sample']} | {g(r['recall_gaps'])} | "
                         f"{g(r['precision_gaps'])} |")
    print('\n'.join(lines))
    problems = [f"precision: {e['provider']} {e['key']} not on the service ({e['precision']} precision, event "
                f"{e['event_date']}, sources {e['sources']}, status {e['status']}) {e['urls'][:200]}" for e in errors]
    problems += [f"recall: {r['service']} {r['recall']}% is {r['drop']} points below the baseline {r['baseline']}%"
                 for r in rec if r.get('problem')]
    for r in frame_sum:                          # a drop below the baseline, or more code bugs than the baseline had
        b = fbase.get(r['kind'])
        if not b:
            continue
        for what in ('recall', 'precision'):
            if r[what] is not None and b.get(what) is not None and b[what] - r[what] > FRAME_MAX_DROP:
                problems.append(f"{r['kind']} {what}: {r[what]}% is {round(b[what] - r[what], 1)} points below the baseline {b[what]}%")
        if r['code_bugs'] > b.get('code_bugs', 0):
            problems.append(f"{r['kind']}: {r['code_bugs']} code-bug gaps (baseline {b.get('code_bugs', 0)}); see frame_gaps.csv")
    # NOTES are shown, never problems: stale official lists (a manual refresh; the precision gate is the weekly guard)
    # and services with too few official arrivals in the window to measure
    notes = list(people_notes)
    stale = sorted({r['month'] for r in rec if r.get('stale')})
    if stale:
        notes.append(f"stale: official lists are for {', '.join(stale)} (run {today[:7]}); recall is measured on "
                     "that month - refresh generator/official_arrivals.csv + accuracy_baseline.json when convenient")
    notes += [f"recall not measured for {r['service']}: {r['note']}" for r in rec if r['status'] == 'skipped']
    cmp = None
    if a.snapshot:                               # DuckDB reads all shelves; then the week-over-week compare (notes only)
        ok_keys = {(f"sd-streaming-{r['provider']}-new", r['key']) for r in rows if r['class'] == 'ok'}
        files, titles = snapshot(pages, Path(a.snapshot), ok_keys)
        print(f'DuckDB read {files} shelf files, {titles} titles')
        cmp = compare_weeks(a.prev, a.snapshot) if a.prev else None
        notes += week_notes(cmp)
    print('RESULT:', 'CLEAN' if not problems else f'{len(problems)} problem(s)')
    for p in problems:
        print(' -', p)
    for n in notes:
        print(' NOTE:', n)
    (out / 'accuracy.json').write_text(json.dumps({'date': today, 'seed': seed, 'precision': table, 'recall': rec,
                                                   'errors': errors, 'frame': frame_sum, 'notes': notes, 'week_compare': cmp}, indent=1), encoding='utf-8')
    if a.report:
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}"
                   f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
        report(problems, [f'**Note:** {n}' for n in notes] + [''] + lines if notes else lines, run_url)
    return 1 if problems and not a.report else 0      # with --report the issue carries the signal (like health.py)


if __name__ == '__main__':
    sys.exit(main())
