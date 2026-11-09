"""مدیریت متمرکز و امن تنظیمات برنامه از فایل .env و متغیرهای محیطی."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import dotenv_values


FORBIDDEN_CREDENTIAL_KEYS = (
    "SAMAD_PASSWORD",
    "SAMAD_PASS",
    "SAMAD_USER_PASSWORD",
    "STUDENT_PASSWORD",
    "SAMAD_USERNAME",
)


def _parse_bool(value: Optional[str], default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: Optional[str], default: int, minimum: Optional[int] = None) -> int:
    if value is None or value.strip() == "":
        result = default
    else:
        result = int(value.strip())
    if minimum is not None and result < minimum:
        return minimum
    return result


def _parse_float(value: Optional[str], default: float, minimum: Optional[float] = None) -> float:
    if value is None or value.strip() == "":
        result = default
    else:
        result = float(value.strip())
    if minimum is not None and result < minimum:
        return minimum
    return result


def _parse_csv_ints(value: Optional[str]) -> List[int]:
    if not value or not value.strip():
        return []
    items: List[int] = []
    for part in value.split(","):
        part = part.strip()
        if part:
            items.append(int(part))
    return items


def _parse_csv_strings(value: Optional[str]) -> List[str]:
    if not value or not value.strip():
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


@dataclass
class AppConfig:
    """پیکربندی کامل اجرای Samad Food Monitor (سازگار با .env.example)."""

    base_dir: Path = field(default_factory=Path.cwd)
    env_file: Optional[Path] = None

    # تنظیمات سامانه سماد (منحصراً دانشگاه شهید بهشتی به‌طور پیش‌فرض)
    samad_web_url: str = "https://samad.app"
    samad_reserve_url: str = "https://samad.app/food/reserve"
    samad_university_key: str = "SBU"
    samad_university_label: str = "دانشگاه شهید بهشتی"
    samad_university_api_url: str = "https://dining.sbu.ac.ir"
    samad_fetch_strategy: str = "auto"  # auto | api | browser
    samad_self_ids: List[int] = field(default_factory=list)
    samad_self_name_filter: List[str] = field(default_factory=list)
    samad_ignore_env_proxy: bool = True
    samad_proxy_url: Optional[str] = None

    # زمان‌بندی و شبکه
    timezone_name: str = "Asia/Tehran"
    check_interval_normal_minutes: int = 60
    check_interval_peak_minutes: int = 12
    peak_interval_min_minutes: int = 10
    peak_interval_max_minutes: int = 15
    check_jitter_seconds: int = 45
    request_min_interval_seconds: float = 1.2
    http_timeout_seconds: float = 25.0
    max_retry_attempts: int = 3
    retry_base_delay_seconds: float = 2.0
    login_timeout_seconds: int = 300

    # مسیرهای ذخیره‌سازی محلی
    data_dir: Path = field(default_factory=lambda: Path("data"))
    logs_dir: Path = field(default_factory=lambda: Path("logs"))
    sqlite_db_path: Path = field(default_factory=lambda: Path("data/samad_monitor.db"))
    browser_profile_dir: Path = field(default_factory=lambda: Path("data/browser_profile"))
    storage_state_path: Path = field(default_factory=lambda: Path("data/storage_state.json"))

    # کانال‌های اعلان
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_ids: List[str] = field(default_factory=list)
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_proxy_url: Optional[str] = None
    telegram_auto_subscribe: bool = False
    telegram_invite_code: str = ""

    windows_notification_enabled: bool = True
    windows_notification_sound: bool = True
    windows_toast_app_id: str = "Samad Food Monitor"

    # سطح لاگ و هشدارهای امنیتی پیکربندی
    log_level: str = "INFO"
    security_warnings: List[str] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.sqlite_db_path

    @property
    def normal_interval_minutes(self) -> int:
        return self.check_interval_normal_minutes

    @property
    def jitter_max_seconds(self) -> int:
        return self.check_jitter_seconds

    def ensure_directories(self) -> None:
        """ایجاد دایرکتوری‌های محلی مورد نیاز با مجوز دسترسی محدود برای پوشه نشست."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
            os.chmod(self.browser_profile_dir, 0o700)
        except OSError:
            pass

    def detect_forbidden_credential_vars(self) -> List[str]:
        found: List[str] = []
        raw_env_file = (
            dotenv_values(self.env_file) if (self.env_file and self.env_file.is_file()) else {}
        )
        for bad_key in FORBIDDEN_CREDENTIAL_KEYS:
            if raw_env_file.get(bad_key) or os.environ.get(bad_key):
                found.append(bad_key)
        return found

    @classmethod
    def load(
        cls,
        env_path: Optional[Path | str] = None,
        base_dir: Optional[Path] = None,
    ) -> "AppConfig":
        """بارگذاری تنظیمات از فایل .env (بدون آلوده کردن دائمی os.environ در تست‌ها) و متغیرهای محیطی."""
        resolved_base = (base_dir or Path.cwd()).resolve()
        candidate_env: Optional[Path] = None

        if env_path is not None:
            candidate_env = Path(env_path).expanduser().resolve()
        else:
            for candidate in (
                resolved_base / ".env",
                Path(__file__).resolve().parents[2] / ".env",
            ):
                if candidate.is_file():
                    candidate_env = candidate.resolve()
                    resolved_base = candidate_env.parent
                    break

        file_vals: Dict[str, Optional[str]] = (
            dict(dotenv_values(candidate_env))
            if (candidate_env and candidate_env.is_file())
            else {}
        )

        def _get(key: str, alt_key: Optional[str] = None) -> Optional[str]:
            if key in os.environ:
                return os.environ[key]
            if key in file_vals and file_vals[key] is not None:
                return file_vals[key]
            if alt_key:
                if alt_key in os.environ:
                    return os.environ[alt_key]
                if alt_key in file_vals and file_vals[alt_key] is not None:
                    return file_vals[alt_key]
            return None

        warnings: List[str] = []
        for bad_key in FORBIDDEN_CREDENTIAL_KEYS:
            if file_vals.get(bad_key) or os.environ.get(bad_key):
                warnings.append(
                    f"کلید غیرمجاز {bad_key} در تنظیمات مشاهده شد و نادیده گرفته می‌شود! "
                    "رمز عبور هرگز نباید در فایل تنظیمات قرار گیرد."
                )

        def _resolve_path(raw: Optional[str], default_rel: str) -> Path:
            val = (raw or default_rel).strip()
            p = Path(val).expanduser()
            return p if p.is_absolute() else (resolved_base / p).resolve()

        strategy = (_get("SAMAD_FETCH_STRATEGY") or "auto").strip().lower()
        if strategy not in {"auto", "api", "browser"}:
            warnings.append(
                f"مقدار نامعتبر SAMAD_FETCH_STRATEGY='{strategy}'؛ به 'auto' بازنشانی شد."
            )
            strategy = "auto"

        samad_proxy = (_get("SAMAD_PROXY_URL") or "").strip() or None
        tg_proxy = (_get("TELEGRAM_PROXY_URL") or "").strip() or None

        peak_mid = _parse_int(_get("CHECK_INTERVAL_PEAK_MINUTES"), 12, minimum=2)
        peak_min = _parse_int(_get("PEAK_INTERVAL_MIN_MINUTES"), max(2, peak_mid - 2), minimum=2)
        peak_max = _parse_int(_get("PEAK_INTERVAL_MAX_MINUTES"), max(peak_min, peak_mid + 3), minimum=peak_min)

        tg_token = (_get("TELEGRAM_BOT_TOKEN") or "").strip()
        tg_chats = _parse_csv_strings(_get("TELEGRAM_CHAT_ID"))
        # اگر کاربر NOTIFICATION_CHANNELS یا TELEGRAM_ENABLED داده باشد هر دو پشتیبانی شوند
        channels_override = [c.lower() for c in _parse_csv_strings(_get("NOTIFICATION_CHANNELS"))]
        if channels_override:
            tg_enabled = "telegram" in channels_override
            win_enabled = "windows" in channels_override
        else:
            default_tg = bool(tg_token and tg_chats)
            tg_enabled = _parse_bool(_get("TELEGRAM_ENABLED"), default_tg)
            win_enabled = _parse_bool(_get("WINDOWS_NOTIFICATION_ENABLED"), True)

        return cls(
            base_dir=resolved_base,
            env_file=candidate_env,
            samad_web_url=(_get("SAMAD_WEB_URL") or "https://samad.app").rstrip("/"),
            samad_reserve_url=(
                _get("SAMAD_RESERVE_URL") or "https://samad.app/food/reserve"
            ).strip(),
            samad_university_key=(_get("SAMAD_UNIVERSITY_KEY") or "SBU").strip().upper(),
            samad_university_label=(
                _get("SAMAD_UNIVERSITY_LABEL") or "دانشگاه شهید بهشتی"
            ).strip(),
            samad_university_api_url=(
                _get("SAMAD_UNIVERSITY_API_URL") or "https://dining.sbu.ac.ir"
            ).rstrip("/"),
            samad_fetch_strategy=strategy,
            samad_self_ids=_parse_csv_ints(_get("SAMAD_SELF_IDS")),
            samad_self_name_filter=_parse_csv_strings(_get("SAMAD_SELF_NAME_FILTER")),
            samad_ignore_env_proxy=_parse_bool(_get("SAMAD_IGNORE_ENV_PROXY"), True),
            samad_proxy_url=samad_proxy,
            timezone_name=(_get("SAMAD_TIMEZONE") or "Asia/Tehran").strip(),
            check_interval_normal_minutes=_parse_int(
                _get("CHECK_INTERVAL_NORMAL_MINUTES"), 60, minimum=5
            ),
            check_interval_peak_minutes=peak_mid,
            peak_interval_min_minutes=peak_min,
            peak_interval_max_minutes=peak_max,
            check_jitter_seconds=_parse_int(_get("CHECK_JITTER_SECONDS"), 45, minimum=0),
            request_min_interval_seconds=_parse_float(
                _get("REQUEST_MIN_INTERVAL_SECONDS"), 1.2, minimum=0.2
            ),
            http_timeout_seconds=_parse_float(
                _get("HTTP_TIMEOUT_SECONDS"), 25.0, minimum=5.0
            ),
            max_retry_attempts=_parse_int(_get("MAX_RETRY_ATTEMPTS"), 3, minimum=1),
            retry_base_delay_seconds=_parse_float(
                _get("RETRY_BASE_DELAY_SECONDS"), 2.0, minimum=0.5
            ),
            login_timeout_seconds=_parse_int(_get("LOGIN_TIMEOUT_SECONDS"), 300, minimum=30),
            data_dir=_resolve_path(_get("DATA_DIR"), "data"),
            logs_dir=_resolve_path(_get("LOGS_DIR", "SAMAD_LOG_DIR"), "logs"),
            sqlite_db_path=_resolve_path(
                _get("SQLITE_DB_PATH", "SAMAD_DB_PATH"), "data/samad_monitor.db"
            ),
            browser_profile_dir=_resolve_path(
                _get("BROWSER_PROFILE_DIR", "SAMAD_BROWSER_PROFILE_DIR"), "data/browser_profile"
            ),
            storage_state_path=_resolve_path(
                _get("STORAGE_STATE_PATH", "SAMAD_STORAGE_STATE_PATH"), "data/storage_state.json"
            ),
            telegram_enabled=tg_enabled,
            telegram_bot_token=tg_token,
            telegram_chat_ids=tg_chats,
            telegram_api_base_url=(
                _get("TELEGRAM_API_BASE_URL") or "https://api.telegram.org"
            ).rstrip("/"),
            telegram_proxy_url=tg_proxy,
            telegram_auto_subscribe=_parse_bool(_get("TELEGRAM_AUTO_SUBSCRIBE"), False),
            telegram_invite_code=(_get("TELEGRAM_INVITE_CODE") or "").strip(),
            windows_notification_enabled=win_enabled,
            windows_notification_sound=_parse_bool(_get("WINDOWS_NOTIFICATION_SOUND"), True),
            log_level=(_get("LOG_LEVEL") or "INFO").strip().upper(),
            security_warnings=warnings,
        )

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "AppConfig":
        return cls.load(env_path=env_file)
