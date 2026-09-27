"""Build exact award winner lists from Wikipedia and resolve them to TMDB ids -> generator/awards.json.

  python awards_wiki.py <resolve_report.csv>

Lists (winners only, newest win first, a title that won several times appears once at its latest win):
  emmy-best-series  Primetime Emmy: Outstanding Drama Series + Outstanding Comedy Series + Outstanding Limited or
                    Anthology Series (incl. its Miniseries-era names). Winner rows are the bold-italic entries of the
                    articles' tables; the year comes from the "(Nth) Primetime Emmy Awards" link (year = 1948 + N).
  sundance-gjp      Sundance Grand Jury Prize, U.S. Dramatic + U.S. Documentary ("second place" rows excluded;
                    the World Cinema / "International winners" section is NOT included).
Every title is resolved with TMDB search; the report lists each one with how it matched. Unresolved titles are
listed there and in awards.json["unresolved"], never silently dropped. Needs TMDB_API_KEY (never printed).
"""
import csv, json, re, sys, unicodedata, urllib.parse, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402  (throttled, key-safe tmdb())

HERE = Path(__file__).resolve().parent
WIKI = 'https://en.wikipedia.org/w/api.php?action=parse&prop=wikitext&format=json&redirects=1&page='
UA = {'User-Agent': 'strand-discovery award lists (personal, non-commercial)'}
LINK = r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]"


def wikitext(page):
    with urllib.request.urlopen(urllib.request.Request(WIKI + urllib.parse.quote(page), headers=UA), timeout=60) as r:
        return json.load(r)['parse']['wikitext']['*']


def display(target, label):
    return re.sub(r'\s*\((?:[^)]*\b(?:film|TV series|series|miniseries|serial|documentary)\b[^)]*)\)$', '',
                  (label or target).strip())


def emmy_winners(page):
    """(year, title) for every bold-italic table entry; year from the nearest preceding ceremony link."""
    w = wikitext(page)
    w = w[w.index('{|'):]
    out, year = [], None
    for row in w.split('\n|-'):                              # winner rows carry the #FAEB86 background
        for c in re.finditer(r"(\d+)(?:st|nd|rd|th) Primetime Emmy Awards", row):
            year = 1948 + int(c.group(1))
        if 'FAEB86' not in row or not year:
            continue
        t = re.search(r"'''''(?:" + LINK + r"|((?:(?!'')[^\[<{|])+))", row)   # linked or plain-text title
        if t:
            title = display(t.group(1), t.group(2)) if t.group(1) else t.group(3).strip()
            out.append((year, title, page, t.group(1)))
    return out


def sundance_dramatic():
    w = wikitext('Grand_Jury_Prize_Dramatic')
    out, year = [], None
    table = w[w.index('{|'):]
    table = table[:table.index('\n|}')]
    for row in table.split('\n|-')[1:]:
        first = row.lstrip('\n').split('||')[0]            # year cell: "| 1982" or "| [[2014 Sundance…|2014]]"
        y = re.search(r"((?:19|20)\d\d)(?:\]\])?\s*$", first) if "''" not in first else None
        if y:
            year = int(y.group(1))
        if 'second place' in row.lower() or not year:
            continue
        t = re.search(r"''" + LINK + "''", row)
        if t:
            out.append((year, display(t.group(1), t.group(2)), 'Grand_Jury_Prize_Dramatic', t.group(1)))
        else:
            t = re.search(r"''((?:(?!'')[^\[\]])+)''", row)
            if t:
                out.append((year, t.group(1).strip(), 'Grand_Jury_Prize_Dramatic', None))
    return out


def sundance_documentary():
    w = wikitext('Grand_Jury_Prize_Documentary')
    w = w.split('==International winners==')[0]              # U.S. only; World Cinema excluded on purpose
    out = []
    for line in w.splitlines():
        y = re.match(r"\*\s*(?:\[\[[^\]|]*\|)?((?:19|20)\d\d)", line)
        if not y:
            continue
        for t in re.finditer(r"''" + LINK + "''", line):
            out.append((int(y.group(1)), display(t.group(1), t.group(2)), 'Grand_Jury_Prize_Documentary', t.group(1)))
    return out


