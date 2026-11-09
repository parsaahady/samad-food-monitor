"""
تست سرتاسری (E2E) واقعی با مرورگر Playwright Headless Chromium:
یک سرور محلی شبیه‌ساز دقیق `samad.app/food/reserve` و API برنامه هفتگی بالا می‌آید،
و `SamadClient` در حالت `browser` با کرومیوم واقعی منوی هفته آینده را استخراج کرده
و هرگونه تلاش برای ارسال POST رزرو غذا را در سطح شبکه مرورگر مسدود می‌کند.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from samad_monitor.browser.client import SamadClient
from samad_monitor.browser.session import SessionManager
from samad_monitor.models import CheckStatus, WeekRange
from tests.fixtures.samad_payloads import (
    make_fake_jwt,
    published_programs_v2_response,
    sbu_selfs_response,
)
from tests.test_monitor_scenarios import _make_test_config


def test_real_playwright_headless_browser_extraction_and_mutation_blocking(
    tmp_path: Path,
) -> None:
    mutation_hits: List[str] = []
    target_week = WeekRange(week_start=date(2026, 11, 14), week_end=date(2026, 11, 20))

    programs_json = published_programs_v2_response(
        [
            {
                "date": "2026-11-14",
                "meal_type_id": 7,
                "meal_type_name": "ناهار",
                "food_name": "چلو خورشت فسنجان",
            },
            {
                "date": "2026-11-16",
                "meal_type_id": 8,
                "meal_type_name": "شام",
                "food_name": "کباب تابه‌ای",
            },
        ]
    )

    class MockSamadHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:  # noqa: N802
            mutation_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/food/reserve":
                html = """<!doctype html>
                <html lang="fa" dir="rtl">
                <head><meta charset="utf-8"><title>سماد - رزرو غذا</title></head>
                <body>
                  <h1>سامانه سماد دانشگاه شهید بهشتی</h1>
                  <button data-testid="next-week" onclick="loadNextWeek()">هفته بعد</button>
                  <div id="grid"></div>
                  <script>
                    fetch('/rest/reservations/selfs');
                    function loadNextWeek() {
                      fetch('/rest/reservations/programs/v2?selfId=101&weekStartDate=2026-11-14+00:00:00');
                      // تلاش شبیه‌سازی‌شده برای ارسال درخواست رزرو که باید توسط ReadOnly Route Guard مسدود شود
                      fetch('/rest/reserves/5001/reserve', { method: 'POST', body: '{}' }).catch(() => {});
                    }
                  </script>
                </body>
                </html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

            if parsed.path == "/rest/reservations/selfs":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(sbu_selfs_response(), ensure_ascii=False).encode("utf-8"))
                return

            if parsed.path == "/rest/reservations/programs/v2":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(programs_json, ensure_ascii=False).encode("utf-8"))
                return

            self.send_response(404)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), MockSamadHandler)
    port = server.server_port
    base_url = f"http://127.0.0.1:{port}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        config = _make_test_config(tmp_path)
        config.samad_web_url = base_url
        config.samad_reserve_url = f"{base_url}/food/reserve"
        config.samad_university_api_url = base_url
        config.samad_fetch_strategy = "browser"

        sm = SessionManager(
            storage_state_path=config.storage_state_path,
            browser_profile_dir=config.browser_profile_dir,
            samad_web_url=base_url,
            default_university_api_url=base_url,
        )
        sm.save_tokens(
            access_token=make_fake_jwt("401243001"),
            refresh_token="refresh-tok",
        )

        client = SamadClient(config=config, session_manager=sm)
        result = client.fetch_week_menu(target_week)

        assert result.status == CheckStatus.MENU_PUBLISHED
        assert len(result.meals) == 2
        assert {m.food_name for m in result.meals} == {"چلو خورشت فسنجان", "کباب تابه‌ای"}
        # درخواست POST رزرو باید در لایه مرورگر مسدود شده و هرگز به سرور نرسیده باشد!
        assert mutation_hits == []
    finally:
        server.shutdown()
        server.server_close()
