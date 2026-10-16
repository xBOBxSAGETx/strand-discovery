"""Our own corrections list for theme cards (issue #117), with a stubbed TMDB (no network):

    python -m unittest tests.test_corrections

A theme shelf that lacks a Wikidata-confirmed title gets it from generator/corrections.json; a title already on the
shelf is not added twice or fetched again; the shelf cap holds.
"""
import json, os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

HERE = Path(build.HERE)
FIXED = {'movie': 3933, 'series': 50241}      # Corpse Bride (zombies card), Lorong Waktu (time-travel card): corrections, not must-haves
DFLT = {'depth': {'popular': 1000, 'new': 500, 'top': 500, 'release_asc': 1000, 'votes': 1000}}
CARD = {'slug': 'theme-zombies', 'kind': 'discover', 'media': ['movie'], 'sort': 'popular', 'movie': {'with_keywords': '12377'}}
CORR_MOVIES = [e['tmdb'] for e in json.loads((HERE / 'corrections.json').read_text(encoding='utf-8'))['theme-zombies']
               if e['type'] == 'movie']
POOL_SIZE = 4


def row(i, name):
    return {'id': i, 'title': name, 'popularity': 100.0 - i % 50, 'release_date': '2014-01-01',
            'poster_path': f'/p{i}.jpg', 'genre_ids': [27], 'vote_count': 500, 'vote_average': 7.0}


class Stub:
    """/discover returns `pool` on page 1; /movie/<id> returns the title for any id (counted)."""

    def __init__(self, pool):
        self.pool, self.details = pool, []

    def __call__(self, path, **params):
        if path.startswith('/discover'):
            return {'page': 1, 'results': [dict(r) for r in self.pool], 'total_results': len(self.pool), 'total_pages': 1}
        self.details.append(path)
        return row(int(path.rsplit('/', 1)[1]), f'Detail {path}')


def build_card(stub, dflt=DFLT, card=CARD):
    real = build.tmdb
    build.tmdb = stub
    build._stable.clear()
    try:
        return build.build_one(card, dflt)[0]
    finally:
        build.tmdb = real


def ids_of(metas):
    return [m['id'] for m in metas]


class Corrections(unittest.TestCase):
    def pool(self, *extra):
        return [row(9000 + n, f'Other {n}') for n in range(POOL_SIZE)] + [row(i, f'T{i}') for i in extra]

    def test_missing_title_is_added_and_present_one_is_not_duplicated(self):
        have = CORR_MOVIES[0]
        missing = [i for i in CORR_MOVIES if i != have]
        stub = Stub(self.pool(have))
        ids = ids_of(build_card(stub))
        for i in missing:
            self.assertEqual(ids.count(f'tmdb:{i}'), 1, i)               # old code: absent
        self.assertEqual(ids.count(f'tmdb:{have}'), 1)                   # already there: no duplicate
        self.assertNotIn(f'/movie/{have}', stub.details)                 # and no extra request for it
        self.assertEqual(len(ids), len(set(ids)))

    def test_shelf_order_is_kept_corrections_are_placed_by_it(self):
        ids = ids_of(build_card(Stub(self.pool())))
        sorted_part = [f'tmdb:{9000 + n}' for n in range(POOL_SIZE)]
        at = [ids.index(i) for i in sorted_part]
        self.assertEqual(at, sorted(at))                                 # discover order untouched
        self.assertTrue(all(f'tmdb:{c}' in ids for c in CORR_MOVIES))     # popularity 100 - id % 50 puts them after the pool here

    def test_cap_holds(self):
        stub = Stub(self.pool())
        must = build.must_haves(CARD, 'movie')
        dflt = {'depth': {'popular': len(must) + len(CORR_MOVIES) + 2}}  # room for the must-haves, the corrections and 2 of the 4 sorted titles
        ids = ids_of(build_card(stub, dflt))
        self.assertEqual(len(ids), len(must) + len(CORR_MOVIES) + 2)     # the cap holds
        self.assertEqual([i for i in ids if i.startswith('tmdb:90')], ['tmdb:9000', 'tmdb:9001'])   # the sorted tail made room
        self.assertTrue(all(f'tmdb:{i}' in ids for i in must) and all(f'tmdb:{c}' in ids for c in CORR_MOVIES))

    def test_other_cards_untouched(self):
        ids = ids_of(build_card(Stub(self.pool()), card={**CARD, 'slug': 'studio-test'}))
        self.assertEqual(ids, [f'tmdb:{9000 + n}' for n in range(POOL_SIZE)])

    def test_only_side_add_is_built(self):
        real = build.load_hand_list
        e = build.hand_entry('movie', 777, 'X', 2000, 's')
        build.load_hand_list = lambda name, *a, **k: {'theme-zombies': [{**e, 'side': 'add'}, {**e, 'tmdb': 778, 'side': 'remove'},
                                                                       {**e, 'tmdb': 779, 'side': 'keep'}]}
        try:
            self.assertEqual(build.corrections(CARD, 'movie'), [777])
        finally:
            build.load_hand_list = real

    def test_not_released_is_skipped(self):
        class Future(Stub):
            def __call__(self, path, **params):
                r = super().__call__(path, **params)
                if not path.startswith('/discover'):
                    r['release_date'] = '2999-01-01'
                return r
        ids = ids_of(build_card(Future(self.pool())))
        self.assertTrue(all(f'tmdb:{c}' not in ids for c in CORR_MOVIES))


