"""Yearly people-list review: which director/actor cards have grown thin, as an issue body (markdown on stdout).

  python people_review.py <summary.json>

Uses the deployed summary.json item counts. The people lists were curated with a floor of 3 qualifying titles
(2 for Up & Coming / Rising Stars); a person under that floor now, or with an empty card, is listed for review.
The list itself is curated by hand - this only points at candidates for a swap.
"""
import json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOW_FLOOR = {'directors-up-and-coming', 'actors-rising-stars'}


def main():
    counts = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))['catalogs']
    spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
    titles = {f['key']: f['title'] for f in spec['folders']}
    people = [c for c in spec['catalogs'] if c['kind'] in ('director', 'actor')]
    live = sum(1 for c in people if f"sd-{c['slug']}" in counts)
    if live < len(people) / 2:                    # people cards not deployed yet (staged rollout)
        print(f'The people catalogs are not deployed yet ({live} of {len(people)} live), so there is nothing to '
              'review this year. This check runs again next October.')
        return
    rows = {}
    for c in spec['catalogs']:
        if c['kind'] not in ('director', 'actor'):
            continue
        n = counts.get(f"sd-{c['slug']}")
        for f in c['folders']:
            floor = 2 if f in LOW_FLOOR else 3
            if n is None or n < floor:
                rows.setdefault(f, []).append((c['title'], n, floor))
    print('Yearly review of the curated people lists (Directors / Actors folders).\n')
    print('A card below its curation floor is a candidate to swap for an alternate '
          '(the curated CSV keeps ranked alternates). Counts are from the deployed summary.json.\n')
    if not rows:
        print('Every person is at or above the floor. Nothing to do this year.')
    for f, people in rows.items():
        print(f"### {titles.get(f, f)}")
        for name, n, floor in people:
            print(f"- {name}: {'missing' if n is None else n} titles (floor {floor})")
        print()


if __name__ == '__main__':
    main()
