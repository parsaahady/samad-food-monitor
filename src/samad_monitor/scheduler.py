"""
زمان‌بند هوشمند مبتنی بر منطقه زمانی `Asia/Tehran`:
- روزهای عادی (شنبه تا چهارشنبه): پیش‌فرض هر ۶۰ دقیقه
- روزهای اوج انتشار منو (پنجشنبه و جمعه): پیش‌فرض هر ۱۰ تا ۱۵ دقیقه
- همراه با Jitter تصادفی، Backoff ملایم در خطاهای شبکه، و تشخیص خودکار ورود مجدد کاربر
"""

from __future__ import annotations

import logging
import random
import signal
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from samad_monitor.config import AppConfig
from samad_monitor.models import CheckStatus
from samad_monitor.monitor import SamadFoodMonitor
from samad_monitor.notifications.telegram import TelegramBotPoller
from samad_monitor.utils.date_utils import is_peak_check_day, now_tehran

logger = logging.getLogger("samad_monitor.scheduler")


class AdaptiveScheduler:
    """حلقه زمان‌بند پایدار برای اجرای پس‌زمینه پایشگر سماد."""

    def __init__(
        self,
        config: AppConfig,
        monitor: SamadFoodMonitor,
        rng: Optional[random.Random] = None,
        enable_bot_poller: bool = True,
    ) -> None:
        self.config = config
        self.monitor = monitor
        self._rng = rng or random.Random()
        self._stop_event = threading.Event()
        self._consecutive_errors = 0
        self._enable_bot_poller = enable_bot_poller
        self._bot_poller: Optional[TelegramBotPoller] = None

    def stop(self) -> None:
        self._stop_event.set()
        if self._bot_poller is not None:
            self._bot_poller.stop()

    def compute_delay_seconds(
        self,
        reference_dt: Optional[datetime] = None,
        consecutive_errors: int = 0,
    ) -> float:
        """محاسبه فاصله زمانی تا بررسی بعدی بر اساس روز هفته به وقت تهران + Jitter."""
        current = reference_dt or now_tehran(tz_name=self.config.timezone_name)
        if is_peak_check_day(current, tz_name=self.config.timezone_name):
            base_minutes = self._rng.uniform(
                float(self.config.peak_interval_min_minutes),
                float(self.config.peak_interval_max_minutes),
            )
        else:
            base_minutes = float(self.config.normal_interval_minutes)

        base_seconds = base_minutes * 60.0

        if consecutive_errors > 0:
            # در صورت خطای شبکه، فاصله کمی افزایش می‌یابد اما از سقف بازه عادی فراتر نمی‌رود
            multiplier = min(2.0, 1.25 ** min(consecutive_errors, 4))
            base_seconds = min(base_seconds * multiplier, 3600.0)

        jitter = (
            self._rng.uniform(0.0, float(self.config.jitter_max_seconds))
            if self.config.jitter_max_seconds > 0
            else 0.0
        )
        return round(base_seconds + jitter, 1)

    def _get_session_mtime(self) -> Optional[float]:
        path: Path = self.config.storage_state_path
        if not path.exists():
            return None
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def _install_signal_handlers(self) -> None:
        def _handle_sig(signum: int, _frame: object) -> None:
            logger.info("سیگنال توقف (%s) دریافت شد؛ در حال خروج ایمن از زمان‌بند...", signum)
            self.stop()

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handle_sig)
                except ValueError:
                    # خارج از ترد اصلی
                    pass

    def _wait_until_relogin_or_stop(self, expired_mtime: Optional[float]) -> None:
        """
        زمانی که نشست منقضی شده است، طبق بند ۱۰ مستند، درخواست مکرر به سماد متوقف می‌شود
        تا زمانی که کاربر با اجرای `python -m samad_monitor login` فایل نشست جدید بسازد.
        """
        logger.warning(
            "پایش خودکار به علت انقضای نشست در حالت انتظار قرار گرفت. "
            "به محض اجرای `python -m samad_monitor login` پایش به‌صورت خودکار ادامه می‌یابد."
        )
        self.monitor.repository.set_runtime_value(
            "next_scheduled_check_at",
            "متوقف تا ورود مجدد (login)",
        )

        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=15.0):
                return
            new_mtime = self._get_session_mtime()
            if new_mtime is not None and new_mtime != expired_mtime:
                session_data = self.monitor.client.session_manager.load()
                if session_data.has_credentials:
                    logger.info("فایل نشست جدید شناسایی شد! ازسرگیری خودکار پایش منو...")
                    self.monitor.repository.set_runtime_value("paused_for_relogin", "false")
                    self.monitor.repository.set_runtime_value("session_status", "ACTIVE")
                    return

    def run_forever(self) -> None:
        """اجرای پیوسته سرویس پایش در پس‌زمینه."""
        self._install_signal_handlers()
        tg_channel = self.monitor.notification_manager.get_telegram_channel()
        if self._enable_bot_poller and tg_channel and self.config.telegram_enabled and tg_channel.has_bot_token:
            self._bot_poller = TelegramBotPoller(
                channel=tg_channel,
                auto_subscribe_getter=lambda: self.config.telegram_auto_subscribe,
                invite_code_getter=lambda: self.config.telegram_invite_code,
            )
            self._bot_poller.start()

        logger.info(
            "سرویس پایش سماد (%s) آغاز به کار کرد | بازه عادی: %d دقیقه | بازه پنجشنبه/جمعه: %d تا %d دقیقه",
            self.config.samad_university_label,
            self.config.normal_interval_minutes,
            self.config.peak_interval_min_minutes,
            self.config.peak_interval_max_minutes,
        )

        while not self._stop_event.is_set():
            pre_mtime = self._get_session_mtime()
            summary = self.monitor.check_once()

            if summary.status == CheckStatus.SESSION_EXPIRED:
                self._consecutive_errors = 0
                self._wait_until_relogin_or_stop(pre_mtime)
                continue

            if summary.status in {CheckStatus.MENU_PUBLISHED, CheckStatus.MENU_NOT_PUBLISHED}:
                self._consecutive_errors = 0
                # پیش‌بارگذاری یک‌باردرروز منوی هفته جاری و هفته قبل در کش RAM و SQLite
                if not self._stop_event.is_set():
                    self.monitor.warm_daily_menus_if_needed()
            else:
                self._consecutive_errors += 1

            now_dt = now_tehran(tz_name=self.config.timezone_name)
            delay_sec = self.compute_delay_seconds(
                reference_dt=now_dt,
                consecutive_errors=self._consecutive_errors,
            )
            next_run_dt = now_dt + timedelta(seconds=delay_sec)
            self.monitor.repository.set_runtime_value(
                "next_scheduled_check_at",
                next_run_dt.isoformat(),
            )

            logger.info(
                "بررسی بعدی در ساعت %s به وقت تهران (پس از %.1f دقیقه) انجام خواهد شد.",
                next_run_dt.strftime("%Y-%m-%d %H:%M:%S"),
                delay_sec / 60.0,
            )

            if self._stop_event.wait(timeout=delay_sec):
                break

        if self._bot_poller is not None:
            self._bot_poller.stop()
        self.monitor.repository.set_runtime_value("next_scheduled_check_at", "سرویس متوقف است")
        logger.info("سرویس پایش سماد به شکل ایمن متوقف شد.")
