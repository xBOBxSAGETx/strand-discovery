"""Accuracy frame, Awards (issue #97, audit job 6 of 6 of #33): the 7 award cards, truth = Wikidata (and, for the three
MDBList-built cards, also the committed Wikipedia reference lists in awards.json).

Same frame as accuracy_works.py (cards -> RECALL + PRECISION -> every gap labelled code bug / deliberate rule / cap /
source limit; rows are built with accuracy_frame.summarize so accuracy_check.py prints them in the same table).

THE CLAIMS and THE RULES in code (generator/spec.json, build.py idlist_ids / mdblist, awards_wiki.py):
  idlist cards   Emmy (Outstanding Drama + Comedy + Limited/Anthology Series winners), Sundance (U.S. Dramatic + U.S.
                 Documentary Grand Jury Prize winners; World Cinema left out on purpose), Palme d'Or (Palme d'Or winners
                 + the pre-1955 Grand Prix; the Special / Honorary Palme is left out), Golden Globe (Best Motion Picture
                 Drama + Musical or Comedy winners). Built from the Wikipedia pages by awards_wiki.py into awards.json, in
                 the page's own order, no rule on top.
  mdblist cards  Oscar Best Picture (winners), Oscar Best Picture Nominees, BAFTA Best Film: a public MDBList list each
                 (build.mdblist), minus the spec's hand exclude list (Sunrise, The Crowd, Chang: the first ceremony's
                 "Outstanding Picture" / "Unique and Artistic Picture" no one counts as a Best Picture).
TRUTH. Wikidata, a different source from Wikipedia: a title is a winner when it has a "winner" statement (P166) whose award
item is the card's award item or a subclass of it (P279*), or (nominees) a "nominated for" statement (P1411). One query
per medium and per property (a property path joined with a union in one query makes WDQS silently drop rows). Emmy: a
win is often recorded on the SEASON item ("Breaking Bad, season 5"), so a season's win counts for its series (P179 /
P361 "part of the series"). The label service asks "en,mul" (items with only a "mul" label still count), and no filter
is put on a label. Recall = Wikidata winners found on the card. Precision = the WHOLE card (the cards are small), each
title looked up on Wikidata by its TMDB id: it must carry the card's award. A title Wikidata has no item for, or no award
statement at all, is UNJUDGED (left out of the precision denominator and counted in `unjudged`), never wrong. A title with
award statements that lack the card's award is "disagrees"; before it is counted the title's own entity is fetched from
Wikidata and every P166 / P1411 statement of any rank is read: only when those really lack the card's award is it a gap.
MDBList cards are also measured against the Wikipedia reference list (awards.json, committed, made by awards_wiki.py): a
second row `<card>@wikipedia`. The idlist cards are built FROM that Wikipedia list, so a Wikipedia truth would be circular:
it is not computed for them.

LABELS (same four): code bug = the card holds a title its own hand exclude list removes, or the card's source list could not
resolve a title that Wikidata names with a TMDB id (awards.json "unresolved"); deliberate rule = the hand exclude list, or
an award item the card leaves out on purpose (Honorary Palme d'Or); cap = never (these cards have no depth cap); source
limit = Wikidata and the card's source disagree, or Wikidata cannot confirm a title.
Pure functions (parse_*, classify_*) take plain dicts and are unit-tested offline (tests/test_accuracy_awards.py).
"""
import json, re, time, urllib.parse, urllib.request
from pathlib import Path

import accuracy_frame as af
import accuracy_corrections as ac
from accuracy_works import sparql, val

