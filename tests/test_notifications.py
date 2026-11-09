"""تست‌های قالب‌بندی فارسی پیام‌ها، تلگرام، ویندوز ۱۱ و پنهان‌سازی اسرار در لاگ (سناریوی ۱۰)."""

from __future__ import annotations

from datetime import date
from typing import List

import httpx

from samad_monitor.models import Meal, Menu, MenuDiff, NotificationMessage, WeekRange
from samad_monitor.notifications.formatter import (
    format_menu_available_notification,
    format_menu_updated_notification,
    format_session_expired_notification,
)
from samad_monitor.notifications.manager import NotificationManager
from samad_monitor.notifications.telegram import (
    TelegramNotificationChannel,
    split_telegram_text,
)
from samad_monitor.notifications.windows import WindowsDesktopNotificationChannel
from samad_monitor.utils.logger import redact_secrets


def _sample_week_and_menu() -> tuple[WeekRange, Menu]:
    wk = WeekRange(week_start=date(2026, 11, 14), week_end=date(2026, 11, 20))
    meals = [
        Meal(
            date="2026-11-14",
            day_name="شنبه",
            meal_type="ناهار",
            meal_type_id=7,
            restaurant="سلف مرکزی ولنجک",
            self_id=101,
            is_free_restaurant=False,
            food_name="چلو جوجه کباب",
            availability="OPEN",
            side_dishes="ماست، دوغ",
            price_rials=150000,
        ),
        Meal(
            date="2026-11-15",
            day_name="یکشنبه",
            meal_type="شام",
            meal_type_id=8,
            restaurant="سلف مرکزی ولنجک",
            self_id=101,
            is_free_restaurant=False,
            food_name="خوراک شنیسل مرغ",
            availability="OPEN",
            price_rials=120000,
        ),
    ]
    menu = Menu(
        week_start=wk.start_iso,
        week_end=wk.end_iso,
        detected_at="2026-11-12T14:00:00+03:30",
        updated_at="2026-11-12T14:00:00+03:30",
        status="PUBLISHED",
        meals=meals,
        content_hash="abc1234567890def",
    )
    return wk, menu


def test_persian_initial_and_updated_formatting() -> None:
    """سناریوی ۱۰: بررسی کامل متن فارسی، تاریخ دوزبانه شمسی/میلادی، نام غذا، سلف و لینک رزرو."""
    wk, menu = _sample_week_and_menu()

    init_msg = format_menu_available_notification(menu, wk)
    assert "منوی هفته آینده سماد منتشر شد" in init_msg.title
    assert "دانشگاه شهید بهشتی" in init_msg.body
    assert "۲۳ آبان ۱۴۰۵" in init_msg.body
    assert "2026-11-14" in init_msg.body
    assert "چلو جوجه کباب" in init_msg.body
    assert "۱۵٬۰۰۰ تومان" in init_msg.body
    assert "https://samad.app/food/reserve" in init_msg.body

    new_meal = Meal(
        date="2026-11-16",
        day_name="دوشنبه",
        meal_type="ناهار",
        meal_type_id=7,
        restaurant="سلف مرکزی ولنجک",
        self_id=101,
        is_free_restaurant=False,
        food_name="قورمه‌سبزی",
    )
    menu.meals.append(new_meal)
    diff = MenuDiff(added_dates=["2026-11-16"], added_meals=[new_meal])
    upd_msg = format_menu_updated_notification(menu, diff, wk)

    assert "تغییرات منوی هفته آینده سماد" in upd_msg.title
    assert "روزهای جدید اضافه‌شده" in upd_msg.body
    assert "قورمه‌سبزی" in upd_msg.body
    assert "https://samad.app/food/reserve" in upd_msg.body

    exp_msg = format_session_expired_notification("SESSION_EXPIRED:test")
    assert "منقضی" in exp_msg.title
    assert "python -m samad_monitor login" in exp_msg.body


