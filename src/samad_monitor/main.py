"""
رابط خط فرمان (CLI) سامانه پایش منوی سماد دانشگاه شهید بهشتی:
- `python -m samad_monitor login`
- `python -m samad_monitor check-now`
- `python -m samad_monitor status`
- `python -m samad_monitor run`
- `python -m samad_monitor doctor`
- `python -m samad_monitor test-notify`
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from samad_monitor.browser.client import SamadClient
from samad_monitor.config import AppConfig
from samad_monitor.database.repository import MonitorRepository
from samad_monitor.database.sqlite import DatabaseManager
from samad_monitor.models import CheckStatus, EventType, NotificationMessage
from samad_monitor.monitor import SamadFoodMonitor
from samad_monitor.notifications.manager import NotificationManager
from samad_monitor.scheduler import AdaptiveScheduler
from samad_monitor.utils.date_utils import (
    format_jalali_verbose,
    get_current_week_range,
    get_next_week_range,
    is_peak_check_day,
    now_tehran,
    parse_iso_date,
    to_persian_digits,
)
from samad_monitor.utils.logger import setup_logging

STATUS_LABELS_FA = {
    CheckStatus.MENU_PUBLISHED.value: "منوی هفته آینده منتشر شده است ✅",
    CheckStatus.MENU_NOT_PUBLISHED.value: "اتصال برقرار — منوی هفته آینده هنوز منتشر نشده است ⏳",
    CheckStatus.SESSION_EXPIRED.value: "نشست کاربری منقضی شده است (نیاز به login) ⚠️",
    CheckStatus.NETWORK_ERROR.value: "خطای شبکه / عدم دسترسی به اینترنت ❌",
    CheckStatus.SITE_UNREACHABLE.value: "سامانه سماد موقتاً از دسترس خارج است ❌",
    CheckStatus.SERVER_ERROR.value: "خطای داخلی سرور سماد ❌",
    CheckStatus.INCOMPLETE_MENU_DATA.value: "داده دریافتی منو ناقص است ⚠️",
    CheckStatus.SITE_STRUCTURE_CHANGED.value: "تغییر ساختار پاسخ/صفحه سامانه سماد ⚠️",
}


def _configure_utf8_console() -> None:
    """تضمین نمایش صحیح حروف فارسی در PowerShell و Windows Terminal ویندوز ۱۱."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        elif stream is not None and hasattr(stream, "buffer"):
            try:
                setattr(
                    sys,
                    stream_name,
                    io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"),
                )
            except Exception:
                pass


def _format_iso_timestamp_fa(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return "ثبت نشده"
    try:
        dt = datetime.fromisoformat(iso_ts)
        j_str = format_jalali_verbose(dt.date())
        return f"{j_str} ساعت {dt.strftime('%H:%M:%S')} ({dt.strftime('%Y-%m-%d')})"
    except Exception:
        return iso_ts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="samad_monitor",
        description="سامانه هوشمند پایش و اعلان انتشار منوی غذای سماد — دانشگاه شهید بهشتی تهران",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="مسیر سفارشی فایل تنظیمات .env",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "login",
        help="باز کردن مرورگر برای ورود دستی به سماد و ذخیره امن نشست محلی",
    )
    subparsers.add_parser(
        "check-now",
        help="اجرای فوری یک نوبت بررسی منوی هفته آینده",
    )
    subparsers.add_parser(
        "status",
        help="نمایش کامل وضعیت اتصال، نشست، آخرین بررسی، وضعیت هفته آینده و کانال‌های اعلان",
    )
    subparsers.add_parser(
        "run",
        help="اجرای پیوسته سرویس پایش هوشمند در پس‌زمینه طبق زمان‌بندی تهران",
    )
    subparsers.add_parser(
        "doctor",
        help="بررسی سلامت محیط اجرا، امنیت فایل‌ها، Playwright و تنظیمات اعلان",
    )
    subparsers.add_parser(
        "test-notify",
        help="ارسال یک پیام آزمایشی به کانال‌های اعلان فعال (تلگرام / ویندوز ۱۱)",
    )

    ui_parser = subparsers.add_parser(
        "ui",
        help="راه‌اندازی پنل گرافیکی مدیریت و پایش در مرورگر (Dashboard)",
    )
    ui_parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="آدرس Bind سرور پنل (پیش‌فرض امن محلی: 127.0.0.1)",
    )
    ui_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="پورت سرور پنل گرافیکی (پیش‌فرض: 8765)",
    )
    ui_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="عدم باز کردن خودکار پنجره مرورگر هنگام شروع پنل",
    )
    ui_parser.add_argument(
        "--auto-start",
        action="store_true",
        help="روشن کردن خودکار زمان‌بند پایش به محض بالا آمدن پنل گرافیکی",
    )
    return parser


