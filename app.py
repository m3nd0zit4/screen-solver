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
MAX_IMAGE_SIDE = 1568  # vision sweet spot: smaller = faster upload + fewer tokens
MAX_IMAGE_BYTES = 4_500_000
TELEGRAM_LIMIT = 4000
CONFIG_PATH = ROOT / "config.toml"

log = logging.getLogger("screen-solver")


def update_config(updates: dict) -> None:
    """Rewrite `key = value` lines in config.toml in place (comments preserved).

    Strings are re-quoted; numbers written raw. Only simple top-level scalar
    keys are supported (hotkey, model, zoom, crop_*), which is all we tune.
    """
    import re
    text = CONFIG_PATH.read_text(encoding="utf-8")
    for key, value in updates.items():
        if isinstance(value, str):
            rendered = f'{key} = "{value}"'
        elif isinstance(value, float):
            rendered = f"{key} = {round(value, 2)}"
        else:
            rendered = f"{key} = {value}"
        pattern = rf'(?m)^{re.escape(key)}\s*=.*$'
        if re.search(pattern, text):
            text = re.sub(pattern, rendered, text, count=1)
        else:
            text += f"\n{rendered}\n"
    CONFIG_PATH.write_text(text, encoding="utf-8")


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"')
    return env


NAMED_KEYS = {
    "add": 0x6B, "plus": 0x6B, "subtract": 0x6D, "minus": 0x6D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09,
}


def parse_hotkey(combo: str) -> tuple[int, int]:
    """'ctrl+alt+s' -> (modifier flags, virtual key code)."""
    mods, vk = 0, None
    for part in combo.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in NAMED_KEYS:
            vk = NAMED_KEYS[part]
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


def _crop(image, cfg: dict):
    """Trim each edge by its configured percentage (0 = keep everything)."""
    w, h = image.size
    left = int(w * max(0, min(90, cfg.get("crop_left", 0))) / 100)
    right = w - int(w * max(0, min(90, cfg.get("crop_right", 0))) / 100)
    top = int(h * max(0, min(90, cfg.get("crop_top", 0))) / 100)
    bottom = h - int(h * max(0, min(90, cfg.get("crop_bottom", 0))) / 100)
    if left < right and top < bottom and (left, top, right, bottom) != (0, 0, w, h):
        return image.crop((left, top, right, bottom))
    return image


def _zoom(image, factor: float):
    """Keep the centre region and enlarge it (factor 1.0 = no change)."""
    try:
        factor = float(factor)
    except (TypeError, ValueError):
        factor = 1.0
    if factor <= 1.0:
        return image
    factor = min(factor, 5.0)
    w, h = image.size
    nw, nh = int(w / factor), int(h / factor)
    left, top = (w - nw) // 2, (h - nh) // 2
    return image.crop((left, top, left + nw, top + nh))


def grab_screen(cfg: dict | None = None) -> bytes:
    """Screenshot of the monitor under the mouse cursor, as PNG or JPEG bytes."""
    cfg = cfg or {}
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    monitor = user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(wintypes.HANDLE(monitor), ctypes.byref(info))
    r = info.rcMonitor
    image = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
    image = _crop(image, cfg)
    image = _zoom(image, cfg.get("zoom", 1.0))
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
        image = grab_screen(cfg)
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


def adjust_zoom(tg: Telegram, cfg: dict, delta: float) -> None:
    """Bump the zoom factor, persist it, and confirm in Telegram (no screen UI)."""
    new = max(1.0, min(5.0, round(cfg.get("zoom", 1.0) + delta, 2)))
    cfg["zoom"] = new
    try:
        update_config({"zoom": new})
    except Exception:
        log.exception("could not persist zoom")
    try:
        tg.send_text(f"🔍 zoom = {new:g}")
    except Exception:
        pass


def worker(jobs: queue.Queue, claude_exe, tg, cfg) -> None:
    while True:
        task = jobs.get()
        if task == "solve":
            solve(claude_exe, tg, cfg)
        elif task == "zoom_in":
            adjust_zoom(tg, cfg, +0.1)
        elif task == "zoom_out":
            adjust_zoom(tg, cfg, -0.1)


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

    # id -> (config key, task). id 1 is the capture hotkey (required).
    bindings = {1: ("hotkey", "solve"),
                2: ("hotkey_zoom_in", "zoom_in"),
                3: ("hotkey_zoom_out", "zoom_out")}
    registered = {}
    for hid, (key, task) in bindings.items():
        combo = (cfg.get(key) or "").strip()
        if not combo:
            continue
        try:
            mods, vk = parse_hotkey(combo)
        except ValueError as e:
            log.error("bad %s: %s", key, e)
            continue
        if user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
            registered[hid] = task
        else:
            log.error("could not register %s=%s (taken by another app?)", key, combo)
    if 1 not in registered:
        log.error("capture hotkey failed to register; exiting")
        sys.exit(1)
    log.info("ready, hotkeys: %s", {cfg.get(bindings[h][0]) for h in registered})

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            task = registered.get(msg.wParam)
            if task:
                jobs.put(task)


if __name__ == "__main__":
    main()
