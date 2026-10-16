"""Accuracy audit 4/6 (issue #95, loop #33): Genre cards (63: Popular / New / Top Rated per genre) and Theme cards (13),
measured against WIKIDATA, not against TMDB genre ids or keywords.

The cards are built from TMDB genre ids / keywords (build.discover), so TMDB's own tags would be a circular truth.
Wikidata is edited separately: a film's or series' genre (P136), with its TMDB id (P4947 movie / P4983 TV series).
Same frame as accuracy_frame.py / accuracy_people.py: seeded cards, RECALL, PRECISION, one label per gap (code bug /
deliberate rule / cap / source limit); the summary row has the same shape, so the check, the baseline and the report
table treat these kinds like the others.

THE CLAIMS (what the card name promises, in plain words) and THE RULES in code (generator/build.py line numbers):
  genre popular  "Action . Popular" = the well-known action movies and series, most popular first.
                 Rule, build.discover() :330-395: TMDB /discover with_genres (the card's genre id, per medium: spec.json),
                 sort popularity.desc, at least 10 votes for movies / 50 for series (spec min_votes), released only
                 (:347-354), movies with a digital / physical / TV release (:354), series with a normal status (:358),
                 no talk / news (:359), no adult-adjacent keywords (:360-366); then the MAIN-GENRE rule (issue #52,
                 GENRE_K :300-306, genre_fits :316, main_genres :328): the title is kept only when the card's genre
                 is among its first 3 TMDB tags (first 2 for Horror and Action, any position for Animation); at most
                 1000 per medium (spec defaults.depth.popular); Anime is the keyword 210024, no genre filter.
  genre new      "Action . New" = the newest action titles: same rules, sort by release date, at least 5 votes, at most
                 500 per medium.
  genre top      "Action . Top Rated" = the best-rated action titles: sort vote_average, at least 300 votes (movies) /
                 150 (series), released at least 180 days ago, movies of 40 minutes or more (:355-356), at most 500.
  theme          "Theme . Zombies" = titles about zombies: TMDB /discover with_keywords (one keyword or several,
                 spec.json), popular first, at least 10 votes, same release / status / adult rules, at most 1000 per
                 medium, PLUS the hand-picked must-have titles of generator/theme_must_haves.json put first and never
                 cut (build.add_must_haves :423, build_one :936).

TRUTH. Genre of a title = Wikidata P136 (or its instance-of P31, e.g. "animated series"), counted for a card when the
value is the mapped class or one of its subclasses (P279*). GENRES / THEMES below map each card to Wikidata classes;
the mapping is ours (TMDB and Wikidata cut genres differently: TMDB's Action & Adventure / Sci-Fi & Fantasy / War &
Politics TV genres are mapped to the union of both Wikidata genres), and genre is partly a matter of taste, so a
disagreement is a source limit, never a card error. A title Wikidata has no genre statement for is UNJUDGED (left out
of the denominator), not correct.
  recall    the claim's notable titles per medium: the first 20 Wikidata items of the genre with a TMDB id, most notable
            first (most Wikipedia language editions = the sitelinks count; Wikidata has no popularity or rating), as:
            Popular = all time; New = released in the last 180 days; Top Rated = the same notable list, kept only when
            TMDB rates it 8.0 or more (Wikidata has no rating; the 8.0 is ours). How many are on the card, anywhere.
  precision 20 seeded card titles per card: Wikidata confirms the genre = ok; Wikidata files it only under other genres
            = a gap (source limit); no genre statement = unjudged. A sampled title that breaks the card's own rules
            (re-read from TMDB) is a code bug.
  themes    recall = the same, with the theme's Wikidata genre (superhero film, zombie film, ...). Two themes have no
            Wikidata genre (Robots & AI, Myths & Legends): no truth, listed. PRECISION: Wikidata can confirm a theme
            but never contradict it (it files a film under "drama film", not under every topic), so there is NO
            TRUTH for theme precision: reported as confirmed / not confirmed counts, and as an own-rule check (is the
            keyword on the title; must-haves excused). The must-have list IS what the build adds, so its recall only
            shows the build applies it (reported apart, not in the headline recall).

LABELS of a missing title (recall): TMDB does not tag it with the card's genre / keyword = source limit (Wikidata and TMDB
disagree); a rule above drops it = deliberate rule (the reason says which); it passes every rule, the card holds its
depth = cap; it passes every rule and the card is under its depth = source limit (TMDB discover paging snapshot, issue #25).

Pure functions take plain dicts and are unit-tested offline (tests/test_accuracy_genres.py).
"""
import datetime as dt, random, re
import build
import accuracy_frame as af

