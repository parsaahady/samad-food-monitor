"""تست‌های خودکار پنل گرافیکی وب (Dashboard API، امنیت ماسک توکن و ویرایشگر .env)."""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest

from samad_monitor.browser.client import resolve_browser_launch_options
from samad_monitor.ui.controller import DashboardController
from samad_monitor.ui.env_writer import update_env_file
from samad_monitor.ui.server import create_http_server
from tests.test_monitor_scenarios import _make_test_config


def test_browser_resolver_falls_back_to_windows_edge_when_chromium_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """در صورت تحریم ۴۰۳ دانلود کرومیوم در ایران، مرورگر Edge یا Chrome نصب‌شده روی ویندوز ۱۱ خودکار شناسایی شود."""
    fake_pf86 = tmp_path / "ProgramFiles_x86"
    edge_exe = fake_pf86 / "Microsoft/Edge/Application/msedge.exe"
    edge_exe.parent.mkdir(parents=True)
    edge_exe.write_bytes(b"MZ-fake-edge")

    monkeypatch.setenv("ProgramFiles(x86)", str(fake_pf86))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "EmptyPF"))
    monkeypatch.setenv("LocalAppData", str(tmp_path / "EmptyLocal"))

    class _FakeChromium:
        executable_path = str(tmp_path / "nonexistent" / "chrome.exe")

    class _FakePlaywright:
        chromium = _FakeChromium()

    opts, label = resolve_browser_launch_options(_FakePlaywright(), platform_override="win32")
    assert opts == {"executable_path": str(edge_exe)}
    assert "Edge" in label


def test_env_writer_blocks_forbidden_password_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    with pytest.raises(ValueError):
        update_env_file(env_file, {"SAMAD_PASSWORD": "secret"})


def test_ui_http_server_endpoints_and_token_masking(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"DATA_DIR={tmp_path / 'data'}",
                f"LOGS_DIR={tmp_path / 'logs'}",
                f"SQLITE_DB_PATH={tmp_path / 'data' / 'ui.db'}",
                f"STORAGE_STATE_PATH={tmp_path / 'data' / 'state.json'}",
                f"BROWSER_PROFILE_DIR={tmp_path / 'data' / 'profile'}",
                "TELEGRAM_ENABLED=true",
                "TELEGRAM_BOT_TOKEN=12345678:SUPER_SECRET_BOT_TOKEN_VALUE",
                "TELEGRAM_CHAT_ID=99887766",
            ]
        ),
        encoding="utf-8",
    )

    cfg = _make_test_config(tmp_path)
    cfg.telegram_bot_token = "12345678:SUPER_SECRET_BOT_TOKEN_VALUE"
    cfg.telegram_chat_ids = ["99887766"]

    controller = DashboardController(config=cfg, env_file_override=env_file)
    server = create_http_server(controller=controller, host="127.0.0.1", port=0)
    port = server.server_port
    base_url = f"http://127.0.0.1:{port}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            # ۱. صفحات استاتیک
            r_html = client.get("/")
            assert r_html.status_code == 200
            assert "پنل هوشمند پایش غذای سماد" in r_html.text

            r_css = client.get("/styles.css")
            assert r_css.status_code == 200

            r_js = client.get("/app.js")
            assert r_js.status_code == 200

            # ۲. وضعیت زنده و پیش‌نمایش دمو
            r_state = client.get("/api/state")
            assert r_state.status_code == 200
            st = r_state.json()
            assert st["university"]["key"] == "SBU"
            assert len(st["menu"]["days"]) == 7

            r_demo = client.get("/api/demo-menu")
            assert r_demo.status_code == 200
            assert r_demo.json()["is_demo"] is True
            assert r_demo.json()["total_meals"] >= 10

            # ۳. امنیت عدم افشای توکن خام در /api/config
            r_cfg = client.get("/api/config")
            assert r_cfg.status_code == 200
            cfg_json = r_cfg.json()
            assert cfg_json["has_telegram_bot_token"] is True
            assert "SUPER_SECRET_BOT_TOKEN_VALUE" not in r_cfg.text

            # ۴. ذخیره تنظیمات جدید با حفظ توکن قبلی در صورت خالی بودن فیلد توکن
            r_save = client.post(
                "/api/config",
                json={
                    "telegram_enabled": True,
                    "telegram_bot_token": "",
                    "telegram_chat_id": "11223344",
                    "telegram_proxy_url": "socks5://127.0.0.1:10808",
                    "windows_notification_enabled": True,
                    "check_interval_normal_minutes": 45,
                    "check_interval_peak_minutes": 10,
                    "samad_self_name_filter": "مرکزی",
                },
            )
            assert r_save.status_code == 200
            assert r_save.json()["ok"] is True
            assert controller.config.check_interval_normal_minutes == 45
            assert controller.config.telegram_chat_ids == ["11223344"]
            # توکن قبلی باید دست‌نخورده باقی مانده باشد
            assert controller.config.telegram_bot_token == "12345678:SUPER_SECRET_BOT_TOKEN_VALUE"

            # ۵. تست شناسایی پورت پروکسی لوکال
            r_proxy = client.get("/api/detect-proxy")
            assert r_proxy.status_code == 200
            assert "ok" in r_proxy.json()

            # ۶. تست کامل مدیریت مشترکین ربات تلگرام (افزودن دوست، تغییر وضعیت و حذف)
            r_subs0 = client.get("/api/subscribers")
            assert r_subs0.status_code == 200
            assert any(s["chat_id"] == "11223344" for s in r_subs0.json()["subscribers"])

            r_add = client.post(
                "/api/subscribers/add",
                json={
                    "display_name": "رضا هم‌اتاقی",
                    "chat_id": "55667788",
                    "role": "subscriber",
                    "send_welcome": False,
                },
            )
            assert r_add.status_code == 200
            assert r_add.json()["ok"] is True
            assert any(
                s["chat_id"] == "55667788" and s["display_name"] == "رضا هم‌اتاقی"
                for s in r_add.json()["subscribers"]
            )

            r_upd = client.post(
                "/api/subscribers/update",
                json={"chat_id": "55667788", "enabled": False},
            )
            assert r_upd.status_code == 200
            sub_row = next(s for s in r_upd.json()["subscribers"] if s["chat_id"] == "55667788")
            assert sub_row["enabled"] is False

            r_auto = client.post(
                "/api/subscribers/auto-settings",
                json={"auto_subscribe": True, "invite_code": "sbu1405"},
            )
            assert r_auto.status_code == 200
            assert r_auto.json()["auto_subscribe"] is True
            assert r_auto.json()["invite_code"] == "sbu1405"

            r_del = client.post("/api/subscribers/delete", json={"chat_id": "55667788"})
            assert r_del.status_code == 200
            assert not any(s["chat_id"] == "55667788" for s in r_del.json()["subscribers"])
    finally:
        controller.shutdown()
        server.shutdown()
        server.server_close()
