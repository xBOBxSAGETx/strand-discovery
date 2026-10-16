"""Accuracy audit 2/6 (issue #93, loop #33): Director and Actor cards, measured against WIKIDATA, not against TMDB credits.

The cards are built from TMDB credits (build.director / build.actor), so TMDB would be a circular truth. Wikidata is
edited separately: a film's director (P57) or cast member (P161), with the film's TMDB id (P4947 movie / P4983 TV
series) and the person's TMDB id (P4985). Same frame as accuracy_frame.py: seeded cards, RECALL, PRECISION, one label
per gap (code bug / deliberate rule / cap / source limit); the summary row has the same shape, so the check, the
baseline and the report table treat the two kinds like the Popular / Top kinds.

THE CLAIMS (what the card name promises, in plain words) and THE RULES in code (generator/build.py line numbers):
  director "Director . Name" = every film they directed, and the TV series they directed. (Owner, issue #30: "director
           cards don't list the director's entire portfolio".)
           Rule, build.director() :464-490: movie_credits crew with job Director / Co-Director and released (:470, released()
           :209); runtime >= 40 minutes, or unknown runtime with >= 500 votes (MIN_RUNTIME :460, KNOWN_VOTES :461, test :475)
           = SHORTS DROPPED; then tv_credits crew with job Director, one episode counts (:483), released only (:483), no
           talk / news / reality genres (NOT_ACTING :496, :484) and not a TV movie already listed as a movie by title + year
           (:480, :485); best-known (votes) first. No depth cap.
  actor    "Actor . Name" = the films and series they act in.
           Rule, build.actor() :501-539 on combined_credits cast: released only (:513), drops talk / news / reality
           (NOT_ACTING :496, :516), self / cameo / archive / uncredited characters (SELF :493, :517), TV roles of fewer
           than 5 episodes (MIN_EPISODES :497, :518), movie roles billed below #15 (MAX_BILLING :498, :519), titles with
           under 10 votes older than a year (:520), and voice roles (VOICE :494, :524-537) unless the film has >= 1000
           votes and the role is top-3 billed (LEAD_ORDER, LEAD_VOTES :495, :526) or most credits are voice (:531). No
           depth cap.

TRUTH for recall: every film / TV series on Wikidata whose director (P57) / cast member (P161) is the person (person
found by their TMDB id, P4985), released by today, that carries a TMDB id. A work with no TMDB id cannot be on a card:
it is listed (side 'unmapped', label source limit) and left out of the recall denominator. TRUTH for precision: of
the sampled card titles, those whose Wikidata item has any director / cast statement are JUDGED (the person is on
it = ok, absent = a gap); the rest are 'unjudged' (Wikidata has nothing to compare with) and left out of the
denominator, so a thin Wikidata cast list lowers the number of judged titles, not the score. A sampled title that
breaks the card's own rules (re-read from the person's TMDB credits) is a code bug.

LABELS of a missing title (recall): the person has no such TMDB credit = source limit (Wikidata and TMDB disagree); a
rule above drops it = deliberate rule (the reason says which: short, billing, voice, ...); it passes every rule and is
still not on the card = code bug (or the credits changed since the daily build).

Pure functions take plain dicts and are unit-tested offline (tests/test_accuracy_people.py).
"""
import datetime as dt, json, random, time, urllib.error, urllib.parse, urllib.request
import build
import accuracy_frame as af

WD_URL = 'https://query.wikidata.org/sparql'
UA = 'strand-discovery/1.0 (accuracy check; +https://github.com/xBOBxSAGETx/strand-discovery)'
PROP = {'director': 'P57', 'actor': 'P161'}
DIRECTOR_JOBS = {'Director', 'Co-Director'}
FILM, TV_SERIES, SHORT_FILM = 'wd:Q11424', 'wd:Q5398426', 'wd:Q24862'


WD_BUDGET = 150        # seconds one query may take over all its tries (a slow query costs one card, not the run)
WD_MAX_FAILS = 3       # this many queries in a row that gave up: Wikidata is treated as down ...
WD_COOLDOWN = 120      # ... for this many seconds (every call fails at once), then one query probes it again
_WD = {'fails': 0, 'last': 0.0}


