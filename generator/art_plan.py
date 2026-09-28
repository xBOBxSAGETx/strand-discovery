"""Plan the artwork for every catalog: writes generator/art.json from spec.json (titles fetched live from TMDB).

  python art_plan.py

Background rules (per-slug overrides and the avoid list in art_overrides.json win):
  people          -> TMDB profile photo (poster)
  franchise       -> first collection's backdrop
  awards lists    -> the list's #1 (newest winner)
  Top Rated cards -> the card's own #1 (classics after the 180-day rule)
  New cards       -> the most-VOTED title of the card's pool released in the last 12 months (stable: no re-render
                     when "New" switches to date-first-seen order)
  every other browse card -> the most-VOTED title of the card's pool (iconic and stable; the art is static, so
                     today's trending title would be baked in forever)
  Always skipped: a backdrop already used by ANY other card (Popular / New / Top of one service must not look alike),
  titles in the avoid list, documentaries on non-documentary cards, and - on documentary cards - titles tagged
  true crime / murder / scandal / abuse (TASTE_KEYWORDS: real people in crimes/scandals make poor card art).
Logos: streaming provider icon, network wordmark, studio wordmark; overrides may swap them (e.g. Paramount+).
"""
import datetime as dt, json, os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402  (reuses its throttled, key-safe tmdb() and the durable cache)

HERE = Path(__file__).resolve().parent
LOGO_SOURCES = json.loads((HERE / 'logo_sources.json').read_text(encoding='utf-8'))     if (HERE / 'logo_sources.json').exists() else {}      # written by logo_sources.py
LOOKAHEAD = 40        # candidates tried per card before giving up on a unique, acceptable backdrop
DOCUMENTARY = 99
# TMDB TV genres that merge two film genres (Action & Adventure, Sci-Fi & Fantasy, War & Politics): cards using one
# take their background from the film half only, so "Sci-Fi" doesn't show the Iron Throne
COMBINED_TV_GENRES = {'10759', '10765', '10768'}
# TMDB /search/keyword ids (2026-09-28): true crime, murder, scandal, sex scandal, serial killer, sexual abuse,
# rape, sex crime, sexual assault
TASTE_KEYWORDS = {33722, 9826, 3692, 2943, 10714, 739, 570, 189409, 190327}
DOC_CARDS = {'genre-documentary', 'genre-documentary-new', 'genre-documentary-top', 'streaming-discoveryplus',
             'streaming-discoveryplus-new', 'streaming-discoveryplus-top', 'streaming-magellantv',
             'streaming-magellantv-new', 'network-history', 'network-discovery', 'network-national-geographic',
             'network-pbs', 'network-aande'}


def details(meta):
    """backdrop, genres, keyword ids of one title (durably cached)."""
    media, tid = meta['type'], meta['id'].split(':', 1)[1]
    key = f"artinfo:{media}:{tid}"
    if key not in build._stable:
        path = f"/{'movie' if media == 'movie' else 'tv'}/{tid}"
        d = build.tmdb(path, append_to_response='keywords')
        kw = d.get('keywords', {})
        build._stable[key] = {'backdrop': d.get('backdrop_path'), 'genres': [g['id'] for g in d.get('genres', [])],
                              'keywords': [k['id'] for k in kw.get('keywords', kw.get('results', []))],
                              'votes': d.get('vote_count') or 0}
    if 'votes' not in build._stable[key]:           # entries cached before votes were recorded
        del build._stable[key]
        return details(meta)
    return build._stable[key]


def pool(c, dflt):
    """Candidate titles for the background, in preference order."""
    if c['kind'] in ('mdblist', 'idlist'):
        return build.build_one(c, dflt)[0][:LOOKAHEAD]
    if c['kind'] != 'discover':
        return []
    movies_only = COMBINED_TV_GENRES & set(str(c.get('series', {}).get('with_genres', '')).split('|'))
    if c['sort'] == 'top':
        got = build.build_one(dict(c, depth=LOOKAHEAD), dflt)[0]
        return [m for m in got if m['type'] == 'movie'] if movies_only else got
    v = dict(c, sort='votes', depth=LOOKAHEAD)
    v.pop('tv_air_window', None)
    v.pop('provider', None)                     # never the first_seen card: we want the pool, not arrivals
    if c['sort'] == 'new':
        since = (dt.date.today() - dt.timedelta(days=365)).isoformat()
        v['movie'] = {**c.get('movie', {}), 'primary_release_date.gte': since}
        v['series'] = {**c.get('series', {}), 'first_air_date.gte': since}
    if c['sort'] == 'release_asc':              # keyword franchises (MonsterVerse, MCU, DC)
        v['min_votes'] = 0
    media_list = ['movie'] if movies_only else c['media']
    got = build.interleave([build.discover(v, media, LOOKAHEAD, dflt) for media in media_list])
    if not got and c['sort'] == 'new':          # nothing from the last 12 months (small services): whole pool
        v['movie'], v['series'] = c.get('movie', {}), c.get('series', {})
        got = build.interleave([build.discover(v, media, LOOKAHEAD, dflt) for media in media_list])
    return got


WINDOW = 6            # candidates are vote-ordered per medium and interleaved; re-ranked by votes per window of 6


