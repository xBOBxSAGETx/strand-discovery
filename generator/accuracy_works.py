"""Accuracy frame, Wikidata-truth kinds (issue #94, audit job 3 of 6 of #33): Studios and Movie series.

Same frame as accuracy_frame.py (seeded cards -> RECALL + PRECISION -> every gap labelled code bug / deliberate rule /
cap / source limit; rows are built with accuracy_frame.summarize so accuracy_check.py prints them in the same table), but
the TRUTH is Wikidata, not a second TMDB discover. Wikidata is a different source from the one the cards are built from.

THE CLAIMS and THE RULES in code (generator/build.py, line numbers of the commit this was written on):
  studio   "Pixar" = the films the studio made (its production-company credit). Rule: TMDB /discover/movie with
           with_companies = the studio's TMDB company id(s) (spec.json, card "movie.with_companies"), sort popularity.desc
           (SORTS :265), at least 10 votes (:342), released only (:347-354), release type digital / physical / TV (4|5|6,
           :354), adult-adjacent keyword exclusions (:361-365), spec deny list, at most 1000 (depth.popular, :411)
           (build_one :924-926). Five studios whose identity is DISTRIBUTION (A24, NEON, Lionsgate, Focus Features,
           Searchlight) also hold every film on studio_distributed.json (add_studio_distributed :248-262), a list made by
           studio_distributed.py from Wikidata P750 (distributor) / P272 (production company), minus a hand EXCLUDE list.
           Those five cards are built from the same Wikidata properties the truth uses, so their score is CIRCULAR
           (kind "studio_listed", reported apart); the 13 production-studio cards are the independent measure.
  series   "Series . Alien" = every film in the series (plus the TV shows named in the card's hand list). Rule: franchise()
           (:439-457): every released part of the card's TMDB collection(s) (collection_ids) in release order, plus the
           hand-listed titles of the card's "ids" (spin-offs TMDB does not group; PR #70). Three universe cards (MCU, DC
           Universe, MonsterVerse) are keyword discover cards instead: TMDB with_keywords, movies + series, release_asc,
           no vote floor (:339-373).

TRUTH. studio: Wikidata films (TMDB movie id P4947, English label, release date P577, not a short film) whose production
company (P272) is one of the studio's Wikidata items (STUDIOS below, matched to the TMDB company id by the item's
"TMDB company ID" P11806 where Wikidata has it, else by hand); the five listed cards use P750 or P272, as the list does.
Recall = the K=100 most-linked of them (sitelinks = popularity proxy, as studio_distributed.py ranks) found on the card.
Precision = a seeded 20 of the card's titles, each must be a Wikidata film of the studio (any rank).
series: Wikidata films (P4947, short films left out as for studios) and TV series (P4983) that are "part of the series" (P179, followed up through sub-series
such as MCU Phase One) of the card's Wikidata series item(s) (SERIES below) or have it as "media franchise" (P8345).
Recall = those found on the card; precision = a seeded 20 of the card's titles, each must be in that set.

LABELS here (same four): code bug = the card breaks its own rule, or a rule/hand list that must hold does not;
deliberate rule = a written rule keeps the title out (vote floor, release types, released-only, hand exclude list);
cap = the card is at its depth; source limit = TMDB and Wikidata disagree (TMDB has no company credit / no collection
grouping / no keyword for it), Wikidata cannot confirm a title (no statement for it: reason starts "unconfirmed"), or no rule
explains the miss (TMDB discover paging, issue #25). A precision gap with an "unconfirmed" or "disagrees" reason is NOT
proven wrong: the two sources differ, nothing says which is right.

The one Wikidata helper here is sparql() (own small function; no key needed). Pure functions (parse_*, classify_*,
pick_*) take plain dicts and are unit-tested offline (tests/test_accuracy_works.py).
"""
import json, random, time, urllib.parse, urllib.request
from pathlib import Path

import accuracy_frame as af

HERE = Path(__file__).resolve().parent
SPARQL = 'https://query.wikidata.org/sparql'
UA = 'strand-discovery-accuracy/1.0 (https://github.com/xBOBxSAGETx/strand-discovery)'
TRUTH_K = 100                      # studio recall: the K most-linked Wikidata films of the studio
STUDIO_DETAIL = 'keywords,release_dates'
SHORT_FILM = 'Q24862'

