"""
کانال اعلان دسکتاپ ویندوز ۱۱ (Windows Desktop Toast Notification):
- موتور اول در ویندوز ۱۱: کتابخانه سبک و بومی `winotify` (با دکمه باز کردن مستقیم صفحه رزرو سماد)
- موتور پشتیبان در ویندوز ۱۱: اسکریپت بومی PowerShell WinRT Toast بدون وابستگی خارجی
- رفتار امن در سیستم‌عامل‌های غیرویندوز: غیرفعال‌سازی محترمانه بدون خطا یا کرش
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Callable, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

from samad_monitor.models import NotificationMessage
from samad_monitor.notifications.base import NotificationChannel

logger = logging.getLogger("samad_monitor.notifications.windows")


class WindowsDesktopNotificationChannel(NotificationChannel):
    """ارسال اعلان بومی Action Center روی Windows 11."""

    def __init__(
        self,
        app_id: str = "Samad Food Monitor",
        platform_override: Optional[str] = None,
        custom_sender: Optional[Callable[[NotificationMessage], None]] = None,
    ) -> None:
        self.app_id = app_id
        self._platform = platform_override or sys.platform
        self._custom_sender = custom_sender

    @property
    def name(self) -> str:
        return "windows"

    def _has_winotify(self) -> bool:
        try:
            import winotify  # type: ignore  # noqa: F401

            return True
        except ImportError:
            return False

    def is_configured(self) -> Tuple[bool, str]:
        if self._custom_sender is not None:
            return (True, "فعال (ارسال‌کننده سفارشی/تست)")
        if self._platform != "win32":
            return (
                False,
                f"غیرفعال در سیستم‌عامل فعلی ({self._platform}؛ مختص Windows 10/11)",
            )
        if self._has_winotify():
            return (True, "فعال (موتور بومی ویندوز ۱۱ - winotify)")
        return (True, "فعال (موتور پشتیبان PowerShell WinRT Toast)")

    def send(self, message: NotificationMessage) -> None:
        if self._custom_sender is not None:
            self._custom_sender(message)
            return

        if self._platform != "win32":
            raise RuntimeError(
                f"اعلان دسکتاپ ویندوز روی سیستم‌عامل {self._platform} پشتیبانی نمی‌شود."
            )

        toast_body = (message.short_summary or message.body).strip()
        if len(toast_body) > 240:
            toast_body = toast_body[:237] + "..."

        if self._has_winotify():
            self._send_with_winotify(message.title, toast_body, message.action_url)
            logger.info("اعلان دسکتاپ ویندوز ۱۱ (winotify) برای %s نمایش داده شد.", message.event_id)
            return

        self._send_with_powershell_winrt(message.title, toast_body, message.action_url)
        logger.info("اعلان دسکتاپ ویندوز ۱۱ (PowerShell WinRT) برای %s نمایش داده شد.", message.event_id)

    def _send_with_winotify(self, title: str, body: str, action_url: str) -> None:
        from winotify import Notification, audio  # type: ignore

        toast = Notification(
            app_id=self.app_id,
            title=title,
            msg=body,
            duration="long",
            launch=action_url or "https://samad.app/food/reserve",
        )
        toast.set_audio(audio.Default, loop=False)
        if action_url:
            toast.add_actions(label="باز کردن صفحه رزرو سماد", launch=action_url)
        toast.show()

    def _send_with_powershell_winrt(self, title: str, body: str, action_url: str) -> None:
        safe_title = xml_escape(title)
        safe_body = xml_escape(body)
        safe_url = xml_escape(action_url or "https://samad.app/food/reserve")

        ps_script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xmlStr = @"
<toast activationType="protocol" launch="{safe_url}">
  <visual>
    <binding template="ToastGeneric">
      <text>{safe_title}</text>
      <text>{safe_body}</text>
    </binding>
  </visual>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($xmlStr)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Windows PowerShell')
$notifier.Show($toast)
"""
        CREATE_NO_WINDOW = 0x08000000
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_script,
            ],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"خطا در نمایش Toast ویندوز با PowerShell: {err}")
