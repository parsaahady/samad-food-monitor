"""تست‌های پارسر JSON/DOM سماد و موتور تشخیص تغییرات منو."""

from __future__ import annotations

from datetime import date

import pytest

from samad_monitor.detector import MenuDetector
from samad_monitor.models import EventType, MealAvailability, SelfServiceInfo, WeekRange
from samad_monitor.parser import (
    IncompleteMenuDataError,
    RawDomCard,
    SiteStructureChangedError,
    parse_samad_dom_cards,
    parse_samad_programs_payload,
)
from tests.fixtures.samad_payloads import published_programs_v2_response


def _target_week() -> WeekRange:
    return WeekRange(week_start=date(2026, 11, 14), week_end=date(2026, 11, 20))


def test_parser_filters_hidden_and_out_of_range_dates() -> None:
    """آیتم‌های خارج از بازه هفته هدف یا دارای hideInPanel=True باید نادیده گرفته شوند."""
    self_info = SelfServiceInfo(id=101, name="سلف مرکزی برادران")
    resp = published_programs_v2_response(
        [
            {"date": "2026-11-13", "food_name": "غذای هفته قبل"},  # خارج از هفته هدف
            {"date": "2026-11-14", "food_name": "چلو کباب کوبیده", "side": "دوغ | ماست"},
            {"date": "2026-11-15", "food_name": "غذای مخفی پنل", "hide": True},
            {"date": "2026-11-21", "food_name": "غذای دو هفته بعد"},  # خارج از هفته هدف
        ]
    )

    meals = parse_samad_programs_payload(resp["payload"], self_info, _target_week())
    assert len(meals) == 1
    assert meals[0].date == "2026-11-14"
    assert meals[0].day_name == "شنبه"
    assert meals[0].food_name == "چلو کباب کوبیده"
    assert "دوغ" in meals[0].side_dishes


def test_parser_detects_structure_change_and_incomplete_data() -> None:
    self_info = SelfServiceInfo(id=101, name="سلف مرکزی")

    with pytest.raises(SiteStructureChangedError):
        parse_samad_programs_payload({"unexpectedKey": []}, self_info, _target_week())

    broken_payload = {
        "selfWeekPrograms": [
            [
                {
                    "programId": 1,
                    "date": "2026-11-14 00:00:00",
                    "mealTypeId": 7,
                    "mealTypeName": "ناهار",
                    "programFoodTypes": [{"foodNames": "   "}],
                }
            ]
        ]
    }
    with pytest.raises(IncompleteMenuDataError):
        parse_samad_programs_payload(broken_payload, self_info, _target_week())


def test_dom_fallback_parser() -> None:
    self_info = SelfServiceInfo(id=101, name="سلف مرکزی")
    cards = [
        RawDomCard(
            header_date_text="شنبه، ۲۳ آبان ۱۴۰۵",
            meal_type_text="ناهار",
            food_name_text="زرشک پلو با مرغ",
            is_reservable=True,
        ),
        RawDomCard(
            header_date_text="جمعه، ۲۲ آبان ۱۴۰۵",  # هفته جاری - نباید وارد هفته آینده شود
            meal_type_text="ناهار",
            food_name_text="قورمه‌سبزی",
            is_reservable=True,
        ),
    ]
    meals = parse_samad_dom_cards(cards, self_info, _target_week())
    assert len(meals) == 1
    assert meals[0].date == "2026-11-14"
    assert meals[0].food_name == "زرشک پلو با مرغ"


def test_detector_ignores_natural_reservation_deadline_closure() -> None:
    """
    بسته شدن طبیعی مهلت رزرو یک روز (OPEN -> CLOSED) نباید هشدار کاذب تغییر منو صادر کند،
    اما باز شدن وعده‌ای که قبلاً بسته بوده (CLOSED -> OPEN) باید گزارش شود.
    """
    detector = MenuDetector()
    wk = _target_week()
    self_info = SelfServiceInfo(id=101, name="سلف مرکزی")

    initial_resp = published_programs_v2_response(
        [{"date": "2026-11-14", "food_name": "قیمه نثار", "violated": False}]
    )
    meals_open = parse_samad_programs_payload(initial_resp["payload"], self_info, wk)
    out1 = detector.evaluate(wk, meals_open, previous_menu=None)
    assert out1.event_type == EventType.MENU_AVAILABLE
    assert out1.menu is not None

    # اکنون مهلت رزرو شنبه گذشته و OPEN -> CLOSED شده است
    closed_resp = published_programs_v2_response(
        [{"date": "2026-11-14", "food_name": "قیمه نثار", "violated": True}]
    )
    meals_closed = parse_samad_programs_payload(closed_resp["payload"], self_info, wk)
    out2 = detector.evaluate(wk, meals_closed, previous_menu=out1.menu)
    assert out2.event_type is None  # بدون هشدار کاذب!

    # اگر مدیریت تغذیه دوباره رزرو را باز کند (CLOSED -> OPEN)
    assert out2.menu is not None
    assert out2.menu.meals[0].availability == MealAvailability.CLOSED.value
    out3 = detector.evaluate(wk, meals_open, previous_menu=out2.menu)
    assert out3.event_type == EventType.MENU_UPDATED
    assert out3.diff is not None
    assert len(out3.diff.newly_opened_meals) == 1
