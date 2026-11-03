# Integration check: creates real task definitions but intercepts every scheduler
# mutation. No task is registered, started, stopped, or removed on this computer.
param([string]$Python = '')
$ErrorActionPreference = 'Stop'
# Load before defining mocks: Windows PowerShell 5.1 module autoload can
# otherwise replace same-named fixture functions during New-ScheduledTask*.
Import-Module ScheduledTasks -ErrorAction Stop
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $ProjectDir 'deploy\windows\python.ps1')
$Python = Resolve-KeeperPython -Python $Python
$Scratch = Join-Path ([IO.Path]::GetTempPath()) ('jnu-setup-check-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Scratch | Out-Null
$Config = Join-Path $Scratch 'config.json'
$global:JnuSetupCheck = @{ Captured=$null; Started=0; Stopped=0; Existing=$null }
function Get-ScheduledTask { param($TaskName) return $global:JnuSetupCheck.Existing }
function Register-ScheduledTask {
    [CmdletBinding()]
    param($TaskName,$InputObject,$User,$Password,$Action,$Trigger,$Principal,$Settings,$Description,[switch]$Force)
    $global:JnuSetupCheck.Captured = @{} + $PSBoundParameters
}
function Start-ScheduledTask { param($TaskName) $global:JnuSetupCheck.Started++ }
function Stop-ScheduledTask {
    param($TaskName)
    if (-not $global:JnuSetupCheck.Existing) { throw 'Unexpected stop during fixture test' }
    $global:JnuSetupCheck.Existing.State = 'Ready'
    $global:JnuSetupCheck.Stopped++
}
function Read-Host { param($Prompt,[switch]$AsSecureString) return (ConvertTo-SecureString 'dummy-windows-test-password' -AsPlainText -Force) }
function Assert-Check { param($Condition,$Message) if (-not $Condition) { throw $Message } }
Push-Location $ProjectDir
try {
    & $Python -c "import sys; from pathlib import Path; from jnu_network_keeper.config import Settings,save_settings; save_settings(Path(sys.argv[1]),Settings(username='test-account',password='dummy-campus-test-password'))" $Config
    if ($LASTEXITCODE -ne 0) { throw 'Fixture config failed' }
    & (Join-Path $ProjectDir 'deploy\windows\install.ps1') -Python $Python -Config $Config -StartupMode Boot
    Assert-Check ($global:JnuSetupCheck.Captured.InputObject.Triggers[0].CimClass.CimClassName -eq 'MSFT_TaskBootTrigger') 'Boot trigger was not created'
    Assert-Check ($global:JnuSetupCheck.Captured.InputObject.Principal.LogonType -eq 1) 'Boot task must use Password logon'
    Assert-Check ($global:JnuSetupCheck.Captured.InputObject.Principal.RunLevel -eq 0) 'Boot task should use limited privileges'
    Assert-Check ($global:JnuSetupCheck.Captured.InputObject.Triggers[0].Delay -eq 'PT30S') 'Boot delay missing'
    Assert-Check ($global:JnuSetupCheck.Captured.Password -eq 'dummy-windows-test-password') 'Task registration did not receive the fixture Windows credential'
    Assert-Check ($global:JnuSetupCheck.Started -eq 1) 'Boot install did not start task'
    & (Join-Path $ProjectDir 'deploy\windows\install.ps1') -Python $Python -Config $Config -StartupMode Logon -NoStart
    Assert-Check ($global:JnuSetupCheck.Captured.Trigger.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger') 'Logon trigger missing'
    Assert-Check ($global:JnuSetupCheck.Captured.Principal.LogonType -eq 3) 'Logon task must use Interactive logon'
    Assert-Check (-not $global:JnuSetupCheck.Captured.ContainsKey('Password')) 'Logon mode must not request a Windows password'
    Assert-Check ($global:JnuSetupCheck.Started -eq 1) 'NoStart unexpectedly started task'
    & (Join-Path $ProjectDir 'setup-windows.ps1') -Python $Python -Config $Config -StartupMode Logon -ReuseConfig
    Assert-Check ($global:JnuSetupCheck.Captured.Trigger.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger') 'Setup wrapper did not reach logon installation'
    $global:JnuSetupCheck.Existing = [pscustomobject]@{
        Description='JNU ePortal reconnect and network monitoring'
        Actions=@([pscustomobject]@{ WorkingDirectory=$ProjectDir })
        Principal=[pscustomobject]@{ UserId=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
        State='Running'
    }
    & (Join-Path $ProjectDir 'setup-windows.ps1') -Python $Python -Config $Config -StartupMode Logon -ReuseConfig
    Assert-Check ($global:JnuSetupCheck.Captured.Force) 'Existing owned task should be updated'
    Assert-Check ($global:JnuSetupCheck.Stopped -eq 1) 'Existing task was not stopped for restart'
    $global:JnuSetupCheck.Existing = [pscustomobject]@{ Description='Unrelated task'; Actions=@(); State='Ready' }
    $Rejected = $false
    try { & (Join-Path $ProjectDir 'deploy\windows\install.ps1') -Python $Python -Config $Config -Replace -StartupMode Logon }
    catch { $Rejected = $_.Exception.Message -like '*another project*' }
    Assert-Check $Rejected 'Replacing an unrelated task should be refused'
    Write-Host 'PASS: Boot/Password, Logon/Interactive, setup wrapper, NoStart, owned-task restart, unrelated-task refusal.'
} finally {
    Pop-Location
    $global:JnuSetupCheck = $null
    # Only fixed fixture files under the verified, uniquely created temp folder.
    $ResolvedScratch = [IO.Path]::GetFullPath($Scratch)
    if (-not $ResolvedScratch.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()),[StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected fixture path' }
    foreach ($Name in @('config.json','keeper.lock')) {
        $File = Join-Path $ResolvedScratch $Name
        if (Test-Path -LiteralPath $File) { Remove-Item -LiteralPath $File -Force }
    }
    Remove-Item -LiteralPath $ResolvedScratch -Force
}
