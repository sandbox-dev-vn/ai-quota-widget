"""Desktop entry point for AI Quota Widget (Always-on-top Windows HUD)."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from backend import CREATE_NO_WINDOW
from backend.providers import invalidate_quota_cache
from backend.server import start_http_server

BASE_DIR = Path(__file__).resolve().parent
WINDOW_TITLE = "AI Quota Widget"

FULL_WIDTH = 360
FULL_HEIGHT = 540
COMPACT_WIDTH = 360
COMPACT_HEIGHT = 138


def _ensure_stdio() -> None:
    """Redirect None stdout/stderr under pythonw.exe to a persistent log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = BASE_DIR / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "app.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file


def _find_app_browser() -> str | None:
    """Locate Microsoft Edge or Google Chrome for standalone --app window fallback."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if Path(path).is_file():
            return path
    return shutil.which("msedge") or shutil.which("chrome")


def _default_window_position(width: int) -> tuple[int, int]:
    """Place widget near top-right corner of the primary screen."""
    if sys.platform != "win32":
        return 60, 40
    try:
        user32 = ctypes.windll.user32
        screen_w = int(user32.GetSystemMetrics(0))
        x = max(16, screen_w - width - 24)
        return x, 28
    except Exception:
        return 60, 40


def _get_hwnd(win: Any) -> int | None:
    if sys.platform != "win32" or win is None:
        return None
    try:
        native = getattr(win, "native", None)
        if native is not None and getattr(native, "Handle", None) is not None:
            return int(native.Handle.ToInt32())
    except Exception:
        pass
    try:
        hwnd = int(ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE))
        return hwnd if hwnd else None
    except Exception:
        return None


def _apply_rounded_corners(hwnd: int) -> None:
    """Apply Windows 11 native rounded corners via DWM."""
    try:
        dwm = ctypes.windll.dwmapi
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        dwm.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), 4)
    except Exception:
        pass


def _invoke_winforms(win: Any, fn: Any) -> bool:
    """Run a UI-thread mutation safely on a pywebview WinForms Form."""
    native = getattr(win, "native", None)
    if native is None:
        return False
    try:
        from System.Windows.Forms import MethodInvoker

        if getattr(native, "InvokeRequired", False):
            native.Invoke(MethodInvoker(fn))
        else:
            fn()
        return True
    except Exception:
        return False


def _apply_topmost(win: Any, hwnd: int | None, pinned: bool) -> None:
    def _set_form_topmost() -> None:
        native = getattr(win, "native", None)
        if native is None:
            return
        if pinned:
            native.TopMost = False
            native.TopMost = True
        else:
            native.TopMost = False

    _invoke_winforms(win, _set_form_topmost)

    if not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        HWND_TOPMOST = wintypes.HWND(-1)
        HWND_NOTOPMOST = wintypes.HWND(-2)
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        insert_after = HWND_TOPMOST if pinned else HWND_NOTOPMOST
        user32.SetWindowPos(
            wintypes.HWND(hwnd),
            insert_after,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    except Exception:
        pass


def _apply_opacity(win: Any, hwnd: int | None, opacity_percent: int) -> None:
    clamped = max(50, min(100, int(opacity_percent)))

    def _set_form_opacity() -> None:
        native = getattr(win, "native", None)
        if native is not None:
            native.Opacity = clamped / 100.0

    if _invoke_winforms(win, _set_form_opacity):
        return

    if not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        LWA_ALPHA = 0x00000002
        alpha = int(round(255 * clamped / 100.0))
        style = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
        if not (style & WS_EX_LAYERED):
            user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, style | WS_EX_LAYERED)
        user32.SetLayeredWindowAttributes(wintypes.HWND(hwnd), 0, alpha, LWA_ALPHA)
    except Exception:
        pass


LOGIN_WINDOW_TITLE = "Đăng nhập Claude"


def _focus_existing_window(title: str = WINDOW_TITLE) -> bool:
    """If an instance is already running, restore and focus it instead of opening a second."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = int(user32.FindWindowW(None, title))
        if not hwnd:
            return False
        user32.ShowWindow(wintypes.HWND(hwnd), 9)  # SW_RESTORE
        user32.ShowWindow(wintypes.HWND(hwnd), 5)  # SW_SHOW
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        return True
    except Exception:
        return False


