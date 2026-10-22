"""Screen solver: global hotkey -> screenshot -> Claude -> answer in Telegram.

No window, no memory. Each hotkey press is one isolated question.
Claude is reached through the Claude Code CLI in headless mode (`claude -p`),
so it runs on the logged-in Claude subscription instead of a paid API key.
Run with pythonw.exe so there is no console.
"""
import ctypes
import html
import io
import logging
import queue
import re
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
    keys are supported (hotkey, model, mouse_button, crop_*), which is all we tune.
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


_FENCE = re.compile(r"```[ \t]*([\w+.-]*)[ \t]*\n?(.*?)```", re.DOTALL)
_INLINE = re.compile(r"`([^`\n]+)`")
_HTML_LIMIT = 3800  # under Telegram's 4096 cap, leaving room for tags


def _safe_lang(lang: str) -> str:
    return re.sub(r"[^\w+.-]", "", lang)[:20]


def _render_text(seg: str) -> str:
    """Escape a prose segment and turn inline `code` into <code> tags."""
    out, i = [], 0
    for m in _INLINE.finditer(seg):
        out.append(html.escape(seg[i:m.start()]))
        out.append("<code>" + html.escape(m.group(1)) + "</code>")
        i = m.end()
    out.append(html.escape(seg[i:]))
    return "".join(out)


def _split_plain(rendered: str, limit: int) -> list[str]:
    """Split already-HTML prose at newline boundaries so tags stay balanced."""
    out, cur = [], ""
    for line in rendered.split("\n"):
        while len(line) > limit:            # pathological single line
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        add = ("\n" if cur else "") + line
        if len(cur) + len(add) > limit:
            out.append(cur)
            cur = line
        else:
            cur += add
    if cur.strip():
        out.append(cur)
    return out