KINDS = ('genre', 'theme')
TRUTH_K = 20                 # claim titles per medium
CANDIDATES = 40              # Wikidata rows read per query (Top Rated keeps the 8.0+ ones of them)
NEW_DAYS = 180               # "New" = released in the last 180 days
TOP_RATING = 8.0             # "Top Rated" claim: TMDB vote_average of 8.0 or more (ours; Wikidata has no rating)
FILM, TV = 'movie', 'series'

# TMDB genre card (slug without genre- / -new / -top) -> Wikidata classes, per medium. The label of each Q id was read
# off Wikidata (wbsearchentities / rdfs:label) when this was written.
GENRES = {
    'action': {FILM: ['Q188473'], TV: ['Q343782', 'Q188473']},              # action film / action television series
    'adventure': {FILM: ['Q319221']},                                       # adventure film
    'animation': {FILM: ['Q202866'], TV: ['Q581714']},                      # animated film / animated series
    'anime': {FILM: ['Q20650540', 'Q1107'], TV: ['Q63952888', 'Q1107']},    # anime film / anime television series / anime
    'comedy': {FILM: ['Q157443'], TV: ['Q9335576', 'Q170238', 'Q7696995']},  # comedy film / comedy TV series / sitcom / comedy TV program
    'crime': {FILM: ['Q959790'], TV: ['Q9335577', 'Q56878968', 'Q2321734']},  # crime film / crime TV series / detective TV series / police procedural
    'documentary': {FILM: ['Q93204'], TV: ['Q7603925', 'Q4164344']},        # documentary film / documentary TV program / documentary
    'drama': {FILM: ['Q130232'], TV: ['Q1366112', 'Q1287716', 'Q25372']},   # drama film / drama TV series / drama TV program / drama
    'family': {FILM: ['Q1361932'], TV: ['Q178840']},                        # family film / family television series
    'fantasy': {FILM: ['Q157394']},                                         # fantasy film
    'history': {FILM: ['Q17013749', 'Q542475']},                            # historical film / period drama film
    'horror': {FILM: ['Q200092']},                                          # horror film
    'kids': {TV: ['Q1273568']},                                             # children's television series
    'music': {FILM: ['Q842256']},                                           # musical film
    'mystery': {FILM: ['Q1200678'], TV: ['Q101898470']},                    # mystery film / mystery television series
    'reality': {TV: ['Q182415']},                                           # reality television
    'romance': {FILM: ['Q1054574']},                                        # romance film
    'sci-fi': {FILM: ['Q471839'], TV: ['Q140472311']},                      # science fiction film / science fiction television series
    'thriller': {FILM: ['Q2484376']},                                       # thriller film
    'war': {FILM: ['Q369747'], TV: ['Q369747']},                            # war film
    'westerns': {FILM: ['Q172980'], TV: ['Q7988576']},                      # Western film / Westerns on television
}
# Theme card slug -> Wikidata classes (same for both media). None = Wikidata has no genre for the theme: no truth.
THEMES = {
    'theme-superheroes': ['Q1535153'],                                      # superhero film
    'theme-zombies': ['Q3072049'],                                          # zombie film
    'theme-space-epics': ['Q468478'],                                       # space opera
    'theme-spies': ['Q2297927'],                                            # spy film
    'theme-aliens': ['Q116680334', 'Q116212252'],                           # alien invasion film / extraterrestrial film
    'theme-robots-and-ai': None,                                            # no Wikidata genre found
    'theme-dystopian': ['Q20443008'],                                       # dystopian film
    'theme-disaster': ['Q846544'],                                          # disaster film
    'theme-creature-features': ['Q1342372'],                                # monster film (zombie film is a subclass)
    'theme-psychological': ['Q109733304', 'Q109629396', 'Q590103'],         # psychological thriller film / horror film / psychological thriller
    'theme-myths-and-legends': None,                                        # no Wikidata genre found
    'theme-heist': ['Q496523'],                                             # heist film
    'theme-time-travel': ['Q104765957'],                                    # time-travel film
}


def base_slug(slug):
    return re.sub(r'^genre-|-(new|top)$', '', slug)


