# Strand Discovery

Discovery catalogs (streaming services, genres, franchises, directors, actors…) generated daily from TMDB and
served as a static Stremio catalog addon via GitHub Pages, plus uniform card artwork for the Strand media app.

- `generator/` — builds the catalogs (`spec.json` describes every card).
- `art/` — generated card artwork, fonts and their licenses.
- `.github/workflows/` — daily build and deploy, weekly status.

Personal, non-commercial use only.

<img src="art/tmdb-logo.svg" alt="TMDB" width="160">

This product uses the TMDB API but is not endorsed or certified by TMDB.

Streaming availability (which titles are on which service) is watch-provider data from
[JustWatch](https://www.justwatch.com), provided through TMDB.
Subscription services count titles included in the subscription; Tubi and Pluto TV count free/ad-supported titles.
Plex cards also include titles Plex rents (the owner's choice: playback is via debrid).

## Operations

### What runs when (UTC)
| Workflow | When | What it does |
|---|---|---|
| `build-and-deploy` | daily 07:17 | first_seen logger (date each title was first seen on each service), then the generator, the guards, the run report, and a Pages deploy. An `alert` job keeps the "health" issue in sync. |
| `weekly-status` | Mondays 08:43 | commits a counts-only `status/last-build.txt` so GitHub keeps the scheduled workflows enabled (they are disabled after 60 days without repository activity). |
| `yearly-refresh` | October 1, 09:23 | re-reads the Wikipedia award lists, pushes `awards-refresh-<year>` if they changed, and opens a review issue with the double-source cross-check; opens the people-list review issue. |

Scheduled deploys stay off unless the repo variable `SD_SCHEDULE_DEPLOY` is `on` (the build still runs and alerts).
Manual runs: *Actions → build-and-deploy → Run workflow* (`deploy` off = dry run).

### The "health" issue
One open issue labelled `health` collects problems; GitHub emails the owner on each new issue or comment, and the
issue closes itself after the next clean run. **A failed build never replaces the live catalogs** - the last good
deploy stays served.

| Alert | Meaning | What to do |
|---|---|---|
| TMDB rejected the API key (HTTP 401) | the TMDB key was revoked or rotated | create a new key on themoviedb.org (Settings → API), save it in the owner's secrets folder, then `gh secret set TMDB_API_KEY -R xBOBxSAGETx/strand-discovery < <that file>`, and re-run the workflow |
| Build job ended failure · Guard: … | a safety check stopped the deploy: more than 40,000 TMDB requests in one run, empty catalogs, the manifest count differs from the spec, total items fell > 30%, or > 10 catalogs lost > 70% | read the run report artifact (`report.csv`, `summary.json`). A TMDB outage clears on the next day's run. If catalogs were removed on purpose, re-run with `allow_catalog_removal` |
| Build job ended failure · Last log line: … | the generator crashed or was cancelled | open the run log; re-run once; if it repeats, fix the code |
| first_seen logger ended failure | New cards fall back to release-date order for that day | nothing, if the next run is clean; if it repeats, check the logger step's log |
| first_seen warning / skipped / dropped | a service's catalogue shrank > 20%, churned > 15%, had no arrivals for 7+ days, or was skipped for time/request budget; "dropped" = a service removed from the spec left the state (expected once; more than 5 at once are kept and warned instead) | usually a TMDB/JustWatch data hiccup; check the service's New card if it persists |
| first_seen state was not restored | the cache and the encrypted backup were both unavailable, so today's run started a fresh baseline (New order restarts its 14-day warm-up) | check that the secret `SD_STATE_KEY` still matches the owner's saved key |

**Day-15 reminder:** once first_seen has 15 days of history, the alert job opens one `reminder` issue (once only) to
re-score the New cards against the services' official arrival lists.

### Rolling back a bad deploy
Pages serves whatever the last successful deploy published. To go back: revert the bad commit on `main` and run
`build-and-deploy`; or, if the code is fine and only the data was bad, re-run the `deploy` job of the last good run
(*that run → Re-run jobs → deploy*; its Pages artifact is kept 1 day) or simply run the workflow again.

### New on X: arrival dates
TMDB says what is on a service, not when it arrived, so the New cards are ordered by the date a title ARRIVED:
1. **Published schedules** (`generator/arrivals.py`, daily step "Arrival signals", after the logger):
   What's on Netflix (weekly "New on Netflix This Week" roundups, dated at the week's start, and "Netflix Adds ... for
   <Month> 1st"), whatsondisneyplus.com (Disney+ US / Hulu / HBO Max monthly lists), Vital Thrills (monthly
   schedules, via its streaming-schedule tag feed; post sitemaps for the backfill), Film-Book (streaming-schedule
   category feed), and the Plex blog ("New on Plex in <Month>").
2. **Our own daily snapshots** (first_seen): the day we first saw a title on the service (after 14 days of history).
3. Otherwise newest releases (the original behaviour).
A card switches to arrival order once it has 5 dated arrivals in the last 45 days. A schedule entry dated in the
future is shown only after its date AND once the title is really on the service (JustWatch data via TMDB); titles are
matched to TMDB by exact title only (anything ambiguous is left out). New seasons of returning shows count.
Day-dated items from a published schedule are trusted for the whole 45-day window. Week/month roundup items (e.g. the
weekly What's on Netflix recap, Plex's monthly list) need confirmation after 7 days: TMDB (JustWatch) must list the
title on the service, otherwise it leaves the card. An item dated after the day our snapshots began, for a title
that was already on the service that day and never left, is a pre-listing or a rotation, not an arrival: it is
dropped whatever its precision. A new season of such a show still counts: season 2 or later AND at least the latest
season TMDB shows as aired by that day (specials and seasons without an air date don't count as aired); a re-listed
older season is dropped. (A Netflix-network series that TMDB has no US provider data for at
all counts as on Netflix; the same for an HBO / Max-network series on HBO Max. Presence is per app: a title JustWatch
lists only on a sibling service - Hulu for Disney+ - is not confirmed.) Optional rule, off by default: repo variable
`SD_ARRIVALS_SINGLE_SOURCE_DROP=1` drops a day-dated item listed by only one schedule and still not on the service 21
days after its date. Only titles, years, seasons, dates and post URLs are kept from the sources - not their text.
Politeness: identifiable User-Agent, at least 3 s between requests to any site (Film-Book's 5 s crawl delay honoured;
Vital Thrills 5 s), conditional GET from a private cache, never search URLs; an HTTP 429/503 is honoured once
(Retry-After) and a second one stops that site for the run (one health warning); the backfill fetches at most 20
Vital Thrills posts per run. Departure sections of a schedule ("Leaving …") are never read as arrivals, nor is a post
whose dated items mostly fall on a month's last day (a leaving list without a "Leaving" heading).
Month-level lists ("Available in October", "New on Plex in September") are dated the 1st of the month and shown from
that day. Not read: Pluto TV posts (prose, not a list), the Criterion Channel's monthly post (collection lists that
mix new and existing films), and BBC Select sections (a separate subscription); Discovery+, Netflix Kids and MagellanTV
have no published schedule source, so their New cards use our own daily snapshots only.
Personal, non-commercial use. Only TMDB ids and dates end up in the
catalogs - the sources' text is never republished. A failing source or a format change shows in the health issue and
the cards fall back to the stored dates and the other sources (never empty).
First run: *Actions -> build-and-deploy -> Run workflow* with `arrivals_backfill` on reads the last 3 months once
(about 30k TMDB requests, 25 minutes); the daily run reads only recent posts.

### Secrets and variables (names only)
- Secrets: `TMDB_API_KEY` (TMDB v3 key), `SD_STATE_KEY` (encrypts the first_seen backup). Planned: Movie of the Night
  and Watchmode keys (accuracy monitoring).
- Variables: `SD_SCHEDULE_DEPLOY` (`on` = scheduled deploys), `SD_KINDS_OFF` / `SD_KEEP` (staged rollout).
- The values live only in the owner's secrets folder on their PC (and in GitHub's encrypted secrets); never in this
  repo. The people-list source CSV and the Strand shelf files are private too and are not in this repo.

### first_seen state
Lives in the Actions cache, with an encrypted 90-day artifact backup (`first-seen-state`, AES-256, key in
`SD_STATE_KEY`). The job restores from the cache, else from the newest backup, else starts a fresh baseline. If the key
is lost, only the backup becomes unreadable; the cache keeps working, and a new key can be set at any time.

When a service's query changes (provider ids added, e.g. Amazon / Apple / Roku channels, or its monetization), titles
that were on the service all along become visible: on that run they are recorded as baseline, never as arrivals, so the
New card does not flood; stored arrival dates are untouched. **Deliberate:** state saved before this rule existed counts
as changed once, so its first run records every newly seen title as baseline - do not "fix" this (see the no-flood test
in the owner's build notes).
