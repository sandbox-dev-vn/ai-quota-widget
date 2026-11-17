"""Quota fetchers for Antigravity, Codex, and Claude."""

from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import psutil

from .claude_auth import (
    has_claude_desktop_installed,
    read_antigravity_cli_oauth,
    read_claude_desktop_cookies,
    read_claude_file_oauth,
    read_claude_keyring_oauth,
)

CACHE_TTL_SECONDS = 15.0

_CACHE_LOCK = threading.Lock()
_QUOTA_CACHE: dict[str, Any] = {
    "fetched_at_mono": 0.0,
    "payload": None,
}
_LAST_ONLINE_SNAPSHOTS: dict[str, dict[str, Any]] = {}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat().replace("+00:00", "Z")


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            secs = float(raw.strip())
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_unix_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        secs = float(raw)
        if secs <= 0:
            return None
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _format_countdown(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    now = _now_utc()
    delta = dt - now
    total_secs = int(delta.total_seconds())
    if total_secs <= 0:
        return "now"
    total_mins = total_secs // 60
    if total_mins == 0:
        return "<1m"
    hours = total_mins // 60
    mins = total_mins % 60
    if hours >= 24:
        days = hours // 24
        rem_hours = hours % 24
        return f"{days}d" if rem_hours == 0 else f"{days}d {rem_hours}h"
    if hours > 0:
        return f"{hours}h" if mins == 0 else f"{hours}h {mins}m"
    return f"{mins}m"


def _clamp_percent(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def _make_window(
    label: str,
    *,
    used_percent: float | None = None,
    remaining_fraction: float | None = None,
    reset_dt: datetime | None = None,
    window_id: str | None = None,
) -> dict[str, Any]:
    if remaining_fraction is not None:
        rem = _clamp_percent(float(remaining_fraction) * 100.0)
        used = _clamp_percent(100.0 - rem)
    elif used_percent is not None:
        used = _clamp_percent(float(used_percent))
        rem = _clamp_percent(100.0 - used)
    else:
        used = 0.0
        rem = 100.0

    item: dict[str, Any] = {
        "label": label,
        "used_percent": used,
        "remaining_percent": rem,
        "reset_at": reset_dt.isoformat().replace("+00:00", "Z") if reset_dt else None,
        "reset_text": _format_countdown(reset_dt),
    }
    if window_id is not None:
        item["id"] = window_id
    return item


def _jwt_payload(token: str | None) -> dict[str, Any] | None:
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1].strip()
    if not payload_b64:
        return None
    pad = "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 6.0,
    ssl_context: ssl.SSLContext | None = None,
) -> tuple[int, dict[str, Any] | list[Any] | None, str]:
    data_bytes = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
            status = getattr(resp, "status", 200)
            raw_text = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw_text) if raw_text.strip() else None
            except json.JSONDecodeError:
                parsed = None
            return status, parsed, raw_text
    except urllib.error.HTTPError as err:
        raw_text = ""
        try:
            raw_text = err.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return err.code, None, raw_text


_CSRF_RE = re.compile(r"--csrf_token(?:\s+|=)(\S+)")
_EXT_CSRF_RE = re.compile(r"--extension_server_csrf_token(?:\s+|=)(\S+)")
_EXT_PORT_RE = re.compile(r"--extension_server_port(?:\s+|=)(\d+)")
_HTTPS_PORT_RE = re.compile(r"--https_server_port(?:\s+|=)(\d+)")


def _detect_antigravity_processes() -> list[dict[str, Any]]:
    """Return candidate Antigravity processes, IDE (with CSRF) prioritized over CLI."""
    ide_matches: list[dict[str, Any]] = []
    cli_matches: list[dict[str, Any]] = []

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            is_ls = "language_server" in name
            is_agy = name in ("agy", "agy.exe")
            if not (is_ls or is_agy):
                continue

            cmdline_list = proc.info.get("cmdline") or []
            cmdline = " ".join(cmdline_list)
            m_csrf = _CSRF_RE.search(cmdline)
            m_ext_csrf = _EXT_CSRF_RE.search(cmdline)
            m_ext_port = _EXT_PORT_RE.search(cmdline) or _HTTPS_PORT_RE.search(cmdline)

            ports: list[int] = []
            try:
                for conn in proc.net_connections(kind="tcp"):
                    if conn.status == "LISTEN" and conn.laddr:
                        port = int(conn.laddr.port)
                        if port not in ports:
                            ports.append(port)
            except Exception:
                pass

            ext_port = int(m_ext_port.group(1)) if m_ext_port else None
            if ext_port and ext_port > 0:
                for offset in range(0, 11):
                    cand = ext_port + offset
                    if cand not in ports:
                        ports.append(cand)

            entry = {
                "pid": proc.info.get("pid"),
                "source": "ide" if m_csrf else "cli",
                "csrf_token": m_csrf.group(1) if m_csrf else "",
                "ext_csrf_token": m_ext_csrf.group(1) if m_ext_csrf else "",
                "ports": ports,
            }
            if m_csrf:
                ide_matches.append(entry)
            elif is_agy or ports:
                cli_matches.append(entry)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue

    return ide_matches + cli_matches


def _call_antigravity_local(
    port: int,
    endpoint: str,
    body: dict[str, Any],
    tokens: list[str],
    ssl_ctx: ssl.SSLContext,
    timeout: float = 2.5,
) -> tuple[int, dict[str, Any] | None]:
    url = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{endpoint}"
    token_candidates = [t for t in tokens if t] or [""]
    last_status = 0
    for token in token_candidates:
        headers = {
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        }
        if token:
            headers["X-Codeium-Csrf-Token"] = token
        try:
            status, parsed, _ = _http_json(
                url,
                method="POST",
                headers=headers,
                body=body,
                timeout=timeout,
                ssl_context=ssl_ctx,
            )
            last_status = status
            if status == 200 and isinstance(parsed, dict):
                return 200, parsed
            if status not in (401, 403):
                return status, None
        except Exception:
            return 0, None
    return last_status, None


