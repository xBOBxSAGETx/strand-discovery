"""The shared accuracy frame (issue #33 audit, job #92): ONE way to measure a card kind that has a plain claim.

  pick N cards of a kind (fixed seed) -> RECALL: titles the claim implies but the card lacks
                                      -> PRECISION: titles on the card that do not meet the claim
                                      -> every gap gets ONE label.

Labels (the audit's four kinds of gap):
  code bug         the card breaks its OWN rule (a title the rule should have kept out is on the card), or the rule is
                   applied wrongly
  deliberate rule  a rule written on purpose (vote floor, release types, status, min age, runtime, adult-keyword and
                   talk/news exclusions, deny list, released-only) keeps a title the claim would include out of the card
  cap              the card is at its depth cap for that medium, so a title further down the ranking is cut
  source limit     TMDB's own data disagrees with itself (discover says a title is on the provider, the title's own
                   watch/providers says not) or no rule explains a missing title (by elimination: TMDB discover paging
                   snapshots, issue #25)

THE CLAIMS (what the name promises, in plain words) and THE RULES in code (generator/build.py discover(), line numbers
of the commit this was written on):
  popular  "Netflix . Popular" = the most-watched-right-now titles (movies and series) you can stream on Netflix in the US
           without paying extra (the card's own monetization: flatrate; Tubi / Pluto free+ads; Plex free+ads+rent).
           Rule: TMDB /discover with the provider ids, watch_region US, sort popularity.desc (SORTS, line 265), at least
           10 votes (spec min_votes, build.py:342), released only: release_date.lte today (build.py:347-354) and for
           movies release types digital / physical / TV (4|5|6, :354), series status Returning/Ended/Canceled/Pilot
           (:359), no talk shows or news on TV (:360), adult-adjacent keyword exclusions (:361-365, spec
           defaults.without_keywords), the spec deny list, at most 1000 per medium (depth, spec defaults.depth.popular,
           cut at :411), movies and series interleaved (interleave, :566).
  top      "Netflix . Top Rated" = the best-rated titles (movies and series) streaming on Netflix in the US that have
           enough votes to count (a title with 12 votes and a 10 does not count).
           Rule: same discover, sort vote_average.desc, at least 300 votes (movies) / 150 (series) (spec min_votes),
           at least 180 days old (spec min_age_days, build.py:347-348), movies of 40 minutes or more (:355-356), same
           released-only, status, talk/news, keyword and deny rules, at most 500 per medium (depth.top).

TRUTH for recall: a second, claim-only /discover on the same provider ids (no vote floor for Popular; a looser 100-vote
floor for Top so a plain "rated, with some votes" reading is the claim; no release-type / status / age / runtime /
keyword rules), the first TRUTH_K titles per medium. The provider data underneath is TMDB's (JustWatch), the same data
the card is built from, so this checks the card's RULES and CAPS, not TMDB's coverage. TRUTH for precision: the
title's own TMDB /watch/providers (US) for the card's providers and monetization (the check New cards already use)
plus the card's own rules read off the title's details.

Pure functions (classify_*, score, pick_cards) take plain dicts and are unit-tested offline (tests/test_accuracy_frame.py).
"""
import datetime as dt, math, random

import accuracy_corrections as ac

KINDS = {
    'popular': {'folder': 'streaming-popular', 'sort_by': 'popularity.desc', 'truth_votes': 0,
                'claim': 'the most popular titles streaming on the service in the US'},
    'top': {'folder': 'streaming-top', 'sort_by': 'vote_average.desc', 'truth_votes': 100,
            'claim': 'the best-rated titles with enough votes, streaming on the service in the US'},
}
TRUTH_K = 20                       # claim titles per medium (one TMDB page)
STATUS_OK = {'Returning Series', 'Ended', 'Canceled', 'Pilot'}      # discover with_status 0|3|4|5
TV_GENRES_OUT = {10767, 10763}     # talk, news (build.discover)
LABELS = ('code bug', 'deliberate rule', 'cap', 'source limit')
DETAIL_APPEND = 'watch/providers,keywords,release_dates'            # one call per title carries every fact needed


def pick_cards(spec, kind, n, seed):
    """The first n of a seeded shuffle of the kind's cards (stable for a given spec and seed)."""
    cards = [c for c in spec['catalogs'] if KINDS[kind]['folder'] in c.get('folders', []) and c.get('kind') == 'discover']
    cards.sort(key=lambda c: c['slug'])
    random.Random(f'{seed}-{kind}').shuffle(cards)
    return sorted(cards[:n], key=lambda c: c['slug'])