def wikidata(query):
    """Run one SPARQL query -> [{var: value}], politely (1 s apart, up to 4 tries inside WD_BUDGET seconds). Never surfaces
    anything but the status. After WD_MAX_FAILS queries in a row gave up, calls fail at once for WD_COOLDOWN seconds."""
    if _WD['fails'] >= WD_MAX_FAILS and time.time() - _WD['last'] < WD_COOLDOWN:
        raise RuntimeError('Wikidata unavailable (several queries in a row gave up)')
    body = urllib.parse.urlencode({'query': query}).encode()
    start, why = time.time(), 'timeout'
    for attempt in range(4):
        time.sleep(1.0)
        try:
            req = urllib.request.Request(WD_URL, data=body, headers={'User-Agent': UA, 'Accept': 'application/sparql-results+json'})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            _WD['fails'] = 0
            return [{k: v['value'] for k, v in b.items()} for b in data['results']['bindings']]
        except urllib.error.HTTPError as e:
            why = f'HTTP {e.code}'
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:
            why = type(e).__name__
        if attempt < 3 and time.time() - start < WD_BUDGET:
            print(f'  wikidata {why}, try {attempt + 2} of 4 in {8 * (attempt + 1)} s: {query[:60]!r}', flush=True)
            time.sleep(8 * (attempt + 1))
            continue
        break
    _WD['fails'] += 1
    _WD['last'] = time.time()
    print(f'  wikidata gave up ({why}) after {round(time.time() - start)} s, {_WD["fails"]} in a row: {query[:60]!r}', flush=True)
    raise RuntimeError(f'Wikidata {why}' if why.startswith('HTTP') else f'Wikidata {why}')


def person_query(pid):
    return f'SELECT ?p WHERE {{ ?p wdt:P4985 "{int(pid)}" }} LIMIT 1'


def works_query(kind, pid):
    """Films + TV series whose director / cast member is the person: one row per work with its TMDB ids, earliest date
    and whether Wikidata calls it a short film."""
    return (f'SELECT ?w (MAX(?m0) AS ?m) (MAX(?t0) AS ?t) (MIN(?d0) AS ?d) (MAX(?s0) AS ?sh) WHERE {{ '
            f'?p wdt:P4985 "{int(pid)}". ?w wdt:{PROP[kind]} ?p. VALUES ?c {{ {FILM} {TV_SERIES} }} ?w wdt:P31/wdt:P279* ?c. '
            f'OPTIONAL {{ ?w wdt:P4947 ?m0 }} OPTIONAL {{ ?w wdt:P4983 ?t0 }} '
            f'OPTIONAL {{ ?w wdt:P577 ?d1 }} OPTIONAL {{ ?w wdt:P580 ?d2 }} BIND(COALESCE(?d1, ?d2) AS ?d0) '
            f'BIND(IF(EXISTS {{ ?w wdt:P31 {SHORT_FILM} }}, 1, 0) AS ?s0) }} GROUP BY ?w')


def parse_works(rows, today):
    """-> (works {key: {'wd', 'date', 'short'}}, unmapped [wd ids], unreleased count). key = 'movie:ID' / 'series:ID'."""
    works, unmapped, unreleased = {}, [], 0
    for r in rows:
        wd, date = r['w'].rsplit('/', 1)[-1], (r.get('d') or '')[:10]
        if date and date > today:
            unreleased += 1
            continue
        keys = ([f"movie:{r['m']}"] if r.get('m') else []) + ([f"series:{r['t']}"] if r.get('t') else [])
        if not keys:
            unmapped.append(wd)
        for k in keys:
            works[k] = {'wd': wd, 'date': date, 'short': r.get('sh') == '1'}
    return works, sorted(unmapped), unreleased


def sample_query(kind, media, ids):
    """Every director / cast member the Wikidata items of these TMDB ids list (one row each); one medium per query
    (a UNION of both media was too slow for the public endpoint)."""
    p4 = 'P4947' if media == 'movie' else 'P4983'
    vals = ' '.join(f'"{i}"' for i in ids)
    return f'SELECT ?id ?c WHERE {{ VALUES ?id {{ {vals} }} ?w wdt:{p4} ?id. ?w wdt:{PROP[kind]} ?c }}'


