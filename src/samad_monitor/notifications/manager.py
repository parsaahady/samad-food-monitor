"""مدیریت مرکزی کانال‌های اعلان با پشتیبانی از ارسال هم‌زمان و جلوگیری از ارسال تکراری در هر کانال."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Set

from samad_monitor.config import AppConfig
from samad_monitor.models import NotificationMessage
from samad_monitor.notifications.base import NotificationChannel
from samad_monitor.notifications.telegram import TelegramNotificationChannel
from samad_monitor.notifications.windows import WindowsDesktopNotificationChannel

if TYPE_CHECKING:
    from samad_monitor.database.repository import MonitorRepository

logger = logging.getLogger("samad_monitor.notifications.manager")


@dataclass
class DeliveryReport:
    """گزارش نتیجه ارسال یک اعلان روی کانال‌های فعال."""

    succeeded: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    skipped_already_sent: List[str] = field(default_factory=list)
    active_channel_names: List[str] = field(default_factory=list)

    @property
    def all_active_delivered(self) -> bool:
        if not self.active_channel_names:
            return False
        done = set(self.succeeded) | set(self.skipped_already_sent)
        return set(self.active_channel_names).issubset(done) and not self.failed


class NotificationManager:
    """مدیریت ثبت، بررسی وضعیت و ارسال پیام به تمام کانال‌های اعلان انتخاب‌شده."""

    def __init__(self, channels: Iterable[NotificationChannel]) -> None:
        self._channels: List[NotificationChannel] = list(channels)

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        repository: Optional["MonitorRepository"] = None,
    ) -> "NotificationManager":
        channels: List[NotificationChannel] = [
            TelegramNotificationChannel(
                bot_token=config.telegram_bot_token,
                chat_id=config.telegram_chat_ids,
                api_base_url=config.telegram_api_base_url,
                proxy_url=config.telegram_proxy_url,
                timeout_seconds=config.http_timeout_seconds,
                enabled=config.telegram_enabled,
                repository=repository,
                university_label=config.samad_university_label,
                reserve_url=config.samad_reserve_url,
                timezone_name=config.timezone_name,
            )
        ]

        if config.windows_notification_enabled:
            channels.append(
                WindowsDesktopNotificationChannel(
                    app_id=config.windows_toast_app_id,
                )
            )

        return cls(channels=channels)

    def get_telegram_channel(self) -> Optional[TelegramNotificationChannel]:
        for ch in self._channels:
            if isinstance(ch, TelegramNotificationChannel):
                return ch
        return None

    @property
    def channels(self) -> List[NotificationChannel]:
        return list(self._channels)

    def active_channels(self) -> List[NotificationChannel]:
        return [ch for ch in self._channels if ch.is_configured()[0]]

    def active_channel_names(self) -> List[str]:
        return [ch.name for ch in self.active_channels()]

    def describe_channels(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for ch in self._channels:
            _, desc = ch.is_configured()
            result[ch.name] = desc
        return result

    def dispatch(
        self,
        message: NotificationMessage,
        already_delivered_channels: Optional[Set[str]] = None,
    ) -> DeliveryReport:
        """ارسال پیام به کانال‌های فعال که هنوز این رویداد را دریافت نکرده‌اند."""
        already = already_delivered_channels or set()
        active = self.active_channels()
        report = DeliveryReport(active_channel_names=[ch.name for ch in active])

        if not active:
            logger.warning(
                "هیچ کانال اعلانی فعال و پیکربندی نشده است! رویداد %s فقط در لاگ ثبت شد.",
                message.event_id,
            )
            return report

        for channel in active:
            if channel.name in already:
                report.skipped_already_sent.append(channel.name)
                continue

            try:
                channel.send(message)
                report.succeeded.append(channel.name)
            except Exception as exc:
                err_msg = str(exc)
                report.failed[channel.name] = err_msg
                logger.error(
                    "خطا در ارسال اعلان %s از طریق کانال %s: %s",
                    message.event_id,
                    channel.name,
                    err_msg,
                )

        return report
