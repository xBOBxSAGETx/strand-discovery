# #79: register / remove the Windows scheduled task "StrandWatch" that runs watch-dispatch.ps1 every 5 minutes.
#   install:   powershell -NoProfile -ExecutionPolicy Bypass -File pc-timer\install-task.ps1
#   preview:   ... -WhatIf        (prints the task definition, registers nothing)
#   undo:      ... -Uninstall     (deletes the task; then restore the cron in watch.yml, or revert the PR)
# Runs as the current user, only while logged on, no stored password. No window: conhost --headless hosts powershell.
param(
    [switch]$Uninstall,
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
$name = 'StrandWatch'

if ($Uninstall) {
    if ($WhatIf) { Write-Output "WhatIf: would delete scheduled task $name"; return }
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Output "Deleted scheduled task $name"
    } else { Write-Output "Task $name not found, nothing to delete" }
    return
}

$script = Join-Path $PSScriptRoot 'watch-dispatch.ps1'
if (-not (Test-Path $script)) { throw "missing $script" }
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
$arg = '--headless "{0}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{1}"' -f $ps, $script
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction -Execute $conhost -Argument $arg
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -Hidden
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description '#79: start the strand-discovery watcher (watch.yml) every 5 minutes. Undo: install-task.ps1 -Uninstall'

if ($WhatIf) {
    Write-Output "WhatIf: would register scheduled task $name"
    Write-Output ("  Run as      : {0} (LogonType Interactive = only while logged on, no stored password)" -f $user)
    Write-Output ("  Program     : {0}" -f $conhost)
    Write-Output ("  Arguments   : {0}" -f $arg)
    Write-Output ("  Trigger     : every 5 minutes from {0:yyyy-MM-dd HH:mm}, repeating forever" -f (Get-Date).Date)
    Write-Output ("  Settings    : hidden, one run at a time, catch up a missed run, time limit 4 min, battery ok")
    return
}
Register-ScheduledTask -TaskName $name -InputObject $task | Out-Null
Write-Output "Registered scheduled task $name"
