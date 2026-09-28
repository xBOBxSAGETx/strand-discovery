"""Fake-server test of the automatic arrivals catch-up (generator/arrivals.py catchup()). Offline, standard library
only: a local 127.0.0.1 server stands in for the sites; TMDB is never called.

    python -m unittest discover tests            (part of the suite)
    python tests/test_catchup.py                 (standalone, prints every check)

Fake Vital Thrills: 30 in-window monthly posts (10 services x Aug/Sep/Oct 2026) + out-of-window posts in 2 post
sitemaps; its tag feed carries 3 of them. Fake whatsondisneyplus: 9 in-window posts (3 services x 3 months), feed 1.
Each simulated day is a separate process (this file with --runner) over the SAME state dir, like the daily workflow.
  run 1 (09-28) VT answers 429 (Retry-After: 1) after 8 posts -> breaker stops VT; step exits 0; 8 VT posts done
                (3 feed + 5 catch-up) persisted in the first_seen state with their signals; no VT health warning
  run 2 (09-29) HTTP cache DELETED first (progress must come from the first_seen state): resumes, 20 new posts,
                0 requests for the 5 posts the catch-up read in run 1; wodp (done in run 1) makes 0 requests
  run 3 (09-30) the last 2 posts -> 30/30 done
  run 4 (10-01) done: 0 catch-up requests (no sitemap, no post), reports done
  run 5 (10-03) re-scan day (SD_HTTP_REVALIDATE=1 so the fake listing is re-read): a new November post appears ->
                exactly that 1 post is fetched
  run 6 (10-04) the catch-up raises -> step exits 0, warning 'catch-up failed', stored progress unchanged, the
                feed rows still merged
  weekly re-read: none before day 7; 10-05 a cycle queues the 8 current/next-month non-feed done posts, VT 429s
                after 3 (queue kept, note only); 10-06 the other 5 incl. a post with a newly added title (picked up);
                10-07 nothing. Feed-path 429 (10-07) = ONE health warning
  run 7 (10-08) PARSER_VERSION bumped -> the catch-up re-opens by itself (cap 20 per run again)
Also: health.py (dry run) prints the progress lines and writes them to the step summary.

The runner (one simulated daily run, child process) patches: urlopen (the real hosts are rewritten to the fake
server; any other host = AssertionError, never sent), TMDB resolution + presence (deterministic fakes), SOURCES =
vt + wodp only, per-host delays 0.05 s. Everything else - get_parsed, the circuit breaker, src_vt / src_wodp,
catchup, merge, refresh, the state write - is the real code.
"""
import gzip, hashlib, http.server, json, os, re, shutil, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GEN = REPO / 'generator'

# ------------------------------------------------------------------ runner (child process) ------------------------


def _runner():
    import urllib.request
    sys.path.insert(0, os.environ['GEN'])
    import arrivals as a

    print(f'production politeness: DELAY={a.DELAY} DEFAULT_DELAY={a.DEFAULT_DELAY} '
          f'RETRY_AFTER_MAX={a.RETRY_AFTER_MAX} CATCHUP_MAX={a.CATCHUP_MAX} UA={a.UA!r}', flush=True)
    base = os.environ['FAKE_BASE']
    real = {'https://www.vitalthrills.com': '/vt', 'https://whatsondisneyplus.com': '/wodp'}
    real_open = urllib.request.urlopen

    def fake_open(req, timeout=None):
        url = req.full_url
        for pre, key in real.items():
            if url.startswith(pre):
                return real_open(urllib.request.Request(base + key + url[len(pre):],
                                                        headers=dict(req.header_items())), timeout=timeout)
        raise AssertionError(f'unexpected host (not sent): {url}')

    urllib.request.urlopen = fake_open
    a.DELAY = {'film-book.com': 0.05, 'vitalthrills.com': 0.05}
    a.DEFAULT_DELAY = 0.05
    a.SOURCES = {'vt': a.src_vt, 'wodp': a.src_wodp}
    a.CATCHUP_MAX = dict(a.CATCHUP_MAX)
    if os.environ.get('FAKE_PARSER_VERSION'):
        a.PARSER_VERSION = int(os.environ['FAKE_PARSER_VERSION'])

    def fake_resolve(row, present):
        tid = int(hashlib.sha256(row['title'].encode()).hexdigest()[:7], 16)
        return (True, row.get('media') or 'movie', tid, row['title'], row.get('year'), 1.0, 'fake exact')

    a.resolve = fake_resolve
    a.Presence.__call__ = lambda self, svc, media, tid: True
    if os.environ.get('FAKE_RAISE') == '1':
        def boom(*args, **kw):
            raise RuntimeError('injected catch-up failure (test)')
        a.sitemap_posts = boom
    sys.argv = ['arrivals.py', os.environ['OUT']]
    a.main()
    print('RUNNER EXIT OK', flush=True)


