"""Accuracy audit 5/6 (issue #96, loop #33): Network cards (25) and the rule-based cards (Decades 7, Acclaimed 2, Hidden
Gems 2, Stand-up 3), same frame as accuracy_frame.py: seeded cards, RECALL, PRECISION, one label per gap (code bug /
deliberate rule / cap / source limit), and a summary row of the same shape (the weekly check, the baseline and the
report table treat these kinds like the others).

THE CLAIMS (what the card name promises, in plain words) and THE RULES in code (generator/build.py discover(), line
numbers of the commit this was written on; the card numbers are in generator/spec.json):
  network     "Network . HBO" = the TV series that aired on HBO, best-known (most votes) first.
              Rule: TMDB /discover/tv with_networks=<the card's network id> (matches ANY network the show ever had, not
              only the first one), sort vote_count.desc (SORTS :267), at least 10 votes (spec min_votes), released only
              and normal status (:358-359), no talk / news (:360), no adult-adjacent keywords (:361-365), the spec deny
              list, at most 1000 (spec defaults.depth.votes, cut at :411).
  decade      "Decade . 1990s" = the movies and series released in the 1990s, best-known (most votes) first.
              Rule: discover with the decade's date window (primary_release_date / first_air_date gte+lte in spec.json;
              2020s open-ended), sort vote_count.desc, at least 10 votes (movies) / 50 (series), movies with a digital /
              physical / TV release (:354) and 40 minutes or more (:355-356, every 'votes' card), series status and
              talk / news rules (:358-360), keyword and deny rules, at most 1000 per medium.
  acclaimed   "Acclaimed Movies / TV" = the best-rated titles that enough people have rated.
              Rule: discover sorted vote_average.desc, rating 7.8+, at least 2000 votes (movies) / 800 (series), at least
              180 days old (min_age_days :347-348), movies of 40 minutes or more, same status / talk / keyword rules, at
              most 500 per medium (depth.top).
  hidden      "Hidden Gem Movies / TV" = good titles few people know: rated 7.5+, popular-first among them.
              Rule: discover sorted popularity.desc, rating 7.5+, between 150 and 1500 votes (movies) / 100 and 600
              (series), at least 730 days (movies) / 365 days (series) old, series first aired in 2000 or later and not
              documentary / reality / kids / news / soap / talk / crime (without_genres), same status rules, at most
              1000 per medium.
  standup     "Stand-up . Popular / New / Top Rated" = stand-up comedy specials and shows.
              Rule: discover with_keywords 9716 (TMDB's "stand-up comedy"), movies of 20 minutes or more
              (with_runtime.gte 20), series without talk / news / reality (without_genres), then the sort: popularity
              (10 votes), release date (3 votes), rating (50 votes movies / 20 series, 180 days old).

TRUTH.
  recall (all kinds but network): a rule-free /discover of the claim itself, the first TRUTH_K = 20 per medium, with
    ONLY the claim (no release-type / status / age / runtime / vote-floor / genre-exclusion rules): decade = the decade's
    dates, most votes first (released only); acclaimed = rated 100+ votes, best-rated first (the frame's looser floor);
    hidden = rated 7.5+, under the card's vote ceiling, 100+ votes (movies) / 50+ (series), popular first; standup =
    keyword 9716, in the card's own sort (New: released only; Top Rated: 10+ votes). The provider data is TMDB's own, so
    this checks the card's RULES and CAPS, not TMDB's coverage.
  recall (network): WIKIDATA. Series whose original broadcaster (P449) is the network (see NETWORKS: our mapping of the
    card's TMDB network id to the Wikidata items, each read off Wikidata), with a TMDB series id (P4983), released by
    today, the 20 most notable (most Wikipedia editions). Wikidata knows only the ORIGINAL broadcaster; the card also
    holds shows that moved to the network later. A network with no Wikidata item gets no truth: listed, not scored.
  outside recall (second list, headline untouched): Acclaimed = the IMDb top list by IMDb's own rule (weighted rating,
    25,000 votes movies / 5,000 series, public datasets.imdbws.com, no key), IMDb ids mapped to TMDB ids through Wikidata
    (P345 -> P4947 / P4983; a title Wikidata cannot map is skipped); Stand-up = Wikidata items of class stand-up comedy
    (Q145806) / stand-up special (Q140299056) with a TMDB id, most notable first.
  precision (all kinds): RULE-APPLIED. 20 seeded card titles per card re-read from TMDB (details); a title that breaks
    the card's own rule (any of the rules above, plus its date window, tag, network) is a gap: a code bug when the break
    is real, a source limit when the title is within TMDB's daily drift of a line (rating within 0.05, votes within 3%)
    or TMDB's detail data lacks the tag its discover listed.
  precision (network, standup): also WIKIDATA-JUDGED (truth_precision): a sampled title whose Wikidata item has a
    broadcaster (P449) / genre (P31 or P136) statement is judged (the network / stand-up class is among them = ok); the
    rest are UNJUDGED and left out of the denominator, never counted correct.

LABELS of a missing title (recall): TMDB's own data lacks the card's network / keyword (the two sources disagree) =
source limit; a rule above drops it = deliberate rule (the reason says which); it passes every rule and the card holds
its depth for that medium = cap; it passes every rule and the card is under its depth = source limit (TMDB discover paging
snapshot, issue #25).

Pure functions take plain dicts and are unit-tested offline (tests/test_accuracy_rules.py).
"""
import datetime as dt, gzip, io, random, urllib.request
import build
import accuracy_frame as af