def parse_sample(rows, media, person):
    """-> {key: (statements, person_is_one)}; person = the person's Wikidata id (Q...)."""
    out = {}
    for r in rows:
        n, hit = out.get(f"{media}:{r['id']}", (0, False))
        out[f"{media}:{r['id']}"] = (n + 1, hit or r['c'].rsplit('/', 1)[-1] == person)
    return out


def pick_people(spec, kind, seed):
    """All cards of the kind in a seeded shuffle (stable for a given spec and seed); the first n that Wikidata knows are measured."""
    cards = sorted((c for c in spec['catalogs'] if c.get('kind') == kind), key=lambda c: c['slug'])
    random.Random(f'{seed}-{kind}').shuffle(cards)
    return cards


# ---------- the card rules, read off the person's TMDB credits (constants come from build.py, not copied) ----------

def released(item, media, today):
    date = item.get('release_date') if media == 'movie' else item.get('first_air_date')
    return bool(date) and date <= today


def director_ctx(movie_credits, tv_credits):
    """The person's crew credits by title: {'movie': {id: [credit]}, 'series': {id: [credit]}, 'film_keys': {(title, year)}}."""
    movie, series = {}, {}
    for c in movie_credits.get('crew', []):
        movie.setdefault(c['id'], []).append(c)
    for c in tv_credits.get('crew', []):
        series.setdefault(c['id'], []).append(c)
    film_keys = {((c.get('title') or '').casefold(), c['release_date'][:4]) for cs in movie.values() for c in cs
                 if c.get('job') in DIRECTOR_JOBS and c.get('release_date')}
    return {'movie': movie, 'series': series, 'film_keys': film_keys}


def director_status(media, tid, ctx, today, runtime_of):
    """None = the title passes every director rule (it should be on the card), else (label, reason)."""
    cs = ctx[media].get(tid)
    if not cs:
        return 'source limit', 'TMDB has no crew credit for this person on it (Wikidata and TMDB disagree)'
    if media == 'movie':
        c = next((x for x in cs if x.get('job') in DIRECTOR_JOBS), None)
        if not c:
            return 'deliberate rule', 'TMDB credits them as ' + '/'.join(sorted({x.get('job') or '?' for x in cs})) + ', not Director / Co-Director'
        if not released(c, 'movie', today):
            return 'deliberate rule', 'not released yet (released-only rule)'
        rt = runtime_of(tid)
        if rt < build.MIN_RUNTIME and not (rt == 0 and (c.get('vote_count') or 0) >= build.KNOWN_VOTES):
            return 'deliberate rule', f'short film: runtime {rt or "unknown"} min, floor {build.MIN_RUNTIME} (MIN_RUNTIME)'
        return None
    c = next((x for x in cs if x.get('job') == 'Director'), None)
    if not c:
        return 'deliberate rule', 'TMDB credits them as ' + '/'.join(sorted({x.get('job') or '?' for x in cs})) + ', not Director'
    if not released(c, 'series', today):
        return 'deliberate rule', 'not released yet (released-only rule)'
    if set(c.get('genre_ids') or []) & build.NOT_ACTING:
        return 'deliberate rule', 'talk / news / reality show (TV genre exclusion)'
    if ((c.get('name') or '').casefold(), (c.get('first_air_date') or '')[:4]) in ctx['film_keys']:
        return 'deliberate rule', 'a TV movie already listed as a movie (title + year)'
    return None


