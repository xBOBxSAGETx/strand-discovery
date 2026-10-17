"""Write generator/spec.json: every catalog of the Strand Discovery build, in folder order.

  python make_spec.py <curated_people_v2.csv> <candidates_v2.json> <metrics_directors_v2.json> <metrics_actors_v2.json>

The private people files (never committed) order the people, most total votes of their films first; the run refuses without them.

One spec entry = one catalog = one AIOStreams Jellyfin library. `folders` lists the Strand folders (shelves) whose
cards point at it; a person listed in two categories is one catalog with two cards. `library` is the library name
(unique across the whole server), `title` is the card title shown in Strand (hidden, the art carries it).
"""
import csv, json, re, sys, unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build                    # the one loader of the hand lists

# ---- folders (order = order in Strand) ---------------------------------------------------------------------------
VARIANTS = [('popular', 'Popular'), ('new', 'New'), ('top', 'Top Rated')]
DIRECTOR_CATS = ['Up & Coming', 'Modern Masters', 'Legends', 'Action', 'Animation', 'Comedy', 'Horror', 'Sci-Fi']
ACTOR_CATS = ['Rising Stars', 'Scene Stealers', 'Legends', 'Action', 'Comedy', 'Drama', 'Horror']
FOLDERS = ([{'key': 'streaming', 'title': 'Streaming', 'shape': 'wide'}]
           + [{'key': f'genres-{v}', 'title': f'Genres · {n}', 'shape': 'wide'} for v, n in VARIANTS]
           + [{'key': k, 'title': t, 'shape': 'wide'} for k, t in [
               ('networks', 'Networks'), ('studios', 'Studios'), ('series', 'Movie Series'), ('themes', 'Themes'),
               ('decades', 'Decades'), ('awards', 'Awards'), ('acclaimed', 'Acclaimed & Hidden Gems'),
               ('standup', 'Stand-up')]])


KEEP_FOLDERS = {'streaming', 'networks', 'studios', 'series', 'themes', 'awards', 'acclaimed', 'standup'}


def keep_catalog(c):
    if c.get('person_id'):                      # every director and actor (they get one folder each, see below)
        return True
    return any(f in KEEP_FOLDERS for f in c['folders'])


def slugify(s):
    # accents -> ASCII first (é->e, ó->o, ñ->n): slugs become catalog ids and Strand library ids, so they must not
    # lose letters ("Chloé Zhao" was "chlo-zhao")
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    s = s.lower().replace('&', 'and').replace('+', 'plus')
    s = re.sub(r"['’.]", '', s)
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


# people: ONE folder for all directors and ONE for all actors (issue #142); the old per-category folders are gone
PEOPLE_FOLDERS = [{'key': 'directors', 'title': 'Directors', 'shape': 'poster'},
                  {'key': 'actors', 'title': 'Actors', 'shape': 'poster'}]
PEOPLE_KINDS = ('director', 'actor')          # the Directors / Actors rows, ordered by the private vote files
# folder order in Strand (issue #142); a folder with no cards left is dropped at the end
FOLDER_ORDER = ['streaming', 'networks', 'directors', 'actors', 'studios', 'series', 'themes',
                'awards', 'acclaimed', 'standup']

# ---- sources -----------------------------------------------------------------------------------------------------
# subscription services: flatrate only; ad-supported / library services: free|ads. Rent/buy never counts as "on it".
FREE_SERVICES = {'Tubi', 'Pluto TV', 'Plex'}
# Plex uses one provider id for its free library and its rental store. The owner keeps rentals on the Plex cards
# (playback is via debrid, so "rent" still means watchable) - explicit here, the single source for build + first_seen.
MONETIZATION = {'Plex': 'free|ads|rent'}
# dropped by the user 2026-09-28: Crunchyroll, Rakuten Viki, The Roku Channel, Kanopy, Hoopla
# channel provider ids (Amazon / Apple / Roku channels of the same service) added 2026-09-28 only where >= 60% of
# the top-40 titles they add are confirmed on another listing of the service (accuracy/providers/channel_ids.md)
STREAMING = [('Netflix', '8'), ('Disney+', '337'), ('HBO Max', '1899'), ('Apple TV', '350'), ('Paramount+', '2616|2303|582|1853'),
             ('Prime Video', '9'), ('Hulu', '15'), ('Peacock', '386|387'), ('Starz', '43|634|1794'), ('Discovery+', '520'),
             ('Netflix Kids', '175'), ('MUBI', '11'), ('Criterion Channel', '258'), ('MagellanTV', '551'),
             ('Shudder', '99|204|2049'), ('AMC+', '526|635|1854'), ('BritBox', '151'), ('Tubi', '73'), ('Pluto TV', '300'), ('Plex', '538'),
             ('Acorn TV', '87|196|2034')]
