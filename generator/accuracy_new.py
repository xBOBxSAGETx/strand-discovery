"""Accuracy frame, Streaming New (issue #97, audit job 6 of 6 of #33): recall + precision of EVERY service's "New" card, and the
label of the dated-precision errors accuracy_check.precision() finds.

THE CLAIM: "Netflix . New" = what arrived on Netflix in the US in the last 45 days (movies and series).
THE RULES in code (generator/build.py): three ways to build the 21 cards, chosen per service by the build (summary.json
new_card_modes): 'arrivals' (>= 5 dated schedule signals: dated arrivals first, then titles first seen, then a release-date
discover fill), 'first_seen' (titles the daily crawl first saw on the service inside the window), 'release_date' (discover
by release date). Signal-dated arrivals and first-seen titles bypass the vote floor (no floor: "arrivals are often
obscure") but are released-only (details_preview) and the crawl drops adult-keyword, talk and news titles
(first_seen.base_params). The card holds at most depth * media titles (spec depth.new = 500 per medium).

TRUTH for recall, per service (accuracy.json says which):
  official  the service's own published arrival list (official_arrivals.csv, accuracy_check.recall(); September only, so 4
            services, stale by late October and not refreshable without a TMDB key: titles must be resolved to ids).
  tmdb-window  all 21 services: TMDB /discover on the card's own provider ids and monetization (watch_region US), movies
            released in the last 45 days, series first aired in the last 45 days, popularity.desc, the first TRUTH_K per
            medium. It is the same TMDB provider data the card is built from, so the row is CIRCULAR (circular=True) and
            reported apart: it checks the card's rules, state and caps, not TMDB's coverage. No independent truth exists
            for the 17 services without an official list.
Recall = the truth titles anywhere on the card. A series' and a movie's own watch/providers (US) decide `on_provider`.
TRUTH for precision: the title's own TMDB watch/providers (US) for the card's provider ids and monetization (Netflix / HBO
Max series of the service's own network with no US provider data count as on the service: rules F2 / F3), plus released-only,
plus (first_seen / release_date cards) the crawl's adult-keyword and talk / news exclusions. A seeded PRECISION_N of the WHOLE
card (accuracy_check.precision() already checks the dated items of the arrivals cards).

LABELS (same four): code bug = the card breaks its own rule (unreleased title, excluded keyword); deliberate rule = a written
rule keeps a title out / lets a dated one stay (card rules, the first_seen window, F1: a day-dated schedule item stays for the
45-day window although the service dropped it); cap = the card holds depth * media titles; source limit = TMDB disagrees
with itself or the crawl never saw the title (discover paging, issue #25).
Pure functions (window_params, classify_*, label_dated_error) take plain dicts and are unit-tested offline
(tests/test_accuracy_new.py).
"""
import datetime as dt, random

import accuracy_frame as af
import accuracy_corrections as ac

WINDOW = 45                        # the New card's arrival window (arrivals.WINDOW)
TRUTH_K = 40                       # truth titles per medium (two TMDB pages)
PRECISION_N = 20                   # card titles checked per card
ORPHAN_NETWORKS = {'netflix': {213}, 'hbo-max': {49, 3186, 8304}}      # arrivals.py F2 / F3 (as accuracy_check.ORPHAN_NETWORKS)
DETAIL = 'watch/providers,keywords,release_dates'


def svc_of(card):
    return card['slug'][len('streaming-'):-len('-new')]


def window_params(card, media, today):
    """The claim-only discover: on the card's provider, released / first aired inside the window, most popular first."""
    lo = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW)).isoformat()
    p = {'sort_by': 'popularity.desc', 'include_adult': 'false', **af.provider_params(card, media)}
    field = 'primary_release_date' if media == 'movie' else 'first_air_date'
    p.update({f'{field}.gte': lo, f'{field}.lte': today})
    return p