def mk(i, pop=50.0, date='2014-01-01', votes=500, rating=7.0, **kw):
    return {'id': i, 'title': f'T{i}', 'name': f'T{i}', 'popularity': pop, 'release_date': date, 'first_air_date': date,
            'vote_count': votes, 'vote_average': rating, 'poster_path': f'/p{i}.jpg', 'genre_ids': [18], **kw}


class World:
    """A stubbed TMDB: `pool` for /discover, `rows` {id: row} for /movie/<id> and /tv/<id>, `paths` {path: response}."""

    def __init__(self, pool=(), rows=None, paths=None):
        self.pool, self.rows, self.paths, self.asked = list(pool), rows or {}, paths or {}, []

    def __call__(self, path, **params):
        self.asked.append(path)
        if path.startswith('/discover'):
            pool = [] if path.endswith('/tv') else self.pool       # the pool is the movie list; TV discover is empty
            return {'page': 1, 'results': [dict(r) for r in pool], 'total_results': len(pool), 'total_pages': 1}
        if path in self.paths:
            return self.paths[path]
        i = int(path.rsplit('/', 1)[1])
        return dict(self.rows[i] if i in self.rows else next(r for r in self.pool if r['id'] == i))


def build_with(world, card, entries, dflt=DFLT, fs=None):
    """build_one for `card` with these corrections entries ({type, tmdb, title, side, ...}); -> (ids, notes)."""
    real = build.tmdb, build.load_hand_list, build.FS_SUMMARY, build.FS_CANDIDATES
    base = real[1]
    group = [build.hand_entry(e.pop('type', 'movie'), e['tmdb'], e.get('title', f"T{e['tmdb']}"), 2014, 's', **{k: v for k, v in e.items() if k not in ('tmdb', 'title')})
             for e in map(dict, entries)]
    build.tmdb = world
    build.load_hand_list = lambda name, *a, **k: {card['slug']: group} if name == 'corrections' else base(name, *a, **k)
    if fs:
        build.FS_SUMMARY, build.FS_CANDIDATES = fs
    for n in ('_stable', '_names', '_keys', '_new_keys'):
        getattr(build, n, {}).clear()
    try:
        metas, notes = build.build_one(card, dflt)
        return ids_of(metas), notes
    finally:
        build.tmdb, build.load_hand_list, build.FS_SUMMARY, build.FS_CANDIDATES = real


def add(i, **kw):
    return {'tmdb': i, 'side': 'add', **kw}


def remove(i, **kw):
    return {'tmdb': i, 'side': 'remove', **kw}