HERE = Path(__file__).resolve().parent
UA = 'strand-discovery-accuracy/1.0 (https://github.com/xBOBxSAGETx/strand-discovery)'
IDP = {'movie': 'P4947', 'series': 'P4983'}          # Wikidata "TMDB movie id" / "TMDB TV series id"
# card slug -> the awards.json list it is built from (idlist) / its Wikipedia reference list (mdblist), the Wikidata award
# item(s) of the claim (subclasses count), award items the card leaves out on purpose, the properties that count, seasons.
CARDS = {
    'awards-emmy-best-series': {'roots': ['Q989438', 'Q2110156', 'Q20714679', 'Q13423511'], 'props': ('P166',), 'seasons': True,
                                'list': 'emmy-best-series'},
    'awards-sundance-gjp': {'roots': ['Q3774974', 'Q2366088'], 'props': ('P166',), 'list': 'sundance-gjp'},
    'awards-palme-dor': {'roots': ['Q179808'], 'out': ['Q1321674'], 'props': ('P166',), 'list': 'palme-dor'},
    'awards-golden-globe-best-picture': {'roots': ['Q1011509', 'Q670282', 'Q3110063'], 'props': ('P166',),
                                         'list': 'golden-globe-best-picture'},
    'awards-oscar-best-picture': {'roots': ['Q102427'], 'props': ('P166',), 'ref': 'oscar-winners'},
    'awards-oscar-best-picture-nominees': {'roots': ['Q102427'], 'props': ('P166', 'P1411'), 'ref': 'oscar-nominees'},
    'awards-bafta-best-film': {'roots': ['Q139184'], 'props': ('P166',), 'ref': 'bafta-best-film'},
}


def qid(uri):
    return uri.rsplit('/', 1)[-1]


# ----- Wikidata queries (pure builders) and their parsing (pure parse_*) -----------------------------------------------

def scope_query(roots):
    """The award items of the claim: the roots and everything that is a subclass of one (a plain path, nothing else)."""
    return 'SELECT DISTINCT ?a WHERE { VALUES ?root { %s } ?a wdt:P279* ?root }' % ' '.join('wd:' + q for q in roots)


def truth_query(scope, prop, media, via=None):
    """Titles with `prop` pointing at an award of `scope`. via = None (the title's own statement) | 'P179' | 'P361' (the
    statement sits on a season / child item that is part of the title): one property per query, no union."""
    vals = ' '.join('wd:' + q for q in sorted(scope))
    link = '?s wdt:%s ?f .' % via if via else ''
    holder = '?s' if via else '?f'
    return """
SELECT ?f (SAMPLE(?label) AS ?title) (GROUP_CONCAT(DISTINCT ?id; separator=" ") AS ?ids) (GROUP_CONCAT(DISTINCT STRAFTER(STR(?a), "entity/"); separator=" ") AS ?awards) WHERE {
  VALUES ?a { %s }
  %s wdt:%s ?a .
  %s
  ?f wdt:%s ?id .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul" . ?f rdfs:label ?label }
} GROUP BY ?f""" % (vals, holder, prop, link, IDP[media])


def parse_truth(rows, media, into=None):
    """Binding rows -> {Wikidata item: {'name','keys': {'movie:1'}, 'awards': {Q..}}}; merges into `into`. A non-numeric
    TMDB id is dropped. Ids stay a SET per item: one film can carry two TMDB ids on Wikidata, either one on the card counts."""
    out = into if into is not None else {}
    for r in rows:
        keys = {f'{media}:{int(i)}' for i in val(r, 'ids', '').split() if i.isdigit()}
        if not keys:
            continue
        e = out.setdefault(qid(val(r, 'f', '')), {'name': val(r, 'title'), 'keys': set(), 'awards': set()})
        e['keys'] |= keys
        e['awards'] |= set(val(r, 'awards', '').split())
        e['name'] = e['name'] or val(r, 'title')
    return out


def lookup_query(media, ids, props, via=None):
    """The award items each TMDB id's Wikidata item carries (?aw = their Q-ids, taken outside any label filter)."""
    idp = IDP[media]
    if via:
        body = '?f wdt:%s ?id . ?s wdt:%s ?f . ?s wdt:%s ?a0 .' % (idp, via, props[0])
        n = 1
    else:
        body = '?f wdt:%s ?id . ' % idp + ' '.join('OPTIONAL { ?f wdt:%s ?a%d }' % (p, i) for i, p in enumerate(props))
        n = len(props)
    sel = ' '.join('(GROUP_CONCAT(DISTINCT STRAFTER(STR(?a%d), "entity/"); separator=" ") AS ?aw%d)' % (i, i) for i in range(n))
    return 'SELECT ?id ?f %s WHERE { VALUES ?id { %s } %s } GROUP BY ?id ?f' % (
        sel, ' '.join('"%d"' % i for i in ids), body)


