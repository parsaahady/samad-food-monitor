"""قالب‌بندی فارسی شیک، تمیز و خوانای پیام‌های ربات تلگرام (HTML) و اعلان‌های دسکتاپ."""

from __future__ import annotations

from collections import Counter, defaultdict
from html import escape as _h
from typing import Dict, Iterable, List, Tuple

from samad_monitor.models import (
    EventType,
    Meal,
    Menu,
    MenuDiff,
    NotificationMessage,
    WeekRange,
)
from samad_monitor.parser import meal_rank, sort_and_deduplicate_meals
from samad_monitor.utils.date_utils import (
    format_jalali_day_month,
    format_jalali_verbose,
    get_persian_weekday,
    parse_iso_date,
    to_persian_digits,
)

EXCLUDED_NON_VELENJAK_DORM_KEYWORDS = (
    "عباسپور",
    "تهرانپارس",
    "حکیمیه",
    "ابوریحان",
    "سلامت",
    "ولیعصر",
    "نرگس",
    "رستگار",
    "هفت تیر",
    "۷ تیر",
    "7 تیر",
    "دانشکده",
    "اساتید",
)


def _norm_fa(text: str) -> str:
    return (text or "").replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ").strip()


def _meal_icon(meal_type: str) -> str:
    norm = _norm_fa(meal_type)
    if "صبحانه" in norm:
        return "🌅"
    if "ناهار" in norm:
        return "☀️"
    if "شام" in norm:
        return "🌙"
    if "سحری" in norm:
        return "🌌"
    if "افطار" in norm:
        return "🌆"
    return "🍽"


def _format_price_toman(price_rials: int) -> str:
    if price_rials <= 0:
        return ""
    tomans = price_rials // 10
    formatted = f"{tomans:,}".replace(",", "٬")
    return f"{to_persian_digits(formatted)} تومان"


def _format_food_item_html(meal: Meal) -> str:
    """قالب‌بندی تمیز یک گزینه غذا بدون شلوغی اضافه."""
    food = _h(meal.food_name.strip())
    parts = [food]
    if meal.side_dishes:
        parts.append(f"<i>({_h(meal.side_dishes.strip())})</i>")
    price_str = _format_price_toman(meal.price_rials)
    if price_str:
        parts.append(f"· <i>{_h(price_str)}</i>")
    return " ".join(parts)


def _render_grouped_meals(meals: Iterable[Meal], show_closed_tag: bool = False) -> str:
    """رندر بسیار تمیز و دسته‌بندی‌شده به تفکیک روز شمسی و وعده غذایی."""
    _ = show_closed_tag  # حذف برچسب شلوغ (بسته‌شده) از کارت تلگرام برای زیبایی پیام
    sorted_meals = sort_and_deduplicate_meals(meals)
    if not sorted_meals:
        return "<i>موردی برای نمایش ثبت نشده است.</i>"

    by_date: Dict[str, List[Meal]] = defaultdict(list)
    for m in sorted_meals:
        by_date[m.date].append(m)

    day_blocks: List[str] = []
    for iso_d in sorted(by_date.keys()):
        day_meals = sorted(
            by_date[iso_d],
            key=lambda x: (meal_rank(x.meal_type_id, x.meal_type), x.self_id, x.food_name),
        )
        parsed_d = parse_iso_date(iso_d)
        if parsed_d:
            day_title = f"{get_persian_weekday(parsed_d)} {format_jalali_day_month(parsed_d)}"
        else:
            day_title = _h(day_meals[0].day_name or iso_d)

        lines: List[str] = [f"🔹 <b>{day_title}</b>"]

        # گروه‌بندی گزینه‌های هم‌نام وعده (مثلاً دو غذای ناهار زیر یک تیتر «☀️ ناهار:»)
        grouped_by_type: List[Tuple[str, List[Meal]]] = []
        type_map: Dict[str, List[Meal]] = {}
        for dm in day_meals:
            mtype = dm.meal_type.strip() or "وعده"
            if mtype not in type_map:
                bucket: List[Meal] = []
                type_map[mtype] = bucket
                grouped_by_type.append((mtype, bucket))
            type_map[mtype].append(dm)

        for mtype, items in grouped_by_type:
            icon = _meal_icon(mtype)
            safe_mtype = _h(mtype)
            if len(items) == 1:
                lines.append(f"  {icon} <b>{safe_mtype}:</b> {_format_food_item_html(items[0])}")
            else:
                lines.append(f"  {icon} <b>{safe_mtype}:</b>")
                for it in items:
                    lines.append(f"     ▫️ {_format_food_item_html(it)}")

        day_blocks.append("\n".join(lines))

    return "\n\n".join(day_blocks)


