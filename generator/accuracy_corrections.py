"""The weekly accuracy check honours OUR corrections list (issue #129; the list is generator/corrections.json, issue #117).

An entry {type, tmdb, ..., side: add|remove|keep, why?} sits under a card slug. The check reads it like this:

  keep    a flag (a recall gap or a precision gap) on that (card, title) is RESOLVED: we looked and decided. It is not
          counted as a gap; the report lists it apart as "checked and kept" with the entry's why.
  add     a claim title that is on its card because our list put it there is "filled by our list": counted apart, never
          TMDB recall (filled()). An add that is NOT on its card is a flag: "deliberate rule" when the card's own rules
          drop it (vote / runtime / age / release-type / status floors, released-only), otherwise "code bug".
          An add that shows up as a precision flag (on the card, but the claim does not list it) is "fixed by our list".
  remove  a removed title still on its card = "code bug". A removed title is never a precision gap, and a claim title
          the card lacks because we removed it is "fixed by our list".

The pure pieces here take plain dicts and are unit-tested offline (tests/test_accuracy_corrections.py).
"""
import build

LABELS = ('code bug', 'deliberate rule', 'cap', 'source limit')       # = accuracy_frame.LABELS (not imported: it imports this module)
SIDES = ('add', 'remove', 'keep')


def load(path=None):
    """-> {(card slug, 'movie:123'): entry} for every entry of corrections.json ({} when the file is missing)."""
    try:
        lists = build.load_hand_list('corrections', path=path)
    except OSError:
        return {}
    return {(slug, f"{e['type']}:{int(e['tmdb'])}"): e for slug, es in lists.items() for e in es if e.get('side') in SIDES}


ENTRIES = None                 # the loaded list; tests set it to a dict


def current():
    global ENTRIES
    if ENTRIES is None:
        ENTRIES = load()
    return ENTRIES


def filled(slug, truth_keys, have, entries=None):
    """How many claim titles of this card are on it because OUR list added them (counted apart from TMDB recall)."""
    entries = current() if entries is None else entries
    have = set(have)
    return sum(1 for k in truth_keys if k in have and entries.get((slug, k), {}).get('side') == 'add')


def _bump(row, side, label, d):
    """Move one gap in/out of a summary row's counters (same fields af.summarize writes)."""
    by = row.get(f'{side}_gaps')
    if by is not None and label in by:
        by[label] += d
    if label == 'code bug' and side in ('recall', 'precision') and 'code_bugs' in row:
        row['code_bugs'] += d


def _why(e):
    return e.get('why') or e.get('source') or ''


def settle(frame_sum, frame_gaps, entries, audited, order_of, reason_of, built=None):
    """Apply the corrections list to the gaps of a run. frame_sum / frame_gaps are changed in place.
    audited = slugs whose whole card was read this run; order_of(slug) -> ordered keys of the card;
    reason_of(slug, key) -> text when the card's own rules drop the title, else None.
    built = date of the deployed build (summary generated_at): an add whose entry was added on or after that day cannot be on
    the live card yet, so it is 'pending' (listed in out['pending']), never a flag.
    Returns {'kept': [...], 'problems': [...]}; every summary row gains 'open', 'fixed_by_list' and 'kept'."""
    rows = {r['kind']: r for r in frame_sum}
    kept, fixed, problems, pending = [], [], [], []
    unbuilt = lambda e: bool(built) and bool(e.get('added')) and str(built)[:10] <= str(e['added'])[:10]
    for g in list(frame_gaps):
        e = entries.get((g['card'], g['key']))
        if not e:
            continue
        side, row = e['side'], rows.get(g['kind'])
        if g['side'] not in ('recall', 'precision'):
            continue
        resolved = None
        if side == 'keep':
            resolved = kept
        elif side == 'remove' or (side == 'add' and g['side'] == 'precision'):
            resolved = fixed          # a removed title is not a precision gap, and not a recall gap either: we took it off on purpose
        elif unbuilt(e):              # add + a recall gap, but the live build is older than the entry: not deployed yet
            resolved = pending
        else:                         # add + a recall gap: the title should be on the card
            lab, why = relabel_add(g['card'], g['key'], reason_of)
            if row:
                _bump(row, 'recall', g['label'], -1)
                _bump(row, 'recall', lab, 1)
            g['label'], g['reason'] = lab, why
            continue
        frame_gaps.remove(g)
        if row:
            _bump(row, g['side'], g['label'], -1)
        resolved.append(dict(g, why=_why(e), side_of_entry=side))
    for (slug, key), e in sorted(entries.items()):          # every add / remove entry of a card read this run
        if slug not in audited:
            continue
        order = order_of(slug)
        if not order:
            continue
        on_card = key in set(order)
        if e['side'] == 'add' and not on_card and unbuilt(e):
            pending.append({'card': slug, 'key': key})
        elif e['side'] == 'add' and not on_card and not any(g['card'] == slug and g['key'] == key for g in frame_gaps):
            lab, why = relabel_add(slug, key, reason_of)
            frame_gaps.append({'kind': 'corrections', 'card': slug, 'side': 'recall', 'key': key, 'name': e.get('title', key),
                               'label': lab, 'reason': why})
            _row(rows, frame_sum)['recall_gaps'][lab] += 1
            if lab == 'code bug':
                rows['corrections']['code_bugs'] += 1
        if e['side'] == 'remove' and on_card:
            frame_gaps.append({'kind': 'corrections', 'card': slug, 'side': 'precision', 'key': key, 'name': e.get('title', key),
                               'label': 'code bug', 'reason': 'our list removes it but it is still on the card'})
            _row(rows, frame_sum)['precision_gaps']['code bug'] += 1
            rows['corrections']['code_bugs'] += 1
            problems.append(f"corrections: {slug} still holds {key} ({e.get('title', '')}), which our list removes")
    for r in frame_sum:
        mine = [g for g in frame_gaps if g['kind'] == r['kind'] and g['side'] in ('recall', 'precision')]
        r['open'] = len(mine)
        r['fixed_by_list'] = r.get('filled', 0) + sum(1 for g in fixed if g['kind'] == r['kind'])
        r['kept'] = [g for g in kept if g['kind'] == r['kind']]
    once = {(p['card'], p['key']): {'card': p['card'], 'key': p['key']} for p in pending}      # an add that is also a claim gap counts once
    return {'kept': kept, 'fixed': fixed, 'problems': problems, 'pending': list(once.values())}


