"""محاسبات دقیق تاریخ و هفته دانشگاهی بر مبنای ساعت رسمی تهران (Asia/Tehran)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jdatetime

from samad_monitor.models import WeekRange

# پایتون: دوشنبه=0 ... پنجشنبه=3، جمعه=4، شنبه=5، یکشنبه=6
PYTHON_SATURDAY = 5
PYTHON_THURSDAY = 3
PYTHON_FRIDAY = 4

PERSIAN_DIGITS_MAP = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
LATIN_DIGITS_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

PERSIAN_WEEKDAYS_BY_PY_WEEKDAY = {
    5: "شنبه",
    6: "یکشنبه",
    0: "دوشنبه",
    1: "سه‌شنبه",
    2: "چهارشنبه",
    3: "پنجشنبه",
    4: "جمعه",
}

ENGLISH_TO_PERSIAN_WEEKDAY = {
    "saturday": "شنبه",
    "sunday": "یکشنبه",
    "monday": "دوشنبه",
    "tuesday": "سه‌شنبه",
    "wednesday": "چهارشنبه",
    "thursday": "پنجشنبه",
    "friday": "جمعه",
}

JALALI_MONTH_NAMES = {
    1: "فروردین",
    2: "اردیبهشت",
    3: "خرداد",
    4: "تیر",
    5: "مرداد",
    6: "شهریور",
    7: "مهر",
    8: "آبان",
    9: "آذر",
    10: "دی",
    11: "بهمن",
    12: "اسفند",
}

JALALI_MONTH_NUMBERS = {name: num for num, name in JALALI_MONTH_NAMES.items()}


def get_tehran_tz(tz_name: str = "Asia/Tehran") -> timezone | ZoneInfo:
    """دریافت منطقه زمانی تهران با پشتیبان ایمن UTC+03:30 (بدون تغییر ساعت تابستانی در ایران)."""
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")


def to_persian_digits(value: object) -> str:
    """تبدیل ارقام لاتین به ارقام فارسی برای نمایش در اعلان‌ها."""
    return str(value).translate(PERSIAN_DIGITS_MAP)


def to_latin_digits(value: str) -> str:
    """تبدیل ارقام فارسی و عربی به لاتین برای پردازش استاندارد."""
    return value.translate(LATIN_DIGITS_MAP)


def now_tehran(ref: Optional[datetime] = None, tz_name: str = "Asia/Tehran") -> datetime:
    """زمان فعلی (یا زمان مرجع ورودی) را دقیقاً در منطقه زمانی Asia/Tehran برمی‌گرداند."""
    tz = get_tehran_tz(tz_name)
    if ref is None:
        return datetime.now(tz)
    if ref.tzinfo is None:
        return ref.replace(tzinfo=tz)
    return ref.astimezone(tz)


def today_tehran(ref: Optional[datetime | date] = None, tz_name: str = "Asia/Tehran") -> date:
    """تاریخ امروز به وقت تهران را برمی‌گرداند (حتی اگر سرور/کامپیوتر در کشور دیگری باشد)."""
    if isinstance(ref, datetime):
        return now_tehran(ref, tz_name).date()
    if isinstance(ref, date):
        return ref
    return now_tehran(None, tz_name).date()


def get_week_start_saturday(target_date: date) -> date:
    """محاسبه روز شنبه ابتدای همان هفته دانشگاهی برای یک تاریخ مشخص."""
    days_since_saturday = (target_date.weekday() - PYTHON_SATURDAY) % 7
    return target_date - timedelta(days=days_since_saturday)


def get_current_week_range(
    ref: Optional[datetime | date] = None, tz_name: str = "Asia/Tehran"
) -> WeekRange:
    """محاسبه بازه هفته جاری دانشگاهی (شنبه تا جمعه) بر اساس تاریخ تهران."""
    current_day = today_tehran(ref, tz_name)
    saturday = get_week_start_saturday(current_day)
    friday = saturday + timedelta(days=6)
    return WeekRange(week_start=saturday, week_end=friday)


def get_next_week_range(
    ref: Optional[datetime | date] = None, tz_name: str = "Asia/Tehran"
) -> WeekRange:
    """محاسبه بازه هفته آینده دانشگاهی (شنبه هفته بعد تا جمعه هفته بعد) بر اساس تاریخ تهران."""
    current = get_current_week_range(ref, tz_name)
    next_saturday = current.week_start + timedelta(days=7)
    next_friday = next_saturday + timedelta(days=6)
    return WeekRange(week_start=next_saturday, week_end=next_friday)


def get_previous_week_range(
    ref: Optional[datetime | date] = None, tz_name: str = "Asia/Tehran"
) -> WeekRange:
    """محاسبه بازه هفته قبل دانشگاهی (شنبه هفته گذشته تا جمعه هفته گذشته) بر اساس تاریخ تهران."""
    current = get_current_week_range(ref, tz_name)
    prev_saturday = current.week_start - timedelta(days=7)
    prev_friday = prev_saturday + timedelta(days=6)
    return WeekRange(week_start=prev_saturday, week_end=prev_friday)


def is_peak_monitoring_day(
    ref: Optional[datetime | date] = None, tz_name: str = "Asia/Tehran"
) -> bool:
    """بررسی اینکه آیا امروز پنجشنبه یا جمعه (روزهای پایانی هفته دانشگاهی) به وقت تهران است."""
    d = today_tehran(ref, tz_name)
    return d.weekday() in {PYTHON_THURSDAY, PYTHON_FRIDAY}


def parse_iso_date(raw: str) -> Optional[date]:
    """استخراج تاریخ استاندارد ISO از رشته‌های تاریخ سماد (مانند '2026-11-14' یا '2026-11-14 00:00:00')."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = to_latin_digits(raw.strip())
    date_part = re.split(r"[T\s+]", cleaned)[0]
    try:
        return date.fromisoformat(date_part)
    except ValueError:
        return None


