"""Screen Solver control CLI.

Run `solver` with no argument for the interactive dashboard, or a subcommand:

  solver init      guided first-time setup (token, encrypt, autostart)
  solver test      capture the screen now and send it (no hotkey needed)
  solver status    is the background app running?
  solver start | stop | restart
  solver config    open config.toml
  solver setup     (re)enter the Telegram token, stored encrypted
  solver verify    security checks with PASS/FAIL
  solver log       recent log lines
  solver doctor    check that dependencies are installed

The rich/questionary libraries give the pretty UI; if they're missing the CLI
still works in plain text and tells you how to install them.
"""
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

# Force UTF-8 so box-drawing and ✓/● render on legacy Windows consoles (cp1252).
try:
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleCP(65001)
except Exception:
    pass
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

import app
import secrets_store

ROOT = Path(__file__).resolve().parent

ACCENT = "#7C5CFF"
OK = "#3FB950"
WARN = "#D29922"
BAD = "#F85149"
MUTE = "grey58"

VERSION = "1.0.0"

BANNER = r"""
███████╗ ██████╗ ██╗    ██╗   ██╗███████╗██████╗
██╔════╝██╔═══██╗██║    ██║   ██║██╔════╝██╔══██╗
███████╗██║   ██║██║    ██║   ██║█████╗  ██████╔╝
╚════██║██║   ██║██║    ╚██╗ ██╔╝██╔══╝  ██╔══██╗
███████║╚██████╔╝███████╗╚████╔╝ ███████╗██║  ██║
╚══════╝ ╚═════╝ ╚══════╝ ╚═══╝  ╚══════╝╚═╝  ╚═╝
"""

try:
    from rich.align import Align
    from rich.box import ROUNDED
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


# ---------------------------------------------------------------- helpers ----
def _cfg() -> dict:
    return tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))


def _load_secrets() -> dict:
    try:
        return secrets_store.load_secrets()
    except FileNotFoundError:
        if (ROOT / ".env").exists():
            return app.load_env(ROOT / ".env")
        print("No secrets yet. Run: solver init")
        raise SystemExit(1)


def _running() -> list[int]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
         "Where-Object { $_.CommandLine -like '*screen-solver*app.py*' }).ProcessId"],
        capture_output=True, text=True,
    )
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def _security_checks() -> list[tuple[str, bool]]:
    checks = []
    code = (ROOT / "app.py").read_text(encoding="utf-8")
    checks.append(("Transport uses HTTPS only",
                   "https://api.telegram.org" in code and "http://" not in code))
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    checks.append(("Secrets excluded from git", ".env" in gi and "secret.dat" in gi))
    enc = (ROOT / "secret.dat").exists()
    readable = False
    if enc:
        readable = "TELEGRAM" in (ROOT / "secret.dat").read_bytes().decode("utf-8", "ignore")
    checks.append(("Token encrypted at rest (DPAPI)", enc and not readable))
    checks.append(("No plaintext .env on disk", not (ROOT / ".env").exists()))
    dec_ok = False
    if enc:
        try:
            s = secrets_store.load_secrets()
            dec_ok = bool(s.get("TELEGRAM_BOT_TOKEN")) and bool(s.get("TELEGRAM_CHAT_ID"))
        except Exception:
            dec_ok = False
    checks.append(("Encrypted token decrypts for your account", dec_ok))
    shots = ROOT / "shots"
    checks.append(("No leftover screenshots", (not shots.exists()) or not any(shots.iterdir())))
    return checks


def _state() -> dict:
    pids = _running()
    try:
        cfg = _cfg()
        hotkey, model = cfg.get("hotkey", "?"), cfg.get("model", "?")
    except Exception:
        hotkey = model = "(config error)"
    checks = _security_checks()
    return {"pids": pids, "hotkey": hotkey, "model": model,
            "passed": sum(ok for _, ok in checks), "total": len(checks)}


MENU = [
    ("test", "Test now", "capture + send this screen"),
    ("start", "Start", "run in background"),
    ("stop", "Stop", "stop the background app"),
    ("restart", "Restart", "stop then start"),
    ("options", "Settings", "hotkey, model, zoom, crop"),
    ("setup", "Set token", "store encrypted"),
    ("verify", "Security check", "PASS/FAIL report"),
    ("log", "View log", "recent lines"),
    ("exit", "Exit", ""),
]