def norm(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', s.replace('&', 'and')).strip()


# Checked by hand against TMDB details (2026-09-27); applied before any search.
OVERRIDES = {
    # 1987 Outstanding Miniseries = the 3-part NBC miniseries (tv 77044, 1986), not the 1987 follow-up series 10756
    ('series', 'a year in the life', 1987): ('series', 77044),
}


def wiki_hint(target):
    """(article title without its disambiguation, year in the disambiguation) from a Wikipedia link target, e.g.
    'The Life and Adventures of Nicholas Nickleby (1982 film)' -> ('The Life and Adventures of …', 1982)."""
    if not target:
        return None, None
    m = re.match(r'^(.*?)\s*\(([^)]*)\)$', target.strip())
    if not m:
        return target.strip(), None
    y = re.search(r"\b(?:19|20)\d\d\b", m.group(2))
    return m.group(1).strip(), int(y.group(0)) if y else None


def _year(r):
    d = r.get('release_date') or r.get('first_air_date') or ''
    return int(d[:4]) if d[:4].isdigit() else None


def _search(title, media, lo, hi):
    """Search results released/first aired within [lo, hi]: (exact-title candidates, all candidates),
    each sorted by vote count, most votes first."""
    res = build.tmdb('/search/movie' if media == 'movie' else '/search/tv', query=title,
                     include_adult='false').get('results', [])
    inwin = sorted((r for r in res if _year(r) is not None and lo <= _year(r) <= hi),
                   key=lambda r: -(r.get('vote_count') or 0))
    return [r for r in inwin if norm(r.get('title') or r.get('name') or '') == norm(title)], inwin


def resolve(title, first_win, media, target=None):
    """(media, id, matched title, year, how) or None. Anchored on the title's EARLIEST win (a long-running
    winner must not match a newer show of the same name - "Modern Family" 2014 remake vs tv 1421):
      series (Emmy): first aired in [earliest win - 20, earliest win]; then the title without its sequel
        number / subtitle ("Prime Suspect 3" -> the show); then a TV movie released in [win - 2, win]
        (the Limited category has included TV movies: Game Change, Behind the Candelabra).
      movies (Sundance): released in [win - 2, win]; then [win - 2, win + 1] (flagged CHECK).
    Among exact-title candidates the one with the most votes wins; >1 candidate is flagged CHECK with all of
    them listed. A non-exact pick is always CHECK."""
    key = (media, norm(title), first_win)
    if key in OVERRIDES:
        m, i = OVERRIDES[key]
        d = build.tmdb(f"/{'movie' if m == 'movie' else 'tv'}/{i}")
        return m, i, d.get('title') or d.get('name'), _year(d), 'OVERRIDE (checked by hand)'
    article, hint_year = wiki_hint(target)
    # the linked article title (e.g. the full "The Life and Adventures of Nicholas Nickleby") is tried first
    queries = ([article] if article and norm(article) != norm(title) else []) + [title]
    found = [(q, _hinted(q, first_win, media, hint_year)) for q in queries]
    found = [(q, f) for q, f in found if f]
    exact = [(q, f) for q, f in found if not f[4].startswith('CHECK: no exact')]
    if not (exact or found):
        return None
    q, f = (exact or found)[0]                  # an exact match from any query beats a fuzzy fallback
    how = f[4]
    # "exact title" only when the wiki title itself equals the TMDB title; otherwise say which name matched
    if how.startswith('exact title') and norm(f[2] or '') != norm(title):
        via = f'Wikipedia article "{q}"' if q != title else 'a TMDB alternative title'
        how = how.replace('exact title', f'exact via {via}', 1)
    return f[:4] + (how,)


def _hinted(title, first_win, media, hint_year):
    if media == 'movie':
        tries = [(title, 'movie', first_win - 2, first_win, False), (title, 'movie', first_win - 2, first_win + 1, True)]
    else:
        short = re.sub(r'(:.*$|\s+\d+$)', '', title).strip()
        tries = [(title, 'series', first_win - 20, first_win, False)] + \
            ([(short, 'series', first_win - 20, first_win, False)] if short != title else []) + \
            [(title, 'movie', first_win - 2, first_win, False)]
    fallback = None
    for q, m, lo, hi, widened in tries:
        exact, inwin = _search(q, m, lo, hi)
        if exact:
            hinted = [r for r in exact if hint_year and _year(r) == hint_year]
            if hinted:                                  # the Wikipedia article's own year settles same-name titles
                best = hinted[0]
                return m, best['id'], best.get('title') or best.get('name'), _year(best),                     f'exact title + Wikipedia year {hint_year}' + (f' ({len(exact)} candidates)' if len(exact) > 1 else '')
            best = exact[0]
            how = 'exact title + year window' if (q, m) == (title, media) else f'exact via {m} "{q}"'
            if widened:
                how = f'CHECK: {how} only with release year {hi}'
            if len(exact) > 1:
                how = 'CHECK: several exact candidates ' + '; '.join(
                    f"{r['id']} {_year(r)} v{r.get('vote_count', 0)}" for r in exact) + f' -> picked {best["id"]}'
            return m, best['id'], best.get('title') or best.get('name'), _year(best), how
        if inwin and not fallback:
            best = inwin[0]
            fallback = (m, best['id'], best.get('title') or best.get('name'), _year(best),
                        f'CHECK: no exact title, most-voted {m} in window ({len(inwin)} candidates)')
    return fallback


def build_list(entries, media):
    wins = {}
    for year, title, src, target in entries:                # all wins per title: earliest anchors, latest orders
        k = norm(title)
        w = wins.setdefault(k, {'title': title, 'src': src, 'target': target, 'first': year, 'last': year})
        w['first'], w['last'] = min(w['first'], year), max(w['last'], year)
        if year >= w['last']:
            w['title'], w['src'], w['target'] = title, src, target or w['target']
    ids, rows, unresolved = [], [], []
    for w in sorted(wins.values(), key=lambda w: -w['last']):
        hit = resolve(w['title'], w['first'], media, w['target'])
        if hit:
            if [hit[0], hit[1]] not in ids:
                ids.append([hit[0], hit[1]])
            rows.append([w['last'], w['first'], w['title'], w['src'], f'{hit[0]}:{hit[1]}', hit[2], hit[3], hit[4]])
        else:
            unresolved.append(f"{w['title']} ({w['first']})")
            rows.append([w['last'], w['first'], w['title'], w['src'], '', '', '', 'UNRESOLVED'])
    return ids, rows, unresolved


def main():
    emmy = []
    for page in ('Primetime_Emmy_Award_for_Outstanding_Drama_Series',
                 'Primetime_Emmy_Award_for_Outstanding_Comedy_Series',
                 'Primetime_Emmy_Award_for_Outstanding_Limited_or_Anthology_Series'):
        emmy += emmy_winners(page)
    sundance = sundance_dramatic() + sundance_documentary()
    out, report = {}, []
    for key, entries, media in (('emmy-best-series', emmy, 'series'), ('sundance-gjp', sundance, 'movie')):
        ids, rows, unresolved = build_list(entries, media)
        out[key] = {'ids': ids, 'unresolved': unresolved, 'wiki_entries': len(entries)}
        report += [[key] + r for r in rows]
        print(f'{key}: {len(entries)} winner rows -> {len(rows)} unique titles -> {len(ids)} resolved, '
              f'{len(unresolved)} unresolved, {sum(1 for r in rows if r[-1].startswith("CHECK"))} to check')
    (HERE / 'awards.json').write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    with open(sys.argv[1], 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['list', 'latest_win', 'first_win', 'wiki_title', 'wiki_page', 'tmdb_id', 'tmdb_title', 'tmdb_year',
                    'match'])
        w.writerows(report)


if __name__ == '__main__':
    main()
