# Screen Solver installer. Run in PowerShell from the repo folder:
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Sets up the venv, stores the Telegram token encrypted, and enables autostart.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

Write-Host "== Screen Solver install ==" -ForegroundColor Cyan

# 1. Python check
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "Python not found. Install Python 3.11+ from python.org and re-run." }

# 2. Claude Code CLI check (this app runs on your Claude subscription via `claude -p`)
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Warning "Claude Code CLI 'claude' not on PATH. Install it and run 'claude' once to log in."
}

# 3. venv + deps
if (-not (Test-Path "$root\.venv")) { python -m venv .venv }
& "$root\.venv\Scripts\python.exe" -m pip install -q --upgrade pip
& "$root\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt

# 4. Store secrets encrypted (DPAPI, per-user) unless already present
if (Test-Path "$root\secret.dat") {
    Write-Host "secret.dat already present - keeping it. Delete it to re-enter the token."
} else {
    Write-Host "`nCreate a bot with @BotFather in Telegram, then paste its token below."
    & "$root\.venv\Scripts\python.exe" secrets_store.py
}

# 5. Global `solver` command: shim in .local\bin (already on PATH for the CLI)
$bin = "$env:USERPROFILE\.local\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
@"
@echo off
rem Global launcher for Screen Solver.
"$root\.venv\Scripts\python.exe" "$root\solver.py" %*
"@ | Out-File -Encoding ascii "$bin\solver.bat"
if (-not (($env:Path -split ';') -contains $bin)) {
    [Environment]::SetEnvironmentVariable(
        "Path", ([Environment]::GetEnvironmentVariable("Path", "User") + ";$bin"), "User")
    Write-Host "Added $bin to your PATH (open a new terminal to use 'solver')." -ForegroundColor Yellow
}
Write-Host "Global command 'solver' installed." -ForegroundColor Green

# 6. Autostart shortcut in the Startup folder
$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "ScreenSolver.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = "C:\Windows\System32\wscript.exe"
$s.Arguments = "`"$root\start.vbs`""
$s.WorkingDirectory = $root
$s.Save()
Write-Host "Autostart enabled ($lnk)" -ForegroundColor Green

# 7. Launch now
& wscript.exe "$root\start.vbs"
Write-Host "`nDone. Run 'solver' for the dashboard, or press your hotkey (Ctrl+Alt+S)." -ForegroundColor Green
