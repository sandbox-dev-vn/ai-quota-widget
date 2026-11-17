"""Local HTTP API & static UI server for AI Quota Widget."""

from __future__ import annotations

import json
import mimetypes
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .providers import fetch_all_quotas, invalidate_quota_cache

DEFAULT_SETTINGS: dict[str, Any] = {
    "pinned": True,
    "compact_mode": False,
    "metric_mode": "remaining",
    "opacity": 96,
    "refresh_interval_sec": 60,
    "demo_unconfigured": True,
    "claude_auto_discovery": True,
    "claude_oauth_token": "",
    "claude_session_key": "",
    "auto_claude_session_key": "",
    "auto_claude_cookie_header": "",
    "window_x": None,
    "window_y": None,
}


class SettingsStore:
    """Thread-safe JSON-backed settings store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data.update(raw)
        except Exception:
            pass
        self._normalize()

    def _normalize(self) -> None:
        self._data["pinned"] = bool(self._data.get("pinned", True))
        self._data["compact_mode"] = bool(self._data.get("compact_mode", False))
        metric = str(self._data.get("metric_mode") or "remaining").lower()
        self._data["metric_mode"] = "used" if metric == "used" else "remaining"
        try:
            opacity = int(self._data.get("opacity", 96))
        except (TypeError, ValueError):
            opacity = 96
        self._data["opacity"] = max(50, min(100, opacity))
        try:
            interval = int(self._data.get("refresh_interval_sec", 60))
        except (TypeError, ValueError):
            interval = 60
        self._data["refresh_interval_sec"] = max(15, min(600, interval))
        self._data["demo_unconfigured"] = bool(self._data.get("demo_unconfigured", True))
        self._data["claude_auto_discovery"] = bool(self._data.get("claude_auto_discovery", True))
        self._data["claude_oauth_token"] = str(self._data.get("claude_oauth_token") or "").strip()
        self._data["claude_session_key"] = str(self._data.get("claude_session_key") or "").strip()
        self._data["auto_claude_session_key"] = str(
            self._data.get("auto_claude_session_key") or ""
        ).strip()
        self._data["auto_claude_cookie_header"] = str(
            self._data.get("auto_claude_cookie_header") or ""
        ).strip()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def get_internal(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def get_public(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pinned": self._data["pinned"],
                "compact_mode": self._data["compact_mode"],
                "metric_mode": self._data["metric_mode"],
                "opacity": self._data["opacity"],
                "refresh_interval_sec": self._data["refresh_interval_sec"],
                "demo_unconfigured": self._data["demo_unconfigured"],
                "claude_auto_discovery": self._data["claude_auto_discovery"],
                "has_claude_oauth_token": bool(self._data["claude_oauth_token"]),
                "has_claude_session_key": bool(
                    self._data["claude_session_key"] or self._data["auto_claude_session_key"]
                ),
                "window_x": self._data.get("window_x"),
                "window_y": self._data.get("window_y"),
            }

    def update(self, patch: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Apply patch and return (public_settings, should_invalidate_cache)."""
        invalidate = False
        with self._lock:
            for key in (
                "pinned",
                "compact_mode",
                "metric_mode",
                "opacity",
                "refresh_interval_sec",
                "demo_unconfigured",
                "claude_auto_discovery",
                "window_x",
                "window_y",
            ):
                if key in patch:
                    if key in ("demo_unconfigured", "claude_auto_discovery") and bool(patch[key]) != self._data.get(key):
                        invalidate = True
                    self._data[key] = patch[key]

            if "claude_oauth_token" in patch:
                val = str(patch["claude_oauth_token"] or "").strip()
                if val != self._data["claude_oauth_token"]:
                    self._data["claude_oauth_token"] = val
                    invalidate = True
            if patch.get("clear_claude_oauth_token"):
                self._data["claude_oauth_token"] = ""
                invalidate = True

            if "claude_session_key" in patch:
                val = str(patch["claude_session_key"] or "").strip()
                if val != self._data["claude_session_key"]:
                    self._data["claude_session_key"] = val
                    invalidate = True
            if "auto_claude_session_key" in patch:
                self._data["auto_claude_session_key"] = str(
                    patch["auto_claude_session_key"] or ""
                ).strip()
            if "auto_claude_cookie_header" in patch:
                self._data["auto_claude_cookie_header"] = str(
                    patch["auto_claude_cookie_header"] or ""
                ).strip()
            if patch.get("clear_claude_session_key"):
                self._data["claude_session_key"] = ""
                self._data["auto_claude_session_key"] = ""
                self._data["auto_claude_cookie_header"] = ""
                invalidate = True

            self._normalize()
            self._save_locked()
        if invalidate:
            invalidate_quota_cache()
        return self.get_public(), invalidate