KINDS = ('network', 'decade', 'acclaimed', 'hidden', 'standup')
TRUTH_K = 20                      # claim titles per medium
CANDIDATES = 40                   # Wikidata rows read per query
NEAR_RATING = 0.05                # rating points a title may sit below a line before it is a real break (daily TMDB drift)
NEAR_VOTES = 0.03                 # share of a vote ceiling a title may sit above it
HIDDEN_FLOOR = {'movie': 100, 'series': 50}      # the claim's looser vote floors (the cards use 150 / 100)
TOP_FLOOR = {'acclaimed': 100, 'standup': 10}
IMDB_RATINGS = 'https://datasets.imdbws.com/title.ratings.tsv.gz'
IMDB_MIN_VOTES = {'movie': 25000, 'series': 5000}
IMDB_POOL = {'movie': 150, 'series': 500}        # IMDb ids read per medium before mapping (episodes / films fall out)
STANDUP_CLASSES = ['Q145806', 'Q140299056']      # stand-up comedy / stand-up special (film and TV genre)

# TMDB network id (the card's `network_id`) -> the Wikidata items that are that network as an original broadcaster (P449).
# Each Q id was read off Wikidata (label + description) when this was written; the one with the most P449 series was taken
# where a name is shared (Nickelodeon, Cartoon Network, History: the US channels). None = Wikidata has no usable item.
NETWORKS = {
    49: ['Q23633', 'Q47494573', 'Q106651154', 'Q94641886', 'Q3502585', 'Q6321919'],   # HBO + HBO Espana / Latino / Portugal / 2 / 3
    174: ['Q294660'],          # AMC
    88: ['Q651228'],           # FX
    4: ['Q191472', 'Q9531'],   # BBC One + the BBC itself (Wikidata's usual broadcaster for BBC shows)
    2552: ['Q62446736'],       # Apple TV (Apple TV+)
    213: ['Q907311'],          # Netflix
    67: ['Q23589'],            # Showtime
    80: ['Q379052'],           # Adult Swim
    47: ['Q131439'],           # Comedy Central
    56: ['Q708290'],           # Cartoon Network (US)
    13: ['Q154958'],           # Nickelodeon (US)
    54: ['Q178837'],           # Disney Channel
    65: ['Q1621107'],          # History (US)
    64: ['Q43404'],            # Discovery Channel
    43: ['Q157118'],           # National Geographic
    129: ['Q277561'],          # A&E
    453: ['Q1630304', 'Q108409114'],   # Hulu + FX on Hulu
    1024: ['Q4740856'],        # Amazon Prime Video
    3353: None,                # Peacock: no Wikidata item used as an original broadcaster was found
    3186: ['Q65359104'],       # HBO Max
    4330: ['Q27903045'],       # Paramount+
    14: ['Q215616'],           # PBS
    33: ['Q43359'],            # MTV
    318: ['Q2422698'],         # Starz
    71: ['Q212252'],           # The CW
}

# Judge-only: a Wikidata broadcaster that also counts as "ok" for the card, but is not used to build its truth list
# (Max originals are filed under HBO; using HBO's whole catalogue as Max's truth would be wrong).
JUDGE_ALSO = {3186: ['Q23633', 'Q47494573', 'Q106651154', 'Q94641886', 'Q3502585', 'Q6321919'], 14: ['Q3245159']}       # PBS Kids is its own TMDB network, so it is not PBS's truth