# A network id may be 'a|b|c': TMDB lists one network under several ids (HBO Max = 3186, 8304 and 6783 "Max"; PBS Kids 122 is
# PBS's kids block; 6631 is Paramount+ with Showtime). The FIRST id is the card's network_id (art, logo, accuracy audit).
NETWORKS = [('HBO', 49), ('AMC', 174), ('FX', 88), ('BBC One', 4), ('Apple TV', 2552), ('Netflix', 213),
            ('Showtime', '67|6631'), ('Adult Swim', 80), ('Comedy Central', 47), ('Cartoon Network', 56), ('Nickelodeon', 13),
            ('Disney Channel', 54), ('History', 65), ('Discovery', 64), ('National Geographic', 43), ('A&E', 129),
            ('Hulu', 453), ('Prime Video', 1024), ('Peacock', 3353), ('HBO Max', '3186|8304|6783'), ('Paramount+', 4330),
            ('PBS', '14|122'), ('MTV', 33), ('Starz', 318), ('The CW', 71)]
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
# Movie Series, A–Z. collection ids / keyword / explicit TMDB ids (for franchises TMDB doesn't group): spin(...).
SPIN_SOURCE = "spin-off TMDB doesn't group into the collection"


def spin(media, tmdb, title, year):
    return build.hand_entry(media, tmdb, title, year, f'{SPIN_SOURCE} (https://www.themoviedb.org/'
                            f"{'movie' if media == 'movie' else 'tv'}/{tmdb})")


