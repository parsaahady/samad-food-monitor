"""کانال رسمی Telegram Bot API با پشتیبانی از چند گیرنده، دکمه‌های شیشه‌ای سلف خوابگاه/مرکزی، منوی هفته قبل و دستورات تعاملی."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import httpx

from samad_monitor.models import CheckStatus, EventType, Menu, NotificationMessage, WeekRange
from samad_monitor.notifications.base import NotificationChannel
from samad_monitor.notifications.formatter import (
    format_menu_available_notification,
    format_scoped_week_menu_message,
)
from samad_monitor.utils.date_utils import (
    get_current_week_range,
    get_next_week_range,
    get_previous_week_range,
    now_tehran,
)

if TYPE_CHECKING:
    from samad_monitor.database.repository import MonitorRepository

logger = logging.getLogger("samad_monitor.notifications.telegram")

TELEGRAM_MAX_MESSAGE_LENGTH = 3900

WeekMenuProvider = Callable[[str, bool], Tuple[WeekRange, Optional[Menu], CheckStatus, str]]

WEEK_LABELS_FA: Dict[str, str] = {
    "prev": "هفته قبل",
    "curr": "هفته جاری",
    "next": "هفته آینده",
}

MAIN_REPLY_KEYBOARD: Dict[str, Any] = {
    "keyboard": [
        [{"text": "🍽 منوی هفته آینده"}, {"text": "📅 منوی هفته قبل"}],
        [{"text": "📆 منوی هفته جاری"}, {"text": "🔕 لغو اعلان"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

BOT_COMMANDS_SPEC: List[Dict[str, str]] = [
    {"command": "lastweek", "description": "📅 منوی هفته قبل (سلف خوابگاه / سلف مرکزی)"},
    {"command": "menu", "description": "🍽 منوی هفته آینده"},
    {"command": "thisweek", "description": "📆 منوی هفته جاری (خوابگاه / مرکزی)"},
    {"command": "start", "description": "🏠 نمایش دکمه‌های اصلی ربات"},
    {"command": "stop", "description": "🔕 لغو دریافت خودکار اعلان‌ها"},
]


def build_self_selector_inline_keyboard(week_key: str) -> Dict[str, Any]:
    """ساخت دو دکمه شیشه‌ای اصلی (منوی سلف خوابگاه کوی / منوی سلف مرکزی) + گزینه همه سلف‌ها."""
    wk = week_key if week_key in {"prev", "curr", "next"} else "prev"
    return {
        "inline_keyboard": [
            [
                {"text": "🏠 منوی سلف خوابگاه کوی", "callback_data": f"menu:{wk}:dorm"},
                {"text": "🏛 منوی سلف مرکزی", "callback_data": f"menu:{wk}:central"},
            ],
            [
                {"text": "📋 همه سلف‌ها یکجا", "callback_data": f"menu:{wk}:all"},
            ],
        ]
    }


def build_post_menu_switch_keyboard(week_key: str, current_scope: str) -> Dict[str, Any]:
    """دکمه‌های شیشه‌ای زیر پیام منو برای جابه‌جایی سریع بین سلف خوابگاه کوی و سلف مرکزی."""
    wk = week_key if week_key in {"prev", "curr", "next"} else "prev"
    w_label = WEEK_LABELS_FA.get(wk, "هفته")
    row1: List[Dict[str, str]] = []
    if current_scope == "dorm":
        row1.append({"text": f"🏛 منوی سلف مرکزی ({w_label})", "callback_data": f"menu:{wk}:central"})
    elif current_scope == "central":
        row1.append({"text": f"🏠 منوی سلف خوابگاه کوی ({w_label})", "callback_data": f"menu:{wk}:dorm"})
    else:
        row1.append({"text": "🏠 فقط خوابگاه کوی", "callback_data": f"menu:{wk}:dorm"})
        row1.append({"text": "🏛 فقط سلف مرکزی", "callback_data": f"menu:{wk}:central"})

    rows: List[List[Dict[str, str]]] = [row1]
    if wk in {"prev", "curr"}:
        rows.append(
            [{"text": "🔄 استعلام مجدد از سرور سماد", "callback_data": f"refresh:{wk}:{current_scope}"}]
        )
    else:
        rows.append(
            [{"text": "📅 مشاهده منوی هفته قبل (خوابگاه / مرکزی)", "callback_data": "pick:prev"}]
        )
    return {"inline_keyboard": rows}


def split_telegram_text(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> List[str]:
    """تقسیم هوشمند پیام‌های بلند در مرز پاراگراف‌ها/روزها بدون بریده شدن نام غذاها."""
    clean = text.strip()
    if len(clean) <= max_len:
        return [clean]

    chunks: List[str] = []
    paragraphs = clean.split("\n\n")
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}".strip() if current else para
        if len(candidate) <= max_len:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(para) <= max_len:
            current = para
        else:
            lines = para.split("\n")
            sub = ""
            for line in lines:
                cand_line = f"{sub}\n{line}".strip() if sub else line
                if len(cand_line) <= max_len:
                    sub = cand_line
                else:
                    if sub:
                        chunks.append(sub)
                    sub = line[:max_len]
            current = sub

    if current:
        chunks.append(current)

    return chunks


def _extract_chat_meta(
    chat_obj: Dict[str, Any], from_obj: Optional[Dict[str, Any]] = None
) -> Tuple[str, str, str, str]:
    """استخراج (chat_id, display_name, username, chat_type) از آبجکت آپدیت تلگرام."""
    cid = str(chat_obj.get("id") or "").strip()
    chat_type = str(chat_obj.get("type") or "private").strip()
    username = (
        str(chat_obj.get("username") or (from_obj or {}).get("username") or "")
        .strip()
        .lstrip("@")
    )

    if chat_type in {"group", "supergroup", "channel"}:
        title = str(chat_obj.get("title") or "").strip()
        display_name = title or (f"@{username}" if username else f"گروه {cid}")
    else:
        first = str(
            chat_obj.get("first_name") or (from_obj or {}).get("first_name") or ""
        ).strip()
        last = str(
            chat_obj.get("last_name") or (from_obj or {}).get("last_name") or ""
        ).strip()
        full = f"{first} {last}".strip()
        display_name = full or (f"@{username}" if username else f"کاربر {cid}")

    return cid, display_name, username, chat_type


class TelegramNotificationChannel(NotificationChannel):
    """ارسال اعلان به تلگرام از طریق Telegram Bot API رسمی با پشتیبانی از لیست مشترکین و منوی تعاملی."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str | List[str] = "",
        api_base_url: str = "https://api.telegram.org",
        proxy_url: Optional[str] = None,
        timeout_seconds: float = 20.0,
        http_transport: Optional[httpx.BaseTransport] = None,
        enabled: bool = True,
        repository: Optional["MonitorRepository"] = None,
        university_label: str = "دانشگاه شهید بهشتی",
        reserve_url: str = "https://samad.app/food/reserve",
        timezone_name: str = "Asia/Tehran",
        week_menu_provider: Optional[WeekMenuProvider] = None,
    ) -> None:
        self._bot_token = (bot_token or "").strip()
        if isinstance(chat_id, list):
            self._chat_ids = [str(c).strip() for c in chat_id if str(c).strip()]
        else:
            self._chat_ids = [c.strip() for c in str(chat_id or "").split(",") if c.strip()]
        self._api_base_url = (api_base_url or "https://api.telegram.org").rstrip("/")
        self._proxy_url = (proxy_url or "").strip() or None
        self._timeout_seconds = timeout_seconds
        self._http_transport = http_transport
        self._enabled = enabled
        self._repository = repository
        self._university_label = university_label
        self._reserve_url = reserve_url
        self._timezone_name = timezone_name
        self._week_menu_provider = week_menu_provider
        self._commands_registered = False

        if self._repository is not None and self._chat_ids:
            try:
                self._repository.sync_env_chat_ids(self._chat_ids)
            except Exception as exc:
                logger.debug("خطای جزئی در همگام‌سازی اولیه گیرندگان .env با دیتابیس: %s", exc)

    def set_week_menu_provider(self, provider: WeekMenuProvider) -> None:
        self._week_menu_provider = provider

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def has_bot_token(self) -> bool:
        return bool(self._bot_token)

    def resolve_recipients(self, event_type: Optional[EventType] = None) -> List[str]:
        """تعیین لیست نهایی شناسه‌های گیرنده بر اساس دیتابیس مشترکین + تنظیمات .env و نوع رویداد."""
        if self._repository is None:
            seen: set[str] = set()
            out: List[str] = []
            for cid in self._chat_ids:
                if cid and cid not in seen:
                    seen.add(cid)
                    out.append(cid)
            return out

        enabled_subs = self._repository.list_telegram_subscribers(only_enabled=True)
        if not enabled_subs:
            all_subs = self._repository.list_telegram_subscribers(only_enabled=False)
            if not all_subs and self._chat_ids:
                return list(dict.fromkeys(self._chat_ids))
            return []

        if event_type == EventType.SESSION_EXPIRED:
            admins = [str(s["chat_id"]) for s in enabled_subs if s.get("role") == "admin"]
            if admins:
                return admins

        return [str(s["chat_id"]) for s in enabled_subs]

    def is_configured(self) -> Tuple[bool, str]:
        if not self._enabled:
            return (False, "غیرفعال توسط تنظیمات (TELEGRAM_ENABLED=false)")
        recipients = self.resolve_recipients()
        if not self._bot_token or not recipients:
            if not self._bot_token:
                return (False, "غیرفعال (TELEGRAM_BOT_TOKEN تنظیم نشده است)")
            return (
                False,
                "غیرفعال (هیچ گیرنده فعالی در تب «مشترکین تلگرام» یا TELEGRAM_CHAT_ID ثبت نشده است)",
            )
        proxy_note = f"با پروکسی {self._proxy_url}" if self._proxy_url else "اتصال مستقیم"
        return (True, f"فعال ({len(recipients)} گیرنده فعال، {proxy_note})")

    def _build_client(self, custom_timeout: Optional[float] = None) -> httpx.Client:
        kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(custom_timeout or self._timeout_seconds),
        }
        if self._http_transport is not None:
            kwargs["transport"] = self._http_transport
        elif self._proxy_url:
            kwargs["proxy"] = self._proxy_url
            kwargs["trust_env"] = False
        return httpx.Client(**kwargs)

    def send_text_to_chat(
        self,
        target_chat: str,
        text: str,
        client: Optional[httpx.Client] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        """ارسال متن به یک گیرنده مشخص با تقسیم هوشمند پیام‌های بلند، کیبورد تعاملی و مدیریت خطای ۴۲۹."""
        if not self._bot_token:
            raise RuntimeError("توکن ربات تلگرام (TELEGRAM_BOT_TOKEN) تنظیم نشده است.")

        cid = str(target_chat or "").strip()
        if not cid:
            raise ValueError("شناسه گیرنده تلگرام خالی است.")

        endpoint = f"{self._api_base_url}/bot{self._bot_token}/sendMessage"
        chunks = split_telegram_text(text)

        owns_client = client is None
        active_client = client or self._build_client()
        try:
            for idx, chunk in enumerate(chunks, start=1):
                prefix = f"({idx}/{len(chunks)})\n" if len(chunks) > 1 else ""
                payload: Dict[str, Any] = {
                    "chat_id": cid,
                    "text": prefix + chunk,
                    "disable_web_page_preview": True,
                }
                # دکمه‌های شیشه‌ای یا کیبورد را به تکه آخر پیام بچسبان
                if reply_markup is not None and idx == len(chunks):
                    payload["reply_markup"] = reply_markup

                last_error: Optional[Exception] = None
                for attempt in range(1, 4):
                    try:
                        resp = active_client.post(endpoint, json=payload)
                    except httpx.HTTPError as exc:
                        last_error = exc
                        if attempt < 3:
                            time.sleep(1.5 * attempt)
                            continue
                        raise RuntimeError(
                            "عدم دسترسی به API تلگرام (در ایران تنظیم پورت پروکسی فیلترشکن در بخش TELEGRAM_PROXY_URL ضروری است)."
                        ) from exc

                    if resp.status_code == 429:
                        retry_after = 3
                        try:
                            retry_after = int(
                                resp.json().get("parameters", {}).get("retry_after", 3)
                            )
                        except Exception:
                            pass
                        if attempt < 3:
                            time.sleep(min(retry_after, 15))
                            continue

                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, dict) and data.get("ok") is True:
                            last_error = None
                            break

                    desc = ""
                    try:
                        desc = str(resp.json().get("description") or "")
                    except Exception:
                        desc = resp.text[:120]

                    last_error = RuntimeError(
                        f"خطای API تلگرام برای گیرنده {cid} (کد HTTP {resp.status_code}): {desc}"
                    )
                    if 400 <= resp.status_code < 500 and resp.status_code != 429:
                        raise last_error
                    if attempt < 3:
                        time.sleep(1.5 * attempt)

                if last_error is not None:
                    raise last_error
        finally:
            if owns_client:
                active_client.close()

    def send(self, message: NotificationMessage) -> None:
        configured, reason = self.is_configured()
        if not configured:
            raise RuntimeError(f"کانال تلگرام پیکربندی نشده است: {reason}")

        recipients = self.resolve_recipients(event_type=message.event_type)
        if not recipients:
            raise RuntimeError("هیچ گیرنده فعالی برای این نوع رویداد در تلگرام یافت نشد.")

        # زیر پیام‌های انتشار منوی هفته آینده، دکمه‌های شیشه‌ای تفکیک خوابگاه/مرکزی و هفته قبل هم بیاید
        inline_kb = (
            build_post_menu_switch_keyboard("next", "all")
            if message.event_type in {EventType.MENU_AVAILABLE, EventType.MENU_UPDATED}
            and self._repository is not None
            else None
        )

        succeeded_chats: List[str] = []
        failed_errors: List[str] = []

        with self._build_client() as client:
            for idx, target_chat in enumerate(recipients):
                if idx > 0 and self._http_transport is None:
                    time.sleep(0.05)
                try:
                    self.send_text_to_chat(
                        target_chat=target_chat,
                        text=message.body,
                        client=client,
                        reply_markup=inline_kb,
                    )
                    succeeded_chats.append(target_chat)
                except Exception as exc:
                    err_str = str(exc)
                    failed_errors.append(f"{target_chat}: {err_str}")
                    logger.warning(
                        "ارسال اعلان به گیرنده %s ناموفق بود (%s)؛ ادامه ارسال به سایر گیرندگان...",
                        target_chat,
                        err_str,
                    )

        if not succeeded_chats:
            raise RuntimeError(
                "؛ ".join(failed_errors) if failed_errors else "ارسال به تمام گیرندگان ناموفق بود."
            )

        if failed_errors:
            logger.warning(
                "اعلان %s به %d از %d گیرنده ارسال شد (%d ناموفق).",
                message.event_id,
                len(succeeded_chats),
                len(recipients),
                len(failed_errors),
            )
        else:
            logger.info(
                "اعلان رویداد %s با موفقیت برای %d گیرنده تلگرام ارسال شد.",
                message.event_id,
                len(succeeded_chats),
            )

    # ------------------------------------------------------------------
    # ثبت منوی دستورات، پروفایل ربات و پردازش تعاملی دکمه‌های شیشه‌ای و کیبورد
    # ------------------------------------------------------------------
    def ensure_bot_commands_registered(self, client: Optional[httpx.Client] = None) -> None:
        if self._commands_registered or not self._bot_token:
            return
        endpoint = f"{self._api_base_url}/bot{self._bot_token}/setMyCommands"
        owns = client is None
        active = client or self._build_client(custom_timeout=8.0)
        try:
            resp = active.post(endpoint, json={"commands": BOT_COMMANDS_SPEC})
            if resp.status_code == 200:
                self._commands_registered = True
        except Exception as exc:
            logger.debug("ثبت منوی دستورات ربات تلگرام رد شد: %s", exc)
        finally:
            if owns:
                active.close()

    def _answer_callback_query(
        self,
        client: httpx.Client,
        callback_query_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        if not callback_query_id or not self._bot_token:
            return
        endpoint = f"{self._api_base_url}/bot{self._bot_token}/answerCallbackQuery"
        payload: Dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text[:190]
        try:
            client.post(endpoint, json=payload)
        except Exception as exc:
            logger.debug("خطا در answerCallbackQuery: %s", exc)

    def get_bot_profile(self, timeout_seconds: float = 10.0) -> Dict[str, Any]:
        """استعلام نام و یوزرنیم ربات از Telegram getMe برای ساخت لینک دعوت آماده اشتراک‌گذاری."""
        if not self._bot_token:
            return {"ok": False, "message": "ابتدا توکن ربات تلگرام را در تب تنظیمات ذخیره کنید."}
        endpoint = f"{self._api_base_url}/bot{self._bot_token}/getMe"
        try:
            with self._build_client(custom_timeout=timeout_seconds) as client:
                resp = client.get(endpoint)
                self.ensure_bot_commands_registered(client=client)
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                res = data.get("result") or {}
                return {
                    "ok": True,
                    "id": res.get("id"),
                    "first_name": res.get("first_name") or "",
                    "username": res.get("username") or "",
                }
            return {
                "ok": False,
                "message": f"پاسخ نامعتبر از تلگرام ({resp.status_code}): {data.get('description', '')}",
            }
        except Exception as exc:
            return {
                "ok": False,
                "message": f"عدم اتصال به تلگرام (پورت پروکسی فیلترشکن را بررسی کنید): {exc}",
            }

    def sync_bot_updates(
        self,
        auto_subscribe: bool = False,
        invite_code: str = "",
        handle_commands: bool = True,
    ) -> Dict[str, Any]:
        """
        خواندن پیام‌ها و کلیک‌های روی دکمه‌های شیشه‌ای ربات (`getUpdates`):
        - دکمه‌های کیبورد پایین صفحه: «🍽 منوی هفته آینده»، «📅 منوی هفته قبل»، «📆 منوی هفته جاری»
        - دکمه‌های شیشه‌ای انتخاب سلف: «🏠 منوی سلف خوابگاه» و «🏛 منوی سلف مرکزی»
        """
        if not self._bot_token:
            return {
                "ok": False,
                "new_updates": 0,
                "auto_added": 0,
                "message": "ابتدا توکن ربات تلگرام را در تب تنظیمات وارد و ذخیره کنید.",
            }
        if self._repository is None:
            return {"ok": False, "new_updates": 0, "auto_added": 0, "message": "دیتابیس متصل نیست."}

        last_offset_str = self._repository.get_runtime_value("telegram_last_update_id")
        params: Dict[str, Any] = {
            "timeout": 0,
            "limit": 100,
            "allowed_updates": json.dumps(
                ["message", "channel_post", "my_chat_member", "callback_query"]
            ),
        }
        if last_offset_str and last_offset_str.isdigit():
            params["offset"] = int(last_offset_str) + 1

        endpoint = f"{self._api_base_url}/bot{self._bot_token}/getUpdates"
        try:
            with self._build_client(custom_timeout=18.0) as client:
                if not self._commands_registered and self._http_transport is None:
                    self.ensure_bot_commands_registered(client=client)

                resp = client.get(endpoint, params=params)
                data = resp.json()
                if resp.status_code != 200 or not data.get("ok"):
                    desc = data.get("description") or resp.text[:120]
                    return {
                        "ok": False,
                        "new_updates": 0,
                        "auto_added": 0,
                        "message": f"خطا در دریافت پیام‌های ربات ({resp.status_code}): {desc}",
                    }

                updates: List[Dict[str, Any]] = data.get("result") or []
                max_update_id: Optional[int] = None
                auto_added_count = 0
                discovered_chats = 0

                for upd in updates:
                    upd_id = upd.get("update_id")
                    if isinstance(upd_id, int):
                        max_update_id = upd_id if max_update_id is None else max(max_update_id, upd_id)

                    # ۱) پردازش کلیک روی دکمه‌های شیشه‌ای (Inline Keyboard Callback)
                    cb_query = upd.get("callback_query")
                    if isinstance(cb_query, dict):
                        if handle_commands:
                            added_now = self._handle_callback_query(
                                client=client,
                                cb_query=cb_query,
                                auto_subscribe=auto_subscribe,
                                invite_code=invite_code,
                            )
                            auto_added_count += added_now
                        continue

                    # ۲) پردازش افزوده شدن ربات به گروه یا کانال
                    my_member = upd.get("my_chat_member")
                    if my_member and isinstance(my_member.get("chat"), dict):
                        cid, dname, uname, ctype = _extract_chat_meta(
                            my_member["chat"], my_member.get("from")
                        )
                        new_status = str(
                            (my_member.get("new_chat_member") or {}).get("status") or ""
                        )
                        if cid and new_status in {"member", "administrator"}:
                            self._repository.record_telegram_contact(
                                chat_id=cid,
                                display_name=dname,
                                username=uname,
                                chat_type=ctype,
                                last_message="[افزوده شدن ربات به گروه/کانال]",
                            )
                            discovered_chats += 1
                        continue

                    # ۳) پردازش پیام‌های متنی و دکمه‌های کیبورد پایین صفحه
                    msg = upd.get("message") or upd.get("channel_post")
                    if not isinstance(msg, dict) or not isinstance(msg.get("chat"), dict):
                        continue

                    cid, dname, uname, ctype = _extract_chat_meta(msg["chat"], msg.get("from"))
                    text = str(msg.get("text") or "").strip()
                    if not cid:
                        continue

                    self._repository.record_telegram_contact(
                        chat_id=cid,
                        display_name=dname,
                        username=uname,
                        chat_type=ctype,
                        last_message=text or "[پیام غیرمتنی]",
                    )
                    discovered_chats += 1

                    if not handle_commands or not text:
                        continue

                    added_from_msg = self._handle_text_command(
                        client=client,
                        cid=cid,
                        dname=dname,
                        uname=uname,
                        ctype=ctype,
                        text=text,
                        auto_subscribe=auto_subscribe,
                        invite_code=invite_code,
                    )
                    auto_added_count += added_from_msg

                if max_update_id is not None:
                    self._repository.set_runtime_value(
                        "telegram_last_update_id", str(max_update_id)
                    )

                return {
                    "ok": True,
                    "new_updates": len(updates),
                    "discovered_chats": discovered_chats,
                    "auto_added": auto_added_count,
                    "message": (
                        f"{len(updates)} پیام/درخواست جدید بررسی شد."
                        if updates
                        else "پیام جدیدی از آخرین بررسی ثبت نشده است."
                    ),
                }
        except Exception as exc:
            return {
                "ok": False,
                "new_updates": 0,
                "auto_added": 0,
                "message": f"خطا در ارتباط با تلگرام (پورت فیلترشکن را بررسی کنید): {exc}",
            }

    def _check_or_auto_authorize(
        self,
        cid: str,
        dname: str,
        uname: str,
        ctype: str,
        auto_subscribe: bool,
        invite_code: str,
    ) -> Tuple[bool, int]:
        """بررسی مجوز کاربر برای مشاهده منوها (با عضویت خودکار در صورتی که بدون کد دعوت باز باشد)."""
        assert self._repository is not None
        existing = self._repository.get_telegram_subscriber(cid)
        if existing and existing.get("enabled"):
            return True, 0
        if cid in self._chat_ids:
            return True, 0
        if auto_subscribe and not invite_code.strip():
            self._repository.upsert_telegram_subscriber(
                chat_id=cid,
                display_name=dname,
                username=uname,
                chat_type=ctype,
                role="subscriber",
                enabled=True,
                added_via="auto_start",
            )
            return True, 1
        return False, 0

    def _handle_text_command(
        self,
        client: httpx.Client,
        cid: str,
        dname: str,
        uname: str,
        ctype: str,
        text: str,
        auto_subscribe: bool,
        invite_code: str,
    ) -> int:
        assert self._repository is not None
        auto_added = 0
        reply_kb = MAIN_REPLY_KEYBOARD if ctype == "private" else None

        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # ۱. دستور /start و نمایش کیبورد اصلی
        if cmd in {"/start", "/help", "شروع"}:
            existing = self._repository.get_telegram_subscriber(cid)
            clean_code = invite_code.strip()
            code_ok = (not clean_code) or (arg == clean_code)

            if existing and existing.get("enabled"):
                self._safe_reply(
                    client,
                    cid,
                    f"✅ سلام {dname} عزیز!\n"
                    f"عضویت شما در ربات اطلاع‌رسانی سلف {self._university_label} فعال است.\n\n"
                    "از دکمه‌های پایین صفحه می‌توانید به راحتی منوی مورد نظرتان را انتخاب کنید:\n"
                    "• 🍽 منوی هفته آینده\n"
                    "• 📅 منوی هفته قبل (به تفکیک سلف خوابگاه و سلف مرکزی)\n"
                    "• 📆 منوی هفته جاری",
                    reply_markup=reply_kb,
                )
            elif auto_subscribe and code_ok:
                self._repository.upsert_telegram_subscriber(
                    chat_id=cid,
                    display_name=dname,
                    username=uname,
                    chat_type=ctype,
                    role="subscriber",
                    enabled=True,
                    added_via="auto_start",
                )
                auto_added += 1
                self._safe_reply(
                    client,
                    cid,
                    f"🎉 سلام {dname} عزیز!\n"
                    f"عضویت شما در ربات اطلاع‌رسانی منوی سلف {self._university_label} با موفقیت فعال شد.\n"
                    "به محض انتشار یا تغییر برنامه غذایی هفته آینده، لیست کامل وعده‌ها برایتان ارسال می‌شود.\n\n"
                    "👇 با دکمه‌های زیر می‌توانید همین الان منوی هفته آینده یا هفته قبل (سلف خوابگاه / مرکزی) را ببینید:",
                    reply_markup=reply_kb,
                )
            elif auto_subscribe and not code_ok:
                self._safe_reply(
                    client,
                    cid,
                    f"🔒 سلام {dname}!\n"
                    "برای عضویت خودکار در این ربات به لینک دعوت دارای کد نیاز دارید، "
                    "یا منتظر بمانید تا مدیر ربات نام شما را از پنل مدیریت تایید کند.\n"
                    f"🆔 شناسه شما: {cid}",
                )
            else:
                self._safe_reply(
                    client,
                    cid,
                    f"👋 سلام {dname}!\n"
                    "درخواست شما در پنل مدیریت ربات ثبت شد. مدیر ربات می‌تواند با یک کلیک شما را به لیست گیرندگان اضافه کند.\n"
                    f"🆔 شناسه عددی شما: {cid}",
                )
            return auto_added

        # ۲. لغو عضویت
        if cmd in {"/stop", "/unsubscribe", "لغو"} or "لغو اعلان" in text:
            existing = self._repository.get_telegram_subscriber(cid)
            if existing and existing.get("enabled"):
                self._repository.update_telegram_subscriber(cid, enabled=False)
                self._safe_reply(
                    client,
                    cid,
                    "🔕 دریافت خودکار اعلان‌های منوی غذا برای شما متوقف شد.\n"
                    "هر زمان مایل بودید با زدن /start دوباره فعالش کنید.",
                )
            return 0

        # ۳. نمایش آیدی عددی
        if cmd in {"/id", "آیدی"}:
            self._safe_reply(client, cid, f"🆔 شناسه عددی این گفتگو: {cid}")
            return 0

        # ۴. منوی هفته قبل -> نمایش فوری دو دکمه شیشه‌ای «سلف خوابگاه» و «سلف مرکزی»
        if (
            cmd in {"/lastweek", "/prevweek", "/prev"}
            or "هفته قبل" in text
            or "هفته ی قبل" in text
            or "هفته‌ی قبل" in text
            or "هفته گذشته" in text
        ):
            authorized, newly_added = self._check_or_auto_authorize(
                cid, dname, uname, ctype, auto_subscribe, invite_code
            )
            auto_added += newly_added
            if not authorized:
                self._safe_reply(
                    client,
                    cid,
                    "⚠️ شما هنوز در لیست اعضای فعال ربات نیستید. ابتدا /start بزنید یا منتظر تایید مدیر ربات بمانید.",
                )
                return auto_added

            self._send_week_self_selector_prompt(client, cid, "prev")
            return auto_added

        # ۵. منوی هفته جاری -> نمایش دو دکمه شیشه‌ای «سلف خوابگاه» و «سلف مرکزی»
        if cmd in {"/thisweek", "/currentweek"} or "هفته جاری" in text or "همین هفته" in text:
            authorized, newly_added = self._check_or_auto_authorize(
                cid, dname, uname, ctype, auto_subscribe, invite_code
            )
            auto_added += newly_added
            if not authorized:
                self._safe_reply(
                    client,
                    cid,
                    "⚠️ شما هنوز در لیست اعضای فعال ربات نیستید. ابتدا /start بزنید.",
                )
                return auto_added

            self._send_week_self_selector_prompt(client, cid, "curr")
            return auto_added

        # ۶. منوی هفته آینده (دستور /menu یا دکمه «🍽 منوی هفته آینده»)
        if cmd in {"/menu", "منو"} or "هفته آینده" in text or "هفته بعد" in text:
            authorized, newly_added = self._check_or_auto_authorize(
                cid, dname, uname, ctype, auto_subscribe, invite_code
            )
            auto_added += newly_added
            if not authorized:
                self._safe_reply(
                    client,
                    cid,
                    "⚠️ شما هنوز در لیست گیرندگان فعال این ربات قرار ندارید. ابتدا دستور /start را ارسال کنید.",
                )
                return auto_added

            # اگر کاربر روی دکمه کیبورد «🍽 منوی هفته آینده» زد و هر دو سلف موجود بود، یا مستقیم منو را با دکمه‌های فیلتر نشان بده
            reply_text, kb = self._build_week_menu_response("next", scope="all", force_refresh=False)
            self._safe_reply(client, cid, reply_text, reply_markup=kb)
            return auto_added

        return auto_added

    def _handle_callback_query(
        self,
        client: httpx.Client,
        cb_query: Dict[str, Any],
        auto_subscribe: bool,
        invite_code: str,
    ) -> int:
        assert self._repository is not None
        cb_id = str(cb_query.get("id") or "")
        cb_data = str(cb_query.get("data") or "").strip()
        msg = cb_query.get("message") or {}
        chat_obj = msg.get("chat") if isinstance(msg, dict) else None
        if not isinstance(chat_obj, dict):
            self._answer_callback_query(client, cb_id)
            return 0

        cid, dname, uname, ctype = _extract_chat_meta(chat_obj, cb_query.get("from"))
        if not cid:
            self._answer_callback_query(client, cb_id)
            return 0

        self._repository.record_telegram_contact(
            chat_id=cid,
            display_name=dname,
            username=uname,
            chat_type=ctype,
            last_message=f"[دکمه: {cb_data}]",
        )

        authorized, newly_added = self._check_or_auto_authorize(
            cid, dname, uname, ctype, auto_subscribe, invite_code
        )
        if not authorized:
            self._answer_callback_query(
                client,
                cb_id,
                "شما هنوز در لیست گیرندگان فعال این ربات نیستید.",
                show_alert=True,
            )
            return newly_added

        if cb_data.startswith("pick:"):
            wk = cb_data.split(":", 1)[1]
            self._answer_callback_query(client, cb_id, "سلف مورد نظر را انتخاب کنید")
            self._send_week_self_selector_prompt(client, cid, wk)
            return newly_added

        if cb_data.startswith(("menu:", "refresh:")):
            parts = cb_data.split(":")
            if len(parts) == 3:
                action, wk, scope = parts[0], parts[1], parts[2]
                force = action == "refresh"
                scope_fa = (
                    "سلف خوابگاه کوی"
                    if scope == "dorm"
                    else ("سلف مرکزی" if scope == "central" else "همه سلف‌ها")
                )
                self._answer_callback_query(
                    client, cb_id, f"⏳ در حال دریافت منوی {scope_fa}..."
                )
                reply_text, kb = self._build_week_menu_response(
                    week_key=wk, scope=scope, force_refresh=force
                )
                self._safe_reply(client, cid, reply_text, reply_markup=kb)
                return newly_added

        self._answer_callback_query(client, cb_id)
        return newly_added

    def _send_week_self_selector_prompt(
        self, client: httpx.Client, chat_id: str, week_key: str
    ) -> None:
        tehran_now = now_tehran(tz_name=self._timezone_name)
        if week_key == "prev":
            wk_range = get_previous_week_range(tehran_now, tz_name=self._timezone_name)
            heading = "📅 منوی هفته قبل"
        elif week_key == "curr":
            wk_range = get_current_week_range(tehran_now, tz_name=self._timezone_name)
            heading = "📆 منوی هفته جاری"
        else:
            week_key = "next"
            wk_range = get_next_week_range(tehran_now, tz_name=self._timezone_name)
            heading = "🍽 منوی هفته آینده"

        prompt_text = (
            f"{heading} — {self._university_label}\n"
            f"🗓 بازه هفته: {wk_range.display_bilingual()}\n"
            "────────────────────\n"
            "👇 لطفاً سلف مورد نظر خود را انتخاب کنید:"
        )
        self._safe_reply(
            client,
            chat_id,
            prompt_text,
            reply_markup=build_self_selector_inline_keyboard(week_key),
        )

    def _resolve_week_data(
        self, week_key: str, force_refresh: bool = False
    ) -> Tuple[WeekRange, Optional[Menu], CheckStatus, str]:
        if self._week_menu_provider is not None:
            return self._week_menu_provider(week_key, force_refresh)

        assert self._repository is not None
        tehran_now = now_tehran(tz_name=self._timezone_name)
        if week_key == "prev":
            wk = get_previous_week_range(tehran_now, tz_name=self._timezone_name)
        elif week_key == "curr":
            wk = get_current_week_range(tehran_now, tz_name=self._timezone_name)
        else:
            wk = get_next_week_range(tehran_now, tz_name=self._timezone_name)

        menu = self._repository.get_menu_by_week(wk.start_iso)
        if menu and menu.meals:
            return wk, menu, CheckStatus.MENU_PUBLISHED, "از دیتابیس محلی"
        return wk, None, CheckStatus.MENU_NOT_PUBLISHED, "در دیتابیس یافت نشد."

    def _build_week_menu_response(
        self, week_key: str, scope: str = "all", force_refresh: bool = False
    ) -> Tuple[str, Dict[str, Any]]:
        clean_wk = week_key if week_key in {"prev", "curr", "next"} else "next"
        w_label = WEEK_LABELS_FA[clean_wk]
        wk_range, menu, status, detail = self._resolve_week_data(
            clean_wk, force_refresh=force_refresh
        )

        if menu and menu.meals:
            if clean_wk == "next" and scope == "all":
                notif = format_menu_available_notification(
                    menu=menu,
                    week_range=wk_range,
                    university_label=self._university_label,
                    reserve_url=self._reserve_url,
                    event_id="BOT_MENU_QUERY",
                )
                return notif.body, build_post_menu_switch_keyboard("next", "all")

            body, _ = format_scoped_week_menu_message(
                meals=menu.meals,
                week_range=wk_range,
                scope=scope,
                week_label_fa=w_label,
                is_past_week=(clean_wk == "prev"),
                university_label=self._university_label,
                reserve_url=self._reserve_url,
            )
            return body, build_post_menu_switch_keyboard(clean_wk, scope)

        if status == CheckStatus.SESSION_EXPIRED:
            return (
                f"⚠️ امکان استعلام زنده منوی {w_label} از سماد وجود ندارد، زیرا نشست اتصال به سماد منقضی شده است و مدیر سیستم باید یک بار وارد سماد شود.",
                build_self_selector_inline_keyboard(clean_wk),
            )

        if status not in {CheckStatus.MENU_NOT_PUBLISHED, CheckStatus.MENU_PUBLISHED}:
            return (
                f"⚠️ خطای موقت در دریافت منوی {w_label} از سرور سماد:\n{detail}",
                build_self_selector_inline_keyboard(clean_wk),
            )

        if clean_wk == "next":
            return (
                f"📭 منوی هفته آینده ({wk_range.display_bilingual()}) هنوز در آخرین بررسی سامانه سماد منتشر نشده است.\n"
                "به محض انتشار، لیست کامل غذاها خودکار برایتان ارسال خواهد شد!\n\n"
                "💡 می‌توانید منوی هفته قبل را با دکمه زیر ببینید:",
                {"inline_keyboard": [[{"text": "📅 مشاهده منوی هفته قبل (خوابگاه / مرکزی)", "callback_data": "pick:prev"}]]},
            )

        return (
            f"📭 برنامه غذایی برای {w_label} ({wk_range.display_bilingual()}) در سامانه سماد یافت نشد.",
            build_self_selector_inline_keyboard(clean_wk),
        )

    def _safe_reply(
        self,
        client: httpx.Client,
        chat_id: str,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            self.send_text_to_chat(
                target_chat=chat_id,
                text=text,
                client=client,
                reply_markup=reply_markup,
            )
        except Exception as exc:
            logger.debug("ارسال پاسخ دستور ربات به %s ناموفق بود: %s", chat_id, exc)


class TelegramBotPoller:
    """گوش‌دهنده سریع پس‌زمینه برای پاسخ‌گویی به دکمه‌های منوی هفته قبل/آینده و انتخاب سلف خوابگاه/مرکزی."""

    def __init__(
        self,
        channel: TelegramNotificationChannel,
        auto_subscribe_getter: Any,
        invite_code_getter: Any,
        poll_interval_seconds: float = 2.5,
    ) -> None:
        self._channel = channel
        self._auto_subscribe_getter = auto_subscribe_getter
        self._invite_code_getter = invite_code_getter
        self._poll_interval = max(1.5, poll_interval_seconds)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._channel.has_bot_token:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="samad-telegram-bot-poller",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._channel.has_bot_token:
                    self._channel.sync_bot_updates(
                        auto_subscribe=bool(self._auto_subscribe_getter()),
                        invite_code=str(self._invite_code_getter() or ""),
                        handle_commands=True,
                    )
            except Exception as exc:
                logger.debug("خطای موقت در حلقه بررسی دستورات ربات تلگرام: %s", exc)
            if self._stop_event.wait(timeout=self._poll_interval):
                break