def main():
    spec = json.loads(Path(os.environ.get('SD_SPEC', HERE / 'spec.json')).read_text(encoding='utf-8'))
    dflt = spec['defaults']
    shapes = {f['key']: f['shape'] for f in spec['folders']}
    overrides = json.loads((HERE / 'art_overrides.json').read_text(encoding='utf-8')) \
        if (HERE / 'art_overrides.json').exists() else {}
    avoid = set(overrides.pop('_avoid', {}).get('titles', []))
    pinned = json.loads((HERE / 'art_pinned.json').read_text(encoding='utf-8'))['pinned']         if (HERE / 'art_pinned.json').exists() else {}
    # approved backgrounds stay as they are and are reserved first, so no other card can take them
    used = {b['path'] for b in pinned.values() if b.get('kind') == 'path'}
    cards, problems, skipped = [], [], {'documentary': 0, 'taste': 0, 'avoid': 0, 'duplicate': 0}
    try:
        for c in spec['catalogs']:
            shape = {shapes[f] for f in c['folders']}
            assert len(shape) == 1, f"{c['slug']}: folders with different shapes {c['folders']}"
            shape = shape.pop()
            assert (shape == 'poster') == (c['kind'] in ('director', 'actor')), f"{c['slug']}: people must be poster"
            card = {'slug': c['slug'], 'shape': shape, 'eyebrow': c['eyebrow'], 'title': c.get('art_title', c['title'])}
            logo_key = (f"streaming:{c['provider']}" if c.get('provider') else
                        f"streaming:{c['slug'][len('streaming-'):].rsplit('-top', 1)[0]}" if c['slug'].startswith('streaming-')
                        else c['slug'] if c['slug'].startswith(('network-', 'studio-')) else None)
            if logo_key and logo_key in LOGO_SOURCES:
                # service / network / studio cards: the brand's own logo, never a movie still (static art can't go stale)
                src = LOGO_SOURCES[logo_key]
                card.update(style='logo', logo_src={'kind': src['kind'], 'id': src['id']},
                            icon_provider=src.get('icon_provider'),
                            badge={'new': 'New', 'top': 'Top Rated'}.get(c.get('sort')),
                            eyebrow={'streaming': 'Streaming', 'network': 'Network', 'studio': 'Studio'}[
                                'streaming' if logo_key.startswith('streaming:') else c['slug'].split('-')[0]])
                base = re.sub(r'-(new|top)$', '', c['slug'])
                card.update({k: v for k, v in overrides.get(base, {}).items() if not k.startswith('_')})
                cards.append(card)
                continue
            if c['slug'].startswith('genre-') and c.get('sort') == 'new':
                card.update(style='type', eyebrow='Genre')          # typographic "NEW" card on a genre colour
                cards.append(card)
                continue
            if c['slug'] in pinned:
                card['bg'] = pinned[c['slug']]
            elif c['kind'] in ('director', 'actor'):
                card['bg'] = {'kind': 'person', 'id': c['person_id']}
            elif c['kind'] == 'franchise':
                card['bg'] = {'kind': 'collection', 'id': c['collection_ids'][0]}
            else:
                doc_card = c['slug'] in DOC_CARDS
                cands = pool(c, dflt)[:LOOKAHEAD * 2]
                if c['kind'] == 'discover' and c['sort'] != 'top':   # most-voted first, movies vs TV compared
                    cands = [m for i in range(0, len(cands), WINDOW)
                             for m in sorted(cands[i:i + WINDOW], key=lambda m: -details(m)['votes'])]
                pick = None
                for m in cands:
                    if f"{m['type']}:{m['id'].split(':', 1)[1]}" in avoid:
                        skipped['avoid'] += 1
                        continue
                    info = details(m)
                    if not info['backdrop']:
                        continue
                    if not doc_card and DOCUMENTARY in info['genres']:
                        skipped['documentary'] += 1
                        continue
                    if doc_card and TASTE_KEYWORDS & set(info['keywords']):
                        skipped['taste'] += 1
                        continue
                    if info['backdrop'] in used:
                        skipped['duplicate'] += 1
                        continue
                    pick = (m, info['backdrop'])
                    break
                if pick:
                    used.add(pick[1])
                    card['bg'] = {'kind': 'path', 'path': pick[1], 'from': f"{pick[0]['name']} ({pick[0]['id']})"}
                else:
                    problems.append(f"{c['slug']}: no acceptable unused backdrop among {len(cands)} candidates")
            p = c.get('movie', {}) if c['kind'] == 'discover' else {}
            q = c.get('series', {}) if c['kind'] == 'discover' else {}
            if 'with_watch_providers' in p:
                card['logo'] = {'kind': 'provider', 'id': int(p['with_watch_providers'].split('|')[0])}
            elif 'with_networks' in q:
                card['logo'] = {'kind': 'network', 'id': int(q['with_networks'])}
            elif 'with_companies' in p:
                card['logo'] = {'kind': 'company', 'id': int(p['with_companies'].split('|')[0])}
            base = re.sub(r'-(new|top)$', '', c['slug'])   # one override covers all three variants
            card.update(overrides.get(base, {}))
            card.update(overrides.get(c['slug'], {}))
            cards.append(card)
    finally:
        build.save_cache()
    (HERE / 'art.json').write_text(json.dumps({'_note': 'generated by art_plan.py; edit art_overrides.json instead',
                                               'cards': cards}, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f"art plan: {len(cards)} cards ({sum(c['shape'] == 'wide' for c in cards)} wide, "
          f"{sum(c['shape'] == 'poster' for c in cards)} poster), {build._calls[0]} TMDB requests, skipped {skipped}")
    for p in problems:
        print('PROBLEM', p)
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
