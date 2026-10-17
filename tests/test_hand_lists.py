"""One validator for every hand-made list (issue #72, part 1 of 3). Each list is read through build.load_hand_list, so the
checks are the same for all of them: type is movie|series, tmdb is an int, no duplicate within a group, every entry has
a source. No network:

    python -m unittest tests.test_hand_lists

KNOWN_GAPS names a list still in an old shape that lacks a field. It is empty since part 2 (#83): every list has the
one shape. A new gap would need a line with a reason, and the test fails when a listed gap is no longer a gap.
"""
import json, os, re, sys, tempfile, unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix='sd-tests-')
os.environ.setdefault('TMDB_API_KEY', 'unused-in-tests')
os.environ['SD_CACHE'] = str(Path(_tmp) / 'sd_cache')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'generator'))
import build  # noqa: E402

# (list, field) -> why. Empty: every hand list carries type, tmdb and source (#83).
KNOWN_GAPS = {}


def key_of(name, e):
    """What makes two entries of one group the same title (the official list repeats a title on another date)."""
    return (e['type'], e['tmdb'], e.get('date')) if name == 'official_arrivals' else (e['type'], e['tmdb'])


def validate(name, groups, gaps=KNOWN_GAPS):
    """-> list of problems (strings) for one loaded list {group: [entry, ...]}."""
    bad = []
    for group, entries in groups.items():
        seen = set()
        for e in entries:
            who = f'{name}/{group} {e.get("title") or e.get("tmdb")}'
            if (name, 'type') not in gaps and e['type'] not in ('movie', 'series'):
                bad.append(f'{who}: bad type {e["type"]!r}')
            if type(e['tmdb']) is not int or e['tmdb'] <= 0:
                bad.append(f'{who}: tmdb is not a positive int: {e["tmdb"]!r}')
            if (name, 'source') not in gaps and not (isinstance(e['source'], str) and e['source'].strip()):
                bad.append(f'{who}: no source')
            if name == 'corrections':      # #117: add|remove|keep, optional why (str) and date (YYYY-MM-DD)
                if e.get('side') not in ('add', 'remove', 'keep'):
                    bad.append(f'{who}: bad side {e.get("side")!r}')
                y = e.get('year')       # #129: a year is a 4-digit int; only a keep may leave it unknown (null)
                if not (type(y) is int and 1850 <= y <= 2100 or (y is None and e.get('side') == 'keep')):
                    bad.append(f'{who}: bad year {y!r}')
                if 'why' in e and not (isinstance(e['why'], str) and e['why'].strip()):
                    bad.append(f'{who}: why is not text')
                if 'date' in e and not re.fullmatch(r'\d{4}-\d\d-\d\d', str(e['date'])):
                    bad.append(f'{who}: bad date {e["date"]!r}')
            k = key_of(name, e)
            if k in seen:
                bad.append(f'{who}: duplicate {k}')
            seen.add(k)
    return bad


class HandListData(unittest.TestCase):
    def test_every_hand_list_passes(self):
        for name in build.HAND_LISTS:
            with self.subTest(list=name):
                groups = build.load_hand_list(name)
                if name == 'card_ids':      # franchise ids live in spec.json; the #139 cut left no franchise card, so also read the pre-cut spec
                    pre = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'spec_before_cut.json').read_text(encoding='utf-8'))
                    groups = {**build.load_hand_list(name, spec=pre), **groups}
                self.assertTrue(groups, f'{name}: loaded nothing')
                self.assertEqual(validate(name, groups), [])

    def test_known_gaps_are_still_gaps(self):
        for name, field in KNOWN_GAPS:
            with self.subTest(gap=(name, field)):
                self.assertIn(name, build.HAND_LISTS)
                entries = [e for es in build.load_hand_list(name).values() for e in es]
                self.assertTrue(any(not e[field] for e in entries),
                                f'{name}.{field} is filled now: delete it from KNOWN_GAPS')


class ValidatorCatches(unittest.TestCase):
    """Replays of the old break: a bad entry used to be silently ignored."""
    GOOD = build.hand_entry('movie', 1, 'A', 2000, 'https://example.org/a')

    def check(self, **change):
        return validate('theme_must_haves', {'g': [self.GOOD, {**self.GOOD, **change}]})

    def test_good_list_is_clean(self):
        self.assertEqual(validate('theme_must_haves', {'g': [self.GOOD]}), [])

    def test_bad_type(self):
        self.assertTrue(any('bad type' in b for b in self.check(type='film', tmdb=2)))

    def test_duplicate(self):
        self.assertTrue(any('duplicate' in b for b in self.check()))

    def test_same_title_in_another_group_is_fine(self):
        self.assertEqual(validate('theme_must_haves', {'g': [self.GOOD], 'h': [self.GOOD]}), [])

    def test_string_id(self):
        self.assertTrue(any('not a positive int' in b for b in self.check(tmdb='2')))

    def test_no_source(self):
        self.assertTrue(any('no source' in b for b in self.check(tmdb=2, source=None)))

    def test_gap_hides_only_its_own_field(self):
        gaps = {('theme_must_haves', 'source')}
        self.assertEqual(validate('theme_must_haves', {'g': [{**self.GOOD, 'source': None}]}, gaps), [])
        self.assertTrue(validate('theme_must_haves', {'g': [{**self.GOOD, 'source': None, 'type': 'x'}]}, gaps))

    def test_corrections_side_why_date_are_checked(self):
        ok = {**self.GOOD, 'side': 'keep', 'why': 'x', 'date': '2026-10-16'}
        self.assertEqual(validate('corrections', {'g': [ok]}, {}), [])
        for bad in ({'side': 'drop'}, {'side': None}, {'why': ''}, {'date': '16/10/2026'}):
            self.assertTrue(validate('corrections', {'g': [{**ok, **bad}]}, {}), bad)

    def test_corrections_year_may_be_unknown_only_for_keep(self):
        ok = {**self.GOOD, 'side': 'keep', 'why': 'x', 'year': None}
        self.assertEqual(validate('corrections', {'g': [ok]}, {}), [])
        for bad in ({'side': 'add'}, {'side': 'remove'}, {'year': '1999'}, {'year': 0}):
            self.assertTrue(any('bad year' in b for b in validate('corrections', {'g': [{**ok, **bad}]}, {})), bad)

    def test_official_arrivals_same_title_other_date_is_not_a_duplicate(self):
        a = {**self.GOOD, 'date': '2026-09-01'}
        self.assertEqual(validate('official_arrivals', {'s': [a, {**a, 'date': '2026-09-02'}]}, {}), [])
        self.assertTrue(validate('official_arrivals', {'s': [a, dict(a)]}, {}))


if __name__ == '__main__':
    unittest.main()