def _cmd_login(config: AppConfig) -> int:
    print("============================================================")
    print(f"🔐 ورود دستی و ایمن به سامانه سماد ({config.samad_university_label})")
    print("============================================================")
    print("۱. اکنون پنجره مرورگر کرومیوم باز می‌شود.")
    print("۲. دانشگاه «دانشگاه شهید بهشتی» را تأیید و با حساب دانشجویی خود وارد شوید.")
    print("۳. به محض ورود به صفحه رزرو غذا، نشست شما به‌صورت محلی و امن ذخیره شده و مرورگر بسته می‌شود.")
    print("   (این برنامه هرگز نام کاربری یا رمز عبور شما را دریافت یا ذخیره نمی‌کند.)\n")

    client = SamadClient(config=config)
    try:
        session_data = client.interactive_login()
    except Exception as exc:
        print(f"\n❌ خطا در فرآیند ورود دستی: {exc}")
        return 1

    db = DatabaseManager(config.db_path)
    repo = MonitorRepository(db, tz_name=config.timezone_name)
    repo.set_runtime_value("session_status", "ACTIVE")
    repo.set_runtime_value("paused_for_relogin", "false")
    repo.set_runtime_value("active_session_fp", session_data.fingerprint)
    if session_data.username:
        repo.set_runtime_value("last_username", session_data.username)

    print("\n✅ ورود با موفقیت انجام شد و نشست محلی ذخیره گردید!")
    print(f"   • دانشگاه ثبت‌شده در نشست : {session_data.university_label} ({session_data.university_key})")
    if session_data.username:
        print(f"   • شناسه کاربری شناسایی‌شده: {session_data.username}")
    print(f"   • فایل نشست محافظت‌شده     : {config.storage_state_path}")
    print("\nاکنون می‌توانید دستور `python -m samad_monitor check-now` یا `python -m samad_monitor run` را اجرا کنید.")
    return 0


def _cmd_check_now(config: AppConfig) -> int:
    monitor = SamadFoodMonitor(config=config)
    summary = monitor.check_once()

    print("============================================================")
    print(f"🔎 نتیجه بررسی فوری سماد — {config.samad_university_label}")
    print("============================================================")
    print(f"• هفته هدف (آینده) : {summary.target_week.display_bilingual()}")
    print(f"• وضعیت بررسی      : {STATUS_LABELS_FA.get(summary.status.value, summary.status.value)}")
    print(f"• منبع استخراج     : {summary.source}")
    print(f"• تعداد ردیف غذا   : {to_persian_digits(summary.meals_count)}")
    print(f"• رویداد تشخیصی    : {summary.event_type.value if summary.event_type else 'بدون رویداد جدید'}")
    print(f"• ارسال اعلان جدید : {'بله ✅' if summary.notification_sent else 'خیر'}")
    print(f"• زمان پاسخ‌گویی    : {to_persian_digits(summary.duration_ms)} میلی‌ثانیه")
    print(f"• توضیحات          : {summary.message}")

    if summary.delivery_report and summary.delivery_report.failed:
        print("\n⚠️ خطا در برخی کانال‌های اعلان:")
        for ch, err in summary.delivery_report.failed.items():
            print(f"   - {ch}: {err}")

    return 0 if summary.status in {CheckStatus.MENU_PUBLISHED, CheckStatus.MENU_NOT_PUBLISHED} else 2


