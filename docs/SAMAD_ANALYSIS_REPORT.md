# گزارش مرحله اول: تحلیل فنی و مشاهده واقعی سامانه سماد (دانشگاه شهید بهشتی)

این سند حاصل بررسی مستقیم نسخه تولیدی سامانه `https://samad.app` (نسخه `7.3` توسعه داده‌شده توسط شرکت دانش‌بنیان داده‌کاوان اندیشه برتر)، فایل پیکربندی دانشگاه‌ها (`https://api-v2.samad.app/config/universities.json?version=7.3`) و باندل‌های کلاینت وب اپلیکیشن سماد است. هیچ Endpoint، کلید نشست یا مدل داده‌ای حدس زده نشده است.

---

## ۱. ساختار واقعی سامانه و معماری کلاینت

- **نوع نرم‌افزار وب:** اپلیکیشن تک‌صفحه‌ای (SPA) مبتنی بر **React + React Router v6 + Ant Design + Zustand + TanStack Query + Axios + `jalali-moment`** (نسخه `7.3`).
- **مسیرهای کلیدی کلاینت (`Routes`):**
  - صفحه ورود: `https://samad.app/login` (هنگام مراجعه بدون احراز هویت به صفحه رزرو، کاربر به‌صورت خودکار به `https://samad.app/login?next=/food/reserve` هدایت می‌شود).
  - صفحه انتخاب سلف (موبایل): `https://samad.app/food/selfs`
  - صفحه رزرو غذا: `https://samad.app/food/reserve`
  - صفحه گزارش رزروها: `https://samad.app/food/reserves-report`
- **مشخصات اختصاصی دانشگاه شهید بهشتی در پیکربندی رسمی سماد (`unikey = "SBU"`):**
  - **نام دانشگاه (`universityLabel`):** `دانشگاه شهید بهشتی`
  - **کد یکتا (`unikey`):** `SBU`
  - **سرور بک‌اند دانشگاه (`universityURL`):** `https://dining.sbu.ac.ir`
  - **آدرس SSO / CAS دانشگاه (`casUrl`):** `https://dining.sbu.ac.ir/index.rose?sharifOauthRedirect=true`
  - **وضعیت پیش‌فرض کپچا (`enableCaptcha`):** `-1` (در ورود عادی غیرفعال؛ در صورت فعال‌سازی پویا توسط سرور یا SSO دانشگاه، ورود دستی کاربر در مرورگر آن را بدون دور زدن مدیریت می‌کند).
  - **خروج خودکار سمت کلاینت (`autoLogoutTime`):** `-1` (غیرفعال).
  - **ماژول تغذیه (`samad.nurture`):** `1` (فعال).

---

## ۲. فرآیند ورود و نگهداری نشست کاربری (Session)

بررسی دقیق `UserContext.tsx`، `UniversityContext.tsx`، `LoginPage.tsx` و `services/http.tsx` نشان می‌دهد:

1. **محل ذخیره نشست در مرورگر:**
   برخلاف سامانه‌های سنتی مبتنی بر Cookie سرور، اپلیکیشن `samad.app` اطلاعات احراز هویت را در **`window.localStorage` تحت دامنه `https://samad.app`** ذخیره می‌کند:
   - `TOKEN`: رشته JSON حاوی JWT `access_token` (ارسال‌شده در هدر `Authorization: Bearer <token>`).
   - `REFRESH_TOKEN`: رشته JSON حاوی `refresh_token` برای تمدید خودکار توکن.
   - `UNIVERSITY`: شیء JSON مشخصات دانشگاه انتخاب‌شده (برای دانشگاه شهید بهشتی شامل `"unikey": "SBU"` و `"universityURL": "https://dining.sbu.ac.ir"`).
   - `releaseDate`: رشته تاریخ انتشار بک‌اند به فرمت `YYMMDDHHmmss` دریافتی از `/rest/config/release`.

2. **روش‌های ورود کاربر در صفحه `/login`:**
   - **روش اول (پیش‌فرض برای SBU — ورود یکپارچه SSO/CAS):** هدایت به `casUrl` دانشگاه شهید بهشتی و بازگشت به مسیر `/login/cas/...` که توکن‌های `access_token` و `refresh_token` را در `localStorage` ثبت می‌کند.
   - **روش دوم (نام کاربری و رمز عبور سماد):** ارسال درخواست `POST https://dining.sbu.ac.ir/oauth/token` با هدر پایه کلاینت سماد و دریافت `access_token` و `refresh_token`.
   - **تصمیم امنیتی در این پروژه:** ورود اولیه صرفاً به‌صورت **دستی و تعاملی توسط خود کاربر** در مرورگر واقعی Playwright (`python -m samad_monitor login`) انجام می‌شود تا هیچ نام کاربری یا رمز عبوری در کد، لاگ یا فایل تنظیمات ذخیره نشود و هرگونه SSO یا CAPTCHA احتمالی به‌شکل کاملاً قانونی توسط خود دانشجو تکمیل گردد.

