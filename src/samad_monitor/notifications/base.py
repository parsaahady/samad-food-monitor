"""کلاس پایه انتزاعی برای کانال‌های اعلان‌رسانی قابل توسعه."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Tuple

from samad_monitor.models import NotificationMessage


class NotificationChannel(ABC):
    """رابط استاندارد هر کانال اعلان (Telegram، Windows Toast، Email/Webhook آینده)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """نام یکتای کانال (مثلاً `telegram` یا `windows`)."""

    @abstractmethod
    def is_configured(self) -> Tuple[bool, str]:
        """بررسی آماده و پیکربندی بودن کانال همراه با توضیح وضعیت برای دستور `status`."""

    @abstractmethod
    def send(self, message: NotificationMessage) -> None:
        """ارسال اعلان؛ در صورت بروز خطا باید استثنا پرتاب کند تا ثبت و مدیریت شود."""
