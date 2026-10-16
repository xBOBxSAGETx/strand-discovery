"""Health alerts for the build workflow (nightly and watcher-started): keeps ONE open GitHub issue labelled "health" in sync with the latest run.

  python health.py <report dir> <build result> <logger outcome> [<arrivals outcome> [<practice> [<deploy result>]]]

<practice> = "true" when the build skipped the logger on purpose (a practice build reusing main's saved state, #73):
only then is a "skipped" logger fine; in every other run it is still reported.

Test mode (no GitHub calls): HEALTH_DRY_RUN=1 prints every write it would make; HEALTH_MOCK_ISSUES=<json file>
({"health": [{"number": 1}], "reminder": [...]}, per label) stands in for `gh issue list`.

<report dir> = the downloaded run-report artifact (out/summary.json, state/fs_summary.json; either may be missing).
One alert per problem streak (#79): builds can run many times a day, so a run whose problems are the same as the last report
on the open issue (numbers ignored) adds no comment (GitHub emails every comment); a changed set does.
#85: only a FAILED build or deploy emails (opens the issue, or adds one comment to it). Routine warnings (logger
failed/skipped, first_seen warnings, stale feeds, arrival sources) go to the run summary and, if an issue is open, its
body (an edit sends no email). Any successful run closes the open issue (no comment), leaving its warnings in the body. Also opens the one-off "re-score New" reminder on day 15 of first_seen
history. Uses the gh CLI with the job's GITHUB_TOKEN (issues: write only).
"""
import hashlib, json, os, re, subprocess, sys
from pathlib import Path

REPO = os.environ['GITHUB_REPOSITORY']
RUN_URL = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{REPO}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
HEALTH_TITLE = 'Daily build health'


DRY = os.environ.get('HEALTH_DRY_RUN') == '1'


