param(
    [string]$Python = '',
    [string]$Config = '',
    [int]$Interval = 60,
    [string]$TaskName = 'JNU Network Keeper',
    [ValidateSet('Boot','Logon')]
    [string]$StartupMode = 'Boot',
    [ValidateRange(0,3600)]
    [int]$StartupDelaySeconds = 30,
    [switch]$Replace,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if ($Interval -lt 10) { throw 'Interval must be at least 10 seconds.' }
. (Join-Path $PSScriptRoot 'python.ps1')
$Python = Resolve-KeeperPython -Python $Python
if (-not $Config) {
    if ($env:JNU_NET_CONFIG) { $Config = $env:JNU_NET_CONFIG }
    else { $Config = Join-Path $env:USERPROFILE '.config\jnu_network_keeper\config.json' }
}
$Config = (Resolve-Path -LiteralPath $Config).Path
# Canonicalize through Python as well (including any OS filesystem redirection).
$Config = (& $Python -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' $Config).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to resolve the config path.' }
$Pythonw = Join-Path (Split-Path $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $Pythonw)) { throw 'pythonw.exe is required beside python.exe for hidden execution.' }
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    if (-not $Replace) { throw "Task already exists: $TaskName. Use setup-windows.ps1 to update this project's task." }
    if ($ExistingTask.Description -ne 'JNU ePortal reconnect and network monitoring' -or
        $ExistingTask.Actions.Count -ne 1 -or $ExistingTask.Actions[0].WorkingDirectory -ne $ProjectDir) {
        throw 'Existing task belongs to another project; choose a different TaskName.'
    }
}
Push-Location $ProjectDir
try {
    & $Python -c "import sys; from pathlib import Path; from jnu_network_keeper.config import load_settings; assert sys.version_info >= (3,10); s=load_settings(Path(sys.argv[1])); s.interval=int(sys.argv[2]); s.validate(); assert s.username and s.password, 'Run init first'" $Config $Interval
    if ($LASTEXITCODE -ne 0) { throw 'Credential/Python validation failed. Run init with this Windows account first.' }
} finally { Pop-Location }
# Windows filenames cannot contain double quotes; pass paths as separately quoted arguments.
$Arguments = '-m jnu_network_keeper run --config "{0}" --interval {1}' -f $Config, $Interval
$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument $Arguments -WorkingDirectory $ProjectDir
$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($ExistingTask) {
    $ExistingIdentity = New-Object Security.Principal.NTAccount($ExistingTask.Principal.UserId)
    try { $ExistingSid = $ExistingIdentity.Translate([Security.Principal.SecurityIdentifier]).Value }
    catch {
        # Some Task Scheduler versions return the SID directly.
        $ExistingSid = $ExistingTask.Principal.UserId
    }
    if ($ExistingSid -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) {
        throw 'Existing task belongs to another Windows account; use the original account.'
    }
}
$Settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)
if ($StartupMode -eq 'Boot') {
    $Trigger = New-ScheduledTaskTrigger -AtStartup
    $Trigger.Delay = 'PT{0}S' -f $StartupDelaySeconds
    Write-Host 'Boot mode needs the password of your CURRENT Windows account, not your campus password or Windows Hello PIN.'
    $SecurePassword = Read-Host "Windows account password for $CurrentUser" -AsSecureString
    if ($SecurePassword.Length -eq 0) { throw 'An empty password cannot be used for boot mode. Choose -StartupMode Logon instead.' }
    $Credential = New-Object System.Management.Automation.PSCredential($CurrentUser, $SecurePassword)
    # The password is passed only to Windows Task Scheduler, never to a file,
    # command line, log, or the campus gateway. Run with normal user privileges.
    $Definition = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings `
        -Principal (New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Password -RunLevel Limited) `
        -Description 'JNU ePortal reconnect and network monitoring'
    try {
        Register-ScheduledTask -TaskName $TaskName -InputObject $Definition -User $CurrentUser `
            -Password ($Credential.GetNetworkCredential().Password) -Force:$Replace | Out-Null
    } finally { $Credential = $null; $SecurePassword.Dispose() }
} else {
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
        -Principal (New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited) `
        -Settings $Settings -Description 'JNU ePortal reconnect and network monitoring' -Force:$Replace | Out-Null
}
if ($ExistingTask -and $ExistingTask.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName
    $Deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ((Get-ScheduledTask -TaskName $TaskName).State -eq 'Running') {
        if ([DateTime]::UtcNow -ge $Deadline) { throw 'Task installed, but old instance is still stopping. Start it after the old process exits.' }
        Start-Sleep -Milliseconds 200
    }
}
if (-not $NoStart) { Start-ScheduledTask -TaskName $TaskName }
Write-Host "Installed task: $TaskName; startup mode: $StartupMode."
Write-Host "Log: $(Join-Path (Split-Path $Config) 'keeper.log')"
