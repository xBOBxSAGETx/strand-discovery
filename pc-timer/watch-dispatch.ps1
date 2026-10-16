# #79: run on the owner's PC every 5 minutes by a Windows scheduled task (see install-task.ps1).
# GitHub's own */5 timer fires only every few hours, so this starts watch.yml instead.
#   1. SD_WATCH (repo variable) is not "on"  -> do nothing (the off switch still works)
#   2. a watch.yml run is already queued / running -> do nothing
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
        $runs = Invoke-Gh run list -w watch.yml -R $Repo -L 20 --json status --jq '.[].status'
        $busy = @($runs | Out-String -Stream | ForEach-Object { $_.Trim() } |
            Where-Object { $_ -in @('queued', 'in_progress', 'waiting', 'pending', 'requested') })
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
