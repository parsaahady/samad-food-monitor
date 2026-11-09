"""نمونه پاسخ‌های JSON منطبق بر DTOهای واقعی سرور سماد (استخراج‌شده از سورس رسمی samad.app v7.3)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, List


def make_fake_jwt(username: str = "401243001", exp_offset_seconds: int = 3600) -> str:
    """ساخت یک JWT آزمایشی (صرفاً برای تست، بدون هیچ داده واقعی کاربری)."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload_dict = {
        "user_name": username,
        "scope": ["read", "write"],
        "exp": int(time.time()) + exp_offset_seconds,
        "jti": f"test-jti-{username}",
    }
    payload = (
        base64.urlsafe_b64encode(json.dumps(payload_dict).encode("utf-8"))
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.signature_placeholder"


def release_response(release_time: str = "260115103000") -> Dict[str, Any]:
    return {
        "type": "SUCCESS",
        "code": 0,
        "messageResource": None,
        "messageFa": None,
        "payload": {"releaseTime": release_time},
    }


def user_profile_response(username: str = "401243001") -> Dict[str, Any]:
    return {
        "type": "SUCCESS",
        "code": 0,
        "payload": {
            "id": 1001,
            "username": username,
            "firstName": "دانشجو",
            "lastName": "آزمایشی",
            "remainCredit": 1500000,
        },
    }


def sbu_selfs_response() -> Dict[str, Any]:
    return {
        "type": "SUCCESS",
        "code": 0,
        "payload": [
            {
                "id": 101,
                "name": "سلف مرکزی برادران - ولنجک",
                "type": "NORMAL",
                "freeRestaurant": False,
            },
            {
                "id": 202,
                "name": "کافه تریا دانشکده مهندسی کامپیوتر",
                "type": "NORMAL",
                "freeRestaurant": True,
            },
            {
                "id": 999,
                "name": "خوابگاه کوی پسران",
                "type": "DORMITORY",
                "freeRestaurant": False,
            },
        ],
    }


def unpublished_program_error_response() -> Dict[str, Any]:
    """پاسخ رسمی سماد زمانی که هنوز منویی برای هفته انتخاب‌شده تعریف نشده است."""
    return {
        "type": "ERROR",
        "code": 10108005,
        "messageResource": "SELF_HAS_NO_PROGRAM",
        "messageFa": "برای این سلف در بازه انتخابی برنامه غذایی تعریف نشده است.",
        "payload": None,
    }


def published_programs_v2_response(
    days_foods: List[Dict[str, Any]],
    self_id: int = 101,
    self_name: str = "سلف مرکزی برادران - ولنجک",
) -> Dict[str, Any]:
    """
    ساخت ساختار واقعی `selfWeekPrograms` دوبعدی سماد.
    هر آیتم `days_foods` شامل:
    `date`, `meal_type_id`, `meal_type_name`, `food_name`, `price`, `side`, `violated`, `hide`
    """
    grouped_by_date: Dict[str, List[Dict[str, Any]]] = {}
    prog_counter = 5000

    for item in days_foods:
        prog_counter += 1
        iso_d = item["date"]
        m_id = item.get("meal_type_id", 7)
        m_name = item.get("meal_type_name", "ناهار")
        f_name = item["food_name"]
        price = item.get("price", 120000)
        side = item.get("side", "")
        violated = bool(item.get("violated", False))
        hide = bool(item.get("hide", False))

        prog_obj = {
            "programId": prog_counter,
            "selfId": self_id,
            "selfName": self_name,
            "date": f"{iso_d} 00:00:00",
            "dayTranslated": item.get("day_translated", ""),
            "mealTypeId": m_id,
            "mealTypeName": m_name,
            "price": price,
            "validTotalCount": 1,
            "reserveRuleViolated": violated,
            "hideInPanel": hide,
            "programFoodTypes": [
                {
                    "programId": prog_counter,
                    "foodTypeId": prog_counter * 10 + 1,
                    "foodTypeTitle": "نوع ۱",
                    "foodNames": f_name,
                    "besideFoodNames": side,
                    "price": price,
                    "validMinCount": 1,
                    "validMaxCount": 1,
                    "hideInPanel": hide,
                    "foodList": [{"id": 1, "name": f_name}],
                }
            ],
        }
        grouped_by_date.setdefault(iso_d, []).append(prog_obj)

    return {
        "type": "SUCCESS",
        "code": 0,
        "payload": {
            "selfWeekPrograms": list(grouped_by_date.values()),
            "userWeekReserves": [],
            "mealTypes": [
                {"id": 6, "name": "صبحانه"},
                {"id": 7, "name": "ناهار"},
                {"id": 8, "name": "شام"},
            ],
        },
    }
