"""Screen solver: global hotkey -> screenshot -> Claude -> answer in Telegram.

No window, no memory. Each hotkey press is one isolated question.
Claude is reached through the Claude Code CLI in headless mode (`claude -p`),
so it runs on the logged-in Claude subscription instead of a paid API key.
Run with pythonw.exe so there is no console.
"""
import ctypes
import io
import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from PIL import ImageGrab

import secrets_store

ROOT = Path(__file__).resolve().parent
SHOTS = ROOT / "shots"
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
MAX_IMAGE_SIDE = 2400
MAX_IMAGE_BYTES = 4_500_000
TELEGRAM_LIMIT = 4000

log = logging.getLogger("screen-solver")


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"')
    return env


def parse_hotkey(combo: str) -> tuple[int, int]:
    """'ctrl+alt+s' -> (modifier flags, virtual key code)."""
    mods, vk = 0, None
    for part in combo.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError(f"Unknown key in hotkey: {part!r}")
    if vk is None or not mods:
        raise ValueError(f"Hotkey needs modifier(s) + one key: {combo!r}")
    return mods, vk


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def grab_screen() -> bytes:
    """Screenshot of the monitor under the mouse cursor, as PNG or JPEG bytes."""
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    monitor = user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(wintypes.HANDLE(monitor), ctypes.byref(info))
    r = info.rcMonitor
    image = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))

    buf = io.BytesIO()
    image.save(buf, "PNG", optimize=True)
    if buf.tell() > MAX_IMAGE_BYTES:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def media_type(data: bytes) -> str:
    return "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"


def _post_with_retry(url: str, *, tries: int = 3, **kwargs):
    """POST over HTTPS, retrying transient network/5xx failures with backoff."""
    last = None
    for attempt in range(tries):
        try:
            r = requests.post(url, **kwargs)
            if r.status_code < 500:
                r.raise_for_status()
                return r
            last = requests.HTTPError(f"{r.status_code} from Telegram")
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
        time.sleep(2 ** attempt)
    raise last


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def send_text(self, text: str) -> None:
        for i in range(0, len(text), TELEGRAM_LIMIT):
            _post_with_retry(f"{self.base}/sendMessage", timeout=30,
                             data={"chat_id": self.chat_id,
                                   "text": text[i:i + TELEGRAM_LIMIT]})

    def send_photo(self, data: bytes, caption: str) -> None:
        ext = "png" if media_type(data) == "image/png" else "jpg"
        _post_with_retry(f"{self.base}/sendPhoto", timeout=60,
                         data={"chat_id": self.chat_id, "caption": caption},
                         files={"photo": (f"screen.{ext}", data)})


def ask_claude(claude_exe: str, cfg: dict, image: bytes) -> str:
    """One isolated headless Claude Code run that reads the screenshot file."""
    SHOTS.mkdir(exist_ok=True)
    name = "screen.png" if media_type(image) == "image/png" else "screen.jpg"
    (SHOTS / name).write_bytes(image)
    prompt = f"Read the image file {name} in the current directory. {cfg['instruction']}"
    # --setting-sources project: skip the user's hooks/plugins/CLAUDE.md so the
    # answer style is controlled only by config.toml. Read is the only tool.
    result = subprocess.run(
        [claude_exe, "-p", prompt,
         "--model", cfg["model"],
         "--system-prompt", cfg["system_prompt"],
         "--setting-sources", "project",
         "--strict-mcp-config",
         "--tools", "Read",
         "--allowedTools", "Read"],
        cwd=SHOTS, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=cfg["timeout_seconds"], stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        log.error("claude exit %s: %s", result.returncode, result.stderr.strip())
        detail = (result.stderr or result.stdout).strip()[:500]
        return f"Error from Claude Code (exit {result.returncode}): {detail}"
    return result.stdout.strip() or "(empty answer)"


def cleanup_shots() -> None:
    """Delete the temp screenshot after use; nothing lingers on disk."""
    if SHOTS.exists():
        for f in SHOTS.iterdir():
            try:
                f.unlink()
            except OSError:
                pass


def solve(claude_exe: str, tg: Telegram, cfg: dict) -> None:
    try:
        image = grab_screen()
        tg.send_photo(image, "Captured. Solving...")
        try:
            answer = ask_claude(claude_exe, cfg, image)
        except subprocess.TimeoutExpired:
            answer = "Error: Claude took too long. Try again."
        tg.send_text(answer)
    except Exception:
        log.exception("solve failed")
    finally:
        cleanup_shots()


def worker(jobs: queue.Queue, claude_exe, tg, cfg) -> None:
    while True:
        jobs.get()
        solve(claude_exe, tg, cfg)


def main() -> None:
    handler = RotatingFileHandler(ROOT / "app.log", maxBytes=500_000, backupCount=2,
                                  encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s")

    kernel32.CreateMutexW(None, False, "ScreenSolverSingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log.info("already running, exiting")
        sys.exit(0)

    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))

    # Prefer the DPAPI-encrypted store; fall back to a legacy plaintext .env.
    try:
        env = secrets_store.load_secrets()
    except FileNotFoundError:
        env = load_env(ROOT / ".env") if (ROOT / ".env").exists() else {}
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not env.get(k)]
    if missing:
        log.error("no secrets found (%s). Run: python secrets_store.py", ", ".join(missing))
        sys.exit(1)

    claude_exe = shutil.which("claude") or str(Path.home() / ".local/bin/claude.exe")
    if not Path(claude_exe).exists():
        log.error("Claude Code CLI not found (looked for %s)", claude_exe)
        sys.exit(1)
    tg = Telegram(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])

    jobs: queue.Queue = queue.Queue()
    threading.Thread(target=worker, args=(jobs, claude_exe, tg, cfg), daemon=True).start()

    mods, vk = parse_hotkey(cfg["hotkey"])
    if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
        log.error("could not register hotkey %s (taken by another app?)", cfg["hotkey"])
        sys.exit(1)
    log.info("ready, hotkey %s", cfg["hotkey"])

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            jobs.put(1)


if __name__ == "__main__":
    main()