class AppContext:
    """Shared state for HTTP request handlers."""

    def __init__(
        self,
        *,
        base_dir: Path,
        settings: SettingsStore,
        window_controller: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.base_dir = base_dir
        self.ui_dir = base_dir / "ui"
        self.settings = settings
        self.window_controller = window_controller


def make_handler(ctx: AppContext) -> type[BaseHTTPRequestHandler]:
    class QuotaWidgetHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _read_json_body(self) -> dict[str, Any]:
            length_hdr = self.headers.get("Content-Length")
            if not length_hdr:
                return {}
            try:
                length = int(length_hdr)
            except ValueError:
                return {}
            if length <= 0 or length > 1_000_000:
                return {}
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}

        def _serve_static(self, rel_path: str) -> None:
            if rel_path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if rel_path in ("", "/"):
                rel_path = "/index.html"
            clean_rel = rel_path.lstrip("/")
            target = (ctx.ui_dir / clean_rel).resolve()
            ui_root = ctx.ui_dir.resolve()
            if not str(target).startswith(str(ui_root)) or not target.is_file():
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return

            mime, _ = mimetypes.guess_type(str(target))
            if target.suffix == ".js":
                mime = "application/javascript; charset=utf-8"
            elif target.suffix == ".css":
                mime = "text/css; charset=utf-8"
            elif target.suffix == ".html":
                mime = "text/html; charset=utf-8"
            elif not mime:
                mime = "application/octet-stream"

            content = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if not path.startswith("/api/"):
                self._serve_static(path)
                return

            try:
                if path == "/api/quotas":
                    force = qs.get("force", ["0"])[0] == "1"
                    internal = ctx.settings.get_internal()

                    def _save_auto_cookie(sk: str, hdr: str) -> None:
                        ctx.settings.update(
                            {
                                "auto_claude_session_key": sk,
                                "auto_claude_cookie_header": hdr,
                            }
                        )

                    data = fetch_all_quotas(
                        internal,
                        force=force,
                        on_auto_cookie=_save_auto_cookie,
                    )
                    self._send_json(
                        {
                            **data,
                            "settings": ctx.settings.get_public(),
                        }
                    )
                    return

                if path == "/api/settings":
                    self._send_json({"settings": ctx.settings.get_public()})
                    return

                self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json(
                    {"error": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            body = self._read_json_body()

            try:
                if path == "/api/settings":
                    public_settings, invalidated = ctx.settings.update(body)
                    if ctx.window_controller is not None:
                        if "pinned" in body:
                            ctx.window_controller("pin", {"pinned": public_settings["pinned"]})
                        if "opacity" in body:
                            ctx.window_controller("opacity", {"opacity": public_settings["opacity"]})
                        if "compact_mode" in body:
                            ctx.window_controller("mode", {"compact": public_settings["compact_mode"]})
                    self._send_json(
                        {
                            "ok": True,
                            "settings": public_settings,
                            "refetch": invalidated,
                        }
                    )
                    return

                if path == "/api/window/control":
                    action = str(body.get("action") or "").strip().lower()
                    if action == "pin":
                        pinned = bool(body.get("pinned", True))
                        ctx.settings.update({"pinned": pinned})
                    elif action == "opacity":
                        ctx.settings.update({"opacity": body.get("opacity", 96)})
                    elif action == "mode":
                        compact = bool(body.get("compact", False))
                        ctx.settings.update({"compact_mode": compact})

                    res = (
                        ctx.window_controller(action, body)
                        if ctx.window_controller is not None
                        else {"ok": True, "desktop": False}
                    )
                    self._send_json({"ok": True, "result": res, "settings": ctx.settings.get_public()})
                    return

                if path == "/api/claude/login-complete":
                    sk = str(body.get("session_key") or "").strip()
                    hdr = str(body.get("cookie_header") or "").strip()
                    if not sk:
                        self._send_json(
                            {"ok": False, "error": "Thiếu session_key."},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    public_settings, _ = ctx.settings.update(
                        {
                            "claude_session_key": "",
                            "auto_claude_session_key": sk,
                            "auto_claude_cookie_header": hdr or f"sessionKey={sk}",
                        }
                    )
                    invalidate_quota_cache()
                    if ctx.window_controller is not None:
                        ctx.window_controller("refresh_ui", {})
                    self._send_json({"ok": True, "settings": public_settings})
                    return

                self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json(
                    {"error": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

    return QuotaWidgetHandler


def start_http_server(
    base_dir: Path,
    *,
    port: int = 0,
    window_controller: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[ThreadingHTTPServer, AppContext]:
    """Create and start the local ThreadingHTTPServer bound to 127.0.0.1."""
    settings = SettingsStore(base_dir / "data" / "settings.json")
    ctx = AppContext(
        base_dir=base_dir,
        settings=settings,
        window_controller=window_controller,
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(ctx))
    server.daemon_threads = True
    return server, ctx
