# Pushes Screen Solver to a NEW public GitHub repo using the gh CLI.
# Run once, from the repo folder:
#   powershell -ExecutionPolicy Bypass -File push.ps1
# Requires: gh installed and logged in (run `gh auth login` first).

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI 'gh' not found. Install from https://cli.github.com then re-run."
}
gh auth status | Out-Null   # throws if not logged in

# Safety: make sure secrets are not about to be committed.
foreach ($f in ".env", "secret.dat") {
    if (Test-Path "$root\$f") {
        $tracked = git ls-files --error-unmatch $f 2>$null
        if ($tracked) { throw "$f is tracked by git! Aborting so your token is not pushed." }
    }
}

if (-not (Test-Path "$root\.git")) { git init | Out-Null }
git add -A
git commit -m "Screen Solver: hotkey screenshot -> Claude -> Telegram" 2>$null

$repo = Read-Host "Repo name (e.g. screen-solver)"
if (-not $repo) { $repo = "screen-solver" }

# Creates the repo under your account, pushes, and sets the remote.
gh repo create $repo --public --source=. --remote=origin --push

$url = gh repo view --json url -q .url
Write-Host "`nPushed to $url" -ForegroundColor Green
Write-Host "Now edit bootstrap.ps1 line with your repo URL so the one-liner installer works." -ForegroundColor Yellow
