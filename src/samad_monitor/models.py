"""مدل‌های داده ساختاریافته پروژه (منطبق بر داده‌های واقعی سامانه سماد)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Dict, List, Optional


class CheckStatus(str, Enum):
    """وضعیت دقیق نتیجه هر نوبت بررسی سامانه (تمایز کامل عدم انتشار از خطاهای فنی)."""

    MENU_NOT_PUBLISHED = "MENU_NOT_PUBLISHED"        # منوی هفته آینده هنوز منتشر نشده است
    MENU_PUBLISHED = "MENU_PUBLISHED"                # منوی هفته آینده منتشر شده و معتبر است
    NETWORK_ERROR = "NETWORK_ERROR"                  # قطعی اینترنت یا عدم برقراری ارتباط شبکه/TLS
    SITE_UNREACHABLE = "SITE_UNREACHABLE"            # سرور سماد در دسترس نیست (502/503/504)
    SESSION_EXPIRED = "SESSION_EXPIRED"              # نشست منقضی شده یا کاربر وارد نشده است
    INCOMPLETE_MENU_DATA = "INCOMPLETE_MENU_DATA"    # اطلاعات برگشتی منو ناقص یا مخدوش است
    SITE_STRUCTURE_CHANGED = "SITE_STRUCTURE_CHANGED"  # ساختار پاسخ سرور یا DOM صفحه تغییر کرده است
    SERVER_ERROR = "SERVER_ERROR"                    # خطای داخلی یا پاسخ ناموفق سرور سماد


class EventType(str, Enum):
    """انواع رویدادهای قابل اعلان سیستم."""

    MENU_AVAILABLE = "MENU_AVAILABLE"    # انتشار اولیه منوی هفته آینده
    MENU_UPDATED = "MENU_UPDATED"        # تغییر غذاها یا اضافه شدن روز/سلف جدید به منوی منتشرشده
    SESSION_EXPIRED = "SESSION_EXPIRED"  # انقضای نشست و نیاز به ورود مجدد دستی کاربر


class MealAvailability(str, Enum):
    """وضعیت باز یا بسته بودن امکان رزرو وعده در سماد (!reserveRuleViolated)."""

    OPEN = "OPEN"        # مهلت رزرو باز است (قابل رزرو توسط دانشجو)
    CLOSED = "CLOSED"    # مهلت رزرو بسته یا غیرفعال است
    UNKNOWN = "UNKNOWN"  # نامشخص (در حالت استخراج محدود DOM)


@dataclass(frozen=True)
class WeekRange:
    """بازه یک هفته دانشگاهی (شنبه تا جمعه) بر مبنای Asia/Tehran."""

    week_start: date  # شنبه (ISO YYYY-MM-DD)
    week_end: date    # جمعه (ISO YYYY-MM-DD)

    @property
    def start_iso(self) -> str:
        return self.week_start.isoformat()

    @property
    def end_iso(self) -> str:
        return self.week_end.isoformat()

    def contains(self, target: date) -> bool:
        return self.week_start <= target <= self.week_end

    def all_dates(self) -> List[date]:
        days_count = (self.week_end - self.week_start).days + 1
        return [self.week_start + timedelta(days=i) for i in range(max(0, days_count))]

    def display_jalali_range(self) -> str:
        from samad_monitor.utils.date_utils import format_week_range_persian

        return format_week_range_persian(self, include_gregorian=False)

    def display_bilingual(self) -> str:
        from samad_monitor.utils.date_utils import format_week_range_persian

        return format_week_range_persian(self, include_gregorian=True)


@dataclass(frozen=True)
class SelfServiceInfo:
    """اطلاعات یک سلف یا رستوران تغذیه در سماد."""

    id: int
    name: str
    self_type: str = "NORMAL"
    free_restaurant: bool = False


@dataclass
class Meal:
    """مدل یک آیتم غذایی واقعی در برنامه هفتگی سماد."""

    date: str                         # تاریخ میلادی استاندارد ISO: YYYY-MM-DD
    day_name: str                     # نام روز هفته به فارسی (شنبه تا جمعه)
    meal_type: str                    # نام وعده غذایی (صبحانه، ناهار، شام، سحری، افطاری)
    restaurant: str                   # نام سلف یا محل توزیع
    food_name: str                    # نام غذا
    availability: str = MealAvailability.OPEN.value
    meal_type_id: int = 0             # شناسه وعده در سماد (مثلاً ۶ صبحانه، ۷ ناهار، ۸ شام)
    self_id: int = 0                  # شناسه سلف در سماد
    is_free_restaurant: bool = False  # آیا رستوران آزاد/مکمل است؟
    program_id: Optional[int] = None  # شناسه برنامه در سماد
    food_type_id: Optional[int] = None
    side_dishes: str = ""             # مخلفات / کنارغذا (در صورت ثبت در سماد)
    price_rials: int = 0              # قیمت ثبت‌شده به ریال (در صورت وجود)
    quota_limit: str = ""             # محدودیت تعداد مجاز انتخاب هر دانشجو (در صورت نمایش)
    id: Optional[int] = None
    menu_id: Optional[int] = None

    def identity_key(self) -> str:
        """کلید یکتای معنایی وعده برای مقایسه تغییرات منو."""
        norm_food = " ".join(self.food_name.strip().split())
        norm_rest = " ".join(self.restaurant.strip().split())
        norm_meal = " ".join(self.meal_type.strip().split())
        self_part = str(self.self_id) if self.self_id else norm_rest
        meal_part = str(self.meal_type_id) if self.meal_type_id else norm_meal
        return f"{self_part}|{self.date}|{meal_part}|{norm_food}"


@dataclass
class Menu:
    """مدل Snapshot برنامه غذایی یک هفته دانشگاهی."""

    week_start: str           # ISO YYYY-MM-DD (شنبه)
    week_end: str             # ISO YYYY-MM-DD (جمعه)
    detected_at: str          # زمان شناسایی (ISO در منطقه زمانی Asia/Tehran)
    status: str               # PUBLISHED | UPDATED
    meals: List[Meal] = field(default_factory=list)
    content_hash: str = ""
    source: str = "api"
    updated_at: Optional[str] = None
    id: Optional[int] = None


@dataclass
class MenuDiff:
    """جزئیات دقیق تغییرات بین دو Snapshot منوی یک هفته."""

    added_dates: List[str] = field(default_factory=list)
    removed_dates: List[str] = field(default_factory=list)
    added_meals: List[Meal] = field(default_factory=list)
    removed_meals: List[Meal] = field(default_factory=list)
    newly_opened_meals: List[Meal] = field(default_factory=list)

    @property
    def has_meaningful_changes(self) -> bool:
        return bool(
            self.added_dates
            or self.removed_dates
            or self.added_meals
            or self.removed_meals
            or self.newly_opened_meals
        )


@dataclass
class DetectionOutcome:
    """خروجی موتور تشخیص انتشار و تغییرات منو."""

    event_type: Optional[EventType]
    event_id: Optional[str]
    menu: Optional[Menu]
    diff: Optional[MenuDiff] = None
    reason: str = ""


@dataclass
class FetchResult:
    """خروجی لایه استخراج اطلاعات از سامانه سماد."""

    status: CheckStatus
    week_range: WeekRange
    meals: List[Meal] = field(default_factory=list)
    selfs_checked: List[SelfServiceInfo] = field(default_factory=list)
    source: str = "api"
    message: str = ""
    http_status: Optional[int] = None
    samad_code: Optional[int] = None
    username: Optional[str] = None


@dataclass
class NotificationMessage:
    """پیام آماده ارسال به کانال‌های اعلان."""

    event_id: str
    event_type: EventType
    title: str
    body: str
    short_summary: str = ""
    action_url: str = "https://samad.app/food/reserve"
    metadata: Dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return self.action_url
