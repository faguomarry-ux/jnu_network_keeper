param([string]$TaskName = 'JNU Network Keeper')
$ErrorActionPreference = 'Stop'
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task: $TaskName. Saved config and logs are retained."
} else { Write-Host "Task not installed: $TaskName" }
