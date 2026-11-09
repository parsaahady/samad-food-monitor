"""
کلاینت پایش سامانه سماد دانشگاه شهید بهشتی:
۱) ورود اولیه دستی و تعاملی در مرورگر واقعی (Playwright Persistent Context)
۲) واکشی فقط‌خواندنی از API رسمی مشاهده‌شده سماد با تمدید خودکار Refresh Token
۳) استخراج پشتیبان با مرورگر Playwright Headless (رهگیری Network + استخراج DOM)
۴) محافظ سخت‌گیرانه عدم انجام رزرو، لغو یا پرداخت (ReadOnlyGuard)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from samad_monitor.browser.session import (
    SBU_DEFAULT_UNIVERSITY_OBJECT,
    SamadSessionData,
    SessionManager,
    restrict_path_permissions,
)
from samad_monitor.config import AppConfig
from samad_monitor.models import (
    CheckStatus,
    FetchResult,
    Meal,
    SelfServiceInfo,
    WeekRange,
)
from samad_monitor.parser import (
    IncompleteMenuDataError,
    RawDomCard,
    SiteStructureChangedError,
    parse_samad_dom_cards,
    parse_samad_envelope,
    parse_samad_programs_payload,
    sort_and_deduplicate_meals,
)
from samad_monitor.utils.date_utils import format_samad_query_date
from samad_monitor.utils.retry import NonRetryableError, RateLimiter, retry_call

logger = logging.getLogger("samad_monitor.client")

# هدر احراز هویت کلاینت رسمی وب/موبایل سماد برای تمدید توکن نشست (مشاهده‌شده در http.tsx سورس samad.app)
SAMAD_OAUTH_CLIENT_BASIC = "Basic c2FtYWQtbW9iaWxlOnNhbWFkLW1vYmlsZS1zZWNyZXQ="

# مرزهای نسخه بک‌اند سماد (مشاهده‌شده در self.service.ts و reserve.service.ts نسخه 7.3 سماد)
SELFS_V2_RELEASE_THRESHOLD = "250908183211"
PROGRAMS_V2_NEW_PATH_THRESHOLD = "251026134000"

# مسیرهای ممنوعه تغییردهنده که تحت هیچ شرایطی اجازه فراخوانی ندارند (بند ۱۵ مشخصات پروژه)
FORBIDDEN_MUTATING_PATH_FRAGMENTS = (
    "/reserve",
    "/sell-reserve",
    "/cancel-sold-reserve",
    "/change-self",
    "/credit-transfers",
    "/epayment",
    "/process-payment",
    "/daily-sell",
    "/votes",
)


class ReadOnlyGuardError(NonRetryableError):
    """خطای امنیتی هنگام تلاش برای ارسال هرگونه درخواست تغییردهنده (رزرو/لغو/پرداخت)."""


class SessionExpiredException(NonRetryableError):
    """انقضای نشست کاربری که نیاز به ورود دستی مجدد دارد."""


class TransientFetchException(Exception):
    """خطای موقت شبکه یا سرور که قابل تلاش مجدد با Exponential Backoff است."""

    def __init__(self, message: str, check_status: CheckStatus, http_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.check_status = check_status
        self.http_status = http_status


def enforce_read_only_request(method: str, url_or_path: str, form_body: Optional[Dict[str, str]] = None) -> None:
    """
    تضمین مطلق عدم انجام رزرو خودکار، لغو رزرو یا پرداخت.
    تنها درخواست‌های `GET` خواندنی و درخواست تمدید توکن نشست (`POST /oauth/token` با `refresh_token`) مجازند.
    """
    upper_method = method.strip().upper()
    parsed = urlparse(url_or_path)
    path = (parsed.path or url_or_path).lower()

    if upper_method == "GET":
        # حتی در GET نیز مسیرهای پرداخت/لاگین‌توکن درگاه بانکی مجاز نیستند
        if "/j_security_check" in path or "/login-token" in path:
            raise ReadOnlyGuardError(f"مسیر حساس غیرپایش مسدود شد: GET {path}")
        return

    if upper_method == "POST" and path.endswith("/oauth/token"):
        grant_type = (form_body or {}).get("grant_type", "")
        if grant_type == "refresh_token":
            return
        raise ReadOnlyGuardError(
            f"ارسال POST به /oauth/token فقط برای تمدید refresh_token مجاز است (دریافت‌شده: {grant_type!r})."
        )

    for frag in FORBIDDEN_MUTATING_PATH_FRAGMENTS:
        if frag in path:
            raise ReadOnlyGuardError(
                f"عملیات رزرو/لغو/پرداخت اکیداً ممنوع است! درخواست مسدود شد: {upper_method} {path}"
            )

    raise ReadOnlyGuardError(
        f"سیستم پایش فقط‌خواندنی است؛ متد تغییردهنده مسدود شد: {upper_method} {path}"
    )


def _is_release_after(release_date: Optional[str], threshold: str) -> bool:
    """مقایسه رشته تاریخ انتشار بک‌اند سماد با قالب 12 رقمی YYMMDDHHmmss."""
    if not release_date or len(release_date) != 12 or not release_date.isdigit():
        return True  # در صورت عدم دسترسی به release، ابتدا مسیر جدید را امتحان و در 404 به مسیر قدیم برمی‌گردیم
    return release_date >= threshold


def resolve_browser_launch_options(
    playwright_instance: Any,
    platform_override: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    تشخیص هوشمند باینری مرورگر برای Playwright (مخصوص دور زدن تحریم ۴۰۳ دانلود کرومیوم در ایران):
    ۱. مسیر سفارشی متغیر محیطی SAMAD_BROWSER_EXECUTABLE_PATH (در صورت تنظیم)
    ۲. باینری داخلی Playwright Chromium (در صورت نصب بودن)
    ۳. مرورگرهای نصب‌شده روی سیستم‌عامل (Google Chrome یا Microsoft Edge پیش‌فرض ویندوز ۱۱)
    """
    custom_env = (os.environ.get("SAMAD_BROWSER_EXECUTABLE_PATH") or "").strip()
    if custom_env and Path(custom_env).is_file():
        return ({"executable_path": custom_env}, f"مرورگر سفارشی ({Path(custom_env).name})")

    try:
        bundled = Path(playwright_instance.chromium.executable_path)
        if bundled.is_file():
            return ({}, f"Playwright Chromium ({bundled.name})")
    except Exception:
        pass

    plat = platform_override or sys.platform
    if plat == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_app = os.environ.get("LocalAppData", "")

        win_candidates = [
            (Path(pf) / "Google/Chrome/Application/chrome.exe", "Google Chrome"),
            (Path(pf86) / "Google/Chrome/Application/chrome.exe", "Google Chrome"),
            (Path(local_app) / "Google/Chrome/Application/chrome.exe", "Google Chrome"),
            (Path(pf86) / "Microsoft/Edge/Application/msedge.exe", "Microsoft Edge (بومی ویندوز ۱۱)"),
            (Path(pf) / "Microsoft/Edge/Application/msedge.exe", "Microsoft Edge (بومی ویندوز ۱۱)"),
            (Path(local_app) / "Microsoft/Edge/Application/msedge.exe", "Microsoft Edge"),
            (Path(pf) / "BraveSoftware/Brave-Browser/Application/brave.exe", "Brave Browser"),
        ]
        for cand_path, label in win_candidates:
            if str(cand_path) and cand_path.is_file():
                return ({"executable_path": str(cand_path)}, label)
    else:
        for bin_name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            found = shutil.which(bin_name)
            if found:
                return ({"executable_path": found}, f"مرورگر سیستم ({bin_name})")

    return (None, "")