def _row(rows, frame_sum):
    """The synthetic row that carries gaps found by the corrections scan itself (no measured sample behind it)."""
    if 'corrections' not in rows:
        rows['corrections'] = {'kind': 'corrections', 'cards': 0, 'truth': 0, 'found': 0, 'recall': None, 'sample': 0, 'ok': 0,
                               'precision': None, 'filled': 0, 'recall_gaps': {l: 0 for l in LABELS},
                               'precision_gaps': {l: 0 for l in LABELS}, 'code_bugs': 0}
        frame_sum.append(rows['corrections'])
    return rows['corrections']


def build_rule_reason(spec, tmdb):
    """reason_of(slug, key) for settle(): the build's OWN check of an add (build.correction_rule, fetched the way
    build.apply_corrections fetches it), so the check and the build can never disagree. -> text, or None when the card takes it."""
    cards = {c['slug']: c for c in spec['catalogs']}

    def reason(slug, key):
        card = cards[slug]
        media, tid = key.split(':')
        if media not in (card.get('media') or ('movie', 'series')):
            return f'card holds no {media}'
        d = build.correction_details(card, media, int(tid), fetch=tmdb)
        return build.correction_rule(card, spec.get('defaults'), media, d) if d.get('id') else 'unknown to TMDB'
    return reason


def recall_lines(frame_sum):
    """Per kind with titles our list filled: recall with and without our lists. 'corrections applied' only for the rows that
    count it (theme rows)."""
    out = []
    for r in frame_sum:
        if r.get('tmdb_recall') is not None and (r.get('filled') or 'corrections' in r):
            applied = f"; corrections applied {r['corrections']['found']}/{r['corrections']['total']}" if 'corrections' in r else ''
            out.append(f"\n{r['kind']} recall: {r['recall']} with our own lists, {r['tmdb_recall']} from TMDB alone "
                       f"({r['filled']} of {r['truth']} claim titles are on the cards because our must-have / corrections lists put them there{applied})")
    return out


def relabel_add(slug, key, reason_of):
    """An add that is not on its card: -> (label, reason)."""
    try:
        rule = reason_of(slug, key)
    except Exception:                                      # TMDB down: never a pretend 'deliberate rule'
        rule = None
    if rule:
        return 'deliberate rule', 'our list adds it, the card\'s own rules drop it: ' + rule
    return 'code bug', 'our list adds it and the card\'s rules allow it, but it is not on the card'


def lines(frame_sum, kept):
    """The report block: per kind, flags open / fixed by our list / checked and kept; then each kept title with its why."""
    shown = [r for r in frame_sum if r.get('open') or r.get('fixed_by_list') or r.get('kept')]
    if not shown:
        return []
    out = ['', '| kind | flags open | fixed by our list | checked and kept |', '|---|---|---|---|']
    out += [f"| {r['kind']} | {r['open']} | {r['fixed_by_list']} | {len(r['kept'])} |" for r in shown]
    if kept:
        out.append(f'\nchecked and kept: {len(kept)}')
        out += [f"- {g['card']} {g['key']} {g['name']} ({g['side']}): {g['why']}" for g in kept]
    return out