def _code_messages(body: str, lang: str, limit: int) -> list[str]:
    """One or more <pre><code> messages for a code block, never breaking tags."""
    open_tag = "<pre><code%s>" % (f' class="language-{_safe_lang(lang)}"' if lang else "")
    close = "</code></pre>"
    budget = max(256, limit - len(open_tag) - len(close))
    step = max(32, budget // 6)             # entity-safe raw chunk size
    lines: list[str] = []
    for line in body.strip("\n").split("\n"):
        if len(line) <= step:
            lines.append(line)
        else:
            lines.extend(line[j:j + step] for j in range(0, len(line), step))
    out, cur, cur_len = [], [], 0
    for line in lines:
        esc_len = len(html.escape(line)) + (1 if cur else 0)
        if cur and cur_len + esc_len > budget:
            out.append(open_tag + html.escape("\n".join(cur)) + close)
            cur, cur_len = [], 0
            esc_len = len(html.escape(line))
        cur.append(line)
        cur_len += esc_len
    if cur:
        out.append(open_tag + html.escape("\n".join(cur)) + close)
    return out


def to_telegram_html(text: str, limit: int = _HTML_LIMIT) -> list[str]:
    """Turn markdown-ish answer (``` fences, `inline`) into Telegram HTML messages."""
    msgs: list[str] = []
    pos = 0
    for m in _FENCE.finditer(text):
        if m.start() > pos:
            msgs += _split_plain(_render_text(text[pos:m.start()]), limit)
        msgs += _code_messages(m.group(2), m.group(1), limit)
        pos = m.end()
    if pos < len(text):
        msgs += _split_plain(_render_text(text[pos:]), limit)
    return [m for m in msgs if m.strip()] or [html.escape(text) or "(empty answer)"]


_LATEX_FENCE = re.compile(r"```latex[ \t]*\n?(.*?)```", re.DOTALL)


def render_latex_block(lines: list[str]) -> bytes:
    """Render LaTeX equation lines to a crisp PNG (real math typesetting)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    lines = [ln.strip() for ln in lines if ln.strip()] or ["\\ "]
    fig = plt.figure(figsize=(7, 0.85 * len(lines)))
    for i, ln in enumerate(lines):
        y = 1 - (i + 0.5) / len(lines)
        fig.text(0.03, y, f"${ln}$", fontsize=26, va="center", ha="left", color="black")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight",
                pad_inches=0.35, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def send_text(self, text: str) -> None:
        for msg in to_telegram_html(text):
            _post_with_retry(f"{self.base}/sendMessage", timeout=30,
                             data={"chat_id": self.chat_id, "text": msg,
                                   "parse_mode": "HTML"})

    def send_answer(self, text: str) -> None:
        """Send an answer, rendering any ```latex blocks as formula images."""
        pos = 0
        for m in _LATEX_FENCE.finditer(text):
            if text[pos:m.start()].strip():
                self.send_text(text[pos:m.start()])
            try:
                png = render_latex_block(m.group(1).splitlines())
                self.send_photo(png, "")
            except Exception:
                log.exception("latex render failed; sending as text")
                self.send_text("```\n" + m.group(1).strip() + "\n```")
            pos = m.end()
        if text[pos:].strip():
            self.send_text(text[pos:])

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
    # Run in the project ROOT (already a trusted folder) and reference the image
    # by relative path. Running in a brand-new cwd can make Claude Code block on a
    # "do you trust this folder?" prompt until the timeout — the "took too long" bug.
    rel = f"shots/{name}"
    prompt = f"Read the image file {rel}. {cfg['instruction']}"
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
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
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
        answer = None
        for attempt in (1, 2):                       # one retry on timeout
            try:
                answer = ask_claude(claude_exe, cfg, image)
                break
            except subprocess.TimeoutExpired:
                log.warning("claude timeout (attempt %s)", attempt)
        if answer is None:
            answer = ("Error: Claude timed out twice. It may be slow or the first "
                      "run is warming up — try again.")
        tg.send_answer(answer)
    except Exception:
        log.exception("solve failed")
    finally:
        cleanup_shots()


def worker(jobs: queue.Queue, claude_exe, tg, cfg) -> None:
    while True:
        task = jobs.get()
        if task == "solve":
            solve(claude_exe, tg, cfg)


# --- Low-level mouse hook, so an extra mouse button (4=back/X1, 5=fwd/X2) fires --
WH_MOUSE_LL = 14
WM_XBUTTONDOWN = 0x020B
HC_ACTION = 0
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_hook_refs = []  # keep proc + handle alive


def install_mouse_hook(want_button: int, jobs: queue.Queue) -> bool:
    """Fire jobs.put('solve') when mouse button 4 or 5 is pressed. Returns success."""
    if want_button not in (4, 5):
        return False
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE,
                                         wintypes.DWORD]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
                                      wintypes.LPARAM]

    def proc(nCode, wParam, lParam):
        if nCode == HC_ACTION and wParam == WM_XBUTTONDOWN:
            data = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            xbtn = (data.mouseData >> 16) & 0xFFFF  # 1 = X1 (btn4), 2 = X2 (btn5)
            if xbtn + 3 == want_button:             # 1->4, 2->5
                jobs.put("solve")
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    cb = HOOKPROC(proc)
    handle = user32.SetWindowsHookExW(WH_MOUSE_LL, cb, None, 0)
    if not handle:
        log.error("could not install mouse hook: %s", ctypes.get_last_error())
        return False
    _hook_refs.extend([cb, handle])
    return True


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

    triggers = []

    # Mouse button trigger (primary now): 4 = back (X1), 5 = forward (X2), 0 = off.
    mouse_button = int(cfg.get("mouse_button", 0) or 0)
    if mouse_button and install_mouse_hook(mouse_button, jobs):
        triggers.append(f"mouse{mouse_button}")

    # Optional keyboard hotkey for capture (empty in config = disabled).
    hk = (cfg.get("hotkey") or "").strip()
    if hk:
        try:
            mods, vk = parse_hotkey(hk)
            if user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
                triggers.append(hk)
            else:
                log.error("could not register hotkey %s (taken by another app?)", hk)
        except ValueError as e:
            log.error("bad hotkey: %s", e)

    if not triggers:
        log.error("no capture trigger active (set mouse_button or hotkey); exiting")
        sys.exit(1)
    log.info("ready, triggers: %s", triggers)

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            jobs.put("solve")


if __name__ == "__main__":
    main()