# studio card slug -> Wikidata item(s) of the studio (label in comment). Matched to the card's TMDB company id by
# P11806 "TMDB company ID" on the item where Wikidata has it (3 Q127552, 6125 Q1047410, 521 Q500088, 3172 Q16984664,
# 10342 Q182950, 6704 Q1189512, 33 Q168383, 12 Q79202, 297 Q301092, 56 Q457893, 923 Q621364; 174 is Q8016302, whose
# films are mostly filed under the parent Q126399); LAIKA (11537) has no P11806 and is by hand.
STUDIOS = {
    'studio-pixar': ['Q127552'], 'studio-walt-disney-animation': ['Q1047410'], 'studio-dreamworks-animation': ['Q500088'],
    'studio-blumhouse': ['Q16984664'], 'studio-studio-ghibli': ['Q182950'], 'studio-illumination': ['Q1189512'],
    'studio-warner-bros-pictures': ['Q126399', 'Q8016302'],         # Warner Bros. Entertainment + Warner Bros. Pictures
    'studio-universal': ['Q168383'], 'studio-legendary': ['Q621364'], 'studio-laika': ['Q1314323'],
    'studio-aardman': ['Q301092'], 'studio-new-line': ['Q79202'], 'studio-amblin': ['Q457893'],
}
# the five distribution-identity cards (built from Wikidata P750 / P272 by studio_distributed.py): circular, kept apart
LISTED = {
    'studio-a24': ['Q16248298'], 'studio-neon': ['Q30603559'], 'studio-lionsgate': ['Q17076072'],
    'studio-focus-features': ['Q649649'], 'studio-searchlight': ['Q953040', 'Q123459512'],
}
# movie-series card slug -> Wikidata series / franchise item(s). Hand-mapped for the seeded cards (a wrong item would
# show as a collapse in recall, so the weekly check notices); the label is the item's English label.
SERIES = {
    'series-fast-and-furious': ['Q1576873'],            # Fast & Furious
    'series-ice-age': ['Q943532'],                      # Ice Age
    'series-kill-bill': ['Q223132'],                    # Kill Bill
    'series-lord-of-the-rings': ['Q190214', 'Q74331'],  # The Lord of the Rings + The Hobbit trilogy
    'series-men-in-black': ['Q3305670'],                # Men in Black (film series)
    'series-rush-hour': ['Q7380709'],                   # Rush Hour (film series)
    'series-scream': ['Q388659'],                       # Scream
    'series-star-wars': ['Q22092344', 'Q462'],          # Star Wars (film series) + Star Wars (franchise: the TV shows)
    'series-the-hangover': ['Q1587838'],                # The Hangover (film series)
    'series-transformers': ['Q2073466'],                # Transformers (film series)
    'series-marvel-cinematic-universe': ['Q642878'],    # Marvel Cinematic Universe
    'series-dc-universe': ['Q18281265', 'Q116545465'],  # DC Extended Universe + DC Universe (media franchise)
    'series-monsterverse': ['Q21527684'],               # MonsterVerse
}
SERIES_CARDS = 10                  # seeded collection cards; the 3 keyword (universe) cards are always all measured


