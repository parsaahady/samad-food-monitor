"""کانال رسمی Telegram Bot API با پشتیبانی از چند گیرنده، کشف خودکار کاربران، دستورات ربات و پروکسی مجزا."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import httpx

from samad_monitor.models import EventType, NotificationMessage
from samad_monitor.notifications.base import NotificationChannel
from samad_monitor.notifications.formatter import format_menu_available_notification
from samad_monitor.utils.date_utils import get_next_week_range, now_tehran

if TYPE_CHECKING:
    from samad_monitor.database.repository import MonitorRepository

logger = logging.getLogger("samad_monitor.notifications.telegram")

TELEGRAM_MAX_MESSAGE_LENGTH = 3900


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


def _extract_chat_meta(chat_obj: Dict[str, Any], from_obj: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str, str]:
    """استخراج (chat_id, display_name, username, chat_type) از آبجکت آپدیت تلگرام."""
    cid = str(chat_obj.get("id") or "").strip()
    chat_type = str(chat_obj.get("type") or "private").strip()
    username = str(chat_obj.get("username") or (from_obj or {}).get("username") or "").strip().lstrip("@")

    if chat_type in {"group", "supergroup", "channel"}:
        title = str(chat_obj.get("title") or "").strip()
        display_name = title or (f"@{username}" if username else f"گروه {cid}")
    else:
        first = str(chat_obj.get("first_name") or (from_obj or {}).get("first_name") or "").strip()
        last = str(chat_obj.get("last_name") or (from_obj or {}).get("last_name") or "").strip()
        full = f"{first} {last}".strip()
        display_name = full or (f"@{username}" if username else f"کاربر {cid}")

    return cid, display_name, username, chat_type


class TelegramNotificationChannel(NotificationChannel):
    """ارسال اعلان به تلگرام از طریق Telegram Bot API رسمی با پشتیبانی از لیست مشترکین دیتابیس."""

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

        if self._repository is not None and self._chat_ids:
            try:
                self._repository.sync_env_chat_ids(self._chat_ids)
            except Exception as exc:
                logger.debug("خطای جزئی در همگام‌سازی اولیه گیرندگان .env با دیتابیس: %s", exc)

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
        # اگر کاربر تمام ردیف‌ها را دستکاری نکرده و هنوز دیتابیس خالی بود، از _chat_ids استفاده کن
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
    ) -> None:
        """ارسال متن به یک گیرنده مشخص با تقسیم هوشمند پیام‌های بلند و مدیریت خطای ۴۲۹."""
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
                payload = {
                    "chat_id": cid,
                    "text": prefix + chunk,
                    "disable_web_page_preview": True,
                }

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

        succeeded_chats: List[str] = []
        failed_errors: List[str] = []

        with self._build_client() as client:
            for idx, target_chat in enumerate(recipients):
                if idx > 0 and self._http_transport is None:
                    # رعایت سقف نرخ ارسال گروهی تلگرام
                    time.sleep(0.05)
                try:
                    self.send_text_to_chat(target_chat=target_chat, text=message.body, client=client)
                    succeeded_chats.append(target_chat)
                except Exception as exc:
                    err_str = str(exc)
                    failed_errors.append(f"{target_chat}: {err_str}")
                    # اگر خطای سمت گیرنده خاص باشد (مثلاً کاربر ربات را بلاک کرده یا استارت نزده)،
                    # ارسال برای سایر دوستان نباید متوقف شود!
                    logger.warning(
                        "ارسال اعلان به گیرنده %s ناموفق بود (%s)؛ ادامه ارسال به سایر گیرندگان...",
                        target_chat,
                        err_str,
                    )

        if not succeeded_chats:
            raise RuntimeError("؛ ".join(failed_errors) if failed_errors else "ارسال به تمام گیرندگان ناموفق بود.")

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
    # پروفایل ربات و دریافت خودکار پیام‌دهندگان / دستورات (/start, /menu, /stop)
    # ------------------------------------------------------------------
    def get_bot_profile(self, timeout_seconds: float = 10.0) -> Dict[str, Any]:
        """استعلام نام و یوزرنیم ربات از Telegram getMe برای ساخت لینک دعوت آماده اشتراک‌گذاری."""
        if not self._bot_token:
            return {"ok": False, "message": "ابتدا توکن ربات تلگرام را در تب تنظیمات ذخیره کنید."}
        endpoint = f"{self._api_base_url}/bot{self._bot_token}/getMe"
        try:
            with self._build_client(custom_timeout=timeout_seconds) as client:
                resp = client.get(endpoint)
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
        خواندن پیام‌های جدید ارسال‌شده به ربات تلگرام (`getUpdates`):
        - ذخیره تمام افراد/گروه‌های پیام‌دهنده در جدول `telegram_recent_contacts` برای افزودن تک‌کلیکی از پنل
        - پردازش اختیاری دستورات `/start`، `/menu`، `/stop` و `/id` برای راحتی دوستان
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
        params: Dict[str, Any] = {"timeout": 0, "limit": 100}
        if last_offset_str and last_offset_str.isdigit():
            params["offset"] = int(last_offset_str) + 1

        endpoint = f"{self._api_base_url}/bot{self._bot_token}/getUpdates"
        try:
            with self._build_client(custom_timeout=12.0) as client:
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

                    msg = upd.get("message") or upd.get("channel_post")
                    my_member = upd.get("my_chat_member")

                    if my_member and isinstance(my_member.get("chat"), dict):
                        cid, dname, uname, ctype = _extract_chat_meta(
                            my_member["chat"], my_member.get("from")
                        )
                        new_status = str((my_member.get("new_chat_member") or {}).get("status") or "")
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

                    parts = text.split(maxsplit=1)
                    cmd = parts[0].split("@")[0].lower()
                    arg = parts[1].strip() if len(parts) > 1 else ""

                    if cmd == "/start":
                        existing = self._repository.get_telegram_subscriber(cid)
                        clean_code = invite_code.strip()
                        code_ok = (not clean_code) or (arg == clean_code)

                        if existing and existing.get("enabled"):
                            self._safe_reply(
                                client,
                                cid,
                                f"✅ سلام {dname} عزیز!\n"
                                f"عضویت شما در ربات اطلاع‌رسانی سلف {self._university_label} فعال است.\n\n"
                                "🍽 دستور /menu : مشاهده آخرین منوی ذخیره‌شده هفته آینده\n"
                                "🔕 دستور /stop : لغو دریافت اعلان",
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
                            auto_added_count += 1
                            self._safe_reply(
                                client,
                                cid,
                                f"🎉 سلام {dname} عزیز!\n"
                                f"عضویت شما در ربات اطلاع‌رسانی منوی سلف {self._university_label} با موفقیت فعال شد.\n"
                                "به محض انتشار یا تغییر برنامه غذایی هفته آینده، لیست کامل وعده‌ها برایتان ارسال می‌شود.\n\n"
                                "🍽 دستور /menu : مشاهده آخرین منوی ذخیره‌شده\n"
                                "🔕 دستور /stop : لغو عضویت",
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

                    elif cmd in {"/stop", "/unsubscribe", "لغو"}:
                        existing = self._repository.get_telegram_subscriber(cid)
                        if existing and existing.get("enabled"):
                            self._repository.update_telegram_subscriber(cid, enabled=False)
                            self._safe_reply(
                                client,
                                cid,
                                "🔕 دریافت اعلان‌های منوی غذا برای شما غیرفعال شد.\n"
                                "هر زمان مایل بودید با ارسال مجدد /start می‌توانید دوباره عضو شوید.",
                            )

                    elif cmd in {"/menu", "منو"}:
                        existing = self._repository.get_telegram_subscriber(cid)
                        if not (existing and existing.get("enabled")):
                            self._safe_reply(
                                client,
                                cid,
                                "⚠️ شما هنوز در لیست گیرندگان فعال این ربات قرار ندارید. ابتدا دستور /start را ارسال کنید.",
                            )
                        else:
                            reply_text = self._build_cached_menu_reply()
                            self._safe_reply(client, cid, reply_text)

                    elif cmd in {"/id", "آیدی"}:
                        self._safe_reply(client, cid, f"🆔 شناسه عددی این گفتگو: {cid}")

                if max_update_id is not None:
                    self._repository.set_runtime_value("telegram_last_update_id", str(max_update_id))

                return {
                    "ok": True,
                    "new_updates": len(updates),
                    "discovered_chats": discovered_chats,
                    "auto_added": auto_added_count,
                    "message": (
                        f"{len(updates)} پیام جدید بررسی شد."
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

    def _build_cached_menu_reply(self) -> str:
        assert self._repository is not None
        tehran_now = now_tehran(tz_name=self._timezone_name)
        next_week = get_next_week_range(tehran_now, tz_name=self._timezone_name)
        menu = self._repository.get_menu_by_week(next_week.start_iso)
        if menu and menu.meals:
            notif = format_menu_available_notification(
                menu=menu,
                week_range=next_week,
                university_label=self._university_label,
                reserve_url=self._reserve_url,
                event_id="BOT_MENU_QUERY",
            )
            return notif.body
        return (
            f"📭 منوی هفته آینده ({next_week.display_bilingual()}) هنوز در آخرین بررسی سامانه سماد منتشر نشده است.\n"
            "به محض انتشار، لیست کامل غذاها خودکار برایتان ارسال خواهد شد!"
        )

    def _safe_reply(self, client: httpx.Client, chat_id: str, text: str) -> None:
        try:
            self.send_text_to_chat(target_chat=chat_id, text=text, client=client)
        except Exception as exc:
            logger.debug("ارسال پاسخ دستور ربات به %s ناموفق بود: %s", chat_id, exc)


class TelegramBotPoller:
    """گوش‌دهنده سبک پس‌زمینه برای پاسخ‌گویی به /start، /menu و /stop در تلگرام بدون هیچ فشاری به سماد."""

    def __init__(
        self,
        channel: TelegramNotificationChannel,
        auto_subscribe_getter: Any,
        invite_code_getter: Any,
        poll_interval_seconds: float = 6.0,
    ) -> None:
        self._channel = channel
        self._auto_subscribe_getter = auto_subscribe_getter
        self._invite_code_getter = invite_code_getter
        self._poll_interval = max(3.0, poll_interval_seconds)
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
