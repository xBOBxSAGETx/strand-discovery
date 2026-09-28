"""Write generator/spec.json: every catalog of the Strand Discovery build, in folder order.

  python make_spec.py <curated_people_v2.csv>

One spec entry = one catalog = one AIOStreams Jellyfin library. `folders` lists the Strand folders (shelves) whose
cards point at it; a person listed in two categories is one catalog with two cards. `library` is the library name
(unique across the whole server), `title` is the card title shown in Strand (hidden, the art carries it).
"""
import csv, json, re, sys, unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ---- folders (order = order in Strand) ---------------------------------------------------------------------------
VARIANTS = [('popular', 'Popular'), ('new', 'New'), ('top', 'Top Rated')]
DIRECTOR_CATS = ['Up & Coming', 'Modern Masters', 'Legends', 'Action', 'Animation', 'Comedy', 'Horror', 'Sci-Fi']
ACTOR_CATS = ['Rising Stars', 'Scene Stealers', 'Legends', 'Action', 'Comedy', 'Drama', 'Horror']
FOLDERS = ([{'key': f'streaming-{v}', 'title': f'Streaming · {n}', 'shape': 'wide'} for v, n in VARIANTS]
           + [{'key': f'genres-{v}', 'title': f'Genres · {n}', 'shape': 'wide'} for v, n in VARIANTS]
           + [{'key': k, 'title': t, 'shape': 'wide'} for k, t in [
               ('networks', 'Networks'), ('studios', 'Studios'), ('series', 'Movie Series'), ('themes', 'Themes'),
               ('decades', 'Decades'), ('awards', 'Awards'), ('acclaimed', 'Acclaimed & Hidden Gems'),
               ('standup', 'Stand-up')]])


def slugify(s):
    # accents -> ASCII first (é->e, ó->o, ñ->n): slugs become catalog ids and Strand library ids, so they must not
    # lose letters ("Chloé Zhao" was "chlo-zhao")
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    s = s.lower().replace('&', 'and').replace('+', 'plus')
    s = re.sub(r"['’.]", '', s)
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


FOLDERS += [{'key': f'directors-{slugify(c)}', 'title': f'Directors · {c}', 'shape': 'poster'} for c in DIRECTOR_CATS]
FOLDERS += [{'key': f'actors-{slugify(c)}', 'title': f'Actors · {c}', 'shape': 'poster'} for c in ACTOR_CATS]

# ---- sources -----------------------------------------------------------------------------------------------------
# subscription services: flatrate only; ad-supported / library services: free|ads. Rent/buy never counts as "on it".
FREE_SERVICES = {'Tubi', 'Pluto TV', 'Plex'}
# Plex uses one provider id for its free library and its rental store. The owner keeps rentals on the Plex cards
# (playback is via debrid, so "rent" still means watchable) - explicit here, the single source for build + first_seen.
MONETIZATION = {'Plex': 'free|ads|rent'}
# dropped by the user 2026-09-28: Crunchyroll, Rakuten Viki, The Roku Channel, Kanopy, Hoopla
STREAMING = [('Netflix', '8'), ('Disney+', '337'), ('HBO Max', '1899'), ('Apple TV', '350'), ('Paramount+', '2616|2303'),
             ('Prime Video', '9'), ('Hulu', '15'), ('Peacock', '386|387'), ('Starz', '43'), ('Discovery+', '520'),
             ('Netflix Kids', '175'), ('MUBI', '11'), ('Criterion Channel', '258'), ('MagellanTV', '551'),
             ('Shudder', '99'), ('AMC+', '526'), ('BritBox', '151'), ('Tubi', '73'), ('Pluto TV', '300'), ('Plex', '538'),
             ('Acorn TV', '87')]
