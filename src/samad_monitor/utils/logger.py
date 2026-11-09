"""سیستم ثبت گزارش (Logging) امن با فیلتر خودکار حذف توکن، کوکی و رمز عبور."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from samad_monitor.utils.date_utils import now_tehran


SENSITIVE_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(bot\d{6,12}:)[A-Za-z0-9_\-]{20,}", re.IGNORECASE), r"\1[REDACTED_BOT_TOKEN]"),
    (
        re.compile(
            r'("(?:access_token|refresh_token|password|captchaText|loginAsToken)"\s*:\s*")[^"]*(")',
            re.IGNORECASE,
        ),
        r"\1[REDACTED]\2",
    ),
    (
        re.compile(
            r"((?:access_token|refresh_token|password|loginAsToken)=)[^&\s]+",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
]


def redact_secrets(text: str) -> str:
    """پاک‌سازی هرگونه توکن، رمز عبور یا کلید حساس از متن گزارش."""
    if not text:
        return text
    sanitized = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


class DynamicStdoutHandler(logging.StreamHandler):
    """نوشتن روی sys.stdout فعلی در لحظه انتشار لاگ (سازگار با تغییر استریم در تست‌ها و ویندوز)."""

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, value: object) -> None:
        pass

    def close(self) -> None:
        logging.Handler.close(self)


class SecretRedactingFormatter(logging.Formatter):
    """فرمتر لاگ بر پایه ساعت تهران با پاک‌سازی خودکار اطلاعات محرمانه."""

    def __init__(self, fmt: str, tz_name: str = "Asia/Tehran") -> None:
        super().__init__(fmt=fmt)
        self.tz_name = tz_name

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        tehran_dt = now_tehran(dt, self.tz_name)
        if datefmt:
            return tehran_dt.strftime(datefmt)
        return tehran_dt.strftime("%Y-%m-%d %H:%M:%S %z")

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_secrets(rendered)


def setup_logging(
    logs_dir: Path,
    level: str = "INFO",
    tz_name: str = "Asia/Tehran",
    log_filename: str = "samad_monitor.log",
) -> logging.Logger:
    """راه‌اندازی لاگر اصلی پروژه با خروجی کنسول UTF-8 و فایل چرخشی."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / log_filename

    # تضمین نمایش صحیح کاراکترهای فارسی در کنسول ویندوز
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root_logger = logging.getLogger("samad_monitor")
    root_logger.setLevel(numeric_level)
    root_logger.propagate = False

    # جلوگیری از افزودن تکراری Handler در فراخوانی‌های مجدد
    if root_logger.handlers:
        for h in list(root_logger.handlers):
            root_logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    formatter = SecretRedactingFormatter(fmt=fmt, tz_name=tz_name)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,  # ۵ مگابایت به ازای هر فایل
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)

    console_handler = DynamicStdoutHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # کاهش نویز کتابخانه‌های شبکه
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return root_logger