def _count_antigravity_offline_sessions() -> int:
    base = Path.home() / ".gemini" / "antigravity"
    brain_dir = base / "brain"
    count = 0
    try:
        if brain_dir.is_dir():
            count = sum(
                1
                for item in brain_dir.iterdir()
                if item.is_dir() and item.name != "tempmediaStorage" and not item.name.startswith(".")
            )
    except Exception:
        count = 0

    if count == 0:
        conv_dir = base / "conversations"
        try:
            if conv_dir.is_dir():
                count = sum(
                    1
                    for item in conv_dir.iterdir()
                    if item.is_file() and item.suffix.lower() in (".db", ".pb")
                )
        except Exception:
            pass
    return count


def _extract_antigravity_remaining(bucket: dict[str, Any]) -> float | None:
    val = bucket.get("remainingFraction")
    if val is None:
        val = bucket.get("remaining_fraction")
    if val is None and isinstance(bucket.get("remaining"), dict):
        rem = bucket["remaining"]
        val = rem.get("remainingFraction") or rem.get("remaining_fraction")
        if val is None and rem.get("case") == "remainingFraction":
            val = rem.get("value")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _parse_antigravity_quota_summary(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    inner = payload.get("response") or payload.get("summary") or payload
    groups = inner.get("groups") if isinstance(inner, dict) else None
    if not isinstance(groups, list):
        return None, None, []

    primary: dict[str, Any] | None = None
    secondary: dict[str, Any] | None = None
    extra_windows: list[dict[str, Any]] = []

    for group in groups:
        if not isinstance(group, dict):
            continue
        g_name = str(group.get("displayName") or group.get("name") or "").strip()
        g_lower = g_name.lower()
        buckets = group.get("buckets") or []
        if not isinstance(buckets, list):
            continue

        is_gemini_main = "gemini" in g_lower and not any(ch.isdigit() for ch in g_lower)
        is_argon = "argon" in g_lower
        is_third_party = "claude" in g_lower or "gpt" in g_lower

        for bucket in buckets:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            rem_frac = _extract_antigravity_remaining(bucket)
            if rem_frac is None:
                continue

            b_id = str(bucket.get("bucketId") or bucket.get("id") or "").strip()
            b_lower = b_id.lower()
            win_tag = str(bucket.get("window") or "").strip().lower()
            reset_raw = bucket.get("resetTime") or bucket.get("reset_time")
            reset_dt = _parse_iso(reset_raw)

            if is_gemini_main:
                if win_tag in ("5h", "session") or "5h" in b_lower or "session" in b_lower:
                    primary = _make_window(
                        "Gemini 5h",
                        remaining_fraction=rem_frac,
                        reset_dt=reset_dt,
                        window_id="gemini-5h",
                    )
                elif win_tag == "weekly" or "weekly" in b_lower:
                    secondary = _make_window(
                        "Gemini Weekly",
                        remaining_fraction=rem_frac,
                        reset_dt=reset_dt,
                        window_id="gemini-weekly",
                    )
                else:
                    extra_windows.append(
                        _make_window(
                            str(bucket.get("displayName") or b_id or "Gemini"),
                            remaining_fraction=rem_frac,
                            reset_dt=reset_dt,
                            window_id=b_id or "gemini-extra",
                        )
                    )
            elif is_argon:
                suffix = " (7d)" if win_tag == "weekly" or "weekly" in b_lower else ""
                extra_windows.append(
                    _make_window(
                        f"Gemini 4 Argon{suffix}",
                        remaining_fraction=rem_frac,
                        reset_dt=reset_dt,
                        window_id=b_id or "gemini-argon",
                    )
                )
            elif is_third_party:
                if win_tag in ("5h", "session") or "5h" in b_lower:
                    label = "Claude/GPT 5h"
                elif win_tag == "weekly" or "weekly" in b_lower:
                    label = "Claude/GPT 7d"
                else:
                    label = str(bucket.get("displayName") or b_id or "Claude/GPT")
                extra_windows.append(
                    _make_window(
                        label,
                        remaining_fraction=rem_frac,
                        reset_dt=reset_dt,
                        window_id=b_id or label.lower().replace(" ", "-"),
                    )
                )
            else:
                b_name = str(bucket.get("displayName") or bucket.get("name") or b_id or "Quota")
                extra_windows.append(
                    _make_window(
                        f"{g_name} · {b_name}".strip(" ·"),
                        remaining_fraction=rem_frac,
                        reset_dt=reset_dt,
                        window_id=b_id or "extra",
                    )
                )

    return primary, secondary, extra_windows


def _parse_antigravity_user_status(
    payload: dict[str, Any],
) -> tuple[str | None, str | None, list[dict[str, str]], dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    us = payload.get("userStatus")
    if not isinstance(us, dict):
        return None, None, [], None, None, []

    email = us.get("email")
    plan: str | None = None
    badges: list[dict[str, str]] = []

    plan_status = us.get("planStatus")
    if isinstance(plan_status, dict):
        plan_info = plan_status.get("planInfo")
        if isinstance(plan_info, dict):
            plan = plan_info.get("planDisplayName") or plan_info.get("planName")
        prompt_credits = plan_status.get("availablePromptCredits")
        flow_credits = plan_status.get("availableFlowCredits")
        if prompt_credits is not None:
            badges.append({"label": "Prompt", "value": str(prompt_credits)})
        if flow_credits is not None:
            badges.append({"label": "Flow", "value": str(flow_credits)})

    if not plan and isinstance(us.get("userTier"), dict):
        plan = us["userTier"].get("name") or us["userTier"].get("description")

    primary: dict[str, Any] | None = None
    secondary: dict[str, Any] | None = None
    extra_windows: list[dict[str, Any]] = []
    configs = (us.get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []
    if isinstance(configs, list):
        for cfg in configs:
            if not isinstance(cfg, dict):
                continue
            qi = cfg.get("quotaInfo")
            if not isinstance(qi, dict) or qi.get("remainingFraction") is None:
                continue
            label = str(cfg.get("label") or cfg.get("modelId") or "Model").strip()
            rem = float(qi["remainingFraction"])
            reset_dt = _parse_iso(qi.get("resetTime"))
            win = _make_window(
                label,
                remaining_fraction=rem,
                reset_dt=reset_dt,
                window_id=label.lower().replace(" ", "-"),
            )
            lower = label.lower()
            if "argon" in lower:
                extra_windows.append(win)
            elif "claude" in lower and primary is None:
                primary = win
            elif "gemini" in lower and "pro" in lower and secondary is None:
                secondary = win
            elif len(extra_windows) < 6:
                extra_windows.append(win)

    return email, plan, badges, primary, secondary, extra_windows


def _finalize_antigravity_online(
    *,
    source_text: str,
    plan: str | None,
    email: str | None,
    primary: dict[str, Any] | None,
    secondary: dict[str, Any] | None,
    extra_windows: list[dict[str, Any]],
    badges: list[dict[str, str]],
) -> dict[str, Any]:
    # Surface Argon right after Gemini 5h/Weekly, before 3p models
    extra_windows.sort(
        key=lambda w: (
            0 if "argon" in w["label"].lower() else 1,
            w["label"],
        )
    )
    result = {
        "id": "antigravity",
        "name": "Antigravity",
        "status": "online",
        "status_text": source_text,
        "plan": plan,
        "account": email,
        "primary": primary,
        "secondary": secondary,
        "extra_windows": extra_windows,
        "badges": badges,
        "error": None,
        "updated_at": _now_iso(),
    }
    _LAST_ONLINE_SNAPSHOTS["antigravity"] = result
    return result


def _format_antigravity_cloud_plan(load_data: dict[str, Any]) -> str | None:
    for key in ("paidTier", "currentTier"):
        tier = load_data.get(key)
        if not isinstance(tier, dict):
            continue
        t_id = str(tier.get("id") or "").lower()
        t_name = str(tier.get("name") or "").strip()
        if "ultra" in t_id or "ultra" in t_name.lower():
            return "Ultra"
        if "pro" in t_id or "pro" in t_name.lower():
            return "Pro"
        if "free" in t_id:
            if key == "paidTier":
                return "Free"
            continue
        if t_name:
            return t_name
    return None


def _call_antigravity_cloud(
    access_token: str,
    method: str,
    body: dict[str, Any],
    hosts: tuple[str, ...] = ("daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com"),
) -> tuple[int, dict[str, Any] | None]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/1.5.10 win32/amd64",
    }
    last_status = 0
    for host in hosts:
        try:
            status, parsed, _ = _http_json(
                f"https://{host}/v1internal:{method}",
                method="POST",
                headers=headers,
                body=body,
                timeout=6.0,
            )
            last_status = status
            if status == 200 and isinstance(parsed, dict):
                return 200, parsed
            if status in (401, 403):
                return status, None
        except Exception:
            continue
    return last_status, None


def _pick_bucket(buckets: list[dict[str, Any]], preferred_ids: tuple[str, ...], substring: str) -> dict[str, Any] | None:
    by_id = {str(b.get("modelId") or "").lower(): b for b in buckets if isinstance(b, dict)}
    for pid in preferred_ids:
        b = by_id.get(pid)
        if b and _extract_antigravity_remaining(b) is not None:
            return b
    for b in buckets:
        if not isinstance(b, dict):
            continue
        mid = str(b.get("modelId") or "").lower()
        if substring in mid and _extract_antigravity_remaining(b) is not None:
            return b
    return None


def _fetch_antigravity_cloud(cred: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    token_obj = cred.get("token") or {}
    access_token = str(token_obj.get("access_token") or "").strip()
    if not access_token:
        return None, False

    email = str(cred.get("id_token_email") or "").strip() or None
    st_lc, load_data = _call_antigravity_cloud(
        access_token,
        "loadCodeAssist",
        {"metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}},
    )
    if st_lc in (401, 403):
        return None, True

    plan = _format_antigravity_cloud_plan(load_data) if isinstance(load_data, dict) else None
    proj = (load_data or {}).get("cloudaicompanionProject") if isinstance(load_data, dict) else None
    rpc_body: dict[str, Any] = {"project": proj} if proj else {}

    st_sum, sum_data = _call_antigravity_cloud(access_token, "retrieveUserQuotaSummary", rpc_body)
    if st_sum in (401, 403):
        return None, True

    primary: dict[str, Any] | None = None
    secondary: dict[str, Any] | None = None
    extra_windows: list[dict[str, Any]] = []
    if st_sum == 200 and sum_data:
        primary, secondary, extra_windows = _parse_antigravity_quota_summary(sum_data)

    st_q, q_data = _call_antigravity_cloud(access_token, "retrieveUserQuota", rpc_body)
    if st_q in (401, 403):
        return None, True

    buckets = (q_data or {}).get("buckets") or [] if isinstance(q_data, dict) else []
    if isinstance(buckets, list):
        if primary is None:
            gem_b = _pick_bucket(
                buckets,
                ("gemini-4-hydrogen-sonar", "gemini-2.5-pro", "gemini-3-flash", "gemini-3.8-flash-tiered", "gemini-2.5-flash"),
                "gemini-",
            )
            if gem_b:
                rem = _extract_antigravity_remaining(gem_b)
                if rem is not None:
                    primary = _make_window(
                        "Gemini 5h",
                        remaining_fraction=rem,
                        reset_dt=_parse_iso(gem_b.get("resetTime")),
                        window_id="gemini-5h",
                    )

        if not any("argon" in w["label"].lower() for w in extra_windows):
            arg_b = _pick_bucket(buckets, ("gemini-4-argon-high", "gemini-4-argon-medium", "gemini-4-argon-low"), "argon")
            if arg_b:
                rem = _extract_antigravity_remaining(arg_b)
                if rem is not None:
                    extra_windows.append(
                        _make_window(
                            "Gemini 4 Argon",
                            remaining_fraction=rem,
                            reset_dt=_parse_iso(arg_b.get("resetTime")),
                            window_id="gemini-argon",
                        )
                    )

        if not any("claude" in w["label"].lower() or "gpt" in w["label"].lower() for w in extra_windows):
            tp_b = _pick_bucket(
                buckets,
                ("claude-opus-5-5-high", "claude-sonnet-5-5-high", "gpt-oss-120b-medium"),
                "claude-",
            )
            if tp_b:
                rem = _extract_antigravity_remaining(tp_b)
                if rem is not None:
                    extra_windows.append(
                        _make_window(
                            "Claude/GPT 5h",
                            remaining_fraction=rem,
                            reset_dt=_parse_iso(tp_b.get("resetTime")),
                            window_id="3p-5h",
                        )
                    )

    if primary is not None or secondary is not None or extra_windows:
        return (
            _finalize_antigravity_online(
                source_text="agy CLI",
                plan=plan,
                email=email,
                primary=primary,
                secondary=secondary,
                extra_windows=extra_windows,
                badges=[],
            ),
            False,
        )
    return None, False


def fetch_antigravity(settings: dict[str, Any]) -> dict[str, Any]:
    ssl_ctx = ssl._create_unverified_context()
    processes = _detect_antigravity_processes()

    for proc in processes:
        tokens = [proc.get("ext_csrf_token", ""), proc.get("csrf_token", "")]
        for port in proc.get("ports", []):
            status_q, quota_json = _call_antigravity_local(
                port,
                "RetrieveUserQuotaSummary",
                {"forceRefresh": True},
                tokens,
                ssl_ctx,
                timeout=2.5,
            )
            status_u, user_json = _call_antigravity_local(
                port,
                "GetUserStatus",
                {
                    "metadata": {
                        "ideName": "antigravity",
                        "extensionName": "antigravity",
                        "ideVersion": "unknown",
                        "locale": "en",
                    }
                },
                tokens,
                ssl_ctx,
                timeout=2.0,
            )

            primary: dict[str, Any] | None = None
            secondary: dict[str, Any] | None = None
            extra_windows: list[dict[str, Any]] = []
            email: str | None = None
            plan: str | None = None
            badges: list[dict[str, str]] = []

            if status_q == 200 and quota_json:
                primary, secondary, extra_windows = _parse_antigravity_quota_summary(quota_json)

            if status_u == 200 and user_json:
                email, plan, badges, fb_pri, fb_sec, fb_extra = _parse_antigravity_user_status(user_json)
                if primary is None and secondary is None:
                    primary = fb_pri
                    secondary = fb_sec
                    if not extra_windows:
                        extra_windows = fb_extra

            if primary is not None or secondary is not None or extra_windows:
                return _finalize_antigravity_online(
                    source_text="Local IDE" if proc["source"] == "ide" else "agy CLI",
                    plan=plan,
                    email=email,
                    primary=primary,
                    secondary=secondary,
                    extra_windows=extra_windows,
                    badges=badges,
                )

    cli_cred = read_antigravity_cli_oauth()
    cli_expired = False
    if cli_cred:
        cloud_res, cli_expired = _fetch_antigravity_cloud(cli_cred)
        if cloud_res:
            return cloud_res

    session_count = _count_antigravity_offline_sessions()
    cached = _LAST_ONLINE_SNAPSHOTS.get("antigravity")
    if cached:
        offline_copy = dict(cached)
        offline_copy["status"] = "offline"
        offline_copy["status_text"] = (
            "Token CLI hết hạn"
            if cli_expired
            else f"Offline · {session_count} phiên" if session_count else "Offline (IDE đang tắt)"
        )
        offline_copy["updated_at"] = _now_iso()
        return offline_copy

    return {
        "id": "antigravity",
        "name": "Antigravity",
        "status": "error" if cli_expired else "offline",
        "status_text": (
            "Token CLI hết hạn"
            if cli_expired
            else f"Offline · {session_count} phiên" if session_count else "IDE chưa chạy"
        ),
        "plan": None,
        "account": str(cli_cred.get("id_token_email") or "").strip() or None if cli_cred else None,
        "primary": None,
        "secondary": None,
        "extra_windows": [],
        "badges": (
            [{"label": "Lịch sử", "value": f"{session_count} phiên"}] if session_count else []
        ),
        "error": (
            "Token agy CLI đã hết hạn. Chạy `agy` để tự gia hạn hoặc mở Antigravity IDE."
            if cli_expired
            else "Mở Antigravity IDE hoặc đăng nhập `agy` CLI để đọc quota real-time."
        ),
        "updated_at": _now_iso(),
    }


_CODEX_PLAN_MAP = {
    "guest": "Guest",
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "pro_lite": "Pro Lite",
    "prolite": "Pro Lite",
    "pro-lite": "Pro Lite",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
    "education": "Education",
    "edu": "Education",
    "free_workspace": "Free Workspace",
    "quorum": "Quorum",
    "k12": "K12",
}


def _codex_home_dir() -> Path:
    env_home = os.environ.get("CODEX_HOME", "").strip()
    if env_home:
        return Path(env_home)
    return Path.home() / ".codex"


def _resolve_codex_base_url(codex_dir: Path) -> str:
    default_url = "https://chatgpt.com/backend-api"
    cfg_path = codex_dir / "config.toml"
    if not cfg_path.is_file():
        return default_url
    try:
        content = cfg_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return default_url

    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        if key.strip() == "chatgpt_base_url":
            url = val.strip().strip('"').strip("'").rstrip("/")
            if not url:
                continue
            if (url.startswith("https://chatgpt.com") or url.startswith("https://chat.openai.com")) and "/backend-api" not in url:
                url += "/backend-api"
            if url.startswith("https://") or url.startswith("http://127.0.0.1") or url.startswith("http://localhost"):
                return url
    return default_url


def _read_codex_auth(auth_path: Path) -> dict[str, Any] | None:
    for attempt in range(3):
        try:
            raw = auth_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            if attempt < 2:
                time.sleep(0.05)
    return None


def _codex_window_label(limit_seconds: Any, fallback_label: str) -> str:
    try:
        secs = int(limit_seconds)
    except (TypeError, ValueError):
        return fallback_label
    mins = secs // 60
    if mins <= 360:
        return "Session (5h)"
    if mins <= 10800:
        return "Weekly (7d)"
    if mins >= 20000:
        return "Monthly (30d)"
    return fallback_label


def _parse_codex_window(win: dict[str, Any] | None, fallback_label: str) -> dict[str, Any] | None:
    if not isinstance(win, dict):
        return None
    used = win.get("used_percent")
    if used is None:
        used = win.get("usage_percent")
    limit_secs = win.get("limit_window_seconds")
    reset_ts = win.get("reset_at")
    if used is None and limit_secs is None and reset_ts is None:
        return None
    label = _codex_window_label(limit_secs, fallback_label)
    reset_dt = _parse_unix_ts(reset_ts)
    return _make_window(
        label,
        used_percent=float(used or 0.0),
        reset_dt=reset_dt,
        window_id=label.lower().replace(" ", "-"),
    )


def fetch_codex(settings: dict[str, Any]) -> dict[str, Any]:
    codex_dir = _codex_home_dir()
    auth_path = codex_dir / "auth.json"
    if not auth_path.is_file():
        return {
            "id": "codex",
            "name": "Codex",
            "status": "unconfigured",
            "status_text": "Chưa đăng nhập",
            "plan": None,
            "account": None,
            "primary": None,
            "secondary": None,
            "extra_windows": [],
            "badges": [],
            "error": "Không tìm thấy ~/.codex/auth.json. Chạy `codex login` để đăng nhập.",
            "updated_at": _now_iso(),
        }

    auth_data = _read_codex_auth(auth_path)
    if not auth_data:
        return {
            "id": "codex",
            "name": "Codex",
            "status": "error",
            "status_text": "Lỗi đọc auth.json",
            "plan": None,
            "account": None,
            "primary": None,
            "secondary": None,
            "extra_windows": [],
            "badges": [],
            "error": "Không thể parse ~/.codex/auth.json.",
            "updated_at": _now_iso(),
        }

    tokens = auth_data.get("tokens") or {}
    access_token = (tokens.get("access_token") or auth_data.get("OPENAI_API_KEY") or "").strip()
    account_id = (tokens.get("account_id") or "").strip()
    id_claims = _jwt_payload(tokens.get("id_token"))
    email = None
    if id_claims:
        email = id_claims.get("email")

    if not access_token:
        return {
            "id": "codex",
            "name": "Codex",
            "status": "unconfigured",
            "status_text": "Thiếu token",
            "plan": None,
            "account": email,
            "primary": None,
            "secondary": None,
            "extra_windows": [],
            "badges": [],
            "error": "Không có access_token trong ~/.codex/auth.json.",
            "updated_at": _now_iso(),
        }

    base_url = _resolve_codex_base_url(codex_dir)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "CodexBar",
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    try:
        status_code, data, raw_err = _http_json(
            f"{base_url}/wham/usage",
            method="GET",
            headers=headers,
            timeout=8.0,
        )
    except Exception as exc:
        cached = _LAST_ONLINE_SNAPSHOTS.get("codex")
        if cached:
            copy_res = dict(cached)
            copy_res["status"] = "offline"
            copy_res["status_text"] = "Mất kết nối (Cache)"
            copy_res["updated_at"] = _now_iso()
            return copy_res
        return {
            "id": "codex",
            "name": "Codex",
            "status": "error",
            "status_text": "Lỗi mạng",
            "plan": None,
            "account": email,
            "primary": None,
            "secondary": None,
            "extra_windows": [],
            "badges": [],
            "error": f"Không thể kết nối ChatGPT API: {exc}",
            "updated_at": _now_iso(),
        }

    if status_code in (401, 403):
        return {
            "id": "codex",
            "name": "Codex",
            "status": "error",
            "status_text": "Token hết hạn",
            "plan": None,
            "account": email,
            "primary": None,
            "secondary": None,
            "extra_windows": [],
            "badges": [],
            "error": "Codex OAuth token đã hết hạn. Chạy `codex login` hoặc mở Codex App.",
            "updated_at": _now_iso(),
        }

    if status_code != 200 or not isinstance(data, dict):
        return {
            "id": "codex",
            "name": "Codex",
            "status": "error",
            "status_text": f"HTTP {status_code}",
            "plan": None,
            "account": email,
            "primary": None,
            "secondary": None,
            "extra_windows": [],
            "badges": [],
            "error": f"Codex API trả về mã {status_code}.",
            "updated_at": _now_iso(),
        }

    raw_plan = str(data.get("plan_type") or "").strip().lower()
    plan_label = _CODEX_PLAN_MAP.get(raw_plan, raw_plan.capitalize() if raw_plan else None)
    account_email = data.get("email") or email

    rate_limit = data.get("rate_limit") or {}
    pri = _parse_codex_window(rate_limit.get("primary_window"), "Session (5h)")
    sec = _parse_codex_window(rate_limit.get("secondary_window"), "Weekly (7d)")

    # Swap if API puts weekly in primary_window and session in secondary_window
    if pri and sec and "weekly" in pri["label"].lower() and "session" in sec["label"].lower():
        pri, sec = sec, pri

    extra_windows: list[dict[str, Any]] = []
    cr_win = _parse_codex_window(
        rate_limit.get("code_review_window") or data.get("code_review_rate_limit"),
        "Code Review",
    )
    if cr_win:
        extra_windows.append(cr_win)

    add_limits = data.get("additional_rate_limits")
    if isinstance(add_limits, list):
        for entry in add_limits:
            if not isinstance(entry, dict):
                continue
            rl = entry.get("rate_limit") or entry
            w_raw = rl.get("primary_window") or rl.get("secondary_window")
            limit_name = entry.get("limit_name") or entry.get("metered_feature") or "Extra"
            parsed_extra = _parse_codex_window(w_raw, str(limit_name))
            if parsed_extra:
                extra_windows.append(parsed_extra)

    badges: list[dict[str, str]] = []
    reset_credits = data.get("rate_limit_reset_credits")
    if isinstance(reset_credits, dict) and reset_credits.get("available_count") is not None:
        badges.append({"label": "Reset credits", "value": str(reset_credits["available_count"])})

    credits = data.get("credits")
    if isinstance(credits, dict) and credits.get("has_credits") and not credits.get("unlimited"):
        bal = credits.get("balance")
        if bal is not None:
            badges.append({"label": "Credits", "value": str(bal)})

    result = {
        "id": "codex",
        "name": "Codex",
        "status": "online",
        "status_text": "OAuth",
        "plan": plan_label,
        "account": account_email,
        "primary": pri,
        "secondary": sec,
        "extra_windows": extra_windows,
        "badges": badges,
        "error": None,
        "updated_at": _now_iso(),
    }
    _LAST_ONLINE_SNAPSHOTS["codex"] = result
    return result


def _format_claude_tier(
    tier: str | None,
    *,
    billing_type: str | None = None,
    capabilities: list[str] | None = None,
) -> str | None:
    caps_lower = {str(c).strip().lower() for c in (capabilities or [])}
    if tier:
        low = tier.strip().lower()
        if low == "free":
            return "Free"
        if "max" in low:
            m = re.search(r"max[\s_-]*(\d+x)", low)
            return f"Max {m.group(1)}" if m else "Max"
        if "pro" in low:
            return "Pro"
        if "team" in low:
            return "Team"
        if "enterprise" in low:
            return "Enterprise"
        if low == "default_claude_ai" and not billing_type and not (
            caps_lower & {"claude_pro", "claude_max", "pro", "max", "team", "enterprise"}
        ):
            return "Free"
    if not billing_type and caps_lower and not (
        caps_lower & {"claude_pro", "claude_max", "pro", "max", "team", "enterprise"}
    ):
        return "Free"
    return None


def _parse_claude_usage_payload(
    data: dict[str, Any],
    *,
    source_label: str,
    plan: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    five = data.get("fiveHour") or data.get("five_hour")
    seven = data.get("sevenDay") or data.get("seven_day")
    sonnet = data.get("sevenDaySonnet") or data.get("seven_day_sonnet")
    opus = data.get("sevenDayOpus") or data.get("seven_day_opus")

    def to_win(obj: Any, label: str, win_id: str) -> dict[str, Any] | None:
        if not isinstance(obj, dict):
            return None
        util = obj.get("utilization")
        if util is None:
            return None
        reset_dt = _parse_iso(obj.get("resetsAt") or obj.get("resets_at"))
        return _make_window(label, used_percent=float(util), reset_dt=reset_dt, window_id=win_id)

    primary = to_win(five, "Session (5h)", "claude-5h")
    secondary = to_win(seven, "Weekly (7d)", "claude-weekly")
    extra_windows: list[dict[str, Any]] = []
    w_sonnet = to_win(sonnet, "Sonnet (7d)", "claude-sonnet")
    w_opus = to_win(opus, "Opus (7d)", "claude-opus")
    if w_sonnet:
        extra_windows.append(w_sonnet)
    if w_opus:
        extra_windows.append(w_opus)

    badges: list[dict[str, str]] = []
    extra = data.get("extraUsage") or data.get("extra_usage")
    if isinstance(extra, dict) and extra.get("isEnabled") or (
        isinstance(extra, dict) and extra.get("is_enabled")
    ):
        used_c = extra.get("usedCredits") if extra.get("usedCredits") is not None else extra.get("used_credits")
        limit_c = extra.get("monthlyLimit") if extra.get("monthlyLimit") is not None else extra.get("monthly_limit")
        if used_c is not None and limit_c is not None:
            badges.append({"label": "Extra", "value": f"{used_c}/{limit_c}"})

    info_message = None
    if primary is None and secondary is None and not extra_windows:
        info_message = (
            "Gói Free không có thanh hạn mức 5h/7d."
            if plan == "Free"
            else "Tài khoản hiện không có hạn mức 5h/7d."
        )

    return {
        "id": "claude",
        "name": "Claude",
        "status": "online",
        "status_text": source_label,
        "plan": plan,
        "account": account,
        "primary": primary,
        "secondary": secondary,
        "extra_windows": extra_windows,
        "badges": badges,
        "needs_login": False,
        "info_message": info_message,
        "error": None,
        "updated_at": _now_iso(),
    }


def _claude_demo_snapshot(*, locked_desktop: bool = False) -> dict[str, Any]:
    now = _now_utc()
    primary = _make_window(
        "Session (5h)",
        used_percent=18.0,
        reset_dt=now + timedelta(hours=3, minutes=40),
        window_id="claude-5h",
    )
    secondary = _make_window(
        "Weekly (7d)",
        used_percent=42.0,
        reset_dt=now + timedelta(days=4, hours=6),
        window_id="claude-weekly",
    )
    sonnet = _make_window(
        "Sonnet (7d)",
        used_percent=25.0,
        reset_dt=now + timedelta(days=4, hours=6),
        window_id="claude-sonnet",
    )
    hint = (
        "Claude Desktop đang khóa Cookies"
        if locked_desktop
        else "Chưa đăng nhập"
    )
    return {
        "id": "claude",
        "name": "Claude",
        "status": "demo",
        "status_text": "Demo",
        "plan": "Pro (Demo)",
        "account": None,
        "primary": primary,
        "secondary": secondary,
        "extra_windows": [sonnet],
        "badges": [{"label": "Trạng thái", "value": hint}],
        "needs_login": True,
        "error": None,
        "updated_at": _now_iso(),
    }


def _fetch_claude_web(cookie_header: str, source_label: str) -> tuple[dict[str, Any] | None, int | None, str | None]:
    """Fetch Claude usage from claude.ai web API.

    Returns (snapshot_if_ok, http_error_status, error_message).
    """
    web_headers = {
        "Cookie": cookie_header,
        "Accept": "application/json",
        "Origin": "https://claude.ai",
        "Referer": "https://claude.ai/settings/usage",
        "anthropic-client-platform": "web_claude_ai",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        ),
    }

    cookie_org_id: str | None = None
    m_org = re.search(r"(?:^|;\s*)lastActiveOrg=([^;]+)", cookie_header)
    if m_org:
        cookie_org_id = m_org.group(1).strip() or None

    org_id = cookie_org_id
    org_plan: str | None = None

    status_orgs, orgs_data, _ = _http_json(
        "https://claude.ai/api/organizations",
        method="GET",
        headers=web_headers,
        timeout=8.0,
    )
    if status_orgs == 200 and isinstance(orgs_data, list) and orgs_data:
        selected_org = None
        if cookie_org_id:
            selected_org = next(
                (o for o in orgs_data if isinstance(o, dict) and o.get("uuid") == cookie_org_id),
                None,
            )
        if selected_org is None and isinstance(orgs_data[0], dict):
            selected_org = orgs_data[0]
            org_id = selected_org.get("uuid")
        if isinstance(selected_org, dict):
            org_plan = _format_claude_tier(
                selected_org.get("rate_limit_tier"),
                billing_type=selected_org.get("billing_type"),
                capabilities=selected_org.get("capabilities"),
            )
    elif not org_id:
        return None, status_orgs, None

    if not org_id:
        return None, 404, "Không tìm thấy organization trên claude.ai."

    status_use, usage_data, _ = _http_json(
        f"https://claude.ai/api/organizations/{org_id}/usage",
        method="GET",
        headers=web_headers,
        timeout=8.0,
    )
    if status_use != 200 or not isinstance(usage_data, dict):
        return None, status_use, None

    acc_email: str | None = None
    acc_plan: str | None = org_plan
    try:
        _, acc_data, _ = _http_json(
            "https://claude.ai/api/account",
            method="GET",
            headers=web_headers,
            timeout=4.0,
        )
        if isinstance(acc_data, dict):
            acc_email = acc_data.get("email_address")
            acc_plan = _format_claude_tier(acc_data.get("rate_limit_tier")) or acc_plan
    except Exception:
        pass

    res = _parse_claude_usage_payload(
        usage_data,
        source_label=source_label,
        plan=acc_plan,
        account=acc_email,
    )
    return res, None, None


def fetch_claude(
    settings: dict[str, Any],
    *,
    on_auto_cookie: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    auto_discovery = bool(settings.get("claude_auto_discovery", True))

    oauth_token = (
        str(settings.get("claude_oauth_token") or "").strip()
        or os.environ.get("CODEXBAR_CLAUDE_OAUTH_TOKEN", "").strip()
    )
    tier_hint: str | None = None
    oauth_source = "OAuth"
    if not oauth_token and auto_discovery:
        file_token, raw_tier = read_claude_file_oauth()
        if file_token:
            oauth_token = file_token
            tier_hint = _format_claude_tier(raw_tier)
            oauth_source = "Claude CLI"
    if not oauth_token and auto_discovery:
        kr_token, raw_tier = read_claude_keyring_oauth()
        if kr_token:
            oauth_token = kr_token
            tier_hint = _format_claude_tier(raw_tier)
            oauth_source = "Claude Keyring"

    if oauth_token:
        try:
            status_code, data, _ = _http_json(
                "https://api.anthropic.com/api/oauth/usage",
                method="GET",
                headers={
                    "Authorization": f"Bearer {oauth_token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "claude-cli/2.1.0 (external, cli)",
                    "Accept": "application/json",
                },
                timeout=8.0,
            )
            if status_code == 200 and isinstance(data, dict):
                res = _parse_claude_usage_payload(
                    data,
                    source_label=oauth_source,
                    plan=tier_hint,
                )
                _LAST_ONLINE_SNAPSHOTS["claude"] = res
                return res
            err_msg = (
                "Claude OAuth bị giới hạn tốc độ (HTTP 429)."
                if status_code == 429
                else f"Claude OAuth token không hợp lệ hoặc hết hạn (HTTP {status_code})."
            )
            return {
                "id": "claude",
                "name": "Claude",
                "status": "error",
                "status_text": f"OAuth HTTP {status_code}",
                "plan": tier_hint,
                "account": None,
                "primary": None,
                "secondary": None,
                "extra_windows": [],
                "badges": [],
                "needs_login": True,
                "error": err_msg,
                "updated_at": _now_iso(),
            }
        except Exception as exc:
            return {
                "id": "claude",
                "name": "Claude",
                "status": "error",
                "status_text": "Lỗi mạng",
                "plan": tier_hint,
                "account": None,
                "primary": None,
                "secondary": None,
                "extra_windows": [],
                "badges": [],
                "error": f"Lỗi kết nối Claude OAuth API: {exc}",
                "updated_at": _now_iso(),
            }

    manual_sk = (
        str(settings.get("claude_session_key") or "").strip()
        or os.environ.get("CLAUDE_AI_SESSION_KEY", "").strip()
    )
    if manual_sk.startswith("sessionKey="):
        manual_sk = manual_sk[len("sessionKey="):].strip()

    auto_header = str(settings.get("auto_claude_cookie_header") or "").strip()
    auto_sk = str(settings.get("auto_claude_session_key") or "").strip()
    if not auto_header and auto_sk:
        auto_header = f"sessionKey={auto_sk}"

    candidates: list[tuple[str, str, bool]] = []
    if manual_sk:
        candidates.append((f"sessionKey={manual_sk}", "Web Cookie", False))
    elif auto_header:
        candidates.append((auto_header, "Claude Auto", True))

    last_web_err: tuple[int | None, str | None] | None = None
    for cookie_hdr, label, is_auto_cached in candidates:
        try:
            res, err_status, err_text = _fetch_claude_web(cookie_hdr, label)
            if res:
                _LAST_ONLINE_SNAPSHOTS["claude"] = res
                return res
            if is_auto_cached and err_status in (401, 403):
                # Stale auto-cached session: clear it and fall through to live re-discovery
                if on_auto_cookie:
                    on_auto_cookie("", "")
            else:
                last_web_err = (err_status, err_text)
        except Exception as exc:
            last_web_err = (None, f"Lỗi kết nối claude.ai: {exc}")

    desktop_sk, desktop_hdr, desktop_hint = (
        read_claude_desktop_cookies() if auto_discovery else (None, None, None)
    )
    if desktop_sk:
        hdr = desktop_hdr or f"sessionKey={desktop_sk}"
        try:
            res, err_status, err_text = _fetch_claude_web(hdr, "Claude Desktop")
            if res:
                if on_auto_cookie:
                    on_auto_cookie(desktop_sk, hdr)
                _LAST_ONLINE_SNAPSHOTS["claude"] = res
                return res
            last_web_err = (err_status, err_text)
        except Exception as exc:
            last_web_err = (None, f"Lỗi kết nối claude.ai: {exc}")

    if last_web_err is not None and manual_sk:
        err_status, err_text = last_web_err
        err_msg = err_text or (
            "sessionKey hết hạn hoặc bị từ chối (HTTP 403)."
            if err_status in (401, 403)
            else f"Không lấy được quota Claude Web (HTTP {err_status})."
        )
        return {
            "id": "claude",
            "name": "Claude",
            "status": "error",
            "status_text": f"HTTP {err_status}" if err_status else "Lỗi mạng",
            "plan": None,
            "account": None,
            "primary": None,
            "secondary": None,
            "extra_windows": [],
            "badges": [],
            "needs_login": True,
            "error": err_msg,
            "updated_at": _now_iso(),
        }

    locked_desktop = desktop_hint == "locked" and has_claude_desktop_installed()
    if settings.get("demo_unconfigured", True):
        return _claude_demo_snapshot(locked_desktop=locked_desktop)

    return {
        "id": "claude",
        "name": "Claude",
        "status": "unconfigured",
        "status_text": "Đang khóa Cookies" if locked_desktop else "Chưa đăng nhập",
        "plan": None,
        "account": None,
        "primary": None,
        "secondary": None,
        "extra_windows": [],
        "badges": [],
        "needs_login": True,
        "error": (
            "Claude Desktop đang chạy nên khóa file Cookies. Bấm Đăng nhập để liên kết 1 lần."
            if locked_desktop
            else "Bấm Đăng nhập để liên kết tài khoản Claude."
        ),
        "updated_at": _now_iso(),
    }


def invalidate_quota_cache() -> None:
    with _CACHE_LOCK:
        _QUOTA_CACHE["fetched_at_mono"] = 0.0


def fetch_all_quotas(
    settings: dict[str, Any],
    *,
    force: bool = False,
    on_auto_cookie: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    now_mono = time.monotonic()
    with _CACHE_LOCK:
        cached_payload = _QUOTA_CACHE.get("payload")
        age = now_mono - float(_QUOTA_CACHE.get("fetched_at_mono", 0.0))
        if not force and cached_payload is not None and age < CACHE_TTL_SECONDS:
            return {
                "providers": cached_payload["providers"],
                "fetched_at": cached_payload["fetched_at"],
                "cached": True,
            }

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        fut_ag = pool.submit(fetch_antigravity, settings)
        fut_cx = pool.submit(fetch_codex, settings)
        fut_cl = pool.submit(fetch_claude, settings, on_auto_cookie=on_auto_cookie)

        providers = [
            fut_ag.result(),
            fut_cx.result(),
            fut_cl.result(),
        ]

    payload = {
        "providers": providers,
        "fetched_at": _now_iso(),
        "cached": False,
    }
    with _CACHE_LOCK:
        _QUOTA_CACHE["fetched_at_mono"] = time.monotonic()
        _QUOTA_CACHE["payload"] = payload
    return payload
