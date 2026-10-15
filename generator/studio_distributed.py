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
CAP = 400      # per studio; a safety limit; none of the five studios reaches it today

# studio card slug -> [(Wikidata QID, its English label: checked against Wikidata on every run)]
STUDIO_ITEMS = {
    'studio-a24': [('Q16248298', 'A24')],
    'studio-neon': [('Q30603559', 'Neon')],
    'studio-lionsgate': [('Q17076072', 'Lionsgate Films')],
    'studio-focus-features': [('Q649649', 'Focus Features')],
    'studio-searchlight': [('Q953040', 'Searchlight Pictures'), ('Q123459512', 'Fox Searchlight Pictures')],
}
# Distributor-only films of a known non-US origin (UK-only or old Canadian releases) are dropped for these studios only:
# on A24 / NEON / Searchlight the same rule also drops real US releases (Parasite, Ex Machina, 28 Days Later).
US_ONLY_FILTER = {'studio-lionsgate', 'studio-focus-features'}
# Only studios whose identity IS distribution. Production studios (Pixar, Disney Animation, DreamWorks, Ghibli,
# Illumination, LAIKA, Aardman, Blumhouse, Legendary, Amblin) and the majors (Warner, Universal, New Line) are left out:
# their TMDB production lists are right, and P750 on a major pulls in territory-only international distribution.


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
SELECT ?f (SAMPLE(?label) AS ?title) (MIN(?date) AS ?released) (MIN(?tmdb) AS ?tmdbid) (MAX(?links) AS ?sitelinks)
       (MAX(IF(?prod, 1, 0)) AS ?produced) (MAX(IF(?us, 1, 0)) AS ?inus) (MAX(IF(?anyc, 1, 0)) AS ?hascountry) WHERE {
  VALUES ?co { %s }
  { ?f wdt:P750 ?co } UNION { ?f wdt:P272 ?co }
  ?f wdt:P4947 ?tmdb ; wdt:P577 ?date ; wikibase:sitelinks ?links ; rdfs:label ?label .
  FILTER(lang(?label) = "en" && ?date <= "%sT23:59:59Z"^^xsd:dateTime)
  FILTER NOT EXISTS { ?f wdt:P31 wd:Q24862 }
  BIND(EXISTS { ?f wdt:P272 ?co } AS ?prod)
  BIND(EXISTS { ?f wdt:P495 wd:Q30 } AS ?us)
  BIND(EXISTS { ?f wdt:P495 ?c } AS ?anyc)
} GROUP BY ?f""" % (values, today))
    out = []
    for r in rows:
        tid = r['tmdbid']['value']
        if not tid.isdigit():
            continue
        out.append({'tmdb': int(tid), 'title': r['title']['value'], 'year': int(r['released']['value'][:4]),
                    'qid': r['f']['value'].rsplit('/', 1)[-1], '_links': int(r['sitelinks']['value']),
                    # distributor-only (no P272) AND a known country of origin without the US = a territory release
                    '_foreign': r['produced']['value'] == '0' and r['hascountry']['value'] == '1' and r['inus']['value'] == '0'})
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
        kept = [r for r in rows if not (r['_foreign'] and slug in US_ONLY_FILTER)]
        result[slug] = [{k: v for k, v in r.items() if not k.startswith('_')} for r in kept[:CAP]]
        print(f'{slug}: {total} films, {total - len(kept)} dropped as distributor-only non-US' + ('' if slug in US_ONLY_FILTER else ' (rule off)') + f'' +
              (f' (capped to {CAP})' if len(kept) > CAP else ''))
        for r in rows:
            if r['_foreign']:
                print('   dropped:', r['title'], r['year'], r['qid'], r['tmdb'])
        time.sleep(1)
    OUT.write_text(json.dumps(result, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
