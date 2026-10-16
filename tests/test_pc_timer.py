"""PC timer script (pc-timer/watch-dispatch.ps1, issue #79) with a stubbed gh (no GitHub, no network):

    python -m unittest tests.test_pc_timer

SD_WATCH not "on" -> nothing dispatched; "on" and no run busy -> dispatched; a run already in progress -> skipped.
Skipped when no PowerShell is installed.
"""
import shutil, subprocess, tempfile, unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'pc-timer' / 'watch-dispatch.ps1'
PS = shutil.which('pwsh') or shutil.which('powershell')

# The stub gh records every call in $calls and answers the two reads the script makes.
STUB = r'''
$calls = New-Object System.Collections.ArrayList
$gh = {
    [void]$calls.Add(($args -join ' '))
    if ($args[0] -eq 'variable') { $WATCH }
    elseif ($args[0] -eq 'run') { $RUNS }
}
'''


def run_script(watch, runs):
    log = tempfile.mkdtemp(prefix='sd-pc-timer-')
    cmd = (STUB.replace('$WATCH', "'%s'" % watch).replace('$RUNS', "@(%s)" % ','.join("'%s'" % r for r in runs))
           + "\n& '%s' -Gh $gh -LogDir '%s' | Out-Host\n" % (SCRIPT, log)
           + "'CALLS:' + ($calls -join ' | ')\n"
           + "'LOG:' + (Get-Content (Join-Path '%s' 'watch.log') -Raw)\n" % log)
    p = subprocess.run([PS, '-NoProfile', '-NonInteractive', '-Command', cmd], capture_output=True, text=True, timeout=60)
    return p.stdout


@unittest.skipUnless(PS, 'no PowerShell on this machine')
class PcTimer(unittest.TestCase):
    def test_off_dispatches_nothing(self):
        for v in ('off', 'true', ''):
            out = run_script(v, ['completed'])
            self.assertNotIn('workflow run', out, out)
            self.assertIn('nothing started', out)

    def test_on_dispatches(self):
        out = run_script('on', ['completed', 'completed'])
        self.assertIn('workflow run watch.yml -R xBOBxSAGETx/strand-discovery --ref main -f dry=false', out)
        self.assertIn('dispatched watch.yml', out)

    def test_busy_run_skips(self):
        out = run_script('on', ['in_progress', 'completed'])
        self.assertNotIn('workflow run', out, out)
        self.assertIn('skipped', out)


if __name__ == '__main__':
    unittest.main()