# A parent organisation, not a channel: a title whose only statement for the card is this one cannot be placed on the card
# (Wikidata's "BBC" may be BBC One or Two), so it is left out of the score as unjudged instead of counted either way.
PARENT_ONLY = {'Q9531'}
# The same network under a second TMDB id the card's with_networks lacks: TMDB lists a title under it, Wikidata agrees, and
# the card misses it (arrivals.py: "TMDB has two, 3186 and 8304"). card network_id -> the other ids.
SECOND_IDS = {3186: [8304, 6783], 14: [122], 67: [6631]}


def pick_cards(spec, kind, n, seed):
    """The cards of the kind: all of them when there are n or fewer, else the first n of a seeded shuffle (stable)."""
    def of_kind(c):
        f, s = c.get('folders', []), c['slug']
        return {'network': 'networks' in f, 'decade': 'decades' in f, 'acclaimed': s.startswith('acclaimed-acclaimed-'),
                'hidden': s.startswith('acclaimed-hidden-gem'), 'standup': 'standup' in f}[kind]
    cards = sorted((c for c in spec['catalogs'] if c.get('kind') == 'discover' and of_kind(c)), key=lambda c: c['slug'])
    if len(cards) > n:
        random.Random(f'{seed}-{kind}').shuffle(cards)
        cards = sorted(cards[:n], key=lambda c: c['slug'])
    return cards


def network_qs(card):
    return NETWORKS.get(card.get('network_id')) or []


def card_networks(card):
    return {int(x) for x in str(card.get('series', {}).get('with_networks', '')).replace(',', '|').split('|') if x.strip().isdigit()}


# ---------- the card rules, read off the title's TMDB details ----------

def facts(det, media):
    f = af.facts(det, media)
    f['networks'] = {n['id'] for n in det.get('networks') or []}
    return f


def rules_of(kind, card, media, spec):
    r = af.card_rules(kind, card, media, spec)
    own = card.get(media, {})
    r['min_runtime'] = int(own.get('with_runtime.gte', r['min_runtime']))     # standup movies: 20 minutes, not 40
    r['own'] = own
    return r


def _ids(s):
    return {int(x) for x in str(s).replace(',', '|').split('|') if x.strip().isdigit()}


def breaks(f, media, card, r, today):
    """-> [(kind, text)] with kind 'rule' (a real break), 'near' (within TMDB's daily drift of a numeric line) or 'tag'
    (TMDB's detail data lacks the network / keyword the card's discover filters on)."""
    # a runtime TMDB now shows as 0 (blank) was filled when the card was built: TMDB changed the data, not the card's rule
    out = [('near' if t.startswith('runtime 0 ') else 'rule', t) for t in af.rule_breaks(f, media, r, today)]
    own = r['own']
    date_gte = own.get('primary_release_date.gte' if media == 'movie' else 'first_air_date.gte')
    date_lte = own.get('primary_release_date.lte' if media == 'movie' else 'first_air_date.lte')
    if f['date'] and date_gte and f['date'] < date_gte:
        out.append(('rule', f"dated {f['date']}, before the card's window start {date_gte}"))
    if f['date'] and date_lte and f['date'] > date_lte:
        out.append(('rule', f"dated {f['date']}, after the card's window end {date_lte}"))
    if 'vote_average.gte' in own and f['rating'] < own['vote_average.gte']:
        near = f['rating'] >= own['vote_average.gte'] - NEAR_RATING
        out.append(('near' if near else 'rule', f"rating {f['rating']:.2f}, floor {own['vote_average.gte']}"))
    if 'vote_count.lte' in own and f['votes'] > own['vote_count.lte']:
        near = f['votes'] <= own['vote_count.lte'] * (1 + NEAR_VOTES)
        out.append(('near' if near else 'rule', f"{f['votes']} votes, ceiling {own['vote_count.lte']}"))
    hit = f['genres'] & _ids(own.get('without_genres', '')) - af.TV_GENRES_OUT      # talk / news: af.rule_breaks already says so
    if hit:
        out.append(('rule', f"genre {sorted(hit)} excluded by the card"))
    wk = own.get('with_keywords')
    if wk:
        want = _ids(wk)
        if not (f['keywords'] & want if '|' in str(wk) or len(want) == 1 else want <= f['keywords']):
            out.append(('tag', f'TMDB details lack keyword {sorted(want)}'))
    nets = card_networks(card) if media == 'series' and card.get('network_id') else set()
    if nets and not f['networks'] & nets:
        out.append(('tag', f'TMDB details list networks {sorted(f["networks"])}, not {sorted(nets)}'))
    return out