class SamadClient:
    """کلاینت یکپارچه تعامل امن و فقط‌خواندنی با سامانه سماد دانشگاه شهید بهشتی."""

    def __init__(
        self,
        config: AppConfig,
        session_manager: Optional[SessionManager] = None,
        http_transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.config = config
        self.session_manager = session_manager or SessionManager(
            storage_state_path=config.storage_state_path,
            browser_profile_dir=config.browser_profile_dir,
            samad_web_url=config.samad_web_url,
            expected_university_key=config.samad_university_key,
            default_university_api_url=config.samad_university_api_url,
        )
        self.rate_limiter = RateLimiter(min_interval_seconds=config.request_min_interval_seconds)
        self._http_transport = http_transport

    def _build_http_client(self, base_url: str) -> httpx.Client:
        kwargs: Dict[str, Any] = {
            "base_url": base_url.rstrip("/"),
            "timeout": httpx.Timeout(self.config.http_timeout_seconds),
            "trust_env": not self.config.samad_ignore_env_proxy,
            "follow_redirects": False,
            "headers": {
                "Accept": "application/json, text/plain, */*",
                "Cache-Control": "no-cache",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            },
        }
        if self._http_transport is not None:
            kwargs["transport"] = self._http_transport
        elif self.config.samad_proxy_url:
            kwargs["proxy"] = self.config.samad_proxy_url
        return httpx.Client(**kwargs)

    # ------------------------------------------------------------------
    # مرحله ۱: ورود اولیه دستی و تعاملی توسط خود کاربر در مرورگر واقعی
    # ------------------------------------------------------------------
    def interactive_login(self) -> SamadSessionData:
        """
        باز کردن مرورگر واقعی Chromium با پروفایل محلی تا دانشجو خودش وارد سامانه سماد شود.
        هیچ نام کاربری یا رمز عبوری توسط برنامه دریافت یا ثبت نمی‌شود.
        """
        if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        ):
            raise RuntimeError(
                "محیط گرافیکی دسکتاپ (DISPLAY) فعال نیست. طبق اصول امنیتی پروژه، ورود دستی به سماد "
                "باید روی کامپیوتر محلی کاربر (Windows 11 یا دسکتاپ لینوکس) انجام شود و نباید اطلاعات "
                "ورود را روی سرور ابری/بدون گرافیک وارد کرد."
            )

        from playwright.sync_api import sync_playwright

        self.config.ensure_directories()
        restrict_path_permissions(self.config.browser_profile_dir, is_dir=True)

        sbu_uni_json = json.dumps(
            {
                **SBU_DEFAULT_UNIVERSITY_OBJECT,
                "unikey": self.config.samad_university_key,
                "universityLabel": self.config.samad_university_label,
                "universityURL": self.config.samad_university_api_url,
            },
            ensure_ascii=False,
        )

        logger.info(
            "مرورگر برای ورود دستی به سماد (%s) باز می‌شود. لطفاً وارد حساب دانشجویی خود شوید...",
            self.config.samad_university_label,
        )

        with sync_playwright() as p:
            browser_extra, browser_label = resolve_browser_launch_options(p)
            if browser_extra is None:
                raise RuntimeError(
                    "هیچ مرورگر سازگاری (Chrome / Edge / Playwright Chromium) روی سیستم یافت نشد. "
                    "روی ویندوز ۱۱ مرورگر Microsoft Edge یا Google Chrome را نصب کنید یا با روشن کردن پروکسی، "
                    "دستور `playwright install chromium` را اجرا نمایید."
                )
            logger.info("استفاده از موتور مرورگر: %s", browser_label)

            launch_kwargs: Dict[str, Any] = {
                **browser_extra,
                "user_data_dir": str(self.config.browser_profile_dir),
                "headless": False,
                "viewport": {"width": 1280, "height": 820},
                "locale": "fa-IR",
                "timezone_id": self.config.timezone_name,
            }
            if self.config.samad_proxy_url:
                launch_kwargs["proxy"] = {"server": self.config.samad_proxy_url}

            context = p.chromium.launch_persistent_context(**launch_kwargs)
            try:
                page = context.pages[0] if context.pages else context.new_page()

                # پیش‌انتخاب دانشگاه شهید بهشتی در localStorage در صورتی که قبلاً تنظیم نشده باشد
                page.add_init_script(
                    f"""
                    try {{
                        if (window.location.origin === {json.dumps(self.config.samad_web_url)}) {{
                            if (!window.localStorage.getItem('UNIVERSITY')) {{
                                window.localStorage.setItem('UNIVERSITY', {json.dumps(sbu_uni_json)});
                            }}
                        }}
                    }} catch (e) {{}}
                    """
                )

                page.goto(self.config.samad_reserve_url, wait_until="domcontentloaded", timeout=60_000)

                deadline = time.monotonic() + self.config.login_timeout_seconds
                authenticated = False

                while time.monotonic() < deadline:
                    if context.pages == []:
                        raise RuntimeError("پنجره مرورگر پیش از تکمیل ورود توسط کاربر بسته شد.")

                    active_page = context.pages[-1]
                    current_url = active_page.url or ""
                    parsed_url = urlparse(current_url)
                    samad_host = urlparse(self.config.samad_web_url).netloc.lower()

                    if parsed_url.netloc.lower() == samad_host and "/login" not in parsed_url.path:
                        try:
                            ls_snapshot = active_page.evaluate(
                                """() => ({
                                    token: window.localStorage.getItem('TOKEN'),
                                    refresh: window.localStorage.getItem('REFRESH_TOKEN'),
                                    uni: window.localStorage.getItem('UNIVERSITY'),
                                    rel: window.localStorage.getItem('releaseDate')
                                })"""
                            )
                        except Exception:
                            ls_snapshot = None

                        if isinstance(ls_snapshot, dict) and ls_snapshot.get("token"):
                            token_str = str(ls_snapshot["token"]).strip('"').strip()
                            if len(token_str) > 20 and token_str != "null":
                                authenticated = True
                                break

                    active_page.wait_for_timeout(1000)

                if not authenticated:
                    raise TimeoutError(
                        f"مهلت زمانی ورود دستی ({self.config.login_timeout_seconds} ثانیه) به پایان رسید."
                    )

                # صبر کوتاه برای تکمیل ذخیره‌سازی کوکی‌ها و releaseDate در مرورگر
                page.wait_for_timeout(1500)
                context.storage_state(path=str(self.config.storage_state_path))
                restrict_path_permissions(self.config.storage_state_path, is_dir=False)
                restrict_path_permissions(self.config.browser_profile_dir, is_dir=True)
            finally:
                context.close()

        session_data = self.session_manager.load()
        if (
            session_data.university_key
            and session_data.university_key.upper() != self.config.samad_university_key.upper()
        ):
            logger.warning(
                "هشدار: دانشگاه انتخاب‌شده در نشست (%s) با دانشگاه هدف (%s - %s) متفاوت است!",
                session_data.university_key,
                self.config.samad_university_key,
                self.config.samad_university_label,
            )

        logger.info("نشست کاربری با موفقیت در مسیر محافظت‌شده محلی ذخیره شد.")
        return session_data

    # ------------------------------------------------------------------
    # مرحله ۲: عملیات شبکه فقط‌خواندنی و تمدید توکن نشست
    # ------------------------------------------------------------------
    def _safe_get_json(
        self,
        client: httpx.Client,
        path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        bearer_token: Optional[str] = None,
    ) -> httpx.Response:
        enforce_read_only_request("GET", path)
        self.rate_limiter.wait()

        headers: Dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"

        def _do_request() -> httpx.Response:
            try:
                resp = client.get(path, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                raise TransientFetchException(
                    "مهلت اتصال به سرور سماد به پایان رسید (احتمال کندی شبکه یا فعال بودن VPN غیرایرانی).",
                    CheckStatus.NETWORK_ERROR,
                ) from exc
            except httpx.ConnectError as exc:
                raise TransientFetchException(
                    "امکان برقراری ارتباط با سرور سماد وجود ندارد (قطعی اینترنت یا عدم دسترسی شبکه).",
                    CheckStatus.NETWORK_ERROR,
                ) from exc
            except httpx.HTTPError as exc:
                raise TransientFetchException(
                    f"خطای شبکه هنگام ارتباط با سرور سماد: {exc}",
                    CheckStatus.NETWORK_ERROR,
                ) from exc

            if resp.status_code in {502, 503, 504}:
                raise TransientFetchException(
                    f"سرور سماد موقتاً در دسترس نیست (کد HTTP {resp.status_code}).",
                    CheckStatus.SITE_UNREACHABLE,
                    http_status=resp.status_code,
                )
            if resp.status_code >= 500:
                raise TransientFetchException(
                    f"خطای داخلی سرور سماد (کد HTTP {resp.status_code}).",
                    CheckStatus.SERVER_ERROR,
                    http_status=resp.status_code,
                )
            return resp

        return retry_call(
            _do_request,
            max_attempts=self.config.max_retry_attempts,
            base_delay=self.config.retry_base_delay_seconds,
            retryable_exceptions=(TransientFetchException,),
            non_retryable_exceptions=(NonRetryableError,),
            operation_name=f"GET {path}",
        )

    def _refresh_access_token(
        self, client: httpx.Client, session: SamadSessionData
    ) -> SamadSessionData:
        """تمدید خودکار توکن دسترسی با استفاده از refresh_token ذخیره‌شده در نشست."""
        if not session.refresh_token:
            self.session_manager.clear_tokens_on_expiry()
            raise SessionExpiredException("توکن تمدید (refresh_token) وجود ندارد؛ ورود مجدد الزامی است.")

        form = {"grant_type": "refresh_token", "refresh_token": session.refresh_token}
        enforce_read_only_request("POST", "/oauth/token", form_body=form)
        self.rate_limiter.wait()

        try:
            resp = client.post(
                "/oauth/token",
                data=form,
                headers={
                    "Authorization": SAMAD_OAUTH_CLIENT_BASIC,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
            )
        except httpx.HTTPError as exc:
            raise TransientFetchException(
                f"خطای شبکه هنگام تمدید توکن نشست: {exc}",
                CheckStatus.NETWORK_ERROR,
            ) from exc

        if resp.status_code in {400, 401, 403}:
            self.session_manager.clear_tokens_on_expiry()
            raise SessionExpiredException("نشست کاربری سماد منقضی شده است. لطفاً مجدداً وارد شوید.")

        if resp.status_code >= 500:
            raise TransientFetchException(
                f"خطای سرور هنگام تمدید توکن نشست ({resp.status_code}).",
                CheckStatus.SERVER_ERROR,
                http_status=resp.status_code,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SiteStructureChangedError("پاسخ نامعتبر هنگام تمدید توکن نشست دریافت شد.") from exc

        new_access = str(payload.get("access_token") or "").strip()
        if not new_access:
            self.session_manager.clear_tokens_on_expiry()
            raise SessionExpiredException("سرور توکن جدیدی صادر نکرد؛ ورود مجدد الزامی است.")

        new_refresh = str(payload.get("refresh_token") or session.refresh_token).strip()
        logger.info("توکن دسترسی نشست سماد با موفقیت تمدید شد.")
        return self.session_manager.save_tokens(
            access_token=new_access,
            refresh_token=new_refresh,
            release_date=session.release_date,
        )

    def _get_authenticated_json(
        self,
        client: httpx.Client,
        path: str,
        session: SamadSessionData,
        params: Optional[Dict[str, str]] = None,
    ) -> Tuple[httpx.Response, Any, SamadSessionData]:
        """اجرای درخواست GET احراز هویت‌شده با پشتیبانی از تمدید یک‌باره در صورت دریافت 401."""
        if not session.access_token or session.is_access_token_expired:
            if session.refresh_token:
                session = self._refresh_access_token(client, session)
            elif not session.access_token:
                raise SessionExpiredException("نشست فعالی یافت نشد.")

        resp = self._safe_get_json(client, path, params=params, bearer_token=session.access_token)
        if resp.status_code == 401:
            session = self._refresh_access_token(client, session)
            resp = self._safe_get_json(client, path, params=params, bearer_token=session.access_token)
            if resp.status_code == 401:
                self.session_manager.clear_tokens_on_expiry()
                raise SessionExpiredException("نشست کاربری منقضی شده است (کد ۴۰۱).")

        if resp.status_code in {301, 302, 303, 307, 308}:
            location = resp.headers.get("Location", "")
            if "login" in location.lower():
                self.session_manager.clear_tokens_on_expiry()
                raise SessionExpiredException("هدایت به صفحه ورود تشخیص داده شد؛ نشست منقضی شده است.")

        content_type = resp.headers.get("content-type", "").lower()
        try:
            data = resp.json()
        except ValueError as exc:
            body_preview = (resp.text or "")[:200].lower()
            if "<html" in body_preview or "<!doctype" in body_preview or "text/html" in content_type:
                if "login" in body_preview or resp.status_code in {401, 403}:
                    raise SessionExpiredException("پاسخ صفحه ورود به جای JSON دریافت شد؛ نشست منقضی است.") from exc
                raise SiteStructureChangedError(
                    "به جای پاسخ JSON API، صفحه HTML غیرمنتظره دریافت شد."
                ) from exc
            raise SiteStructureChangedError("پاسخ سرور سماد فرمت JSON معتبر ندارد.") from exc

        return resp, data, session

    def _filter_food_selfs(
        self, raw_selfs: Any, ignore_user_filter: bool = False
    ) -> List[SelfServiceInfo]:
        """استخراج سلف‌های تغذیه (`NORMAL`) و اعمال فیلتر اختیاری کاربر."""
        if not isinstance(raw_selfs, list):
            raise SiteStructureChangedError("ساختار لیست سلف‌ها آرایه معتبر نیست.")

        food_selfs: List[SelfServiceInfo] = []
        for item in raw_selfs:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            raw_type = str(item.get("type") or "NORMAL").strip().upper()
            if raw_type == "":
                raw_type = "NORMAL"
            if raw_type != "NORMAL":
                continue

            try:
                self_id = int(item["id"])
            except (TypeError, ValueError):
                continue

            name = " ".join(str(item.get("name") or f"سلف {self_id}").strip().split())
            norm_name = name.replace("ي", "ی").replace("ك", "ک")
            # در بین خوابگاه‌های دانشگاه شهید بهشتی، فقط «خوابگاه کوی» مدنظر است (مگر اینکه کاربر فیلتر دستی داده باشد)
            has_custom_filter = bool(
                not ignore_user_filter
                and (self.config.samad_self_ids or self.config.samad_self_name_filter)
            )
            if (
                not has_custom_filter
                and ("خوابگاه" in norm_name or "سرا" in norm_name)
                and "کوی" not in norm_name
                and "مرکزی" not in norm_name
            ):
                continue

            free_rest = bool(item.get("freeRestaurant"))
            food_selfs.append(
                SelfServiceInfo(
                    id=self_id,
                    name=name,
                    self_type=raw_type,
                    free_restaurant=free_rest,
                )
            )

        if not ignore_user_filter:
            if self.config.samad_self_ids:
                allowed_ids = set(self.config.samad_self_ids)
                food_selfs = [s for s in food_selfs if s.id in allowed_ids]

            if self.config.samad_self_name_filter:
                food_selfs = [
                    s
                    for s in food_selfs
                    if any(tok in s.name for tok in self.config.samad_self_name_filter)
                ]

        return food_selfs

    def _fetch_via_api(
        self, week_range: WeekRange, ignore_user_self_filter: bool = False
    ) -> FetchResult:
        """واکشی برنامه غذایی هفته آینده از طریق API رسمی مشاهده‌شده سامانه سماد."""
        session = self.session_manager.load()
        if not session.has_credentials:
            return FetchResult(
                status=CheckStatus.SESSION_EXPIRED,
                week_range=week_range,
                source="api",
                message="هیچ نشست فعالی یافت نشد. ابتدا دستور `python -m samad_monitor login` را اجرا کنید.",
            )

        base_url = session.university_url or self.config.samad_university_api_url

        try:
            with self._build_http_client(base_url) as client:
                # ۱) دریافت releaseTime سرور در صورت نبودن در نشست
                release_date = session.release_date
                if not release_date:
                    try:
                        rel_resp = self._safe_get_json(client, "/rest/config/release")
                        if rel_resp.status_code == 200:
                            rel_json = rel_resp.json()
                            rel_payload = rel_json.get("payload") if isinstance(rel_json, dict) else None
                            if isinstance(rel_payload, dict) and rel_payload.get("releaseTime"):
                                release_date = str(rel_payload["releaseTime"])
                                if session.access_token:
                                    session = self.session_manager.save_tokens(
                                        access_token=session.access_token,
                                        refresh_token=session.refresh_token,
                                        release_date=release_date,
                                    )
                    except Exception as exc:
                        logger.debug("عدم دریافت release از سرور (ادامه با تشخیص خودکار مسیر): %s", exc)

                # ۲) بررسی صحت نشست کاربری (/rest/users/me)
                me_resp, me_json, session = self._get_authenticated_json(
                    client, "/rest/users/me", session
                )
                me_status, me_payload, me_msg, me_code = parse_samad_envelope(me_json)
                if me_status == CheckStatus.SESSION_EXPIRED:
                    self.session_manager.clear_tokens_on_expiry()
                    return FetchResult(
                        status=CheckStatus.SESSION_EXPIRED,
                        week_range=week_range,
                        source="api",
                        message=me_msg or "نشست کاربری منقضی شده است.",
                        http_status=me_resp.status_code,
                        samad_code=me_code,
                    )
                if me_status not in {CheckStatus.MENU_PUBLISHED}:
                    return FetchResult(
                        status=me_status,
                        week_range=week_range,
                        source="api",
                        message=me_msg or "خطا در دریافت پروفایل کاربری سماد.",
                        http_status=me_resp.status_code,
                        samad_code=me_code,
                    )

                username = session.username
                if isinstance(me_payload, dict) and me_payload.get("username"):
                    username = str(me_payload["username"])

                # ۳) دریافت لیست سلف‌های تغذیه دانشجو (با fallback خودکار مسیر نسخه قدیم/جدید)
                primary_selfs_ep = (
                    "/rest/reservations/selfs"
                    if _is_release_after(release_date, SELFS_V2_RELEASE_THRESHOLD)
                    else "/rest/selfs"
                )
                fallback_selfs_ep = (
                    "/rest/selfs"
                    if primary_selfs_ep == "/rest/reservations/selfs"
                    else "/rest/reservations/selfs"
                )

                selfs_resp, selfs_json, session = self._get_authenticated_json(
                    client, primary_selfs_ep, session
                )
                if selfs_resp.status_code == 404:
                    selfs_resp, selfs_json, session = self._get_authenticated_json(
                        client, fallback_selfs_ep, session
                    )

                s_status, s_payload, s_msg, s_code = parse_samad_envelope(selfs_json)
                if s_status != CheckStatus.MENU_PUBLISHED:
                    return FetchResult(
                        status=s_status,
                        week_range=week_range,
                        source="api",
                        message=s_msg or "خطا در دریافت فهرست سلف‌های دانشجو.",
                        http_status=selfs_resp.status_code,
                        samad_code=s_code,
                        username=username,
                    )

                food_selfs = self._filter_food_selfs(
                    s_payload, ignore_user_filter=ignore_user_self_filter
                )
                if not food_selfs:
                    return FetchResult(
                        status=CheckStatus.MENU_NOT_PUBLISHED,
                        week_range=week_range,
                        selfs_checked=[],
                        source="api",
                        message="هیچ سلف تغذیه فعالی مطابق فیلتر برای حساب کاربری یافت نشد.",
                        username=username,
                    )

                # ۴) واکشی برنامه غذایی هفته آینده برای هر سلف تغذیه
                primary_prog_ep = (
                    "/rest/reservations/programs/v2"
                    if _is_release_after(release_date, PROGRAMS_V2_NEW_PATH_THRESHOLD)
                    else "/rest/programs/v2"
                )
                fallback_prog_ep = (
                    "/rest/programs/v2"
                    if primary_prog_ep == "/rest/reservations/programs/v2"
                    else "/rest/reservations/programs/v2"
                )

                query_date = format_samad_query_date(week_range.week_start)
                all_meals: List[Meal] = []
                last_samad_code: Optional[int] = None

                for self_info in food_selfs:
                    params = {"selfId": str(self_info.id), "weekStartDate": query_date}
                    prog_resp, prog_json, session = self._get_authenticated_json(
                        client, primary_prog_ep, session, params=params
                    )
                    if prog_resp.status_code == 404:
                        primary_prog_ep = fallback_prog_ep
                        prog_resp, prog_json, session = self._get_authenticated_json(
                            client, primary_prog_ep, session, params=params
                        )

                    p_status, p_payload, p_msg, p_code = parse_samad_envelope(prog_json)
                    last_samad_code = p_code

                    if p_status == CheckStatus.MENU_NOT_PUBLISHED:
                        continue
                    if p_status == CheckStatus.SESSION_EXPIRED:
                        self.session_manager.clear_tokens_on_expiry()
                        return FetchResult(
                            status=CheckStatus.SESSION_EXPIRED,
                            week_range=week_range,
                            selfs_checked=food_selfs,
                            source="api",
                            message=p_msg or "نشست در حین بررسی منو منقضی شد.",
                            http_status=prog_resp.status_code,
                            samad_code=p_code,
                            username=username,
                        )
                    if p_status != CheckStatus.MENU_PUBLISHED:
                        return FetchResult(
                            status=p_status,
                            week_range=week_range,
                            selfs_checked=food_selfs,
                            source="api",
                            message=p_msg or f"خطا در دریافت برنامه سلف {self_info.name}",
                            http_status=prog_resp.status_code,
                            samad_code=p_code,
                            username=username,
                        )

                    self_meals = parse_samad_programs_payload(
                        p_payload or {}, self_info=self_info, target_week=week_range
                    )
                    all_meals.extend(self_meals)

                sorted_meals = sort_and_deduplicate_meals(all_meals)
                if not sorted_meals:
                    return FetchResult(
                        status=CheckStatus.MENU_NOT_PUBLISHED,
                        week_range=week_range,
                        meals=[],
                        selfs_checked=food_selfs,
                        source="api",
                        message="منوی هفته آینده هنوز در هیچ‌یک از سلف‌ها منتشر نشده است.",
                        samad_code=last_samad_code,
                        username=username,
                    )

                return FetchResult(
                    status=CheckStatus.MENU_PUBLISHED,
                    week_range=week_range,
                    meals=sorted_meals,
                    selfs_checked=food_selfs,
                    source="api",
                    message=f"منوی هفته آینده با {len(sorted_meals)} ردیف غذا استخراج شد.",
                    samad_code=last_samad_code,
                    username=username,
                )

        except SessionExpiredException as exc:
            return FetchResult(
                status=CheckStatus.SESSION_EXPIRED,
                week_range=week_range,
                source="api",
                message=str(exc),
                http_status=401,
            )
        except IncompleteMenuDataError as exc:
            return FetchResult(
                status=CheckStatus.INCOMPLETE_MENU_DATA,
                week_range=week_range,
                source="api",
                message=str(exc),
            )
        except SiteStructureChangedError as exc:
            return FetchResult(
                status=CheckStatus.SITE_STRUCTURE_CHANGED,
                week_range=week_range,
                source="api",
                message=str(exc),
            )
        except TransientFetchException as exc:
            return FetchResult(
                status=exc.check_status,
                week_range=week_range,
                source="api",
                message=str(exc),
                http_status=exc.http_status,
            )

    # ------------------------------------------------------------------
    # مرحله ۳: حالت پشتیبان مرورگر Headless با رهگیری Network و استخراج DOM
    # ------------------------------------------------------------------
    def _fetch_via_browser(
        self, week_range: WeekRange, ignore_user_self_filter: bool = False
    ) -> FetchResult:
        """باز کردن صفحه رزرو غذا در حالت Headless با مسدودسازی کامل درخواست‌های تغییردهنده."""
        if not self.session_manager.exists():
            return FetchResult(
                status=CheckStatus.SESSION_EXPIRED,
                week_range=week_range,
                source="browser",
                message="فایل نشست مرورگر یافت نشد. ابتدا دستور `python -m samad_monitor login` را اجرا کنید.",
            )

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        captured_payloads: List[Tuple[int, Dict[str, Any]]] = []
        captured_selfs: Dict[int, SelfServiceInfo] = {}

        try:
            with sync_playwright() as p:
                browser_extra, _ = resolve_browser_launch_options(p)
                if browser_extra is None:
                    return FetchResult(
                        status=CheckStatus.NETWORK_ERROR,
                        week_range=week_range,
                        source="browser",
                        message="مرورگر Chrome / Edge / Chromium روی سیستم یافت نشد.",
                    )
                launch_opts: Dict[str, Any] = {**browser_extra, "headless": True}
                if self.config.samad_proxy_url:
                    launch_opts["proxy"] = {"server": self.config.samad_proxy_url}

                browser = p.chromium.launch(**launch_opts)
                try:
                    context = browser.new_context(
                        storage_state=str(self.config.storage_state_path),
                        viewport={"width": 1440, "height": 900},
                        locale="fa-IR",
                        timezone_id=self.config.timezone_name,
                    )

                    # قفل سخت‌گیرانه فقط‌خواندنی روی لایه شبکه مرورگر
                    def _route_guard(route: Any) -> None:
                        req = route.request
                        method = req.method.upper()
                        url = req.url
                        if method != "GET":
                            parsed_u = urlparse(url)
                            path_l = (parsed_u.path or "").lower()
                            if method == "POST" and (
                                path_l.endswith("/oauth/token") or path_l.endswith("/rest/captcha")
                            ):
                                route.continue_()
                                return
                            logger.warning("درخواست غیرخواندنی مرورگر مسدود شد: %s %s", method, url)
                            route.abort("blockedbyclient")
                            return
                        route.continue_()

                    context.route("**/*", _route_guard)

                    def _on_response(response: Any) -> None:
                        url = response.url
                        if "/selfs" in url and response.status == 200:
                            try:
                                data = response.json()
                                for s in self._filter_food_selfs(
                                    data.get("payload") or [],
                                    ignore_user_filter=ignore_user_self_filter,
                                ):
                                    captured_selfs[s.id] = s
                            except Exception:
                                pass
                        elif "/programs/v2" in url and response.status in {200, 400}:
                            try:
                                parsed_q = urlparse(url)
                                qs = dict(
                                    part.split("=", 1)
                                    for part in parsed_q.query.split("&")
                                    if "=" in part
                                )
                                sid = int(qs.get("selfId", "0") or 0)
                                captured_payloads.append((sid, response.json()))
                            except Exception:
                                pass

                    page = context.new_page()
                    page.on("response", _on_response)

                    page.goto(
                        self.config.samad_reserve_url,
                        wait_until="domcontentloaded",
                        timeout=int(self.config.http_timeout_seconds * 1000),
                    )
                    page.wait_for_timeout(2000)

                    if "/login" in page.url:
                        self.session_manager.clear_tokens_on_expiry()
                        return FetchResult(
                            status=CheckStatus.SESSION_EXPIRED,
                            week_range=week_range,
                            source="browser",
                            message="مرورگر به صفحه ورود هدایت شد؛ نشست منقضی شده است.",
                        )

                    # اگر مودال انتخاب سلف باز است، اولین سلف غذا را انتخاب کن
                    modal_self_item = page.locator(".ant-modal-body .cursor-pointer").first
                    if modal_self_item.count() > 0 and modal_self_item.is_visible():
                        modal_self_item.click()
                        page.wait_for_timeout(1500)

                    # پیمایش به هفته قبل یا هفته بعد در نمای دسکتاپ RTL بر اساس هفته هدف
                    from samad_monitor.utils.date_utils import get_current_week_range

                    curr_wk = get_current_week_range(tz_name=self.config.timezone_name)
                    if week_range.week_start < curr_wk.week_start:
                        prev_week_btn = page.locator(
                            "[data-testid='prev-week'], .anticon-right, svg.lucide-chevron-right"
                        ).first
                        if prev_week_btn.count() > 0 and prev_week_btn.is_visible():
                            prev_week_btn.click()
                            page.wait_for_timeout(2000)
                    elif week_range.week_start > curr_wk.week_start:
                        next_week_btn = page.locator(
                            "[data-testid='next-week'], .anticon-left, svg.lucide-chevron-left"
                        ).first
                        if next_week_btn.count() > 0 and next_week_btn.is_visible():
                            next_week_btn.click()
                            page.wait_for_timeout(2000)

                    # ذخیره توکن‌های احتمالاً تمدیدشده توسط خود اپ React
                    context.storage_state(path=str(self.config.storage_state_path))
                    restrict_path_permissions(self.config.storage_state_path, is_dir=False)

                    # اولویت ۱: پارس کردن JSONهای رهگیری‌شده از شبکه خود مرورگر
                    meals: List[Meal] = []
                    for sid, env_json in captured_payloads:
                        st, pl, _, _ = parse_samad_envelope(env_json)
                        if st == CheckStatus.MENU_PUBLISHED and isinstance(pl, dict):
                            s_info = captured_selfs.get(
                                sid, SelfServiceInfo(id=sid or 1, name="سلف دانشگاه شهید بهشتی")
                            )
                            meals.extend(parse_samad_programs_payload(pl, s_info, week_range))

                    if meals:
                        return FetchResult(
                            status=CheckStatus.MENU_PUBLISHED,
                            week_range=week_range,
                            meals=sort_and_deduplicate_meals(meals),
                            selfs_checked=list(captured_selfs.values()),
                            source="browser_network",
                            message="منوی هفته آینده از طریق مرورگر Headless استخراج شد.",
                        )

                    # اولویت ۲: استخراج مستقیم کارت‌ها از DOM در صورت عدم رهگیری JSON
                    raw_dom_items = page.evaluate(
                        """() => {
                            const results = [];
                            const columns = document.querySelectorAll('[data-day-column]');
                            if (columns.length > 0) {
                                columns.forEach(col => {
                                    const header = col.getAttribute('data-day-header') || col.querySelector('h3, .day-header')?.innerText || '';
                                    col.querySelectorAll('[data-meal-card], .reserve-card').forEach(card => {
                                        const mealType = card.querySelector('[data-meal-type], .meal-type')?.innerText || '';
                                        const foodName = card.querySelector('[data-food-name], .food-name')?.innerText || '';
                                        const btn = card.querySelector('button');
                                        const reservable = btn ? !btn.disabled : null;
                                        results.push({ header, mealType, foodName, reservable });
                                    });
                                });
                            }
                            return results;
                        }"""
                    )

                    if isinstance(raw_dom_items, list) and raw_dom_items:
                        dom_cards = [
                            RawDomCard(
                                header_date_text=str(it.get("header") or ""),
                                meal_type_text=str(it.get("mealType") or ""),
                                food_name_text=str(it.get("foodName") or ""),
                                is_reservable=it.get("reservable"),
                            )
                            for it in raw_dom_items
                            if isinstance(it, dict)
                        ]
                        default_self = next(
                            iter(captured_selfs.values()),
                            SelfServiceInfo(id=1, name="سلف دانشگاه شهید بهشتی"),
                        )
                        dom_meals = parse_samad_dom_cards(dom_cards, default_self, week_range)
                        if dom_meals:
                            return FetchResult(
                                status=CheckStatus.MENU_PUBLISHED,
                                week_range=week_range,
                                meals=dom_meals,
                                selfs_checked=[default_self],
                                source="browser_dom",
                                message="منوی هفته آینده از طریق DOM مرورگر استخراج شد.",
                            )

                    # بررسی وجود صفحه معتبر رزرو در برابر تغییر ساختار سایت
                    body_text = page.locator("body").inner_text(timeout=5000)
                    if any(
                        kw in body_text
                        for kw in ("هیچ برنامه‌ای", "سلف", "رزرو", "سماد", "برنامه غذایی")
                    ):
                        return FetchResult(
                            status=CheckStatus.MENU_NOT_PUBLISHED,
                            week_range=week_range,
                            source="browser",
                            message="صفحه رزرو بررسی شد؛ منوی هفته آینده هنوز منتشر نشده است.",
                        )

                    return FetchResult(
                        status=CheckStatus.SITE_STRUCTURE_CHANGED,
                        week_range=week_range,
                        source="browser",
                        message="ساختار DOM صفحه رزرو با الگوهای شناخته‌شده سماد هم‌خوانی نداشت.",
                    )
                finally:
                    browser.close()

        except PlaywrightTimeoutError:
            return FetchResult(
                status=CheckStatus.NETWORK_ERROR,
                week_range=week_range,
                source="browser",
                message="مهلت بارگذاری صفحه سماد در مرورگر به پایان رسید (احتمال کندی اینترنت یا فعال بودن VPN خارجی).",
            )
        except IncompleteMenuDataError as exc:
            return FetchResult(
                status=CheckStatus.INCOMPLETE_MENU_DATA,
                week_range=week_range,
                source="browser_dom",
                message=str(exc),
            )
        except Exception as exc:
            return FetchResult(
                status=CheckStatus.NETWORK_ERROR,
                week_range=week_range,
                source="browser",
                message=f"خطا در اجرای مرورگر Headless: {exc}",
            )

    # ------------------------------------------------------------------
    # متد اصلی عمومی برای موتور پایش
    # ------------------------------------------------------------------
    def fetch_week_menu(
        self, week_range: WeekRange, ignore_user_self_filter: bool = False
    ) -> FetchResult:
        """اجرای استراتژی انتخاب‌شده (`auto`، `api` یا `browser`) برای استخراج منوی هفته هدف."""
        strategy = self.config.samad_fetch_strategy

        if strategy == "browser":
            return self._fetch_via_browser(
                week_range, ignore_user_self_filter=ignore_user_self_filter
            )

        api_result = self._fetch_via_api(
            week_range, ignore_user_self_filter=ignore_user_self_filter
        )
        if strategy == "api":
            return api_result

        # در حالت auto: اگر ساختار API تغییر کرده بود، به‌صورت هوشمند با مرورگر نیز بررسی کن
        if api_result.status == CheckStatus.SITE_STRUCTURE_CHANGED and self._http_transport is None:
            logger.warning(
                "ساختار پاسخ API غیرمنتظره بود (%s)؛ تلاش پشتیبان با مرورگر Playwright Headless...",
                api_result.message,
            )
            browser_result = self._fetch_via_browser(
                week_range, ignore_user_self_filter=ignore_user_self_filter
            )
            if browser_result.status in {CheckStatus.MENU_PUBLISHED, CheckStatus.MENU_NOT_PUBLISHED}:
                return browser_result

        return api_result