def parse_lookup(rows, into=None):
    """-> {tmdb id (int): {'qid', 'win': {Q..} (P166), 'nom': {Q..} (P1411), 'awards': win | nom}}; an id absent from the
    result has no Wikidata item. aw0 = P166, aw1 = P1411. Merges into `into`."""
    out = into if into is not None else {}
    for r in rows:
        if not val(r, 'id', '').isdigit():
            continue
        e = out.setdefault(int(val(r, 'id')), {'qid': qid(val(r, 'f', '')), 'win': set(), 'nom': set(), 'awards': set()})
        e['win'] |= set(val(r, 'aw0', '').split())
        e['nom'] |= set(val(r, 'aw1', '').split())
        e['awards'] = e['win'] | e['nom']
    return out


def entity_awards(entity_json, qid_, props=('P166', 'P1411')):
    """Every award item named by `props` on the entity, ANY rank (best-rank wdt: statements miss a deprecated one)."""
    claims = entity_json.get('entities', {}).get(qid_, {}).get('claims', {})
    out = set()
    for p in props:
        for s in claims.get(p, []):
            v = (s.get('mainsnak') or {}).get('datavalue', {}).get('value')
            if isinstance(v, dict) and v.get('id'):
                out.add(v['id'])
    return out


# ----- classification (pure) ----------------------------------------------------------------------------------------------

def classify_missing(entry, ctx):
    """ONE Wikidata winner the card lacks. entry = parse_truth item. ctx: 'excluded' {keys}, 'out' {award Qs}, 'ref' {keys}
    | None (idlist card), 'unresolved' [titles of the card's source list that had no TMDB id]. -> (label, reason)."""
    name = (entry['name'] or '').lower()
    if entry['keys'] & ctx['excluded']:
        return 'deliberate rule', 'hand exclude list (spec exclude / deny): the card removes it on purpose'
    if entry['awards'] and entry['awards'] <= ctx['out']:
        return 'deliberate rule', 'only an award item the card leaves out on purpose (e.g. the Honorary Palme d\'Or)'
    if name and any(u.lower().startswith(name) for u in ctx['unresolved']):
        return 'code bug', 'the card\'s source list (awards.json "unresolved") could not match it to a TMDB id; Wikidata names ' + \
            ', '.join(sorted(entry['keys']))
    if ctx.get('wiki') and entry['name'] and listed_in(ctx['wiki'], entry['name']):
        return 'code bug', ("the card's source page on Wikipedia lists it, yet it is not in awards.json (awards_wiki.py did not "
                            "take the row: unlinked title or text after the link)")
    if ctx['ref'] is not None:
        if entry['keys'] & ctx['ref']:
            return 'source limit', "the MDBList list lacks it; the Wikipedia reference list has it"
        return 'source limit', 'on Wikidata only: neither the MDBList list nor the Wikipedia list has it'
    return 'source limit', "Wikidata records the award; the card's source (the Wikipedia list in awards.json) does not list it"


def classify_extra(key, wd, scope, nominee_claim):
    """ONE card title Wikidata's truth set does not contain. wd = parse_lookup entry | None. -> (label, reason).
    'unconfirmed' reasons are UNJUDGED: Wikidata has no statement about the card's award for it (no item, no award at all,
    or only OTHER awards: Wikidata's award coverage is thin, and a missing statement is not a contradiction).
    The one POSITIVE contradiction: Wikidata lists the title as a NOMINEE of the card's award but not as a winner
    (winner cards only; the nominees card counts both)."""
    if not wd:
        return 'source limit', 'unconfirmed: no Wikidata item for this TMDB id'
    if not wd['awards']:
        return 'source limit', f"unconfirmed: Wikidata item {wd['qid']} has no award statement"
    if not nominee_claim and wd['nom'] & scope:
        return 'source limit', f"disagrees: Wikidata {wd['qid']} lists it as a NOMINEE of the card's award, not a winner"
    return 'source limit', f"unconfirmed: Wikidata item {wd['qid']} names other awards only, none of the card's"