def actor_drop_reason(c, media, today):
    """The first rule that drops one cast credit (same order as build.actor), or None."""
    date = c.get('release_date') or c.get('first_air_date')
    year_ago = (dt.date.fromisoformat(today) - dt.timedelta(days=365)).isoformat()
    if not released(c, media, today):
        return 'not released yet (released-only rule)'
    return ('talk / news / reality (NOT_ACTING)' if set(c.get('genre_ids') or []) & build.NOT_ACTING else
            'self / cameo / archive / uncredited character' if build.SELF.search(c.get('character') or '') else
            f"minor TV role: {c.get('episode_count') or 0} episodes, floor {build.MIN_EPISODES}"
            if media == 'series' and (c.get('episode_count') or 0) < build.MIN_EPISODES else
            f"bit part: billed #{(c.get('order') or 0) + 1}, floor #{build.MAX_BILLING + 1}"
            if media == 'movie' and (c.get('order') or 0) > build.MAX_BILLING else
            'obscure: under 10 votes and older than a year' if (c.get('vote_count') or 0) < 10 and date < year_ago else None)


def actor_ctx(combined_credits, today):
    """{(media, id): [credit]} of the cast, plus whether the person is mainly a voice actor (build.actor's rule)."""
    by, voices, live = {}, 0, set()
    for c in combined_credits.get('cast', []):
        media = {'movie': 'movie', 'tv': 'series'}.get(c.get('media_type'))
        if not media:
            continue
        by.setdefault((media, c['id']), []).append((media, c))
        if actor_drop_reason(c, media, today):
            continue
        if build.VOICE.search(c.get('character') or ''):
            voices += 1
        else:
            live.add((media, c['id']))
    return {'by': by, 'voice_actor': voices * 2 > len(live) + voices}


def actor_status(media, tid, ctx, today):
    """None = the title passes every actor rule (it should be on the card), else (label, reason)."""
    cs = ctx['by'].get((media, tid))
    if not cs:
        return 'source limit', 'TMDB has no acting credit for this person on it (Wikidata and TMDB disagree)'
    reasons = []
    for _, c in cs:
        why = actor_drop_reason(c, media, today)
        if why:
            reasons.append(why)
        elif build.VOICE.search(c.get('character') or ''):
            lead = c.get('order') is not None and c['order'] <= build.LEAD_ORDER and (c.get('vote_count') or 0) >= build.LEAD_VOTES
            if ctx['voice_actor'] or lead:
                return None
            reasons.append('voice role, not a lead and not mainly a voice actor')
        else:
            return None
    return 'deliberate rule', '; '.join(sorted(set(reasons)))


def credit_name(cs):
    c = cs[0][1] if isinstance(cs[0], tuple) else cs[0]
    return c.get('title') or c.get('name')


# ---------- the measurement ----------