def classify_missing(f, media, card, r, today, mcount):
    """Label ONE claim title the card lacks. -> (label, reason)."""
    bs = breaks(f, media, card, r, today)
    tag = [t for k, t in bs if k == 'tag']
    second = sorted(f.get('networks', set()) & set(SECOND_IDS.get(card.get('network_id'), []))) if media == 'series' else []
    if second and not f['networks'] & card_networks(card):
        return 'code bug', f"card misses its network's second TMDB id {second[0]} (TMDB lists the title under it; the card's with_networks lacks it)"
    if tag:
        return 'source limit', '; '.join(tag) + ' (the two sources disagree)'
    if bs:
        return 'deliberate rule', '; '.join(t for _, t in bs)
    if mcount >= r['depth']:
        return 'cap', f"{media} part of the card is at its cap of {r['depth']}"
    return 'source limit', 'passes every rule and the card is under its cap: TMDB discover paging snapshot (issue #25)'


def classify_extra(f, media, card, r, today):
    """Label ONE sampled card title that breaks the card's own rules, or None. -> (label, reason) | None."""
    bs = breaks(f, media, card, r, today)
    if not bs:
        return None
    if any(k == 'rule' for k, _ in bs):
        return 'code bug', 'on the card but breaks its own rule: ' + '; '.join(t for k, t in bs if k == 'rule')
    if any(k == 'near' for k, _ in bs):
        return 'source limit', "within TMDB's daily drift of the card's line: " + '; '.join(t for _, t in bs)
    return 'source limit', "TMDB's discover listed it, its own details do not agree: " + '; '.join(t for _, t in bs)


# ---------- TMDB claim (rule-free discover) ----------

def claim_params(kind, card, media, today):
    """The claim-only /discover for this card and medium; sort and the claim's own window only."""
    movie = media == 'movie'
    own = card.get(media, {})
    p = {'include_adult': 'false'}
    if kind == 'decade':
        p['sort_by'] = 'vote_count.desc'
        p.update({k: v for k, v in own.items() if 'date' in k})
        lte = 'primary_release_date.lte' if movie else 'first_air_date.lte'
        p[lte] = min(p.get(lte, today), today)
    elif kind == 'acclaimed':
        p.update({'sort_by': 'vote_average.desc', 'vote_count.gte': TOP_FLOOR['acclaimed']})
    elif kind == 'hidden':
        p.update({'sort_by': 'popularity.desc', 'vote_average.gte': own['vote_average.gte'],
                  'vote_count.gte': HIDDEN_FLOOR[media], 'vote_count.lte': own['vote_count.lte']})
    elif kind == 'standup':
        p['with_keywords'] = own['with_keywords']
        s = card['sort']
        if s == 'popular':
            p['sort_by'] = 'popularity.desc'
        elif s == 'new':
            p.update({'sort_by': 'primary_release_date.desc' if movie else 'first_air_date.desc',
                      'release_date.lte' if movie else 'first_air_date.lte': today})
        else:
            p.update({'sort_by': 'vote_average.desc', 'vote_count.gte': TOP_FLOOR['standup']})
    return p


# ---------- Wikidata ----------

def _values(qs):
    return ' '.join(f'wd:{q}' for q in qs)


def broadcaster_truth_query(qs, limit=CANDIDATES):
    """Series whose ORIGINAL broadcaster (P449) is one of the network items, with a TMDB series id, most notable first."""
    par = ', '.join('wd:' + q for q in sorted(PARENT_ONLY & set(qs)))
    spec = f' (MAX(IF(?B IN ({par}), 0, 1)) AS ?spec)' if par else ''       # only cards that list a parent organisation pay for it
    return (f'SELECT ?id (MAX(?sl0) AS ?sl) (MIN(?d0) AS ?d){spec} '
            f'WHERE {{ VALUES ?B {{ {_values(qs)} }} ?w wdt:P449 ?B. '
            f'?w wdt:P4983 ?id. ?w wikibase:sitelinks ?sl0. OPTIONAL {{ ?w wdt:P580 ?d1 }} OPTIONAL {{ ?w wdt:P577 ?d2 }} '
            f'BIND(COALESCE(?d1, ?d2) AS ?d0) }} GROUP BY ?id ORDER BY DESC(?sl) LIMIT {int(limit)}')


