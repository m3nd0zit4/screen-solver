# Screen Solver

Press a hotkey → your screen is captured → Claude solves what's on it → the
screenshot and the answer arrive in a Telegram chat you can read on your phone.

No window, no tray, no memory. Each press is one independent question. Claude is
reached through the Claude Code CLI (`claude -p`), so it runs on your logged-in
Claude subscription — **no paid API key needed**.

```
   hotkey  ──►  screenshot of the monitor under the cursor
           ──►  claude -p reads it and answers
           ──►  photo + answer sent to your Telegram bot  (over HTTPS)
```

---

## Install

**Requirements:** Windows, [Python 3.11+](https://python.org), and the Claude
Code CLI (run `claude` once to log in). For pushing/cloning: [git](https://git-scm.com).

### One-line install (recommended)

In PowerShell (after this repo is on GitHub — replace `<you>`):

```powershell
irm https://raw.githubusercontent.com/<you>/screen-solver/main/bootstrap.ps1 | iex
```

It clones the repo, installs everything, asks for your bot token once (stored
encrypted), enables autostart, and leaves a global `solver` command ready.

### Manual install

```powershell
git clone https://github.com/<you>/screen-solver.git
cd screen-solver
powershell -ExecutionPolicy Bypass -File install.ps1
```

### Get a Telegram bot (2 minutes)

1. In Telegram, message **@BotFather** → `/newbot` → choose a name → copy the token.
2. Open your new bot and send it any message once.
3. During setup, paste the token when asked. (Chat id is detected from your message;
   or read it at `https://api.telegram.org/bot<TOKEN>/getUpdates` → `chat.id`.)

---

## The `solver` command

Run `solver` with no argument for the interactive dashboard, or use a subcommand:

| Command | What it does |
|---|---|
| `solver` | Interactive dashboard (status, security, menu) |
| `solver init` | Guided first-time setup: token → encrypt → autostart → start |
| `solver test` | Capture the screen now and send it (no hotkey needed) |
| `solver status` | Is the background app running? |
| `solver start` / `stop` / `restart` | Control the background app |
| `solver config` | Open `config.toml` (hotkey, model, prompt) |
| `solver setup` | Re-enter the Telegram token (stored encrypted) |
| `solver verify` | Security checks, PASS/FAIL |
| `solver log` | Recent log lines |
| `solver doctor` | Check dependencies are installed |

The pretty UI uses `rich` + `questionary`. If they're missing, `solver` still
works in plain text and `solver doctor` tells you how to install them.

---

## Configure

Edit `config.toml`, then `solver restart`.

```toml
hotkey = "ctrl+alt+s"     # modifiers: ctrl, alt, shift, win + a letter/digit/F-key
model = "opus"            # opus | sonnet | haiku
timeout_seconds = 180
system_prompt = "..."     # how Claude should answer
instruction = "Solve what is on that screen."
```

---

## Security

- **Token encrypted at rest** — Windows DPAPI (`secret.dat`), tied to your Windows
  account. Copying the file to another machine or user won't decrypt it.
- **Transport is TLS** — Telegram and Claude are reached only over HTTPS.
- **Nothing secret is committed** — `.gitignore` excludes `secret.dat`, `.env`,
  the venv, screenshots, and logs. `push.ps1` refuses to push if a secret is tracked.
- **Screenshots are deleted** right after each send.
- **Privacy:** whatever is on the captured monitor is sent to Anthropic and to
  Telegram's servers. Don't trigger the hotkey over passwords or data you wouldn't
  paste into a chat.
- This is a normal background app (`pythonw.exe` in Task Manager). It does **not**
  hide from you, the OS, or your security tools. If antivirus flags it, add a
  folder exclusion yourself.

Verify any time:

```powershell
solver verify
```

---

## Publish to GitHub

```powershell
gh auth login                 # once, if not already logged in
powershell -ExecutionPolicy Bypass -File push.ps1
```

Creates a public repo under your account and pushes (secrets excluded). Afterwards,
put the repo URL into `bootstrap.ps1` so the one-line installer works.

---

## Files

| File | Purpose |
|------|---------|
| `app.py` | Background app: hotkey → capture → `claude -p` → Telegram |
| `solver.py` | The `solver` control CLI |
| `secrets_store.py` | DPAPI encrypt/decrypt of the token |
| `config.toml` | Hotkey, model, prompt, timeout |
| `install.ps1` | Setup: venv, deps, token, autostart, global `solver` |
| `bootstrap.ps1` | One-line remote installer |
| `push.ps1` | Publish to GitHub |
| `start.vbs` / `stop.bat` | Start hidden / stop |
| `pyproject.toml` | Package metadata + `solver` entry point (for `pipx`) |

---

## Uninstall

```powershell
solver stop
```

Then delete `ScreenSolver.lnk` from your Startup folder (`shell:startup`), remove
`%USERPROFILE%\.local\bin\solver.bat`, delete `secret.dat`, and remove the folder.
