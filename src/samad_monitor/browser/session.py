"""مدیریت امن نشست محلی کاربر (Playwright storage_state + پروفایل مرورگر)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("samad_monitor.session")

# پیکربندی رسمی مشاهده‌شده دانشگاه شهید بهشتی در samad.app
SBU_DEFAULT_UNIVERSITY_OBJECT: Dict[str, Any] = {
    "unikey": "SBU",
    "universityLabel": "دانشگاه شهید بهشتی",
    "universityURL": "https://dining.sbu.ac.ir",
    "casUrl": "https://dining.sbu.ac.ir/index.rose?sharifOauthRedirect=true",
    "autoLogoutTime": -1,
    "enableCaptcha": -1,
    "onPremiseUrl": "",
    "services": {"washer": 0, "booklet": 1, "feedbackProjectId": 183, "help_desk": ""},
    "samad": {
        "nurture": 1,
        "faceEnrollment": 1,
        "faceEnrollmentType": "video",
        "faceEnrollmentURL": "https://dining-face.sbu.ac.ir:8181",
        "dormitory": 0,
        "dormitoryVersion": 1,
        "sport": 0,
        "transport": 0,
        "forceupdate": 0,
        "QRscanner": 1,
        "teammate": 1,
        "cvv2": 1,
        "lastsecond": 1,
        "Attendance": 0,
        "Survey": 0,
        "ActivityReport": 1,
        "help_desk": "",
        "Clinics": "",
    },
}


def decode_jwt_claims(token: str) -> Dict[str, Any]:
    """استخراج ایمن payload توکن JWT بدون نیاز به کتابخانه خارجی (صرفاً جهت بررسی انقضا و نام کاربری)."""
    if not token or "." not in token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        decoded_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        data = json.loads(decoded_bytes.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _unwrap_json_string(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    text = raw.strip()
    if not text or text == "null":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def restrict_path_permissions(path: Path, is_dir: bool = False) -> None:
    """محدودسازی سطح دسترسی فایل/پوشه نشست فقط به کاربر فعلی سیستم‌عامل."""
    if not path.exists():
        return
    try:
        if sys.platform == "win32":
            username = os.environ.get("USERNAME")
            if username:
                subprocess.run(
                    [
                        "icacls",
                        str(path),
                        "/inheritance:r",
                        "/grant:r",
                        f"{username}:(OI)(CI)F" if is_dir else f"{username}:F",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
        else:
            os.chmod(path, 0o700 if is_dir else 0o600)
    except Exception as exc:
        logger.debug("امکان اعمال محدودیت سطح دسترسی فایل روی %s نبود: %s", path, exc)


@dataclass
class SamadSessionData:
    """داده‌های استخراج‌شده از نشست محلی ذخیره‌شده کاربر."""

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    university_key: Optional[str] = None
    university_label: Optional[str] = None
    university_url: Optional[str] = None
    release_date: Optional[str] = None
    username: Optional[str] = None
    access_token_exp: Optional[int] = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    @property
    def is_access_token_expired(self) -> bool:
        if not self.access_token:
            return True
        if self.access_token_exp is None:
            return False
        return int(time.time()) >= (self.access_token_exp - 30)

    @property
    def fingerprint(self) -> str:
        """شناسه کوتاه غیرحساس از نشست فعلی برای جلوگیری از ارسال تکراری هشدار انقضای نشست."""
        seed = self.refresh_token or self.access_token or "no-session"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


class SessionManager:
    """مدیریت خواندن، اعتبارسنجی، به‌روزرسانی و حفاظت از نشست مرورگر سماد."""

    def __init__(
        self,
        storage_state_path: Path,
        browser_profile_dir: Path,
        samad_web_url: str = "https://samad.app",
        expected_university_key: str = "SBU",
        default_university_api_url: str = "https://dining.sbu.ac.ir",
    ) -> None:
        self.storage_state_path = storage_state_path
        self.browser_profile_dir = browser_profile_dir
        self.samad_web_url = samad_web_url.rstrip("/")
        self.expected_university_key = expected_university_key.upper()
        self.default_university_api_url = default_university_api_url.rstrip("/")

    def _origin_match(self, origin: str) -> bool:
        try:
            p1 = urlparse(origin)
            p2 = urlparse(self.samad_web_url)
            return p1.netloc.lower() == p2.netloc.lower()
        except Exception:
            return origin.rstrip("/") == self.samad_web_url

    def exists(self) -> bool:
        return self.storage_state_path.is_file()

    def load(self) -> SamadSessionData:
        """بارگذاری اطلاعات نشست از فایل `storage_state.json`."""
        if not self.storage_state_path.is_file():
            return SamadSessionData(
                university_key=self.expected_university_key,
                university_url=self.default_university_api_url,
            )

        try:
            raw = json.loads(self.storage_state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("خطا در خواندن فایل نشست %s: %s", self.storage_state_path, exc)
            return SamadSessionData()

        origins = raw.get("origins") if isinstance(raw, dict) else []
        local_map: Dict[str, str] = {}
        if isinstance(origins, list):
            for entry in origins:
                if not isinstance(entry, dict):
                    continue
                origin_url = str(entry.get("origin") or "")
                if self._origin_match(origin_url) or not local_map:
                    for item in entry.get("localStorage") or []:
                        if isinstance(item, dict) and "name" in item and "value" in item:
                            local_map[str(item["name"])] = str(item["value"])
                    if self._origin_match(origin_url):
                        break

        access_val = _unwrap_json_string(local_map.get("TOKEN"))
        refresh_val = _unwrap_json_string(local_map.get("REFRESH_TOKEN"))
        uni_val = _unwrap_json_string(local_map.get("UNIVERSITY"))
        release_val = local_map.get("releaseDate")
        if release_val:
            unwrapped_rel = _unwrap_json_string(release_val)
            release_val = str(unwrapped_rel) if unwrapped_rel is not None else None

        access_token = str(access_val).strip() if isinstance(access_val, str) and access_val.strip() else None
        refresh_token = (
            str(refresh_val).strip() if isinstance(refresh_val, str) and refresh_val.strip() else None
        )

        uni_key = self.expected_university_key
        uni_label = "دانشگاه شهید بهشتی"
        uni_url = self.default_university_api_url

        if isinstance(uni_val, dict):
            uni_key = str(uni_val.get("unikey") or uni_key).upper()
            uni_label = str(uni_val.get("universityLabel") or uni_label)
            raw_u_url = uni_val.get("universityURL")
            if isinstance(raw_u_url, list) and raw_u_url:
                uni_url = str(raw_u_url[0]).rstrip("/")
            elif isinstance(raw_u_url, str) and raw_u_url.strip():
                uni_url = raw_u_url.strip().rstrip("/")

        claims = decode_jwt_claims(access_token or "")
        username = str(claims.get("user_name") or "").strip() or None
        exp_raw = claims.get("exp")
        try:
            exp_int = int(exp_raw) if exp_raw is not None else None
        except (TypeError, ValueError):
            exp_int = None

        return SamadSessionData(
            access_token=access_token,
            refresh_token=refresh_token,
            university_key=uni_key,
            university_label=uni_label,
            university_url=uni_url,
            release_date=release_val,
            username=username,
            access_token_exp=exp_int,
        )

    def save_tokens(
        self,
        access_token: str,
        refresh_token: Optional[str] = None,
        release_date: Optional[str] = None,
        university_obj: Optional[Dict[str, Any]] = None,
    ) -> SamadSessionData:
        """ذخیره اتمیک توکن‌های تازه یا تمدیدشده در `storage_state.json` با حفظ کوکی‌ها."""
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        state: Dict[str, Any] = {"cookies": [], "origins": []}
        if self.storage_state_path.is_file():
            try:
                loaded = json.loads(self.storage_state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state = loaded
            except Exception:
                pass

        origins = state.setdefault("origins", [])
        target_origin: Optional[Dict[str, Any]] = None
        for entry in origins:
            if isinstance(entry, dict) and self._origin_match(str(entry.get("origin") or "")):
                target_origin = entry
                break

        if target_origin is None:
            target_origin = {"origin": self.samad_web_url, "localStorage": []}
            origins.append(target_origin)

        ls_list = target_origin.setdefault("localStorage", [])
        ls_dict: Dict[str, str] = {
            str(item["name"]): str(item["value"])
            for item in ls_list
            if isinstance(item, dict) and "name" in item and "value" in item
        }

        ls_dict["TOKEN"] = json.dumps(access_token, ensure_ascii=False)
        if refresh_token:
            ls_dict["REFRESH_TOKEN"] = json.dumps(refresh_token, ensure_ascii=False)
        if release_date:
            ls_dict["releaseDate"] = str(release_date)
        if university_obj is not None:
            ls_dict["UNIVERSITY"] = json.dumps(university_obj, ensure_ascii=False)
        elif "UNIVERSITY" not in ls_dict:
            ls_dict["UNIVERSITY"] = json.dumps(SBU_DEFAULT_UNIVERSITY_OBJECT, ensure_ascii=False)

        target_origin["localStorage"] = [{"name": k, "value": v} for k, v in ls_dict.items()]

        tmp_path = self.storage_state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        restrict_path_permissions(tmp_path, is_dir=False)
        tmp_path.replace(self.storage_state_path)
        restrict_path_permissions(self.storage_state_path, is_dir=False)
        return self.load()

    def clear_tokens_on_expiry(self) -> None:
        """پاک کردن توکن‌های نامعتبر پس از انقضای قطعی نشست بدون حذف تاریخچه دیتابیس."""
        if not self.storage_state_path.is_file():
            return
        try:
            state = json.loads(self.storage_state_path.read_text(encoding="utf-8"))
            for entry in state.get("origins") or []:
                if isinstance(entry, dict) and isinstance(entry.get("localStorage"), list):
                    entry["localStorage"] = [
                        item
                        for item in entry["localStorage"]
                        if isinstance(item, dict)
                        and item.get("name") not in {"TOKEN", "REFRESH_TOKEN"}
                    ]
            self.storage_state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            restrict_path_permissions(self.storage_state_path, is_dir=False)
        except Exception as exc:
            logger.debug("خطا هنگام پاک‌سازی توکن منقضی‌شده: %s", exc)
