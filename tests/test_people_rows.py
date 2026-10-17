"""People rows (issue #142): every director and actor is back, in ONE 'Directors' folder and ONE 'Actors' folder, ranked
1..N; studios, movie series and themes are back too; and every card keeps the slug, library name, params and media it had
before the #139 cut (those slugs are the server library ids). Offline:

    python -m unittest tests.test_people_rows

Control: the spec as the #139 cut left it (21d3c18) fails problems(); the second test breaks a copy of today's spec on
purpose and checks that problems() notices.
"""
import copy, json, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRE = {c['slug']: c for c in json.loads((ROOT / 'tests' / 'fixtures' / 'spec_before_cut.json').read_text(encoding='utf-8'))['catalogs']}
ORDER = ['streaming-popular', 'streaming-new', 'networks', 'directors', 'actors', 'studios', 'series', 'themes',
         'awards', 'acclaimed', 'standup']


def problems(spec):
    out = []
    folders = [f['key'] for f in spec['folders']]
    if folders != ORDER:
        out.append(f'folder order {folders}')
    for key, role, n in (('directors', 'director', 177), ('actors', 'actor', 173)):
        people = [c for c in spec['catalogs'] if c.get('kind') == role]
        if len(people) != n or any(c['folders'] != [key] for c in people):
            out.append(f'{key}: {len(people)} people, not all in the one {key} folder')
        if sorted(c['ranks'].get(key, 0) for c in people) != list(range(1, n + 1)):
            out.append(f'{key}: ranks are not 1..{n}')
    for k in ('studios', 'series', 'themes'):
        if not any(k in c['folders'] for c in spec['catalogs']):
            out.append(f'folder {k} has no cards')
    for c in spec['catalogs']:
        old = PRE.get(c['slug'])
        if old is None:
            out.append(f"{c['slug']}: not in the pre-cut spec")
        elif {k: v for k, v in c.items() if k not in ('folders', 'ranks')} != {k: v for k, v in old.items() if k not in ('folders', 'ranks')}:
            out.append(f"{c['slug']}: changed since before the cut")
    return out


SPEC = json.loads((ROOT / 'generator' / 'spec.json').read_text(encoding='utf-8'))


class PeopleRows(unittest.TestCase):
    def test_spec_has_one_directors_row_one_actors_row_and_the_old_ids(self):
        self.assertEqual(problems(SPEC), [])
        self.assertEqual(len(SPEC['catalogs']), 518)       # 81 kept + 177 + 173 + 18 + 57 + 13, Tarantino counted once

    def test_control_a_broken_copy_is_noticed(self):
        bad = copy.deepcopy(SPEC)
        d = next(c for c in bad['catalogs'] if c.get('kind') == 'director')
        d['folders'] = ['directors-legends']                               # the old per-category folder
        bad['catalogs'] = [c for c in bad['catalogs'] if 'studios' not in c['folders']]
        bad['catalogs'][0]['library'] += ' x'                              # a renamed library = a new server id
        self.assertGreaterEqual(len(problems(bad)), 3)


if __name__ == '__main__':
    unittest.main()