def sparql(query):
    """Wikidata SPARQL -> list of binding dicts. Retries; needs no key."""
    req = urllib.request.Request(SPARQL + '?format=json&query=' + urllib.parse.quote(query), headers={'User-Agent': UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)['results']['bindings']
        except Exception:
            if attempt == 3:
                raise
            time.sleep(10 * (attempt + 1))


def val(row, key, default=None):
    return row[key]['value'] if key in row else default


# ----- card picks -------------------------------------------------------------------------------------------------

def pick_studio_cards(spec):
    """(independent cards, listed cards): every studio card (there are 18; no sampling needed), split by how it is built."""
    cards = {c['slug']: c for c in spec['catalogs'] if 'studios' in c.get('folders', []) and c.get('kind') == 'discover'}
    return ([cards[s] for s in sorted(STUDIOS) if s in cards], [cards[s] for s in sorted(LISTED) if s in cards])


def pick_series_cards(spec, n=SERIES_CARDS, seed='audit-94'):
    """n seeded collection (franchise) cards of the 54 + all 3 keyword (discover) universe cards."""
    allc = [c for c in spec['catalogs'] if 'series' in c.get('folders', [])]
    coll = sorted((c for c in allc if c['kind'] == 'franchise'), key=lambda c: c['slug'])
    random.Random(f'{seed}-series').shuffle(coll)
    uni = [c for c in allc if c['kind'] == 'discover']
    return sorted(coll[:n] + uni, key=lambda c: c['slug'])


# ----- Wikidata queries and their parsing (pure parse_*) ----------------------------------------------------------

def studio_truth_query(qids, props):
    values = ' '.join('wd:' + q for q in qids)
    union = ' UNION '.join('{ ?f wdt:%s ?co }' % p for p in props)
    return """
SELECT ?f (SAMPLE(?label) AS ?title) (MIN(?date) AS ?released) (MIN(?tmdb) AS ?tmdbid) (MAX(?links) AS ?sitelinks) WHERE {
  VALUES ?co { %s }
  %s
  ?f wdt:P4947 ?tmdb ; wikibase:sitelinks ?links ; rdfs:label ?label .
  OPTIONAL { ?f wdt:P577 ?date }
  FILTER(lang(?label) = "en")
  FILTER NOT EXISTS { ?f wdt:P31 wd:%s }
} GROUP BY ?f""" % (values, union, SHORT_FILM)


def series_truth_query(qids):
    values = ' '.join('wd:' + q for q in qids)
    return """
SELECT ?f ?kind (SAMPLE(?label) AS ?title) (MIN(?date) AS ?released) (MIN(?id) AS ?tmdbid) (MAX(?links) AS ?sitelinks) WHERE {
  VALUES ?root { %s }
  { ?f wdt:P179 ?s . ?s wdt:P179* ?root } UNION { ?f wdt:P8345 ?root }
  { ?f wdt:P4947 ?id . BIND("movie" AS ?kind) } UNION { ?f wdt:P4983 ?id . BIND("series" AS ?kind) }
  ?f wikibase:sitelinks ?links ; rdfs:label ?label .
  OPTIONAL { { ?f wdt:P577 ?date } UNION { ?f wdt:P580 ?date } }
  FILTER(lang(?label) = "en")
  FILTER NOT EXISTS { ?f wdt:P31 wd:%s }
} GROUP BY ?f ?kind""" % (values, SHORT_FILM)


def parse_truth(rows, today, kind='movie'):
    """Wikidata binding rows -> {'movie:123': {'name','date','links','qid'}}. A row without a numeric TMDB id is dropped
    (P4947 is the TMDB *movie* id, P4983 the *TV* id); a film dated after `today` is not yet released: dropped; no date kept."""
    out = {}
    for r in rows:
        tid, k = val(r, 'tmdbid', ''), val(r, 'kind', kind)
        if not tid.isdigit():
            continue
        date = (val(r, 'released') or '')[:10]
        if date and date > today:
            continue
        key = f'{k}:{int(tid)}'
        links = int(val(r, 'sitelinks', 0))
        if key not in out or links > out[key]['links']:
            out[key] = {'name': val(r, 'title'), 'date': date, 'links': links, 'qid': val(r, 'f', '').rsplit('/', 1)[-1]}
    return out


def top_k(truth, k):
    """The k most-linked titles, ties by id (a stable order); -> ordered list of keys."""
    return [key for key, _ in sorted(truth.items(), key=lambda kv: (-kv[1]['links'], kv[0]))][:k]


def lookup_query(media, ids, props):
    """The Wikidata item (if any) of each TMDB id and the labels of its `props` statements."""
    idp = 'P4947' if media == 'movie' else 'P4983'
    sel = ' '.join(f'(GROUP_CONCAT(DISTINCT ?l{i}; separator="; ") AS ?v{i})' for i in range(len(props)))
    opt = ' '.join('OPTIONAL { ?f wdt:%s ?o%d . ?o%d rdfs:label ?l%d . FILTER(lang(?l%d) = "en") }' % (p, i, i, i, i)
                   for i, p in enumerate(props))
    return 'SELECT ?id ?f %s WHERE { VALUES ?id { %s } ?f wdt:%s ?id . %s } GROUP BY ?id ?f' % (
        sel, ' '.join('"%d"' % i for i in ids), idp, opt)


def parse_lookup(rows, props):
    """-> {tmdb id (int): {'qid', 'statements': 'label; label' joined over props}}; absent id = no Wikidata item."""
    out = {}
    for r in rows:
        txt = '; '.join(x for x in (val(r, f'v{i}', '') for i in range(len(props))) if x)
        out[int(val(r, 'id'))] = {'qid': val(r, 'f', '').rsplit('/', 1)[-1], 'statements': txt}
    return out


# ----- labels (pure) ----------------------------------------------------------------------------------------------

def company_ids(card):
    return {int(x) for x in str(card['movie']['with_companies']).split('|') if x}


def keyword_ids(card, media):
    return {int(x) for x in str(card.get(media, {}).get('with_keywords', '')).split('|') if x}


def classify_studio_missing(f, r, card, listed, hand_ids, excluded, today, mcount):
    """ONE Wikidata film the studio card lacks. f = facts (accuracy_frame.facts + 'companies'). -> (label, reason)."""
    if f['id'] in excluded:
        return 'deliberate rule', "hand exclude list (studio_distributed.py EXCLUDE): another company released it in the US"
    breaks = af.rule_breaks(f, 'movie', r, today)
    if breaks:
        return 'deliberate rule', '; '.join(breaks)
    credited = bool(f['companies'] & company_ids(card))
    if listed and f['id'] not in hand_ids and not credited:
        return 'code bug', 'in the Wikidata query studio_distributed.json is built from, not in the file (list needs a refresh)'
    if not listed and not credited:
        return 'source limit', "TMDB has no production-company credit for this studio (Wikidata does)"
    if mcount >= r['depth']:
        return 'cap', f"the card is at its cap of {r['depth']}"
    return 'source limit', 'passes every rule and the card is under its cap: TMDB discover paging snapshot (issue #25)'


def classify_studio_extra(f, r, today, wd):
    """ONE sampled studio-card title Wikidata does not list for the studio. wd = parse_lookup entry or None.
    -> (label, reason). Never None: the caller only asks for titles not in the Wikidata set."""
    breaks = af.rule_breaks(f, 'movie', r, today)
    if breaks:
        return 'code bug', 'on the card but breaks its own rule: ' + '; '.join(breaks)
    if not wd:
        return 'source limit', 'unconfirmed: no Wikidata item for this TMDB id'
    if not wd['statements']:
        return 'source limit', f"unconfirmed: Wikidata item {wd['qid']} has no production company / distributor statement"
    return 'source limit', f"disagrees: Wikidata {wd['qid']} names {wd['statements']}, not the studio"


def classify_series_missing(f, media, card, r, today, mcount, ctx):
    """ONE Wikidata series member the card lacks. ctx = {'collections': {int}, 'hand': {key}}. -> (label, reason)."""
    keyword_card = card['kind'] == 'discover'
    if not f['date'] or f['date'] > today:
        return 'deliberate rule', 'not released yet (released-only rule)'
    if keyword_card:
        breaks = af.rule_breaks(f, media, r, today)
        if breaks:
            return 'deliberate rule', '; '.join(breaks)
        if not f['keywords'] & keyword_ids(card, media):
            return 'source limit', 'TMDB has not tagged it with the card\'s keyword (Wikidata lists it in the series)'
        if mcount >= r['depth']:
            return 'cap', f"the card is at its cap of {r['depth']}"
        return 'source limit', 'passes every rule and the card is under its cap: TMDB discover paging snapshot (issue #25)'
    if media == 'series':
        return 'source limit', "TMDB collections hold films only and the card's hand list does not name this show"
    if f['collection'] in ctx['collections']:
        return 'code bug', 'in the card\'s TMDB collection and released, yet not on the card'
    return 'source limit', "TMDB does not group it into the card's collection (Wikidata does); a hand-list id would add it"


def classify_series_extra(f, media, card, r, today, wd):
    """ONE sampled series-card title that is not in the Wikidata set. -> (label, reason)."""
    if not f['date'] or f['date'] > today:
        return 'code bug', 'on the card but not released yet (released-only rule)'
    if card['kind'] == 'discover':
        breaks = af.rule_breaks(f, media, r, today)
        if breaks:
            return 'code bug', 'on the card but breaks its own rule: ' + '; '.join(breaks)
    if not wd:
        return 'source limit', 'unconfirmed: no Wikidata item for this TMDB id'
    if not wd['statements']:
        return 'source limit', f"unconfirmed: Wikidata item {wd['qid']} is in no series / franchise"
    return 'source limit', f"disagrees: Wikidata {wd['qid']} is part of {wd['statements']}, not the card's series"


# ----- the measure -------------------------------------------------------------------------------------------------

def wd_studio_truth(slug, listed, today):
    qids = (LISTED if listed else STUDIOS)[slug]
    return parse_truth(sparql(studio_truth_query(qids, ('P750', 'P272') if listed else ('P272',))), today)


def wd_series_truth(slug, today):
    return parse_truth(sparql(series_truth_query(SERIES[slug])), today)


def wd_lookup(media, ids, props):
    ids = sorted(ids)
    out = {}
    for i in range(0, len(ids), 40):
        out.update(parse_lookup(sparql(lookup_query(media, ids[i:i + 40], props)), props))
    return out


def detail_facts(tmdb, media, tid):
    det = tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}", append_to_response=STUDIO_DETAIL)
    f = af.facts(det, media)
    f['companies'] = {c['id'] for c in det.get('production_companies') or []}
    f['collection'] = (det.get('belongs_to_collection') or {}).get('id')
    return f


