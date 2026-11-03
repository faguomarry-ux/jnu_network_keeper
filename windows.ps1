param(
    [ValidateSet('Init','Status','Login','Run','Install','Start','Restart','Stop','Uninstall','Logs','Paths')]
    [string]$Action = 'Run',
    [string]$Python = '',
    [string]$Config = '',
    [string]$GatewayIp = '',
    [string]$SourceAddress = '',
    [ValidateSet('Boot','Logon')]
    [string]$StartupMode = 'Boot'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'deploy\windows\python.ps1')
$Python = Resolve-KeeperPython -Python $Python
if (-not $Config) {
    if ($env:JNU_NET_CONFIG) { $Config = $env:JNU_NET_CONFIG }
    else { $Config = Join-Path $env:USERPROFILE '.config\jnu_network_keeper\config.json' }
}
if ($Action -eq 'Install') {
    & (Join-Path $PSScriptRoot 'setup-windows.ps1') -Python $Python -Config $Config -StartupMode $StartupMode -ReuseConfig
    exit
}
if ($Action -eq 'Stop') { Stop-ScheduledTask -TaskName 'JNU Network Keeper'; exit }
if ($Action -eq 'Start') { Start-ScheduledTask -TaskName 'JNU Network Keeper'; exit }
if ($Action -eq 'Restart') {
    Stop-ScheduledTask -TaskName 'JNU Network Keeper'
    Start-ScheduledTask -TaskName 'JNU Network Keeper'
    exit
}
if ($Action -eq 'Uninstall') { & (Join-Path $PSScriptRoot 'deploy\windows\uninstall.ps1'); exit }
if ($Action -eq 'Logs') { Get-Content -LiteralPath (Join-Path (Split-Path $Config) 'keeper.log') -Tail 40 -Wait; exit }
$CommonArgs = @('--config',$Config)
if ($GatewayIp) { $CommonArgs += @('--gateway-ip', $GatewayIp) }
if ($SourceAddress) { $CommonArgs += @('--source-address', $SourceAddress) }
$CliArgs = @('-m','jnu_network_keeper',$Action.ToLowerInvariant()) + $CommonArgs
Push-Location $PSScriptRoot
try {
    if ($Action -eq 'Run' -and -not (Test-Path -LiteralPath $Config)) {
        $InitArgs = @('-m','jnu_network_keeper','init') + $CommonArgs
        & $Python @InitArgs
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & $Python @CliArgs
    exit $LASTEXITCODE
} finally { Pop-Location }