3. **تمدید توکن و تشخیص انقضای نشست:**
   - در صورت دریافت کد `401 Unauthorized`، کلاینت رسمی سماد درخواست `POST {universityURL}/oauth/token` با `grant_type=refresh_token` ارسال می‌کند.
   - اگر `refresh_token` نیز منقضی یا نامعتبر باشد (`400` یا `401`)، توکن‌ها پاک شده و نشست منقضی (`SESSION_EXPIRED`) تلقی می‌شود.

---

## ۳. نحوه دریافت منوی غذا از سرور (APIهای مشاهده‌شده)

واکشی اطلاعات منو در `samad.app` از طریق REST APIهای رسمی خود سامانه روی سرور دانشگاه (`https://dining.sbu.ac.ir`) انجام می‌شود:

1. **بررسی نسخه بک‌اند دانشگاه (بدون نیاز به احراز هویت):**
   - `GET {universityURL}/rest/config/release`
   - خروجی شامل `payload.version` و `payload.releaseTime` (فرمت `YYMMDDHHmmss`) است.

2. **اعتبارسنجی وضعیت نشست کاربر:**
   - `GET {universityURL}/rest/users/me` با هدر `Authorization: Bearer <TOKEN>`

3. **دریافت فهرست سلف‌ها و رستوران‌های مجاز دانشجو:**
   - اگر `releaseTime >= "250908183211"`:
     `GET {universityURL}/rest/reservations/selfs`
   - در نسخه‌های قدیمی‌تر بک‌اند:
     `GET {universityURL}/rest/selfs`
   - هر آیتم سلف دارای `id`، `name`، `type` (سلف‌های تغذیه دارای `type == "NORMAL"` یا خالی هستند؛ در مقابل `SPORT`، `HEALTH`، `TRANSPORT`) و فیلد `freeRestaurant` (رستوران آزاد/مکمل یا سلف یارانه‌ای) است.

4. **دریافت برنامه غذایی هفتگی هر سلف:**
   - اگر `releaseTime >= "251026134000"`:
     `GET {universityURL}/rest/reservations/programs/v2?selfId={selfId}&weekStartDate={YYYY-MM-DD}+00:00:00`
   - در نسخه‌های قدیمی‌تر بک‌اند:
     `GET {universityURL}/rest/programs/v2?selfId={selfId}&weekStartDate={YYYY-MM-DD}+00:00:00`
   - پارامتر `weekStartDate` تاریخ میلادی روز **شنبه** ابتدای هفته مورد نظر به فرمت `YYYY-MM-DD 00:00:00` است.

5. **رفتار سرور هنگام منتشر نشدن منوی هفته آینده:**
   - حالت اول: پاسخ JSON با `type: "ERROR"` و `messageResource: "SELF_HAS_NO_PROGRAM"` (یا `code: 10108005` — «هیچ برنامه‌ای برای این سلف تعریف نشده است»). در سورس خود `samad.app` نیز `SELF_HAS_NO_PROGRAM` در لیست `ERR_IGNORE_KEY` قرار دارد و به‌معنای **عدم انتشار برنامه غذایی آن هفته** است، نه خطای فنی.
   - حالت دوم: پاسخ `type: "INFO"` با آرایه خالی `payload.selfWeekPrograms: []` یا آیتم‌هایی که همگی `hideInPanel: true` دارند.

---

## ۴. اطلاعات واقعی قابل استخراج (و محدودیت‌های واقعی داده)

از پاسخ `programs/v2` (و در حالت پشتیبان از DOM صفحه `/food/reserve` در کامپوننت‌های `ReserveProgramDesktop` و `ReserveCard`) فیلدهای زیر به‌طور واقعی قابل استخراج هستند:

| فیلد در سامانه سماد | معادل در مدل داده پروژه | توضیح واقعی |
|---|---|---|
| `programId` | `program_id` | شناسه یکتای برنامه غذایی در سماد |
| `selfId` + نام سلف | `self_id`, `restaurant` | شناسه و نام سلف یا رستوران توزیع غذا |
| `freeRestaurant` | `is_free_restaurant` | تفکیک سلف یارانه‌ای از رستوران آزاد/مکمل |
| `date` | `date` | تاریخ دقیق میلادی وعده (`YYYY-MM-DD`) |
| `dayTranslated` / محاسبه تاریخ | `day_name` | نام فارسی روز هفته (`شنبه` تا `جمعه`) |
| `mealTypeId`, `mealTypeName` | `meal_type_id`, `meal_type` | وعده غذایی (`صبحانه`، `ناهار`، `شام`، `سحری`، `افطاری`) |
| `programFoodTypes[].foodNames` / `foodName` | `food_name` | نام کامل غذا |
| `programFoodTypes[].besideFoodNames` | `side_dishes` | مخلفات / کنارغذا (در صورت ثبت توسط سلف) |
| `programFoodTypes[].price` / `price` | `price_rials` | قیمت به ریال (در رابط سماد تقسیم بر ۱۰ به تومان نمایش داده می‌شود) |
| `!reserveRuleViolated` | `availability` | وضعیت باز بودن مهلت رزرو (`OPEN` قابل رزرو / `CLOSED` بسته) |
| `validMinCount`, `validMaxCount`, `validTotalCount` | `quota_limit` | سقف مجاز انتخاب هر دانشجو (مثلاً حداکثر ۱ پرس) |