def _cmd_status(config: AppConfig) -> int:
    config.ensure_directories()
    db = DatabaseManager(config.db_path)
    repo = MonitorRepository(db, tz_name=config.timezone_name)
    client = SamadClient(config=config)
    notif_mgr = NotificationManager.from_config(config, repository=repo)

    tehran_now = now_tehran(tz_name=config.timezone_name)
    curr_week = get_current_week_range(tehran_now, tz_name=config.timezone_name)
    next_week = get_next_week_range(tehran_now, tz_name=config.timezone_name)
    summary = repo.get_status_summary(next_week.start_iso)
    session_data = client.session_manager.load()

    # تعیین وضعیت اتصال
    last_check = summary["last_check"]
    if last_check is None:
        conn_status = "هنوز بررسی انجام نشده است"
    else:
        st_code = last_check["status"]
        conn_status = STATUS_LABELS_FA.get(st_code, st_code)

    # تعیین وضعیت سشن
    if not session_data.has_credentials:
        if summary["session_status"] == "EXPIRED" or summary["paused_for_relogin"]:
            sess_status = "منقضی شده ⚠️ (پایش متوقف تا اجرای `python -m samad_monitor login`)"
        else:
            sess_status = "فاقد نشست ذخیره‌شده (نیاز به اجرای `python -m samad_monitor login`)"
    elif summary["session_status"] == "EXPIRED":
        sess_status = "منقضی شده ⚠️ (نیاز به ورود مجدد)"
    else:
        user_tag = f" (کاربر: {session_data.username or summary['last_username']})" if (session_data.username or summary["last_username"]) else ""
        token_note = "توکن آماده تمدید خودکار با Refresh Token" if session_data.is_access_token_expired else "معتبر و فعال ✅"
        sess_status = f"{token_note}{user_tag}"

    # آخرین بررسی موفق
    last_ok = summary["last_successful_check"]
    last_ok_str = _format_iso_timestamp_fa(last_ok["checked_at"]) if last_ok else "هنوز ثبت نشده"

    # آخرین تاریخ منوی دیده‌شده در کل دیتابیس
    latest_menu = summary["latest_menu"]
    if latest_menu and latest_menu.meals:
        max_meal_date_iso = max(m.date for m in latest_menu.meals)
        parsed_max = parse_iso_date(max_meal_date_iso)
        last_seen_menu_str = (
            f"{format_jalali_verbose(parsed_max)} ({max_meal_date_iso}) — مربوط به هفته شروع {latest_menu.week_start}"
            if parsed_max
            else max_meal_date_iso
        )
    else:
        last_seen_menu_str = "هنوز هیچ منوی منتشرشده‌ای ثبت نشده است"

    # وضعیت انتشار هفته آینده
    nw_menu = summary["next_week_menu"]
    if nw_menu and nw_menu.meals:
        days_cnt = len({m.date for m in nw_menu.meals})
        nw_status = (
            f"منتشر شده ✅ ({to_persian_digits(len(nw_menu.meals))} گزینه غذایی در "
            f"{to_persian_digits(days_cnt)} روز | وضعیت: {nw_menu.status})"
        )
    else:
        nw_status = "هنوز منتشر نشده است ⏳"

    # زمان بررسی بعدی
    raw_next_sched = summary["next_scheduled_check_at"]
    if raw_next_sched and raw_next_sched not in {"سرویس متوقف است", "متوقف تا ورود مجدد (login)"}:
        next_sched_str = _format_iso_timestamp_fa(raw_next_sched)
    elif raw_next_sched:
        next_sched_str = raw_next_sched
    else:
        mode_note = (
            f"پنجشنبه/جمعه (هر {to_persian_digits(config.peak_interval_min_minutes)} تا {to_persian_digits(config.peak_interval_max_minutes)} دقیقه)"
            if is_peak_check_day(tehran_now, tz_name=config.timezone_name)
            else f"روز عادی (هر {to_persian_digits(config.normal_interval_minutes)} دقیقه)"
        )
        next_sched_str = f"سرویس پس‌زمینه در حال اجرا نیست — رژیم زمانی فعلی: {mode_note}"

    print("======================================================================")
    print(f"📊 وضعیت سامانه پایش غذای سماد — {config.samad_university_label}")
    print("======================================================================")
    print(f"🕒 زمان فعلی (تهران)         : {_format_iso_timestamp_fa(tehran_now.isoformat())}")
    print(f"📅 هفته جاری دانشگاهی        : {curr_week.display_bilingual()}")
    print(f"🎯 هفته هدف (هفته آینده)     : {next_week.display_bilingual()}")
    print("----------------------------------------------------------------------")
    print(f"🌐 وضعیت اتصال               : {conn_status}")
    print(f"🔑 وضعیت نشست کاربری (سشن)   : {sess_status}")
    print(f"✅ زمان آخرین بررسی موفق     : {last_ok_str}")
    print(f"🍽 آخرین تاریخ منوی دیده‌شده : {last_seen_menu_str}")
    print(f"📢 وضعیت انتشار هفته آینده   : {nw_status}")
    print(f"⏱ زمان بررسی بعدی           : {next_sched_str}")
    print(f"📈 کل بررسی‌ها / اعلان‌ها    : {to_persian_digits(summary['total_checks'])} بررسی / {to_persian_digits(summary['total_sent_events'])} رویداد ارسال‌شده")
    print("----------------------------------------------------------------------")
    print("🔔 وضعیت کانال‌های اعلان:")
    for ch_name, ch_desc in notif_mgr.describe_channels().items():
        print(f"   • {ch_name:10s} : {ch_desc}")

    if summary["last_error"]:
        err = summary["last_error"]
        print("----------------------------------------------------------------------")
        print(
            f"⚠️ آخرین خطای ثبت‌شده ({_format_iso_timestamp_fa(err['logged_at'])}): "
            f"[{err['status']}] {err['message']}"
        )
    print("======================================================================")
    return 0


