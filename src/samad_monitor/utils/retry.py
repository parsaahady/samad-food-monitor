"""مدیریت Rate Limiting، Timeout و تلاش مجدد هوشمند با Exponential Backoff."""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Tuple, Type, TypeVar

logger = logging.getLogger("samad_monitor.retry")

T = TypeVar("T")


class NonRetryableError(Exception):
    """خطاهایی که نباید مجدداً تلاش شوند (مانند انقضای نشست یا نقض محدودیت امنیتی)."""


class RateLimiter:
    """محدودکننده نرخ درخواست برای جلوگیری از ارسال درخواست‌های پشت‌سرهم و سنگین به سماد."""

    def __init__(self, min_interval_seconds: float = 1.2) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._last_call_monotonic: float = 0.0

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_call_monotonic
        if self._last_call_monotonic > 0 and elapsed < self.min_interval_seconds:
            sleep_for = self.min_interval_seconds - elapsed
            time.sleep(sleep_for)
        self._last_call_monotonic = time.monotonic()


def compute_backoff_delay(
    attempt: int,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    jitter_ratio: float = 0.25,
) -> float:
    """محاسبه تأخیر نمایی (Exponential Backoff) همراه با Jitter تصادفی."""
    exp_delay = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
    jitter = exp_delay * jitter_ratio * random.random()
    return min(max_delay, exp_delay + jitter)


def retry_call(
    operation: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    retryable_exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    non_retryable_exceptions: Tuple[Type[BaseException], ...] = (NonRetryableError,),
    operation_name: str = "operation",
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    """اجرای تابع با مکانیزم تلاش مجدد هوشمند فقط برای خطاهای موقت."""
    attempts = max(1, int(max_attempts))
    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except non_retryable_exceptions:
            raise
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt >= attempts:
                logger.warning(
                    "عملیات '%s' پس از %d تلاش ناموفق بود: %s",
                    operation_name,
                    attempt,
                    exc,
                )
                raise
            delay = compute_backoff_delay(attempt, base_delay=base_delay, max_delay=max_delay)
            logger.info(
                "خطای موقت در '%s' (تلاش %d از %d): %s — تلاش مجدد پس از %.1f ثانیه...",
                operation_name,
                attempt,
                attempts,
                exc,
                delay,
            )
            sleep_fn(delay)

    assert last_exc is not None
    raise last_exc