def classify_ref_extra(key, ctx):
    """A MDBList-card title the Wikipedia reference list lacks. -> (label, reason)."""
    if key in ctx['excluded']:
        return 'code bug', 'on the card but in the hand exclude list'
    return 'source limit', 'the MDBList list has it; the Wikipedia reference list does not'


def listed_in(text, name):
    """The Wikipedia wikitext shows `name` as an italic (film / series) title, linked or not: ''Name'' or ''[[Name]]'' or
    ''[[Target|Name]]''. A name in running text or inside another title does not count."""
    return bool(re.search(r"''+\s*(?:\[\[(?:[^\]|]*\|)?)?" + re.escape(name) + r"(?![\w])", text, re.I))


def source_pages(entries):
    """The Wikipedia page names an awards.json list was built from (its `source` urls)."""
    pages = []
    for e in entries:
        for u in (e.get('source') or '').split(' ; '):
            if '/wiki/' in u:
                n = urllib.parse.unquote(u.split('/wiki/', 1)[1])
                if n not in pages:
                    pages.append(n)
    return pages


# ----- Wikidata fetches ---------------------------------------------------------------------------------------------------

def wd_scope(roots):
    return {qid(val(r, 'a', '')) for r in sparql(scope_query(roots))}


def wd_truth(cfg, scope, medias):
    truth = {}
    for media in medias:
        for prop in cfg['props']:
            parse_truth(sparql(truth_query(scope, prop, media)), media, truth)
            if cfg.get('seasons') and media == 'series':
                for via in ('P179', 'P361'):
                    parse_truth(sparql(truth_query(scope, prop, media, via)), media, truth)
    return truth


def wd_lookup(cfg, media, ids):
    out = {}
    ids = sorted(ids)
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        parse_lookup(sparql(lookup_query(media, chunk, ('P166', 'P1411'))), out)
        if cfg.get('seasons') and media == 'series':
            for via in ('P179', 'P361'):
                parse_lookup(sparql(lookup_query(media, chunk, ('P166',), via)), out)
    return out


