"""ریپوزیتوری عملیات خواندن و نوشتن منوها، تاریخچه پایش، وضعیت نشست و جلوگیری از تکرار اعلان."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional, Set

from samad_monitor.database.sqlite import DatabaseManager
from samad_monitor.models import CheckStatus, EventType, Meal, Menu
from samad_monitor.utils.date_utils import now_tehran


class MonitorRepository:
    """دسترسی سطح بالا به داده‌های پایدار سامانه پایش سماد."""

    def __init__(self, db: DatabaseManager, tz_name: str = "Asia/Tehran") -> None:
        self.db = db
        self.tz_name = tz_name

    # ------------------------------------------------------------------
    # مدیریت Menu و Meal
    # ------------------------------------------------------------------
    def get_menu_by_week(self, week_start_iso: str) -> Optional[Menu]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM menus WHERE week_start = ?", (week_start_iso,)
            ).fetchone()
            if row is None:
                return None

            meal_rows = conn.execute(
                "SELECT * FROM meals WHERE menu_id = ? ORDER BY date ASC, meal_type_id ASC, id ASC",
                (row["id"],),
            ).fetchall()

            meals = [
                Meal(
                    date=m["date"],
                    day_name=m["day_name"],
                    meal_type=m["meal_type"],
                    meal_type_id=int(m["meal_type_id"]),
                    restaurant=m["restaurant"],
                    self_id=int(m["self_id"]),
                    is_free_restaurant=bool(m["is_free_restaurant"]),
                    food_name=m["food_name"],
                    availability=m["availability"],
                    program_id=m["program_id"],
                    food_type_id=m["food_type_id"],
                    side_dishes=m["side_dishes"] or "",
                    price_rials=int(m["price_rials"] or 0),
                    quota_limit=m["quota_limit"] or "",
                )
                for m in meal_rows
            ]

            return Menu(
                id=int(row["id"]),
                week_start=row["week_start"],
                week_end=row["week_end"],
                detected_at=row["detected_at"],
                updated_at=row["updated_at"],
                status=row["status"],
                meals=meals,
                content_hash=row["content_hash"],
                source=row["source"],
            )

    def get_latest_menu(self) -> Optional[Menu]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT week_start FROM menus ORDER BY week_start DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
        return self.get_menu_by_week(str(row["week_start"]))

    def save_menu(self, menu: Menu, snapshot_event_type: Optional[EventType] = None) -> Menu:
        """ذخیره یا به‌روزرسانی اتمیک منو و ردیف‌های وعده غذایی آن هفته."""
        now_iso = now_tehran(tz_name=self.tz_name).isoformat()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT id, detected_at FROM menus WHERE week_start = ?",
                (menu.week_start,),
            ).fetchone()

            if existing is None:
                cur = conn.execute(
                    """
                    INSERT INTO menus (week_start, week_end, detected_at, updated_at, status, content_hash, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        menu.week_start,
                        menu.week_end,
                        menu.detected_at or now_iso,
                        menu.updated_at or now_iso,
                        menu.status,
                        menu.content_hash,
                        menu.source,
                    ),
                )
                menu_id = int(cur.lastrowid)
                detected_at = menu.detected_at or now_iso
            else:
                menu_id = int(existing["id"])
                detected_at = str(existing["detected_at"])
                conn.execute(
                    """
                    UPDATE menus
                    SET week_end = ?, updated_at = ?, status = ?, content_hash = ?, source = ?
                    WHERE id = ?
                    """,
                    (
                        menu.week_end,
                        menu.updated_at or now_iso,
                        menu.status,
                        menu.content_hash,
                        menu.source,
                        menu_id,
                    ),
                )
                conn.execute("DELETE FROM meals WHERE menu_id = ?", (menu_id,))

            for meal in menu.meals:
                conn.execute(
                    """
                    INSERT INTO meals (
                        menu_id, date, day_name, meal_type, meal_type_id,
                        restaurant, self_id, is_free_restaurant, food_name,
                        availability, program_id, food_type_id, side_dishes,
                        price_rials, quota_limit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        menu_id,
                        meal.date,
                        meal.day_name,
                        meal.meal_type,
                        meal.meal_type_id,
                        meal.restaurant,
                        meal.self_id,
                        1 if meal.is_free_restaurant else 0,
                        meal.food_name,
                        meal.availability,
                        meal.program_id,
                        meal.food_type_id,
                        meal.side_dishes,
                        meal.price_rials,
                        meal.quota_limit,
                    ),
                )

            if snapshot_event_type is not None:
                meals_json = json.dumps(
                    [asdict(m) for m in menu.meals], ensure_ascii=False
                )
                conn.execute(
                    """
                    INSERT INTO menu_snapshots (
                        week_start, week_end, content_hash, event_type, captured_at, meals_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        menu.week_start,
                        menu.week_end,
                        menu.content_hash,
                        snapshot_event_type.value,
                        now_iso,
                        meals_json,
                    ),
                )

        menu.id = menu_id
        menu.detected_at = detected_at
        return menu

    # ------------------------------------------------------------------
    # جلوگیری از ارسال تکراری اعلان (Idempotency)
    # ------------------------------------------------------------------
    def get_delivered_channels(self, event_id: str) -> Set[str]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT channels_succeeded FROM sent_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return set()
            try:
                items = json.loads(row["channels_succeeded"])
                return {str(x) for x in items} if isinstance(items, list) else set()
            except Exception:
                return set()

    def is_event_fully_delivered(self, event_id: str, required_channels: Iterable[str]) -> bool:
        req = {c for c in required_channels if c}
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT fully_delivered, channels_succeeded FROM sent_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return False
            if not req:
                return bool(row["fully_delivered"])
            try:
                delivered = set(json.loads(row["channels_succeeded"]))
            except Exception:
                delivered = set()
            return req.issubset(delivered)

    def has_any_delivery_for_event(self, event_id: str) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT fully_delivered, channels_succeeded FROM sent_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return False
            if row["fully_delivered"]:
                return True
            try:
                return len(json.loads(row["channels_succeeded"])) > 0
            except Exception:
                return False

    def record_event_delivery(
        self,
        event_id: str,
        event_type: EventType,
        week_start: Optional[str],
        succeeded_channels: Iterable[str],
        failed_channels: Iterable[str],
        required_channels: Iterable[str],
    ) -> None:
        now_iso = now_tehran(tz_name=self.tz_name).isoformat()
        already_ok = self.get_delivered_channels(event_id)
        merged_ok = sorted(already_ok | {c for c in succeeded_channels if c})
        failed_only = sorted({c for c in failed_channels if c} - set(merged_ok))
        req_set = {c for c in required_channels if c}
        fully = 1 if (not req_set or req_set.issubset(set(merged_ok))) and not failed_only else 0

        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT created_at FROM sent_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO sent_events (
                        event_id, event_type, week_start, created_at, updated_at,
                        channels_succeeded, channels_failed, fully_delivered
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type.value,
                        week_start,
                        now_iso,
                        now_iso,
                        json.dumps(merged_ok, ensure_ascii=False),
                        json.dumps(failed_only, ensure_ascii=False),
                        fully,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE sent_events
                    SET updated_at = ?, channels_succeeded = ?, channels_failed = ?, fully_delivered = ?
                    WHERE event_id = ?
                    """,
                    (
                        now_iso,
                        json.dumps(merged_ok, ensure_ascii=False),
                        json.dumps(failed_only, ensure_ascii=False),
                        fully,
                        event_id,
                    ),
                )

    # ------------------------------------------------------------------
    # ثبت تاریخچه بررسی‌ها، خطاها و وضعیت اجرایی
    # ------------------------------------------------------------------
    def record_check_run(
        self,
        target_week_start: str,
        target_week_end: str,
        status: CheckStatus,
        source: str,
        meals_count: int,
        duration_ms: int,
        message: str,
        event_id: Optional[str] = None,
        http_status: Optional[int] = None,
        samad_code: Optional[int] = None,
    ) -> None:
        now_iso = now_tehran(tz_name=self.tz_name).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO check_history (
                    checked_at, target_week_start, target_week_end, status,
                    source, meals_count, event_id, duration_ms, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso,
                    target_week_start,
                    target_week_end,
                    status.value,
                    source,
                    meals_count,
                    event_id,
                    duration_ms,
                    message,
                ),
            )

            # خطاهای فنی واقعی در جدول error_logs نیز ثبت می‌شوند (نه عدم انتشار عادی منو)
            if status not in {CheckStatus.MENU_PUBLISHED, CheckStatus.MENU_NOT_PUBLISHED}:
                conn.execute(
                    """
                    INSERT INTO error_logs (logged_at, status, message, http_status, samad_code)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (now_iso, status.value, message, http_status, samad_code),
                )

            # نگهداری حداکثر ۲۰۰۰ رکورد تاریخچه برای جلوگیری از رشد بی‌رویه دیتابیس در طول سال‌ها
            conn.execute(
                """
                DELETE FROM check_history
                WHERE id NOT IN (SELECT id FROM check_history ORDER BY id DESC LIMIT 2000)
                """
            )
            conn.execute(
                """
                DELETE FROM error_logs
                WHERE id NOT IN (SELECT id FROM error_logs ORDER BY id DESC LIMIT 500)
                """
            )

    def set_runtime_value(self, key: str, value: str) -> None:
        now_iso = now_tehran(tz_name=self.tz_name).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now_iso),
            )

    def get_runtime_value(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM runtime_state WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row is not None else default

    def get_status_summary(self, next_week_start_iso: str) -> Dict[str, Any]:
        """جمع‌آوری خلاصه کامل وضعیت سیستم برای نمایش در دستور `status`."""
        with self.db.transaction() as conn:
            last_check = conn.execute(
                "SELECT * FROM check_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_success = conn.execute(
                """
                SELECT * FROM check_history
                WHERE status IN (?, ?)
                ORDER BY id DESC LIMIT 1
                """,
                (CheckStatus.MENU_PUBLISHED.value, CheckStatus.MENU_NOT_PUBLISHED.value),
            ).fetchone()
            last_error = conn.execute(
                "SELECT * FROM error_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            total_checks_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM check_history"
            ).fetchone()
            sent_events_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM sent_events WHERE fully_delivered = 1"
            ).fetchone()

        next_week_menu = self.get_menu_by_week(next_week_start_iso)
        latest_menu = self.get_latest_menu()

        return {
            "last_check": dict(last_check) if last_check else None,
            "last_successful_check": dict(last_success) if last_success else None,
            "last_error": dict(last_error) if last_error else None,
            "next_week_menu": next_week_menu,
            "latest_menu": latest_menu,
            "total_checks": int(total_checks_row["cnt"]) if total_checks_row else 0,
            "total_sent_events": int(sent_events_row["cnt"]) if sent_events_row else 0,
            "next_scheduled_check_at": self.get_runtime_value("next_scheduled_check_at"),
            "session_status": self.get_runtime_value("session_status", "UNKNOWN"),
            "paused_for_relogin": self.get_runtime_value("paused_for_relogin", "false") == "true",
            "last_username": self.get_runtime_value("last_username"),
        }

    def get_recent_activity(self, limit: int = 25) -> Dict[str, List[Dict[str, Any]]]:
        """دریافت آخرین بررسی‌ها، رویدادهای اعلان و خطاهای ثبت‌شده برای نمایش در داشبورد گرافیکی."""
        safe_limit = max(1, min(int(limit), 100))
        with self.db.transaction() as conn:
            checks = conn.execute(
                "SELECT * FROM check_history ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            events = conn.execute(
                "SELECT * FROM sent_events ORDER BY updated_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            errors = conn.execute(
                "SELECT * FROM error_logs ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            weeks = conn.execute(
                "SELECT id, week_start, week_end, status, updated_at, source FROM menus ORDER BY week_start DESC LIMIT 12"
            ).fetchall()

        return {
            "checks": [dict(r) for r in checks],
            "events": [dict(r) for r in events],
            "errors": [dict(r) for r in errors],
            "saved_weeks": [dict(r) for r in weeks],
        }

    # ------------------------------------------------------------------
    # مدیریت مشترکین و گیرندگان ربات تلگرام (چندکاربره)
    # ------------------------------------------------------------------
    def sync_env_chat_ids(self, env_chat_ids: Iterable[str]) -> None:
        """ثبت خودکار شناسه‌های موجود در TELEGRAM_CHAT_ID فایل .env به‌عنوان مدیر (بدون بازنویسی تغییرات کاربر)."""
        cleaned = [str(c).strip() for c in env_chat_ids if str(c).strip()]
        if not cleaned:
            return
        now_iso = now_tehran(tz_name=self.tz_name).isoformat()
        with self.db.transaction() as conn:
            for idx, cid in enumerate(cleaned, start=1):
                existing = conn.execute(
                    "SELECT chat_id FROM telegram_subscribers WHERE chat_id = ?",
                    (cid,),
                ).fetchone()
                if existing is None:
                    label = "مدیر اصلی (.env)" if idx == 1 else f"گیرنده .env شماره {idx}"
                    chat_type = "group" if cid.startswith("-") else ("channel" if cid.startswith("@") else "private")
                    conn.execute(
                        """
                        INSERT INTO telegram_subscribers (
                            chat_id, display_name, username, chat_type, role, enabled, added_via, created_at, updated_at
                        ) VALUES (?, ?, '', ?, 'admin', 1, 'env', ?, ?)
                        """,
                        (cid, label, chat_type, now_iso, now_iso),
                    )

    def list_telegram_subscribers(self, only_enabled: bool = False) -> List[Dict[str, Any]]:
        query = """
            SELECT * FROM telegram_subscribers
            {where}
            ORDER BY CASE WHEN role = 'admin' THEN 0 ELSE 1 END ASC, created_at ASC
        """.format(where="WHERE enabled = 1" if only_enabled else "")
        with self.db.transaction() as conn:
            rows = conn.execute(query).fetchall()
        result: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["enabled"] = bool(d.get("enabled", 1))
            result.append(d)
        return result

    def get_telegram_subscriber(self, chat_id: str) -> Optional[Dict[str, Any]]:
        cid = str(chat_id or "").strip()
        if not cid:
            return None
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM telegram_subscribers WHERE chat_id = ?",
                (cid,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["enabled"] = bool(d.get("enabled", 1))
        return d

    def upsert_telegram_subscriber(
        self,
        chat_id: str,
        display_name: str = "",
        username: str = "",
        chat_type: str = "private",
        role: str = "subscriber",
        enabled: bool = True,
        added_via: str = "manual",
    ) -> Dict[str, Any]:
        cid = str(chat_id or "").strip()
        if not cid:
            raise ValueError("شناسه چت (Chat ID) نمی‌تواند خالی باشد.")
        clean_role = "admin" if str(role).strip().lower() == "admin" else "subscriber"
        clean_username = str(username or "").strip().lstrip("@")
        clean_name = str(display_name or "").strip()
        if not clean_name:
            clean_name = f"@{clean_username}" if clean_username else f"کاربر {cid}"
        inferred_type = str(chat_type or "").strip() or (
            "group" if cid.startswith("-") else ("channel" if cid.startswith("@") else "private")
        )
        now_iso = now_tehran(tz_name=self.tz_name).isoformat()

        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM telegram_subscribers WHERE chat_id = ?",
                (cid,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO telegram_subscribers (
                        chat_id, display_name, username, chat_type, role, enabled, added_via, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        clean_name,
                        clean_username,
                        inferred_type,
                        clean_role,
                        1 if enabled else 0,
                        added_via,
                        now_iso,
                        now_iso,
                    ),
                )
            else:
                final_name = clean_name if display_name.strip() else (existing["display_name"] or clean_name)
                final_user = clean_username or existing["username"] or ""
                final_role = (
                    "admin"
                    if existing["role"] == "admin" and added_via == "auto_start"
                    else clean_role
                )
                conn.execute(
                    """
                    UPDATE telegram_subscribers
                    SET display_name = ?, username = ?, chat_type = ?, role = ?, enabled = ?, updated_at = ?
                    WHERE chat_id = ?
                    """,
                    (
                        final_name,
                        final_user,
                        inferred_type,
                        final_role,
                        1 if enabled else 0,
                        now_iso,
                        cid,
                    ),
                )
        sub = self.get_telegram_subscriber(cid)
        assert sub is not None
        return sub

    def update_telegram_subscriber(
        self,
        chat_id: str,
        display_name: Optional[str] = None,
        role: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        cid = str(chat_id or "").strip()
        current = self.get_telegram_subscriber(cid)
        if current is None:
            return None
        new_name = str(display_name).strip() if display_name is not None and str(display_name).strip() else current["display_name"]
        new_role = (
            ("admin" if str(role).strip().lower() == "admin" else "subscriber")
            if role is not None
            else current["role"]
        )
        new_enabled = bool(enabled) if enabled is not None else bool(current["enabled"])
        now_iso = now_tehran(tz_name=self.tz_name).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE telegram_subscribers
                SET display_name = ?, role = ?, enabled = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (new_name, new_role, 1 if new_enabled else 0, now_iso, cid),
            )
        return self.get_telegram_subscriber(cid)

    def delete_telegram_subscriber(self, chat_id: str) -> bool:
        cid = str(chat_id or "").strip()
        if not cid:
            return False
        with self.db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM telegram_subscribers WHERE chat_id = ?",
                (cid,),
            )
            return cur.rowcount > 0

    def record_telegram_contact(
        self,
        chat_id: str,
        display_name: str = "",
        username: str = "",
        chat_type: str = "private",
        last_message: str = "",
    ) -> None:
        cid = str(chat_id or "").strip()
        if not cid:
            return
        now_iso = now_tehran(tz_name=self.tz_name).isoformat()
        clean_user = str(username or "").strip().lstrip("@")
        clean_name = str(display_name or "").strip() or (f"@{clean_user}" if clean_user else f"کاربر {cid}")
        clean_msg = str(last_message or "").strip()[:200]
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO telegram_recent_contacts (
                    chat_id, display_name, username, chat_type, last_message, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    username = CASE WHEN excluded.username != '' THEN excluded.username ELSE telegram_recent_contacts.username END,
                    chat_type = excluded.chat_type,
                    last_message = CASE WHEN excluded.last_message != '' THEN excluded.last_message ELSE telegram_recent_contacts.last_message END,
                    last_seen_at = excluded.last_seen_at
                """,
                (cid, clean_name, clean_user, chat_type or "private", clean_msg, now_iso),
            )

    def list_recent_telegram_contacts(self, limit: int = 50) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.*,
                    s.role AS subscriber_role,
                    s.enabled AS subscriber_enabled
                FROM telegram_recent_contacts c
                LEFT JOIN telegram_subscribers s ON s.chat_id = c.chat_id
                ORDER BY c.last_seen_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["is_subscribed"] = d.get("subscriber_role") is not None and bool(d.get("subscriber_enabled"))
            out.append(d)
        return out