def _cmd_run(config: AppConfig) -> int:
    monitor = SamadFoodMonitor(config=config)
    scheduler = AdaptiveScheduler(config=config, monitor=monitor)
    scheduler.run_forever()
    return 0


def _cmd_doctor(config: AppConfig) -> int:
    print("============================================================")
    print("🩺 عیب‌یاب سلامت سیستم (Doctor) — Samad Food Monitor")
    print("============================================================")
    issues = 0

    # ۱. بررسی نسخه پایتون و منطقه زمانی تهران
    tehran_now = now_tehran(tz_name=config.timezone_name)
    print(f"[✅] پایتون {sys.version.split()[0]} روی پلتفرم {sys.platform}")
    print(f"[✅] منطقه زمانی مرجع ({config.timezone_name}): {tehran_now.strftime('%Y-%m-%d %H:%M:%S %z')}")

    # ۲. بررسی امنیت متغیرهای محیطی
    forbidden = config.detect_forbidden_credential_vars()
    if forbidden:
        issues += 1
        print(f"[❌] کلیدهای ممنوعه رمز عبور در محیط یافت شدند (فوراً حذف شوند): {', '.join(forbidden)}")
    else:
        print("[✅] هیچ رمز عبور یا نام کاربری دانشجویی در فایل تنظیمات ذخیره نشده است.")

    # ۳. بررسی نصب بودن مرورگر Playwright Chromium یا مرورگر سیستم (Chrome / Edge)
    try:
        from playwright.sync_api import sync_playwright
        from samad_monitor.browser.client import resolve_browser_launch_options

        with sync_playwright() as p:
            b_opts, b_label = resolve_browser_launch_options(p)
            if b_opts is not None:
                print(f"[✅] مرورگر آماده اجرا است: {b_label}")
            else:
                issues += 1
                print("[❌] هیچ مرورگر Chromium / Chrome / Edge یافت نشد.")
    except Exception as exc:
        issues += 1
        print(f"[❌] خطا در بررسی Playwright: {exc}")

    # ۴. بررسی دیتابیس SQLite
    try:
        config.ensure_directories()
        db = DatabaseManager(config.db_path)
        with db.transaction() as conn:
            conn.execute("SELECT 1").fetchone()
        print(f"[✅] دیتابیس SQLite سالم و قابل نوشتن است: {config.db_path}")
    except Exception as exc:
        issues += 1
        print(f"[❌] خطا در دسترسی به دیتابیس SQLite: {exc}")

    # ۵. بررسی وضعیت نشست محلی
    client = SamadClient(config=config)
    sess = client.session_manager.load()
    if sess.has_credentials:
        print(f"[✅] فایل نشست محلی موجود است ({sess.university_label} - {sess.university_key})")
    else:
        print("[ℹ️] فایل نشست فعال یافت نشد (برای ورود اولیه اجرا کنید: python -m samad_monitor login)")

    # ۶. بررسی کانال‌های اعلان
    repo_for_notif = MonitorRepository(DatabaseManager(config.db_path), tz_name=config.timezone_name)
    notif_mgr = NotificationManager.from_config(config, repository=repo_for_notif)
    active = notif_mgr.active_channel_names()
    for ch_name, ch_desc in notif_mgr.describe_channels().items():
        mark = "✅" if ch_name in active else "ℹ️"
        print(f"[{mark}] کانال اعلان {ch_name}: {ch_desc}")

    if not active:
        issues += 1
        print("[⚠️] هیچ کانال اعلانی در حال حاضر فعال نیست! در فایل .env تلگرام یا اعلان ویندوز را فعال کنید.")

    # ۷. یادآوری شبکه ایران برای تفکیک VPN
    if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY"):
        if config.samad_ignore_env_proxy:
            print("[✅] متغیر پروکسی سیستم روشن است، اما SAMAD_IGNORE_ENV_PROXY=true مانع اختلال در اتصال به سماد می‌شود.")
        else:
            print("[⚠️] پروکسی سیستم روشن است و SAMAD_IGNORE_ENV_PROXY=false است؛ سرورهای دانشگاه ممکن است IP خارجی را رد کنند.")

    print("============================================================")
    return 0 if issues == 0 else 1