def classes_for(card, media):
    """The Wikidata classes that stand for this card's genre / theme for this medium, or [] (no truth available)."""
    if card['slug'].startswith('theme-'):
        return THEMES.get(card['slug']) or []
    return GENRES.get(base_slug(card['slug']), {}).get(media, [])


def card_tag(card, media):
    """('with_genres' | 'with_keywords', {ids}) the card's TMDB filter for this medium (OR list)."""
    p = card.get(media, {})
    for field in ('with_genres', 'with_keywords'):
        if p.get(field):
            return field, {int(x) for x in re.split(r'[|,]', str(p[field])) if x.strip().isdigit()}
    return None, set()


def pick_cards(spec, kind, n, seed):
    """genre: n cards, a third each of Popular / New / Top Rated (a seeded shuffle per sort); theme: all of them."""
    if kind == 'theme':
        return sorted((c for c in spec['catalogs'] if 'themes' in c.get('folders', [])), key=lambda c: c['slug'])
    out = []
    for sort in ('popular', 'new', 'top'):
        cards = sorted((c for c in spec['catalogs'] if c['kind'] == 'discover' and c['sort'] == sort
                        and any(f.startswith('genres-') for f in c.get('folders', []))), key=lambda c: c['slug'])
        random.Random(f'{seed}-{kind}-{sort}').shuffle(cards)
        out += cards[:n // 3]
    return sorted(out, key=lambda c: c['slug'])


# ---------- Wikidata ----------

def _values(qs):
    return ' '.join(f'wd:{q}' for q in qs)


def truth_query(media, qs, limit=CANDIDATES, since=None):
    """The most notable (sitelinks) items of the genre classes (and their subclasses) with a TMDB id: one row per TMDB id."""
    p = 'P4947' if media == FILM else 'P4983'
    having = f' HAVING (MIN(?d0) >= "{since}T00:00:00Z"^^xsd:dateTime)' if since else ''
    floor = '' if since else ' FILTER(?sl0 >= 3)'
    return (f'SELECT ?id (MAX(?sl0) AS ?sl) (MIN(?d0) AS ?d) WHERE {{ VALUES ?G {{ {_values(qs)} }} ?g wdt:P279* ?G. '
            f'?w (wdt:P136|wdt:P31) ?g. ?w wdt:{p} ?id. ?w wikibase:sitelinks ?sl0.{floor} '
            f'OPTIONAL {{ ?w wdt:P577 ?d1 }} OPTIONAL {{ ?w wdt:P580 ?d2 }} BIND(COALESCE(?d1, ?d2) AS ?d0) }} '
            f'GROUP BY ?id{having} ORDER BY DESC(?sl) LIMIT {int(limit)}')


def parse_truth(rows, media, today):
    """-> [(key, sitelinks, date)] in notability order, unreleased dropped."""
    out = []
    for r in rows:
        date = (r.get('d') or '')[:10]
        if date and date > today:
            continue
        out.append((f"{media}:{r['id']}", int(r.get('sl') or 0), date))
    return out


def judge_query(media, qs, ids):
    """One row per TMDB id: how many genre statements (P136) its item has, and whether it is (a subclass of) the classes."""
    p = 'P4947' if media == FILM else 'P4983'
    vals = ' '.join(f'"{i}"' for i in ids)
    return (f'SELECT ?id (MAX(?hit0) AS ?hit) (COUNT(DISTINCT ?x) AS ?n) WHERE {{ VALUES ?id {{ {vals} }} ?w wdt:{p} ?id. '
            f'OPTIONAL {{ ?w wdt:P136 ?x }} '
            f'BIND(IF(EXISTS {{ VALUES ?G {{ {_values(qs)} }} ?w (wdt:P136|wdt:P31) ?g. ?g wdt:P279* ?G }}, 1, 0) AS ?hit0) }} '
            f'GROUP BY ?id')


def parse_judge(rows, media):
    """-> {key: (genre statements, is_the_genre)}; a TMDB id with no Wikidata item is absent."""
    out = {}
    for r in rows:
        k = f"{media}:{r['id']}"
        n, hit = out.get(k, (0, False))
        out[k] = (max(n, int(r.get('n') or 0)), hit or r.get('hit') == '1')
    return out


# ---------- the card rules, read off the title's TMDB details ----------

def title_facts(det, media):
    f = af.facts(det, media)
    f['order'] = [g['id'] for g in det.get('genres', [])]        # TMDB lists the main genre first
    return f


def tag_missing(f, card, media):
    """True when the title does not carry the card's genre / keyword in TMDB's data."""
    field, ids = card_tag(card, media)
    return not ((f['genres'] if field == 'with_genres' else f['keywords']) & ids)


def rule_breaks(f, media, card, r, today):
    """The card's own rules the title fails (empty = it may be on the card), tag presence NOT included."""
    out = af.rule_breaks(f, media, r, today)
    mains = build.main_genres(card, media)
    if mains and not build.genre_fits(f['order'], mains):
        out.append(f"main-genre rule: genre {sorted(mains)} is not among the title's first tags {f['order'][:3]} (issue #52)")
    own = card.get(media, {})
    if f['keywords'] & {int(x) for x in str(own.get('without_keywords', '')).split('|') if x}:
        out.append("card's own excluded keyword")
    if f['genres'] & {int(x) for x in str(own.get('without_genres', '')).split('|') if x}:
        out.append("card's own excluded genre")
    return out


def classify_missing(f, media, card, r, today, mcount):
    """Label ONE claim title the card lacks. -> (label, reason)."""
    field, ids = card_tag(card, media)
    if tag_missing(f, card, media):
        what = 'genre' if field == 'with_genres' else 'keyword'
        return 'source limit', f'Wikidata files it under the genre; TMDB has no {what} {sorted(ids)} on it (the two sources disagree)'
    breaks = rule_breaks(f, media, card, r, today)
    if breaks:
        return 'deliberate rule', '; '.join(breaks)
    if mcount >= r['depth']:
        return 'cap', f"{media} part of the card is at its cap of {r['depth']}"
    return 'source limit', 'passes every rule and the card is under its cap: TMDB discover paging snapshot (issue #25)'


def classify_extra(f, media, card, r, today):
    """Label ONE sampled card title that breaks the card's own rules, or None. -> (label, reason) | None."""
    breaks = (['no longer carries the card tag'] if tag_missing(f, card, media) else []) + rule_breaks(f, media, card, r, today)
    return ('code bug', 'on the card but breaks its own rule: ' + '; '.join(breaks)) if breaks else None


# ---------- the measurement ----------

def measure_cards(kind, cards, spec, order_of, tmdb, sparql, today, n_sample, seed):
    """Walk the picked cards. order_of(slug) -> ordered keys of the card; tmdb(path, **params) -> json; sparql(query) -> rows.
    Returns (summary row, gap rows). Themes: precision has no truth (see the module doc)."""
    theme = kind == 'theme'
    gaps, rf, rt, samp, ok, unjudged = [], 0, 0, 0, 0, 0
    confirmed = not_confirmed = own_n = own_ok = 0
    mh_found = mh_total = 0
    per_card, no_truth = [], []
    by_sort = {}
    cache = {}
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=NEW_DAYS)).isoformat()

    def detail(media, tid):
        """The title's facts, or None when TMDB does not know the id (a Wikidata id TMDB dropped / merged): never a crash."""
        if (media, tid) not in cache:
            try:
                cache[(media, tid)] = title_facts(tmdb(f"/{'movie' if media == FILM else 'tv'}/{tid}", append_to_response=af.DETAIL_APPEND), media)
            except RuntimeError as e:
                if 'HTTPError' not in str(e):
                    raise
                cache[(media, tid)] = None
        return cache[(media, tid)]
    for card in cards:
        slug, order = card['slug'], order_of(card['slug'])
        have = set(order)
        sort = card['sort']
        c = {'card': slug, 'items': len(order), 'truth': 0, 'found': 0, 'judged': 0, 'ok': 0, 'confirmed': 0, 'own_ok': 0, 'own_n': 0}
        must = set()
        for media in card['media']:
            r = af.card_rules(kind, card, media, spec)
            mcount = sum(1 for k in order if k.startswith(media + ':'))
            must |= {f'{media}:{i}' for i in build.must_haves(card, media)} if theme else set()
            qs = classes_for(card, media)
            if not qs:
                no_truth.append(f'{slug}:{media}')
                continue
            cand = parse_truth(sparql(truth_query(media, qs, since=since if sort == 'new' else None)), media, today)
            keys = [k for k, _, _ in cand]
            if sort == 'top':                         # the claim is the well-rated ones; Wikidata has no rating: TMDB's
                keys = [k for k in keys if (detail(media, int(k.split(':')[1])) or {}).get('rating', 0) >= TOP_RATING]
            truth = keys[:TRUTH_K]
            found, total, missing = af.score(truth, have)
            c['found'] += found
            c['truth'] += total
            for k in missing:
                f = detail(media, int(k.split(':')[1]))
                lab, why = classify_missing(f, media, card, r, today, mcount) if f else ('source limit', 'TMDB does not know this id (Wikidata lists it)')
                gaps.append({'kind': kind, 'card': slug, 'side': 'recall', 'key': k, 'name': f['name'] if f else k, 'label': lab, 'reason': why})
        if theme:                                     # the hand list the build adds: recall shows the build applies it
            for media in card['media']:
                ids = build.must_haves(card, media)
                mh_total += len(ids)
                mh_found += sum(1 for i in ids if f'{media}:{i}' in have)
                for i in ids:
                    if f'{media}:{i}' not in have:
                        f = detail(media, i)
                        released = bool(f) and bool(f['date']) and f['date'] <= today
                        gaps.append({'kind': kind, 'card': slug, 'side': 'must_have', 'key': f'{media}:{i}', 'name': f['name'] if f else f'{media}:{i}',
                                     'label': 'code bug' if released else 'source limit',
                                     'reason': 'hand-picked must-have is not on its card' if released else 'not released / unknown to TMDB'})
        rf += c['found']
        rt += c['truth']
        sample = random.Random(f'{seed}-{slug}').sample(list(order), min(n_sample, len(order)))
        judge = {}
        for media in card['media']:
            qs = classes_for(card, media)
            ids = [k.split(':')[1] for k in sample if k.startswith(media + ':')]
            if qs and ids:
                judge.update(parse_judge(sparql(judge_query(media, qs, ids)), media))
        for k in sample:
            media, tid = k.split(':')
            f = detail(media, int(tid))
            if not f:                                 # on the card but TMDB no longer knows the id: not judged
                unjudged += 1
                continue
            r = af.card_rules(kind, card, media, spec)
            bad = None if k in must else classify_extra(f, media, card, r, today)      # a must-have is excused from the rules
            if theme:
                c['own_n'] += 1
                own_n += 1
                if bad:
                    gaps.append({'kind': kind, 'card': slug, 'side': 'precision', 'key': k, 'name': f['name'], 'label': bad[0], 'reason': bad[1]})
                else:
                    c['own_ok'] += 1
                    own_ok += 1
                n, hit = judge.get(k, (0, False))
                confirmed += hit
                not_confirmed += not hit
                c['confirmed'] += hit
                continue
            if bad:
                gaps.append({'kind': kind, 'card': slug, 'side': 'precision', 'key': k, 'name': f['name'], 'label': bad[0], 'reason': bad[1]})
                samp += 1
                c['judged'] += 1
                continue
            n, hit = judge.get(k, (0, False))
            if not (n or hit):
                unjudged += 1                         # Wikidata has no genre statement for it: nothing to compare with
                continue
            samp += 1
            c['judged'] += 1
            if hit:
                ok += 1
                c['ok'] += 1
            else:
                gaps.append({'kind': kind, 'card': slug, 'side': 'precision', 'key': k, 'name': f['name'], 'label': 'source limit',
                             'reason': 'Wikidata files it under other genres, not this one (TMDB tags it with the card genre; genre is partly taste)'})
        c['recall'] = af.pct(c['found'], c['truth'])
        c['precision'] = af.pct(c['ok'], c['judged']) if not theme else None
        per_card.append(c)
        s = by_sort.setdefault(sort, {'cards': 0, 'found': 0, 'truth': 0, 'judged': 0, 'ok': 0})
        for key, v in (('cards', 1), ('found', c['found']), ('truth', c['truth']), ('judged', c['judged']), ('ok', c['ok'])):
            s[key] += v
    row = af.summarize(kind, cards, rf, rt, samp, ok, gaps)
    row.update({'per_card': per_card, 'unjudged': unjudged, 'no_truth': no_truth})
    if theme:
        row.update({'precision': None, 'confirmed': confirmed, 'not_confirmed': not_confirmed,
                    'own_rule': {'ok': own_ok, 'n': own_n}, 'must_have': {'found': mh_found, 'total': mh_total, 'recall': af.pct(mh_found, mh_total)}})
    else:
        row['by_sort'] = {s: dict(v, recall=af.pct(v['found'], v['truth']), precision=af.pct(v['ok'], v['judged'])) for s, v in by_sort.items()}
    return row, gaps
