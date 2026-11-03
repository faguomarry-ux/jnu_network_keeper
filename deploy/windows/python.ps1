function Resolve-KeeperPython {
    param([string]$Python = '')
    $Candidates = @()
    if ($Python) { $Candidates += $Python }
    else {
        $Command = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($Command -and $Command.Source -notmatch 'WindowsApps') { $Candidates += $Command.Source }
        $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($Launcher) {
            $Launched = & $Launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $Launched) { $Candidates += $Launched.Trim() }
        }
        # Optional convenience on this Codex desktop. Normal Python is preferred.
        $Candidates += Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    }
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            & $Candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return (Resolve-Path -LiteralPath $Candidate).Path }
        }
    }
    throw 'Python 3.10+ not found. Install Python, or supply -Python with the full python.exe path.'
}