POP = {'slug': 'theme-x', 'kind': 'discover', 'media': ['movie'], 'sort': 'popular', 'movie': {'with_keywords': '1'}}
DAYS = lambda n: (__import__('datetime').date.fromisoformat(build.TODAY) - __import__('datetime').timedelta(days=n)).isoformat()


class EveryKind(unittest.TestCase):
    """Issue #129: the corrections list works on every card kind, add and remove, and an add lands where the card's own order puts it."""

    def test_remove_on_a_franchise_card(self):
        parts = [mk(1, date='1999-01-01'), mk(2, date='2001-01-01'), mk(3, date='2003-01-01')]
        w = World(paths={'/collection/9': {'name': 'F', 'parts': parts}})
        card = {'slug': 'series-f', 'kind': 'franchise', 'media': ['movie'], 'collection_ids': [9], 'ids': []}
        self.assertEqual(build_with(w, card, [])[0], ['tmdb:1', 'tmdb:2', 'tmdb:3'])
        self.assertEqual(build_with(w, card, [remove(2)])[0], ['tmdb:1', 'tmdb:3'])        # old code: 2 stays

    def test_add_on_a_franchise_card_goes_by_release_date(self):
        parts = [mk(1, date='1999-01-01'), mk(3, date='2003-01-01')]
        w = World(rows={2: mk(2, date='2001-05-05')}, paths={'/collection/9': {'name': 'F', 'parts': parts}})
        card = {'slug': 'series-f', 'kind': 'franchise', 'media': ['movie'], 'collection_ids': [9], 'ids': []}
        self.assertEqual(build_with(w, card, [add(2)])[0], ['tmdb:1', 'tmdb:2', 'tmdb:3'])

    def test_remove_on_an_actor_card(self):
        cast = [{'media_type': 'movie', 'id': i, 'title': f'T{i}', 'release_date': '2010-01-01', 'vote_count': v, 'order': 0,
                 'character': 'X', 'genre_ids': [18], 'popularity': 1} for i, v in ((1, 900), (2, 500), (3, 100))]
        w = World(paths={'/person/5/combined_credits': {'cast': cast}})
        card = {'slug': 'actor-a', 'kind': 'actor', 'person_id': 5}
        self.assertEqual(build_with(w, card, [remove(1)])[0], ['tmdb:2', 'tmdb:3'])            # old code: 1 stays

    def test_add_on_an_actor_card_goes_by_vote_count(self):
        cast = [{'media_type': 'movie', 'id': i, 'title': f'T{i}', 'release_date': '2010-01-01', 'vote_count': v, 'order': 0,
                 'character': 'X', 'genre_ids': [18], 'popularity': 1} for i, v in ((1, 900), (3, 100))]
        w = World(rows={2: mk(2, votes=500)}, paths={'/person/5/combined_credits': {'cast': cast}})
        card = {'slug': 'actor-a', 'kind': 'actor', 'person_id': 5}
        self.assertEqual(build_with(w, card, [add(2)])[0], ['tmdb:1', 'tmdb:2', 'tmdb:3'])

    def test_add_on_a_popularity_card_lands_at_its_popularity_rank(self):
        pool = [mk(1, pop=90), mk(2, pop=80), mk(3, pop=70), mk(4, pop=60)]
        w = World(pool, rows={9: mk(9, pop=75)})
        ids, _ = build_with(w, POP, [add(9)])
        self.assertEqual(ids, ['tmdb:1', 'tmdb:2', 'tmdb:9', 'tmdb:3', 'tmdb:4'])             # old code: last
        self.assertEqual(build_with(World(pool, rows={9: mk(9, pop=99)}), POP, [add(9)])[0][0], 'tmdb:9')   # outranks all: first

    def test_add_on_a_rating_card_lands_by_rating(self):
        pool = [mk(1, rating=9.0), mk(2, rating=8.0), mk(3, rating=7.0)]
        card = {**POP, 'sort': 'top'}
        ids, _ = build_with(World(pool, rows={9: mk(9, rating=8.5, runtime=120)}), card, [add(9)])
        self.assertEqual(ids, ['tmdb:1', 'tmdb:9', 'tmdb:2', 'tmdb:3'])

    def test_add_on_a_release_date_card_lands_by_date(self):
        pool = [mk(1, date='2020-01-01'), mk(2, date='2010-01-01'), mk(3, date='2000-01-01')]
        card = {**POP, 'sort': 'new'}
        ids, _n = build_with(World(pool, rows={9: mk(9, date='2005-06-06')}), card, [add(9)])
        self.assertEqual(ids, ['tmdb:1', 'tmdb:2', 'tmdb:9', 'tmdb:3'])
        card = {**POP, 'sort': 'release_asc'}                                              # oldest first
        pool = [mk(3, date='2000-01-01'), mk(2, date='2010-01-01'), mk(1, date='2020-01-01')]
        ids, _n = build_with(World(pool, rows={9: mk(9, date='2005-06-06')}), card, [add(9)])
        self.assertEqual(ids, ['tmdb:3', 'tmdb:9', 'tmdb:2', 'tmdb:1'])

    def test_dated_add_on_a_first_seen_card_lands_at_its_date(self):
        cands = [{'id': f'tmdb:{i}', 'type': 'movie', 'name': f'T{i}', 'first_seen': DAYS(n), 'popularity': 5.0}
                 for i, n in ((1, 100), (2, 150), (3, 200))]                               # all older than 30 days: newest first
        card = {'slug': 'streaming-x-new', 'kind': 'discover', 'media': ['movie'], 'sort': 'new', 'provider': 'x'}
        fs = ({'providers': {'x': {'mode': 'first_seen'}}}, {'x': cands})
        w = World(rows={9: mk(9, pop=1.0)})
        ids, _ = build_with(w, card, [add(9, date=DAYS(120))], fs=fs)
        self.assertEqual(ids, ['tmdb:1', 'tmdb:9', 'tmdb:2', 'tmdb:3'])                    # old code: last (or absent)
        ids, notes = build_with(w, card, [add(9)], fs=fs)                                  # no date on a New card: skipped, named
        self.assertEqual(ids, ['tmdb:1', 'tmdb:2', 'tmdb:3'])
        self.assertIn('correction skipped: T9 (no date for a New card)', notes)

    def test_cap_holds_and_additions_and_must_haves_are_never_cut(self):
        pool = [mk(i, pop=100 - i) for i in range(1, 7)]
        must = build.must_haves({**POP, 'slug': 'theme-zombies'}, 'movie')
        card = {**POP, 'slug': 'theme-zombies'}
        rows = {9: mk(9, pop=0.5), **{i: mk(i, pop=0.1) for i in must}}                   # the addition ranks LAST by popularity
        ids, _ = build_with(World(pool, rows=rows), card, [add(9)], dflt={'depth': {'popular': len(must) + 4}})
        self.assertEqual(len(ids), len(must) + 4)
        self.assertIn('tmdb:9', ids)                                                       # not cut, though it ranks last
        self.assertTrue(all(f'tmdb:{i}' in ids for i in must))
        self.assertEqual([i for i in ids if i in {f'tmdb:{n}' for n in range(1, 7)}], [f'tmdb:{n}' for n in range(1, 4)])   # the tail made room

    def test_an_add_must_still_pass_the_cards_other_rules(self):
        cases = {'min_votes': mk(9, votes=3), 'not released': mk(9, date='2999-01-01'), 'adult': mk(9, adult=True),
                 'runtime': mk(9, runtime=30)}
        for why, row in cases.items():
            card = {**POP, 'sort': 'top'} if why == 'runtime' else POP
            ids, notes = build_with(World([mk(1)], rows={9: row}), card, [add(9)])
            self.assertEqual(ids, ['tmdb:1'], why)
            self.assertIn(f'correction skipped: T9 ({why})', notes)
        kw = mk(9, keywords={'keywords': [{'id': 195669}]})                                # a default excluded keyword
        ids, notes = build_with(World([mk(1)], rows={9: kw}), POP, [add(9)], dflt={**DFLT, 'without_keywords': '195669|285672'})
        self.assertIn('(without_keywords)', notes)
        old = mk(9, date=DAYS(10))                                                         # min_age_days
        self.assertIn('(min_age_days)', build_with(World([mk(1)], rows={9: old}), {**POP, 'min_age_days': 180}, [add(9)])[1])
        talk = mk(9, genre_ids=[10767], first_air_date='2014-01-01')                       # series: no talk shows
        card = {**POP, 'media': ['series'], 'series': {}}
        self.assertIn('(without_genres)', build_with(World([], rows={9: talk}), card, [add(9, type='series')])[1])
        no_release = {**mk(9), 'release_dates': {'results': [{'release_dates': [{'type': 1}]}]}}   # theatrical only
        self.assertIn('(release type 4|5|6)', build_with(World([mk(1)], rows={9: no_release}), POP, [add(9)])[1])
        denied = {**DFLT, 'deny': [build.hand_entry('movie', 9, 'T9', 2014, 's')]}
        self.assertIn('(deny list)', build_with(World([mk(1)], rows={9: mk(9)}), POP, [add(9)], dflt=denied)[1])
        fine = build_with(World([mk(1)], rows={9: mk(9)}), POP, [add(9)])                  # control: a fitting add goes in
        self.assertEqual(fine, (['tmdb:1', 'tmdb:9'], ''))

    def test_trim_never_cuts_a_correction_already_in_the_sorted_list(self):
        pool = [mk(1, pop=90), mk(2, pop=80), mk(3, pop=10)]          # 3 is a correction that TMDB already ranks last
        ids, _ = build_with(World(pool, rows={9: mk(9, pop=85)}), POP, [add(3), add(9)], dflt={'depth': {'popular': 3}})
        self.assertEqual(ids, ['tmdb:1', 'tmdb:9', 'tmdb:3'])        # 2 made room; old tail-trim cut 3

    def test_a_tv_film_already_on_the_card_as_a_movie_is_not_added_twice(self):
        card = {**POP, 'media': ['movie', 'series'], 'series': {}}
        pool = [mk(1, pop=90, title='Category 7', release_date='2005-01-01')]
        tv = mk(9, pop=50, name='Category 7', date='2005-01-01')
        tv.pop('title')
        ids, notes = build_with(World(pool, rows={9: tv}), card, [add(9, type='series')])
        self.assertEqual(ids, ['tmdb:1'])
        self.assertIn('correction skipped: T9 (same title and year already on the card as a movie)', notes)

    def test_remove_wins_and_keep_changes_nothing(self):
        pool = [mk(1, pop=90), mk(2, pop=80)]
        self.assertEqual(build_with(World(pool), POP, [remove(1), {'tmdb': 2, 'side': 'keep'}])[0], ['tmdb:2'])


