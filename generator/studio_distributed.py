"""Refresh generator/studio_distributed.json: films each studio card's company DISTRIBUTED (issue #66).

TMDB's with_companies only knows production companies, so A24's Lady Bird / Hereditary and NEON's Parasite are
missing from the studio cards. Wikidata knows the distributor (P750) and carries the TMDB movie id (P4947).

    python generator/studio_distributed.py            # needs no key; ~20 SPARQL calls; run by hand, commit the JSON

build.py reads the JSON; it never calls Wikidata. Per studio: every released feature film with a TMDB id whose
"distributed by" (P750) or "production company" (P272, catches TMDB company gaps) is one of the studio's Wikidata items,
most-linked first (sitelink count = popularity proxy), at most CAP per studio. P4947 is the TMDB *movie* id, so every hit is a film; short films (P31 = short film) are left out.
"""
import datetime as dt, json, sys, time, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'studio_distributed.json'
SPARQL = 'https://query.wikidata.org/sparql'
UA = 'strand-discovery-studio-distributed/1.0 (https://github.com/xBOBxSAGETx/strand-discovery)'
CAP = 400      # per studio; only Universal and Warner Bros. (thousands of distributed titles) reach it

# studio card slug -> [(Wikidata QID, its English label: checked against Wikidata on every run)]
STUDIO_ITEMS = {
    'studio-pixar': [('Q127552', 'Pixar')],
    'studio-walt-disney-animation': [('Q1047410', 'Walt Disney Animation Studios')],
    'studio-dreamworks-animation': [('Q500088', 'DreamWorks Animation')],
    'studio-a24': [('Q16248298', 'A24')],
    'studio-blumhouse': [('Q16984664', 'Blumhouse Productions')],
    'studio-studio-ghibli': [('Q182950', 'Studio Ghibli')],
    'studio-illumination': [('Q1189512', 'Illumination')],
    'studio-warner-bros-pictures': [('Q8016302', 'Warner Bros. Pictures')],
    'studio-universal': [('Q168383', 'Universal Pictures')],
    'studio-neon': [('Q30603559', 'Neon')],
    'studio-legendary': [('Q621364', 'Legendary Pictures')],
    'studio-lionsgate': [('Q17076072', 'Lionsgate Films')],
    'studio-focus-features': [('Q649649', 'Focus Features')],
    'studio-searchlight': [('Q953040', 'Searchlight Pictures'), ('Q123459512', 'Fox Searchlight Pictures')],
    'studio-laika': [('Q1314323', 'Laika')],
    'studio-aardman': [('Q301092', 'Aardman Animations')],
    'studio-new-line': [('Q79202', 'New Line Cinema')],
    'studio-amblin': [('Q457893', 'Amblin Entertainment')],
}


def sparql(query):
    req = urllib.request.Request(SPARQL + '?format=json&query=' + urllib.parse.quote(query), headers={'User-Agent': UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)['results']['bindings']
        except Exception:
            if attempt == 3:
                raise
            time.sleep(10 * (attempt + 1))


def check_labels():
    """Each hand-mapped QID must still carry the label we mapped it by (a merged/renamed item stops the run)."""
    wanted = {q: lab for items in STUDIO_ITEMS.values() for q, lab in items}
    rows = sparql('SELECT ?i ?l WHERE { VALUES ?i { %s } ?i rdfs:label ?l. FILTER(lang(?l)="en") }'
                  % ' '.join('wd:' + q for q in wanted))
    got = {r['i']['value'].rsplit('/', 1)[-1]: r['l']['value'] for r in rows}
    bad = {q: (lab, got.get(q)) for q, lab in wanted.items() if got.get(q) != lab}
    if bad:
        sys.exit(f'QID label mismatch (expected, found): {bad}')


def films(qids, today):
    values = ' '.join('wd:' + q for q in qids)
    rows = sparql("""
SELECT ?f (SAMPLE(?label) AS ?title) (MIN(?date) AS ?released) (MIN(?tmdb) AS ?tmdbid) (MAX(?links) AS ?sitelinks) WHERE {
  VALUES ?co { %s }
  { ?f wdt:P750 ?co } UNION { ?f wdt:P272 ?co }
  ?f wdt:P4947 ?tmdb ; wdt:P577 ?date ; wikibase:sitelinks ?links ; rdfs:label ?label .
  FILTER(lang(?label) = "en" && ?date <= "%sT23:59:59Z"^^xsd:dateTime)
  FILTER NOT EXISTS { ?f wdt:P31 wd:Q24862 }
} GROUP BY ?f""" % (values, today))
    out = []
    for r in rows:
        tid = r['tmdbid']['value']
        if not tid.isdigit():
            continue
        out.append({'tmdb': int(tid), 'title': r['title']['value'], 'year': int(r['released']['value'][:4]),
                    'qid': r['f']['value'].rsplit('/', 1)[-1], '_links': int(r['sitelinks']['value'])})
    return out


def main():
    check_labels()
    today = dt.date.today().isoformat()
    result = {}
    for slug, items in STUDIO_ITEMS.items():
        rows, seen = [], set()
        for r in sorted(films([q for q, _ in items], today), key=lambda r: (-r['_links'], r['tmdb'])):
            if r['tmdb'] not in seen:
                seen.add(r['tmdb'])
                rows.append(r)
        total = len(rows)
        result[slug] = [{k: v for k, v in r.items() if k != '_links'} for r in rows[:CAP]]
        print(f'{slug}: {total} films' + (f' (capped to {CAP})' if total > CAP else ''))
        time.sleep(1)
    OUT.write_text(json.dumps(result, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