def wd_entity(q):
    req = urllib.request.Request(f'https://www.wikidata.org/wiki/Special:EntityData/{q}.json', headers={'User-Agent': UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def wd_wikitext(page):
    """Raw wikitext of an English Wikipedia page ('' when it cannot be read: the code-bug check is then skipped)."""
    req = urllib.request.Request('https://en.wikipedia.org/w/index.php?action=raw&title=' + urllib.parse.quote(page), headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode('utf-8')
    except Exception:
        return ''


def load_lists():
    return json.loads((HERE / 'awards.json').read_text(encoding='utf-8'))


def keys_of(entries):
    return {f"{e['type']}:{int(e['tmdb'])}" for e in entries}


# ----- the measure --------------------------------------------------------------------------------------------------------

def measure_card(card, order, spec, lists, wd=None, names=None):
    """One award card -> list of (summary row, gap rows): the Wikidata row, and for an MDBList card the Wikipedia row.
    order = ordered keys of the whole card. wd = {'scope','truth','lookup','entity'} functions (None: live Wikidata)."""
    cfg = CARDS[card['slug']]
    names = names or {}
    wd = wd or {'scope': wd_scope, 'truth': wd_truth, 'lookup': wd_lookup, 'entity': wd_entity, 'wikitext': wd_wikitext}
    slug, have = card['slug'], set(order)
    excluded = {f"{e['type']}:{int(e['tmdb'])}" for e in card.get('exclude', [])}
    excluded |= {f"{e['type']}:{int(e['tmdb'])}" for e in spec['defaults'].get('deny', [])}
    ref = keys_of(lists[cfg['ref']]['ids']) if 'ref' in cfg else None
    src = lists[cfg.get('list') or cfg['ref']]
    ctx = {'excluded': excluded, 'out': set(cfg.get('out', [])), 'ref': ref, 'unresolved': src.get('unresolved', []), 'wiki': ''}
    if 'list' in cfg:                              # idlist card: the Wikipedia pages its list was built from
        ctx['wiki'] = '\n'.join(wd['wikitext'](pg) for pg in source_pages(src['ids']))
    medias = [m for m in ('movie', 'series') if m in card['media']]
    scope = wd['scope'](cfg['roots'])
    truth = wd['truth'](cfg, scope, medias)
    out, gaps = [], []
    # recall: a Wikidata winner is found when ANY of its TMDB ids is on the card
    found = filled = 0
    for q, e in sorted(truth.items(), key=lambda kv: (kv[1]['name'] or '', kv[0])):
        if e['keys'] & have:
            found += 1
            filled += any(ac.filled(slug, [k], have) for k in e['keys'])
            continue
        lab, why = classify_missing(e, ctx)
        gaps.append({'kind': slug, 'card': slug, 'side': 'recall', 'key': sorted(e['keys'])[0], 'name': e['name'], 'label': lab,
                     'reason': why})
    # precision: the whole card, each title by its TMDB id
    truth_keys = {k for e in truth.values() for k in e['keys']}
    unj = disagrees = 0
    extra = [k for k in order if k not in truth_keys]
    ok = len(order) - len(extra)
    looked = {m: wd['lookup'](cfg, m, [int(k.split(':')[1]) for k in extra if k.startswith(m + ':')]) for m in medias}
    for k in extra:
        media, tid = k.split(':')
        if k in ctx['excluded']:                   # the card holds a title its own exclude list removes
            gaps.append({'kind': slug, 'card': slug, 'side': 'precision', 'key': k, 'name': names.get(k, k), 'label': 'code bug',
                         'reason': 'on the card but in the hand exclude list'})
            continue
        w = looked.get(media, {}).get(int(tid))
        lab, why = classify_extra(k, w, scope, 'P1411' in cfg['props'])
        if why.startswith('disagrees'):            # confirm on the title's own entity (any rank) before it counts as a gap
            if entity_awards(wd['entity'](w['qid']), w['qid'], ('P166',)) & scope:
                ok += 1
                continue
            disagrees += 1
        elif why.startswith('unconfirmed'):
            unj += 1
            continue
        gaps.append({'kind': slug, 'card': slug, 'side': 'precision', 'key': k, 'name': names.get(k, k), 'label': lab, 'reason': why})
    judged = len(order) - unj
    row = af.summarize(slug, [card], found, len(truth), judged, ok, gaps, filled)
    row.update({'unjudged': unj, 'sample': judged, 'precision': af.pct(ok, judged), 'coverage': af.pct(judged, len(order)), 'at_cap': [], 'precision_disagrees': disagrees,
                'truth_source': 'Wikidata ' + '/'.join(cfg['props']), 'circular': False})
    out.append((row, gaps))
    if ref is not None:                            # MDBList card: also against the Wikipedia reference list
        rg, rf = [], 0
        for k in sorted(ref):
            if k in have:
                rf += 1
            else:
                lab, why = ('deliberate rule', 'hand exclude list (spec exclude): the card removes it on purpose') if k in excluded else \
                    ('source limit', 'the Wikipedia reference list has it; the MDBList list does not')
                rg.append({'kind': slug + '@wikipedia', 'card': slug, 'side': 'recall', 'key': k, 'name': names.get(k, k), 'label': lab, 'reason': why})
        rok = 0
        for k in order:
            if k in ref:
                rok += 1
            else:
                lab, why = classify_ref_extra(k, ctx)
                rg.append({'kind': slug + '@wikipedia', 'card': slug, 'side': 'precision', 'key': k, 'name': names.get(k, k), 'label': lab, 'reason': why})
        rrow = af.summarize(slug + '@wikipedia', [card], rf, len(ref), len(order), rok, rg)
        rrow.update({'unjudged': 0, 'sample': len(order), 'at_cap': [], 'precision_disagrees': 0,
                     'truth_source': f"Wikipedia list ({cfg['ref']}, awards.json)", 'circular': False})
        out.append((rrow, rg))
    return out