def gh(*args, check=True):
    if DRY:
        if args[:2] == ('issue', 'list'):
            mock = read(os.environ.get('HEALTH_MOCK_ISSUES', '')) or {}
            return json.dumps(mock.get(args[args.index('--label') + 1], []))
        print('WOULD RUN: gh ' + ' '.join(args))
        return 'https://github.com/<dry-run>/issues/<new>'
    r = subprocess.run(['gh', *args, '-R', REPO], capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"gh {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def ensure_label(name, color, desc):
    if DRY:
        return
    subprocess.run(['gh', 'label', 'create', name, '-R', REPO, '--color', color, '--description', desc],
                   capture_output=True, text=True)       # "already exists" is fine


def read(p):
    try:
        return json.loads(Path(p).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def problems(report, build_result, logger_outcome, arrivals_outcome='skipped', practice=False):
    out = []
    summary = read(report / 'out' / 'summary.json') or read(report / 'summary.json')
    fs = read(report / 'state' / 'fs_summary.json') or read(report / 'fs_summary.json')
    log = Path(report / 'build.log').read_text(encoding='utf-8', errors='replace') if (report / 'build.log').exists() else ''
    if 'TMDB HTTP 401' in log:
        out.append('**TMDB rejected the API key (HTTP 401).** Renew it and update the repo secret `TMDB_API_KEY`.')
    if build_result != 'success':
        lines = [l.strip() for l in log.splitlines() if l.strip()]
        guard = [l for l in lines if 'GUARD:' in l]
        why = (f" Guard: `{guard[-1][:300]}`" if guard else f" Last log line: `{lines[-1][:300]}`" if lines
               else ' No build log (the job died before the generator ran).')
        out.append(f"Build job ended **{build_result}**.{why} The last good deploy stays served.")
    if logger_outcome != 'success' and not (practice and logger_outcome == 'skipped'):
        out.append(f"first_seen logger ended **{logger_outcome}** - New cards fall back to release-date order today.")
    if arrivals_outcome not in ('success', 'skipped'):
        out.append(f"Arrival signals step ended **{arrivals_outcome}** - New cards use the stored arrival dates "
                   "(and first-seen / release-date order) today.")
    ar = read(report / 'arrivals' / 'arrivals_summary.json')
    for w in (ar or {}).get('warnings', []):
        out.append(f"Arrival source warning: {w[:220]} - New cards fall back to the other sources and the stored "
                   "dates (never empty).")
    if fs:
        out += [f"first_seen warning: {w}" for w in fs.get('warnings', [])]
        out += [f"first_seen skipped: {s}" for s in fs.get('skipped', [])]
        if fs.get('dropped'):          # expected once when services are removed from the spec; closes next run
            out.append(f"first_seen dropped the history of services no longer in the spec: {', '.join(fs['dropped'])}")
        if str(fs.get('state_source', '')).startswith('none') and summary and summary.get('first_seen', {}).get('providers'):
            if any(p.get('history_days', 0) > 0 for p in summary['first_seen']['providers'].values()):
                out.append(f"first_seen state was not restored ({fs['state_source']}).")
    if summary is None and build_result == 'success':
        out.append('Run report is missing (summary.json not found).')
    return out, summary, fs


SIG_RX = re.compile(r'<!-- health-sig:([0-9a-f]{12}) -->')


def signature(found):
    """Fingerprint of the problem list, digits ignored (sizes and counts move every hour, the problem is the same)."""
    return hashlib.sha1('\n'.join(re.sub(r'\d+', '#', p) for p in found).encode()).hexdigest()[:12]


def last_signature(issue):
    """The signature in the newest comment of an open health issue, else in its body, else None."""
    for text in [c.get('body') or '' for c in reversed(issue.get('comments') or [])] + [issue.get('body') or '']:
        m = SIG_RX.search(text)
        if m:
            return m.group(1)
    return None


def main():
    report, build_result, logger_outcome = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    arrivals_outcome = sys.argv[4] if len(sys.argv) > 4 else 'skipped'
    practice = len(sys.argv) > 5 and sys.argv[5] == 'true'
    deploy_result = sys.argv[6] if len(sys.argv) > 6 else 'skipped'
    found, summary, fs = problems(report, build_result, logger_outcome, arrivals_outcome, practice)
    if deploy_result == 'failure':
        found.append('Deploy job ended **failure** - the site was not published; the last good deploy stays served.')
    failed = build_result != 'success' or deploy_result == 'failure'   # only a failure may email (new comment / new issue)
    # arrivals catch-up progress (information, never a problem by itself): run page summary + log, and the issue body
    progress = (read(report / 'arrivals' / 'arrivals_summary.json') or {}).get('catchup_lines') or []
    for line in progress:
        print('progress:', line)
    if os.environ.get('GITHUB_STEP_SUMMARY') and (progress or found):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            if found:
                f.write('### Build health\n' + ''.join(f'- {p}\n' for p in found))
            if progress:
                f.write('### Arrivals catch-up\n' + ''.join(f'- {line}\n' for line in progress))
    ensure_label('health', 'd73a4a', 'Daily build health (opened/closed automatically)')
    existing = json.loads(gh('issue', 'list', '--label', 'health', '--state', 'open', '--json', 'number,body,comments', '--limit', '5') or '[]')
    sig = signature(found)
    if found:
        body = (f"Latest run: {RUN_URL}\n\n" + '\n'.join(f'- {p}' for p in found) +
                ("\n\nArrivals catch-up (information):\n" + '\n'.join(f'- {line}' for line in progress)
                 if progress else '') +
                "\n\nThis issue closes itself after the next successful run. What each alert means: README → Operations."
                f"\n\n<!-- health-sig:{sig} -->")
    if failed:
        # a new comment / new issue is the email: failures only (#85)
        if existing and last_signature(existing[0]) == sig:
            print(f"health: same {len(found)} problems as the last report on issue #{existing[0]['number']}; no new comment")
        elif existing:
            gh('issue', 'comment', str(existing[0]['number']), '--body', body)
            print(f"health: updated issue #{existing[0]['number']} ({len(found)} problems)")
        else:
            print('health:', gh('issue', 'create', '--title', HEALTH_TITLE, '--label', 'health', '--body', body).strip())
    else:
        # build and deploy are fine: routine warnings go to the body (an edit sends no email) and the run summary, and
        # the failure issue closes. No comment, no new issue.
        for i in existing:
            if found:
                gh('issue', 'edit', str(i['number']), '--body', body)
            gh('issue', 'close', str(i['number']))
            print(f"health: closed issue #{i['number']}" + (f" ({len(found)} warnings left in its body)" if found else ''))
        if not existing:
            print('health: clean, no open issue' if not found else
                  f"health: no failure, no open issue ({len(found)} warnings in the run summary)")

    # one-off reminder on day 15 of first_seen history (earliest provider baseline)
    hist = [p.get('history_days', 0) for p in ((summary or {}).get('first_seen', {}).get('providers') or {}).values()]
    if hist and max(hist) >= 15:
        ensure_label('reminder', '0e8a16', 'One-off maintenance reminders')
        title = 'Re-score "New on X" against the official arrival lists (first_seen day 15)'
        prior = json.loads(gh('issue', 'list', '--label', 'reminder', '--state', 'all', '--search', title,
                              '--json', 'number', '--limit', '5') or '[]')
        if not prior:
            print('reminder:', gh('issue', 'create', '--title', title, '--label', 'reminder', '--body',
                                  "first_seen has 15+ days of history, so the streaming New cards now use date-first-seen "
                                  "order. Re-score them against the services' official September/October lists with "
                                  "`score_official.py` (see the accuracy audit) and compare with the baseline numbers "
                                  "(New held 4-35% of official arrivals; the TMDB ceiling was 70-98%).\n\n"
                                  f"Opened automatically by {RUN_URL}").strip())


if __name__ == '__main__':
    main()