def new_facts(det, media):
    f = af.facts(det, media)
    f['networks'] = {n['id'] for n in det.get('networks') or []}
    f['has_wp'] = bool((det.get('watch/providers') or {}).get('results', {}).get('US'))
    return f


def on_service(f, card, media, svc):
    """On the service per the title's own data, or (Netflix / HBO Max series) of the service's own network with no US data."""
    if af.on_provider(f, card, media):
        return True
    return media == 'series' and svc in ORPHAN_NETWORKS and not f['has_wp'] and bool(ORPHAN_NETWORKS[svc] & f['networks'])


def card_checks(card, spec, media, mode):
    """The rules a New card applies to every title: released-only always; the crawl's keyword + talk / news exclusions on
    first_seen / release_date cards. Floors (votes, release types, status) are NOT applied: signal and first-seen titles
    have none."""
    r = af.card_rules('new', card, media, spec)
    r = dict(r, min_votes=0, min_age_days=0, min_runtime=0, deny=set())
    if mode == 'arrivals':
        r['keywords_out'] = set()
    return r


def breaks(f, media, r, today, mode):
    out = []
    if not f['date'] or f['date'] > today:
        out.append('not released yet (released-only rule)')
    if f['keywords'] & r['keywords_out']:
        out.append('adult-adjacent keyword exclusion')
    if media == 'series' and mode != 'arrivals' and f['genres'] & af.TV_GENRES_OUT:
        out.append('talk show / news (TV genre exclusion)')
    return out


def state_item(items, key):
    m, i = key.split(':')
    return (items or {}).get(('m:' if m == 'movie' else 't:') + i)


def classify_missing(f, media, card, r, mode, item, lo, today, mcount, cap, svc):
    """ONE truth title the card lacks. item = first_seen state entry [first_seen, last_seen] | None. -> (label, reason)."""
    bad = breaks(f, media, r, today, mode)
    if bad:
        return 'deliberate rule', '; '.join(bad)
    if not on_service(f, card, media, svc):
        return 'source limit', "TMDB discover listed it for the provider, the title's own watch/providers (US) does not"
    if mode in ('first_seen', 'arrivals'):
        if item and (item[0] == 'baseline' or item[0] < lo):
            return 'deliberate rule', (f"first seen on the service {item[0]}, before the {WINDOW}-day window: the "
                                       f"{mode} card shows titles first seen / dated inside the window")
        if not item:
            return 'source limit', "the daily crawl never saw it on the service (not in the first_seen state)"
    if mcount >= cap:
        return 'cap', f"the card holds its cap of {cap}"
    return 'source limit', 'passes every rule and the card is under its cap: no rule explains it; labelled source limit by elimination (TMDB discover paging, issue #25; not verified)'


def classify_extra(f, media, card, r, mode, cand, item, today, svc):
    """ONE sampled card title. -> (label, reason) for a gap, or None when it meets the claim. cand = a dated arrival
    candidate of arrivals.py {'date','precision','sources'} | None."""
    bad = breaks(f, media, r, today, mode)
    if bad:
        return 'code bug', 'on the card but breaks its own rule: ' + '; '.join(bad)
    if on_service(f, card, media, svc):
        return None
    if cand:
        left = f" (the service's catalogue last listed it {item[1]})" if item else ''
        return 'deliberate rule', (f"dated {cand['date']} by a published schedule and kept for the {WINDOW}-day window (rule F1) "
                                   f"although the title's own watch/providers (US) no longer lists it{left}")
    return 'source limit', "on the card, but the title's own watch/providers (US) does not list the provider (TMDB disagrees with itself)"