NETWORKS = [('HBO', 49), ('AMC', 174), ('FX', 88), ('BBC One', 4), ('Apple TV', 2552), ('Netflix', 213),
            ('Showtime', 67), ('Adult Swim', 80), ('Comedy Central', 47), ('Cartoon Network', 56), ('Nickelodeon', 13),
            ('Disney Channel', 54), ('History', 65), ('Discovery', 64), ('National Geographic', 43), ('A&E', 129),
            ('Hulu', 453), ('Prime Video', 1024), ('Peacock', 3353), ('HBO Max', 3186), ('Paramount+', 4330),
            ('PBS', 14), ('MTV', 33), ('Starz', 318), ('The CW', 71)]
STUDIOS = [('Pixar', '3'), ('Walt Disney Animation', '6125'), ('DreamWorks Animation', '521'), ('A24', '41077'),
           ('Blumhouse', '3172'), ('Studio Ghibli', '10342'), ('Illumination', '6704'), ('Warner Bros. Pictures', '174'),
           ('Universal', '33'), ('NEON', '90733'), ('Legendary', '923'), ('Lionsgate', '1632'), ('Focus Features', '10146'),
           ('Searchlight', '127929|43'), ('LAIKA', '11537'), ('Aardman', '297'), ('New Line', '12'), ('Amblin', '56')]
# (card, movie params, series params) — None = medium not built
GENRES = [('Action', {'with_genres': '28'}, {'with_genres': '10759'}),
          ('Adventure', {'with_genres': '12'}, None),
          ('Animation', {'with_genres': '16'}, {'with_genres': '16'}),
          ('Anime', {'with_keywords': '210024'}, {'with_keywords': '210024'}),
          ('Comedy', {'with_genres': '35'}, {'with_genres': '35'}),
          ('Crime', {'with_genres': '80'}, {'with_genres': '80'}),
          ('Documentary', {'with_genres': '99'}, {'with_genres': '99'}),
          ('Drama', {'with_genres': '18'}, {'with_genres': '18'}),
          ('Family', {'with_genres': '10751'}, {'with_genres': '10751'}),
          ('Fantasy', {'with_genres': '14'}, None),
          ('History', {'with_genres': '36'}, None),
          ('Horror', {'with_genres': '27'}, None),
          ('Kids', None, {'with_genres': '10762'}),
          ('Music', {'with_genres': '10402'}, None),
          ('Mystery', {'with_genres': '9648'}, {'with_genres': '9648'}),
          ('Reality', None, {'with_genres': '10764'}),
          ('Romance', {'with_genres': '10749'}, None),
          ('Sci-Fi', {'with_genres': '878'}, {'with_genres': '10765'}),
          ('Thriller', {'with_genres': '53'}, None),
          ('War', {'with_genres': '10752'}, {'with_genres': '10768'}),
          ('Westerns', {'with_genres': '37'}, {'with_genres': '37'})]
THEMES = [('Superheroes', '9715'), ('Zombies', '12377'), ('Space Epics', '161176'), ('Spies', '470'), ('Aliens', '9951'),
          ('Robots & AI', '310'), ('Dystopian', '4565'), ('Disaster', '10617'), ('Creature Features', '158126|161791|11100|33696|158252|7035|321335|158108'),
          ('Psychological', '12565'), ('Myths & Legends', '2035'), ('Heist', '10051'), ('Time Travel', '4379')]