def _cmd_test_notify(config: AppConfig) -> int:
    config.ensure_directories()
    repo = MonitorRepository(DatabaseManager(config.db_path), tz_name=config.timezone_name)
    notif_mgr = NotificationManager.from_config(config, repository=repo)
    active = notif_mgr.active_channel_names()
    if not active:
        print("❌ هیچ کانال اعلانی فعال نیست. ابتدا فایل .env را تکمیل کنید.")
        return 1

    tehran_now = now_tehran(tz_name=config.timezone_name)
    next_week = get_next_week_range(tehran_now, tz_name=config.timezone_name)
    test_msg = NotificationMessage(
        event_id=f"TEST_NOTIFY:{int(tehran_now.timestamp())}",
        event_type=EventType.MENU_AVAILABLE,
        title="🧪 پیام آزمایشی پایشگر سماد",
        body=(
            "🧪 پیام آزمایشی سامانه پایش غذای سماد\n"
            f"🏛 {config.samad_university_label}\n"
            f"🗓 هفته هدف فعلی: {next_week.display_bilingual()}\n"
            "────────────────────\n"
            "این پیام صرفاً جهت اطمینان از صحت تنظیمات کانال اعلان ارسال شده است.\n"
            f"🔗 {config.samad_reserve_url}"
        ),
        short_summary="تست اعلان Samad Food Monitor با موفقیت انجام شد.",
        action_url=config.samad_reserve_url,
    )
    report = notif_mgr.dispatch(test_msg)
    if report.succeeded:
        print(f"✅ پیام آزمایشی با موفقیت به کانال‌های زیر ارسال شد: {', '.join(report.succeeded)}")
    if report.failed:
        for ch, err in report.failed.items():
            print(f"❌ خطا در کانال {ch}: {err}")
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    _configure_utf8_console()
    parser = _build_parser()
    args = parser.parse_args(argv)

    config = AppConfig.from_env(env_file=args.env_file)
    config.ensure_directories()
    logger = setup_logging(
        logs_dir=config.logs_dir,
        level=config.log_level,
        tz_name=config.timezone_name,
    )

    forbidden = config.detect_forbidden_credential_vars()
    if forbidden:
        logger.warning(
            "هشدار امنیتی: متغیرهای رمز عبور غیرمجاز (%s) در محیط دیده شدند و توسط برنامه نادیده گرفته می‌شوند.",
            ", ".join(forbidden),
        )

    if args.command == "login":
        return _cmd_login(config)
    if args.command == "check-now":
        return _cmd_check_now(config)
    if args.command == "status":
        return _cmd_status(config)
    if args.command == "run":
        return _cmd_run(config)
    if args.command == "doctor":
        return _cmd_doctor(config)
    if args.command == "test-notify":
        return _cmd_test_notify(config)
    if args.command == "ui":
        from samad_monitor.ui.server import run_ui_server

        return run_ui_server(
            config=config,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
            auto_start_scheduler=args.auto_start,
            env_file_override=args.env_file,
        )

    parser.print_help()
    return 1