def label_dated_error(item, cand, run_date, today):
    """Label of ONE accuracy_check precision ERROR (ii): a day-dated schedule item, not on the service, older than the lag
    allowance. item = first_seen state entry | None; cand = its arrivals candidate; run_date = the provider's last crawl.
    -> (label, reason). The gate itself is unchanged."""
    age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(cand['date'])).days
    if item and run_date and item[1] < run_date:
        return 'deliberate rule', (f"rule F1: a day-dated schedule item stays on the card for the {WINDOW}-day window; dated "
                                   f"{cand['date']} ({age} days ago), the service's catalogue last listed it {item[1]} (left since)")
    if item:
        return 'source limit', (f"dated {cand['date']}; the last crawl ({item[1]}) still listed it for the provider but its own "
                                f"watch/providers (US) lacks it: TMDB disagrees with itself")
    return 'source limit', (f"dated {cand['date']} ({age} days ago) by a published schedule, but the daily crawl never saw it on "
                            f"the service: the schedule's arrival is not in TMDB's provider data")


# ----- the measure -------------------------------------------------------------------------------------------------

def measure(card, order, spec, tmdb, today, mode, items, cands, seed, n_sample=PRECISION_N, truth_k=TRUTH_K):
    """One New card -> (summary row, gap rows). order = ordered keys of the whole card; tmdb(path, **p) -> json; items = the
    provider's first_seen state items ({'m:1': [first, last]}); cands = {key: candidate} dated arrivals of the provider."""
    svc, slug = svc_of(card), card['slug']
    lo = (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW)).isoformat()
    have = set(order)
    cap = card.get('depth') or spec['defaults']['depth']['new']
    cache, gaps = {}, []

    def detail(media, tid):
        if (media, tid) not in cache:
            try:
                cache[(media, tid)] = new_facts(tmdb(f"/{'movie' if media == 'movie' else 'tv'}/{tid}", append_to_response=DETAIL), media)
            except RuntimeError:
                cache[(media, tid)] = None
        return cache[(media, tid)]
    rf = rt = filled = 0
    at_cap = []
    for media in card['media']:
        r = card_checks(card, spec, media, mode)
        mcount = sum(1 for k in order if k.startswith(media + ':'))
        if mcount >= cap:
            at_cap.append(f'{slug}:{media}')
        truth = []
        for page in range(1, truth_k // 20 + 1):
            res = tmdb(f"/discover/{'movie' if media == 'movie' else 'tv'}", page=page, **window_params(card, media, today))
            truth += [(f"{media}:{x['id']}", x.get('title') or x.get('name')) for x in res.get('results', [])]
            if page >= res.get('total_pages', 0):
                break
        truth = truth[:truth_k]
        found, total, missing = af.score([k for k, _ in truth], have)
        filled += ac.filled(slug, [k for k, _ in truth], have)
        rf += found
        rt += total
        names = dict(truth)
        for k in missing:
            f = detail(media, int(k.split(':')[1]))
            if f is None:
                lab, why = 'source limit', 'TMDB returned no such title'
            else:
                lab, why = classify_missing(f, media, card, r, mode, state_item(items, k), lo, today, mcount, cap, svc)
            gaps.append({'kind': 'new-' + svc, 'card': slug, 'side': 'recall', 'key': k, 'name': names[k], 'label': lab, 'reason': why})
    picked = random.Random(f'{seed}-{slug}').sample(list(order), min(n_sample, len(order)))
    ok = 0
    for k in picked:
        media, tid = k.split(':')
        f = detail(media, int(tid))
        if f is None:
            gaps.append({'kind': 'new-' + svc, 'card': slug, 'side': 'precision', 'key': k, 'name': k, 'label': 'source limit',
                         'reason': 'TMDB returned no such title'})
            continue
        bad = classify_extra(f, media, card, card_checks(card, spec, media, mode), mode, cands.get(k), state_item(items, k), today, svc)
        if bad:
            gaps.append({'kind': 'new-' + svc, 'card': slug, 'side': 'precision', 'key': k, 'name': f['name'], 'label': bad[0], 'reason': bad[1]})
        else:
            ok += 1
    row = af.summarize('new-' + svc, [card], rf, rt, len(picked), ok, gaps, filled)
    row.update({'at_cap': at_cap, 'mode': mode, 'truth_source': 'tmdb-window', 'circular': True, 'card_size': len(order)})
    return row, gaps
