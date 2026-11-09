"""موتور هماهنگ‌کننده بررسی منوی هفته آینده، تشخیص تغییرات، ثبت پایدار و ارسال اعلان."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from samad_monitor.browser.client import SamadClient
from samad_monitor.config import AppConfig
from samad_monitor.database.repository import MonitorRepository
from samad_monitor.database.sqlite import DatabaseManager
from samad_monitor.detector import (
    MenuDetector,
    build_initial_event_id,
    build_session_expired_event_id,
)
from samad_monitor.models import CheckStatus, EventType, Menu, WeekRange
from samad_monitor.notifications.formatter import (
    format_menu_available_notification,
    format_menu_updated_notification,
    format_session_expired_notification,
)
from samad_monitor.notifications.manager import DeliveryReport, NotificationManager
from samad_monitor.utils.date_utils import (
    get_current_week_range,
    get_next_week_range,
    get_previous_week_range,
    now_tehran,
)

logger = logging.getLogger("samad_monitor.monitor")


@dataclass
class CheckExecutionSummary:
    """نتیجه کامل اجرای یک نوبت بررسی منو."""

    status: CheckStatus
    target_week: WeekRange
    meals_count: int
    event_type: Optional[EventType]
    event_id: Optional[str]
    notification_sent: bool
    delivery_report: Optional[DeliveryReport]
    message: str
    duration_ms: int
    source: str = "api"
    menu: Optional[Menu] = None


class SamadFoodMonitor:
    """هسته اصلی پایش سامانه سماد دانشگاه شهید بهشتی."""

    def __init__(
        self,
        config: AppConfig,
        repository: Optional[MonitorRepository] = None,
        client: Optional[SamadClient] = None,
        notification_manager: Optional[NotificationManager] = None,
        detector: Optional[MenuDetector] = None,
    ) -> None:
        self.config = config
        self.config.ensure_directories()

        if repository is None:
            db = DatabaseManager(config.db_path)
            self.repository = MonitorRepository(db, tz_name=config.timezone_name)
        else:
            self.repository = repository

        self.client = client or SamadClient(config=config)
        self.notification_manager = notification_manager or NotificationManager.from_config(
            config, repository=self.repository
        )
        self.detector = detector or MenuDetector(tz_name=config.timezone_name)
        self._on_demand_lock = threading.Lock()
        # کش فوق‌سریع در حافظه RAM برای پاسخ آنی (<1ms) به ربات تلگرام بدون مراجعه به سماد
        # ساختار: {week_start_iso: (cached_tehran_date_iso, Menu)}
        self._ram_week_cache: dict[str, tuple[str, Menu]] = {}

        tg_ch = self.notification_manager.get_telegram_channel()
        if tg_ch is not None and hasattr(tg_ch, "set_week_menu_provider"):
            tg_ch.set_week_menu_provider(self.fetch_week_on_demand)

        self._preload_ram_cache_from_db()

    def _preload_ram_cache_from_db(self) -> None:
        """بارگذاری اولیه منوهای ذخیره‌شده از SQLite به RAM هنگام بالا آمدن برنامه."""
        try:
            now_dt = now_tehran(tz_name=self.config.timezone_name)
            today_iso = now_dt.date().isoformat()
            for getter in (get_previous_week_range, get_current_week_range, get_next_week_range):
                wk = getter(now_dt, tz_name=self.config.timezone_name)
                m = self.repository.get_menu_by_week(wk.start_iso)
                if m and m.meals:
                    saved_day = (
                        self.repository.get_runtime_value(f"daily_menu_date:{wk.start_iso}")
                        or today_iso
                    )
                    self._ram_week_cache[wk.start_iso] = (saved_day, m)
        except Exception:
            pass

    def check_once(self, reference_dt: Optional[datetime] = None) -> CheckExecutionSummary:
        """اجرای یک نوبت کامل بررسی وضعیت منوی هفته آینده."""
        started = time.monotonic()
        next_week = get_next_week_range(reference_dt, tz_name=self.config.timezone_name)

        pre_session = self.client.session_manager.load()
        if pre_session.has_credentials:
            session_fp = pre_session.fingerprint
            self.repository.set_runtime_value("active_session_fp", session_fp)
        else:
            session_fp = (
                self.repository.get_runtime_value("active_session_fp")
                or pre_session.fingerprint
            )

        logger.info(
            "شروع بررسی منوی هفته آینده (%s تا %s | %s)",
            next_week.start_iso,
            next_week.end_iso,
            next_week.display_jalali_range(),
        )

        fetch_res = self.client.fetch_week_menu(next_week)
        duration_ms = int((time.monotonic() - started) * 1000)
        active_channels = self.notification_manager.active_channel_names()

        if fetch_res.username:
            self.repository.set_runtime_value("last_username", fetch_res.username)

        # ۱) مدیریت انقضای نشست (ارسال یک‌باره هشدار + علامت‌گذاری توقف تا ورود مجدد)
        if fetch_res.status == CheckStatus.SESSION_EXPIRED:
            self.repository.set_runtime_value("session_status", "EXPIRED")
            self.repository.set_runtime_value("paused_for_relogin", "true")

            exp_event_id = build_session_expired_event_id(session_fp)
            notif_sent = False
            report: Optional[DeliveryReport] = None

            if not self.repository.is_event_fully_delivered(exp_event_id, active_channels):
                already = self.repository.get_delivered_channels(exp_event_id)
                msg_obj = format_session_expired_notification(
                    event_id=exp_event_id,
                    university_label=self.config.samad_university_label,
                    reason=fetch_res.message,
                )
                report = self.notification_manager.dispatch(
                    msg_obj, already_delivered_channels=already
                )
                self.repository.record_event_delivery(
                    event_id=exp_event_id,
                    event_type=EventType.SESSION_EXPIRED,
                    week_start=None,
                    succeeded_channels=report.succeeded,
                    failed_channels=report.failed.keys(),
                    required_channels=active_channels,
                )
                notif_sent = bool(report.succeeded)

            self.repository.record_check_run(
                target_week_start=next_week.start_iso,
                target_week_end=next_week.end_iso,
                status=CheckStatus.SESSION_EXPIRED,
                source=fetch_res.source,
                meals_count=0,
                duration_ms=duration_ms,
                message=fetch_res.message,
                event_id=exp_event_id if notif_sent else None,
                http_status=fetch_res.http_status,
                samad_code=fetch_res.samad_code,
            )
            logger.warning("نشست کاربری منقضی شده است: %s", fetch_res.message)
            return CheckExecutionSummary(
                status=CheckStatus.SESSION_EXPIRED,
                target_week=next_week,
                meals_count=0,
                event_type=EventType.SESSION_EXPIRED if notif_sent else None,
                event_id=exp_event_id,
                notification_sent=notif_sent,
                delivery_report=report,
                message=fetch_res.message,
                duration_ms=duration_ms,
                source=fetch_res.source,
            )

        # ۲) مدیریت حالت عادی «منوی هفته آینده هنوز منتشر نشده است» (بدون اعلان کاذب)
        if fetch_res.status == CheckStatus.MENU_NOT_PUBLISHED:
            self.repository.set_runtime_value("session_status", "ACTIVE")
            self.repository.set_runtime_value("paused_for_relogin", "false")
            self.repository.record_check_run(
                target_week_start=next_week.start_iso,
                target_week_end=next_week.end_iso,
                status=CheckStatus.MENU_NOT_PUBLISHED,
                source=fetch_res.source,
                meals_count=0,
                duration_ms=duration_ms,
                message=fetch_res.message,
                samad_code=fetch_res.samad_code,
            )
            logger.info("منوی هفته آینده هنوز منتشر نشده است (%s).", next_week.display_jalali_range())
            return CheckExecutionSummary(
                status=CheckStatus.MENU_NOT_PUBLISHED,
                target_week=next_week,
                meals_count=0,
                event_type=None,
                event_id=None,
                notification_sent=False,
                delivery_report=None,
                message=fetch_res.message,
                duration_ms=duration_ms,
                source=fetch_res.source,
            )

        # ۳) مدیریت خطاهای فنی (قطعی اینترنت، خطای سرور، داده ناقص، تغییر ساختار سایت)
        if fetch_res.status != CheckStatus.MENU_PUBLISHED:
            self.repository.record_check_run(
                target_week_start=next_week.start_iso,
                target_week_end=next_week.end_iso,
                status=fetch_res.status,
                source=fetch_res.source,
                meals_count=0,
                duration_ms=duration_ms,
                message=fetch_res.message,
                http_status=fetch_res.http_status,
                samad_code=fetch_res.samad_code,
            )
            logger.error(
                "خطای فنی در بررسی سامانه سماد [%s]: %s",
                fetch_res.status.value,
                fetch_res.message,
            )
            return CheckExecutionSummary(
                status=fetch_res.status,
                target_week=next_week,
                meals_count=0,
                event_type=None,
                event_id=None,
                notification_sent=False,
                delivery_report=None,
                message=fetch_res.message,
                duration_ms=duration_ms,
                source=fetch_res.source,
            )

        # ۴) منوی هفته آینده منتشر شده است -> مقایسه با وضعیت قبلی دیتابیس
        self.repository.set_runtime_value("session_status", "ACTIVE")
        self.repository.set_runtime_value("paused_for_relogin", "false")

        previous_menu = self.repository.get_menu_by_week(next_week.start_iso)
        outcome = self.detector.evaluate(
            week_range=next_week,
            current_meals=fetch_res.meals,
            previous_menu=previous_menu,
            source=fetch_res.source,
        )

        if outcome.menu is None:
            # آیتم‌ها خارج از بازه هفته آینده بودند
            self.repository.record_check_run(
                target_week_start=next_week.start_iso,
                target_week_end=next_week.end_iso,
                status=CheckStatus.MENU_NOT_PUBLISHED,
                source=fetch_res.source,
                meals_count=0,
                duration_ms=duration_ms,
                message=outcome.reason,
            )
            return CheckExecutionSummary(
                status=CheckStatus.MENU_NOT_PUBLISHED,
                target_week=next_week,
                meals_count=0,
                event_type=None,
                event_id=None,
                notification_sent=False,
                delivery_report=None,
                message=outcome.reason,
                duration_ms=duration_ms,
                source=fetch_res.source,
            )

        saved_menu = self.repository.save_menu(
            outcome.menu,
            snapshot_event_type=outcome.event_type,
        )
        today_iso = now_tehran(reference_dt, tz_name=self.config.timezone_name).date().isoformat()
        self._ram_week_cache[next_week.start_iso] = (today_iso, saved_menu)
        self.repository.set_runtime_value(f"daily_menu_date:{next_week.start_iso}", today_iso)

        event_type = outcome.event_type
        event_id = outcome.event_id
        initial_eid = build_initial_event_id(next_week.start_iso)

        # اگر در اجرای قبلی اعلان اولیه به دلیل خطای موقت شبکه تلگرام ارسال نشده بود، اکنون تکمیل شود
        if (
            event_type is None
            and active_channels
            and not self.repository.is_event_fully_delivered(initial_eid, active_channels)
        ):
            event_type = EventType.MENU_AVAILABLE
            event_id = initial_eid

        notif_sent = False
        delivery_report: Optional[DeliveryReport] = None

        if event_type is not None and event_id is not None:
            if self.repository.is_event_fully_delivered(event_id, active_channels):
                logger.info(
                    "رویداد %s قبلاً به تمام کانال‌ها ارسال شده است؛ از ارسال تکراری جلوگیری شد.",
                    event_id,
                )
            else:
                already_sent_channels: set[str] = self.repository.get_delivered_channels(event_id)
                if event_type == EventType.MENU_UPDATED and outcome.diff is not None:
                    notif_msg = format_menu_updated_notification(
                        menu=saved_menu,
                        diff=outcome.diff,
                        week_range=next_week,
                        university_label=self.config.samad_university_label,
                        reserve_url=self.config.samad_reserve_url,
                        event_id=event_id,
                    )
                else:
                    notif_msg = format_menu_available_notification(
                        menu=saved_menu,
                        week_range=next_week,
                        university_label=self.config.samad_university_label,
                        reserve_url=self.config.samad_reserve_url,
                        event_id=event_id,
                    )

                delivery_report = self.notification_manager.dispatch(
                    notif_msg, already_delivered_channels=already_sent_channels
                )
                self.repository.record_event_delivery(
                    event_id=event_id,
                    event_type=event_type,
                    week_start=next_week.start_iso,
                    succeeded_channels=delivery_report.succeeded,
                    failed_channels=delivery_report.failed.keys(),
                    required_channels=active_channels,
                )
                notif_sent = bool(delivery_report.succeeded)

        summary_msg = outcome.reason
        self.repository.record_check_run(
            target_week_start=next_week.start_iso,
            target_week_end=next_week.end_iso,
            status=CheckStatus.MENU_PUBLISHED,
            source=fetch_res.source,
            meals_count=len(saved_menu.meals),
            duration_ms=duration_ms,
            message=summary_msg,
            event_id=event_id if notif_sent else None,
        )

        logger.info(
            "بررسی کامل شد: وضعیت=%s | تعداد وعده‌ها=%d | رویداد=%s | اعلان جدید=%s",
            CheckStatus.MENU_PUBLISHED.value,
            len(saved_menu.meals),
            event_type.value if event_type else "NONE",
            notif_sent,
        )

        return CheckExecutionSummary(
            status=CheckStatus.MENU_PUBLISHED,
            target_week=next_week,
            meals_count=len(saved_menu.meals),
            event_type=event_type,
            event_id=event_id,
            notification_sent=notif_sent,
            delivery_report=delivery_report,
            message=summary_msg,
            duration_ms=duration_ms,
            source=fetch_res.source,
            menu=saved_menu,
        )

    def fetch_week_on_demand(
        self,
        week_key: str = "prev",
        force_refresh: bool = False,
        reference_dt: Optional[datetime] = None,
    ) -> Tuple[WeekRange, Optional[Menu], CheckStatus, str]:
        """
        پاسخ‌گویی آنی از کش RAM / SQLite به دکمه‌های ربات تلگرام:
        - `prev` (هفته قبل): یک بار واکشی می‌شود و برای کل هفته از حافظه RAM پاسخ داده می‌شود.
        - `curr` (هفته جاری): حداکثر یک بار در هر روز تقویمی تهران واکشی می‌شود و تمام روز از RAM پاسخ داده می‌شود.
        - `next` (هفته آینده): مستقیم از حافظه RAM / دیتابیس پاسخ داده می‌شود.
        """
        ref = reference_dt or now_tehran(tz_name=self.config.timezone_name)
        today_iso = ref.date().isoformat()
        clean_key = (week_key or "prev").strip().lower()
        if clean_key == "prev":
            target_week = get_previous_week_range(ref, tz_name=self.config.timezone_name)
        elif clean_key == "curr":
            target_week = get_current_week_range(ref, tz_name=self.config.timezone_name)
        else:
            clean_key = "next"
            target_week = get_next_week_range(ref, tz_name=self.config.timezone_name)

        # مسیر سریع بدون قفل (O(1) RAM Cache) برای پاسخ زیر ۱ میلی‌ثانیه به همه کاربران!
        if not force_refresh:
            ram_entry = self._ram_week_cache.get(target_week.start_iso)
            if ram_entry is not None:
                cached_day_iso, ram_menu = ram_entry
                if ram_menu.meals:
                    if clean_key in {"prev", "next"} or cached_day_iso == today_iso:
                        return (target_week, ram_menu, CheckStatus.MENU_PUBLISHED, "از حافظه سریع RAM")

        with self._on_demand_lock:
            cached = self.repository.get_menu_by_week(target_week.start_iso)
            daily_date_key = f"daily_menu_date:{target_week.start_iso}"
            legacy_flag_key = f"unfiltered_week_cached:{target_week.start_iso}"
            saved_day_iso = self.repository.get_runtime_value(daily_date_key)
            has_legacy_flag = bool(self.repository.get_runtime_value(legacy_flag_key))

            if clean_key == "next":
                if cached and cached.meals and not force_refresh:
                    self._ram_week_cache[target_week.start_iso] = (today_iso, cached)
                    return (
                        target_week,
                        cached,
                        CheckStatus.MENU_PUBLISHED,
                        "از آخرین بررسی ذخیره‌شده در دیتابیس",
                    )
                if force_refresh:
                    summary = self.check_once(reference_dt=ref)
                    return (target_week, summary.menu, summary.status, summary.message)
                return (
                    target_week,
                    None,
                    CheckStatus.MENU_NOT_PUBLISHED,
                    "منوی هفته آینده هنوز در آخرین بررسی منتشر نشده است.",
                )

            if cached and cached.meals and not force_refresh:
                # هفته قبل: ثابت است و اصلاً نیاز به استعلام مجدد ندارد
                if clean_key == "prev" and (saved_day_iso or has_legacy_flag):
                    self._ram_week_cache[target_week.start_iso] = (saved_day_iso or today_iso, cached)
                    return (target_week, cached, CheckStatus.MENU_PUBLISHED, "از کش دیتابیس")
                # هفته جاری: فقط یک بار در هر روز تقویمی تهران کافی است!
                if clean_key == "curr" and saved_day_iso == today_iso:
                    self._ram_week_cache[target_week.start_iso] = (today_iso, cached)
                    return (target_week, cached, CheckStatus.MENU_PUBLISHED, "از کش روزانه دیتابیس")

            logger.info(
                "واکشی روزانه منوی هفته %s (%s تا %s) از سماد و ذخیره در کش...",
                clean_key,
                target_week.start_iso,
                target_week.end_iso,
            )
            fetch_res = self.client.fetch_week_menu(
                target_week, ignore_user_self_filter=True
            )

            if fetch_res.status == CheckStatus.MENU_PUBLISHED and fetch_res.meals:
                outcome = self.detector.evaluate(
                    week_range=target_week,
                    current_meals=fetch_res.meals,
                    previous_menu=None,
                    source=fetch_res.source,
                )
                if outcome.menu is not None:
                    saved = self.repository.save_menu(outcome.menu)
                    self.repository.set_runtime_value(daily_date_key, today_iso)
                    self.repository.set_runtime_value(legacy_flag_key, str(int(time.time())))
                    self._ram_week_cache[target_week.start_iso] = (today_iso, saved)
                    return (target_week, saved, CheckStatus.MENU_PUBLISHED, fetch_res.message)

            if cached and cached.meals:
                self._ram_week_cache[target_week.start_iso] = (today_iso, cached)
                return (target_week, cached, CheckStatus.MENU_PUBLISHED, "از آرشیو ذخیره‌شده دیتابیس")

            return (target_week, None, fetch_res.status, fetch_res.message)

    def warm_daily_menus_if_needed(self, reference_dt: Optional[datetime] = None) -> None:
        """
        پیش‌بارگذاری خودکار روزانه در پس‌زمینه (حداکثر یک بار در روز برای هفته جاری و یک بار برای هفته قبل)
        تا وقتی هر کاربری در تلگرام دکمه منو را می‌زند، بدون حتی ۱ ثانیه صبر کردن برای سماد، پاسخ آنی بگیرد!
        """
        try:
            if not self.client.session_manager.load().has_credentials:
                return
            for wk_key in ("curr", "prev"):
                self.fetch_week_on_demand(wk_key, force_refresh=False, reference_dt=reference_dt)
        except Exception as exc:
            logger.debug("پیش‌بارگذاری پس‌زمینه کش روزانه رد شد: %s", exc)