class AddMustPassEveryDiscoverFilter(unittest.TestCase):
    """An add also passes each discover() filter its card uses: one case per filter, each with a fitting control."""

    def skipped(self, card, row, media='movie'):
        ids, notes = build_with(World([mk(1)], rows={9: row}), card, [add(9, type=media)])
        return notes, ids

    def check(self, why, card, bad, good, media='movie'):
        notes, ids = self.skipped(card, bad, media)
        self.assertIn(f'correction skipped: T9 ({why})', notes, why)                  # old code: no note, the add went in
        self.assertNotIn('tmdb:9', ids)
        notes, ids = self.skipped(card, good, media)                                  # control: a fitting title goes in
        self.assertEqual(notes, '', why)
        self.assertIn('tmdb:9', ids)

    def own(self, **f):
        return {**POP, 'movie': {**POP['movie'], **f}}

    def test_min_rating(self):
        self.check('vote_average', self.own(**{'vote_average.gte': '7.8'}), mk(9, rating=7.0), mk(9, rating=8.2))

    def test_max_votes(self):
        self.check('vote_count.lte', self.own(**{'vote_count.lte': '600'}), mk(9, votes=5000), mk(9, votes=300))

    def test_language(self):
        self.check('language', self.own(with_original_language='ja'), mk(9, original_language='en'), mk(9, original_language='ja'))

    def test_movie_date_window(self):
        card = self.own(**{'primary_release_date.gte': '1990-01-01', 'primary_release_date.lte': '1999-12-31'})
        self.check('date window', card, mk(9, date='1985-05-01'), mk(9, date='1995-05-01'))
        self.assertIn('(date window)', self.skipped(card, mk(9, date='2004-05-01'))[0])

    def test_series_date_window(self):
        card = {**POP, 'media': ['series'], 'series': {'first_air_date.gte': '2000-01-01', 'first_air_date.lte': '2009-12-31'}}
        self.check('date window', card, mk(9, date='2015-01-01'), mk(9, date='2005-01-01'), 'series')

    def test_series_status(self):
        card = {**POP, 'media': ['series'], 'series': {}}
        self.check('status', card, mk(9, status='Planned'), mk(9, status='Ended'), 'series')
        self.assertIn('(status)', self.skipped(card, mk(9, status='In Production'), 'series')[0])

    def test_tv_air_window(self):
        card = {**POP, 'media': ['series'], 'series': {}, 'tv_air_window': 45}
        self.check('air window', card, mk(9, status='Ended', last_air_date=DAYS(200)), mk(9, status='Returning Series', last_air_date=DAYS(5)), 'series')

    def test_watch_provider_region_and_monetization(self):
        card = self.own(with_watch_providers='8|337', watch_region='US', with_watch_monetization_types='flatrate')

        def on(region, kind, pid):
            return mk(9, **{'watch/providers': {'results': {region: {kind: [{'provider_id': pid}]}}}})
        self.check("not on the card's service", card, on('US', 'flatrate', 15), on('US', 'flatrate', 337))      # other service
        self.assertIn("(not on the card's service)", self.skipped(card, on('GB', 'flatrate', 8))[0])          # other region
        self.assertIn("(not on the card's service)", self.skipped(card, on('US', 'rent', 8))[0])              # rent, card wants flatrate

    def test_watch_provider_also_binds_a_new_card(self):
        card = {**self.own(with_watch_providers='8', watch_region='US'), 'provider': 'netflix'}
        off = mk(9, **{'watch/providers': {'results': {'US': {'flatrate': [{'provider_id': 15}]}}}})
        ids, notes = build_with(World([mk(1)], rows={9: off}), card, [add(9, date='2026-10-01')],
                                fs=({'providers': {}}, {}))
        self.assertIn("(not on the card's service)", notes)