def get_persian_weekday(d: date, server_day_translated: str = "") -> str:
    """نام فارسی روز هفته را بر اساس تاریخ واقعی برمی‌گرداند (با اولویت محاسبه دقیق تقویمی)."""
    computed = PERSIAN_WEEKDAYS_BY_PY_WEEKDAY.get(d.weekday())
    if computed:
        return computed
    key = (server_day_translated or "").strip().lower()
    return ENGLISH_TO_PERSIAN_WEEKDAY.get(key, server_day_translated or "")


def to_jalali_parts(d: date) -> Tuple[int, int, int]:
    """تبدیل تاریخ میلادی به (سال، ماه، روز) شمسی با پشتیبانی کامل از کبیسه و تغییر سال."""
    jd = jdatetime.date.fromgregorian(date=d)
    return jd.year, jd.month, jd.day


def format_jalali_numeric(d: date) -> str:
    """فرمت عددی تاریخ شمسی با ارقام فارسی مانند ۱۴۰۵/۰۸/۲۳."""
    jy, jm, jd = to_jalali_parts(d)
    return to_persian_digits(f"{jy:04d}/{jm:02d}/{jd:02d}")


def format_jalali_date(d: date, persian_digits: bool = False) -> str:
    """فرمت عددی تاریخ شمسی YYYY/MM/DD (لاتین یا فارسی)."""
    jy, jm, jd = to_jalali_parts(d)
    raw = f"{jy:04d}/{jm:02d}/{jd:02d}"
    return to_persian_digits(raw) if persian_digits else raw


def format_jalali_day_month(d: date) -> str:
    """فرمت کوتاه روز و ماه شمسی مانند «۲۳ آبان»."""
    _, jm, jd = to_jalali_parts(d)
    return f"{to_persian_digits(jd)} {JALALI_MONTH_NAMES[jm]}"


def format_jalali_full(d: date, include_weekday: bool = True) -> str:
    """فرمت کامل فارسی تاریخ شمسی مانند «شنبه ۲۳ آبان ۱۴۰۵»."""
    jy, jm, jd = to_jalali_parts(d)
    core = f"{to_persian_digits(jd)} {JALALI_MONTH_NAMES[jm]} {to_persian_digits(jy)}"
    if include_weekday:
        return f"{get_persian_weekday(d)} {core}"
    return core


format_jalali_verbose = format_jalali_full
is_peak_check_day = is_peak_monitoring_day


def format_week_range_persian(week: WeekRange, include_gregorian: bool = True) -> str:
    """نمایش خوانای بازه هفته به شمسی (با مدیریت تغییر ماه و سال شمسی و میلادی)."""
    sy, sm, sd = to_jalali_parts(week.week_start)
    ey, em, ed = to_jalali_parts(week.week_end)
    start_wd = get_persian_weekday(week.week_start)
    end_wd = get_persian_weekday(week.week_end)

    if sy == ey:
        shamsi_part = (
            f"{start_wd} {to_persian_digits(sd)} {JALALI_MONTH_NAMES[sm]} "
            f"تا {end_wd} {to_persian_digits(ed)} {JALALI_MONTH_NAMES[em]} {to_persian_digits(ey)}"
        )
    else:
        shamsi_part = (
            f"{start_wd} {to_persian_digits(sd)} {JALALI_MONTH_NAMES[sm]} {to_persian_digits(sy)} "
            f"تا {end_wd} {to_persian_digits(ed)} {JALALI_MONTH_NAMES[em]} {to_persian_digits(ey)}"
        )

    if include_gregorian:
        return f"{shamsi_part} ({week.start_iso} تا {week.end_iso})"
    return shamsi_part


def parse_persian_header_date(text: str) -> Optional[date]:
    """تبدیل عنوان تاریخ شمسی کارت‌های DOM سماد (مثلاً «شنبه، ۲۳ آبان ۱۴۰۵») به تاریخ میلادی ISO."""
    if not text:
        return None
    normalized = to_latin_digits(text.replace("\u200c", " ").replace("،", " "))
    normalized = " ".join(normalized.split())
    for month_name, month_num in JALALI_MONTH_NUMBERS.items():
        pattern = rf"\b(\d{{1,2}})\s+{re.escape(month_name)}\s+(\d{{4}})\b"
        match = re.search(pattern, normalized)
        if match:
            day_num = int(match.group(1))
            year_num = int(match.group(2))
            try:
                return jdatetime.date(year_num, month_num, day_num).togregorian()
            except ValueError:
                return None
    return None


def format_samad_query_date(week_start: date) -> str:
    """فرمت استاندارد پارامتر weekStartDate مورد انتظار API سماد ('YYYY-MM-DD 00:00:00')."""
    return f"{week_start.isoformat()} 00:00:00"