def is_any_dormitory_self_name(restaurant_name: str) -> bool:
    norm = _norm_fa(restaurant_name)
    if "مرکزی" in norm:
        return False
    return any(kw in norm for kw in ("خوابگاه", "کوی", "سرا"))


def is_koy_dormitory_self_name(restaurant_name: str) -> bool:
    """تشخیص کاندیدای «خوابگاه کوی ولنجک» (با حذف خوابگاه‌های عباسپور، حکیمیه، ابوریحان و ...)."""
    norm = _norm_fa(restaurant_name)
    if "مرکزی" in norm:
        return False
    if any(bad in norm for bad in EXCLUDED_NON_VELENJAK_DORM_KEYWORDS):
        return False
    return "کوی" in norm


def select_koy_velenjak_meals(meals: Iterable[Meal]) -> List[Meal]:
    """
    انتخاب دقیق و انحصاری وعده‌های «سلف خوابگاه کوی ولنجک»:
    ۱. حذف تمام خوابگاه‌های غیر ولنجک (عباسپور، تهرانپارس، حکیمیه، ابوریحان، سلامت و ...).
    ۲. اولویت با سلف یارانه‌ای اصلی (حذف رستوران مکمل/آزاد در صورت وجود سلف اصلی).
    ۳. اولویت با نام دارای «ولنجک» در صورت وجود چند کوی.
    ۴. تضمین انتخاب حداکثر یک سلف واحد تا هرگز دو سلف با هم قاطی نشوند.
    """
    candidates = [m for m in meals if is_koy_dormitory_self_name(m.restaurant)]
    if not candidates:
        return []

    subsidized = [
        m
        for m in candidates
        if not m.is_free_restaurant
        and "مکمل" not in _norm_fa(m.restaurant)
        and "آزاد" not in _norm_fa(m.restaurant)
    ]
    pool = subsidized if subsidized else candidates

    velenjak_only = [m for m in pool if "ولنجک" in _norm_fa(m.restaurant)]
    if velenjak_only:
        pool = velenjak_only

    unique_self_ids = {m.self_id for m in pool}
    if len(unique_self_ids) > 1:
        primary_self_id = Counter(m.self_id for m in pool).most_common(1)[0][0]
        pool = [m for m in pool if m.self_id == primary_self_id]

    return pool


def select_central_self_meals(meals: Iterable[Meal]) -> List[Meal]:
    """انتخاب تمیز وعده‌های «سلف مرکزی» (بدون قاطی شدن با کافه‌تریاها یا مکمل‌ها)."""
    all_list = list(meals)
    explicit_central = [m for m in all_list if "مرکزی" in _norm_fa(m.restaurant)]
    pool = explicit_central if explicit_central else [
        m for m in all_list if not is_any_dormitory_self_name(m.restaurant)
    ]
    if not pool:
        return []

    subsidized = [
        m
        for m in pool
        if not m.is_free_restaurant
        and "مکمل" not in _norm_fa(m.restaurant)
        and "آزاد" not in _norm_fa(m.restaurant)
    ]
    if subsidized:
        pool = subsidized

    unique_self_ids = {m.self_id for m in pool}
    if len(unique_self_ids) > 1:
        primary_self_id = Counter(m.self_id for m in pool).most_common(1)[0][0]
        pool = [m for m in pool if m.self_id == primary_self_id]

    return pool


def filter_meals_by_self_scope(meals: Iterable[Meal], scope: str) -> Tuple[List[Meal], str]:
    """
    جداسازی وعده‌های غذایی بر اساس سلف:
    - `dorm`    -> فقط و فقط «خوابگاه کوی ولنجک»
    - `central` -> فقط «سلف مرکزی»
    - `all`     -> اولویت با خوابگاه کوی ولنجک (تا فقط منوی خوابگاه کوی ولنجک ارسال شود)
    """
    all_list = list(meals)
    clean_scope = (scope or "dorm").strip().lower()

    if clean_scope == "dorm":
        return select_koy_velenjak_meals(all_list), "🏠 خوابگاه کوی ولنجک"

    if clean_scope == "central":
        return select_central_self_meals(all_list), "🏛 سلف مرکزی"

    # در حالت پیش‌فرض نیز اگر خوابگاه کوی ولنجک موجود باشد، فقط همان ارسال شود
    koy = select_koy_velenjak_meals(all_list)
    if koy:
        return koy, "🏠 خوابگاه کوی ولنجک"
    central = select_central_self_meals(all_list)
    if central:
        return central, "🏛 سلف مرکزی"
    return all_list, "🍽 سلف دانشگاه"


