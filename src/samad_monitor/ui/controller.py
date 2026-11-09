"""کنترلر مرکزی داشبورد گرافیکی با ایمنی ترد (Thread-Safe) و مدیریت زمان‌بند پس‌زمینه."""

from __future__ import annotations

import logging
import socket
import sys
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from samad_monitor.browser.client import SamadClient
from samad_monitor.config import AppConfig
from samad_monitor.database.repository import MonitorRepository
from samad_monitor.database.sqlite import DatabaseManager
from samad_monitor.models import (
    CheckStatus,
    EventType,
    Meal,
    Menu,
    NotificationMessage,
    WeekRange,
)
from samad_monitor.monitor import SamadFoodMonitor
from samad_monitor.notifications.manager import NotificationManager
from samad_monitor.notifications.telegram import TelegramBotPoller
from samad_monitor.parser import meal_rank
from samad_monitor.scheduler import AdaptiveScheduler
from samad_monitor.ui.env_writer import update_env_file
from samad_monitor.utils.date_utils import (
    format_jalali_day_month,
    format_jalali_full,
    get_current_week_range,
    get_next_week_range,
    get_persian_weekday,
    get_previous_week_range,
    is_peak_check_day,
    now_tehran,
    parse_iso_date,
    to_persian_digits,
)
from samad_monitor.utils.logger import redact_secrets

logger = logging.getLogger("samad_monitor.ui")