SERIES = {
    '007': {'collections': [645]},
    'Alien': {'collections': [8091], 'ids': [
        spin('movie', 70981, 'Prometheus', 2012),
        spin('movie', 126889, 'Alien: Covenant', 2017),
        spin('movie', 945961, 'Alien: Romulus', 2024)]},   # spin-offs TMDB doesn't group: Prometheus, Covenant, Romulus
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
    'Ghostbusters': {'collections': [2980], 'ids': [spin('movie', 43074, 'Ghostbusters', 2016)]},   # spin-offs TMDB doesn't group: 2016
    'Halloween': {'collections': [91361], 'ids': [spin('movie', 2082, 'Halloween', 2007), spin('movie', 24150, 'Halloween II', 2009)]},   # spin-offs TMDB doesn't group: Rob Zombie 2007, Halloween II 2009
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
    'Lord of the Rings': {'collections': [119, 121938], 'ids': [spin('movie', 839033, 'The Lord of the Rings: The War of the Rohirrim', 2024)]},   # spin-offs TMDB doesn't group: The War of the Rohirrim
    'Mad Max': {'collections': [8945]},
    'Madagascar': {'collections': [14740], 'ids': [spin('movie', 270946, 'Penguins of Madagascar', 2014)]},   # spin-offs TMDB doesn't group: Penguins of Madagascar
    'Marvel Cinematic Universe': {'keywords': '180547'},
    'Maze Runner': {'collections': [295130]},
    'Men in Black': {'collections': [86055], 'ids': [spin('movie', 479455, 'Men in Black: International', 2019)]},   # spin-offs TMDB doesn't group: International
    'Mission: Impossible': {'collections': [87359]},
    'MonsterVerse': {'keywords': '380322'},
    "Ocean's": {'collections': [304], 'ids': [spin('movie', 402900, "Ocean's Eight", 2018)]},   # spin-offs TMDB doesn't group: Ocean's 8
    'Pirates of the Caribbean': {'collections': [295]},
    'Planet of the Apes': {'collections': [1709, 173710], 'ids': [spin('movie', 869, 'Planet of the Apes', 2001)]},   # spin-offs TMDB doesn't group: 2001
    'Predator': {'collections': [399], 'ids': [spin('movie', 1376434, 'Predator: Killer of Killers', 2025)]},   # spin-offs TMDB doesn't group: Killer of Killers
    'Rocky': {'collections': [1575]},
    'Rush Hour': {'collections': [90863]},
    'Saw': {'collections': [656]},
    'Scary Movie': {'collections': [4246]},
    'Scream': {'collections': [2602]},
    'Shrek': {'collections': [2150], 'ids': [spin('movie', 417859, 'Puss in Boots', 2011), spin('movie', 315162, 'Puss in Boots: The Last Wish', 2022)]},   # spin-offs TMDB doesn't group: Puss in Boots 2011, The Last Wish
    'Spider-Man': {'collections': [556, 125574, 531241, 573436]},
    'Star Wars': {'collections': [10], 'ids': [
        spin('movie', 330459, 'Rogue One: A Star Wars Story', 2016),
        spin('movie', 348350, 'Solo: A Star Wars Story', 2018),
        spin('series', 82856, 'The Mandalorian', 2019),
        spin('series', 83867, 'Andor', 2022),
        spin('series', 114461, 'Ahsoka', 2023),
        spin('series', 4194, 'Star Wars: The Clone Wars', 2008),
        spin('series', 115036, 'The Book of Boba Fett', 2021),
        spin('series', 92830, 'Obi-Wan Kenobi', 2022),
        spin('series', 105971, 'Star Wars: The Bad Batch', 2021),
        spin('series', 60554, 'Star Wars Rebels', 2014),
        spin('series', 202879, 'Star Wars: Skeleton Crew', 2024),
        spin('series', 114479, 'The Acolyte', 2024)]},
    'Terminator': {'collections': [528]},
    'The Conjuring': {'collections': [313086]},
    'The Dark Knight': {'collections': [263]},
    'The Godfather': {'collections': [230]},
    'The Hangover': {'collections': [86119]},
    'The Hunger Games': {'collections': [131635, 1701563]},
    'The Matrix': {'collections': [2344]},
    'Toy Story': {'collections': [10194], 'ids': [spin('movie', 718789, 'Lightyear', 2022)]},   # spin-offs TMDB doesn't group: Lightyear
    'Transformers': {'collections': [8650], 'ids': [
        spin('movie', 424783, 'Bumblebee', 2018),
        spin('movie', 667538, 'Transformers: Rise of the Beasts', 2023),
        spin('movie', 698687, 'Transformers One', 2024)]},   # spin-offs TMDB doesn't group: Bumblebee, Rise of the Beasts, Transformers One
    'Twilight': {'collections': [33514]},
    'X-Men': {'collections': [748], 'ids': [
        spin('movie', 76170, 'The Wolverine', 2013),
        spin('movie', 2080, 'X-Men Origins: Wolverine', 2009),
        spin('movie', 263115, 'Logan', 2017),
        spin('movie', 293660, 'Deadpool', 2016),
        spin('movie', 383498, 'Deadpool 2', 2018),
        spin('movie', 533535, 'Deadpool & Wolverine', 2024),
        spin('movie', 340102, 'The New Mutants', 2020)]},   # spin-offs TMDB doesn't group: The Wolverine, Origins, Logan, Deadpool 1-3, The New Mutants
}
# (card name, source) in folder order. Source = an MDBList list, or an awards.json key (built from the Wikipedia winner
# tables by awards_wiki.py; build.py reads the ids at build time, so the yearly refresh only has to update awards.json).
# Palme d'Or and Golden Globe moved from MDBList to Wikipedia 2026-09-28 (MDBList had a wrong film on each).
OSCAR_WHY = ('1927/28 "Unique and Artistic Production" (Sunrise, The Crowd, Chang): a separate award, not Best Picture; '
             'the Academy starts with Wings')
OSCAR_NOT_BP = [build.hand_entry('movie', i, t, y, f'{OSCAR_WHY} (https://www.themoviedb.org/movie/{i})')
                for i, t, y in [(631, 'Sunrise', 1927), (3061, 'The Crowd', 1928), (44657, 'Chang', 1927)]]
