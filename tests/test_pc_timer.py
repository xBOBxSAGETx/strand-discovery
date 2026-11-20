"""PC timer script (pc-timer/watch-dispatch.ps1, issue #79) with a stubbed gh (no GitHub, no network):

    python -m unittest tests.test_pc_timer

SD_WATCH not "on" -> nothing dispatched; "on" and no run busy -> dispatched; a run already in progress -> skipped.
Skipped when no PowerShell is installed.
"""
import datetime, shutil, subprocess, tempfile, unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'pc-timer' / 'watch-dispatch.ps1'
PS = shutil.which('pwsh') or shutil.which('powershell')

# The stub gh records every call in $calls and answers the two reads the script makes.
STUB = r'''
$calls = New-Object System.Collections.ArrayList
$gh = {
    [void]$calls.Add(($args -join ' '))
    if ($args[0] -eq 'variable') { $WATCH }
    elseif ($args[0] -eq 'run' -and $args[1] -eq 'list') { $RUNS }
    elseif ($args[0] -eq 'run' -and $args[1] -eq 'cancel') { if ($CANCELFAIL) { $global:LASTEXITCODE = 1; 'boom' } }
}
'''


def row(status, minutes, rid=7):
    """One `gh run list` line (status, createdAt, id) as the script's --jq prints it."""
    t = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return '%s	%s	%s' % (status, t, rid)


def run_script(watch, runs, cancel_fails=False):
    log = tempfile.mkdtemp(prefix='sd-pc-timer-')
    cmd = (STUB.replace('$WATCH', "'%s'" % watch).replace('$CANCELFAIL', '$true' if cancel_fails else '$false').replace('$RUNS', "@(%s)" % ','.join("'%s'" % r for r in runs))
           + "\n& '%s' -Gh $gh -LogDir '%s' | Out-Host\n" % (SCRIPT, log)
           + "'CALLS:' + ($calls -join ' | ')\n"
           + "'LOG:' + (Get-Content (Join-Path '%s' 'watch.log') -Raw)\n" % log)
    p = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-Command', cmd], capture_output=True, text=True, timeout=60)
    return p.stdout


@unittest.skipUnless(PS, 'no PowerShell on this machine')
class PcTimer(unittest.TestCase):
    def test_off_dispatches_nothing(self):
        for v in ('off', 'true', ''):
            out = run_script(v, [row('completed', 5)])
            self.assertNotIn('workflow run', out, out)
            self.assertIn('nothing started', out)

    def test_on_dispatches(self):
        out = run_script('on', [row('completed', 5), row('completed', 90)])
        self.assertIn('workflow run watch.yml -R xBOBxSAGETx/strand-discovery --ref main -f dry=false', out)
        self.assertIn('dispatched watch.yml', out)

    def test_busy_run_skips(self):
        out = run_script('on', [row('in_progress', 3), row('completed', 90)])
        self.assertNotIn('workflow run', out, out)
        self.assertIn('skipped', out)

    def test_stale_queued_run_is_cancelled_then_dispatched(self):
        out = run_script('on', [row('queued', 30, 555)])
        self.assertIn('run cancel 555 -R xBOBxSAGETx/strand-discovery', out)
        self.assertIn('cancelled stale queued run 555 (30 min)', out)
        self.assertIn('workflow run watch.yml', out)

    def test_young_queued_run_still_skips(self):
        out = run_script('on', [row('queued', 3)])
        self.assertNotIn('run cancel', out, out)
        self.assertNotIn('workflow run', out, out)
        self.assertIn('skipped', out)

    def test_stale_in_progress_run_is_cancelled(self):
        out = run_script('on', [row('in_progress', 40, 556)])
        self.assertIn('cancelled stale in_progress run 556', out)

    def test_failed_cancel_means_no_dispatch(self):
        out = run_script('on', [row('queued', 30, 557)], cancel_fails=True)
        self.assertNotIn('workflow run', out, out)
        self.assertIn('ERROR', out)


if __name__ == '__main__':
    unittest.main()
