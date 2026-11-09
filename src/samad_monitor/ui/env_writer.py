"""خواندن و به‌روزرسانی ایمن و اتمیک فایل .env از طریق پنل گرافیکی بدون افشا یا ذخیره رمز عبور."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from samad_monitor.browser.session import restrict_path_permissions
from samad_monitor.config import FORBIDDEN_CREDENTIAL_KEYS

ALLOWED_UI_ENV_KEYS = {
    "SAMAD_FETCH_STRATEGY",
    "SAMAD_SELF_IDS",
    "SAMAD_SELF_NAME_FILTER",
    "SAMAD_IGNORE_ENV_PROXY",
    "SAMAD_PROXY_URL",
    "CHECK_INTERVAL_NORMAL_MINUTES",
    "CHECK_INTERVAL_PEAK_MINUTES",
    "CHECK_JITTER_SECONDS",
    "TELEGRAM_ENABLED",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_API_BASE_URL",
    "TELEGRAM_PROXY_URL",
    "TELEGRAM_AUTO_SUBSCRIBE",
    "TELEGRAM_INVITE_CODE",
    "WINDOWS_NOTIFICATION_ENABLED",
    "WINDOWS_NOTIFICATION_SOUND",
    "LOG_LEVEL",
}

_KEY_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _sanitize_env_value(val: str) -> str:
    """حذف کاراکترهای خط جدید برای جلوگیری از تزریق کلید اضافی در فایل .env."""
    cleaned = str(val or "").replace("\r", "").replace("\n", "").strip()
    return cleaned


def update_env_file(
    env_path: Path,
    updates: Dict[str, str],
    template_path: Path | None = None,
) -> None:
    """
    به‌روزرسانی اتمیک کلیدهای مجاز در فایل `.env` با حفظ کامنت‌ها و ترتیب خطوط.
    کلیدهای ممنوعه رمز عبور سماد تحت هیچ شرایطی پذیرفته نمی‌شوند.
    """
    for k in updates:
        upper_k = k.strip().upper()
        if upper_k in FORBIDDEN_CREDENTIAL_KEYS:
            raise ValueError(f"ذخیره کلید رمز عبور ({upper_k}) اکیداً ممنوع است.")
        if upper_k not in ALLOWED_UI_ENV_KEYS:
            raise ValueError(f"کلید غیرمجاز برای ویرایش از پنل: {upper_k}")

    normalized_updates = {
        k.strip().upper(): _sanitize_env_value(v) for k, v in updates.items()
    }

    lines: List[str] = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    elif template_path and template_path.is_file():
        lines = template_path.read_text(encoding="utf-8").splitlines()

    seen_keys: set[str] = set()
    output_lines: List[str] = []

    for line in lines:
        match = _KEY_LINE_RE.match(line)
        if not match:
            output_lines.append(line)
            continue

        key = match.group(1).upper()
        if key in FORBIDDEN_CREDENTIAL_KEYS:
            continue

        if key in normalized_updates:
            output_lines.append(f"{key}={normalized_updates[key]}")
            seen_keys.add(key)
        else:
            output_lines.append(line)

    missing = [k for k in normalized_updates if k not in seen_keys]
    if missing:
        if output_lines and output_lines[-1].strip() != "":
            output_lines.append("")
        output_lines.append("# --- تنظیمات ذخیره‌شده از پنل گرافیکی ---")
        for k in missing:
            output_lines.append(f"{k}={normalized_updates[k]}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = env_path.with_suffix(".env.tmp")
    tmp_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    restrict_path_permissions(tmp_path, is_dir=False)
    tmp_path.replace(env_path)
    restrict_path_permissions(env_path, is_dir=False)