AWARDS = [('Oscar Best Picture', {'list': 'apg2886/oscar-best-picture-winners', 'exclude': OSCAR_NOT_BP}),
          ('Oscar Best Picture Nominees', {'list': 'gatornylon/oscar-best-picture-nominated', 'exclude': OSCAR_NOT_BP}),
          ("Palme d'Or", {'awards_key': 'palme-dor'}),
          ('BAFTA Best Film', {'list': 'imkaptain/bafta-best-film-all-time'}),
          ('Golden Globe Best Picture', {'awards_key': 'golden-globe-best-picture'}),
          ('Emmy · Best Series Winners', {'awards_key': 'emmy-best-series', 'library': 'Emmy Best Series Winners'}),
          ('Sundance · Grand Jury Prize', {'awards_key': 'sundance-gjp', 'library': 'Sundance Grand Jury Prize'})]

# Hidden gems = well rated, few viewers have heard of it (issue #53). A low TMDB vote count alone is not that: long-running
# network procedurals have few votes too. So: a stricter rating, a vote band (min via the card's min_votes), TV from 2000 on,
# no soaps/crime/documentary/talk/news/reality/kids, movies at least two years old (this year's blockbusters drop out).
# TMDB discover params: https://developer.themoviedb.org/reference/discover-tv and .../discover-movie
# TV genre ids: 80 crime, 99 documentary, 10762 kids, 10763 news, 10764 reality, 10766 soap, 10767 talk.
GEM_MIN_AGE_MOVIES = 730
GEM_TV_FIRST_AIR = '2000-01-01'
GEM_TV_NO_GENRES = '80|99|10762|10763|10764|10766|10767'

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


# issue #143: one card per streaming service / stand-up = the union of its Popular + New + Top Rated parts (build.py kind
# 'union'). Each part is the discover card it was before the cut, kept verbatim under its old slug (the Popular part under
# the card's own slug), so every hand list and correction keyed by those slugs still applies. A union holds at most
# UNION_CAP titles: the AIOStreams server reads only the first JELLYFIN_MAX_SORT_ITEMS=2000 for a sort (year, date, rating).
UNION_CAP = 2000
PART_KEYS = ('slug', 'sort', 'min_votes', 'min_age_days', 'tv_air_window', 'provider', 'depth')


def union_card(cards, folder, title=None):
    """cards = the discover cards (Popular first, then New, then Top Rated) -> ONE union catalog under the Popular slug.
    title: the card's own title when the Popular part's title names its variant (library name and id stay)."""
    base = cards[0]
    assert base['sort'] == 'popular'
    u = {k: base[k] for k in ('slug', 'library', 'title', 'eyebrow')}
    if title:
        u['title'] = title
    u.update(folders=[folder], kind='union', media=base['media'], sort='popular', min_votes=base['min_votes'], cap=UNION_CAP)
    for k in ('movie', 'series'):
        if k in base:
            u[k] = base[k]
    if any(c.get('provider') for c in cards):                  # the New part's service: arrival / first_seen order, watch triggers
        u['provider'] = next(c['provider'] for c in cards if c.get('provider'))
    u['parts'] = [{k: c[k] for k in PART_KEYS if k in c} for c in cards]
    return u


def load_votes(paths):
    """-> {(role, tmdb_id): total audience votes of their films} from the private people files, or {} without any.
    Two shapes are read: candidates_v2.json ({"directors": {cat: {"candidates"|"near_misses": [{tmdb_id, metrics}]}}, "actors": ...},
    only the shortlisted people) and metrics_{directors,actors}_v2.json ({id: {dept, total_votes | all.total_votes}},
    everyone). Only the numbers are used for ordering; nothing from the files is written to the spec."""
    votes = {}

    def put(role, tid, v):
        if v is not None:
            votes[(role, int(tid))] = max(v, votes.get((role, int(tid)), 0))
    for path in paths:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if 'directors' in data or 'actors' in data:
            for role, key in (('Director', 'directors'), ('Actor', 'actors')):
                for grp in data.get(key, {}).values():
                    for cand in grp.get('candidates', []) + grp.get('near_misses', []):
                        put(role, cand['tmdb_id'], (cand.get('metrics') or {}).get('total_votes'))
        else:
            name = Path(path).name.lower()          # metrics_directors_v2.json holds DIRECTING votes whatever the person's main job
            fixed = 'Director' if 'director' in name else 'Actor' if 'actor' in name else None
            for e in data.values():
                role = fixed or ('Director' if e.get('dept') == 'Directing' else 'Actor')
                put(role, e['tmdb_id'], e.get('total_votes', (e.get('all') or {}).get('total_votes')))
    return votes