def measure_people(kind, pool, spec, order_of, tmdb, sparql, today, n_cards, n_sample, seed):
    """Walk the seeded pool until n_cards cards that Wikidata knows are measured. order_of(slug) -> ordered keys of the card;
    tmdb(path, **params) -> json; sparql(query) -> rows. Returns (summary row, gap rows)."""
    gaps, rf, rt, samp, ok, unjudged, unmapped = [], 0, 0, 0, 0, 0, 0
    measured, skipped, per_card = [], [], []
    medium = {m: [0, 0] for m in ('movie', 'series')}
    wd_short = [0, 0]                                     # Wikidata short films: [on the card, total]
    runtimes = {}

    def runtime_of(tid):
        if tid not in runtimes:
            runtimes[tid] = tmdb(f'/movie/{tid}').get('runtime') or 0
        return runtimes[tid]
    for card in pool:
        if len(measured) >= n_cards:
            break
        slug, pid = card['slug'], card['person_id']
        found_person = sparql(person_query(pid))
        if not found_person:
            skipped.append(slug)
            continue
        person = found_person[0]['p'].rsplit('/', 1)[-1]
        works, unm, _ = parse_works(sparql(works_query(kind, pid)), today)
        order = order_of(slug)
        have = set(order)
        if kind == 'director':
            ctx = director_ctx(tmdb(f'/person/{pid}/movie_credits'), tmdb(f'/person/{pid}/tv_credits'))
            status = lambda media, tid: director_status(media, tid, ctx, today, runtime_of)
            title_of = lambda media, tid: credit_name(ctx[media][tid]) if ctx[media].get(tid) else None
        else:
            ctx = actor_ctx(tmdb(f'/person/{pid}/combined_credits'), today)
            status = lambda media, tid: actor_status(media, tid, ctx, today)
            title_of = lambda media, tid: credit_name(ctx['by'][(media, tid)]) if ctx['by'].get((media, tid)) else None
        measured.append(card)
        truth = sorted(works)
        found, total, missing = af.score(truth, have)
        rf += found
        rt += total
        for m in medium:
            ks = [k for k in truth if k.startswith(m + ':')]
            medium[m][0] += sum(1 for k in ks if k in have)
            medium[m][1] += len(ks)
        for k, w in works.items():
            if w['short']:
                wd_short[1] += 1
                wd_short[0] += k in have
        for k in missing:
            media, tid = k.split(':')
            s = status(media, int(tid)) or ('code bug', 'passes every rule of the card but is not on it (or its TMDB credits changed since the daily build)')
            gaps.append({'kind': kind, 'card': slug, 'side': 'recall', 'key': k, 'name': title_of(media, int(tid)) or works[k]['wd'],
                         'label': s[0], 'reason': s[1]})
        for wd in unm:
            unmapped += 1
            gaps.append({'kind': kind, 'card': slug, 'side': 'unmapped', 'key': wd, 'name': wd, 'label': 'source limit',
                         'reason': 'the Wikidata item has no TMDB id, so it cannot be matched to a card title'})
        sample = random.Random(f'{seed}-{slug}').sample(list(order), min(n_sample, len(order)))
        judge = {}                                         # only titles the person's works list does not already confirm
        for m in ('movie', 'series'):
            ids = [k.split(':')[1] for k in sample if k.startswith(m + ':') and k not in works]
            if ids:
                judge.update(parse_sample(sparql(sample_query(kind, m, ids)), m, person))
        c_ok = c_judged = 0
        for k in sample:
            media, tid = k.split(':')
            name = title_of(media, int(tid)) or k
            s = status(media, int(tid))
            if s:                                          # on the card, yet the person's own credits fail the card's rules
                lab = 'code bug' if s[0] == 'deliberate rule' else 'source limit'
                gaps.append({'kind': kind, 'card': slug, 'side': 'precision', 'key': k, 'name': name, 'label': lab,
                             'reason': 'on the card but ' + s[1]})
                samp += 1
                c_judged += 1
                continue
            n, hit = (1, True) if k in works else judge.get(k, (0, False))
            if not n:
                unjudged += 1                              # Wikidata has no director / cast list for it: nothing to compare with
                continue
            samp += 1
            c_judged += 1
            if hit:
                ok += 1
                c_ok += 1
            else:
                lab = 'deliberate rule' if kind == 'director' and media == 'series' else 'source limit'
                why = ('TV series listed because they directed an episode (TMDB tv_credits); Wikidata lists the series director(s) only'
                       if lab == 'deliberate rule' else 'Wikidata lists other people as ' + ('director' if kind == 'director' else 'cast') +
                       ' of it, not this person (the two sources disagree)')
                gaps.append({'kind': kind, 'card': slug, 'side': 'precision', 'key': k, 'name': name, 'label': lab, 'reason': why})
        per_card.append({'card': slug, 'items': len(order), 'truth': total, 'found': found, 'recall': af.pct(found, total),
                         'judged': c_judged, 'ok': c_ok, 'precision': af.pct(c_ok, c_judged), 'unmapped': len(unm)})
    row = af.summarize(kind, measured, rf, rt, samp, ok, gaps)
    row.update({'by_medium': {m: {'found': f, 'total': t, 'recall': af.pct(f, t)} for m, (f, t) in medium.items()},
                'unmapped': unmapped, 'unjudged': unjudged, 'no_wikidata': skipped, 'per_card': per_card,
                'wikidata_short_films': {'on_card': wd_short[0], 'total': wd_short[1]}})
    if kind == 'director':                                 # the shorts question (#30), both ways, rule left unchanged
        shorts = sum(1 for g in gaps if g['side'] == 'recall' and g['reason'].startswith('short film'))
        row['shorts'] = {'missing_as_shorts': shorts, 'recall_shorts_in_claim': row['recall'],
                         'recall_shorts_out_of_claim': af.pct(rf, rt - shorts)}
    return row, gaps