def format_scoped_week_menu_message(
    meals: Iterable[Meal],
    week_range: WeekRange,
    scope: str,
    week_label_fa: str,
    is_past_week: bool = False,
    university_label: str = "دانشگاه شهید بهشتی",
    reserve_url: str = "https://samad.app/food/reserve",
) -> Tuple[str, int]:
    """ساخت کارت شیک و خلوت تلگرام (HTML) برای منوی هفته قبل / جاری / آینده."""
    all_meals = sort_and_deduplicate_meals(meals)
    filtered, scope_title = filter_meals_by_self_scope(all_meals, scope)

    jalali_span = _h(week_range.display_jalali_range())
    safe_uni = _h(university_label)
    safe_wlabel = _h(week_label_fa)

    lead_icon = "🏠" if scope == "dorm" else ("🏛" if scope == "central" else "🍽")
    clean_title = scope_title.replace("🏠 ", "").replace("🏛 ", "").replace("🍽 ", "")

    if not filtered:
        msg = (
            f"{lead_icon} <b>برنامه غذایی {_h(clean_title)}</b>\n"
            f"🗓 <b>{safe_wlabel}</b>  ·  {jalali_span}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ در این هفته برنامه‌ای برای <b>{_h(clean_title)}</b> در سامانه سماد ثبت نشده است."
        )
        return msg, 0

    actual_self_name = _h(filtered[0].restaurant)
    lines = [
        f"{lead_icon} <b>برنامه غذایی {_h(clean_title)}</b>",
        f"🗓 <b>{safe_wlabel}</b>  ·  {jalali_span}",
        f"🏛 {safe_uni}  <i>({actual_self_name})</i>",
        "━━━━━━━━━━━━━━━━━━",
        "",
        _render_grouped_meals(filtered),
    ]
    if not is_past_week:
        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━━━━━",
                f'🌐 <a href="{_h(reserve_url)}">ورود به سامانه رزرو سماد</a>',
            ]
        )
    return "\n".join(lines), len(filtered)