def card_rules(kind, card, media, spec):
    """The numbers the card's rules use for this medium (read from the spec, not copied)."""
    votes = card.get('min_votes', 10)
    votes = votes[media] if isinstance(votes, dict) else votes
    d = spec['defaults']
    kw = {int(x) for x in str(d.get('without_keywords', '')).split('|') if x}
    if media == 'series':
        kw |= {int(x) for x in str(d.get('without_keywords_tv', '')).split('|') if x}
    return {'min_votes': votes, 'min_age_days': card.get('min_age_days', 0),
            'min_runtime': 40 if (media == 'movie' and card['sort'] in ('top', 'votes')) else 0,
            'depth': card.get('depth') or d['depth'][card['sort']], 'keywords_out': kw,
            'deny': {e['tmdb'] for e in d.get('deny', []) if e['type'] == media}}


def provider_params(card, media):
    p = card['movie' if media == 'movie' else 'series']
    return {'watch_region': p['watch_region'], 'with_watch_providers': p['with_watch_providers'],
            'with_watch_monetization_types': p['with_watch_monetization_types']}


def truth_params(kind, card, media):
    """The claim-only discover for this card and medium (no release-type / status / age / runtime / keyword rules)."""
    k = KINDS[kind]
    p = {'sort_by': k['sort_by'], 'include_adult': 'false', **provider_params(card, media)}
    if k['truth_votes']:
        p['vote_count.gte'] = k['truth_votes']
    return p


def facts(det, media):
    """The title facts the labels need, from one /movie/{id} or /tv/{id} call made with DETAIL_APPEND."""
    wp = (det.get('watch/providers') or {}).get('results', {}).get('US', {})
    types = {t['type'] for c in (det.get('release_dates') or {}).get('results', []) for t in c.get('release_dates', [])}
    kws = (det.get('keywords') or {})
    kws = kws.get('keywords') if media == 'movie' else kws.get('results')
    return {'votes': det.get('vote_count') or 0, 'rating': det.get('vote_average') or 0, 'runtime': det.get('runtime') or 0,
            'date': det.get('release_date') if media == 'movie' else det.get('first_air_date'),
            'status': det.get('status'), 'genres': {g['id'] for g in det.get('genres', [])},
            'keywords': {k['id'] for k in kws or []}, 'release_types': types,
            'providers': {kd: {p['provider_id'] for p in v} for kd, v in wp.items() if isinstance(v, list)},
            'id': det.get('id'), 'name': det.get('title') or det.get('name')}


def on_provider(f, card, media):
    p = provider_params(card, media)
    ids = {int(i) for i in str(p['with_watch_providers']).split('|')}
    return any(f['providers'].get(kd, set()) & ids for kd in str(p['with_watch_monetization_types']).split('|'))


def rule_breaks(f, media, r, today):
    """The card's own rules a title fails (empty list = the title may be on the card). Text = the rule in words."""
    out = []
    if not f['date'] or f['date'] > today:
        out.append('not released yet (released-only rule)')
    if f['votes'] < r['min_votes']:
        out.append(f"{f['votes']} votes, floor {r['min_votes']}")
    if r['min_age_days'] and f['date'] and f['date'] > (dt.date.fromisoformat(today) - dt.timedelta(days=r['min_age_days'])).isoformat():
        out.append(f"released {f['date']}, under {r['min_age_days']} days old")
    if media == 'movie':
        if r['min_runtime'] and f['runtime'] < r['min_runtime']:
            out.append(f"runtime {f['runtime']} min, floor {r['min_runtime']}")
        if not f['release_types'] & {4, 5, 6}:
            out.append('no digital / physical / TV release (release types 4|5|6)')
    else:
        if f['status'] not in STATUS_OK:
            out.append(f"status {f['status']}")
        if f['genres'] & TV_GENRES_OUT:
            out.append('talk show / news (TV genre exclusion)')
    if f['keywords'] & r['keywords_out']:
        out.append('adult-adjacent keyword exclusion')
    if f['id'] in r['deny']:
        out.append('spec deny list')
    return out


def classify_missing(f, media, r, today, medium_count, rank):
    """Label ONE claim title the card lacks (rank = its place, 1-based, in the claim list). -> (label, reason).
    A cap can only cut a title whose claim rank is beyond the cap: with the top-20 claim list and caps of 500 / 1000
    that never happens, so 'cap' is reported only when it is possible (the card-level cap exposure is `at_cap`)."""
    breaks = rule_breaks(f, media, r, today)
    if breaks:
        return 'deliberate rule', '; '.join(breaks)
    if medium_count >= r['depth'] and rank > r['depth']:
        return 'cap', f"{media} part of the card is at its cap of {r['depth']}"
    return 'source limit', 'passes every rule and the card is under its cap: TMDB discover paging snapshot (issue #25)'


