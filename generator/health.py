"""Health alerts for the daily workflow: keeps ONE open GitHub issue labelled "health" in sync with the latest run.

  python health.py <report dir> <build result> <logger outcome>

Test mode (no GitHub calls): HEALTH_DRY_RUN=1 prints every write it would make; HEALTH_MOCK_ISSUES=<json file>
({"health": [{"number": 1}], "reminder": [...]}, per label) stands in for `gh issue list`.

<report dir> = the downloaded run-report artifact (out/summary.json, state/fs_summary.json; either may be missing).
Opens (or updates) the issue when the build failed, the first_seen logger failed, a guard tripped, TMDB rejected the
key (401), or the logger reported warnings/skips; closes it with a comment when a later run is clean. GitHub emails
the repo owner on new issues and comments. Also opens the one-off "re-score New" reminder on day 15 of first_seen
history. Uses the gh CLI with the job's GITHUB_TOKEN (issues: write only).
"""
import json, os, subprocess, sys
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


def problems(report, build_result, logger_outcome):
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
    if logger_outcome != 'success':
        out.append(f"first_seen logger ended **{logger_outcome}** - New cards fall back to release-date order today.")
    if fs:
        out += [f"first_seen warning: {w}" for w in fs.get('warnings', [])]
        out += [f"first_seen skipped: {s}" for s in fs.get('skipped', [])]
        if str(fs.get('state_source', '')).startswith('none') and summary and summary.get('first_seen', {}).get('providers'):
            if any(p.get('history_days', 0) > 0 for p in summary['first_seen']['providers'].values()):
                out.append(f"first_seen state was not restored ({fs['state_source']}).")
    if summary is None and build_result == 'success':
        out.append('Run report is missing (summary.json not found).')
    return out, summary, fs


def main():
    report, build_result, logger_outcome = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    found, summary, fs = problems(report, build_result, logger_outcome)
    ensure_label('health', 'd73a4a', 'Daily build health (opened/closed automatically)')
    existing = json.loads(gh('issue', 'list', '--label', 'health', '--state', 'open', '--json', 'number', '--limit', '5') or '[]')
    if found:
        body = (f"Latest run: {RUN_URL}\n\n" + '\n'.join(f'- {p}' for p in found) +
                "\n\nThis issue closes itself after the next clean run. What each alert means: README → Operations.")
        if existing:
            gh('issue', 'comment', str(existing[0]['number']), '--body', body)
            print(f"health: updated issue #{existing[0]['number']} ({len(found)} problems)")
        else:
            print('health:', gh('issue', 'create', '--title', HEALTH_TITLE, '--label', 'health', '--body', body).strip())
    else:
        for i in existing:
            gh('issue', 'close', str(i['number']), '--comment', f"Clean run: {RUN_URL}")
            print(f"health: closed issue #{i['number']}")
        if not existing:
            print('health: clean, no open issue')

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
