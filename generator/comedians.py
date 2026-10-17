"""Who gets a comedian card in the Stand-up row (issue #149): writes generator/comedians.json.

  TMDB_API_KEY=... python comedians.py

1. Every stand-up special TMDB knows (keyword 9716: the 1000 most-voted + the 400 most popular since 2022); its
   headliner = the cast member billed first who plays themself.
2. Each headliner's own specials by the card's rule (build.is_special: English, Comedy, plays themself, headlines it).
3. The greats: the GREATS comedians whose specials have the most audience votes (3+ specials, or 2 with one since 2023),
   plus LEGENDS (TMDB's stand-up tag misses most of their specials), best-known first.
4. Making a wave: headliners of the most popular English stand-up specials released since WAVE_SINCE, with 2+ specials,
   not already in, first special since NEWCOMER (no veterans: Jamie Foxx, Mike Epps); the WAVE whose specials since WAVE_SINCE have the most audience votes (the special's popularity
   today let in actors with one club show; person popularity let in TV stars).
5. Everyone: their card holds MIN_TITLES+ titles and their TMDB gender is not in SKIP_GENDER.
About 1,700 TMDB requests (~2 min). Re-run to refresh the list; the owner can cut or add names in CUT / ADD.
"""
import datetime as dt, json, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build  # noqa: E402  (its throttled, key-safe tmdb() and the specials rule)

STANDUP = 9716
GREATS, WAVE, WAVE_SINCE, RECENT, NEWCOMER = 70, 30, '2023-01-01', '2023-01-01', '2012-01-01'
# owner, AIO chat 2026-10-17 ~1:15 PM PT: "Richard Pryor, etc the fucking greats". TMDB ids (search 2026-10-17).
LEGENDS = {3198: 'Rodney Dangerfield', 130281: 'Sam Kinison', 7167: 'Don Rickles',
           59255: 'Mitch Hedberg', 543256: "Patrice O'Neal", 1897: 'Bernie Mac'}
CUT = {}          # tmdb id -> name: never a card (owner's cuts)
SKIP_GENDER = {1}  # the owner's choice (AIO chat 2026-10-17 ~2:13 PM PT): TMDB person gender values left out
ADD = {}          # tmdb id -> name: always a card, after the greats (owner's adds)


def headliner(mid, title=''):
    """(id, name) of the cast member billed first, when they play themself (or have a blank role and their name is in the title)."""
    c = next((c for c in build.tmdb(f'/movie/{mid}/credits').get('cast', []) if c.get('order') == 0), None)
    if not c:
        return None
    ch = c.get('character') or ''
    named = c['name'].split()[-1].casefold() in title.casefold()
    return (c['id'], c['name']) if build.PLAYS_SELF.search(ch) or (not ch.strip() and named) else None


def profile(person):
    pid, name = person
    sp = build.specials_of(build.tmdb(f'/person/{pid}/movie_credits').get('cast', []), name)
    return {'id': pid, 'name': name, 'specials': len(sp), 'newest': sp[0]['release_date'] if sp else '',
            'first': sp[-1]['release_date'] if sp else '',
            'recent': sum(c['release_date'] >= RECENT for c in sp), 'votes': sum(c.get('vote_count') or 0 for c in sp),
            'wave_votes': sum(c.get('vote_count') or 0 for c in sp if c['release_date'] >= WAVE_SINCE),
            'best': max(sp, key=lambda c: c.get('vote_count') or 0)['title'] if sp else ''}


MIN_TITLES = 3       # a card with fewer titles is left out (#149 Done-when)
_full = {}


def full(p):
    """Their card (build.comedian: specials, then acting) holds MIN_TITLES or more, and they are not skipped by gender."""
    if p['id'] not in _full and build.tmdb(f"/person/{p['id']}").get('gender') in SKIP_GENDER:
        _full[p['id']] = False
    if p['id'] not in _full:
        _full[p['id']] = len(build.comedian({'person_id': p['id'], 'title': p['name']}, {})) >= MIN_TITLES
    return _full[p['id']]


def pages(n, **q):
    return [m for p in range(1, n + 1) for m in build.tmdb('/discover/movie', with_keywords=STANDUP, page=p, **q).get('results', [])]


def main():
    pool = {m['id']: m for m in pages(50, sort_by='vote_count.desc')
            + pages(20, sort_by='popularity.desc', **{'primary_release_date.gte': '2022-01-01'})}
    wave_specials = pages(10, sort_by='popularity.desc', with_original_language='en', **{'primary_release_date.gte': WAVE_SINCE})
    titles = {m['id']: m.get('title') or '' for m in list(pool.values()) + wave_specials}
    ids = list(titles)
    with ThreadPoolExecutor(8) as ex:
        head_of = dict(zip(ids, ex.map(headliner, ids, [titles[i] for i in ids])))
        heads = dict(h for h in head_of.values() if h)
        people = {**heads, **LEGENDS, **ADD}
        prof = {p['id']: p for p in ex.map(profile, people.items())}
    wave_pool = {head_of[m['id']][0] for m in wave_specials if head_of[m['id']]}
    ok = [p for p in prof.values() if p['id'] not in CUT and (p['specials'] >= 3 or (p['specials'] >= 2 and p['recent']))]
    ok.sort(key=lambda p: (-p['votes'], p['name']))
    greats, short = [], []                                       # short = under MIN_TITLES or skipped by gender
    for p in ok:                                                 # the first GREATS that make a full card
        if len(greats) < GREATS:
            (greats if full(p) else short).append(p)
    have = {p['id'] for p in greats}
    for i in LEGENDS:
        if i not in have and i not in CUT:
            (greats if full(prof[i]) else short).append(prof[i])
    greats.sort(key=lambda p: (-p['votes'], p['name']))
    greats += [prof[i] for i in ADD if i not in {p['id'] for p in greats}]
    have = {p['id'] for p in greats} | set(ADD)
    wave = [p for p in sorted((prof[i] for i in wave_pool if i not in have and i not in CUT and prof[i]['specials'] >= 2
                               and prof[i]['first'] >= NEWCOMER), key=lambda p: (-p['wave_votes'], p['name']))
            if full(p)][:WAVE]
    short = [p['name'] for p in short]
    out = [dict(p, group='greats') for p in greats] + [dict(p, group='wave') for p in wave]
    (HERE / 'comedians.json').write_text(json.dumps(
        {'_doc': 'who gets a comedian card (issue #149), in Stand-up row order; written by comedians.py',
         'generated': dt.date.today().isoformat(), 'comedians': out}, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f"specials seen {len(pool)}, headliners {len(heads)}, greats {len(greats)}, wave {len(wave)}, "
          f"requests {build._calls[0]}; left out of the greats (under {MIN_TITLES} titles or by gender): {short}")


if __name__ == '__main__':
    main()
