"""زیرسیستم چندکاناله اعلان‌رسانی (تلگرام، ویندوز ۱۱ و قالب‌بندی فارسی)."""

from samad_monitor.notifications.base import NotificationChannel
from samad_monitor.notifications.formatter import (
    format_menu_available_notification,
    format_menu_updated_notification,
    format_session_expired_notification,
)
from samad_monitor.notifications.manager import DeliveryReport, NotificationManager
from samad_monitor.notifications.telegram import TelegramNotificationChannel
from samad_monitor.notifications.windows import WindowsDesktopNotificationChannel

__all__ = [
    "DeliveryReport",
    "NotificationChannel",
    "NotificationManager",
    "TelegramNotificationChannel",
    "WindowsDesktopNotificationChannel",
    "format_menu_available_notification",
    "format_menu_updated_notification",
    "format_session_expired_notification",
]