# ------------------------------------------------------------------ fake sites -----------------------------------
VT_SVCS = ['netflix', 'hbo-max', 'starz', 'peacock', 'apple-tv', 'prime-video', 'paramount-plus', 'tubi', 'britbox',
           'acorn-tv']
MONTHS = ['august', 'september', 'october']
VT_POSTS = [f'{s}-{m}-2026' for m in MONTHS for s in VT_SVCS]
VT_EXTRA = ['netflix-june-2026', 'netflix-october-2025', 'best-horror-movies-2026']      # out of window / not a schedule
VT_FEED = ['netflix-october-2026', 'hbo-max-october-2026', 'starz-october-2026']
WODP_POSTS = [f'whats-coming-to-{k}-{m}-2026{"-us" if k == "disney-in" else ""}'
              for m in MONTHS for k in ('disney-in', 'hulu-hulu-on-disney-in', 'hbo-max-in')]
WODP_FEED = ['whats-coming-to-hbo-max-in-october-2026']
CTRL = {}
LOG = []                                                     # (run, site, path, status, user-agent)


def urlset(site, slugs):
    return ('<?xml version="1.0"?><urlset>' + ''.join(f'<url><loc>{site}/{s}/</loc></url>' for s in slugs) +
            '</urlset>')


def rss(site, slugs):
    items = ''.join(f'<item><title>{s}</title><link>{site}/{s}/</link><pubDate>Fri, 25 Sep 2026 10:00:00 +0000'
                    f'</pubDate></item>' for s in slugs)
    return f'<?xml version="1.0"?><rss><channel>{items}</channel></rss>'


def vt_page(slug):
    m = re.match(r'(.+)-([a-z]+)-(20\d\d)$', slug)
    svc, mon = m.group(1), m.group(2)
    t = f'{svc.replace("-", " ").title()} {mon.title()}'
    return (f'<html><body><article><div class="entry-content"><h2>{t} 2026 Schedule</h2>'
            f'<p>{mon.title()} 3</p><ul><li>{t} Film One (2021)</li><li>{t} Film Two (2019)</li></ul>'
            f'<p>{mon.title()} 17</p><ul><li>{t} Film Three (2024)</li>'
            + ''.join(f'<li>{x}</li>' for x in CTRL['extra'].get(slug, [])) + '</ul></div></article></body></html>')


