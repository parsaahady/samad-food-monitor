"""قالب‌بندی فارسی اعلان‌های انتشار اولیه منو، تغییرات منو و انقضای نشست."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List

from samad_monitor.models import (
    EventType,
    Meal,
    MealAvailability,
    Menu,
    MenuDiff,
    NotificationMessage,
    WeekRange,
)
from samad_monitor.parser import meal_rank, sort_and_deduplicate_meals
from samad_monitor.utils.date_utils import (
    format_jalali_verbose,
    get_persian_weekday,
    parse_iso_date,
    to_persian_digits,
)


def _format_price_toman(price_rials: int) -> str:
    if price_rials <= 0:
        return ""
    tomans = price_rials // 10
    formatted = f"{tomans:,}".replace(",", "٬")
    return f"{to_persian_digits(formatted)} تومان"


def _format_single_meal_line(meal: Meal, show_restaurant: bool) -> str:
    parts: List[str] = [f"• {meal.meal_type}: {meal.food_name}"]
    if meal.side_dishes:
        parts.append(f"(مخلفات: {meal.side_dishes})")

    price_str = _format_price_toman(meal.price_rials)
    if price_str:
        parts.append(f"— {price_str}")

    if show_restaurant:
        parts.append(f"[{meal.restaurant}]")

    if meal.availability == MealAvailability.CLOSED.value:
        parts.append("(بسته‌شده)")

    return " ".join(parts)


def _render_grouped_meals(meals: Iterable[Meal]) -> str:
    sorted_meals = sort_and_deduplicate_meals(meals)
    if not sorted_meals:
        return "موردی ثبت نشده است."

    unique_restaurants = {m.restaurant for m in sorted_meals}
    show_restaurant = len(unique_restaurants) > 1

    by_date: Dict[str, List[Meal]] = defaultdict(list)
    for m in sorted_meals:
        by_date[m.date].append(m)

    blocks: List[str] = []
    for iso_d in sorted(by_date.keys()):
        day_meals = sorted(
            by_date[iso_d],
            key=lambda x: (meal_rank(x.meal_type_id, x.meal_type), x.self_id, x.food_name),
        )
        parsed_d = parse_iso_date(iso_d)
        if parsed_d:
            day_header = f"📅 {format_jalali_verbose(parsed_d)} ({iso_d})"
        else:
            day_header = f"📅 {day_meals[0].day_name} ({iso_d})"

        lines = [day_header]
        for dm in day_meals:
            lines.append("  " + _format_single_meal_line(dm, show_restaurant=show_restaurant))
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def format_menu_available_notification(
    menu: Menu,
    week_range: WeekRange,
    university_label: str = "دانشگاه شهید بهشتی",
    reserve_url: str = "https://samad.app/food/reserve",
    event_id: str = "",
) -> NotificationMessage:
    """ساخت پیام فارسی استاندارد برای انتشار اولیه منوی هفته آینده."""
    title = "🍽 منوی هفته آینده سماد منتشر شد!"
    restaurants = sorted({m.restaurant for m in menu.meals})
    restaurants_str = "، ".join(restaurants) if restaurants else "سلف دانشگاه"
    days_count = len({m.date for m in menu.meals})
    meals_count = len(menu.meals)

    header_lines = [
        title,
        f"🏛 {university_label}",
        f"🗓 بازه هفته: {week_range.display_bilingual()}",
        f"🏢 سلف: {restaurants_str}",
        f"📊 تعداد کل گزینه‌ها: {to_persian_digits(meals_count)} وعده در {to_persian_digits(days_count)} روز",
        "────────────────────",
        _render_grouped_meals(menu.meals),
        "────────────────────",
        f"🔗 لینک رزرو: {reserve_url}",
    ]
    body = "\n".join(header_lines)

    short_summary = (
        f"{university_label} | بازه {week_range.display_jalali_range()}\n"
        f"{to_persian_digits(meals_count)} گزینه غذایی در {to_persian_digits(days_count)} روز "
        f"({restaurants_str}) منتشر شد."
    )

    return NotificationMessage(
        event_id=event_id or f"{EventType.MENU_AVAILABLE.value}:{week_range.start_iso}",
        event_type=EventType.MENU_AVAILABLE,
        title=title,
        body=body,
        short_summary=short_summary,
        action_url=reserve_url,
    )


def format_menu_updated_notification(
    menu: Menu,
    diff: MenuDiff,
    week_range: WeekRange,
    university_label: str = "دانشگاه شهید بهشتی",
    reserve_url: str = "https://samad.app/food/reserve",
    event_id: str = "",
) -> NotificationMessage:
    """ساخت پیام فارسی اختصاصی برای تغییرات منوی هفته آینده (متمایز از انتشار اولیه)."""
    title = "🔄 تغییرات منوی هفته آینده سماد"
    unique_restaurants = {m.restaurant for m in menu.meals}
    show_rest = len(unique_restaurants) > 1

    change_sections: List[str] = []

    if diff.added_dates:
        formatted_dates = []
        for d_iso in diff.added_dates:
            pd = parse_iso_date(d_iso)
            formatted_dates.append(f"{format_jalali_verbose(pd)} ({d_iso})" if pd else d_iso)
        change_sections.append("🟢 روزهای جدید اضافه‌شده:\n  • " + "\n  • ".join(formatted_dates))

    if diff.added_meals:
        lines = ["➕ غذاها / وعده‌های جدید یا ویرایش‌شده:"]
        for m in diff.added_meals:
            pd = parse_iso_date(m.date)
            d_label = format_jalali_verbose(pd) if pd else f"{get_persian_weekday(pd)} {m.date}" if pd else m.date
            rest_tag = f" [{m.restaurant}]" if show_rest else ""
            lines.append(f"  • {d_label} — {m.meal_type}: {m.food_name}{rest_tag}")
        change_sections.append("\n".join(lines))

    if diff.removed_meals:
        lines = ["➖ غذاهای حذف یا جایگزین‌شده:"]
        for m in diff.removed_meals:
            pd = parse_iso_date(m.date)
            d_label = format_jalali_verbose(pd) if pd else m.date
            rest_tag = f" [{m.restaurant}]" if show_rest else ""
            lines.append(f"  • {d_label} — {m.meal_type}: {m.food_name}{rest_tag}")
        change_sections.append("\n".join(lines))

    if diff.newly_opened_meals:
        lines = ["🔓 وعده‌های تازه بازشده برای رزرو:"]
        for m in diff.newly_opened_meals:
            pd = parse_iso_date(m.date)
            d_label = format_jalali_verbose(pd) if pd else m.date
            lines.append(f"  • {d_label} — {m.meal_type}: {m.food_name}")
        change_sections.append("\n".join(lines))

    changes_text = "\n\n".join(change_sections) if change_sections else "به‌روزرسانی در جزئیات برنامه غذایی."

    body_lines = [
        title,
        f"🏛 {university_label}",
        f"🗓 بازه هفته: {week_range.display_bilingual()}",
        "────────────────────",
        changes_text,
        "────────────────────",
        "📋 برنامه کامل به‌روزشده هفته آینده:",
        _render_grouped_meals(menu.meals),
        "────────────────────",
        f"🔗 لینک رزرو: {reserve_url}",
    ]
    body = "\n".join(body_lines)

    added_cnt = len(diff.added_meals)
    days_cnt = len(diff.added_dates)
    summary_parts = []
    if days_cnt:
        summary_parts.append(f"{to_persian_digits(days_cnt)} روز جدید")
    if added_cnt:
        summary_parts.append(f"{to_persian_digits(added_cnt)} غذای جدید/تغییریافته")
    if diff.newly_opened_meals:
        summary_parts.append(f"{to_persian_digits(len(diff.newly_opened_meals))} وعده بازشده")
    summary_detail = " و ".join(summary_parts) or "تغییر در برنامه غذایی"

    short_summary = (
        f"تغییر در منوی هفته آینده ({week_range.display_jalali_range()}): {summary_detail}."
    )

    return NotificationMessage(
        event_id=event_id or f"{EventType.MENU_UPDATED.value}:{week_range.start_iso}:{menu.content_hash[:16]}",
        event_type=EventType.MENU_UPDATED,
        title=title,
        body=body,
        short_summary=short_summary,
        action_url=reserve_url,
    )


def format_session_expired_notification(
    event_id: str,
    university_label: str = "دانشگاه شهید بهشتی",
    reason: str = "",
) -> NotificationMessage:
    """ساخت هشدار اختصاصی انقضای نشست کاربری سماد."""
    title = "⚠️ نشست کاربری سماد منقضی شده است"
    detail_line = f"علت تشخیص: {reason}\n" if reason else ""
    body = (
        f"{title}\n"
        f"🏛 {university_label}\n"
        "────────────────────\n"
        f"{detail_line}"
        "بررسی خودکار منوی غذا تا زمان ورود مجدد متوقف شد تا درخواستی با نشست نامعتبر ارسال نشود.\n\n"
        "لطفاً روی سیستم خود دستور زیر را اجرا و وارد حساب سماد شوید:\n"
        "python -m samad_monitor login"
    )
    short_summary = (
        "نشست سماد منقضی شده و پایش متوقف است. لطفاً دستور python -m samad_monitor login را اجرا کنید."
    )
    return NotificationMessage(
        event_id=event_id,
        event_type=EventType.SESSION_EXPIRED,
        title=title,
        body=body,
        short_summary=short_summary,
        action_url="https://samad.app/login?next=/food/reserve",
    )