def hand_ids_of(slug):
    try:
        data = json.loads((HERE / 'studio_distributed.json').read_text(encoding='utf-8'))
    except OSError:
        return set()
    return {int(e['tmdb']) for e in data.get(slug, [])}


def measure(kind, cards, spec, order_of, tmdb, today, n_sample, seed, truth_for=None, lookup=None, excluded=None,
            hand_for=hand_ids_of):
    """kind = 'studio' | 'studio_listed' | 'series'. order_of(slug) -> ordered keys of the whole card; tmdb(path, **p) ->
    json; truth_for(card) -> parse_truth dict; lookup(media, ids) -> parse_lookup dict. -> (summary row, gap rows)."""
    studio, listed = kind.startswith('studio'), kind == 'studio_listed'
    truth_for = truth_for or (lambda c: wd_studio_truth(c['slug'], listed, today) if studio else wd_series_truth(c['slug'], today))
    lookup = lookup or (lambda media, ids: wd_lookup(media, ids, ('P272', 'P750') if studio else ('P179', 'P8345')))
    if excluded is None:
        from studio_distributed import EXCLUDE
        excluded = {s: {e['tmdb'] for e in es} for s, es in EXCLUDE.items()}
    gaps, rf, rt, samp, ok, at_cap, cache = [], 0, 0, 0, 0, [], {}
    prec = {'unconfirmed': 0, 'disagrees': 0}

    def detail(media, tid):
        if (media, tid) not in cache:
            cache[(media, tid)] = detail_facts(tmdb, media, tid)
        return cache[(media, tid)]
    for card in cards:
        slug, order = card['slug'], order_of(card['slug'])
        have = set(order)
        truth = truth_for(card)
        medias = ['movie'] if studio else sorted({m for m in card['media']} | {e['type'] for e in card.get('ids', [])})
        ctx = {'collections': {int(c) for c in card.get('collection_ids', [])}}
        r = {m: af.card_rules('popular', {**card, 'sort': card['sort'] if card['kind'] == 'discover' else 'popular'}, m, spec)
             for m in medias}
        counts = {m: sum(1 for k in order if k.startswith(m + ':')) for m in medias}
        claim = top_k({k: v for k, v in truth.items() if k.split(':')[0] in medias}, TRUTH_K if studio else 10 ** 6)
        for m in medias:
            if card['kind'] == 'discover' and counts[m] >= r[m]['depth']:
                at_cap.append(f'{slug}:{m}')
        found, total, missing = af.score(claim, have)
        rf += found
        rt += total
        for k in missing:
            media, tid = k.split(':')
            f = detail(media, int(tid))
            if studio:
                lab, why = classify_studio_missing(f, r['movie'], card, listed, hand_for(slug), excluded.get(slug, set()),
                                                   today, counts['movie'])
            else:
                lab, why = classify_series_missing(f, media, card, r[media], today, counts[media], ctx)
            gaps.append({'kind': kind, 'card': slug, 'side': 'recall', 'key': k, 'name': truth[k]['name'], 'label': lab, 'reason': why})
        pool = list(order)
        picked = random.Random(f'{seed}-{slug}').sample(pool, min(n_sample, len(pool)))
        bad = [k for k in picked if k not in truth]
        samp += len(picked)
        ok += len(picked) - len(bad)
        wd = {m: lookup(m, [int(k.split(':')[1]) for k in bad if k.startswith(m + ':')]) for m in ('movie', 'series')} if bad else {}
        for k in bad:
            media, tid = k.split(':')
            f = detail(media, int(tid))
            w = wd[media].get(int(tid))
            lab, why = (classify_studio_extra(f, r['movie'], today, w) if studio
                        else classify_series_extra(f, media, card, r.get(media) or r[medias[0]], today, w))
            for tag in prec:
                if why.startswith(tag):
                    prec[tag] += 1
            gaps.append({'kind': kind, 'card': slug, 'side': 'precision', 'key': k, 'name': f['name'], 'label': lab, 'reason': why})
    row = af.summarize(kind, cards, rf, rt, samp, ok, gaps)
    row['at_cap'] = at_cap
    row['precision_unconfirmed'], row['precision_disagrees'] = prec['unconfirmed'], prec['disagrees']
    return row, gaps
