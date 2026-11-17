"""Automatic credential & cookie discovery for Claude (CLI, Keyring, Desktop, Browsers)."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sqlite3
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Any


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("dwInfoVersion", wintypes.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wintypes.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wintypes.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wintypes.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wintypes.ULONG),
        ("cbAAD", wintypes.ULONG),
        ("cbData", ctypes.c_uint64),
        ("dwFlags", wintypes.ULONG),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _dpapi_decrypt(raw_bytes: bytes) -> bytes | None:
    if sys.platform != "win32" or not raw_bytes:
        return None
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = (ctypes.c_ubyte * len(raw_bytes)).from_buffer_copy(raw_bytes)
    in_blob = _DATA_BLOB(len(raw_bytes), buf)
    out_blob = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        return None
    try:
        return bytes(ctypes.string_at(out_blob.pbData, out_blob.cbData))
    finally:
        k32.LocalFree(out_blob.pbData)


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes | None:
    if sys.platform != "win32":
        return None
    bcrypt = ctypes.WinDLL("bcrypt")
    h_alg = ctypes.c_void_p()
    h_key = ctypes.c_void_p()
    if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0) != 0:
        return None
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        if (
            bcrypt.BCryptSetProperty(
                h_alg, "ChainingMode", mode, ctypes.sizeof(mode), 0
            )
            != 0
        ):
            return None
        key_buf = (ctypes.c_ubyte * len(key)).from_buffer_copy(key)
        if (
            bcrypt.BCryptGenerateSymmetricKey(
                h_alg, ctypes.byref(h_key), None, 0, key_buf, len(key), 0
            )
            != 0
        ):
            return None
        try:
            n_buf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
            t_buf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
            info = _BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO()
            info.cbSize = ctypes.sizeof(info)
            info.dwInfoVersion = 1
            info.pbNonce = n_buf
            info.cbNonce = len(nonce)
            info.pbTag = t_buf
            info.cbTag = len(tag)

            ct_len = len(ciphertext)
            ct_buf = (
                (ctypes.c_ubyte * ct_len).from_buffer_copy(ciphertext)
                if ct_len > 0
                else (ctypes.c_ubyte * 1)()
            )
            pt_buf = (ctypes.c_ubyte * max(1, ct_len))()
            out_len = wintypes.ULONG()
            st = bcrypt.BCryptDecrypt(
                h_key,
                ct_buf,
                ct_len,
                ctypes.byref(info),
                None,
                0,
                pt_buf,
                ct_len,
                ctypes.byref(out_len),
                0,
            )
            if st != 0:
                return None
            return bytes(pt_buf[: out_len.value])
        finally:
            bcrypt.BCryptDestroyKey(h_key)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


def _read_bytes_shared(path: Path) -> tuple[bytes | None, str | None]:
    """Read a possibly-locked file using Windows FILE_SHARE_ALL.

    Returns (bytes, error_reason) where error_reason may be 'locked'.
    """
    if sys.platform != "win32":
        try:
            return path.read_bytes(), None
        except Exception:
            return None, "io_error"

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE

    GENERIC_READ = 0x80000000
    FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    handle = k32.CreateFileW(
        str(path), GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING, 0, None
    )
    if handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        if err == 32:
            return None, "locked"
        return None, f"win_err_{err}"

    try:
        chunks = bytearray()
        buf = ctypes.create_string_buffer(65536)
        read_bytes = wintypes.DWORD()
        while (
            k32.ReadFile(handle, buf, 65536, ctypes.byref(read_bytes), None)
            and read_bytes.value > 0
        ):
            chunks.extend(buf.raw[: read_bytes.value])
        return bytes(chunks), None
    finally:
        k32.CloseHandle(handle)


def _parse_claude_oauth_json(raw_text: str) -> tuple[str | None, str | None]:
    try:
        data = json.loads(raw_text)
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else data
    if not isinstance(oauth, dict):
        return None, None
    token = (oauth.get("accessToken") or "").strip() or None
    tier = oauth.get("rateLimitTier")
    return token, str(tier) if tier else None


def read_claude_file_oauth() -> tuple[str | None, str | None]:
    cred_path = Path.home() / ".claude" / ".credentials.json"
    if not cred_path.is_file():
        return None, None
    try:
        return _parse_claude_oauth_json(cred_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None


def read_claude_keyring_oauth() -> tuple[str | None, str | None]:
    """Read Claude Code credentials from Windows Credential Manager (advapi32)."""
    if sys.platform != "win32":
        return None, None

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]

    CRED_TYPE_GENERIC = 1
    candidates = ["Claude Code-credentials"]
    user = os.environ.get("USERNAME", "").strip()
    if user:
        candidates.append(f"Claude Code-credentials-{user}")

    for target in candidates:
        p_cred = ctypes.POINTER(_CREDENTIALW)()
        if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(p_cred)):
            continue
        try:
            cred = p_cred.contents
            if not cred.CredentialBlob or cred.CredentialBlobSize <= 0:
                continue
            blob = bytes(ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize))
            for encoding in ("utf-8", "utf-16-le"):
                try:
                    text = blob.decode(encoding).strip("\x00").strip()
                    if text.startswith("{"):
                        token, tier = _parse_claude_oauth_json(text)
                        if token:
                            return token, tier
                except Exception:
                    continue
        finally:
            advapi32.CredFree(p_cred)

    return None, None


def _claude_desktop_store_dirs() -> list[Path]:
    """Return candidate Claude Desktop profile directories ordered by newest Cookies."""
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))

    candidates: list[Path] = []

    claude_data = local / "Claude-Data"
    if claude_data.is_dir():
        for entry in claude_data.iterdir():
            if entry.is_dir() and entry.name.lower().startswith("claude_"):
                candidates.append(entry)

    candidates.append(roaming / "Claude")

    packages = local / "Packages"
    if packages.is_dir():
        for entry in packages.iterdir():
            if entry.is_dir() and entry.name.lower().startswith("claude_"):
                candidates.append(entry / "LocalCache" / "Roaming" / "Claude")

    candidates.append(local / "AnthropicClaude")

    def cookie_mtime(store: Path) -> float:
        for rel in (Path("Network") / "Cookies", Path("Cookies")):
            p = store / rel
            if p.is_file():
                try:
                    return p.stat().st_mtime
                except Exception:
                    return 0.0
        return -1.0

    valid = [d for d in candidates if cookie_mtime(d) >= 0]
    valid.sort(key=cookie_mtime, reverse=True)
    return valid


def _chromium_master_key(local_state_path: Path) -> tuple[bytes | None, bool]:
    raw_bytes, _ = _read_bytes_shared(local_state_path)
    if not raw_bytes:
        return None, False
    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return None, False
    os_crypt = data.get("os_crypt") or {}
    has_abe = bool(os_crypt.get("app_bound_encrypted_key"))
    enc_b64 = os_crypt.get("encrypted_key")
    if not enc_b64:
        return None, has_abe
    try:
        enc = base64.b64decode(enc_b64)
    except Exception:
        return None, has_abe
    if len(enc) < 5 or enc[:5] != b"DPAPI":
        return None, has_abe
    return _dpapi_decrypt(enc[5:]), has_abe


def _decrypt_chromium_cookie(blob: bytes, key: bytes | None) -> str | None:
    if not blob:
        return ""
    if blob[:3] == b"v20":
        return None
    if len(blob) >= 31 and blob[:3] in (b"v10", b"v11"):
        if not key:
            return None
        nonce = blob[3:15]
        body = blob[15:]
        if len(body) < 16:
            return None
        ciphertext = body[:-16]
        tag = body[-16:]
        pt = _aes_gcm_decrypt(key, nonce, ciphertext, tag)
        if pt is None:
            return None
        if len(pt) > 32 and any(b < 32 or b > 127 for b in pt[:32]):
            pt = pt[32:]
        try:
            return pt.decode("utf-8")
        except UnicodeDecodeError:
            return None

    dec = _dpapi_decrypt(blob)
    if dec is None:
        return None
    try:
        return dec.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _extract_session_from_chromium_store(
    cookies_db: Path, local_state: Path
) -> tuple[str | None, str | None, str | None]:
    """Return (session_key, full_cookie_header, status_reason)."""
    key, _ = _chromium_master_key(local_state)
    raw_db, err_reason = _read_bytes_shared(cookies_db)
    if not raw_db:
        return None, None, err_reason

    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "Cookies"
        tmp_db.write_bytes(raw_db)
        try:
            conn = sqlite3.connect(str(tmp_db))
            rows = conn.execute(
                "SELECT name, encrypted_value, value FROM cookies WHERE host_key LIKE '%claude.ai%'"
            ).fetchall()
            conn.close()
        except Exception:
            return None, None, "sqlite_error"

    pairs: list[str] = []
    session_key: str | None = None
    for name, enc_val, plain_val in rows:
        val: str | None = None
        if enc_val:
            val = _decrypt_chromium_cookie(bytes(enc_val), key)
        elif plain_val:
            val = str(plain_val)
        if val:
            pairs.append(f"{name}={val}")
            if name == "sessionKey":
                session_key = val.strip()

    if session_key:
        return session_key, "; ".join(pairs), None
    return None, None, "no_session"


def _extract_session_from_firefox() -> tuple[str | None, str | None]:
    roaming = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    profiles_dir = roaming / "Mozilla" / "Firefox" / "Profiles"
    if not profiles_dir.is_dir():
        return None, None
    for prof in profiles_dir.iterdir():
        db = prof / "cookies.sqlite"
        if not db.is_file():
            continue
        raw_db, _ = _read_bytes_shared(db)
        if not raw_db:
            continue
        with tempfile.TemporaryDirectory() as td:
            tmp_db = Path(td) / "cookies.sqlite"
            tmp_db.write_bytes(raw_db)
            try:
                conn = sqlite3.connect(str(tmp_db))
                rows = conn.execute(
                    "SELECT name, value FROM moz_cookies WHERE host LIKE '%claude.ai%'"
                ).fetchall()
                conn.close()
            except Exception:
                continue
        pairs: list[str] = []
        session_key: str | None = None
        for name, val in rows:
            if val:
                pairs.append(f"{name}={val}")
                if name == "sessionKey":
                    session_key = str(val).strip()
        if session_key:
            return session_key, "; ".join(pairs)
    return None, None


def read_claude_desktop_cookies() -> tuple[str | None, str | None, str | None]:
    """Attempt to read claude.ai sessionKey from Claude Desktop or Firefox.

    Returns (session_key, full_cookie_header, status_hint) where status_hint may be
    'locked' if Claude Desktop is running and holding an exclusive lock on Cookies.
    """
    saw_locked = False
    for store in _claude_desktop_store_dirs():
        local_state = store / "Local State"
        cookies_db = store / "Network" / "Cookies"
        if not cookies_db.is_file():
            cookies_db = store / "Cookies"
        if not (cookies_db.is_file() and local_state.is_file()):
            continue
        sk, header, reason = _extract_session_from_chromium_store(cookies_db, local_state)
        if sk:
            return sk, header, None
        if reason == "locked":
            saw_locked = True

    sk_ff, header_ff = _extract_session_from_firefox()
    if sk_ff:
        return sk_ff, header_ff, None

    return None, None, ("locked" if saw_locked else None)


def has_claude_desktop_installed() -> bool:
    return len(_claude_desktop_store_dirs()) > 0