def class_closure_query(qs):
    """The classes and every subclass of them. Kept apart from the truth / judge queries: a property path (P279*) next to a
    class union in one WDQS query silently drops rows."""
    return f'SELECT ?c WHERE {{ VALUES ?G {{ {_values(qs)} }} ?c wdt:P279* ?G }}'


def class_closure(sparql, qs):
    got = {r['c'].rsplit('/', 1)[-1] for r in sparql(class_closure_query(qs)) if r.get('c')}
    return sorted(got | set(qs))


def standup_truth_query(media, classes, prop, limit=CANDIDATES):
    """Titles typed (P31) or genred (P136) as one of the classes, with a TMDB id. One film / TV query per property, no path."""
    p = 'P4947' if media == 'movie' else 'P4983'
    return (f'SELECT ?id (MAX(?sl0) AS ?sl) (MIN(?d0) AS ?d) WHERE {{ VALUES ?G {{ {_values(classes)} }} '
            f'?w wdt:{prop} ?G. ?w wdt:{p} ?id. ?w wikibase:sitelinks ?sl0. OPTIONAL {{ ?w wdt:P577 ?d1 }} '
            f'OPTIONAL {{ ?w wdt:P580 ?d2 }} BIND(COALESCE(?d1, ?d2) AS ?d0) }} GROUP BY ?id ORDER BY DESC(?sl) LIMIT {int(limit)}')


def standup_truth(sparql, media, classes, today, k):
    rows = {}
    for prop in ('P31', 'P136'):
        for r in sparql(standup_truth_query(media, classes, prop)):
            if r['id'] not in rows or int(r.get('sl') or 0) > int(rows[r['id']].get('sl') or 0):
                rows[r['id']] = r
    ordered = sorted(rows.values(), key=lambda r: -int(r.get('sl') or 0))
    return parse_truth(ordered, media, today)[:k]


def split_wide(rows):
    """-> (rows the card can be judged on, rows whose only statement is a parent organisation). Rows with no 'spec' column
    (other kinds, tests) are all kept."""
    wide = [r for r in rows if r.get('spec') == '0']
    return [r for r in rows if r.get('spec') != '0'], wide


def parse_truth(rows, media, today):
    """-> [key] in notability order, unreleased dropped."""
    return [f"{media}:{r['id']}" for r in rows if not ((r.get('d') or '')[:10] > today)]


def judge_query(kind, media, ids, qs):
    """One row per TMDB id: how many statements its item has (broadcasters P449 / genres+types P31,P136) and whether one
    of them is (a subclass of) the classes."""
    p = 'P4947' if media == 'movie' else 'P4983'
    vals = ' '.join(f'"{i}"' for i in ids)
    if kind == 'network':
        # the plain list of the title's original broadcasters; which one counts is decided in parse_judge (an EXISTS /
        # aggregate version timed out on the GitHub runner for BBC One)
        return f'SELECT ?id ?x WHERE {{ VALUES ?id {{ {vals} }} ?w wdt:{p} ?id. OPTIONAL {{ ?w wdt:P449 ?x }} }}'
    # stand-up: the title's genres (P136) and types (P31), kept apart; "is it stand-up" is decided in Python against the closure
    return (f'SELECT ?id ?g ?t WHERE {{ VALUES ?id {{ {vals} }} ?w wdt:{p} ?id. OPTIONAL {{ ?w wdt:P136 ?g }} '
            f'OPTIONAL {{ ?w wdt:P31 ?t }} }}')


def parse_judge_classes(rows, media, closure):
    """Rows (id, g genre, t type) -> {key: (statements, is_it)}. A title is judged only when Wikidata gives it a genre (P136),
    or a type that is stand-up; no genre statement at all = (0, False) = unjudged, never wrong."""
    gs, ts, out = {}, {}, {}
    for r in rows:
        k = f"{media}:{r['id']}"
        g, t = gs.setdefault(k, set()), ts.setdefault(k, set())
        if r.get('g'):
            g.add(r['g'].rsplit('/', 1)[-1])
        if r.get('t'):
            t.add(r['t'].rsplit('/', 1)[-1])
    for k in gs:
        hit = bool((gs[k] | ts[k]) & set(closure))
        out[k] = (len(gs[k]) or (1 if hit else 0), hit)
    return out