def test_telegram_channel_sending_and_long_message_splitting() -> None:
    sent_payloads: List[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        sent_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(_handler)
    tg = TelegramNotificationChannel(
        bot_token="123456789:ABCDEF_TEST_SECRET_TOKEN_XYZ",
        chat_id="987654321",
        http_transport=transport,
    )

    wk, menu = _sample_week_and_menu()
    msg = format_menu_available_notification(menu, wk)
    # تست پیام بسیار طولانی برای اطمینان از تقسیم صحیح زیر سقف ۴۰۹۶ کاراکتر تلگرام
    long_body = "\n\n".join([msg.body for _ in range(18)])
    chunks = split_telegram_text(long_body)
    assert len(chunks) >= 2
    assert all(len(c) <= 3900 for c in chunks)

    long_msg = NotificationMessage(
        event_id=msg.event_id,
        event_type=msg.event_type,
        title=msg.title,
        body=long_body,
        short_summary=msg.short_summary,
    )
    tg.send(long_msg)
    assert len(sent_payloads) == len(chunks)
    assert sent_payloads[0]["chat_id"] == "987654321"
    assert "(1/" in sent_payloads[0]["text"]


def test_windows_channel_graceful_non_windows_and_custom_sender() -> None:
    """در لینوکس بدون خطا غیرفعال می‌شود و در ویندوز/ارسال‌کننده بومی پیام را تحویل می‌دهد."""
    linux_ch = WindowsDesktopNotificationChannel(platform_override="linux")
    configured, desc = linux_ch.is_configured()
    assert configured is False
    assert "Windows" in desc

    delivered: List[NotificationMessage] = []
    win_mock_ch = WindowsDesktopNotificationChannel(custom_sender=delivered.append)
    mgr = NotificationManager([linux_ch, win_mock_ch])

    wk, menu = _sample_week_and_menu()
    msg = format_menu_available_notification(menu, wk)
    report = mgr.dispatch(msg)

    assert report.succeeded == ["windows"]
    assert len(delivered) == 1


def test_secret_redaction_in_logs() -> None:
    raw = (
        'GET /api Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret '
        'https://api.telegram.org/bot123456789:AAH_SECRET_BOT_TOKEN_12345/sendMessage'
    )
    cleaned = redact_secrets(raw)
    assert "eyJhbGci" not in cleaned
    assert "AAH_SECRET_BOT_TOKEN_12345" not in cleaned
    assert "[REDACTED_TOKEN]" in cleaned
    assert "[REDACTED_BOT_TOKEN]" in cleaned


def test_multi_subscriber_broadcast_fault_isolation_and_bot_commands(tmp_path) -> None:
    """
    بررسی کامل قابلیت چندکاربره ربات تلگرام:
    ۱. دریافت منو توسط همه مشترکین حتی اگر یکی از دوستان ربات را بلاک کرده باشد (403)
    ۲. ارسال هشدار SESSION_EXPIRED منحصراً به مدیران (نه همه دوستان)
    ۳. عضویت خودکار با /start + کد دعوت، استعلام /menu از دیتابیس و لغو عضویت با /stop
    """
    import json
    from samad_monitor.database.repository import MonitorRepository
    from samad_monitor.database.sqlite import DatabaseManager

    db = DatabaseManager(tmp_path / "subs.db")
    repo = MonitorRepository(db, tz_name="Asia/Tehran")
    from samad_monitor.utils.date_utils import get_next_week_range, now_tehran

    real_nw = get_next_week_range(now_tehran(tz_name="Asia/Tehran"), tz_name="Asia/Tehran")
    _, sample_menu = _sample_week_and_menu()
    sample_menu.week_start = real_nw.start_iso
    sample_menu.week_end = real_nw.end_iso
    repo.save_menu(sample_menu)

    # ثبت یک دوست بلاک‌کننده و یک دوست سالم
    repo.upsert_telegram_subscriber("222222", display_name="دوست بلاک‌کرده", role="subscriber")
    repo.upsert_telegram_subscriber("333333", display_name="دوست فعال", role="subscriber")

    delivered_to: List[tuple[str, str]] = []
    updates_queue = [
        {
            "update_id": 501,
            "message": {
                "chat": {"id": 444444, "first_name": "سارا", "username": "sara_sbu", "type": "private"},
                "text": "/start sbu1405",
            },
        },
        {
            "update_id": 502,
            "message": {
                "chat": {"id": 444444, "first_name": "سارا", "username": "sara_sbu", "type": "private"},
                "text": "/menu",
            },
        },
        {
            "update_id": 503,
            "message": {
                "chat": {"id": 333333, "first_name": "امیر", "type": "private"},
                "text": "/stop",
            },
        },
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            offset = int(request.url.params.get("offset", "0") or 0)
            batch = [u for u in updates_queue if u["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": batch})

        payload = json.loads(request.content.decode("utf-8"))
        cid = str(payload["chat_id"])
        if cid == "222222":
            return httpx.Response(
                403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}
            )
        delivered_to.append((cid, payload["text"]))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 10}})

    tg = TelegramNotificationChannel(
        bot_token="123456:TEST_MULTI_USER_TOKEN",
        chat_id=["111111"],  # مدیر اصلی از .env
        http_transport=httpx.MockTransport(_handler),
        repository=repo,
    )

    # ۱. اعلان انتشار منو باید هم به مدیر (111111) و هم به دوست فعال (333333) برسد بدون خطا بابت 222222
    menu_msg = format_menu_available_notification(sample_menu, real_nw)
    tg.send(menu_msg)
    assert [c for c, _ in delivered_to] == ["111111", "333333"]

    # ۲. هشدار انقضای سشن فقط باید برای مدیر (111111) ارسال شود، نه دوستان مشترک!
    delivered_to.clear()
    exp_msg = format_session_expired_notification("SESSION_EXPIRED:fp1")
    tg.send(exp_msg)
    assert [c for c, _ in delivered_to] == ["111111"]

    # ۳. پردازش دستورات /start (با کد دعوت)، /menu و /stop
    delivered_to.clear()
    sync_res = tg.sync_bot_updates(auto_subscribe=True, invite_code="sbu1405", handle_commands=True)
    assert sync_res["ok"] is True
    assert sync_res["auto_added"] == 1

    # سارا (444444) باید عضو فعال شده باشد و پاسخ خوش‌آمدگویی + متن منو را گرفته باشد
    sara = repo.get_telegram_subscriber("444444")
    assert sara is not None and sara["enabled"] is True
    sara_msgs = [txt for cid, txt in delivered_to if cid == "444444"]
    assert any("عضویت شما" in m for m in sara_msgs)
    assert any("چلو جوجه کباب" in m for m in sara_msgs)

    # امیر (333333) با دستور /stop باید غیرفعال شده باشد
    amir = repo.get_telegram_subscriber("333333")
    assert amir is not None and amir["enabled"] is False