# --------------------------------------------------------- rich rendering ----
def _rich_header():
    g = Group(
        Text(BANNER.strip("\n"), style=f"bold {ACCENT}"),
        Text(f"Screen Solver  v{VERSION}", style="white"),
        Text("screen → Claude → Telegram", style=MUTE),
    )
    return g


def _rich_dashboard():
    s = _state()
    status = (Text("● running", style=f"bold {OK}") + Text(f"  pid {s['pids'][0]}", style=MUTE)
              if s["pids"] else Text("○ stopped", style=f"bold {BAD}"))
    sec = (Text(f"✓ {s['passed']}/{s['total']} passing", style=OK)
           if s["passed"] == s["total"] else Text(f"! {s['passed']}/{s['total']} passing", style=WARN))
    t = Table.grid(padding=(0, 2))
    t.add_column(style=MUTE, justify="right")
    t.add_column()
    t.add_row("status", status)
    t.add_row("hotkey", Text(s["hotkey"], style=WARN))
    t.add_row("model", Text(s["model"], style="white"))
    t.add_row("security", sec)
    return Panel(t, title="[bold]dashboard[/]", title_align="left",
                 box=ROUNDED, border_style="grey37", padding=(1, 2))


# -------------------------------------------------------- plain rendering ----
def _plain_dashboard() -> None:
    s = _state()
    st = f"running (pid {s['pids'][0]})" if s["pids"] else "stopped"
    print("\n  ==== Screen Solver ====")
    print(f"  status   : {st}")
    print(f"  hotkey   : {s['hotkey']}")
    print(f"  model    : {s['model']}")
    print(f"  security : {s['passed']}/{s['total']} checks")
    if not HAS_RICH:
        print("  (plain mode — for the pretty UI: solver doctor)")
    print()


def _choose() -> str | None:
    if HAS_RICH:
        try:
            import questionary
            from questionary import Style
            style = Style([("qmark", ACCENT), ("pointer", ACCENT),
                           ("highlighted", f"bold {ACCENT}"), ("selected", OK)])
            return questionary.select(
                "What now?",
                choices=[questionary.Choice(f"{label:<14} {hint}", value=key)
                         for key, label, hint in MENU],
                style=style, qmark="◆", instruction="(↑↓ + enter)").ask()
        except Exception:
            pass
    for i, (_, label, hint) in enumerate(MENU, 1):
        print(f"   {i}. {label:<14} {hint}")
    raw = input("  choose: ").strip()
    idx = int(raw) - 1 if raw.isdigit() else -1
    return MENU[idx][0] if 0 <= idx < len(MENU) else None


def say(msg: str, color: str = "") -> None:
    if HAS_RICH:
        console.print(f"[{color}]{msg}[/]" if color else msg)
    else:
        print(msg)


# --------------------------------------------------------------- commands ----
def cmd_doctor() -> None:
    py = sys.executable
    print(f"python : {py}")
    for mod in ("rich", "questionary", "PIL", "requests"):
        try:
            __import__(mod)
            say(f"[{OK}]ok[/]  {mod}" if HAS_RICH else f"ok  {mod}")
        except ImportError:
            say(f"[{BAD}]MISSING[/]  {mod}" if HAS_RICH else f"MISSING  {mod}")
    if not HAS_RICH:
        print("\nInstall the pretty UI with:")
        print(f'  "{py}" -m pip install rich questionary')


def cmd_test() -> None:
    cfg = _cfg()
    sec = _load_secrets()
    tg = app.Telegram(sec["TELEGRAM_BOT_TOKEN"], sec["TELEGRAM_CHAT_ID"])
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude.exe")
    if HAS_RICH:
        with console.status("[bold]Capturing and asking Claude…[/]", spinner="dots"):
            app.solve(claude, tg, cfg)
    else:
        print("Capturing and asking Claude...")
        app.solve(claude, tg, cfg)
    say("✓ Sent. Check Telegram.", OK)


def cmd_status() -> None:
    pids = _running()
    say(f"● running (pid {pids[0]})" if pids else "○ not running — start with: solver start",
        OK if pids else BAD)