def parse_judge(rows, media, qs):
    """Rows (id, x broadcaster) -> {key: (statements, is_it)}; a TMDB id with no Wikidata item is absent. is_it = one of the
    title's broadcasters is the card's network (qs without the parent organisation). A title whose only match is the parent
    organisation, or that has no broadcaster statement, is (0, False) = unjudged."""
    own, seen, out = set(qs) - PARENT_ONLY, {}, {}
    for r in rows:
        s = seen.setdefault(f"{media}:{r['id']}", set())
        if r.get('x'):
            s.add(r['x'].rsplit('/', 1)[-1])
    for k, s in seen.items():
        if s & own:
            out[k] = (len(s), True)
        elif s & PARENT_ONLY:
            out[k] = (0, False)
        else:
            out[k] = (len(s), False)
    return out


# ---------- the IMDb top list (outside truth for Acclaimed) ----------

def imdb_top(lines, media, k=None):
    """IMDb ids of the top k by IMDb's weighted rating (WR = v/(v+m)*R + m/(v+m)*C, m = the minimum votes, C = the mean
    rating of the titles that clear it). lines = rows of title.ratings.tsv (header included): tconst, averageRating,
    numVotes. Includes every title type (episodes, TV movies); the mapping to a TMDB movie / series id drops most."""
    m = IMDB_MIN_VOTES[media]
    rows = []
    for ln in lines:
        p = ln.rstrip('\n').split('\t')
        if len(p) < 3 or not p[2].isdigit() or int(p[2]) < m:
            continue
        rows.append((p[0], float(p[1]), int(p[2])))
    if not rows:
        return []
    c = sum(r for _, r, _ in rows) / len(rows)
    rows.sort(key=lambda x: -(x[2] / (x[2] + m) * x[1] + m / (x[2] + m) * c))
    return [t for t, _, _ in rows[:k or IMDB_POOL[media]]]


def imdb_map_query(ids):
    vals = ' '.join(f'"{i}"' for i in ids)
    return (f'SELECT ?i ?m ?t WHERE {{ VALUES ?i {{ {vals} }} ?w wdt:P345 ?i. OPTIONAL {{ ?w wdt:P4947 ?m }} '
            f'OPTIONAL {{ ?w wdt:P4983 ?t }} }}')