def _format_iso_fa(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "ثبت نشده"
    try:
        dt = datetime.fromisoformat(iso_str)
        return f"{format_jalali_full(dt.date())} ساعت {to_persian_digits(dt.strftime('%H:%M:%S'))}"
    except Exception:
        return iso_str


def _serialize_week_days(
    week_range: WeekRange, meals: List[Meal]
) -> List[Dict[str, Any]]:
    """ساخت آرایه کامل ۷ روز هفته (شنبه تا جمعه) حتی اگر برخی روزها فاقد وعده باشند."""
    by_date: Dict[str, List[Meal]] = {}
    for m in meals:
        by_date.setdefault(m.date, []).append(m)

    days_out: List[Dict[str, Any]] = []
    for d in week_range.all_dates():
        iso_d = d.isoformat()
        day_meals = sorted(
            by_date.get(iso_d, []),
            key=lambda x: (meal_rank(x.meal_type_id, x.meal_type), x.self_id, x.food_name),
        )
        serialized_meals = []
        for dm in day_meals:
            price_toman = dm.price_rials // 10 if dm.price_rials > 0 else 0
            serialized_meals.append(
                {
                    **asdict(dm),
                    "price_toman": price_toman,
                    "price_toman_fa": (
                        f"{to_persian_digits(f'{price_toman:,}'.replace(',', '٬'))} تومان"
                        if price_toman > 0
                        else "رایگان / نامشخص"
                    ),
                }
            )

        days_out.append(
            {
                "date_iso": iso_d,
                "weekday_fa": get_persian_weekday(d),
                "jalali_short": format_jalali_day_month(d),
                "jalali_full": format_jalali_full(d, include_weekday=True),
                "meals": serialized_meals,
                "meals_count": len(serialized_meals),
            }
        )
    return days_out


class DashboardController:
    """هماهنگ‌کننده عملیات پنل وب با هسته پایشگر، دیتابیس و زمان‌بند."""

    def __init__(self, config: AppConfig, env_file_override: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._env_file_override = env_file_override
        self._bot_poller: Optional[TelegramBotPoller] = None
        self._last_env_mtime: Optional[float] = None
        self._profile_probed_token: str = ""
        self.config = config
        self._rebuild_components()

        self._scheduler: Optional[AdaptiveScheduler] = None
        self._scheduler_thread: Optional[threading.Thread] = None
        self._is_checking_now = False
        self._is_logging_in = False

    def _resolve_target_env_path(self) -> Path:
        if self._env_file_override is not None:
            return self._env_file_override
        if self.config.env_file is not None:
            return self.config.env_file
        return self.config.base_dir / ".env"

    def _rebuild_components(self) -> None:
        if self._bot_poller is not None:
            self._bot_poller.stop()
            self._bot_poller = None

        self.config.ensure_directories()
        self.db = DatabaseManager(self.config.db_path)
        self.repository = MonitorRepository(self.db, tz_name=self.config.timezone_name)
        self.client = SamadClient(config=self.config)
        self.notification_manager = NotificationManager.from_config(
            self.config, repository=self.repository
        )
        self.monitor = SamadFoodMonitor(
            config=self.config,
            repository=self.repository,
            client=self.client,
            notification_manager=self.notification_manager,
        )

        env_p = self._resolve_target_env_path()
        self._last_env_mtime = env_p.stat().st_mtime if env_p.is_file() else None

        tg_ch = self.notification_manager.get_telegram_channel()
        if tg_ch and self.config.telegram_enabled and tg_ch.has_bot_token:
            self._bot_poller = TelegramBotPoller(
                channel=tg_ch,
                auto_subscribe_getter=lambda: self.config.telegram_auto_subscribe,
                invite_code_getter=lambda: self.config.telegram_invite_code,
            )
            self._bot_poller.start()

    def shutdown(self) -> None:
        self.stop_background_scheduler()
        if self._bot_poller is not None:
            self._bot_poller.stop()
            self._bot_poller = None

    @property
    def is_scheduler_running(self) -> bool:
        t = self._scheduler_thread
        return bool(t is not None and t.is_alive())

    # ------------------------------------------------------------------
    # وضعیت زنده کل داشبورد
    # ------------------------------------------------------------------
    def get_dashboard_state(self, week_start_query: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            tehran_now = now_tehran(tz_name=self.config.timezone_name)
            prev_week = get_previous_week_range(tehran_now, tz_name=self.config.timezone_name)
            curr_week = get_current_week_range(tehran_now, tz_name=self.config.timezone_name)
            next_week = get_next_week_range(tehran_now, tz_name=self.config.timezone_name)

            summary = self.repository.get_status_summary(next_week.start_iso)
            activity = self.repository.get_recent_activity(limit=15)
            session_data = self.client.session_manager.load()

            # اگر کاربر از لیست هفته‌های ذخیره‌شده هفته دیگری را انتخاب کرده باشد
            selected_week = next_week
            selected_menu: Optional[Menu] = summary["next_week_menu"]
            if week_start_query and week_start_query != next_week.start_iso:
                parsed_ws = parse_iso_date(week_start_query)
                if parsed_ws:
                    selected_week = WeekRange(
                        week_start=parsed_ws,
                        week_end=parsed_ws + timedelta(days=6),
                    )
                    selected_menu = self.repository.get_menu_by_week(parsed_ws.isoformat())

            meals_list = selected_menu.meals if selected_menu else []
            week_days = _serialize_week_days(selected_week, meals_list)

            # تعیین وضعیت سشن برای نمایش بصری
            if not session_data.has_credentials:
                if summary["session_status"] == "EXPIRED" or summary["paused_for_relogin"]:
                    sess_code = "EXPIRED"
                    sess_label = "منقضی شده — نیازمند ورود مجدد"
                else:
                    sess_code = "NOT_LOGGED_IN"
                    sess_label = "وارد نشده — ابتدا دکمه «ورود به سماد» را بزنید"
            elif summary["session_status"] == "EXPIRED":
                sess_code = "EXPIRED"
                sess_label = "منقضی شده — نیازمند ورود مجدد"
            else:
                sess_code = "ACTIVE"
                sess_label = (
                    "آماده تمدید خودکار با Refresh Token"
                    if session_data.is_access_token_expired
                    else "فعال و معتبر"
                )

            last_check = summary["last_check"]
            last_ok = summary["last_successful_check"]
            last_err = summary["last_error"]
            channels_desc = self.notification_manager.describe_channels()
            active_ch = set(self.notification_manager.active_channel_names())

            next_sched_raw = summary["next_scheduled_check_at"]
            next_sched_fa = _format_iso_fa(next_sched_raw) if next_sched_raw else "غیرفعال"

            return {
                "university": {
                    "key": self.config.samad_university_key,
                    "label": self.config.samad_university_label,
                    "reserve_url": self.config.samad_reserve_url,
                    "api_url": self.config.samad_university_api_url,
                },
                "clock": {
                    "now_iso": tehran_now.isoformat(),
                    "now_jalali": format_jalali_full(tehran_now.date(), include_weekday=True),
                    "now_time_fa": to_persian_digits(tehran_now.strftime("%H:%M:%S")),
                    "timezone": self.config.timezone_name,
                    "is_peak_day": is_peak_check_day(tehran_now, tz_name=self.config.timezone_name),
                },
                "weeks": {
                    "previous": {
                        "start_iso": prev_week.start_iso,
                        "end_iso": prev_week.end_iso,
                        "jalali_range": prev_week.display_jalali_range(),
                        "bilingual": prev_week.display_bilingual(),
                    },
                    "current": {
                        "start_iso": curr_week.start_iso,
                        "end_iso": curr_week.end_iso,
                        "jalali_range": curr_week.display_jalali_range(),
                        "bilingual": curr_week.display_bilingual(),
                    },
                    "next": {
                        "start_iso": next_week.start_iso,
                        "end_iso": next_week.end_iso,
                        "jalali_range": next_week.display_jalali_range(),
                        "bilingual": next_week.display_bilingual(),
                    },
                    "selected": {
                        "start_iso": selected_week.start_iso,
                        "end_iso": selected_week.end_iso,
                        "jalali_range": selected_week.display_jalali_range(),
                        "bilingual": selected_week.display_bilingual(),
                        "is_next_week": selected_week.start_iso == next_week.start_iso,
                    },
                    "saved_weeks": activity["saved_weeks"],
                },
                "session": {
                    "code": sess_code,
                    "label": sess_label,
                    "username": session_data.username or summary["last_username"] or "",
                    "university_label": session_data.university_label or self.config.samad_university_label,
                    "paused_for_relogin": summary["paused_for_relogin"],
                },
                "scheduler": {
                    "running_in_panel": self.is_scheduler_running,
                    "is_checking_now": self._is_checking_now,
                    "is_logging_in": self._is_logging_in,
                    "normal_interval_minutes": self.config.check_interval_normal_minutes,
                    "peak_interval_min_minutes": self.config.peak_interval_min_minutes,
                    "peak_interval_max_minutes": self.config.peak_interval_max_minutes,
                    "next_scheduled_check_fa": next_sched_fa,
                    "next_scheduled_check_raw": next_sched_raw,
                },
                "stats": {
                    "total_checks": summary["total_checks"],
                    "total_sent_events": summary["total_sent_events"],
                    "last_check": (
                        {
                            **last_check,
                            "checked_at_fa": _format_iso_fa(last_check["checked_at"]),
                        }
                        if last_check
                        else None
                    ),
                    "last_successful_check_fa": (
                        _format_iso_fa(last_ok["checked_at"]) if last_ok else "هنوز ثبت نشده"
                    ),
                    "last_error": (
                        {
                            **last_err,
                            "logged_at_fa": _format_iso_fa(last_err["logged_at"]),
                        }
                        if last_err
                        else None
                    ),
                },
                "channels": [
                    {
                        "name": name,
                        "active": name in active_ch,
                        "description": desc,
                    }
                    for name, desc in channels_desc.items()
                ],
                "subscribers_summary": {
                    "enabled_count": len(self.repository.list_telegram_subscribers(only_enabled=True)),
                    "total_count": len(self.repository.list_telegram_subscribers(only_enabled=False)),
                },
                "menu": {
                    "published": bool(selected_menu and selected_menu.meals),
                    "is_demo": False,
                    "status": selected_menu.status if selected_menu else "NOT_PUBLISHED",
                    "detected_at_fa": (
                        _format_iso_fa(selected_menu.detected_at) if selected_menu else None
                    ),
                    "updated_at_fa": (
                        _format_iso_fa(selected_menu.updated_at) if selected_menu else None
                    ),
                    "total_meals": len(meals_list),
                    "restaurants": sorted({m.restaurant for m in meals_list}),
                    "days": week_days,
                },
            }

    # ------------------------------------------------------------------
    # پیش‌نمایش نمونه (دمو) بدون تغییر در دیتابیس واقعی
    # ------------------------------------------------------------------
    def get_demo_preview_menu(self) -> Dict[str, Any]:
        """ساخت یک پیش‌نمایش نمونه از منوی هفتگی دانشگاه شهید بهشتی صرفاً در حافظه جهت آشنایی کاربر با ظاهر پنل."""
        tehran_now = now_tehran(tz_name=self.config.timezone_name)
        nw = get_next_week_range(tehran_now, tz_name=self.config.timezone_name)
        dates = nw.all_dates()

        sample_specs = [
            (0, "صبحانه", 6, "پنیر سفید، گردو و نان سنگک", "", 45000),
            (0, "ناهار", 7, "چلو کباب کوبیده مخصوص", "دوغ، ماست موسیر", 150000),
            (0, "شام", 8, "خوراک شنیسل مرغ", "گوجه، خیارشور", 120000),
            (1, "ناهار", 7, "زرشک پلو با مرغ زعفرانی", "ماست", 145000),
            (1, "شام", 8, "کوکو سبزی با نان تازه", "گوجه فرنگی", 95000),
            (2, "ناهار", 7, "چلو خورشت قورمه‌سبزی", "سالاد فصل", 135000),
            (2, "شام", 8, "ماکارونی فرمی با گوشت", "ماست", 110000),
            (3, "ناهار", 7, "چلو جوجه کباب با استخوان", "دوغ", 155000),
            (3, "شام", 8, "خوراک کباب تابه‌ای", "خیارشور", 125000),
            (4, "ناهار", 7, "چلو خورشت قیمه سیب‌زمینی", "ماست", 130000),
            (4, "شام", 8, "عدسی با نان بربری", "لیمو ترش", 80000),
            (5, "ناهار", 7, "سبزی پلو با ماهی قزل‌آلا", "ترشی", 160000),
        ]

        demo_meals: List[Meal] = []
        for day_idx, m_type, m_id, f_name, side, price in sample_specs:
            if day_idx < len(dates):
                d = dates[day_idx]
                is_dorm_sample = m_type in {"صبحانه", "شام"}
                rest_name = (
                    "سلف خوابگاه کوی پسران (پیش‌نمایش آزمایشی)"
                    if is_dorm_sample
                    else "سلف مرکزی ولنجک (پیش‌نمایش آزمایشی)"
                )
                demo_meals.append(
                    Meal(
                        date=d.isoformat(),
                        day_name=get_persian_weekday(d),
                        meal_type=m_type,
                        meal_type_id=m_id,
                        restaurant=rest_name,
                        self_id=102 if is_dorm_sample else 101,
                        is_free_restaurant=False,
                        food_name=f_name,
                        availability="OPEN",
                        side_dishes=side,
                        price_rials=price,
                        quota_limit="حداکثر ۱ پرس",
                    )
                )

        return {
            "published": True,
            "is_demo": True,
            "status": "DEMO_PREVIEW",
            "week_bilingual": nw.display_bilingual(),
            "total_meals": len(demo_meals),
            "restaurants": [
                "سلف خوابگاه کوی پسران (پیش‌نمایش آزمایشی)",
                "سلف مرکزی ولنجک (پیش‌نمایش آزمایشی)",
            ],
            "days": _serialize_week_days(nw, demo_meals),
        }

    def trigger_fetch_week(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """واکشی درخواستی منوی هفته قبل (`prev`) یا هفته جاری (`curr`) از سماد برای همه سلف‌ها (خوابگاه و مرکزی)."""
        week_key = str(payload.get("week_key") or "prev").strip().lower()
        force = bool(payload.get("force_refresh", False))
        try:
            wk_range, menu, status, msg = self.monitor.fetch_week_on_demand(
                week_key=week_key, force_refresh=force
            )
            ok = status == CheckStatus.MENU_PUBLISHED and bool(menu and menu.meals)
            return {
                "ok": ok,
                "status": status.value,
                "week_start": wk_range.start_iso,
                "meals_count": len(menu.meals) if menu else 0,
                "message": (
                    f"منوی {('هفته قبل' if week_key == 'prev' else 'هفته جاری')} ({len(menu.meals)} گزینه غذا در سلف خوابگاه/مرکزی) آماده شد!"
                    if ok and menu
                    else msg
                ),
            }
        except Exception as exc:
            logger.exception("خطا در واکشی درخواستی هفته %s", week_key)
            return {"ok": False, "status": "ERROR", "message": str(exc)}

    # ------------------------------------------------------------------
    # عملیات‌های اجرایی یک‌کلیکی
    # ------------------------------------------------------------------
    def trigger_check_now(self) -> Dict[str, Any]:
        if not self._action_lock.acquire(blocking=False):
            return {
                "ok": False,
                "message": "یک عملیات بررسی در همین لحظه در حال اجراست؛ لطفاً چند ثانیه صبر کنید.",
            }
        self._is_checking_now = True
        try:
            summary = self.monitor.check_once()
            ok = summary.status in {CheckStatus.MENU_PUBLISHED, CheckStatus.MENU_NOT_PUBLISHED}
            return {
                "ok": ok,
                "status": summary.status.value,
                "meals_count": summary.meals_count,
                "event_type": summary.event_type.value if summary.event_type else None,
                "notification_sent": summary.notification_sent,
                "duration_ms": summary.duration_ms,
                "message": summary.message,
            }
        except Exception as exc:
            logger.exception("خطا هنگام بررسی فوری از داشبورد")
            return {"ok": False, "status": "ERROR", "message": str(exc)}
        finally:
            self._is_checking_now = False
            self._action_lock.release()

    def trigger_interactive_login(self) -> Dict[str, Any]:
        if self._is_logging_in:
            return {
                "ok": False,
                "message": "پنجره مرورگر ورود هم‌اکنون باز است؛ لطفاً ورود را در مرورگر کامل کنید.",
            }

        self._is_logging_in = True
        try:
            session_data = self.client.interactive_login()
            with self._lock:
                self.repository.set_runtime_value("session_status", "ACTIVE")
                self.repository.set_runtime_value("paused_for_relogin", "false")
                self.repository.set_runtime_value("active_session_fp", session_data.fingerprint)
                if session_data.username:
                    self.repository.set_runtime_value("last_username", session_data.username)
            return {
                "ok": True,
                "message": f"ورود با موفقیت انجام شد و نشست ({session_data.university_label}) ذخیره گردید!",
                "username": session_data.username or "",
            }
        except Exception as exc:
            return {
                "ok": False,
                "message": str(exc),
            }
        finally:
            self._is_logging_in = False

    def trigger_test_notify(self) -> Dict[str, Any]:
        with self._lock:
            active = self.notification_manager.active_channel_names()
            if not active:
                return {
                    "ok": False,
                    "message": "هیچ کانال اعلانی فعال نیست! ابتدا در تب «تنظیمات» تلگرام یا اعلان ویندوز ۱۱ را فعال کنید.",
                }

            tehran_now = now_tehran(tz_name=self.config.timezone_name)
            nw = get_next_week_range(tehran_now, tz_name=self.config.timezone_name)
            test_msg = NotificationMessage(
                event_id=f"UI_TEST:{int(tehran_now.timestamp())}",
                event_type=EventType.MENU_AVAILABLE,
                title="🧪 پیام آزمایشی پنل مدیریت سماد",
                body=(
                    "🧪 تست موفق سامانه پایش غذای سماد\n"
                    f"🏛 {self.config.samad_university_label}\n"
                    f"🗓 هفته هدف فعلی: {nw.display_bilingual()}\n"
                    "────────────────────\n"
                    "اتصال کانال اعلان شما کاملاً برقرار و آماده اطلاع‌رسانی منوی هفته آینده است.\n"
                    f"🔗 {self.config.samad_reserve_url}"
                ),
                short_summary="تست اعلان از پنل گرافیکی Samad Food Monitor با موفقیت انجام شد.",
                action_url=self.config.samad_reserve_url,
            )
            report = self.notification_manager.dispatch(test_msg)

        if report.failed:
            errs = " | ".join(f"{ch}: {err}" for ch, err in report.failed.items())
            return {"ok": False, "message": f"خطا در ارسال اعلان: {errs}"}

        return {
            "ok": True,
            "message": f"پیام آزمایشی با موفقیت به کانال‌های ({', '.join(report.succeeded)}) ارسال شد!",
        }

    def run_doctor_checks(self) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []
        tehran_now = now_tehran(tz_name=self.config.timezone_name)

        checks.append(
            {
                "title": "نسخه پایتون و سیستم‌عامل",
                "ok": True,
                "detail": f"Python {sys.version.split()[0]} ({sys.platform})",
            }
        )
        checks.append(
            {
                "title": "منطقه زمانی مرجع تهران",
                "ok": True,
                "detail": f"{self.config.timezone_name} — {tehran_now.strftime('%Y-%m-%d %H:%M:%S %z')}",
            }
        )

        forbidden = self.config.detect_forbidden_credential_vars()
        checks.append(
            {
                "title": "امنیت عدم ذخیره رمز عبور سماد",
                "ok": len(forbidden) == 0,
                "detail": (
                    "هیچ رمز عبور دانشجویی در تنظیمات ذخیره نشده است."
                    if not forbidden
                    else f"کلیدهای غیرمجاز یافت شد: {', '.join(forbidden)}"
                ),
            }
        )

        pw_ok = False
        pw_detail = ""
        try:
            from playwright.sync_api import sync_playwright
            from samad_monitor.browser.client import resolve_browser_launch_options

            with sync_playwright() as p:
                b_opts, b_label = resolve_browser_launch_options(p)
                pw_ok = b_opts is not None
                pw_detail = (
                    f"نصب و آماده اجرا ({b_label})"
                    if pw_ok
                    else "هیچ مرورگری یافت نشد (Chrome یا Edge نصب کنید)"
                )
        except Exception as exc:
            pw_detail = f"خطا در بررسی Playwright: {exc}"

        checks.append({"title": "موتور مرورگر (Chromium / Chrome / Edge)", "ok": pw_ok, "detail": pw_detail})

        db_ok = False
        try:
            with self.db.transaction() as conn:
                conn.execute("SELECT 1").fetchone()
            db_ok = True
            db_detail = f"سالم و آماده ({self.config.db_path.name})"
        except Exception as exc:
            db_detail = str(exc)
        checks.append({"title": "دیتابیس پایدار SQLite", "ok": db_ok, "detail": db_detail})

        sess = self.client.session_manager.load()
        checks.append(
            {
                "title": "نشست ذخیره‌شده سماد",
                "ok": sess.has_credentials,
                "detail": (
                    f"موجود ({sess.university_label})"
                    if sess.has_credentials
                    else "هنوز وارد نشده‌اید (روی دکمه «ورود به سماد» کلیک کنید)"
                ),
            }
        )

        active_ch = self.notification_manager.active_channel_names()
        checks.append(
            {
                "title": "کانال‌های اعلان فعال",
                "ok": len(active_ch) > 0,
                "detail": (
                    f"کانال‌های آماده: {', '.join(active_ch)}"
                    if active_ch
                    else "هیچ کانال اعلانی فعال نیست (در تب تنظیمات تلگرام یا ویندوز را فعال کنید)"
                ),
            }
        )

        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    # ------------------------------------------------------------------
    # شروع / توقف زمان‌بند خودکار درون پنل
    # ------------------------------------------------------------------
    def start_background_scheduler(self) -> Dict[str, Any]:
        with self._lock:
            if self.is_scheduler_running:
                return {"ok": True, "message": "پایش خودکار هم‌اکنون در حال اجراست."}

            sched = AdaptiveScheduler(
                config=self.config, monitor=self.monitor, enable_bot_poller=False
            )
            # غیرفعال کردن ثبت signal در ترد پس‌زمینه وب
            sched._install_signal_handlers = lambda: None  # type: ignore[method-assign]
            thread = threading.Thread(
                target=sched.run_forever,
                name="samad-ui-scheduler",
                daemon=True,
            )
            self._scheduler = sched
            self._scheduler_thread = thread
            thread.start()
            return {
                "ok": True,
                "message": "پایش خودکار پس‌زمینه روشن شد! اولین بررسی همین الان آغاز گردید.",
            }

    def stop_background_scheduler(self) -> Dict[str, Any]:
        with self._lock:
            if not self.is_scheduler_running or self._scheduler is None:
                return {"ok": True, "message": "پایش خودکار در حال حاضر متوقف است."}

            self._scheduler.stop()
            self.repository.set_runtime_value("next_scheduled_check_at", "سرویس متوقف است")
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=3.0)
        return {"ok": True, "message": "پایش خودکار پس‌زمینه متوقف شد."}

    # ------------------------------------------------------------------
    # خواندن و ذخیره امن تنظیمات (.env) بدون افشای توکن‌ها
    # ------------------------------------------------------------------
    def _maybe_reload_env_if_changed(self) -> None:
        """اگر کاربر فایل .env را دستی روی دیسک تغییر داده باشد، بدون نیاز به ری‌استارت بازخوانی شود."""
        env_p = self._resolve_target_env_path()
        cur_mtime = env_p.stat().st_mtime if env_p.is_file() else None
        if cur_mtime is not None and cur_mtime != self._last_env_mtime:
            self.config = AppConfig.load(env_path=env_p, base_dir=self.config.base_dir)
            self._rebuild_components()

    def get_safe_config(self) -> Dict[str, Any]:
        with self._lock:
            self._maybe_reload_env_if_changed()
            has_token = bool(self.config.telegram_bot_token)
            masked_hint = ""
            if has_token:
                tok = self.config.telegram_bot_token
                masked_hint = f"{tok[:5]}••••••••••••" if len(tok) > 8 else "••••••••"

            return {
                "env_path": str(self._resolve_target_env_path()),
                "samad_fetch_strategy": self.config.samad_fetch_strategy,
                "samad_self_ids": ",".join(str(i) for i in self.config.samad_self_ids),
                "samad_self_name_filter": ",".join(self.config.samad_self_name_filter),
                "samad_ignore_env_proxy": self.config.samad_ignore_env_proxy,
                "samad_proxy_url": self.config.samad_proxy_url or "",
                "check_interval_normal_minutes": self.config.check_interval_normal_minutes,
                "check_interval_peak_minutes": self.config.check_interval_peak_minutes,
                "check_jitter_seconds": self.config.check_jitter_seconds,
                "telegram_enabled": self.config.telegram_enabled,
                "has_telegram_bot_token": has_token,
                "telegram_bot_token_masked": masked_hint,
                "telegram_chat_id": ",".join(self.config.telegram_chat_ids),
                "telegram_api_base_url": self.config.telegram_api_base_url,
                "telegram_proxy_url": self.config.telegram_proxy_url or "",
                "telegram_auto_subscribe": self.config.telegram_auto_subscribe,
                "telegram_invite_code": self.config.telegram_invite_code,
                "windows_notification_enabled": self.config.windows_notification_enabled,
                "windows_notification_sound": self.config.windows_notification_sound,
                "log_level": self.config.log_level,
            }

    def update_safe_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        was_running = self.is_scheduler_running
        if was_running:
            self.stop_background_scheduler()

        with self._lock:
            env_path = self._resolve_target_env_path()
            template_path = self.config.base_dir / ".env.example"

            normal_min = max(5, int(payload.get("check_interval_normal_minutes", 60)))
            peak_min = max(2, int(payload.get("check_interval_peak_minutes", 12)))
            jitter_sec = max(0, int(payload.get("check_jitter_seconds", 45)))

            strategy = str(payload.get("samad_fetch_strategy", "auto")).strip().lower()
            if strategy not in {"auto", "api", "browser"}:
                strategy = "auto"

            log_level = str(payload.get("log_level", "INFO")).strip().upper()
            if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
                log_level = "INFO"

            updates: Dict[str, str] = {
                "SAMAD_FETCH_STRATEGY": strategy,
                "SAMAD_SELF_IDS": str(payload.get("samad_self_ids", "")).strip(),
                "SAMAD_SELF_NAME_FILTER": str(payload.get("samad_self_name_filter", "")).strip(),
                "SAMAD_IGNORE_ENV_PROXY": (
                    "true" if bool(payload.get("samad_ignore_env_proxy", True)) else "false"
                ),
                "SAMAD_PROXY_URL": str(payload.get("samad_proxy_url", "")).strip(),
                "CHECK_INTERVAL_NORMAL_MINUTES": str(normal_min),
                "CHECK_INTERVAL_PEAK_MINUTES": str(peak_min),
                "CHECK_JITTER_SECONDS": str(jitter_sec),
                "TELEGRAM_ENABLED": (
                    "true" if bool(payload.get("telegram_enabled", False)) else "false"
                ),
                "TELEGRAM_CHAT_ID": str(payload.get("telegram_chat_id", "")).strip(),
                "TELEGRAM_API_BASE_URL": (
                    str(payload.get("telegram_api_base_url", "https://api.telegram.org")).strip()
                    or "https://api.telegram.org"
                ),
                "TELEGRAM_PROXY_URL": str(payload.get("telegram_proxy_url", "")).strip(),
                "TELEGRAM_AUTO_SUBSCRIBE": (
                    "true"
                    if bool(payload.get("telegram_auto_subscribe", self.config.telegram_auto_subscribe))
                    else "false"
                ),
                "TELEGRAM_INVITE_CODE": str(
                    payload.get("telegram_invite_code", self.config.telegram_invite_code)
                ).strip(),
                "WINDOWS_NOTIFICATION_ENABLED": (
                    "true" if bool(payload.get("windows_notification_enabled", True)) else "false"
                ),
                "WINDOWS_NOTIFICATION_SOUND": (
                    "true" if bool(payload.get("windows_notification_sound", True)) else "false"
                ),
                "LOG_LEVEL": log_level,
            }

            # توکن ربات فقط در صورتی بازنویسی می‌شود که کاربر توکن جدیدی وارد کرده باشد (یا تیک پاک‌سازی زده باشد)
            new_bot_token = str(payload.get("telegram_bot_token", "")).strip()
            if new_bot_token:
                updates["TELEGRAM_BOT_TOKEN"] = new_bot_token
            elif payload.get("clear_telegram_bot_token") is True:
                updates["TELEGRAM_BOT_TOKEN"] = ""

            update_env_file(env_path=env_path, updates=updates, template_path=template_path)
            self.config = AppConfig.load(env_path=env_path, base_dir=self.config.base_dir)
            self._rebuild_components()

        if was_running:
            self.start_background_scheduler()

        return {
            "ok": True,
            "message": "تنظیمات با موفقیت در فایل .env ذخیره و بدون نیاز به ری‌استارت اعمال شد!",
            "config": self.get_safe_config(),
        }

    # ------------------------------------------------------------------
    # تاریخچه بررسی‌ها و آخرین خطوط لاگ سانسورشده
    # ------------------------------------------------------------------
    def get_history_and_logs(self, limit: int = 30, log_lines: int = 80) -> Dict[str, Any]:
        with self._lock:
            activity = self.repository.get_recent_activity(limit=limit)
            for row in activity["checks"]:
                row["checked_at_fa"] = _format_iso_fa(row.get("checked_at"))
            for row in activity["events"]:
                row["updated_at_fa"] = _format_iso_fa(row.get("updated_at"))
            for row in activity["errors"]:
                row["logged_at_fa"] = _format_iso_fa(row.get("logged_at"))

            log_file = self.config.logs_dir / "samad_monitor.log"
            tail_text = ""
            if log_file.is_file():
                try:
                    all_lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                    tail_text = redact_secrets("\n".join(all_lines[-max(10, min(log_lines, 300)) :]))
                except Exception as exc:
                    tail_text = f"خطا در خواندن فایل لاگ: {exc}"

            return {
                **activity,
                "log_tail": tail_text,
            }

    def detect_local_vpn_proxy(self) -> Dict[str, Any]:
        """تشخیص خودکار پورت فیلترشکن فعال روی ویندوز ۱۱ یا پورت‌های رایج لوکال (Hiddify / v2rayN / Nekoray / Clash)."""
        # ۱. بررسی تنظیمات System Proxy ویندوز در رجیستری
        if sys.platform == "win32":
            try:
                import winreg  # type: ignore

                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                ) as key:
                    proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                    proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    if proxy_enable and proxy_server:
                        raw_srv = str(proxy_server).strip()
                        if "=" in raw_srv:
                            # فرمت http=127.0.0.1:10809;https=127.0.0.1:10809
                            for part in raw_srv.split(";"):
                                if part.startswith("https=") or part.startswith("http="):
                                    raw_srv = part.split("=", 1)[1]
                                    break
                        if not raw_srv.startswith(("http://", "https://", "socks5://")):
                            raw_srv = f"http://{raw_srv}"
                        return {
                            "ok": True,
                            "proxy_url": raw_srv,
                            "source": "تنظیمات پروکسی فعال ویندوز ۱۱",
                            "message": f"پورت فعال فیلترشکن از تنظیمات ویندوز شناسایی شد: {raw_srv}",
                        }
            except Exception:
                pass

        # ۲. اسکن سریع پورت‌های استاندارد کلاینت‌های رایج در ایران روی 127.0.0.1
        common_ports = [
            (12334, "http", "Hiddify"),
            (10808, "socks5", "v2rayN (SOCKS5)"),
            (10809, "http", "v2rayN (HTTP)"),
            (2080, "socks5", "Nekoray / Throne"),
            (2081, "http", "Nekoray (HTTP)"),
            (7890, "http", "Clash / Mihomo"),
            (1080, "socks5", "SOCKS5 محلی"),
        ]
        for port, scheme, app_name in common_ports:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                    url = f"{scheme}://127.0.0.1:{port}"
                    return {
                        "ok": True,
                        "proxy_url": url,
                        "source": app_name,
                        "message": f"پورت فعال {app_name} شناسایی شد: {url}",
                    }
            except OSError:
                continue

        return {
            "ok": False,
            "proxy_url": "",
            "message": "هیچ پورت فیلترشکن فعالی روی سیستم شناسایی نشد. ابتدا برنامه فیلترشکن خود را روشن کنید.",
        }

    # ------------------------------------------------------------------
    # مدیریت مشترکین و گیرندگان ربات تلگرام (افزودن دوستان، لینک دعوت، کشف خودکار)
    # ------------------------------------------------------------------
    def _build_invite_link(self, bot_username: str) -> str:
        uname = (bot_username or "").strip().lstrip("@")
        if not uname:
            return ""
        code = (self.config.telegram_invite_code or "").strip()
        if code:
            return f"https://t.me/{uname}?start={code}"
        return f"https://t.me/{uname}"

    def get_subscribers_state(self) -> Dict[str, Any]:
        with self._lock:
            self._maybe_reload_env_if_changed()
            bot_username = self.repository.get_runtime_value("telegram_bot_username") or ""
            bot_first_name = self.repository.get_runtime_value("telegram_bot_first_name") or ""
            tg_ch = self.notification_manager.get_telegram_channel()
            need_auto_probe = bool(
                self.config.telegram_bot_token
                and not bot_username
                and tg_ch is not None
                and self._profile_probed_token != self.config.telegram_bot_token
            )
            if need_auto_probe:
                self._profile_probed_token = self.config.telegram_bot_token

        if need_auto_probe and tg_ch is not None:
            prof = tg_ch.get_bot_profile(timeout_seconds=3.5)
            if prof.get("ok"):
                with self._lock:
                    if prof.get("username"):
                        bot_username = str(prof["username"])
                        self.repository.set_runtime_value("telegram_bot_username", bot_username)
                    if prof.get("first_name"):
                        bot_first_name = str(prof["first_name"])
                        self.repository.set_runtime_value("telegram_bot_first_name", bot_first_name)

        with self._lock:
            subs = self.repository.list_telegram_subscribers(only_enabled=False)
            for s in subs:
                s["created_at_fa"] = _format_iso_fa(s.get("created_at"))
                s["updated_at_fa"] = _format_iso_fa(s.get("updated_at"))

            contacts = self.repository.list_recent_telegram_contacts(limit=50)
            for c in contacts:
                c["last_seen_fa"] = _format_iso_fa(c.get("last_seen_at"))

            return {
                "ok": True,
                "telegram_enabled": self.config.telegram_enabled,
                "has_bot_token": bool(self.config.telegram_bot_token),
                "bot_username": bot_username,
                "bot_first_name": bot_first_name,
                "invite_link": self._build_invite_link(bot_username),
                "auto_subscribe": self.config.telegram_auto_subscribe,
                "invite_code": self.config.telegram_invite_code,
                "subscribers": subs,
                "recent_contacts": contacts,
            }

    def sync_bot_contacts_now(self) -> Dict[str, Any]:
        """استعلام نام ربات + خواندن پیام‌ها/استارت‌های جدید از تلگرام برای افزودن تک‌کلیکی دوستان."""
        with self._lock:
            tg_ch = self.notification_manager.get_telegram_channel()
            if tg_ch is None or not tg_ch.has_bot_token:
                return {
                    "ok": False,
                    "message": "ابتدا توکن ربات تلگرام (TELEGRAM_BOT_TOKEN) را در تب تنظیمات ذخیره کنید.",
                }

            prof = tg_ch.get_bot_profile()
            if prof.get("ok"):
                if prof.get("username"):
                    self.repository.set_runtime_value("telegram_bot_username", str(prof["username"]))
                if prof.get("first_name"):
                    self.repository.set_runtime_value("telegram_bot_first_name", str(prof["first_name"]))

            sync_res = tg_ch.sync_bot_updates(
                auto_subscribe=self.config.telegram_auto_subscribe,
                invite_code=self.config.telegram_invite_code,
                handle_commands=True,
            )

        state = self.get_subscribers_state()
        if not sync_res.get("ok"):
            return {
                **state,
                "ok": False,
                "message": sync_res.get("message") or prof.get("message") or "خطا در ارتباط با تلگرام.",
            }

        extra = ""
        if sync_res.get("auto_added"):
            extra = f" ({to_persian_digits(sync_res['auto_added'])} نفر خودکار عضو شدند!)"
        return {
            **state,
            "ok": True,
            "message": f"ارتباط با تلگرام برقرار شد؛ {sync_res.get('message', '')}{extra}",
        }

    def add_subscriber(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(payload.get("chat_id") or "").strip()
        if not cid:
            return {"ok": False, "message": "لطفاً شناسه عددی چت (Chat ID) یا آیدی کانال (@...) را وارد کنید."}

        display_name = str(payload.get("display_name") or "").strip()
        username = str(payload.get("username") or "").strip().lstrip("@")
        chat_type = str(payload.get("chat_type") or "").strip() or (
            "group" if cid.startswith("-") else ("channel" if cid.startswith("@") else "private")
        )
        role = "admin" if str(payload.get("role") or "").strip().lower() == "admin" else "subscriber"
        send_welcome = bool(payload.get("send_welcome", True))

        with self._lock:
            sub = self.repository.upsert_telegram_subscriber(
                chat_id=cid,
                display_name=display_name,
                username=username,
                chat_type=chat_type,
                role=role,
                enabled=True,
                added_via=str(payload.get("added_via") or "manual"),
            )

            # اگر توکن ست شده ولی تلگرام خاموش بوده، خودکار در .env روشنش کن
            if self.config.telegram_bot_token and not self.config.telegram_enabled:
                env_path = self._resolve_target_env_path()
                update_env_file(
                    env_path=env_path,
                    updates={"TELEGRAM_ENABLED": "true"},
                    template_path=self.config.base_dir / ".env.example",
                )
                self.config = AppConfig.load(env_path=env_path, base_dir=self.config.base_dir)
                self._rebuild_components()

            tg_ch = self.notification_manager.get_telegram_channel()

        welcome_note = ""
        if send_welcome and tg_ch and tg_ch.has_bot_token:
            try:
                tg_ch.send_text_to_chat(
                    target_chat=cid,
                    text=(
                        f"🎉 سلام {sub['display_name']} عزیز!\n"
                        f"شما به لیست دریافت‌کنندگان اعلان منوی سلف {self.config.samad_university_label} اضافه شدید.\n"
                        "به محض انتشار یا تغییر منوی هفته آینده، برنامه غذایی برایتان ارسال می‌شود.\n\n"
                        "🍽 ارسال دستور /menu : مشاهده آخرین منوی ذخیره‌شده\n"
                        "🔕 ارسال دستور /stop : لغو دریافت اعلان"
                    ),
                )
                welcome_note = " و پیام خوش‌آمدگویی در تلگرام برایش ارسال شد!"
            except Exception:
                welcome_note = (
                    " (توجه: پیام خوش‌آمدگویی ارسال نشد؛ به دوستتان بگویید یک بار وارد ربات شما شود و دکمه Start را بزند تا تلگرام اجازه ارسال پیام بدهد.)"
                )

        return {
            **self.get_subscribers_state(),
            "ok": True,
            "message": f"«{sub['display_name']}» با موفقیت به لیست گیرندگان اضافه شد{welcome_note}",
        }

    def update_subscriber(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(payload.get("chat_id") or "").strip()
        if not cid:
            return {"ok": False, "message": "شناسه گیرنده مشخص نشده است."}

        with self._lock:
            updated = self.repository.update_telegram_subscriber(
                chat_id=cid,
                display_name=payload.get("display_name"),
                role=payload.get("role"),
                enabled=payload.get("enabled"),
            )
        if updated is None:
            return {"ok": False, "message": "گیرنده مورد نظر یافت نشد."}

        return {
            **self.get_subscribers_state(),
            "ok": True,
            "message": f"وضعیت «{updated['display_name']}» به‌روزرسانی شد.",
        }

    def delete_subscriber(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(payload.get("chat_id") or "").strip()
        if not cid:
            return {"ok": False, "message": "شناسه گیرنده مشخص نشده است."}

        with self._lock:
            deleted = self.repository.delete_telegram_subscriber(cid)
            # اگر این شناسه در TELEGRAM_CHAT_ID فایل .env هم بود حذف شود تا دوباره برنگردد
            if cid in self.config.telegram_chat_ids:
                remaining = [x for x in self.config.telegram_chat_ids if x != cid]
                env_path = self._resolve_target_env_path()
                update_env_file(
                    env_path=env_path,
                    updates={"TELEGRAM_CHAT_ID": ",".join(remaining)},
                    template_path=self.config.base_dir / ".env.example",
                )
                self.config = AppConfig.load(env_path=env_path, base_dir=self.config.base_dir)
                self._rebuild_components()

        if not deleted:
            return {"ok": False, "message": "گیرنده مورد نظر یافت نشد."}

        return {
            **self.get_subscribers_state(),
            "ok": True,
            "message": f"گیرنده ({cid}) از لیست حذف شد.",
        }

    def test_single_subscriber(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(payload.get("chat_id") or "").strip()
        if not cid:
            return {"ok": False, "message": "شناسه گیرنده مشخص نشده است."}

        with self._lock:
            tg_ch = self.notification_manager.get_telegram_channel()
            sub = self.repository.get_telegram_subscriber(cid)
            if tg_ch is None or not tg_ch.has_bot_token:
                return {"ok": False, "message": "توکن ربات تلگرام تنظیم نشده است."}
            tehran_now = now_tehran(tz_name=self.config.timezone_name)
            nw = get_next_week_range(tehran_now, tz_name=self.config.timezone_name)

        name = sub["display_name"] if sub else cid
        try:
            tg_ch.send_text_to_chat(
                target_chat=cid,
                text=(
                    f"🧪 پیام تست اختصاصی برای {name}\n"
                    f"🏛 ربات پایش غذای سماد — {self.config.samad_university_label}\n"
                    f"🗓 هفته هدف فعلی: {nw.display_bilingual()}\n"
                    "────────────────────\n"
                    "✅ اتصال تلگرام شما برقرار است و منوی هفته آینده به محض انتشار برایتان ارسال خواهد شد."
                ),
            )
            return {"ok": True, "message": f"پیام تست با موفقیت برای «{name}» ارسال شد!"}
        except Exception as exc:
            return {
                "ok": False,
                "message": f"ارسال به «{name}» ناموفق بود: {exc} (مطمئن شوید این کاربر یک بار ربات شما را Start کرده است.)",
            }

    def update_auto_subscribe_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        auto_sub = bool(payload.get("auto_subscribe", False))
        invite_code = str(payload.get("invite_code") or "").strip()

        with self._lock:
            env_path = self._resolve_target_env_path()
            update_env_file(
                env_path=env_path,
                updates={
                    "TELEGRAM_AUTO_SUBSCRIBE": "true" if auto_sub else "false",
                    "TELEGRAM_INVITE_CODE": invite_code,
                },
                template_path=self.config.base_dir / ".env.example",
            )
            self.config = AppConfig.load(env_path=env_path, base_dir=self.config.base_dir)
            self._rebuild_components()

        return {
            **self.get_subscribers_state(),
            "ok": True,
            "message": (
                "تنظیمات عضویت خودکار ذخیره شد! اکنون دوستان شما با زدن Start در ربات خودکار عضو می‌شوند."
                if auto_sub
                else "عضویت خودکار غیرفعال شد (افزودن فقط با تایید شما از پنل انجام می‌شود)."
            ),
        }


