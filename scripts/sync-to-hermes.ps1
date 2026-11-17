# Development helper: copies this checkout into the Hermes plugins folder.
# A Windows junction cannot point at a network drive, so a checkout kept on one is copied instead.
# Other machines should install the plugin with `hermes plugins install` (see README), not this script.
# Usage:  powershell -File scripts\sync-to-hermes.ps1
$ErrorActionPreference = 'Stop'
$src = Split-Path -Parent $PSScriptRoot
$hermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
$dst = Join-Path $hermesHome 'plugins\repo-status'

if ((Test-Path $dst) -and ((Get-Item $dst -Force).LinkType)) {
    throw "$dst is a link; remove it first (cmd /c rmdir `"$dst`")"
}

# /MIR mirrors (removes files deleted in the source) but only inside $dst.
robocopy $src $dst /MIR /XD .git __pycache__ .pytest_cache /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
Write-Output "Synced $src -> $dst"
Write-Output "Agent half: restart the gateway / start a new session. Desktop half: Capabilities -> Plugins -> Rescan."
