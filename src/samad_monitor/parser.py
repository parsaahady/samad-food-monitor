"""استخراج و نرمال‌سازی ساختاریافته برنامه غذایی از پاسخ‌های واقعی API و DOM سماد."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from samad_monitor.models import (
    CheckStatus,
    Meal,
    MealAvailability,
    SelfServiceInfo,
    WeekRange,
)
from samad_monitor.utils.date_utils import (
    get_persian_weekday,
    parse_iso_date,
    parse_persian_header_date,
    to_persian_digits,
)

# شناسه‌های استاندارد وعده‌ها در بک‌اند سماد (Dadekavan)
DEFAULT_MEAL_TYPE_NAMES: Dict[int, str] = {
    10: "سحری",
    6: "صبحانه",
    7: "ناهار",
    9: "افطاری",
    8: "شام",
    1: "ناهار",
    2: "شام",
    3: "صبحانه",
}

MEAL_SORT_RANK: Dict[str, int] = {
    "سحری": 0,
    "صبحانه": 1,
    "ناهار": 2,
    "افطاری": 3,
    "شام": 4,
}


class SiteStructureChangedError(Exception):
    """زمانی که ساختار پاسخ JSON یا DOM سامانه با ساختار شناخته‌شده سماد مطابقت ندارد."""


class IncompleteMenuDataError(Exception):
    """زمانی که پاسخ منو دریافت شده اما رکوردهای غذا فاقد فیلدهای حیاتی (تاریخ یا نام غذا) هستند."""


def meal_rank(meal_type_id: int, meal_type_name: str) -> int:
    """ترتیب طبیعی وعده‌های غذایی در طول یک روز."""
    clean_name = (meal_type_name or "").strip()
    if clean_name in MEAL_SORT_RANK:
        return MEAL_SORT_RANK[clean_name]
    id_rank = {10: 0, 6: 1, 3: 1, 7: 2, 1: 2, 9: 3, 8: 4, 2: 4}
    return id_rank.get(meal_type_id, 20 + max(0, meal_type_id))


def _normalize_whitespace(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def _extract_sides(food_type_obj: Dict[str, Any], program_obj: Dict[str, Any]) -> str:
    raw_beside = _normalize_whitespace(
        food_type_obj.get("besideFoodNames") or program_obj.get("besideFoodNames") or ""
    )
    if raw_beside:
        parts = [p.strip() for p in raw_beside.replace("|", "،").replace(",", "،").split("،") if p.strip()]
        return "، ".join(parts)

    beside_list = food_type_obj.get("besideFoodList")
    if isinstance(beside_list, list):
        titles = [
            _normalize_whitespace(item.get("title") or item.get("name"))
            for item in beside_list
            if isinstance(item, dict) and (item.get("title") or item.get("name"))
        ]
        return "، ".join(titles)
    return ""


def _extract_quota_limit(food_type_obj: Dict[str, Any], program_obj: Dict[str, Any]) -> str:
    valid_max = food_type_obj.get("validMaxCount")
    if valid_max is None:
        valid_max = program_obj.get("validTotalCount")
    valid_min = food_type_obj.get("validMinCount")

    try:
        max_int = int(valid_max) if valid_max is not None else 0
    except (TypeError, ValueError):
        max_int = 0

    try:
        min_int = int(valid_min) if valid_min is not None else 0
    except (TypeError, ValueError):
        min_int = 0

    if max_int > 0 and min_int > 1 and min_int != max_int:
        return f"{to_persian_digits(min_int)} تا {to_persian_digits(max_int)} پرس"
    if max_int > 0:
        return f"حداکثر {to_persian_digits(max_int)} پرس"
    return ""


def _extract_food_name(food_type_obj: Dict[str, Any], program_obj: Dict[str, Any]) -> str:
    for key in ("foodNames", "foodName", "standardFoodNames", "fullName"):
        candidate = _normalize_whitespace(food_type_obj.get(key))
        if candidate:
            return candidate

    food_list = food_type_obj.get("foodList")
    if isinstance(food_list, list) and food_list:
        names = [
            _normalize_whitespace(f.get("name") or f.get("title"))
            for f in food_list
            if isinstance(f, dict) and (f.get("name") or f.get("title"))
        ]
        if names:
            return " + ".join(names)

    for key in ("foodName", "foodNames"):
        candidate = _normalize_whitespace(program_obj.get(key))
        if candidate:
            return candidate

    return ""


def _flatten_week_programs(raw_programs: Any) -> List[Dict[str, Any]]:
    """پشتیبانی هم‌زمان از آرایه دوبعدی `selfWeekPrograms: [[...], [...]]` و آرایه مسطح."""
    flattened: List[Dict[str, Any]] = []
    if not isinstance(raw_programs, list):
        raise SiteStructureChangedError("فیلد selfWeekPrograms آرایه معتبر نیست.")

    for entry in raw_programs:
        if isinstance(entry, list):
            for sub in entry:
                if isinstance(sub, dict):
                    flattened.append(sub)
                elif sub is not None:
                    raise SiteStructureChangedError("آیتم نامعتبر در زیرآرایه selfWeekPrograms.")
        elif isinstance(entry, dict):
            flattened.append(entry)
        elif entry is not None:
            raise SiteStructureChangedError("ساختار ناشناخته در selfWeekPrograms.")
    return flattened


def _collect_hidden_program_ids(
    programs: Iterable[Dict[str, Any]],
    user_week_reserves: Any,
) -> set[int]:
    """مطابق منطق `collectHiddenProgramIds` در فرانت‌اند رسمی سماد."""
    hidden: set[int] = set()
    for prog in programs:
        if prog.get("hideInPanel") and prog.get("programId") is not None:
            try:
                hidden.add(int(prog["programId"]))
            except (TypeError, ValueError):
                pass
        for ft in prog.get("programFoodTypes") or []:
            if isinstance(ft, dict) and ft.get("hideInPanel") and ft.get("programId") is not None:
                try:
                    hidden.add(int(ft["programId"]))
                except (TypeError, ValueError):
                    pass

    if isinstance(user_week_reserves, list):
        for ur in user_week_reserves:
            if isinstance(ur, dict) and ur.get("hideInPanel") and ur.get("programId") is not None:
                try:
                    hidden.add(int(ur["programId"]))
                except (TypeError, ValueError):
                    pass
    return hidden


def sort_and_deduplicate_meals(meals: Iterable[Meal]) -> List[Meal]:
    """مرتب‌سازی قطعی و حذف رکوردهای تکراری در یک نوبت استخراج."""
    dedup: Dict[str, Meal] = {}
    for meal in meals:
        key = meal.identity_key()
        if key not in dedup:
            dedup[key] = meal
        else:
            existing = dedup[key]
            # اگر یکی از رکوردها باز (OPEN) است، وضعیت باز را حفظ کن
            if (
                existing.availability != MealAvailability.OPEN.value
                and meal.availability == MealAvailability.OPEN.value
            ):
                dedup[key] = meal

    return sorted(
        dedup.values(),
        key=lambda m: (
            m.date,
            meal_rank(m.meal_type_id, m.meal_type),
            m.self_id,
            m.restaurant,
            m.food_name,
        ),
    )


def parse_samad_programs_payload(
    payload: Dict[str, Any],
    self_info: SelfServiceInfo,
    target_week: WeekRange,
) -> List[Meal]:
    """
    پارس کردن پاسخ واقعی `/rest/reservations/programs/v2` یا `/rest/programs/v2`.
    فقط وعده‌هایی را برمی‌گرداند که تاریخ واقعی آن‌ها دقیقاً داخل `target_week` باشد.
    """
    if not isinstance(payload, dict):
        raise SiteStructureChangedError("پاسخ payload دیکشنری معتبر نیست.")

    if "selfWeekPrograms" not in payload:
        raise SiteStructureChangedError("کلید selfWeekPrograms در پاسخ سرور سماد یافت نشد.")

    raw_week_programs = payload.get("selfWeekPrograms")
    if raw_week_programs is None:
        return []

    programs = _flatten_week_programs(raw_week_programs)
    if not programs:
        return []

    meal_type_lookup: Dict[int, str] = dict(DEFAULT_MEAL_TYPE_NAMES)
    raw_meal_types = payload.get("mealTypes")
    if isinstance(raw_meal_types, list):
        for mt in raw_meal_types:
            if isinstance(mt, dict) and mt.get("id") is not None and mt.get("name"):
                try:
                    meal_type_lookup[int(mt["id"])] = _normalize_whitespace(mt["name"])
                except (TypeError, ValueError):
                    pass

    hidden_ids = _collect_hidden_program_ids(programs, payload.get("userWeekReserves"))

    extracted: List[Meal] = []
    invalid_records = 0
    total_visible_candidates = 0

    for prog in programs:
        if prog.get("hideInPanel") is True:
            continue

        raw_prog_id = prog.get("programId")
        prog_id: Optional[int] = None
        if raw_prog_id is not None:
            try:
                prog_id = int(raw_prog_id)
            except (TypeError, ValueError):
                prog_id = None

        if prog_id is not None and prog_id in hidden_ids:
            continue

        parsed_date = parse_iso_date(str(prog.get("date") or ""))
        if parsed_date is None:
            total_visible_candidates += 1
            invalid_records += 1
            continue

        # اعتبارسنجی حیاتی: تاریخ واقعی آیتم باید در بازه شنبه تا جمعه هفته هدف باشد
        if not target_week.contains(parsed_date):
            continue

        day_fa = get_persian_weekday(parsed_date, str(prog.get("dayTranslated") or ""))

        try:
            meal_type_id = int(prog.get("mealTypeId") or 0)
        except (TypeError, ValueError):
            meal_type_id = 0

        meal_type_name = _normalize_whitespace(prog.get("mealTypeName")) or meal_type_lookup.get(
            meal_type_id, "وعده غذایی"
        )
        availability = (
            MealAvailability.CLOSED.value
            if bool(prog.get("reserveRuleViolated"))
            else MealAvailability.OPEN.value
        )

        food_types = prog.get("programFoodTypes")
        option_dicts: List[Dict[str, Any]] = []
        if isinstance(food_types, list) and food_types:
            for ft in food_types:
                if not isinstance(ft, dict):
                    continue
                if ft.get("hideInPanel") is True:
                    continue
                ft_prog_id = ft.get("programId")
                if ft_prog_id is not None:
                    try:
                        if int(ft_prog_id) in hidden_ids:
                            continue
                    except (TypeError, ValueError):
                        pass
                option_dicts.append(ft)
        else:
            # برخی نسخه‌های سرور سماد غذا را در سطح بالای شیء program برمی‌گردانند
            option_dicts.append({})

        for ft in option_dicts:
            total_visible_candidates += 1
            food_name = _extract_food_name(ft, prog)
            if not food_name:
                invalid_records += 1
                continue

            raw_ft_id = ft.get("foodTypeId", prog.get("foodTypeId"))
            try:
                food_type_id = int(raw_ft_id) if raw_ft_id is not None else None
            except (TypeError, ValueError):
                food_type_id = None

            raw_price = ft.get("price", prog.get("price", 0))
            try:
                price_rials = int(raw_price or 0)
            except (TypeError, ValueError):
                price_rials = 0

            extracted.append(
                Meal(
                    date=parsed_date.isoformat(),
                    day_name=day_fa,
                    meal_type=meal_type_name,
                    meal_type_id=meal_type_id,
                    restaurant=self_info.name,
                    self_id=self_info.id,
                    is_free_restaurant=self_info.free_restaurant,
                    food_name=food_name,
                    availability=availability,
                    program_id=prog_id,
                    food_type_id=food_type_id,
                    side_dishes=_extract_sides(ft, prog),
                    price_rials=price_rials,
                    quota_limit=_extract_quota_limit(ft, prog),
                )
            )

    if total_visible_candidates > 0 and not extracted and invalid_records > 0:
        raise IncompleteMenuDataError(
            f"تعداد {invalid_records} رکورد برنامه دریافت شد اما نام غذا یا تاریخ معتبر نداشتند."
        )

    return sort_and_deduplicate_meals(extracted)


@dataclass
class RawDomCard:
    """نمایش خام یک کارت غذا در استخراج پشتیبان از DOM صفحه `/food/reserve`."""

    header_date_text: str
    meal_type_text: str
    food_name_text: str
    price_text: str = ""
    is_reservable: Optional[bool] = None


def parse_samad_dom_cards(
    dom_cards: Iterable[RawDomCard],
    self_info: SelfServiceInfo,
    target_week: WeekRange,
) -> List[Meal]:
    """تبدیل کارت‌های استخراج‌شده از DOM صفحه رزرو سماد به لیست استاندارد `Meal`."""
    meals: List[Meal] = []
    invalid_count = 0
    total_cards = 0

    reverse_meal_ids = {"صبحانه": 6, "ناهار": 7, "شام": 8, "افطاری": 9, "سحری": 10}

    for card in dom_cards:
        total_cards += 1
        parsed_date = parse_iso_date(card.header_date_text) or parse_persian_header_date(
            card.header_date_text
        )
        if parsed_date is None:
            invalid_count += 1
            continue

        if not target_week.contains(parsed_date):
            continue

        food_name = _normalize_whitespace(card.food_name_text)
        if not food_name:
            invalid_count += 1
            continue

        meal_type = _normalize_whitespace(card.meal_type_text) or "وعده غذایی"
        meal_type_id = reverse_meal_ids.get(meal_type, 0)

        if card.is_reservable is True:
            avail = MealAvailability.OPEN.value
        elif card.is_reservable is False:
            avail = MealAvailability.CLOSED.value
        else:
            avail = MealAvailability.UNKNOWN.value

        meals.append(
            Meal(
                date=parsed_date.isoformat(),
                day_name=get_persian_weekday(parsed_date),
                meal_type=meal_type,
                meal_type_id=meal_type_id,
                restaurant=self_info.name,
                self_id=self_info.id,
                is_free_restaurant=self_info.free_restaurant,
                food_name=food_name,
                availability=avail,
            )
        )

    if total_cards > 0 and not meals and invalid_count == total_cards:
        raise IncompleteMenuDataError("کارت‌های غذا در DOM یافت شدند اما تاریخ یا نام غذای معتبری نداشتند.")

    return sort_and_deduplicate_meals(meals)


def parse_samad_envelope(data: Any) -> Tuple[CheckStatus, Optional[Any], str, Optional[int]]:
    """
    تحلیل پاکت پاسخ استاندارد سرور سماد (`{type, code, messageResource, messageFa, payload}`).
    برمی‌گرداند: (وضعیت، payload، پیام، کد سماد).
    """
    if not isinstance(data, dict):
        return (
            CheckStatus.SITE_STRUCTURE_CHANGED,
            None,
            "پاسخ JSON سرور سماد ساختار دیکشنری مورد انتظار را ندارد.",
            None,
        )

    resp_type = str(data.get("type") or "").upper()
    raw_code = data.get("code")
    try:
        samad_code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        samad_code = None

    resource = str(data.get("messageResource") or "")
    msg_fa = _normalize_whitespace(
        data.get("messageFa") or data.get("message") or data.get("error_description") or ""
    )

    # خطاهای OAuth2 در صورت نامعتبر بودن توکن
    if data.get("error") in {"invalid_token", "unauthorized", "invalid_grant"}:
        return (
            CheckStatus.SESSION_EXPIRED,
            None,
            msg_fa or "توکن نشست نامعتبر یا منقضی شده است.",
            samad_code,
        )

    if resp_type == "ERROR":
        if resource == "SELF_HAS_NO_PROGRAM" or samad_code == 10108005:
            return (
                CheckStatus.MENU_NOT_PUBLISHED,
                None,
                msg_fa or "هنوز برنامه غذایی برای این سلف و هفته تعریف نشده است.",
                samad_code,
            )

        if (
            resource == "USER_FORCE_CHANGE_PASSWORD"
            or "token" in resource.lower()
            or "auth" in resource.lower()
            or samad_code in {401, 10101001}
        ):
            return (
                CheckStatus.SESSION_EXPIRED,
                None,
                msg_fa or "نشست کاربری منقضی شده یا نیاز به ورود مجدد دارد.",
                samad_code,
            )

        return (
            CheckStatus.SERVER_ERROR,
            None,
            msg_fa or f"خطای سرور سماد ({resource or samad_code})",
            samad_code,
        )

    if resp_type in {"INFO", "SUCCESS"} or "payload" in data:
        return (CheckStatus.MENU_PUBLISHED, data.get("payload"), msg_fa, samad_code)

    return (
        CheckStatus.SITE_STRUCTURE_CHANGED,
        None,
        "پاسخ ناشناخته از سرور سماد دریافت شد.",
        samad_code,
    )