def classify_extra(f, media, card, r, today):
    """Label ONE sampled card title that does not meet the claim, or None when it does. -> (label, reason) | None."""
    breaks = rule_breaks(f, media, r, today)
    if breaks:
        return 'code bug', 'on the card but breaks its own rule: ' + '; '.join(breaks)
    if not on_provider(f, card, media):
        return 'source limit', "TMDB discover listed it for the provider, the title's own watch/providers (US) does not"
    return None


def score(truth, card_keys):
    """Recall inputs: (found, total, missing keys in truth order). truth = ordered keys, card_keys = any iterable."""
    have = set(card_keys)
    missing = [k for k in truth if k not in have]
    return len(truth) - len(missing), len(truth), missing


def pct(a, b):
    return round(100 * a / b, 1) if b else None


def summarize(kind, cards, recall_found, recall_total, sample, ok, gaps, filled=0):
    """One row per kind for the report table / accuracy.json. gaps = [{'side','label',...}]. filled = claim titles on the
    cards because OUR corrections list added them (#129): shown apart, TMDB-alone recall is `tmdb_recall`."""
    by = {side: {lab: sum(1 for g in gaps if g['side'] == side and g['label'] == lab) for lab in LABELS}
          for side in ('recall', 'precision')}
    return {'kind': kind, 'cards': len(cards), 'truth': recall_total, 'found': recall_found,
            'recall': pct(recall_found, recall_total), 'filled': filled, 'tmdb_recall': pct(recall_found - filled, recall_total),
            'sample': sample, 'ok': ok, 'precision': pct(ok, sample),
            'recall_gaps': by['recall'], 'precision_gaps': by['precision'],
            'code_bugs': by['precision']['code bug'] + by['recall']['code bug']}


def measure(kind, cards, spec, order_of, tmdb, today, n_sample, seed, truth_k=TRUTH_K):
    """Run the frame on the picked cards. order_of(slug) -> ordered keys 'movie:1' / 'series:2' of the whole card;
    tmdb(path, **params) -> json. Returns (summary row, gap rows)."""
    gaps, rf, rt, samp, ok, at_cap, filled = [], 0, 0, 0, 0, [], 0
    cache = {}

    def detail(media, tid):
        if (media, tid) not in cache:
            cache[(media, tid)] = facts(tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}",
                                             append_to_response=DETAIL_APPEND), media)
        return cache[(media, tid)]
    for card in cards:
        slug, order = card['slug'], order_of(card['slug'])
        have = set(order)
        for media in ('movie', 'series'):
            r = card_rules(kind, card, media, spec)
            mcount = sum(1 for k in order if k.startswith(media + ':'))
            truth = []
            for page in range(1, math.ceil(truth_k / 20) + 1):
                res = tmdb(f"/discover/{'movie' if media == 'movie' else 'tv'}", page=page, **truth_params(kind, card, media))
                truth += [(f"{media}:{x['id']}", x.get('title') or x.get('name')) for x in res.get('results', [])]
            truth = truth[:truth_k]
            truth_keys = [k for k, _ in truth]
            if mcount >= r['depth']:
                at_cap.append(f'{slug}:{media}')
            found, total, missing = score(truth_keys, have)
            filled += ac.filled(slug, truth_keys, have)
            rf += found
            rt += total
            names = dict(truth)
            for k in missing:
                lab, why = classify_missing(detail(media, int(k.split(':')[1])), media, r, today, mcount, truth_keys.index(k) + 1)
                gaps.append({'kind': kind, 'card': slug, 'side': 'recall', 'key': k, 'name': names[k], 'label': lab, 'reason': why})
        pool = [k for k in order]
        rng = random.Random(f'{seed}-{slug}')
        for k in rng.sample(pool, min(n_sample, len(pool))):
            media, tid = k.split(':')
            f = detail(media, int(tid))
            samp += 1
            bad = classify_extra(f, media, card, card_rules(kind, card, media, spec), today)
            if bad:
                gaps.append({'kind': kind, 'card': slug, 'side': 'precision', 'key': k, 'name': f['name'], 'label': bad[0],
                             'reason': bad[1]})
            else:
                ok += 1
    row = summarize(kind, cards, rf, rt, samp, ok, gaps, filled)
    row['at_cap'] = at_cap                         # card parts that hold exactly their depth: titles beyond it are cut
    return row, gaps