def wodp_page(slug):
    m = re.match(r'whats-coming-to-(.+)-in-([a-z]+)-2026', slug)
    t = f'{m.group(1).replace("-", " ").title()} {m.group(2).title()}'
    return (f'<html><body><article><div class="entry-content"><h2>{m.group(2).title()} 5</h2>'
            f'<ul><li>{t} Movie One (2022)</li><li>{t} Movie Two (2020)</li></ul></div></article></body></html>')


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        site, _, path = self.path.lstrip('/').partition('/')
        path = '/' + path
        code, body, headers = 404, '', {}
        if site == 'vt':
            real = 'https://www.vitalthrills.com'
            slug = path.strip('/')
            if CTRL['vt_ok_posts'] is not None and CTRL['vt_posts_ok'] >= CTRL['vt_ok_posts']:
                code, headers = 429, {'Retry-After': '1'}
            elif path == '/sitemap_index.xml':
                code, body = 200, ('<?xml version="1.0"?><sitemapindex>'
                                   f'<sitemap><loc>{real}/post-sitemap.xml</loc></sitemap>'
                                   f'<sitemap><loc>{real}/post-sitemap2.xml</loc></sitemap>'
                                   f'<sitemap><loc>{real}/page-sitemap.xml</loc></sitemap></sitemapindex>')
            elif path == '/post-sitemap.xml':
                code, body = 200, urlset(real, VT_EXTRA + VT_POSTS[:12])
            elif path == '/post-sitemap2.xml':
                code, body = 200, urlset(real, VT_POSTS[12:] + CTRL['vt_new'])
            elif path == '/tag/streaming-schedule/feed/':
                code, body = 200, rss(real, VT_FEED)
            elif slug in VT_POSTS + VT_EXTRA + CTRL['vt_new']:
                code, body = 200, vt_page(slug)
                CTRL['vt_posts_ok'] += 1
        elif site == 'wodp':
            real = 'https://whatsondisneyplus.com'
            slug = path.strip('/')
            if path == '/sitemap.xml':
                code, body = 200, ('<?xml version="1.0"?><sitemapindex>'
                                   f'<sitemap><loc>{real}/post-sitemap.xml</loc></sitemap></sitemapindex>')
            elif path == '/post-sitemap.xml':
                code, body = 200, urlset(real, WODP_POSTS + ['whats-coming-to-disney-in-october-2026'])  # non-US
            elif path == '/feed/':
                code, body = 200, rss(real, WODP_FEED)
            elif slug in WODP_POSTS:
                code, body = 200, wodp_page(slug)
        LOG.append((CTRL['run'], site, path, code, self.headers.get('User-Agent', '')))
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        data = body.encode()
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Server(http.server.ThreadingHTTPServer):
    request_queue_size = 128                                 # LESSONS: never the default 5
    daemon_threads = True


# ------------------------------------------------------------------ the scenario ---------------------------------