def _run_claude_login_dialog(callback_url: str) -> int:
    """Run an isolated non-InPrivate WebView2 login window so Google OAuth popups work."""
    import webview

    temp_profile = tempfile.mkdtemp(prefix="quota-claude-login-")
    login_win = webview.create_window(
        LOGIN_WINDOW_TITLE,
        url="https://claude.ai/login",
        width=460,
        height=680,
        on_top=True,
        background_color="#0b0b0d",
    )

    def _get_claude_ai_cookies() -> list[tuple[str, str]]:
        """Read cookies specifically for https://claude.ai via WebView2 CookieManager."""
        try:
            from System import Action, Type
            from System.Collections.Generic import List
            from System.Threading import Semaphore
            from System.Windows.Forms import MethodInvoker
            from Microsoft.Web.WebView2.Core import CoreWebView2Cookie
            import clr  # noqa: F401
        except Exception:
            try:
                cookies = login_win.get_cookies() or []
                return [
                    (str(getattr(c, "name", "") or ""), str(getattr(c, "value", "") or "").strip())
                    for c in cookies
                    if "claude.ai" in str(getattr(c, "domain", "") or "").lower()
                ]
            except Exception:
                return []

        result_pairs: list[tuple[str, str]] = []
        raw_list: list[tuple[str, str]] = []
        sem = Semaphore(0, 1)

        def _fetch_async() -> None:
            try:
                ec = login_win.native.browser
                core = ec.webview.CoreWebView2

                def _on_done(task: Any) -> None:
                    try:
                        for c in task.Result:
                            raw_list.append((str(c.Name or ""), str(c.Value or "").strip()))
                    except Exception:
                        pass
                    finally:
                        sem.Release()

                cookie_task = core.CookieManager.GetCookiesAsync("https://claude.ai")
                cookie_task.ContinueWith(
                    Action[type(cookie_task)](_on_done),
                    ec.syncContextTaskScheduler,
                )
            except Exception:
                sem.Release()

        try:
            login_win.native.Invoke(MethodInvoker(_fetch_async))
            sem.WaitOne(2500)
            result_pairs.extend(raw_list)
        except Exception:
            pass
        return result_pairs

    def _on_login_started() -> None:
        try:
            login_win.events.loaded.wait(timeout=12)
            time.sleep(0.25)
        except Exception:
            pass

        # Unhook pywebview's NewWindowRequested override so Google OAuth opens
        # a native linked popup window without navigating claude.ai/login away.
        try:
            from System.Windows.Forms import MethodInvoker

            def _unhook_popup() -> None:
                ec = getattr(login_win.native, "browser", None)
                if ec and ec.webview and ec.webview.CoreWebView2:
                    ec.webview.CoreWebView2.NewWindowRequested -= ec.on_new_window_request

            login_win.native.Invoke(MethodInvoker(_unhook_popup))
        except Exception:
            pass

        for _ in range(600):
            time.sleep(1.0)
            if login_win not in webview.windows:
                break

            pairs: list[tuple[str, str]] = []
            try:
                cur_url = str(login_win.get_current_url() or "").lower()
                if "claude.ai" in cur_url:
                    for c in login_win.get_cookies() or []:
                        dom = str(getattr(c, "domain", "") or "").lower()
                        if "claude.ai" in dom or not dom:
                            pairs.append(
                                (str(getattr(c, "name", "") or ""), str(getattr(c, "value", "") or "").strip())
                            )
            except Exception:
                pass

            if not any(n == "sessionKey" and v for n, v in pairs):
                pairs = _get_claude_ai_cookies()

            session_key = ""
            header_parts: list[str] = []
            for name, val in pairs:
                if not name or not val:
                    continue
                header_parts.append(f"{name}={val}")
                if name == "sessionKey":
                    session_key = val

            if session_key:
                hdr = "; ".join(header_parts) if header_parts else f"sessionKey={session_key}"
                if callback_url:
                    try:
                        payload = json.dumps(
                            {"session_key": session_key, "cookie_header": hdr}
                        ).encode("utf-8")
                        req = urllib.request.Request(
                            callback_url,
                            data=payload,
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        urllib.request.urlopen(req, timeout=5).read()
                    except Exception:
                        pass
                try:
                    login_win.destroy()
                except Exception:
                    pass
                break

    try:
        webview.start(
            _on_login_started,
            gui="edgechromium",
            private_mode=False,
            storage_path=temp_profile,
        )
    finally:
        shutil.rmtree(temp_profile, ignore_errors=True)
    return 0


def main() -> int:
    _ensure_stdio()
    parser = argparse.ArgumentParser(description="AI Quota Widget Desktop App")
    parser.add_argument(
        "--server-only",
        action="store_true",
        help="Run only the local HTTP backend & UI server without opening a GUI window.",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Launch in standalone Edge/Chrome --app desktop window instead of pywebview.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to bind the local HTTP server (0 = auto).",
    )
    parser.add_argument(
        "--claude-login-dialog",
        action="store_true",
        help="Internal flag to run the isolated Claude login WebView2 window.",
    )
    parser.add_argument(
        "--callback-url",
        type=str,
        default="",
        help="Callback URL for --claude-login-dialog.",
    )
    args = parser.parse_args()

    if args.claude_login_dialog:
        return _run_claude_login_dialog(args.callback_url)

    if not args.server_only and not args.browser and _focus_existing_window():
        return 0

    window_ref: list[Any] = []
    login_proc_ref: list[subprocess.Popen[Any]] = []
    hwnd_ref: list[int | None] = [None]

    server, ctx = start_http_server(BASE_DIR, port=args.port)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}"

    def persist_window_position() -> None:
        if not window_ref:
            return
        win = window_ref[0]
        try:
            x, y = int(win.x), int(win.y)
            ctx.settings.update({"window_x": x, "window_y": y})
        except Exception:
            pass

    def handle_window_control(action: str, body: dict[str, Any]) -> dict[str, Any]:
        if not window_ref:
            return {"ok": False, "reason": "no_window"}
        win = window_ref[0]
        hwnd = hwnd_ref[0] or _get_hwnd(win)
        if hwnd:
            hwnd_ref[0] = hwnd

        if action == "pin":
            pinned = bool(body.get("pinned", True))
            if sys.platform == "win32":
                _apply_topmost(win, hwnd, pinned)
            return {"ok": True, "pinned": pinned}

        if action == "opacity":
            opacity = int(body.get("opacity", 96))
            if sys.platform == "win32":
                _apply_opacity(win, hwnd, opacity)
            return {"ok": True, "opacity": opacity}

        if action == "mode":
            compact = bool(body.get("compact", False))
            target_h = COMPACT_HEIGHT if compact else FULL_HEIGHT
            target_w = COMPACT_WIDTH if compact else FULL_WIDTH
            try:
                win.resize(target_w, target_h)
            except Exception:
                pass
            return {"ok": True, "compact": compact, "width": target_w, "height": target_h}

        if action == "minimize":
            try:
                win.minimize()
            except Exception:
                pass
            return {"ok": True}

        if action == "close":
            persist_window_position()
            threading.Timer(0.05, lambda: win.destroy()).start()
            return {"ok": True}

        if action == "drag":
            if hwnd and sys.platform == "win32":
                try:
                    user32 = ctypes.windll.user32
                    user32.ReleaseCapture()
                    user32.SendMessageW(hwnd, 0x00A1, 2, 0)
                except Exception:
                    pass
            return {"ok": True}

        if action == "refresh_ui":
            try:
                win.evaluate_js("window.__refreshQuotas && window.__refreshQuotas(true)")
            except Exception:
                pass
            return {"ok": True}

        if action == "claude_login":
            if login_proc_ref and login_proc_ref[0].poll() is None:
                _focus_existing_window(LOGIN_WINDOW_TITLE)
                return {"ok": True, "already_open": True}
            try:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--claude-login-dialog",
                        "--callback-url",
                        f"{url}/api/claude/login-complete",
                    ],
                    cwd=str(BASE_DIR),
                )
                login_proc_ref.clear()
                login_proc_ref.append(proc)
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        return {"ok": False, "error": f"Unknown action: {action}"}

    ctx.window_controller = handle_window_control

    server_thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="http-server"
    )
    server_thread.start()

    def cleanup() -> None:
        persist_window_position()
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass

    if args.server_only:
        print(f"QUOTA_WIDGET_URL={url}", flush=True)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            cleanup()
            return 0

    initial_settings = ctx.settings.get_public()
    compact = bool(initial_settings.get("compact_mode", False))
    pinned = bool(initial_settings.get("pinned", True))
    opacity = int(initial_settings.get("opacity", 96))
    init_w = COMPACT_WIDTH if compact else FULL_WIDTH
    init_h = COMPACT_HEIGHT if compact else FULL_HEIGHT

    saved_x = initial_settings.get("window_x")
    saved_y = initial_settings.get("window_y")
    if isinstance(saved_x, int) and isinstance(saved_y, int) and saved_x >= -50 and saved_y >= -20:
        init_x, init_y = saved_x, saved_y
    else:
        init_x, init_y = _default_window_position(init_w)

    if not args.browser:
        try:
            import webview

            win = webview.create_window(
                WINDOW_TITLE,
                url=url,
                width=init_w,
                height=init_h,
                x=init_x,
                y=init_y,
                min_size=(310, 120),
                resizable=False,
                frameless=True,
                easy_drag=False,
                on_top=pinned,
                background_color="#0b0b0d",
            )
            window_ref.append(win)

            def _on_shown() -> None:
                win.events.shown.wait(timeout=5)
                try:
                    win.resize(init_w, init_h)
                    win.move(init_x, init_y)
                    win.show()
                except Exception:
                    pass
                hwnd = _get_hwnd(win)
                hwnd_ref[0] = hwnd
                if hwnd and sys.platform == "win32":
                    _apply_rounded_corners(hwnd)
                    _apply_topmost(win, hwnd, pinned)
                    _apply_opacity(win, hwnd, opacity)
                    try:
                        ctypes.windll.user32.SetForegroundWindow(wintypes.HWND(hwnd))
                    except Exception:
                        pass

            webview.start(_on_shown, gui="edgechromium")
            cleanup()
            return 0
        except Exception as exc:
            print(f"[AIQuotaWidget] pywebview fallback ({exc})", file=sys.stderr)

    browser_exe = _find_app_browser()
    if browser_exe:
        proc = subprocess.Popen(
            [
                browser_exe,
                f"--app={url}",
                f"--window-size={init_w},{init_h}",
            ],
            creationflags=CREATE_NO_WINDOW,
        )
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        cleanup()
        return 0

    webbrowser.open(url)
    print(f"AI Quota Widget running at {url}. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
