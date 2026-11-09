"""وب‌سرور سبک و بدون وابستگی خارجی برای پنل مدیریت گرافیکی Samad Food Monitor."""

from __future__ import annotations

import json
import logging
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from samad_monitor.config import AppConfig
from samad_monitor.ui.controller import DashboardController

logger = logging.getLogger("samad_monitor.ui.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


def create_http_server(
    controller: DashboardController,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """ایجاد نمونه ThreadingHTTPServer متصل به DashboardController."""

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            logger.debug("UI HTTP: " + format, *args)

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, filename: str) -> None:
            target = (STATIC_DIR / filename).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
                self._send_json({"ok": False, "message": "فایل یافت نشد."}, status=404)
                return

            content = target.read_bytes()
            mime = MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 256_000:
                raise ValueError("حجم درخواست بیش از حد مجاز است.")
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("فرمت بدنه JSON باید یک شیء (Object) باشد.")
            return data

        def do_HEAD(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path

            if path in {"/", "/index.html"}:
                self._send_static("index.html")
                return
            if path == "/styles.css":
                self._send_static("styles.css")
                return
            if path == "/app.js":
                self._send_static("app.js")
                return
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if path == "/api/state":
                qs = parse_qs(parsed.query)
                week_q = qs.get("week", [None])[0]
                self._send_json(controller.get_dashboard_state(week_start_query=week_q))
                return

            if path == "/api/config":
                self._send_json(controller.get_safe_config())
                return

            if path == "/api/logs":
                self._send_json(controller.get_history_and_logs())
                return

            if path == "/api/doctor":
                self._send_json(controller.run_doctor_checks())
                return

            if path == "/api/demo-menu":
                self._send_json(controller.get_demo_preview_menu())
                return

            if path == "/api/detect-proxy":
                self._send_json(controller.detect_local_vpn_proxy())
                return

            if path == "/api/subscribers":
                self._send_json(controller.get_subscribers_state())
                return

            self._send_json({"ok": False, "message": "مسیر یافت نشد."}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path

            try:
                if path == "/api/check-now":
                    self._send_json(controller.trigger_check_now())
                    return

                if path == "/api/login":
                    self._send_json(controller.trigger_interactive_login())
                    return

                if path == "/api/test-notify":
                    self._send_json(controller.trigger_test_notify())
                    return

                if path == "/api/scheduler/start":
                    self._send_json(controller.start_background_scheduler())
                    return

                if path == "/api/scheduler/stop":
                    self._send_json(controller.stop_background_scheduler())
                    return

                if path == "/api/config":
                    body = self._read_json_body()
                    self._send_json(controller.update_safe_config(body))
                    return

                if path == "/api/subscribers/sync":
                    self._send_json(controller.sync_bot_contacts_now())
                    return

                if path == "/api/subscribers/add":
                    body = self._read_json_body()
                    self._send_json(controller.add_subscriber(body))
                    return

                if path == "/api/subscribers/update":
                    body = self._read_json_body()
                    self._send_json(controller.update_subscriber(body))
                    return

                if path == "/api/subscribers/delete":
                    body = self._read_json_body()
                    self._send_json(controller.delete_subscriber(body))
                    return

                if path == "/api/subscribers/test":
                    body = self._read_json_body()
                    self._send_json(controller.test_single_subscriber(body))
                    return

                if path == "/api/subscribers/auto-settings":
                    body = self._read_json_body()
                    self._send_json(controller.update_auto_subscribe_settings(body))
                    return

                self._send_json({"ok": False, "message": "مسیر نامعتبر است."}, status=404)
            except Exception as exc:
                logger.warning("خطا در پردازش درخواست UI (%s): %s", path, exc)
                self._send_json({"ok": False, "message": str(exc)}, status=400)

    return ThreadingHTTPServer((host, port), DashboardRequestHandler)


def run_ui_server(
    config: AppConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    auto_start_scheduler: bool = False,
    env_file_override: Optional[Path] = None,
) -> int:
    """راه‌اندازی پنل وب محلی و باز کردن مرورگر کاربر."""
    controller = DashboardController(config=config, env_file_override=env_file_override)
    if auto_start_scheduler:
        controller.start_background_scheduler()

    server = create_http_server(controller=controller, host=host, port=port)
    actual_port = server.server_port
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    local_url = f"http://{display_host}:{actual_port}"

    print("======================================================================")
    print(f"✨ پنل گرافیکی مدیریت Samad Food Monitor آماده است!")
    print(f"🏛 دانشگاه هدف : {config.samad_university_label}")
    print(f"🌐 آدرس پنل    : {local_url}")
    print("   (برای خروج از پنل، در این پنجره کلیدهای Ctrl + C را بزنید)")
    print("======================================================================")

    if open_browser:
        try:
            webbrowser.open(local_url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nدر حال بستن ایمن پنل گرافیکی...")
    finally:
        controller.shutdown()
        server.server_close()

    return 0
