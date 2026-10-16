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
WIKIDATA = 'https://www.wikidata.org/wiki/'     # an entry's source: its Wikidata item
CAP = 400      # per studio; a safety limit; none of the five studios reaches it today

# studio card slug -> [(Wikidata QID, its English label: checked against Wikidata on every run)]
STUDIO_ITEMS = {
    'studio-a24': [('Q16248298', 'A24')],
    'studio-neon': [('Q30603559', 'Neon')],
    'studio-lionsgate': [('Q17076072', 'Lionsgate Films')],
    'studio-focus-features': [('Q649649', 'Focus Features')],
    'studio-searchlight': [('Q953040', 'Searchlight Pictures'), ('Q123459512', 'Fox Searchlight Pictures')],
}
# Only studios whose identity IS distribution. Production studios (Pixar, Disney Animation, DreamWorks, Ghibli,
# Illumination, LAIKA, Aardman, Blumhouse, Legendary, Amblin) and the majors (Warner, Universal, New Line) are left out:
# their TMDB production lists are right, and P750 on a major pulls in territory-only international distribution.

# Hand exclude list: films another company released in the US, so they do not belong on the studio card (the studio only
# had a territory release, e.g. Lionsgate UK). A title is listed only with a source that names the US distributor.
# `why` = the quote + URL (Box Office Mojo "Domestic Distributor" unless it says Wikipedia); `source` = that URL.
# Each entry is a hand-list entry {"type","tmdb","title","year","source"} (+ why).
EXCLUDE = {
    'studio-lionsgate': [
        {'type': 'movie', 'tmdb': 366668, 'title': "Playmobil: The Movie", 'year': 2019, 'source': 'https://www.boxofficemojo.com/title/tt4199898/', 'why': "Box Office Mojo \"Domestic Distributor: STX Entertainment\" (https://www.boxofficemojo.com/title/tt4199898/)"},
        {'type': 'movie', 'tmdb': 86828, 'title': "Absolutely Anything", 'year': 2015, 'source': 'https://www.boxofficemojo.com/title/tt1727770/', 'why': "Box Office Mojo \"Domestic Distributor: Atlas Distribution Company\" (https://www.boxofficemojo.com/title/tt1727770/)"},
        {'type': 'movie', 'tmdb': 283227, 'title': "A Little Chaos", 'year': 2015, 'source': 'https://www.boxofficemojo.com/title/tt2639254/', 'why': "Box Office Mojo \"Domestic Distributor: Focus World\" (https://www.boxofficemojo.com/title/tt2639254/)"},
        {'type': 'movie', 'tmdb': 27429, 'title': "Shivers", 'year': 1975, 'source': 'https://en.wikipedia.org/wiki/Shivers_(1975_film)', 'why': "Wikipedia (https://en.wikipedia.org/wiki/Shivers_(1975_film)): \"Cronenberg blaming American International Pictures's poor distribution\" (US release as They Came from Within); Box Office Mojo has no distributor"},
        {'type': 'movie', 'tmdb': 11838, 'title': "Ju-On: The Grudge", 'year': 2002, 'source': 'https://www.boxofficemojo.com/title/tt0364385/', 'why': "Box Office Mojo \"Domestic Distributor: Vitagraph Films\" (https://www.boxofficemojo.com/title/tt0364385/)"},
        {'type': 'movie', 'tmdb': 300155, 'title': "A Royal Night Out", 'year': 2015, 'source': 'https://www.boxofficemojo.com/title/tt1837562/', 'why': "Box Office Mojo \"Domestic Distributor: Atlas Distribution Company\" (https://www.boxofficemojo.com/title/tt1837562/)"},
        {'type': 'movie', 'tmdb': 205588, 'title': "Our Kind of Traitor", 'year': 2016, 'source': 'https://www.boxofficemojo.com/title/tt1995390/', 'why': "Box Office Mojo \"Domestic Distributor: Roadside Attractions\" (https://www.boxofficemojo.com/title/tt1995390/)"},
        {'type': 'movie', 'tmdb': 391714, 'title': "On Chesil Beach", 'year': 2018, 'source': 'https://www.boxofficemojo.com/title/tt1667321/', 'why': "Box Office Mojo \"Domestic Distributor: Bleecker Street Media\" (https://www.boxofficemojo.com/title/tt1667321/)"},
        {'type': 'movie', 'tmdb': 438259, 'title': "Journey's End", 'year': 2017, 'source': 'https://www.boxofficemojo.com/title/tt3780500/', 'why': "Box Office Mojo \"Domestic Distributor: Good Deed Entertainment\" (https://www.boxofficemojo.com/title/tt3780500/)"},
        {'type': 'movie', 'tmdb': 429417, 'title': "Ghost Stories", 'year': 2018, 'source': 'https://www.boxofficemojo.com/title/tt5516328/', 'why': "Box Office Mojo \"Domestic Distributor: IFC Films\" (https://www.boxofficemojo.com/title/tt5516328/)"},
        {'type': 'movie', 'tmdb': 200727, 'title': "Love, Rosie", 'year': 2014, 'source': 'https://www.boxofficemojo.com/title/tt1638002/', 'why': "Box Office Mojo \"Domestic Distributor: The Film Arcade\" (https://www.boxofficemojo.com/title/tt1638002/)"},
    ],
    'studio-focus-features': [
        {'type': 'movie', 'tmdb': 26428, 'title': "Agora", 'year': 2009, 'source': 'https://www.boxofficemojo.com/title/tt1186830/', 'why': "Box Office Mojo \"Domestic Distributor: Newmarket Films\" (https://www.boxofficemojo.com/title/tt1186830/)"},
        {'type': 'movie', 'tmdb': 8338, 'title': "Blindness", 'year': 2008, 'source': 'https://www.boxofficemojo.com/title/tt0861689/', 'why': "Box Office Mojo \"Domestic Distributor: Miramax\" (https://www.boxofficemojo.com/title/tt0861689/)"},
        {'type': 'movie', 'tmdb': 43079, 'title': "Mad About Mambo", 'year': 2000, 'source': 'https://www.boxofficemojo.com/title/tt0156757/', 'why': "Box Office Mojo \"Domestic Distributor: USA Films\" (https://www.boxofficemojo.com/title/tt0156757/)"},
        {'type': 'movie', 'tmdb': 121830, 'title': "Mr. Pip", 'year': 2012, 'source': 'https://www.boxofficemojo.com/title/tt1485749/', 'why': "Box Office Mojo \"Domestic Distributor: Freestyle Releasing\" (https://www.boxofficemojo.com/title/tt1485749/)"},
    ],
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
        qid = r['f']['value'].rsplit('/', 1)[-1]
        out.append({'type': 'movie', 'tmdb': int(tid), 'title': r['title']['value'], 'year': int(r['released']['value'][:4]),
                    'source': WIKIDATA + qid, 'qid': qid, '_links': int(r['sitelinks']['value'])})
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
        gone = {e['tmdb'] for e in EXCLUDE.get(slug, [])}
        rows = [r for r in rows if r['tmdb'] not in gone]
        total = len(rows)
        result[slug] = [{k: v for k, v in r.items() if k != '_links'} for r in rows[:CAP]]
        print(f'{slug}: {total} films' + (f' (capped to {CAP})' if total > CAP else ''))
        time.sleep(1)
    OUT.write_text(json.dumps(result, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
