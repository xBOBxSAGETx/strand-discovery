"""Yearly double-source check of the award cards: each card's list vs an independent second list (markdown on stdout).

  python awards_crosscheck.py [awards_resolve.csv]   (after awards_wiki.py; the CSV only supplies titles)

MDBList cards (Oscar winners / nominees, BAFTA) are compared with our Wikipedia reference lists (awards.json,
REFERENCE_ONLY); our Wikipedia cards (Palme d'Or, Golden Globe) with the MDBList lists they replaced. The card's own
exclusions are applied first, so a deliberate exclusion is not reported. Emmy and Sundance have no public second list
(the MDBList ones need an API key), so they are listed as single-source. No TMDB requests.
"""
import csv, json, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAIRS = {  # card slug -> (second source kind, value)
    'awards-oscar-best-picture': ('wiki', 'oscar-winners'),
    'awards-oscar-best-picture-nominees': ('wiki', 'oscar-nominees'),
    'awards-bafta-best-film': ('wiki', 'bafta-best-film'),
    'awards-palme-dor': ('mdblist', 'sheeper/cannes-palme-dor-winners'),
    'awards-golden-globe-best-picture': ('mdblist', 'oldmankestis/golden-globe-best-picture-winners'),
}
SHOW = 15            # example rows per direction


def mdblist(slug):
    with urllib.request.urlopen(urllib.request.Request(f'https://mdblist.com/lists/{slug}/json',
                                                       headers={'User-Agent': 'strand-discovery'}), timeout=60) as r:
        return {it['id']: f"{it.get('title')} ({it.get('release_year')})" for it in json.load(r)
                if it.get('mediatype') == 'movie' and it.get('id')}


def sources(c, awards):
    """(card ids->title, card source label, second ids->title, second source label)."""
    if c['kind'] == 'mdblist':
        card, card_src = mdblist(c['list']), f"MDBList `{c['list']}`"
    else:
        card, card_src = {i: '' for m, i in awards[c['awards_key']]['ids'] if m == 'movie'}, f"Wikipedia (`{c['awards_key']}`)"
    kind, val = PAIRS[c['slug']]
    if kind == 'wiki':
        second, second_src = {i: '' for m, i in awards[val]['ids'] if m == 'movie'}, f'Wikipedia (`{val}`)'
    else:
        second, second_src = mdblist(val), f'MDBList `{val}`'
    return card, card_src, second, second_src


def main():
    awards = json.loads((HERE / 'awards.json').read_text(encoding='utf-8'))
    titles = {}
    if len(sys.argv) > 1:
        for r in csv.DictReader(open(sys.argv[1], encoding='utf-8')):
            if r['tmdb_id'].startswith('movie:'):
                titles[int(r['tmdb_id'].split(':')[1])] = f"{r['tmdb_title']} ({r['tmdb_year']})"
    spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
    cards = [c for c in spec['catalogs'] if c['slug'].startswith('awards-')]
    print('Each award card compared with an independent second list (TMDB ids). Differences need a look: a real '
          'list error, a scope difference, or a resolver miss (see the awards refresh CSV).\n')
    print('| card | card source | second source | in both | only on card | only in second |')
    print('|---|---|---|---|---|---|')
    details = []
    for c in cards:
        if c['slug'] not in PAIRS:
            src = f"awards.json `{c['awards_key']}`" if 'awards_key' in c else f"MDBList `{c.get('list')}`"
            print(f"| {c['title']} | {src} | none public (single source) | - | - | - |")
            continue
        excl = {i for m, i in c.get('exclude', [])}
        try:
            card, card_src, second, second_src = sources(c, awards)
        except Exception as e:                     # MDBList down / format change: report, keep the yearly job going
            print(f"| {c['title']} | - | - | unavailable ({type(e).__name__}) | - | - |")
            continue
        card = {i: t for i, t in card.items() if i not in excl}
        second = {i: t for i, t in second.items() if i not in excl}
        only_card, only_second = sorted(set(card) - set(second)), sorted(set(second) - set(card))
        print(f"| {c['title']} | {card_src} | {second_src} | {len(set(card) & set(second))} | {len(only_card)} | "
              f"{len(only_second)} |")
        names = {**titles, **{i: t for i, t in {**second, **card}.items() if t}}
        for label, ids in (('only on the card', only_card), ('only in the second source', only_second)):
            if ids:
                details.append(f"**{c['title']}** - {label}: " + ', '.join(
                    f"[{names.get(i) or i}](https://www.themoviedb.org/movie/{i})" for i in ids[:SHOW])
                    + (f' (+{len(ids) - SHOW} more)' if len(ids) > SHOW else ''))
    if details:
        print('\n' + '\n\n'.join(details))
    else:
        print('\nNo differences.')


if __name__ == '__main__':
    sys.exit(main())