def harness(work, verbose=True):
    """Runs every simulated day in `work`; returns the list of failed checks (empty = pass)."""
    work = Path(work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    problems = []
    CTRL.clear()
    CTRL.update({'run': 0, 'vt_ok_posts': None, 'vt_posts_ok': 0, 'vt_new': [], 'extra': {}})
    LOG.clear()

    def say(msg):
        if verbose:
            print(msg, flush=True)

    def check(ok, msg):
        say(('ok   ' if ok else 'FAIL ') + msg)
        if not ok:
            problems.append(msg)

    srv = Server(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{srv.server_address[1]}'
    state, http_cache, cache = work / 'state', work / 'http', work / 'cache'
    state.mkdir()
    with gzip.open(state / 'first_seen.json.gz', 'wt', encoding='utf-8') as fh:   # a logger state exists
        json.dump({'providers': {}, 'runs': []}, fh)

    def load_state():
        with gzip.open(state / 'first_seen.json.gz', 'rt', encoding='utf-8') as fh:
            return json.load(fh)

    def run(n, today, **env_extra):
        CTRL['run'], CTRL['vt_posts_ok'] = n, 0
        out = work / f'out{n}'
        env = {**os.environ, 'GEN': str(GEN), 'FAKE_BASE': base, 'SD_STATE': str(state),
               'SD_HTTP_CACHE': str(http_cache), 'SD_CACHE': str(cache), 'SD_TODAY': today, 'OUT': str(out),
               'TMDB_API_KEY': 'unused-fake', 'PYTHONIOENCODING': 'utf-8', **env_extra}
        env.pop('SD_HTTP_REVALIDATE', None) if 'SD_HTTP_REVALIDATE' not in env_extra else None
        t0 = time.time()
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--runner'], env=env,
                           capture_output=True, text=True, encoding='utf-8')
        say(f'\n===== run {n} ({today}) exit {p.returncode} in {time.time() - t0:.1f} s =====')
        say(p.stdout.rstrip())
        if p.stderr.strip():
            say('--- stderr ---\n' + p.stderr.rstrip()[-3000:])
        f = out / 'arrivals_summary.json'
        summ = json.loads(f.read_text(encoding='utf-8')) if f.exists() else {}
        say('catchup_lines: ' + json.dumps(summ.get('catchup_lines'), indent=1))
        say(f"warnings: {summ.get('warnings')}")
        reqs = [r for r in LOG if r[0] == n]
        say(f'requests this run: vt {sum(r[1] == "vt" for r in reqs)} (429: {sum(r[3] == 429 for r in reqs)}), '
            f'wodp {sum(r[1] == "wodp" for r in reqs)}')
        return p, summ, reqs

    VT = lambda s: f'https://www.vitalthrills.com/{s}/'
    done_of = lambda st, src: set(st.get('catchup', {}).get('sources', {}).get(src, {}).get('done', {}))
    sig_urls = lambda st: {o[6] for prov in st['providers'].values() for v in (prov.get('signals') or {}).values()
                           for o in v['o']}
    post_paths = lambda reqs, site: [r[2].strip('/') for r in reqs if r[1] == site and r[2].count('/') == 2 and
                                     not r[2].endswith(('.xml', '/feed/'))]
    try:
        # ---- run 1: 429 mid-catch-up
        CTRL['vt_ok_posts'] = 8
        p1, s1, r1 = run(1, '2026-09-28')
        CTRL['vt_ok_posts'] = None
        st1 = load_state()
        d1 = done_of(st1, 'vt')
        check(p1.returncode == 0 and 'RUNNER EXIT OK' in p1.stdout, '(a) run 1 exits 0 despite the 429')
        check(sum(1 for r in r1 if r[1] == 'vt' and r[3] == 429) == 2,
              '(a) exactly 2 VT 429s: the first honoured (Retry-After), the second stops the host')
        last_429 = max(i for i, r in enumerate(LOG) if r[0] == 1 and r[3] == 429)
        check(not any(r[0] == 1 and r[1] == 'vt' for r in LOG[last_429 + 1:]), '(a) no VT request after the stop')
        check(len(d1) == 8 and set(VT_FEED) <= {u.split('/')[-2] for u in d1},
              f'(a) progress persisted: 8 VT posts done in the first_seen state (3 feed + 5 catch-up): {len(d1)}')
        check(d1 <= sig_urls(st1), '(a) every post marked done has its signals in the same state write')
        rep1 = s1.get('catchup', {}).get('vt', {})
        check(rep1.get('state', '').startswith('stopped: HTTP 429') and rep1.get('pending') == 22 and
              rep1.get('listed') == 30,
              f"(a) report: stopped, 8/30 done, 22 pending: {rep1.get('state')!r} {rep1.get('done')}/{rep1.get('listed')}")
        check(not any('vitalthrills' in w for w in s1.get('warnings', [])),
              '(a) the catch-up 429 is a note, not a health warning')
        check(s1.get('catchup', {}).get('wodp', {}).get('state') == 'done' and len(done_of(st1, 'wodp')) == 9,
              '(a) the other site (wodp) is unaffected: 9/9 done in run 1')
        check(all(r[4].startswith('strand-discovery/1.0') for r in r1), '(a) every request carries the identifying UA')
        check(not any('?s=' in r[2] or '&s=' in r[2] for r in LOG), 'no search URLs requested')
        catchup_run1 = d1 - {VT(s) for s in VT_FEED}
        check(all('-october-' in u or '-september-' in u for u in catchup_run1),
              f'newest month first: run 1 read {sorted(catchup_run1)}')

        # ---- run 2: resume from the first_seen state alone (HTTP cache deleted)
        shutil.rmtree(http_cache)
        p2, s2, r2 = run(2, '2026-09-29')
        st2 = load_state()
        d2 = done_of(st2, 'vt')
        vt_posts2 = post_paths(r2, 'vt')
        refetched = {VT(s) for s in vt_posts2} & catchup_run1
        check(p2.returncode == 0, '(b) run 2 exits 0')
        check(not refetched, f'(b) 0 requests for the 5 posts the catch-up read in run 1 (refetched: {sorted(refetched)})')
        check(len(set(vt_posts2) - set(VT_FEED)) == 20,
              f'(b) exactly the cap (20) new catch-up posts: {len(set(vt_posts2) - set(VT_FEED))}')
        check(len(d2) == 28 and d1 <= d2, f'(b) 28/30 done after run 2: {len(d2)}')
        check(s2['catchup']['vt'].get('eta_runs') == 1 and s2['catchup']['vt'].get('pending') == 2,
              '(b) 2 left, ETA 1 run')
        check(not any(r[1] == 'wodp' and ('sitemap' in r[2] or r[2].strip('/') in WODP_POSTS and
                                          r[2].strip('/') not in WODP_FEED) for r in r2),
              '(b) wodp (done, not due) makes no catch-up request')
        check(d2 <= sig_urls(st2), '(b) signals of all done posts in the state')

        # ---- run 3: the last 2
        p3, s3, r3 = run(3, '2026-09-30')
        st3 = load_state()
        check(p3.returncode == 0 and len(done_of(st3, 'vt')) == 30 and s3['catchup']['vt']['state'] == 'done',
              f"(c) run 3 finishes the catch-up: {len(done_of(st3, 'vt'))}/30, {s3['catchup']['vt']['state']!r}")
        check(len(post_paths(r3, 'vt')) == 2, f'(c) run 3 fetched only the 2 pending posts: {post_paths(r3, "vt")}')

        # ---- run 4: done -> no fetching
        p4, s4, r4 = run(4, '2026-10-01')
        cu4 = [r for r in r4 if 'sitemap' in r[2] or
               (r[1] == 'vt' and r[2].strip('/') in VT_POSTS and r[2].strip('/') not in VT_FEED) or
               (r[1] == 'wodp' and r[2].strip('/') in WODP_POSTS and r[2].strip('/') not in WODP_FEED)]
        listing4 = [r[2] for r in r4 if r[1] == 'wodp' and r[2].endswith('.xml')]
        check(p4.returncode == 0 and not [r for r in r4 if r[1] == 'vt'],
              f"(c) VT done (scanned 09-30, not due): 0 VT requests on run 4 ({sum(r[1] == 'vt' for r in r4)})")
        check([r for r in cu4 if not r[2].endswith('.xml')] == [] and listing4 == ['/sitemap.xml', '/post-sitemap.xml'],
              f'(c) wodp re-scan day (scanned 09-28 + 3 days, HTTP cache emptied before run 2): only its 2 listing '
              f'requests, 0 posts: {listing4}')
        check(all(s4['catchup'][k]['state'].startswith('done') for k in ('vt', 'wodp')),
              f"(c) run 4 reports done: {s4['catchup_lines']}")

        # ---- run 4b: done, neither site due -> no request at all
        p4b, s4b, r4b = run(41, '2026-10-02')
        check(p4b.returncode == 0 and not r4b, f'(c) done and not due: 0 requests of any kind on 10-02 ({len(r4b)})')
        check(all(s4b['catchup'][k]['state'].startswith('done (listing re-checked on') for k in ('vt', 'wodp')),
              f"(c) 10-02 reports done: {s4b['catchup_lines']}")

        # ---- run 5: re-scan day, a new post appears
        CTRL['vt_new'] = ['netflix-november-2026']
        p5, s5, r5 = run(5, '2026-10-03', SD_HTTP_REVALIDATE='1')
        new5 = set(post_paths(r5, 'vt')) - set(VT_FEED)
        check(p5.returncode == 0 and new5 == {'netflix-november-2026'},
              f'(c) re-scan after 3 days fetches only the new post: {new5}')
        check(VT('netflix-november-2026') in done_of(load_state(), 'vt'), '(c) the new post is recorded done')

        # ---- run 6: the catch-up raises
        before6 = load_state().get('catchup')
        p6, s6, r6 = run(6, '2026-10-04', FAKE_RAISE='1')
        st6 = load_state()
        check(p6.returncode == 0 and 'RUNNER EXIT OK' in p6.stdout,
              '(exception) run 6 exits 0 although the catch-up raised')
        check(any(w.startswith('catch-up failed: RuntimeError') for w in s6.get('warnings', [])),
              '(exception) one health warning')
        check(st6.get('catchup') == before6, '(exception) stored progress unchanged')
        check((state / 'signal_candidates.json').exists() and s6['sources']['vt']['posts'] == 3,
              '(exception) the daily feed path still ran and the state + candidates were written')

        # ---- weekly re-read (cycle started 2026-09-28 with the first catch-up run)
        rr_of = lambda st, src='vt': st['catchup']['sources'][src].get('reread', {})
        check(rr_of(st1).get('last') == '2026-09-28' and not rr_of(st1).get('queue'),
              f're-read: cycle date set on the first run, nothing queued ({rr_of(st1)})')
        no_rr = [n for n, s in ((2, s2), (3, s3), (4, s4), (41, s4b), (5, s5))
                 if (s['catchup']['vt'].get('reread') or {}).get('read') or
                 (s['catchup']['vt'].get('reread') or {}).get('queued')]
        check(not no_rr and rr_of(load_state())['last'] == '2026-09-28',
              f're-read: none before 7 days have passed (runs 09-29 .. 10-04; re-read on runs {no_rr})')
        check(not post_paths(r4b, 'vt') and not post_paths(r6, 'vt'),
              're-read: 0 post requests on 10-02 and 10-04 (day 4 and 6)')

        # R1 (10-05, day 7): a title is added to an already-done post; VT answers 429 after 3 re-reads
        done_post = 'peacock-october-2026'
        check(VT(done_post) in done_of(load_state(), 'vt'), f're-read: {done_post} is an already-done (non-feed) post')
        new_title = 'Peacock October Film Four (2025)'
        CTRL['extra'][done_post] = [new_title]
        CTRL['vt_ok_posts'] = 3
        pR1, sR1, rR1 = run(51, '2026-10-05')
        CTRL['vt_ok_posts'] = None
        stR1 = load_state()
        rrR1 = sR1['catchup']['vt'].get('reread', {})
        vt_rr1 = [r for r in rR1 if r[1] == 'vt']
        check(pR1.returncode == 0, '(re-read 429) the run exits 0')
        check(rr_of(stR1)['last'] == '2026-10-05' and rrR1.get('read') == 3 and rrR1.get('queued') == 5 and
              rrR1.get('stopped', '').startswith('HTTP 429'),
              f'(re-read 429) cycle started 10-05: 8 queued (Oct non-feed 7 + Nov 1), 3 re-read, stopped by the 429, '
              f'5 kept: {rrR1}')
        check(sum(r[3] == 429 for r in vt_rr1) == 2 and vt_rr1[-1][3] == 429,
              f'(re-read 429) exactly 2 VT 429s and no VT request after the stop ({[(r[2], r[3]) for r in vt_rr1]})')
        check(not any('vitalthrills' in w for w in sR1.get('warnings', [])), '(re-read 429) a note, not a health warning')
        targets = {f'{s}-october-2026' for s in VT_SVCS} - set(VT_FEED) | {'netflix-november-2026'}
        check(set(post_paths(rR1, 'vt')) <= targets and len(targets) == 8,
              f're-read: only current (Oct) / next (Nov) month non-feed done posts requested: {post_paths(rR1, "vt")}')
        check('weekly re-read: 3 re-read, 5 queued' in ' '.join(sR1['catchup_lines']),
              f"re-read progress in the health line: {sR1['catchup_lines'][0]}")

        # R2 (10-06): the queue continues within the cap; the new title in the done post is picked up
        new_key = f"movie:{int(hashlib.sha256(new_title.replace(' (2025)', '').encode()).hexdigest()[:7], 16)}"
        check(new_key not in (stR1['providers'].get('peacock', {}).get('signals') or {}),
              '(new title) not in the state before its re-read')
        pR2, sR2, rR2 = run(52, '2026-10-06')
        stR2 = load_state()
        rrR2 = sR2['catchup']['vt'].get('reread', {})
        check(pR2.returncode == 0 and rrR2.get('read') == 5 and rrR2.get('queued') == 0 and
              done_post in post_paths(rR2, 'vt'),
              f're-read: the 5 queued posts re-read on 10-06, queue empty: {rrR2} {post_paths(rR2, "vt")}')
        sig = (stR2['providers'].get('peacock', {}).get('signals') or {}).get(new_key)
        check(bool(sig) and any(o[6] == VT(done_post) for o in sig['o']),
              f'(new title) the title added to the done post is in the state after the re-read: {new_key} {sig}')

        # R3 (10-07): cycle done, next one on 10-12 -> no re-read
        pR3, sR3, rR3 = run(53, '2026-10-07')
        check(pR3.returncode == 0 and not [r for r in rR3 if r[1] == 'vt'] and
              sR3['catchup']['vt']['reread'].get('next_cycle') == '2026-10-12',
              f"re-read: no VT request 2 days into the cycle (next cycle {sR3['catchup']['vt']['reread']})")

        # ---- 429 policy: a 429 in the REGULAR feed path stays ONE health warning per stopped host
        CTRL['vt_ok_posts'] = 0                                  # VT answers 429 to everything, the tag feed included
        pF, sF, rF = run(54, '2026-10-07', SD_HTTP_REVALIDATE='1')
        CTRL['vt_ok_posts'] = None
        vt_warn = [w for w in sF.get('warnings', []) if 'vitalthrills' in w]
        check(pF.returncode == 0 and len(vt_warn) == 1 and 'HTTP 429 twice' in vt_warn[0],
              f'(feed 429) ONE health warning for the stopped host: {vt_warn}')
        check(sum(r[1] == 'vt' for r in rF) == 2 and all(r[3] == 429 for r in rF if r[1] == 'vt'),
              f"(feed 429) 2 VT requests, both 429, then the host is left alone "
              f"({[(r[2], r[3]) for r in rF if r[1] == 'vt']})")
        check(sF['catchup']['vt']['state'].startswith('done') or sF['catchup']['vt']['state'].startswith('skipped'),
              f"(feed 429) the catch-up makes no VT request after the feed stop: {sF['catchup']['vt']['state']!r}")
        check(any('vitalthrills' in w for w in vt_warn) and not any('vitalthrills' in w for w in s1.get('warnings', [])),
              '(policy) feed-path 429 = warning (this run); catch-up 429 = note (run 1)')

        # ---- run 7: parser version bump re-opens the catch-up
        p7, s7, r7 = run(7, '2026-10-08', FAKE_PARSER_VERSION='99')
        rep7 = s7['catchup']['vt']
        check(p7.returncode == 0 and rep7['fetched'] == 20 and rep7['state'] == 'catching up',
              f"(version) PARSER_VERSION bump re-opens the catch-up by itself: fetched {rep7['fetched']}, "
              f"{rep7['state']!r}")

        # ---- health.py shows the progress (dry run, no GitHub calls)
        rep_dir = work / 'report'
        (rep_dir / 'arrivals').mkdir(parents=True)
        (rep_dir / 'out').mkdir()
        (rep_dir / 'out' / 'summary.json').write_text('{"first_seen": {"providers": {}}}', encoding='utf-8')
        shutil.copy(work / 'out2' / 'arrivals_summary.json', rep_dir / 'arrivals' / 'arrivals_summary.json')
        (work / 'mock_issues.json').write_text('{"health": [], "reminder": []}', encoding='utf-8')
        step_summary = work / 'step_summary.md'
        env = {**os.environ, 'HEALTH_DRY_RUN': '1', 'HEALTH_MOCK_ISSUES': str(work / 'mock_issues.json'),
               'GITHUB_REPOSITORY': 'test/test', 'GITHUB_STEP_SUMMARY': str(step_summary), 'PYTHONIOENCODING': 'utf-8'}
        h = subprocess.run([sys.executable, str(GEN / 'health.py'), str(rep_dir), 'success', 'success', 'success'],
                           env=env, capture_output=True, text=True, encoding='utf-8')
        say('\n===== health.py (dry run) on run 2 report =====\n' + h.stdout + h.stderr)
        check(h.returncode == 0 and 'progress: vt catch-up: 28/30 posts, 2 left, ETA 1 run' in h.stdout,
              'health.py prints the progress line')
        check(step_summary.exists() and 'vt catch-up: 28/30 posts' in step_summary.read_text(encoding='utf-8'),
              'health.py writes it to the run summary page')
        check('health: clean' in h.stdout, 'progress alone opens no health issue')
    finally:
        srv.shutdown()
        srv.server_close()
    say('\nRESULT: ' + ('PASS' if not problems else f'FAIL ({len(problems)}): ' + ' | '.join(problems)))
    return problems


class CatchupFakeServerTest(unittest.TestCase):
    def test_catchup_scenario(self):
        with tempfile.TemporaryDirectory(prefix='sd-catchup-') as tmp:
            problems = harness(Path(tmp) / 'work', verbose=bool(os.environ.get('SD_TEST_VERBOSE')))
        self.assertEqual(problems, [], 'catch-up fake-server checks failed (SD_TEST_VERBOSE=1 prints every check)')


if __name__ == '__main__':
    if '--runner' in sys.argv:
        _runner()
    else:
        with tempfile.TemporaryDirectory(prefix='sd-catchup-') as tmp:
            sys.exit(1 if harness(Path(tmp) / 'work') else 0)
