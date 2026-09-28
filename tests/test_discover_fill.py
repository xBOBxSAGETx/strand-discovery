"""discover() pager vs TMDB's per-page caching (issue #25), with a stubbed TMDB (no network):

    python -m unittest discover -s tests

TMDB caches every /discover page on its own, so the pages of one query can come from different ranking snapshots:
some ids show up on two pages and others never. The stub serves such "stale" pages for the card's sort and fresh,
consistent pages for the reverse sort.
"""
import os, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

DFLT = {'depth': {'popular': 1000, 'new': 500, 'top': 500, 'release_asc': 1000, 'votes': 1000}}


def pool(n, date_gaps=()):
    """n movies, popularity strictly decreasing with the id; ids in date_gaps have no release date."""
    return [{'id': i, 'title': f'T{i}', 'popularity': 1000.0 - i, 'vote_count': 5000 - i, 'vote_average': 9 - i / 1000,
             'release_date': '' if i in date_gaps else f'{2000 + i // 12:04d}-{i % 12 + 1:02d}-01',
             'poster_path': f'/p{i}.jpg', 'genre_ids': [28]} for i in range(1, n + 1)]


class StubTMDB:
    """Card-sort pages: built from a stale ordering in which `hidden` ids are replaced by repeats of ids from the
    previous page. Reverse-sort pages: the true order, reversed. Records every request."""

    def __init__(self, items, key, hidden=(), per_page=20):
        self.items, self.key, self.hidden, self.per_page, self.calls = items, key, set(hidden), per_page, []

    def order(self, direction):
        true = sorted(self.items, key=lambda r: (r[self.key] or ''), reverse=direction == 'desc')
        return true

    def stale(self, direction):
        rows, pp = self.order(direction), self.per_page
        out = []
        for n, r in enumerate(rows):
            if r['id'] in self.hidden and n >= pp:
                out.append(out[(n // pp) * pp - 1])        # a repeat of the previous page's last row
            else:
                out.append(r)
        return out

    def __call__(self, path, **params):
        self.calls.append(dict(params))
        field, _, direction = params['sort_by'].rpartition('.')
        stale_dir = self.card_direction
        rows = self.stale(direction) if direction == stale_dir else self.order(direction)
        pg, pp = params['page'], self.per_page
        total = len(self.items)
        return {'page': pg, 'results': [dict(r) for r in rows[(pg - 1) * pp:pg * pp]],
                'total_results': total, 'total_pages': -(-total // pp)}


def run(stub, card, direction):
    stub.card_direction = direction
    real = build.tmdb
    build.tmdb = stub
    try:
        return build.discover(card, 'movie', build.depth_of(card, DFLT), DFLT)
    finally:
        build.tmdb = real


CARD = {'slug': 'studio-test', 'kind': 'discover', 'media': ['movie'], 'sort': 'popular', 'movie': {'with_companies': '1'}}


class DiscoverFill(unittest.TestCase):
    def test_holes_filled_to_the_whole_pool(self):
        items = pool(95)
        hidden = {23, 24, 47, 61, 62, 63, 88}
        stub = StubTMDB(items, 'popularity', hidden)
        forward = stub.stale('desc')
        self.assertEqual(len({r['id'] for r in forward}), 95 - len(hidden))    # the stub really has holes
        out = run(stub, CARD, 'desc')
        ids = [int(m['id'].split(':')[1]) for m in out]
        self.assertEqual(len(ids), 95)
        self.assertEqual(len(set(ids)), 95)                                   # no duplicates
        self.assertEqual(ids, list(range(1, 96)))                             # popularity order, holes in place
        self.assertTrue(hidden <= set(ids))
        rev = [c for c in stub.calls if c['sort_by'] == 'popularity.asc']
        self.assertGreater(len(rev), 0)
        self.assertLessEqual(len(rev), 5)                                     # bounded by total_pages
        self.assertTrue(all({k: v for k, v in c.items() if k not in ('sort_by', 'page')} ==
                            {k: v for k, v in stub.calls[0].items() if k not in ('sort_by', 'page')} for c in rev))
        self.assertTrue(all(m['type'] == 'movie' and m['poster'].endswith('.jpg') for m in out))

    def test_stable_order(self):
        items = pool(95)
        a = run(StubTMDB(items, 'popularity', {30, 31, 70}), CARD, 'desc')
        b = run(StubTMDB(items, 'popularity', {30, 31, 70}), CARD, 'desc')
        self.assertEqual(a, b)

    def test_no_holes_no_extra_requests(self):
        stub = StubTMDB(pool(95), 'popularity')
        out = run(stub, CARD, 'desc')
        self.assertEqual([int(m['id'].split(':')[1]) for m in out], list(range(1, 96)))
        self.assertEqual(len(stub.calls), 5)
        self.assertFalse(any(c['sort_by'].endswith('.asc') for c in stub.calls))

    def test_deep_pool_reaches_depth_without_fill(self):
        stub = StubTMDB(pool(300), 'popularity', {25, 26, 27, 50})
        card = dict(CARD, depth=100)
        out = run(stub, card, 'desc')
        ids = [int(m['id'].split(':')[1]) for m in out]
        self.assertEqual(len(ids), 100)
        self.assertEqual(len(set(ids)), 100)
        self.assertFalse(any(c['sort_by'].endswith('.asc') for c in stub.calls))

    def test_fill_stops_once_pool_complete(self):
        stub = StubTMDB(pool(200), 'popularity', {190})          # the missing id sits at the tail
        out = run(stub, CARD, 'desc')
        self.assertEqual(len(out), 200)
        rev = [c for c in stub.calls if c['sort_by'] == 'popularity.asc']
        self.assertEqual(len(rev), 1)                             # found on the reverse pass's first page

    def test_date_sort_and_missing_dates(self):
        items = pool(60, date_gaps={5})
        stub = StubTMDB(items, 'release_date', {33, 34})
        card = dict(CARD, sort='new')
        out = run(stub, card, 'desc')
        ids = [int(m['id'].split(':')[1]) for m in out]
        self.assertEqual(len(ids), 60)
        self.assertEqual(len(set(ids)), 60)
        self.assertEqual(stub.calls[0]['sort_by'], 'primary_release_date.desc')
        self.assertTrue(any(c['sort_by'] == 'primary_release_date.asc' for c in stub.calls))
        self.assertEqual(ids[-1], 5)                              # no date: last
        dates = [next(r['release_date'] for r in items if r['id'] == i) for i in ids[:-1]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_genre_tags_kept_for_filled_items(self):
        out = run(StubTMDB(pool(95), 'popularity', {40}), CARD, 'desc')
        self.assertEqual(next(m for m in out if m['id'] == 'tmdb:40')['_g'], ['Action'])


if __name__ == '__main__':
    unittest.main()