> **نکته فنی مهم درباره ظرفیت باقی‌مانده آشپزخانه:** در پاسخ `programs/v2` سامانه سماد، موجودی کل انبار/آشپزخانه ارسال **نمی‌شود** (صرفاً سقف سهمیه فردی `validMaxCount`/`validTotalCount` و باز یا بسته بودن قانون رزرو `reserveRuleViolated` ارسال می‌گردد؛ شمارنده ظرفیت فقط در فروش روزانه `/rest/daily-sell-programs` وجود دارد). بنابراین مطابق دستورالعمل پروژه، هیچ فیلد جعلی برای موجودی انبار ساخته نشده است.

---

## ۵. نحوه تشخیص هفته آینده

- مطابق `useWeekManagement.ts` در سورس `samad.app`، هفته دانشگاهی دقیقاً از **شنبه (`Saturday`) تا جمعه (`Friday`)** محاسبه می‌شود.
- برنامه همیشه ساعت فعلی را در منطقه زمانی `Asia/Tehran` محاسبه کرده، شنبه هفته جاری را یافته و بازه **شنبه هفته آینده (`week_start`) تا جمعه هفته آینده (`week_end`)** را استخراج می‌کند.
- علاوه‌بر ارسال `weekStartDate` شنبه هفته آینده به سرور، تاریخ واقعی تک‌تک آیتم‌های برگشتی (`meal.date`) با بازه `[week_start, week_end]` تطبیق داده می‌شود تا هیچ داده‌ای از هفته دیگر به‌اشتباه ثبت نشود.

---

## ۶. نقاط ضعف، محدودیت‌های شبکه و تدابیر پایداری

1. **محدودیت جغرافیایی IP (GeoIP / شبکه ملی اطلاعات):**
   سرورهای `samad.app` (`46.102.137.20`) و `dining.sbu.ac.ir` ترافیک ورودی از IPهای خارج از ایران (سرورهای ابری خارجی و VPNهای غیرایرانی) را مسدود یا دچار Timeout می‌کنند. در مقابل، سرور `api.telegram.org` از داخل ایران نیازمند پروکسی است.
   - **راه‌حل پیاده‌سازی‌شده:** کلاینت سماد به‌طور پیش‌فرض متغیرهای پروکسی سیستم را نادیده می‌گیرد (`SAMAD_IGNORE_ENV_PROXY=true`) تا ارتباط با سماد مستقیم از اینترنت داخلی برقرار شود، در حالی که کانال تلگرام پروکسی اختصاصی خود (`TELEGRAM_PROXY_URL` یا `TELEGRAM_API_BASE_URL`) را پشتیبانی می‌کند.
2. **تفاوت نسخه‌های بک‌اند سماد بین ترم‌ها:**
   با بررسی خودکار `/rest/config/release` و پشتیبانی هم‌زمان از مسیرهای جدید (`/rest/reservations/...`) و مسیرهای پیشین (`/rest/...`) به‌همراه حالت پشتیبان مرورگر Playwright Headless، برنامه در برابر ارتقای نسخه سرور مقاوم است.
3. **انقضای Refresh Token یا الزام تغییر رمز عبور دوره‌ای (`USER_FORCE_CHANGE_PASSWORD`):**
   برنامه بین خطای موقت شبکه و انقضای واقعی نشست تمایز کامل قائل شده، در صورت انقضای قطعی نشست، یک اعلان مشخص به کاربر ارسال می‌کند و چرخه پایش را متوقف می‌سازد تا درخواست بی‌مورد ارسال نشود.
4. **ایمنی در برابر رزرو ناخواسته:**
   در لایه کلاینت API و لایه رهگیری شبکه Playwright یک قفل سخت‌افزاری/نرم‌افزاری فقط‌خواندنی (`ReadOnlyGuard`) تعبیه شده که هرگونه متد تغییردهنده (`POST/PUT/DELETE` روی مسیرهای رزرو، لغو یا پرداخت) را مسدود می‌کند.
