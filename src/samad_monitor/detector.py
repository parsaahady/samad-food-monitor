"""موتور تشخیص انتشار اولیه و تغییرات منوی هفته آینده همراه با کلید یکتای رویداد."""

from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, List, Optional

from samad_monitor.models import (
    DetectionOutcome,
    EventType,
    Meal,
    MealAvailability,
    Menu,
    MenuDiff,
    WeekRange,
)
from samad_monitor.parser import sort_and_deduplicate_meals
from samad_monitor.utils.date_utils import now_tehran


def compute_menu_hash(meals: Iterable[Meal]) -> str:
    """
    محاسبه هش یکتا و پایدار (SHA-256) از محتوای منو.
    نکته طراحی: بسته شدن خودکار مهلت رزرو در طول زمان (OPEN -> CLOSED) نباید هش محتوای منو را
    به‌گونه‌ای تغییر دهد که هشدار کاذب «تغییر منو» صادر شود؛ اما تغییر غذاها، روزها، سلف‌ها یا باز شدن
    وعده جدید در امضای محتوا لحاظ می‌شود.
    """
    sorted_meals = sort_and_deduplicate_meals(meals)
    canonical_rows = [
        {
            "self_id": m.self_id,
            "restaurant": m.restaurant.strip(),
            "date": m.date,
            "meal_type_id": m.meal_type_id,
            "meal_type": m.meal_type.strip(),
            "food_name": " ".join(m.food_name.strip().split()),
            "is_open": m.availability == MealAvailability.OPEN.value,
        }
        for m in sorted_meals
    ]
    serialized = json.dumps(canonical_rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_core_food_hash(meals: Iterable[Meal]) -> str:
    """هش خالص ترکیب روزها، سلف‌ها، وعده‌ها و نام غذاها (مستقل از بسته شدن مهلت رزرو)."""
    sorted_meals = sort_and_deduplicate_meals(meals)
    canonical_rows = [
        {
            "self_id": m.self_id,
            "restaurant": m.restaurant.strip(),
            "date": m.date,
            "meal_type_id": m.meal_type_id,
            "meal_type": m.meal_type.strip(),
            "food_name": " ".join(m.food_name.strip().split()),
        }
        for m in sorted_meals
    ]
    serialized = json.dumps(canonical_rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def diff_menus(previous_meals: Iterable[Meal], current_meals: Iterable[Meal]) -> MenuDiff:
    """استخراج دقیق تفاوت‌ها بین منوی قبلی و منوی فعلی همان هفته."""
    prev_sorted = sort_and_deduplicate_meals(previous_meals)
    curr_sorted = sort_and_deduplicate_meals(current_meals)

    prev_dates = {m.date for m in prev_sorted}
    curr_dates = {m.date for m in curr_sorted}

    added_dates = sorted(curr_dates - prev_dates)
    removed_dates = sorted(prev_dates - curr_dates)

    prev_by_key: Dict[str, Meal] = {m.identity_key(): m for m in prev_sorted}
    curr_by_key: Dict[str, Meal] = {m.identity_key(): m for m in curr_sorted}

    added_meals = [curr_by_key[k] for k in curr_by_key.keys() - prev_by_key.keys()]
    removed_meals = [prev_by_key[k] for k in prev_by_key.keys() - curr_by_key.keys()]

    newly_opened: List[Meal] = []
    for k in curr_by_key.keys() & prev_by_key.keys():
        old_m = prev_by_key[k]
        new_m = curr_by_key[k]
        if (
            old_m.availability == MealAvailability.CLOSED.value
            and new_m.availability == MealAvailability.OPEN.value
        ):
            newly_opened.append(new_m)

    return MenuDiff(
        added_dates=added_dates,
        removed_dates=removed_dates,
        added_meals=sort_and_deduplicate_meals(added_meals),
        removed_meals=sort_and_deduplicate_meals(removed_meals),
        newly_opened_meals=sort_and_deduplicate_meals(newly_opened),
    )


def build_initial_event_id(week_start_iso: str) -> str:
    return f"{EventType.MENU_AVAILABLE.value}:{week_start_iso}"


def build_updated_event_id(week_start_iso: str, signature_hash: str) -> str:
    return f"{EventType.MENU_UPDATED.value}:{week_start_iso}:{signature_hash[:16]}"


def build_session_expired_event_id(session_fingerprint: str) -> str:
    return f"{EventType.SESSION_EXPIRED.value}:{session_fingerprint}"


class MenuDetector:
    """تشخیص‌دهنده وضعیت انتشار اولیه و تغییرات منوی هفته آینده."""

    def __init__(self, tz_name: str = "Asia/Tehran") -> None:
        self.tz_name = tz_name

    def evaluate(
        self,
        week_range: WeekRange,
        current_meals: List[Meal],
        previous_menu: Optional[Menu],
        source: str = "api",
    ) -> DetectionOutcome:
        """
        مقایسه وعده‌های استخراج‌شده فعلی با آخرین Snapshot ذخیره‌شده در دیتابیس.
        """
        normalized_meals = [
            m for m in sort_and_deduplicate_meals(current_meals)
            if week_range.start_iso <= m.date <= week_range.end_iso
        ]

        if not normalized_meals:
            return DetectionOutcome(
                event_type=None,
                event_id=None,
                menu=None,
                diff=None,
                reason="منوی معتبری برای تاریخ‌های هفته آینده منتشر نشده است.",
            )

        detected_iso = now_tehran(tz_name=self.tz_name).isoformat()
        full_hash = compute_menu_hash(normalized_meals)
        food_hash = compute_core_food_hash(normalized_meals)

        # حالت الف: انتشار اولیه منوی هفته آینده
        if previous_menu is None or not previous_menu.meals:
            new_menu = Menu(
                week_start=week_range.start_iso,
                week_end=week_range.end_iso,
                detected_at=detected_iso,
                updated_at=detected_iso,
                status="PUBLISHED",
                meals=normalized_meals,
                content_hash=full_hash,
                source=source,
            )
            return DetectionOutcome(
                event_type=EventType.MENU_AVAILABLE,
                event_id=build_initial_event_id(week_range.start_iso),
                menu=new_menu,
                diff=None,
                reason="منوی هفته آینده برای نخستین بار شناسایی شد.",
            )

        # حالت ب: بررسی تغییرات نسبت به منوی ذخیره‌شده قبلی
        diff = diff_menus(previous_menu.meals, normalized_meals)
        if not diff.has_meaningful_changes:
            unchanged_menu = Menu(
                id=previous_menu.id,
                week_start=week_range.start_iso,
                week_end=week_range.end_iso,
                detected_at=previous_menu.detected_at,
                updated_at=detected_iso,
                status=previous_menu.status,
                meals=normalized_meals,
                content_hash=previous_menu.content_hash or full_hash,
                source=source,
            )
            return DetectionOutcome(
                event_type=None,
                event_id=None,
                menu=unchanged_menu,
                diff=diff,
                reason="منوی هفته آینده بدون تغییر نسبت به بررسی قبلی است.",
            )

        # اگر باز شدن جدیدی هم رخ داده از هش کامل استفاده می‌کنیم، در غیر این صورت از هش غذاها
        sig_hash = full_hash if diff.newly_opened_meals else food_hash
        updated_menu = Menu(
            id=previous_menu.id,
            week_start=week_range.start_iso,
            week_end=week_range.end_iso,
            detected_at=previous_menu.detected_at,
            updated_at=detected_iso,
            status="UPDATED",
            meals=normalized_meals,
            content_hash=sig_hash,
            source=source,
        )
        return DetectionOutcome(
            event_type=EventType.MENU_UPDATED,
            event_id=build_updated_event_id(week_range.start_iso, sig_hash),
            menu=updated_menu,
            diff=diff,
            reason="تغییر در غذاها یا روزهای منوی هفته آینده شناسایی شد.",
        )
