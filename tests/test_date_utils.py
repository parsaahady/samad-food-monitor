"""تست‌های جامع محاسبات تاریخ، منطقه زمانی تهران و مرزهای ماه و سال (سناریوی ۹)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from samad_monitor.utils.date_utils import (
    format_jalali_date,
    get_current_week_range,
    get_next_week_range,
    is_peak_check_day,
    parse_persian_header_date,
)

TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def test_next_week_from_every_day_of_week() -> None:
    """در تمام روزهای هفته جاری (شنبه تا جمعه)، هفته آینده باید دقیقاً شنبه تا جمعه بعدی باشد."""
    # هفته جاری: شنبه 2026-11-07 تا جمعه 2026-11-13
    expected_next_start = date(2026, 11, 14)
    expected_next_end = date(2026, 11, 20)

    for day_offset in range(7):
        ref = datetime(2026, 11, 7 + day_offset, 14, 30, tzinfo=TEHRAN_TZ)
        curr = get_current_week_range(ref)
        nxt = get_next_week_range(ref)

        assert curr.week_start == date(2026, 11, 7)
        assert curr.week_end == date(2026, 11, 13)
        assert nxt.week_start == expected_next_start
        assert nxt.week_end == expected_next_end


def test_tehran_timezone_overrides_utc_host_clock() -> None:
    """
    اگر ساعت سرور روی UTC هنوز جمعه ۲۲:۰۰ باشد (2026-11-13 22:00 UTC)،
    در تهران ساعت ۰۱:۳۰ بامداد شنبه 2026-11-14 است و هفته جدید آغاز شده است!
    """
    utc_friday_night = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)
    nxt = get_next_week_range(utc_friday_night)

    # چون در تهران وارد شنبه ۱۴ نوامبر شده‌ایم، هفته آینده باید ۲۱ تا ۲۷ نوامبر باشد
    assert nxt.week_start == date(2026, 11, 21)
    assert nxt.week_end == date(2026, 11, 27)


def test_gregorian_year_boundary_rollover() -> None:
    """عبور هفته آینده از مرز سال میلادی (دسامبر ۲۰۲۶ به ژانویه ۲۰۲۷)."""
    ref = datetime(2026, 12, 30, 11, 0, tzinfo=TEHRAN_TZ)  # چهارشنبه ۹ دی ۱۴۰۵
    nxt = get_next_week_range(ref)
    assert nxt.week_start == date(2027, 1, 2)
    assert nxt.week_end == date(2027, 1, 8)
    assert len(nxt.all_dates()) == 7


def test_shamsi_month_and_nowruz_year_rollover() -> None:
    """عبور دقیق بازه هفته از مرز ماه‌های شمسی (آبان به آذر) و مرز سال نو شمسی (اسفند ۱۴۰۵ به فروردین ۱۴۰۶)."""
    # ۱. مرز ماه آبان به آذر ۱۴۰۵
    ref_aban = datetime(2026, 11, 16, 9, 0, tzinfo=TEHRAN_TZ)  # دوشنبه ۲۵ آبان ۱۴۰۵
    nxt_az = get_next_week_range(ref_aban)
    # شنبه ۳۰ آبان ۱۴۰۵ (2026-11-21) تا جمعه ۶ آذر ۱۴۰۵ (2026-11-27)
    assert nxt_az.week_start == date(2026, 11, 21)
    assert nxt_az.week_end == date(2026, 11, 27)
    assert format_jalali_date(nxt_az.week_start) == "1405/08/30"
    assert format_jalali_date(nxt_az.week_end) == "1405/09/06"

    # ۲. مرز تحویل سال شمسی (پنجشنبه ۲۷ اسفند ۱۴۰۵ -> هفته آینده: شنبه ۲۹ اسفند ۱۴۰۵ تا جمعه ۶ فروردین ۱۴۰۶)
    ref_esfand = datetime(2027, 3, 18, 18, 0, tzinfo=TEHRAN_TZ)
    nxt_nowruz = get_next_week_range(ref_esfand)
    assert nxt_nowruz.week_start == date(2027, 3, 20)
    assert nxt_nowruz.week_end == date(2027, 3, 26)
    assert format_jalali_date(nxt_nowruz.week_start) == "1405/12/29"
    assert format_jalali_date(nxt_nowruz.week_end) == "1406/01/06"


def test_peak_days_thursday_and_friday() -> None:
    """تشخیص صحیح روزهای اوج انتشار منو (پنجشنبه و جمعه به وقت تهران)."""
    wed = datetime(2026, 11, 11, 12, 0, tzinfo=TEHRAN_TZ)
    thu = datetime(2026, 11, 12, 12, 0, tzinfo=TEHRAN_TZ)
    fri = datetime(2026, 11, 13, 12, 0, tzinfo=TEHRAN_TZ)
    sat = datetime(2026, 11, 14, 12, 0, tzinfo=TEHRAN_TZ)

    assert is_peak_check_day(wed) is False
    assert is_peak_check_day(thu) is True
    assert is_peak_check_day(fri) is True
    assert is_peak_check_day(sat) is False


def test_parse_persian_dom_header_date() -> None:
    """تبدیل سربرگ فارسی ستون‌های DOM سماد (با ارقام فارسی) به تاریخ میلادی استاندارد."""
    parsed = parse_persian_header_date("شنبه، ۲۳ آبان ۱۴۰۵")
    assert parsed == date(2026, 11, 14)