def format_menu_available_notification(
    menu: Menu,
    week_range: WeekRange,
    university_label: str = "دانشگاه شهید بهشتی",
    reserve_url: str = "https://samad.app/food/reserve",
    event_id: str = "",
) -> NotificationMessage:
    """ساخت پیام شیک انتشار اولیه منوی هفته آینده (منحصراً خوابگاه کوی ولنجک در صورت وجود)."""
    title = "🍽 منوی هفته آینده سماد منتشر شد!"

    koy_meals = select_koy_velenjak_meals(menu.meals)
    if koy_meals:
        target_meals = sort_and_deduplicate_meals(koy_meals)
        self_header = "🏠 خوابگاه کوی ولنجک"
    else:
        central_meals = select_central_self_meals(menu.meals)
        target_meals = sort_and_deduplicate_meals(central_meals or menu.meals)
        self_header = f"🏛 {target_meals[0].restaurant}" if target_meals else "سلف دانشگاه"

    days_count = len({m.date for m in target_meals})
    meals_count = len(target_meals)
    jalali_span = _h(
        f"{format_jalali_verbose(week_range.week_start)} تا {format_jalali_verbose(week_range.week_end)}"
    )

    header_lines = [
        f"<b>{_h(title)}</b>",
        f"<b>{_h(self_header)}</b>  ·  🏛 {_h(university_label)}",
        f"🗓 {jalali_span}",
        "━━━━━━━━━━━━━━━━━━",
        "",
        _render_grouped_meals(target_meals),
        "",
        "━━━━━━━━━━━━━━━━━━",
        f'🌐 <a href="{_h(reserve_url)}">ورود مستقیم به سامانه رزرو سماد</a>',
        f"🕒 <code>{week_range.start_iso} .. {week_range.end_iso}</code>",
    ]
    body = "\n".join(header_lines)

    short_summary = (
        f"{university_label} | {self_header} ({week_range.display_jalali_range()})\n"
        f"{to_persian_digits(meals_count)} گزینه غذایی در {to_persian_digits(days_count)} روز منتشر شد."
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
    """ساخت پیام شیک تغییرات منوی هفته آینده (متمرکز روی خوابگاه کوی ولنجک در صورت وجود)."""
    title = "🔄 تغییرات منوی هفته آینده سماد"

    koy_meals = select_koy_velenjak_meals(menu.meals)
    if koy_meals:
        target_meals = sort_and_deduplicate_meals(koy_meals)
        allowed_self_ids = {m.self_id for m in target_meals}
        self_header = "🏠 خوابگاه کوی ولنجک"
        added_meals = [m for m in diff.added_meals if m.self_id in allowed_self_ids] or list(diff.added_meals)
        removed_meals = [m for m in diff.removed_meals if m.self_id in allowed_self_ids]
        opened_meals = [m for m in diff.newly_opened_meals if m.self_id in allowed_self_ids]
    else:
        central_meals = select_central_self_meals(menu.meals)
        target_meals = sort_and_deduplicate_meals(central_meals or menu.meals)
        self_header = f"🏛 {target_meals[0].restaurant}" if target_meals else "سلف دانشگاه"
        added_meals = list(diff.added_meals)
        removed_meals = list(diff.removed_meals)
        opened_meals = list(diff.newly_opened_meals)

    change_sections: List[str] = []

    if diff.added_dates:
        formatted_dates = []
        for d_iso in diff.added_dates:
            pd = parse_iso_date(d_iso)
            formatted_dates.append(_h(format_jalali_verbose(pd)) if pd else _h(d_iso))
        change_sections.append("🟢 <b>روزهای جدید اضافه‌شده:</b>\n  ▫️ " + "\n  ▫️ ".join(formatted_dates))

    if added_meals:
        lines = ["➕ <b>غذاهای جدید یا ویرایش‌شده:</b>"]
        for m in added_meals:
            pd = parse_iso_date(m.date)
            d_label = f"{get_persian_weekday(pd)} {format_jalali_day_month(pd)}" if pd else _h(m.date)
            lines.append(f"  ▫️ <b>{d_label}</b> ({_h(m.meal_type)}): {_h(m.food_name)}")
        change_sections.append("\n".join(lines))

    if removed_meals:
        lines = ["➖ <b>غذاهای حذف یا جایگزین‌شده:</b>"]
        for m in removed_meals:
            pd = parse_iso_date(m.date)
            d_label = f"{get_persian_weekday(pd)} {format_jalali_day_month(pd)}" if pd else _h(m.date)
            lines.append(f"  ▫️ <b>{d_label}</b> ({_h(m.meal_type)}): {_h(m.food_name)}")
        change_sections.append("\n".join(lines))

    if opened_meals:
        lines = ["🔓 <b>وعده‌های تازه بازشده برای رزرو:</b>"]
        for m in opened_meals:
            pd = parse_iso_date(m.date)
            d_label = f"{get_persian_weekday(pd)} {format_jalali_day_month(pd)}" if pd else _h(m.date)
            lines.append(f"  ▫️ <b>{d_label}</b> ({_h(m.meal_type)}): {_h(m.food_name)}")
        change_sections.append("\n".join(lines))

    changes_text = "\n\n".join(change_sections) if change_sections else "<i>به‌روزرسانی در جزئیات برنامه غذایی</i>"
    jalali_span = _h(week_range.display_jalali_range())

    body_lines = [
        f"<b>{_h(title)}</b>",
        f"<b>{_h(self_header)}</b>  ·  🏛 {_h(university_label)}",
        f"🗓 {jalali_span}",
        "━━━━━━━━━━━━━━━━━━",
        changes_text,
        "━━━━━━━━━━━━━━━━━━",
        "📋 <b>برنامه کامل به‌روزشده:</b>",
        "",
        _render_grouped_meals(target_meals),
        "",
        "━━━━━━━━━━━━━━━━━━",
        f'🌐 <a href="{_h(reserve_url)}">ورود مستقیم به سامانه رزرو سماد</a>',
    ]
    body = "\n".join(body_lines)

    added_cnt = len(added_meals)
    days_cnt = len(diff.added_dates)
    summary_parts = []
    if days_cnt:
        summary_parts.append(f"{to_persian_digits(days_cnt)} روز جدید")
    if added_cnt:
        summary_parts.append(f"{to_persian_digits(added_cnt)} غذای جدید/تغییریافته")
    if opened_meals:
        summary_parts.append(f"{to_persian_digits(len(opened_meals))} وعده بازشده")
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
    detail_line = f"علت تشخیص: <code>{_h(reason)}</code>\n" if reason else ""
    body = (
        f"<b>{_h(title)}</b>\n"
        f"🏛 {_h(university_label)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{detail_line}"
        "بررسی خودکار منوی غذا تا زمان ورود مجدد متوقف شد تا درخواستی با نشست نامعتبر ارسال نشود.\n\n"
        "لطفاً روی سیستم خود دستور زیر را اجرا و وارد حساب سماد شوید:\n"
        "<code>python -m samad_monitor login</code>"
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