def cmd_start() -> None:
    if _running():
        say("Already running.", WARN)
        return
    subprocess.run(["wscript.exe", str(ROOT / "start.vbs")])
    say("✓ Started.", OK)


def cmd_stop() -> None:
    subprocess.run([str(ROOT / "stop.bat")], shell=True)
    say("✓ Stopped.", OK)


def cmd_restart() -> None:
    cmd_stop()
    cmd_start()


def _build_hotkey() -> str | None:
    """Interactive hotkey builder: pick modifiers, then the key."""
    try:
        import questionary
    except Exception:
        raw = input("  New hotkey (e.g. ctrl+alt+s): ").strip()
        return raw or None
    mods = questionary.checkbox(
        "Modifiers (space to toggle, at least one):",
        choices=["ctrl", "alt", "shift", "win"]).ask()
    if not mods:
        say("Need at least one modifier.", WARN)
        return None
    key = questionary.text(
        "Key (a letter, digit, F1–F12, or a name like add / up):").ask()
    if not key:
        return None
    combo = "+".join(mods + [key.strip().lower()])
    try:
        app.parse_hotkey(combo)
    except ValueError as e:
        say(f"Invalid: {e}", BAD)
        return None
    return combo


def cmd_options() -> None:
    """Interactive settings editor (no need to open the file)."""
    try:
        cfg = _cfg()
    except Exception as e:
        say(f"config error: {e}", BAD)
        return
    fields = [
        ("hotkey", f"Capture hotkey  [{cfg.get('hotkey')}]"),
        ("model", f"Model  [{cfg.get('model')}]"),
        ("zoom", f"Zoom  [{cfg.get('zoom', 1.0)}]"),
        ("crop_top", f"Crop top %  [{cfg.get('crop_top', 0)}]"),
        ("crop_bottom", f"Crop bottom %  [{cfg.get('crop_bottom', 0)}]"),
        ("advanced", "Open full config file"),
        ("back", "Back"),
    ]
    try:
        import questionary
        choice = questionary.select(
            "Edit which setting?",
            choices=[questionary.Choice(label, value=key) for key, label in fields]).ask()
    except Exception:
        for i, (_, label) in enumerate(fields, 1):
            print(f"  {i}. {label}")
        raw = input("  choose: ").strip()
        choice = fields[int(raw) - 1][0] if raw.isdigit() and 1 <= int(raw) <= len(fields) else None

    if choice in (None, "back"):
        return
    if choice == "advanced":
        subprocess.run(["notepad", str(ROOT / "config.toml")])
        return
    if choice == "hotkey":
        combo = _build_hotkey()
        if combo:
            app.update_config({"hotkey": combo})
            say(f"✓ Hotkey set to {combo}. Restarting…", OK)
            cmd_restart()
        return
    if choice == "model":
        try:
            import questionary
            val = questionary.select("Model:", choices=["haiku", "sonnet", "opus"]).ask()
        except Exception:
            val = input("  model (haiku/sonnet/opus): ").strip()
        if val:
            app.update_config({"model": val})
            say(f"✓ Model = {val}. Restarting…", OK)
            cmd_restart()
        return
    # numeric fields: zoom, crop_top, crop_bottom
    raw = input(f"  New value for {choice}: ").strip()
    try:
        val = float(raw) if choice == "zoom" else int(raw)
    except ValueError:
        say("Not a number.", BAD)
        return
    app.update_config({choice: val})
    say(f"✓ {choice} = {val}. Restarting…", OK)
    cmd_restart()


def cmd_config() -> None:
    subprocess.run(["notepad", str(ROOT / "config.toml")])


def cmd_setup() -> None:
    subprocess.run([sys.executable, str(ROOT / "secrets_store.py")])


def cmd_log() -> None:
    logf = ROOT / "app.log"
    if not logf.exists():
        say("No log yet.", MUTE)
        return
    lines = logf.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
    text = "\n".join(lines) or "(empty)"
    if HAS_RICH:
        console.print(Panel(text, title="app.log", box=ROUNDED, border_style="grey37"))
    else:
        print(text)