# Movie Series, A–Z. collection ids / keyword / explicit TMDB ids (for franchises TMDB doesn't group).
SERIES = {
    '007': {'collections': [645]},
    'Alien': {'collections': [8091]},
    'Avatar': {'collections': [87096]},
    'Back to the Future': {'collections': [264]},
    'Bad Boys': {'collections': [14890]},
    'Cars': {'collections': [87118]},
    'Creed': {'collections': [553717]},
    'DC Universe': {'keywords': '229266|312528'},
    'Despicable Me': {'collections': [86066]},
    'Die Hard': {'collections': [1570]},
    'Dune': {'collections': [726871]},
    'Fast & Furious': {'collections': [9485]},
    'Frozen': {'collections': [386382]},
    'Ghostbusters': {'collections': [2980]},
    'Halloween': {'collections': [91361]},
    'Harry Potter': {'collections': [1241, 435259]},
    'How to Train Your Dragon': {'collections': [89137, 1458864]},
    'Ice Age': {'collections': [8354]},
    'Indiana Jones': {'collections': [84]},
    'Jason Bourne': {'collections': [31562]},
    'John Wick': {'collections': [404609]},
    'Jurassic Park': {'collections': [328]},
    'Kill Bill': {'collections': [2883]},
    'Knives Out': {'collections': [722971]},
    'Kung Fu Panda': {'collections': [77816]},
    'Lethal Weapon': {'collections': [945]},
    'Lord of the Rings': {'collections': [119, 121938]},
    'Mad Max': {'collections': [8945]},
    'Madagascar': {'collections': [14740]},
    'Marvel Cinematic Universe': {'keywords': '180547'},
    'Maze Runner': {'collections': [295130]},
    'Men in Black': {'collections': [86055]},
    'Mission: Impossible': {'collections': [87359]},
    'MonsterVerse': {'keywords': '380322'},
    "Ocean's": {'collections': [304]},
    'Pirates of the Caribbean': {'collections': [295]},
    'Planet of the Apes': {'collections': [1709, 173710]},
    'Predator': {'collections': [399]},
    'Rocky': {'collections': [1575]},
    'Rush Hour': {'collections': [90863]},
    'Saw': {'collections': [656]},
    'Scary Movie': {'collections': [4246]},
    'Scream': {'collections': [2602]},
    'Shrek': {'collections': [2150]},
    'Spider-Man': {'collections': [556, 125574, 531241, 573436]},
    'Star Wars': {'collections': [10], 'ids': [
        ['movie', 330459], ['movie', 348350],                                    # Rogue One, Solo
        ['series', 82856], ['series', 83867], ['series', 114461], ['series', 4194], ['series', 115036],
        ['series', 92830], ['series', 105971], ['series', 60554], ['series', 202879], ['series', 114479]]},
    'Terminator': {'collections': [528]},
    'The Conjuring': {'collections': [313086]},
    'The Dark Knight': {'collections': [263]},
    'The Godfather': {'collections': [230]},
    'The Hangover': {'collections': [86119]},
    'The Hunger Games': {'collections': [131635, 1701563]},
    'The Matrix': {'collections': [2344]},
    'Toy Story': {'collections': [10194]},
    'Transformers': {'collections': [8650]},
    'Twilight': {'collections': [33514]},
    'X-Men': {'collections': [748]},
}
# (card name, source) in folder order. Source = an MDBList list, or an awards.json key (built from the Wikipedia winner
# tables by awards_wiki.py; build.py reads the ids at build time, so the yearly refresh only has to update awards.json).
# Palme d'Or and Golden Globe moved from MDBList to Wikipedia 2026-09-28 (MDBList had a wrong film on each).
OSCAR_NOT_BP = [['movie', 631], ['movie', 3061], ['movie', 44657]]   # 1927/28 "Unique and Artistic Production" (Sunrise,
#                                    The Crowd, Chang): a separate award, not Best Picture - the Academy starts with Wings
AWARDS = [('Oscar Best Picture', {'list': 'apg2886/oscar-best-picture-winners', 'exclude': OSCAR_NOT_BP}),
          ('Oscar Best Picture Nominees', {'list': 'gatornylon/oscar-best-picture-nominated', 'exclude': OSCAR_NOT_BP}),
          ("Palme d'Or", {'awards_key': 'palme-dor'}),
          ('BAFTA Best Film', {'list': 'imkaptain/bafta-best-film-all-time'}),
          ('Golden Globe Best Picture', {'awards_key': 'golden-globe-best-picture'}),
          ('Emmy · Best Series Winners', {'awards_key': 'emmy-best-series', 'library': 'Emmy Best Series Winners'}),
          ('Sundance · Grand Jury Prize', {'awards_key': 'sundance-gjp', 'library': 'Sundance Grand Jury Prize'})]

