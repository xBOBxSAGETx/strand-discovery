# #79: run on the owner's PC every 5 minutes by a Windows scheduled task (see install-task.ps1).
# GitHub's own */5 timer fires only every few hours, so this starts watch.yml instead.
#   1. SD_WATCH (repo variable) is not "on"  -> do nothing (the off switch still works)
#   2. a watch.yml run is queued / running -> do nothing; one stuck (queued > 20 min, running > 15) is cancelled first
#   3. else: gh workflow run watch.yml --ref main -f dry=false
# One log line per run in %LOCALAPPDATA%\strand-watch\watch.log (rolls to watch.log.old at 400 KB: under 1 MB in all).
# Works in Windows PowerShell 5.1 and PowerShell 7. -Gh is a scriptblock so tests can stub GitHub.
param(
    [scriptblock]$Gh = { & $script:GhExe @args },
    [string]$LogDir = (Join-Path $env:LOCALAPPDATA 'strand-watch'),
    [string]$Repo = 'xBOBxSAGETx/strand-discovery'
)
$ErrorActionPreference = 'Stop'

function Find-Gh {
    $c = Get-Command gh -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @("$env:LOCALAPPDATA\gh\bin\gh.exe", "$env:ProgramFiles\GitHub CLI\gh.exe")) {
        if (Test-Path $p) { return $p }
    }
    throw 'gh not found'
}

function Write-Log([string]$Msg) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $log = Join-Path $LogDir 'watch.log'
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 400KB) {
        Move-Item -Force $log "$log.old"
    }
    Add-Content -Path $log -Value ('{0} {1}' -f (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz'), $Msg)
}

# Any comma list passed to gh (e.g. --json a,b) MUST be quoted: PowerShell turns an unquoted a,b into an array = separate words.
function Invoke-Gh {
    $global:LASTEXITCODE = 0
    $out = & $Gh @args 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("gh failed ({0}): {1}" -f $LASTEXITCODE, ($out -join ' ')) }
    return $out
}

$result = 'unknown'
try {
    if (-not $PSBoundParameters.ContainsKey('Gh')) { $script:GhExe = Find-Gh }
    $v = (Invoke-Gh variable get SD_WATCH -R $Repo | Out-String).Trim()
    if ($v -ne 'on') {
        $result = "off (SD_WATCH='$v'), nothing started"
    } else {
        $rows = Invoke-Gh run list -w watch.yml -R $Repo -L 20 --json 'status,createdAt,databaseId' `
            --jq '.[] | [.status, .createdAt, .databaseId] | @tsv'
        $busy = @()
        foreach ($line in @($rows | Out-String -Stream | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
            $f = $line -split "`t"
            if ($f[0] -notin @('queued', 'in_progress', 'waiting', 'pending', 'requested')) { continue }
            $age = 0
            try { $age = [int]((Get-Date).ToUniversalTime() - [datetime]::Parse($f[1]).ToUniversalTime()).TotalMinutes } catch { }
            $limit = if ($f[0] -eq 'in_progress') { 15 } else { 20 }   # the job's own timeout is 10 min
            if ($age -gt $limit) {
                # stale: cancel it so it stops blocking; a failed cancel throws -> ERROR, no dispatch this tick
                try {
                    Invoke-Gh run cancel $f[2] -R $Repo | Out-Null
                    Write-Log "cancelled stale $($f[0]) run $($f[2]) ($age min)"
                } catch {
                    # GitHub can list a finished run as queued forever; cancel then answers HTTP 409 "completed". Not busy: skip it.
                    if ($_.Exception.Message -match '(?i)cannot cancel a workflow run that is completed') {
                        Write-Log "ghost $($f[0]) run $($f[2]) already completed; skipping cancel"
                    } else { throw }   # any other cancel error stays fail-closed
                }
            } else { $busy += $f[0] }
        }
        if ($busy.Count -gt 0) {
            $result = "skipped, a watch.yml run is already $($busy[0])"
        } else {
            # dry=false: a manual run defaults to dry (dispatch nothing, save nothing), which would watch for nothing
            Invoke-Gh workflow run watch.yml -R $Repo --ref main -f dry=false | Out-Null
            $result = 'dispatched watch.yml'
        }
    }
} catch {
    $result = 'ERROR ' + ($_.Exception.Message -replace '\s+', ' ')
}
Write-Log $result
Write-Output $result
if ($result -like 'ERROR*') { exit 1 }
