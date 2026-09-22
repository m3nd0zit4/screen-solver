# One-line installer for Screen Solver.
# Run in PowerShell (edit the repo URL after you push to GitHub):
#   irm https://raw.githubusercontent.com/<you>/screen-solver/main/bootstrap.ps1 | iex

$ErrorActionPreference = "Stop"
$repo = "https://github.com/<you>/screen-solver.git"   # <-- set after pushing
$dest = "$env:USERPROFILE\screen-solver"

Write-Host "== Screen Solver bootstrap ==" -ForegroundColor Cyan

foreach ($tool in "git", "python") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool not found. Install it first, then re-run."
    }
}
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Warning "Claude Code CLI not found. Install it and run 'claude' once to log in."
}

if (Test-Path $dest) {
    Write-Host "Updating existing install..." -ForegroundColor Yellow
    git -C $dest pull --ff-only
} else {
    git clone $repo $dest
}

Set-Location $dest
powershell -ExecutionPolicy Bypass -File "$dest\install.ps1"