VOTES = {'popular': 10, 'new': 5, 'top': {'movie': 300, 'series': 150}}
TOP_MIN_AGE = 180            # days: early-rating inflation put this month's releases on top of every Top Rated card
NO_TOP = {'MagellanTV', 'Netflix Kids', 'Discovery+', 'Acorn TV'}   # Top Rated had 1-15 items (run 1)


def disc(slug, library, title, eyebrow, folder, media, sort, movie=None, series=None, min_votes=None, depth=None):
    e = {'slug': slug, 'library': library, 'title': title, 'eyebrow': eyebrow, 'folders': [folder], 'kind': 'discover',
         'media': media, 'sort': sort, 'min_votes': VOTES.get(sort, 10) if min_votes is None else min_votes}
    if movie is not None:
        e['movie'] = movie
    if series is not None:
        e['series'] = series
    if depth:
        e['depth'] = depth
    if sort == 'top':
        e['min_age_days'] = TOP_MIN_AGE
    return e


def main():
    cats = []
    for v, n in VARIANTS:                                    # streaming x3
        for name, ids in STREAMING:
            if v == 'top' and name in NO_TOP:
                continue
            p = {'watch_region': 'US', 'with_watch_providers': ids,
                 'with_watch_monetization_types': MONETIZATION.get(name, 'free|ads' if name in FREE_SERVICES else 'flatrate')}
            eyebrow = {'popular': 'Streaming', 'new': 'New on Streaming', 'top': 'Top Rated · Streaming'}[v]
            cats.append(disc(f'streaming-{slugify(name)}' + ('' if v == 'popular' else f'-{v}'), f'{name} · {n}', name,
                             eyebrow, f'streaming-{v}', ['movie', 'series'], v, movie=p, series=p))
            if v == 'new':
                # TV "New" = episodes aired in the last 45 days (new seasons count), until the provider's first_seen
                # history is long enough; then build.py orders the whole card by date first seen on the service
                cats[-1]['tv_air_window'] = 45
                cats[-1]['provider'] = slugify(name)
    for v, n in VARIANTS:                                    # genres x3
        for name, mp, sp in GENRES:
            media = [m for m, p in (('movie', mp), ('series', sp)) if p is not None]
            eyebrow = {'popular': 'Genre', 'new': 'New · Genre', 'top': 'Top Rated · Genre'}[v]
            # Popular: TV needs >= 50 votes (regional long-runners with a handful of votes led Comedy/Drama/Family)
            votes = {'movie': 10, 'series': 50} if v == 'popular' else None
            cats.append(disc(f'genre-{slugify(name)}' + ('' if v == 'popular' else f'-{v}'), f'{name} · {n}', name,
                             eyebrow, f'genres-{v}', media, v, movie=mp, series=sp, min_votes=votes))
    for name, nid in NETWORKS:
        # defining shows first: popularity put The Scandal / WWE Raw / Sesame Street on top of Network · Netflix
        cats.append(disc(f'network-{slugify(name)}', f'Network · {name}', name, 'Network', 'networks', ['series'],
                         'votes', series={'with_networks': str(nid)}, min_votes=10))
        cats[-1]['network_id'] = nid
    for name, cid in STUDIOS:
        cats.append(disc(f'studio-{slugify(name)}', f'Studio · {name}', name, 'Studio', 'studios', ['movie'], 'popular',
                         movie={'with_companies': cid}))
    for name, spec in sorted(SERIES.items(), key=lambda kv: re.sub(r'^the ', '', kv[0].lower())):
        e = {'slug': f'series-{slugify(name)}', 'library': f'Series · {name}', 'title': name, 'eyebrow': 'Movie Series',
             'folders': ['series']}
        if 'keywords' in spec:
            e.update(kind='discover', media=['movie', 'series'], sort='release_asc', min_votes=0,
                     movie={'with_keywords': spec['keywords']}, series={'with_keywords': spec['keywords']})
        else:
            e.update(kind='franchise', media=['movie', 'series'] if spec.get('ids') else ['movie'],
                     collection_ids=spec['collections'], ids=spec.get('ids', []))
        cats.append(e)
    for name, kw in THEMES:
        p = {'with_keywords': kw}
        mp, sp = p, p
        if name == 'Creature Features':
            # creature feature, kaiju, giant monster, sea monster, giant animal, giant insect, killer animal, killer
            # shark - minus superhero (9715: Superman / Suicide Squad led) and, on TV, animation (Yu-Gi-Oh!, Powerpuff)
            mp = {**p, 'without_keywords': '9715'}
            sp = {**p, 'without_keywords': '9715', 'without_genres': '10767|10763|16'}
        cats.append(disc(f'theme-{slugify(name)}', f'Theme · {name}', name, 'Theme', 'themes', ['movie', 'series'],
                         'popular', movie=mp, series=sp))
    for d in range(1960, 2030, 10):
        mp = {'primary_release_date.gte': f'{d}-01-01'}
        sp = {'first_air_date.gte': f'{d}-01-01'}
        if d < 2020:
            mp['primary_release_date.lte'] = f'{d + 9}-12-31'
            sp['first_air_date.lte'] = f'{d + 9}-12-31'
        # defining titles per decade (vote count); popularity led the 1960s with "The Ape Woman"
        cats.append(disc(f'decade-{d}s', f'Decade · {d}s', f'{d}s', 'Decade', 'decades', ['movie', 'series'], 'votes',
                         movie=mp, series=sp, min_votes={'movie': 10, 'series': 50}))
    awards = json.loads((HERE / 'awards.json').read_text(encoding='utf-8'))
    for name, src in AWARDS:
        card = {'library': f"Award · {src.get('library', name)}", 'title': name, 'eyebrow': 'Awards', 'folders': ['awards']}
        if 'list' in src:
            card.update(slug=f'awards-{slugify(name)}', kind='mdblist', media=['movie'], list=src['list'], order='year_desc')
        else:
            key = src['awards_key']
            assert awards[key]['card'], f'{key} is a reference list, not a card'
            card.update(slug=f'awards-{key}', kind='idlist', media=sorted({m for m, _ in awards[key]['ids']}),
                        awards_key=key)
        if src.get('exclude'):
            card['exclude'] = src['exclude']
        cats.append(card)
    for name, media, avg, lo, hi in [('Acclaimed Movies', 'movie', 7.8, 2000, None),
                                     ('Acclaimed TV', 'series', 7.8, 800, None),
                                     ('Hidden Gem Movies', 'movie', 7.3, 150, 1500),
                                     ('Hidden Gem TV', 'series', 7.3, 80, 600)]:
        p = {'vote_average.gte': avg}
        if hi:
            p['vote_count.lte'] = hi
        acclaimed = name.startswith('Acclaimed')
        if not acclaimed and media == 'series':
            p['without_genres'] = '10767|10763|10764|10762'          # no talk/news/reality/kids in TV gems
        # Acclaimed: rating order is safe behind high vote floors. Gems: rating order surfaced novelty entries and
        # popularity order surfaced this month's releases (smoke test), so popular + at least a year old.
        e = disc(f'acclaimed-{slugify(name)}', f'{name}', name, 'Acclaimed' if acclaimed else 'Hidden Gems',
                 'acclaimed', [media], 'top' if acclaimed else 'popular', min_votes=lo, **{media: p})
        if not acclaimed:
            e['min_age_days'] = 365
        cats.append(e)
    for v, n in VARIANTS:
        p = {'with_keywords': '9716'}
        mp = {**p, 'with_runtime.gte': 20}                    # half-hour specials count; clips don't
        sp = {**p, 'without_genres': '10767|10763|10764'}     # no talent/reality shows ("India's Got Latent")
        cats.append(disc('standup' + ('' if v == 'popular' else f'-{v}'), f'Stand-up · {n}', f'Stand-up · {n}',
                         {'popular': 'Stand-up', 'new': 'New · Stand-up', 'top': 'Top Rated · Stand-up'}[v],
                         'standup', ['movie', 'series'], v, movie=mp, series=sp,
                         min_votes={'popular': 10, 'new': 3, 'top': {'movie': 50, 'series': 20}}[v]))

    # people: one catalog per person+role; folder list in CSV rank order
    people = {}
    for r in csv.DictReader(open(sys.argv[1], encoding='utf-8')):
        if not r['rank'].isdigit():                          # 'alt-N' rows are unused alternates
            continue
        role, cat = [s.strip() for s in r['category'].split(':', 1)]
        key = (role, int(r['tmdb_id']))
        folder = f"{'directors' if role == 'Director' else 'actors'}-{slugify(cat)}"
        if key not in people:
            people[key] = {'slug': f"{'dir' if role == 'Director' else 'actor'}-{slugify(r['name'])}",
                           'library': f"{role} · {r['name']}", 'title': r['name'], 'eyebrow': role,
                           'folders': [], 'ranks': {}, 'kind': 'director' if role == 'Director' else 'actor',
                           'media': ['movie'] if role == 'Director' else ['movie', 'series'],
                           'person_id': int(r['tmdb_id'])}
        people[key]['folders'].append(folder)
        people[key]['ranks'][folder] = int(r['rank'])
    cats += list(people.values())

    # structural asserts
    keys = {f['key'] for f in FOLDERS}
    slugs = [c['slug'] for c in cats]
    libs = [c['library'] for c in cats]
    assert len(slugs) == len(set(slugs)), 'duplicate slug'
    bad = [s for s in slugs if not re.fullmatch(r'[a-z0-9-]+', s)]
    assert not bad, f'non-ASCII / invalid slugs (they become permanent library ids): {bad}'
    assert len(libs) == len(set(libs)), 'duplicate library name'
    for c in cats:
        assert set(c['folders']) <= keys, c
        assert 'collection' not in f"sd-{c['slug']} {c['library']}".lower(), c['slug']
    cards = sum(len(c['folders']) for c in cats)
    spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
    spec['defaults'] = {'page_size': 100, 'depth': {'popular': 1000, 'new': 500, 'top': 500, 'release_asc': 1000,
                                                    'votes': 1000},
                        'region': 'US',
                        # every discover card, movie + tv. TMDB /search/keyword ids (2026-09-27): ecchi, etchi, hentai,
                        # hentai adaptation, softcore, gay softcore, sexploitation, sexsploitation, pantsu, animated porn
                        'without_keywords': '195669|285672|198385|384581|155477|382870|10053|335048|281749|378816',
                        # manual deny-list for leaks the keywords miss: [medium, tmdb id, why]
                        # TV only: 'erotic' (256466). On movies it also removes Quills / Fair Play (run-1 measurement)
                        'without_keywords_tv': '256466',
                        'deny': [['series', 95897, 'Overflow (2020): ecchi anime not flagged adult'],
                                 ['series', 70998, 'Souryo to Majiwaru Shikiyoku no Yoru ni (2017): explicit']]}
    spec['folders'] = FOLDERS
    spec.pop('cards', None)
    spec['catalogs'] = cats
    (HERE / 'spec.json').write_text(json.dumps(spec, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    by_folder = {f['key']: 0 for f in FOLDERS}
    for c in cats:
        for f in c['folders']:
            by_folder[f] += 1
    print(f'catalogs={len(cats)} cards={cards} folders={len(FOLDERS)}')
    for f in FOLDERS:
        print(f"  {f['title']:<28} {f['shape']:<6} {by_folder[f['key']]}")


if __name__ == '__main__':
    main()
