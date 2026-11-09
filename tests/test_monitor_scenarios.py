"""
تست یکپارچه سناریوهای ۱ تا ۸ بخش ۱۶ مشخصات پروژه + محافظ عدم رزرو خودکار و تمدید خودکار توکن:
۱. عدم انتشار منو -> بدون اعلان کاذب
۲. انتشار اولیه منو -> دقیقاً یک اعلان
۳. اجرای مجدد با همان منو -> عدم ارسال اعلان تکراری
۴. تغییر در یکی از غذاها -> یک اعلان تغییر (MENU_UPDATED)
۵. اضافه شدن روز جدید -> یک اعلان تغییر (MENU_UPDATED)
۶. قطع اینترنت یا خطای شبکه -> ثبت در لاگ بدون کرش
۷. انقضای سشن -> تشخیص دقیق، یک هشدار انقضای سشن و توقف تا ورود مجدد
۸. ری‌استارت کامل برنامه -> بازیابی کامل وضعیت از دیتابیس SQLite
+ محافظ سخت‌گیرانه ReadOnlyGuard (ممنوعیت مطلق رزرو/لغو/پرداخت)
+ تمدید خودکار توکن منقضی‌شده با refresh_token
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List
from zoneinfo import ZoneInfo

import httpx
import pytest

from samad_monitor.browser.client import (
    ReadOnlyGuardError,
    SamadClient,
    enforce_read_only_request,
)
from samad_monitor.browser.session import SessionManager
from samad_monitor.config import AppConfig
from samad_monitor.models import CheckStatus, EventType, NotificationMessage
from samad_monitor.monitor import SamadFoodMonitor
from samad_monitor.notifications.base import NotificationChannel
from samad_monitor.notifications.manager import NotificationManager
from tests.fixtures.samad_payloads import (
    make_fake_jwt,
    published_programs_v2_response,
    release_response,
    sbu_selfs_response,
    unpublished_program_error_response,
    user_profile_response,
)

TEHRAN_TZ = ZoneInfo("Asia/Tehran")
# پنجشنبه ۲۱ آبان ۱۴۰۵ (2026-11-12) -> هفته آینده: شنبه 2026-11-14 تا جمعه 2026-11-20
REF_THU = datetime(2026, 11, 12, 15, 0, tzinfo=TEHRAN_TZ)


class RecordingChannel(NotificationChannel):
    """کانال اعلان در حافظه برای شمارش دقیق اعلان‌های ارسالی در تست‌ها."""

    def __init__(self, channel_name: str = "telegram") -> None:
        self._name = channel_name
        self.messages: List[NotificationMessage] = []

    @property
    def name(self) -> str:
        return self._name

    def is_configured(self) -> tuple[bool, str]:
        return (True, "فعال (تست)")

    def send(self, message: NotificationMessage) -> None:
        self.messages.append(message)


def _make_test_config(tmp_path: Path) -> AppConfig:
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    return AppConfig(
        base_dir=tmp_path,
        samad_web_url="https://samad.app",
        samad_reserve_url="https://samad.app/food/reserve",
        samad_university_key="SBU",
        samad_university_label="دانشگاه شهید بهشتی",
        samad_university_api_url="https://dining.sbu.ac.ir",
        samad_self_ids=[101],  # تمرکز روی سلف مرکزی برادران در سناریوی تست
        samad_self_name_filter=[],
        samad_fetch_strategy="api",
        samad_proxy_url=None,
        samad_ignore_env_proxy=True,
        timezone_name="Asia/Tehran",
        check_interval_normal_minutes=60,
        check_interval_peak_minutes=12,
        peak_interval_min_minutes=10,
        peak_interval_max_minutes=15,
        check_jitter_seconds=0,
        http_timeout_seconds=5.0,
        max_retry_attempts=2,
        retry_base_delay_seconds=0.01,
        request_min_interval_seconds=0.0,
        login_timeout_seconds=10,
        data_dir=data_dir,
        logs_dir=logs_dir,
        sqlite_db_path=data_dir / "samad_monitor.db",
        storage_state_path=data_dir / "storage_state.json",
        browser_profile_dir=data_dir / "browser_profile",
        telegram_enabled=True,
        telegram_bot_token="test-token",
        telegram_chat_ids=["12345"],
        telegram_api_base_url="https://api.telegram.org",
        telegram_proxy_url=None,
        windows_notification_enabled=False,
        log_level="DEBUG",
    )


def _seed_valid_session(config: AppConfig, exp_offset: int = 3600) -> SessionManager:
    sm = SessionManager(
        storage_state_path=config.storage_state_path,
        browser_profile_dir=config.browser_profile_dir,
    )
    sm.save_tokens(
        access_token=make_fake_jwt("401243001", exp_offset_seconds=exp_offset),
        refresh_token="valid-refresh-token-sbu",
        release_date="260115103000",
    )
    return sm


def test_full_lifecycle_scenarios_1_to_5_and_8_restart_persistence(tmp_path: Path) -> None:
    """
    آزمون یکپارچه چرخه عمر واقعی پایش منو:
    - سناریوی ۱: منو منتشر نشده -> ۰ اعلان
    - سناریوی ۲: انتشار اولیه -> ۱ اعلان MENU_AVAILABLE
    - سناریوی ۳: بررسی مجدد بدون تغییر -> ۰ اعلان جدید
    - سناریوی ۸: بستن کامل برنامه و ری‌استارت از روی SQLite -> ۰ اعلان تکراری
    - سناریوی ۴: تغییر غذای شنبه -> ۱ اعلان MENU_UPDATED
    - سناریوی ۵: اضافه شدن روز یکشنبه -> ۱ اعلان MENU_UPDATED
    """
    config = _make_test_config(tmp_path)
    _seed_valid_session(config)

    state: Dict[str, Any] = {"program_response": unpublished_program_error_response()}

    def _mock_samad_api(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/rest/config/release":
            return httpx.Response(200, json=release_response())
        if path == "/rest/users/me":
            return httpx.Response(200, json=user_profile_response())
        if path == "/rest/reservations/selfs":
            return httpx.Response(200, json=sbu_selfs_response())
        if path == "/rest/reservations/programs/v2":
            return httpx.Response(200, json=state["program_response"])
        return httpx.Response(404, json={"type": "ERROR", "messageFa": "Not found"})

    transport = httpx.MockTransport(_mock_samad_api)
    recorder = RecordingChannel()

    def _build_monitor_instance() -> SamadFoodMonitor:
        client = SamadClient(config=config, http_transport=transport)
        notif_mgr = NotificationManager([recorder])
        return SamadFoodMonitor(config=config, client=client, notification_manager=notif_mgr)

    monitor = _build_monitor_instance()

    # --- سناریوی ۱: منوی هفته آینده هنوز منتشر نشده است ---
    s1 = monitor.check_once(REF_THU)
    assert s1.status == CheckStatus.MENU_NOT_PUBLISHED
    assert s1.notification_sent is False
    assert len(recorder.messages) == 0

    # --- سناریوی ۲: منوی هفته آینده برای اولین بار منتشر شد ---
    state["program_response"] = published_programs_v2_response(
        [
            {"date": "2026-11-14", "meal_type_id": 7, "meal_type_name": "ناهار", "food_name": "چلو کباب کوبیده"},
        ]
    )
    s2 = monitor.check_once(REF_THU)
    assert s2.status == CheckStatus.MENU_PUBLISHED
    assert s2.event_type == EventType.MENU_AVAILABLE
    assert s2.notification_sent is True
    assert len(recorder.messages) == 1
    assert recorder.messages[0].event_type == EventType.MENU_AVAILABLE
    assert "چلو کباب کوبیده" in recorder.messages[0].body

    # --- سناریوی ۳: اجرای مجدد با همان منو -> هیچ اعلان تکراری ارسال نشود ---
    s3 = monitor.check_once(REF_THU)
    assert s3.status == CheckStatus.MENU_PUBLISHED
    assert s3.event_type is None
    assert s3.notification_sent is False
    assert len(recorder.messages) == 1

    # --- سناریوی ۸: شبیه‌سازی ری‌استارت کامل فرآیند پایتون و ساخت نمونه جدید از روی دیتابیس SQLite ---
    del monitor
    restarted_monitor = _build_monitor_instance()
    s_restart = restarted_monitor.check_once(REF_THU)
    assert s_restart.status == CheckStatus.MENU_PUBLISHED
    assert s_restart.notification_sent is False
    assert len(recorder.messages) == 1  # همچنان بدون تکرار بعد از ری‌استارت!

    # --- سناریوی ۴: تغییر در غذای روز شنبه ---
    state["program_response"] = published_programs_v2_response(
        [
            {"date": "2026-11-14", "meal_type_id": 7, "meal_type_name": "ناهار", "food_name": "چلو جوجه کباب زعفرانی"},
        ]
    )
    s4 = restarted_monitor.check_once(REF_THU)
    assert s4.status == CheckStatus.MENU_PUBLISHED
    assert s4.event_type == EventType.MENU_UPDATED
    assert s4.notification_sent is True
    assert len(recorder.messages) == 2
    assert "تغییرات منوی هفته آینده" in recorder.messages[-1].title
    assert "چلو جوجه کباب زعفرانی" in recorder.messages[-1].body

    # --- سناریوی ۵: اضافه شدن روز جدید (یکشنبه) به برنامه هفته آینده ---
    state["program_response"] = published_programs_v2_response(
        [
            {"date": "2026-11-14", "meal_type_id": 7, "meal_type_name": "ناهار", "food_name": "چلو جوجه کباب زعفرانی"},
            {"date": "2026-11-15", "meal_type_id": 7, "meal_type_name": "ناهار", "food_name": "زرشک پلو با مرغ"},
        ]
    )
    s5 = restarted_monitor.check_once(REF_THU)
    assert s5.status == CheckStatus.MENU_PUBLISHED
    assert s5.event_type == EventType.MENU_UPDATED
    assert s5.notification_sent is True
    assert len(recorder.messages) == 3
    assert "روزهای جدید اضافه‌شده" in recorder.messages[-1].body
    assert "زرشک پلو با مرغ" in recorder.messages[-1].body


def test_scenario_6_network_outage_logged_gracefully_without_crash(tmp_path: Path) -> None:
    """سناریوی ۶: قطع اینترنت یا خطای شبکه بدون کرش ثبت شود و با «عدم انتشار منو» اشتباه نشود."""
    config = _make_test_config(tmp_path)
    _seed_valid_session(config)

    def _offline_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Network is unreachable", request=request)

    recorder = RecordingChannel()
    monitor = SamadFoodMonitor(
        config=config,
        client=SamadClient(config=config, http_transport=httpx.MockTransport(_offline_handler)),
        notification_manager=NotificationManager([recorder]),
    )

    summary = monitor.check_once(REF_THU)
    assert summary.status == CheckStatus.NETWORK_ERROR
    assert summary.notification_sent is False
    assert len(recorder.messages) == 0

    status_info = monitor.repository.get_status_summary("2026-11-14")
    assert status_info["last_error"] is not None
    assert status_info["last_error"]["status"] == CheckStatus.NETWORK_ERROR.value


def test_scenario_7_session_expiration_and_auto_token_refresh(tmp_path: Path) -> None:
    """
    سناریوی ۷ + تمدید توکن:
    الف) ابتدا توکن منقضی‌شده با refresh_token معتبر به‌صورت خودکار تمدید می‌شود.
    ب) زمانی که refresh_token نیز منقضی شود (401)، وضعیت SESSION_EXPIRED تشخیص داده شده،
       دقیقاً یک هشدار انقضای سشن ارسال می‌شود و در فراخوانی بعدی اسپم تکراری ارسال نمی‌گردد.
    """
    config = _make_test_config(tmp_path)
    _seed_valid_session(config, exp_offset=-120)  # توکن دسترسی منقضی شده است

    refresh_allowed = {"ok": True}

    def _auth_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth/token":
            if refresh_allowed["ok"]:
                return httpx.Response(
                    200,
                    json={
                        "access_token": make_fake_jwt("401243001", exp_offset_seconds=3600),
                        "refresh_token": "rotated-refresh-token",
                    },
                )
            return httpx.Response(401, json={"error": "invalid_grant"})

        auth_hdr = request.headers.get("Authorization", "")
        if not refresh_allowed["ok"] or not auth_hdr.startswith("Bearer "):
            return httpx.Response(401, json={"error": "invalid_token"})

        if path == "/rest/users/me":
            return httpx.Response(200, json=user_profile_response())
        if path == "/rest/reservations/selfs":
            return httpx.Response(200, json=sbu_selfs_response())
        if path == "/rest/reservations/programs/v2":
            return httpx.Response(200, json=unpublished_program_error_response())
        return httpx.Response(200, json=release_response())

    recorder = RecordingChannel()
    monitor = SamadFoodMonitor(
        config=config,
        client=SamadClient(config=config, http_transport=httpx.MockTransport(_auth_handler)),
        notification_manager=NotificationManager([recorder]),
    )

    # الف: تمدید خودکار موفق با Refresh Token
    ok_summary = monitor.check_once(REF_THU)
    assert ok_summary.status == CheckStatus.MENU_NOT_PUBLISHED
    assert len(recorder.messages) == 0

    # ب: منقضی شدن کامل نشست (401 روی refresh_token)
    _seed_valid_session(config, exp_offset=-120)
    refresh_allowed["ok"] = False

    exp1 = monitor.check_once(REF_THU)
    assert exp1.status == CheckStatus.SESSION_EXPIRED
    assert exp1.notification_sent is True
    assert len(recorder.messages) == 1
    assert recorder.messages[0].event_type == EventType.SESSION_EXPIRED

    # ج: اجرای مجدد نباید هشدار انقضای سشن تکراری ارسال کند و پرچم توقف باید روشن باشد
    exp2 = monitor.check_once(REF_THU)
    assert exp2.status == CheckStatus.SESSION_EXPIRED
    assert exp2.notification_sent is False
    assert len(recorder.messages) == 1
    assert monitor.repository.get_runtime_value("paused_for_relogin") == "true"


def test_strict_read_only_guard_blocks_all_reservations_and_mutations() -> None:
    """تضمین مطلق بند ۱۵: هیچ درخواست رزرو، لغو، تغییر سلف یا پرداخت هرگز اجازه عبور ندارد."""
    # درخواست‌های خواندنی مجاز
    enforce_read_only_request("GET", "/rest/reservations/programs/v2?selfId=101")
    enforce_read_only_request(
        "POST", "/oauth/token", form_body={"grant_type": "refresh_token"}
    )

    # درخواست‌های ممنوعه رزرو/لغو/پرداخت/ارسال رمز عبور
    forbidden_calls: List[Callable[[], None]] = [
        lambda: enforce_read_only_request("POST", "/rest/reserves/5001/reserve"),
        lambda: enforce_read_only_request("DELETE", "/rest/reserves/999"),
        lambda: enforce_read_only_request("PUT", "/rest/reserves/999/change-self/102"),
        lambda: enforce_read_only_request("POST", "/rest/daily-sell-programs/10/reserve"),
        lambda: enforce_read_only_request("POST", "/rest/modules/epayment/process-payment"),
        lambda: enforce_read_only_request(
            "POST", "/oauth/token", form_body={"grant_type": "password"}
        ),
    ]
    for fn in forbidden_calls:
        with pytest.raises(ReadOnlyGuardError):
            fn()