def cmd_verify() -> None:
    checks = _security_checks()
    if HAS_RICH:
        t = Table(box=ROUNDED, border_style="grey37", show_header=True,
                  header_style=f"bold {ACCENT}")
        t.add_column("", width=6, justify="center")
        t.add_column("check")
        for name, ok in checks:
            t.add_row(f"[{OK}]PASS[/]" if ok else f"[{BAD}]FAIL[/]", name)
        console.print(t)
    else:
        for name, ok in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if all(ok for _, ok in checks):
        say("All checks pass.", OK)
    else:
        say("Some fail — if token not encrypted yet, run: solver init", WARN)


def _check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    color = OK if ok else BAD
    tail = f"  [{MUTE}]{detail}[/]" if (HAS_RICH and detail) else (f"  {detail}" if detail else "")
    if HAS_RICH:
        console.print(f"  [{color}]{mark}[/] {label}{tail}")
    else:
        print(f"  {mark} {label}{('  ' + detail) if detail else ''}")


def _banner_only() -> None:
    if HAS_RICH:
        console.print(Text(BANNER.strip("\n"), style=f"bold {ACCENT}"))
        console.print(f"[white]Screen Solver[/]  [{MUTE}]v{VERSION}[/]")
        console.print(f"[{MUTE}]screen → Claude → Telegram[/]\n")
    else:
        print(BANNER)
        print(f"Screen Solver  v{VERSION}\n")


def cmd_init() -> None:
    console.clear() if HAS_RICH else None
    _banner_only()

    say("Preparing your setup…\n", "bold" if HAS_RICH else "")
    py_ok = sys.version_info >= (3, 11)
    _check("Python 3.11+", py_ok, sys.version.split()[0])
    claude = shutil.which("claude")
    _check("Claude Code CLI", bool(claude), "runs on your subscription (no API key)")
    deps_ok = True
    for m in ("rich", "questionary", "PIL", "requests"):
        try:
            __import__(m)
        except ImportError:
            deps_ok = False
    _check("Dependencies installed", deps_ok, "" if deps_ok else "run: solver doctor")
    print()

    if not (ROOT / "secret.dat").exists():
        say("Now connect your Telegram bot:", "bold" if HAS_RICH else "")
        print("  1. In Telegram, message @BotFather → /newbot → copy the token.")
        print("  2. Send your new bot any message once.\n")
        cmd_setup()
    if (ROOT / ".env").exists():
        try:
            (ROOT / ".env").unlink()
        except OSError:
            pass
    _check("Telegram token stored (encrypted)", (ROOT / "secret.dat").exists())

    cmd_start()
    _check("Background app running", bool(_running()))
    print()

    hotkey = "your hotkey"
    try:
        hotkey = _cfg().get("hotkey", hotkey)
    except Exception:
        pass
    say(f"Ready. Press {hotkey} to solve a screen.", "bold " + OK if HAS_RICH else "")
    print()
    print("Next:")
    print(f"  1  Press {hotkey} anywhere — the answer lands in Telegram.")
    print("  2  Run  solver  for the dashboard and menu.")
    print("  3  Run  solver verify  to check security anytime.")


COMMANDS = {
    "init": cmd_init, "test": cmd_test, "status": cmd_status, "start": cmd_start,
    "stop": cmd_stop, "restart": cmd_restart, "config": cmd_config,
    "options": cmd_options, "setup": cmd_setup, "verify": cmd_verify,
    "log": cmd_log, "doctor": cmd_doctor,
}


def interactive() -> None:
    if HAS_RICH:
        console.clear()
        console.print(_rich_header())
    while True:
        if HAS_RICH:
            console.print(_rich_dashboard())
        else:
            _plain_dashboard()
        choice = _choose()
        if choice in (None, "exit"):
            say("bye", MUTE)
            return
        if HAS_RICH:
            console.print(Rule(style="grey30"))
        COMMANDS[choice]()
        input("\n  press Enter for the menu… ")
        if HAS_RICH:
            console.clear()
            console.print(_rich_header())


def main() -> None:
    if len(sys.argv) < 2:
        interactive()
        return
    arg = sys.argv[1].lstrip("-").lower()
    if arg in ("help", "h"):
        print(__doc__)
        return
    if arg not in COMMANDS:
        print(f"Unknown command: {sys.argv[1]}")
        print("Commands: " + ", ".join(COMMANDS) + " — or run 'solver'.")
        return
    COMMANDS[arg]()


if __name__ == "__main__":
    main()