def imdb_ratings_lines(url=IMDB_RATINGS):
    """The IMDb ratings dataset as text lines (public, no key). RuntimeError on any failure."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'strand-discovery/1.0 (accuracy check)'})
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
        return gzip.decompress(raw).decode('utf-8').splitlines()
    except Exception as e:
        raise RuntimeError(f'IMDb dataset {type(e).__name__}') from None


def imdb_truth(lines, sparql, k=TRUTH_K):
    """-> {'movie': [keys], 'series': [keys]}: the IMDb top titles that Wikidata maps to a TMDB movie / series id."""
    out = {}
    for media, field in (('movie', 'm'), ('series', 't')):
        ids = imdb_top(lines, media)
        keys = {}
        for i in range(0, len(ids), 100):
            for r in sparql(imdb_map_query(ids[i:i + 100])):
                if r.get(field):
                    keys.setdefault(r['i'], f"{media}:{r[field]}")
        out[media] = [keys[i] for i in ids if i in keys][:k]
    return out


# ---------- the measurement ----------

def measure_rules(kind, cards, spec, order_of, tmdb, sparql, today, n_sample, seed, imdb=None, truth_k=TRUTH_K):
    """Walk the cards. order_of(slug) -> ordered keys of the whole card; tmdb(path, **params) -> json; sparql(query) -> rows;
    imdb = {'movie': [keys], 'series': [keys]} (Acclaimed's outside list) or None. -> (summary row, gap rows)."""
    gaps, rf, rt, samp, ok = [], 0, 0, 0, 0
    of = ot = 0                                      # outside recall (IMDb / Wikidata stand-up)
    tj = tok = tun = 0                               # truth precision (Wikidata-judged)
    runj = 0                                         # recall truth titles left out as unjudged (parent-organisation-only)
    no_truth, at_cap, per_card, cache, clo, wd_failed = [], [], [], {}, [], []

    def ask(slug, q):
        """One Wikidata query for one card. WDQS can time out on a single heavy network (PBS); that card is listed as
        unmeasured this week and the others still count."""
        try:
            return sparql(q)
        except RuntimeError as e:
            if 'Wikidata' not in str(e):
                raise
            if slug not in wd_failed:
                wd_failed.append(slug)
            return None

    def closure():
        if not clo:
            clo.extend(class_closure(sparql, STANDUP_CLASSES))
        return clo

    def detail(media, tid):
        """The title's facts, or None when TMDB does not know the id (a Wikidata id TMDB merged / dropped)."""
        if (media, tid) not in cache:
            try:
                cache[(media, tid)] = facts(tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}",
                                                 append_to_response=af.DETAIL_APPEND), media)
            except RuntimeError as e:
                if 'HTTPError' not in str(e):
                    raise
                cache[(media, tid)] = None
        return cache[(media, tid)]

    def label_missing(slug, side, media, k, r, card, mcount):
        f = detail(media, int(k.split(':')[1]))
        if f:
            lab, why = classify_missing(f, media, card, r, today, mcount)
        else:
            lab, why = 'source limit', 'TMDB does not know this id'
        gaps.append({'kind': kind, 'card': slug, 'side': side, 'key': k, 'name': f['name'] if f else k, 'label': lab, 'reason': why})

    for card in cards:
        slug, order = card['slug'], order_of(card['slug'])
        have = set(order)
        c = {'card': slug, 'items': len(order), 'truth': 0, 'found': 0, 'judged': 0, 'ok': 0}
        for media in card['media']:
            r = rules_of(kind, card, media, spec)
            mcount = sum(1 for k in order if k.startswith(media + ':'))
            if mcount >= r['depth']:
                at_cap.append(f'{slug}:{media}')
            if kind == 'network':
                qs = network_qs(card)
                if not qs:
                    no_truth.append(slug)
                    continue
                rows = ask(slug, broadcaster_truth_query(qs))
                if rows is None:
                    continue
                keep, wide = split_wide(rows)
                wide_keys = {f"{media}:{r['id']}" for r in wide}
                runj += sum(1 for k in parse_truth(rows, media, today)[:truth_k] if k in wide_keys)    # truth wider than the card
                truth = parse_truth(keep, media, today)[:truth_k]
            else:
                res = tmdb(f"/discover/{'movie' if media == 'movie' else 'tv'}", page=1, **claim_params(kind, card, media, today))
                truth = [f"{media}:{x['id']}" for x in res.get('results', [])][:truth_k]
            found, total, missing = af.score(truth, have)
            rf += found
            rt += total
            c['found'] += found
            c['truth'] += total
            for k in missing:
                label_missing(slug, 'recall', media, k, r, card, mcount)
            outside = None                           # a second, independent list: IMDb (Acclaimed) / Wikidata (Stand-up)
            if kind == 'acclaimed' and imdb is not None:
                outside = imdb.get(media, [])[:truth_k]
            elif kind == 'standup':
                outside = standup_truth(sparql, media, closure(), today, truth_k)
            if outside:
                f2, t2, m2 = af.score(outside, have)
                of += f2
                ot += t2
                for k in m2:
                    label_missing(slug, 'outside_recall', media, k, r, card, mcount)
        # precision: the card's own rules, on a seeded sample of what the card holds
        sample = random.Random(f'{seed}-{slug}').sample(list(order), min(n_sample, len(order)))
        judge = {}
        if kind in ('network', 'standup'):
            for media in card['media']:
                ids = [k.split(':')[1] for k in sample if k.startswith(media + ':')]
                if kind == 'network':
                    qs = network_qs(card) + JUDGE_ALSO.get(card['network_id'], [])
                    if ids and qs:
                        jr = ask(slug, judge_query(kind, media, ids, qs))
                        judge.update(parse_judge(jr, media, qs) if jr is not None else {})
                elif ids:
                    judge.update(parse_judge_classes(sparql(judge_query(kind, media, ids, STANDUP_CLASSES)), media, closure()))
        for k in sample:
            media, tid = k.split(':')
            f = detail(media, int(tid))
            r = rules_of(kind, card, media, spec)
            samp += 1
            c['judged'] += 1
            bad = classify_extra(f, media, card, r, today) if f else ('source limit', 'TMDB does not know this id')
            if bad:
                gaps.append({'kind': kind, 'card': slug, 'side': 'precision', 'key': k, 'name': f['name'] if f else k,
                             'label': bad[0], 'reason': bad[1]})
            else:
                ok += 1
                c['ok'] += 1
            if kind in ('network', 'standup'):
                n, hit = judge.get(k, (0, False))
                if not n:
                    tun += 1
                    continue
                tj += 1
                if hit:
                    tok += 1
                    continue
                if kind == 'network':
                    lab = 'deliberate rule' if f and card_networks(card) & f['networks'] else 'source limit'
                    why = ("Wikidata's original broadcaster is another network; the card counts any network the show aired on "
                           "(TMDB lists this network among its networks)" if lab == 'deliberate rule' else
                           'Wikidata lists another original broadcaster and TMDB does not list this network (the two disagree)')
                else:
                    lab, why = 'source limit', 'Wikidata has no stand-up type or genre for it, only generic ones such as film / television special (the two sources disagree)'
                gaps.append({'kind': kind, 'card': slug, 'side': 'truth_precision', 'key': k, 'name': f['name'] if f else k,
                             'label': lab, 'reason': why})
        per_card.append({**c, 'recall': af.pct(c['found'], c['truth']), 'precision': af.pct(c['ok'], c['judged'])})
    row = af.summarize(kind, cards, rf, rt, samp, ok, gaps)

    def by(side):
        return {lab: sum(1 for g in gaps if g['side'] == side and g['label'] == lab) for lab in af.LABELS}
    if ot or kind == 'acclaimed':
        row['outside'] = {'source': 'IMDb top list' if kind == 'acclaimed' else 'Wikidata stand-up items',
                          'found': of, 'truth': ot, 'recall': af.pct(of, ot), 'gaps': by('outside_recall')}
    if kind in ('network', 'standup'):
        row['truth_precision'] = {'ok': tok, 'judged': tj, 'unjudged': tun, 'precision': af.pct(tok, tj), 'gaps': by('truth_precision')}
    row.update({'recall_unjudged': runj, 'wd_failed': wd_failed, 'no_truth': no_truth, 'at_cap': at_cap, 'per_card': per_card})
    return row, gaps