def order_people(cats, votes):
    """Put every director in ONE 'directors' folder and every actor in ONE 'actors' folder, best-known first:
    most total votes, then lowest old rank, then name. ranks = 1..N. Non-people stay in place, people go last."""
    rest = [c for c in cats if c['kind'] not in PEOPLE_KINDS]          # comedians (#149) keep their Stand-up row order
    out = []
    for role, folder in (('Director', 'directors'), ('Actor', 'actors')):
        group = [c for c in cats if c['kind'] in PEOPLE_KINDS and c['eyebrow'] == role]
        group.sort(key=lambda c: (-votes.get((role, c['person_id']), -1), min(c['ranks'].values()), c['title']))
        for n, c in enumerate(group, 1):
            c['folders'] = [folder]
            c['ranks'] = {folder: n}
        out += group
    return rest + out


def main():
    if len(sys.argv) < 3:
        sys.exit('make_spec.py needs the private order files after the CSV (candidates_v2.json, metrics_directors_v2.json, '
                 'metrics_actors_v2.json): with only the CSV it would silently reshuffle the Directors and Actors rows. '
                 'Usage: python make_spec.py <curated_people_v2.csv> <candidates_v2.json> <metrics_directors_v2.json> <metrics_actors_v2.json>')
    cats = []
    for name, ids in STREAMING:                              # one union card per service (issue #143)
        p = {'watch_region': 'US', 'with_watch_providers': ids,
             'with_watch_monetization_types': MONETIZATION.get(name, 'free|ads' if name in FREE_SERVICES else 'flatrate')}
        parts = []
        for v, n in VARIANTS:
            if v == 'top' and name in NO_TOP:
                continue
            eyebrow = {'popular': 'Streaming', 'new': 'New on Streaming', 'top': 'Top Rated · Streaming'}[v]
            parts.append(disc(f'streaming-{slugify(name)}' + ('' if v == 'popular' else f'-{v}'), f'{name} · {n}', name,
                              eyebrow, 'streaming', ['movie', 'series'], v, movie=p, series=p))
            if v == 'new':
                # TV "New" = episodes aired in the last 45 days (new seasons count), until the provider's first_seen
                # history is long enough; then build.py orders the whole part by date first seen on the service
                parts[-1]['tv_air_window'] = 45
                parts[-1]['provider'] = slugify(name)
        cats.append(union_card(parts, 'streaming'))
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
        cats[-1]['network_id'] = int(str(nid).split('|')[0])
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
            e.update(kind='franchise', media=['movie', 'series'] if any(x['type'] == 'series' for x in spec.get('ids', [])) else ['movie'],
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
    awards = json.loads((HERE / 'awards.json').read_text(encoding='utf-8'))      # only the `card` flag is read from it here
    award_ids = build.load_hand_list('awards')
    for name, src in AWARDS:
        card = {'library': f"Award · {src.get('library', name)}", 'title': name, 'eyebrow': 'Awards', 'folders': ['awards']}
        if 'list' in src:
            card.update(slug=f'awards-{slugify(name)}', kind='mdblist', media=['movie'], list=src['list'], order='year_desc')
        else:
            key = src['awards_key']
            assert awards[key]['card'], f'{key} is a reference list, not a card'
            card.update(slug=f'awards-{key}', kind='idlist', media=sorted({e['type'] for e in award_ids[key]}),
                        awards_key=key)
        if src.get('exclude'):
            card['exclude'] = src['exclude']
        cats.append(card)
    for name, media, avg, lo, hi in [('Acclaimed Movies', 'movie', 7.8, 2000, None),
                                     ('Acclaimed TV', 'series', 7.8, 800, None),
                                     ('Hidden Gem Movies', 'movie', 7.5, 150, 1500),
                                     ('Hidden Gem TV', 'series', 7.5, 100, 600)]:
        p = {'vote_average.gte': avg}
        if hi:
            p['vote_count.lte'] = hi
        acclaimed = name.startswith('Acclaimed')
        if not acclaimed and media == 'series':
            p['without_genres'] = GEM_TV_NO_GENRES
            p['first_air_date.gte'] = GEM_TV_FIRST_AIR
        # Acclaimed: rating order is safe behind high vote floors. Gems: rating order surfaced novelty entries and
        # popularity order surfaced this month's releases (smoke test), so popular + at least a year old.
        e = disc(f'acclaimed-{slugify(name)}', f'{name}', name, 'Acclaimed' if acclaimed else 'Hidden Gems',
                 'acclaimed', [media], 'top' if acclaimed else 'popular', min_votes=lo, **{media: p})
        if not acclaimed:
            e['min_age_days'] = GEM_MIN_AGE_MOVIES if media == 'movie' else 365
        cats.append(e)
    p = {'with_keywords': '9716'}                            # Stand-up: Popular + New in one card (Top Rated stays cut)
    mp = {**p, 'with_runtime.gte': 20}                       # half-hour specials count; clips don't
    sp = {**p, 'without_genres': '10767|10763|10764'}        # no talent/reality shows ("India's Got Latent")
    cats.append(union_card([disc('standup' + ('' if v == 'popular' else f'-{v}'), f'Stand-up · {n}', f'Stand-up · {n}',
                                 {'popular': 'Stand-up', 'new': 'New · Stand-up'}[v], 'standup', ['movie', 'series'], v,
                                 movie=mp, series=sp, min_votes={'popular': 10, 'new': 3}[v])
                           for v, n in VARIANTS[:2]], 'standup', title='Stand-up'))   # #147: one card, no "· Popular"
    # issue #149: one card per comedian after the Stand-up card, in comedians.json order (greats, then making a wave)
    for n, c in enumerate(json.loads((HERE / 'comedians.json').read_text(encoding='utf-8'))['comedians'], 1):
        cats.append({'slug': f"comic-{slugify(c['name'])}", 'library': f"Comedian · {c['name']}", 'title': c['name'],
                     'eyebrow': 'Comedian', 'folders': ['standup'], 'ranks': {'standup': n}, 'kind': 'comedian',
                     'media': ['movie', 'series'], 'person_id': c['id']})

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

    # keep list (issue #142, owner yes AIO chat 2026-10-17 ~10:52 AM PT; it widens the #139 cut of 2026-10-16): streaming,
    # networks, studios, movie series, themes, awards, acclaimed, stand-up, and every director and actor in one folder
    # each. Genres and Decades stay cut. Issue #143: streaming and stand-up are ONE union card each (Popular + New [+ Top
    # Rated]); the old -new / -top catalogs are retired, the Popular slug (= the library id) stays.
    cats = [c for c in cats if keep_catalog(c)]
    votes = load_votes(sys.argv[2:])
    nameless = [f"{c['eyebrow']} {c['title']} ({c['person_id']})" for c in cats
                if c['kind'] in PEOPLE_KINDS and (c['eyebrow'], c['person_id']) not in votes]
    if nameless:                                             # they would sink to the bottom of their row (critic note on #144)
        sys.exit(f'{len(nameless)} people have no vote numbers in the private order files, so they would rank last: '
                 f'{nameless[:8]}... Fill them first (one TMDB pass into the metrics files).')
    cats = order_people(cats, votes)
    byk = {f['key']: f for f in FOLDERS + PEOPLE_FOLDERS}
    used = {f for c in cats for f in c['folders']}
    FOLDERS[:] = [byk[k] for k in FOLDER_ORDER if k in used]

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
                        # manual deny-list for leaks the keywords miss: hand entries, the source = why it is denied
                        # TV only: 'erotic' (256466). On movies it also removes Quills / Fair Play (run-1 measurement)
                        'without_keywords_tv': '256466',
                        'deny': [build.hand_entry('series', 95897, 'Overflow', 2020, 'Overflow (2020): ecchi anime not flagged adult'),
                                 build.hand_entry('series', 70998, 'Souryo to Majiwaru Shikiyoku no Yoru ni', 2017,
                                                  'Souryo to Majiwaru Shikiyoku no Yoru ni (2017): explicit')]}
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
