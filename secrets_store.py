"""Encrypted secret storage using Windows DPAPI (per-user, no extra deps).

Secrets (bot token, chat id) are encrypted with CryptProtectData, tied to the
current Windows account, and written to secret.dat. Only the same Windows user
on the same machine can decrypt them; the file is useless if copied elsewhere.
"""
import ctypes
import json
from ctypes import wintypes
from pathlib import Path

SECRET_FILE = Path(__file__).resolve().parent / "secret.dat"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _protect(data: bytes) -> bytes:
    out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_blob(data)), "screen-solver", None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)
    ):
        raise ctypes.WinError()
    try:
        return _blob_bytes(out)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _unprotect(data: bytes) -> bytes:
    out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_blob(data)), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)
    ):
        raise ctypes.WinError()
    try:
        return _blob_bytes(out)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def save_secrets(values: dict) -> None:
    SECRET_FILE.write_bytes(_protect(json.dumps(values).encode("utf-8")))


def load_secrets() -> dict:
    if not SECRET_FILE.exists():
        raise FileNotFoundError(
            "secret.dat missing. Run install.ps1 (or setup.py) to store the token."
        )
    return json.loads(_unprotect(SECRET_FILE.read_bytes()).decode("utf-8"))


if __name__ == "__main__":
    # One-shot setup: prompt for token + chat id, encrypt, store.
    import getpass
    token = getpass.getpass("Telegram bot token (input hidden): ").strip()
    chat_id = input("Telegram chat id: ").strip()
    if not token or not chat_id:
        raise SystemExit("Both values required.")
    save_secrets({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
    print(f"Saved encrypted secrets to {SECRET_FILE}")