class CorrectionData(unittest.TestCase):
    def test_every_add_on_a_new_card_has_a_date(self):
        spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
        new = {c['slug'] for c in spec['catalogs'] if c.get('provider')}
        for slug, entries in build.load_hand_list('corrections').items():
            for e in entries:
                if slug in new and e['side'] == 'add':
                    self.assertTrue(e.get('date'), f"{slug}: {e['title']} needs the date it reached the service")


    def test_file_is_well_formed(self):
        lists = build.load_hand_list('corrections')
        must = build.load_hand_list('theme_must_haves')
        spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
        slugs = {c['slug'] for c in spec['catalogs']}
        self.assertTrue(set(lists) <= slugs)
        self.assertIn(FIXED['movie'], [e['tmdb'] for e in lists['theme-zombies']])
        for slug, entries in lists.items():
            for e in entries:
                self.assertTrue(e['title'] and e['added'], f"{slug}: {e['title']}")
                self.assertTrue(e['source'].startswith('https://') and e['why'], f"{slug}: {e['title']}")      # evidence URL + the reason
                if e['side'] != 'keep':
                    self.assertTrue(isinstance(e['year'], int), f"{slug}: {e['title']}")   # an add/remove carries a year (its second source is the evidence URL: Wikidata, Wikipedia or the service's own page)
            held = {(m['type'], m['tmdb']) for m in must.get(slug, [])}
            self.assertFalse(held & {(e['type'], e['tmdb']) for e in entries if e['side'] == 'add'}, f'{slug}: a correction repeats a must-have')


if __name__ == '__main__':
    unittest.main()