# ---------- the weekly check's glue (kept here so accuracy_check.py only grows by a small block) ----------

def run_all(rules_cards, spec, order_of, tmdb, sparql, today, n_sample, seed, imdb_lines=imdb_ratings_lines):
    """Measure every kind in rules_cards {kind: [cards]}. -> (summary rows, gap rows, notes). A Wikidata / IMDb / TMDB
    outage is a note, never a problem."""
    rows, gaps, notes, imdb = [], [], [], None
    if rules_cards.get('acclaimed'):
        try:
            imdb = imdb_truth(imdb_lines(), sparql)
        except RuntimeError as e:
            notes.append(f'Acclaimed: the IMDb list was not read this week ({e}); only the TMDB claim was measured')
    for kind, cs in rules_cards.items():
        try:
            row, g = measure_rules(kind, cs, spec, order_of, tmdb, sparql, today, n_sample, seed, imdb)
        except RuntimeError as e:
            notes.append(f'{kind} cards not measured this week: {e}')
            continue
        rows.append(row)
        gaps += g
    return rows, gaps, notes


def extra_lines(rows):
    """Report table of the numbers the shared table has no column for (second list, Wikidata-judged precision)."""
    out = []
    for r in rows:
        o, t = r.get('outside'), r.get('truth_precision')
        if r.get('wd_failed'):
            out.append(f"| {r['kind']} | cards Wikidata did not answer for this week (not scored) | - | {', '.join(r['wd_failed'])} |")
        if r.get('recall_unjudged'):
            out.append(f"| {r['kind']} | recall truth left out as unjudged (parent organisation only) | - | {r['recall_unjudged']} titles |")
        if o:
            out.append(f"| {r['kind']} | outside recall ({o['source']}) | {o['recall']} | {o['found']}/{o['truth']} |")
        if t:
            out.append(f"| {r['kind']} | Wikidata-judged precision | {t['precision']} | {t['ok']}/{t['judged']} judged, {t['unjudged']} unjudged |")
    return ['', '| kind | second measure | % | titles |', '|---|---|---|---|'] + out if out else []


def extra_problems(rows, fbase, max_drop):
    """A drop of more than max_drop points of the outside recall / Wikidata-judged precision below the baseline."""
    out = []
    for r in rows:
        b = fbase.get(r['kind'], {})
        for what, cur in (('outside_recall', (r.get('outside') or {}).get('recall')),
                          ('truth_precision', (r.get('truth_precision') or {}).get('precision'))):
            if cur is not None and b.get(what) is not None and b[what] - cur > max_drop:
                out.append(f"{r['kind']} {what}: {cur}% is {round(b[what] - cur, 1)} points below the baseline {b[what]}%")
    return out
