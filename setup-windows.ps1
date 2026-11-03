<#
Configure credentials and install automatic startup, from the project root:
  powershell -NoProfile -ExecutionPolicy Bypass -File .\setup-windows.ps1
Default Boot mode works before desktop sign-in and requests your Windows
account password at installation. For sign-in startup without that password:
  .\setup-windows.ps1 -StartupMode Logon
Campus and Windows account passwords are entered interactively, never here.
#>
param(
    [ValidateSet('Boot','Logon')]
    [string]$StartupMode = 'Boot',
    [string]$Python = '',
    [string]$Config = '',
    [ValidateRange(10,900)]
    [int]$Interval = 60,
    [string]$GatewayIp,
    [string]$SourceAddress,
    [string]$Service,
    [switch]$ReuseConfig,
    [switch]$Accounts,
    [ValidateRange(1,2147483647)]
    [int]$FailoverThreshold = 100000
)
$ErrorActionPreference = 'Stop'
if ($StartupMode -eq 'Boot') {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'For boot mode, open PowerShell as administrator using this same Windows account, then rerun. Or choose -StartupMode Logon.'
    }
}
. (Join-Path $PSScriptRoot 'deploy\windows\python.ps1')
$Python = Resolve-KeeperPython -Python $Python
if (-not $Config) {
    if ($env:JNU_NET_CONFIG) { $Config = $env:JNU_NET_CONFIG }
    else { $Config = Join-Path $env:USERPROFILE '.config\jnu_network_keeper\config.json' }
}
$Config = [IO.Path]::GetFullPath($Config)
Push-Location $PSScriptRoot
try {
    if ($Accounts -and $ReuseConfig) { throw 'Accounts and ReuseConfig cannot be combined.' }
    if ($PSBoundParameters.ContainsKey('FailoverThreshold') -and -not $Accounts) { throw 'FailoverThreshold requires Accounts.' }
    $CommonArgs = @('--config',$Config,'--interval',"$Interval")
    # An explicitly supplied empty string clears a previously pinned IP/service.
    foreach ($Pair in @(@('GatewayIp','--gateway-ip'),@('SourceAddress','--source-address'),@('Service','--service'))) {
        if ($PSBoundParameters.ContainsKey($Pair[0])) { $CommonArgs += '{0}={1}' -f $Pair[1],(Get-Variable $Pair[0] -ValueOnly) }
    }
    if (-not $ReuseConfig) {
        $InitCommand = if ($Accounts) { 'init-accounts' } else { 'init' }
        $InitArgs = @('-m','jnu_network_keeper',$InitCommand) + $CommonArgs
        if ($Accounts) { $InitArgs += @('--failover-threshold',"$FailoverThreshold") }
        & $Python @InitArgs
        if ($LASTEXITCODE -ne 0) { throw 'Campus account configuration failed; startup task was not changed.' }
    } elseif (-not (Test-Path -LiteralPath $Config)) {
        throw 'No saved config found. Omit -ReuseConfig to enter your campus account.'
    } elseif ($PSBoundParameters.ContainsKey('GatewayIp') -or $PSBoundParameters.ContainsKey('SourceAddress') -or $PSBoundParameters.ContainsKey('Service')) {
        throw 'Network changes require initialization. Omit -ReuseConfig and press Enter to keep saved credentials.'
    }
    # Fail before task registration if a foreground keeper still owns this config.
    $OwnTask = Get-ScheduledTask -TaskName 'JNU Network Keeper' -ErrorAction SilentlyContinue
    if (-not ($OwnTask -and $OwnTask.Description -eq 'JNU ePortal reconnect and network monitoring' -and
              $OwnTask.Actions[0].WorkingDirectory -eq $PSScriptRoot -and $OwnTask.State -eq 'Running')) {
        & $Python -c "import sys; from pathlib import Path; from jnu_network_keeper.keeper import InstanceLock; p=Path(sys.argv[1]); lock=InstanceLock(p.parent/'keeper.lock'); lock.__enter__(); lock.__exit__()" $Config
        if ($LASTEXITCODE -ne 0) { throw 'Stop the foreground keeper first, then rerun this setup.' }
    }
    & (Join-Path $PSScriptRoot 'deploy\windows\install.ps1') -Python $Python -Config $Config `
        -Interval $Interval -StartupMode $StartupMode -Replace
    Write-Host 'Setup complete. Status: .\start-windows.cmd -Action Status'
    Write-Host 'Task details: Get-ScheduledTask -TaskName "JNU Network Keeper"'
} finally { Pop-Location }
