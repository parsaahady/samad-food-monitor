"use strict";

const STATUS_LABELS = {
  MENU_PUBLISHED: "منتشر شده ✅",
  MENU_NOT_PUBLISHED: "هنوز منتشر نشده ⏳",
  SESSION_EXPIRED: "نشست منقضی شده ⚠️",
  NETWORK_ERROR: "خطای شبکه / اینترنت ❌",
  SITE_UNREACHABLE: "سماد موقتاً در دسترس نیست ❌",
  SERVER_ERROR: "خطای سرور سماد ❌",
  INCOMPLETE_MENU_DATA: "داده ناقص ⚠️",
  SITE_STRUCTURE_CHANGED: "تغییر ساختار صفحه ⚠️",
};

let currentState = null;
let demoMenuData = null;
let selectedWeekIso = "";

function toFaDigits(val) {
  return String(val ?? "").replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)]);
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showToast(message, isError = false) {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.remove();
  }, 5500);
}

async function apiGet(path) {
  const resp = await fetch(path, { cache: "no-store" });
  const data = await resp.json();
  if (data && typeof data === "object") {
    data._httpStatus = resp.status;
  }
  return data;
}

async function apiPost(path, body = {}) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return await resp.json();
}

/* ==================== رندر وضعیت کلی صفحه ==================== */
function renderDashboard(state) {
  currentState = state;

  document.getElementById("uni-subtitle").textContent =
    `ویژه دانشجویان ${state.university.label} • بدون ذخیره رمز عبور`;
  document.getElementById("link-open-samad").href = state.university.reserve_url;

  // ساعت تهران
  document.getElementById("tehran-date").textContent = state.clock.now_jalali;
  document.getElementById("tehran-time").textContent = state.clock.now_time_fa;
  const peakBadge = document.getElementById("peak-badge");
  peakBadge.textContent = state.clock.is_peak_day ? "روز اوج بررسی (پنجشنبه/جمعه)" : "";

  // کارت ۱: نشست کاربری
  const sessDot = document.getElementById("session-dot");
  const sessText = document.getElementById("session-status-text");
  const sessSub = document.getElementById("session-user-sub");
  sessText.textContent = state.session.label;
  if (state.session.code === "ACTIVE") {
    sessDot.className = "status-dot status-ok";
    sessSub.textContent = state.session.username
      ? `شناسه دانشجویی: ${state.session.username}`
      : state.session.university_label;
  } else if (state.session.code === "EXPIRED") {
    sessDot.className = "status-dot status-err";
    sessSub.textContent = "پایش متوقف است تا مجدداً وارد شوید";
  } else {
    sessDot.className = "status-dot status-warn";
    sessSub.textContent = "روی «ورود دستی به سماد» کلیک کنید";
  }

  // کارت ۲: زمان‌بند خودکار
  const schedDot = document.getElementById("sched-dot");
  const schedText = document.getElementById("sched-status-text");
  const schedSub = document.getElementById("sched-next-sub");
  const btnSched = document.getElementById("btn-scheduler-toggle");
  const btnSchedIcon = document.getElementById("btn-sched-icon");
  const btnSchedLabel = document.getElementById("btn-sched-label");

  if (state.scheduler.running_in_panel) {
    schedDot.className = "status-dot status-ok";
    schedText.textContent = "فعال و در حال پایش خودکار";
    schedSub.textContent = `بررسی بعدی: ${state.scheduler.next_scheduled_check_fa}`;
    btnSched.classList.add("running");
    btnSchedIcon.textContent = "⏹";
    btnSchedLabel.textContent = "توقف پایش خودکار";
  } else {
    schedDot.className = "status-dot status-neutral";
    schedText.textContent = "متوقف در پنل";
    schedSub.textContent = `رژیم زمانی: عادی هر ${toFaDigits(state.scheduler.normal_interval_minutes)} دقیقه`;
    btnSched.classList.remove("running");
    btnSchedIcon.textContent = "▶️";
    btnSchedLabel.textContent = "روشن کردن پایش خودکار";
  }

  // کارت ۳: وضعیت منوی هفته آینده
  const menuDot = document.getElementById("menu-dot");
  const menuText = document.getElementById("menu-status-text");
  const menuSub = document.getElementById("menu-week-sub");
  menuSub.textContent = state.weeks.next.jalali_range;

  if (state.menu.published && state.weeks.selected.is_next_week) {
    menuDot.className = "status-dot status-ok";
    menuText.textContent = `منتشر شده (${toFaDigits(state.menu.total_meals)} گزینه غذا)`;
  } else {
    menuDot.className = "status-dot status-warn";
    menuText.textContent = "هنوز منتشر نشده است";
  }

  // کارت ۴: کانال‌های اعلان
  const activeChannels = state.channels.filter((c) => c.active).map((c) => c.name);
  const notifDot = document.getElementById("notif-dot");
  const notifText = document.getElementById("notif-status-text");
  if (activeChannels.length > 0) {
    notifDot.className = "status-dot status-ok";
    notifText.textContent = `فعال: ${activeChannels.join(" + ")}`;
  } else {
    notifDot.className = "status-dot status-warn";
    notifText.textContent = "هیچ کانالی فعال نیست";
  }
  document.getElementById("stats-sub").textContent =
    `${toFaDigits(state.stats.total_checks)} بررسی کل • ${toFaDigits(state.stats.total_sent_events)} اعلان ارسال‌شده`;

  // نمایش تعداد گیرندگان فعال روی تب مشترکین تلگرام
  const subBtn = document.getElementById("tab-btn-subscribers");
  if (subBtn && state.subscribers_summary) {
    const cnt = state.subscribers_summary.enabled_count || 0;
    subBtn.textContent = cnt > 0
      ? `👥 مشترکین ربات تلگرام (${toFaDigits(cnt)})`
      : "👥 مشترکین ربات تلگرام";
  }

  // بنر آخرین خطای فنی در صورت وجود
  const errBanner = document.getElementById("error-banner");
  const lastCheck = state.stats.last_check;
  if (
    lastCheck &&
    lastCheck.status !== "MENU_PUBLISHED" &&
    lastCheck.status !== "MENU_NOT_PUBLISHED"
  ) {
    errBanner.classList.remove("hidden");
    errBanner.innerHTML = `<div><strong>⚠️ نتیجه آخرین بررسی (${escapeHtml(lastCheck.checked_at_fa)}):</strong> [${escapeHtml(STATUS_LABELS[lastCheck.status] || lastCheck.status)}] — ${escapeHtml(lastCheck.message)}</div>`;
  } else {
    errBanner.classList.add("hidden");
  }

  // لیست هفته‌های ذخیره‌شده
  const weekSel = document.getElementById("saved-weeks-select");
  const prevVal = selectedWeekIso;
  weekSel.innerHTML = `<option value="">هفته آینده (${escapeHtml(state.weeks.next.start_iso)})</option>`;
  (state.weeks.saved_weeks || []).forEach((w) => {
    if (w.week_start !== state.weeks.next.start_iso) {
      const opt = document.createElement("option");
      opt.value = w.week_start;
      opt.textContent = `هفته ذخیره‌شده: ${w.week_start} تا ${w.week_end}`;
      weekSel.appendChild(opt);
    }
  });
  weekSel.value = prevVal;

  // عنوان پویا بر اساس هفته انتخابی
  const boardTitle = document.getElementById("board-week-title");
  if (boardTitle && state.weeks) {
    if (state.weeks.selected.start_iso === state.weeks.previous?.start_iso) {
      boardTitle.textContent = "📅 برنامه غذایی هفته قبل";
    } else if (state.weeks.selected.start_iso === state.weeks.current?.start_iso) {
      boardTitle.textContent = "📆 برنامه غذایی هفته جاری";
    } else {
      boardTitle.textContent = "🍽 برنامه غذایی هفته آینده";
    }
  }

  if (!demoMenuData) {
    renderMenuBoard(state.menu, state.weeks.selected.bilingual);
  }
}

/* ==================== رندر گرید ۷ روزه منو ==================== */
function getMealBadgeClass(mealType) {
  if (mealType.includes("صبحانه") || mealType.includes("سحری")) return "meal-breakfast";
  if (mealType.includes("شام") || mealType.includes("افطار")) return "meal-dinner";
  return "meal-lunch";
}

function isDormRestaurantName(name) {
  const norm = String(name || "").replace(/ي/g, "ی").replace(/ك/g, "ک");
  if (norm.includes("مرکزی")) return false;
  return norm.includes("خوابگاه") || norm.includes("کوی") || norm.includes("سرا");
}

function matchesSelfScope(restaurantName, scope, allRestaurants) {
  if (!scope || scope === "ALL") return true;
  const norm = String(restaurantName || "").replace(/ي/g, "ی").replace(/ك/g, "ک");
  if (scope === "DORM") return isDormRestaurantName(norm);
  if (scope === "CENTRAL") {
    const hasExplicitCentral = (allRestaurants || []).some((r) =>
      String(r || "").replace(/ي/g, "ی").replace(/ك/g, "ک").includes("مرکزی")
    );
    if (hasExplicitCentral) return norm.includes("مرکزی");
    return !isDormRestaurantName(norm);
  }
  return true;
}

function renderMenuBoard(menuObj, weekLabel) {
  const demoBanner = document.getElementById("demo-banner");
  const emptyCard = document.getElementById("empty-menu-state");
  const grid = document.getElementById("days-grid");

  document.getElementById("board-week-subtitle").textContent = `بازه هفته: ${weekLabel}`;

  if (menuObj.is_demo) {
    demoBanner.classList.remove("hidden");
  } else {
    demoBanner.classList.add("hidden");
  }

  const searchQuery = document.getElementById("food-search").value.trim().toLowerCase();
  const mealFilter = document.getElementById("meal-type-filter").value;
  const selfScope = document.getElementById("self-scope-filter")?.value || "ALL";

  if (!menuObj.published) {
    emptyCard.classList.remove("hidden");
    grid.innerHTML = "";
    return;
  }

  emptyCard.classList.add("hidden");
  grid.innerHTML = "";

  (menuObj.days || []).forEach((day) => {
    const filteredMeals = (day.meals || []).filter((m) => {
      if (!matchesSelfScope(m.restaurant, selfScope, menuObj.restaurants)) return false;
      if (mealFilter !== "ALL" && m.meal_type !== mealFilter) return false;
      if (searchQuery) {
        const hay = `${m.food_name} ${m.restaurant} ${m.side_dishes}`.toLowerCase();
        if (!hay.includes(searchQuery)) return false;
      }
      return true;
    });

    const card = document.createElement("article");
    card.className = "day-card";

    let mealsHtml = "";
    if (filteredMeals.length === 0) {
      mealsHtml = `<div class="no-meal-day">وعده‌ای برای این روز ثبت نشده است</div>`;
    } else {
      mealsHtml = filteredMeals
        .map((m) => {
          const isClosed = m.availability === "CLOSED";
          const availClass = isClosed ? "avail-closed" : "avail-open";
          const availText = isClosed ? "مهلت گذشته / بسته" : "قابل رزرو";
          const sideHtml = m.side_dishes
            ? `<div class="muted-text">🥗 مخلفات: ${escapeHtml(m.side_dishes)}</div>`
            : "";

          return `
            <div class="meal-item">
              <div class="meal-top-row">
                <span class="meal-badge ${getMealBadgeClass(m.meal_type)}">${escapeHtml(m.meal_type)}</span>
                <span class="avail-pill ${availClass}">${availText}</span>
              </div>
              <div class="food-title">${escapeHtml(m.food_name)}</div>
              ${sideHtml}
              <div class="meal-meta">
                <span>🏢 ${escapeHtml(m.restaurant)}</span>
                <span>💰 ${escapeHtml(m.price_toman_fa)}</span>
              </div>
            </div>
          `;
        })
        .join("");
    }

    card.innerHTML = `
      <div class="day-card-header">
        <span class="day-name">${escapeHtml(day.weekday_fa)}، ${escapeHtml(day.jalali_short)}</span>
        <span class="day-dates">${escapeHtml(day.date_iso)}</span>
      </div>
      <div class="day-meals-list">${mealsHtml}</div>
    `;
    grid.appendChild(card);
  });
}

/* ==================== بارگذاری و ذخیره تنظیمات ==================== */
async function loadConfigForm() {
  const cfg = await apiGet("api/config");
  document.getElementById("cfg-tg-enabled").checked = Boolean(cfg.telegram_enabled);
  document.getElementById("cfg-tg-token").value = "";
  document.getElementById("cfg-tg-token-hint").textContent = cfg.has_telegram_bot_token
    ? `توکن فعلی ثبت شده است (${cfg.telegram_bot_token_masked}). فقط در صورت تمایل به تغییر، توکن جدید وارد کنید.`
    : "هنوز توکن ربات تلگرام ثبت نشده است.";

  document.getElementById("cfg-tg-chat").value = cfg.telegram_chat_id || "";
  document.getElementById("cfg-tg-proxy").value = cfg.telegram_proxy_url || "";
  document.getElementById("cfg-win-enabled").checked = Boolean(cfg.windows_notification_enabled);
  document.getElementById("cfg-win-sound").checked = Boolean(cfg.windows_notification_sound);
  document.getElementById("cfg-normal-min").value = cfg.check_interval_normal_minutes ?? 60;
  document.getElementById("cfg-peak-min").value = cfg.check_interval_peak_minutes ?? 12;
  document.getElementById("cfg-self-filter").value = cfg.samad_self_name_filter || "";
  document.getElementById("cfg-strategy").value = cfg.samad_fetch_strategy || "auto";
  document.getElementById("cfg-log-level").value = cfg.log_level || "INFO";
  document.getElementById("cfg-ignore-proxy").checked = Boolean(cfg.samad_ignore_env_proxy);
}

/* ==================== تاریخچه و لاگ ==================== */
async function loadLogsAndHistory() {
  const data = await apiGet("api/logs");
  const tbody = document.getElementById("history-tbody");
  const checks = data.checks || [];

  if (checks.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted-center">هنوز بررسی ثبت نشده است.</td></tr>`;
  } else {
    tbody.innerHTML = checks
      .map(
        (c) => `
        <tr>
          <td>${escapeHtml(c.checked_at_fa)}</td>
          <td>${escapeHtml(c.target_week_start)}</td>
          <td><strong>${escapeHtml(STATUS_LABELS[c.status] || c.status)}</strong></td>
          <td>${toFaDigits(c.meals_count)}</td>
          <td>${toFaDigits(c.duration_ms)} ms</td>
          <td>${escapeHtml(c.message)}</td>
        </tr>
      `
      )
      .join("");
  }

  const logEl = document.getElementById("log-viewer");
  logEl.textContent = data.log_tail || "هنوز خطی در فایل گزارش ثبت نشده است.";
  logEl.scrollTop = logEl.scrollHeight;
}

/* ==================== مدیریت مشترکین ربات تلگرام ==================== */
let lastSubscribersState = null;

function renderSubscribersState(data) {
  lastSubscribersState = data;
  const warnEl = document.getElementById("sub-token-warning");
  const linkInput = document.getElementById("sub-invite-link");

  // اگر سرور پایتون قدیمی هنوز در حافظه باز باشد و مسیر /api/subscribers خطای 404 بدهد:
  if (data._httpStatus === 404 || typeof data.has_bot_token !== "boolean") {
    warnEl.classList.remove("hidden");
    warnEl.innerHTML = `<div><strong>🔄 نیاز به ری‌استارت سرور پایتون:</strong> فایل‌های ظاهری آپدیت شده‌اند، اما در پنجره PowerShell هنوز نسخه قبلی برنامه در حال اجراست! یک بار در PowerShell کلید <code>Ctrl + C</code> را بزنید، دستور <code>pip install -e .</code> و سپس <code>python -m samad_monitor ui</code> را اجرا کنید.</div>`;
    linkInput.value = "لطفاً ابتدا سرور پایتون را در PowerShell یک بار ری‌استارت کنید...";
    return;
  }

  if (!data.has_bot_token) {
    warnEl.classList.remove("hidden");
  } else {
    warnEl.classList.add("hidden");
  }

  document.getElementById("sub-auto-enabled").checked = Boolean(data.auto_subscribe);
  document.getElementById("sub-invite-code").value = data.invite_code || "";

  if (data.invite_link) {
    linkInput.value = data.invite_link;
  } else if (data.has_bot_token) {
    linkInput.value = "توکن ثبت شده است ✓ — روی دکمه «🔄 بروزرسانی از تلگرام» پایین بزنید تا آدرس ربات دریافت شود";
  } else {
    linkInput.value = "ابتدا توکن ربات تلگرام را در تب تنظیمات ذخیره کنید";
  }

  // ۱. لیست پیام‌دهندگان اخیر ربات (برای افزودن تک‌کلیکی)
  const contactsGrid = document.getElementById("recent-contacts-list");
  const contacts = data.recent_contacts || [];
  if (contacts.length === 0) {
    contactsGrid.innerHTML = `<div class="muted-text">هنوز پیامی دریافت نشده است. به دوستتان بگویید در تلگرام به ربات شما <code>/start</code> بزند و سپس دکمه «بروزرسانی از تلگرام» را بزنید.</div>`;
  } else {
    contactsGrid.innerHTML = contacts
      .map((c) => {
        const icon = c.chat_type === "private" ? "👤" : c.chat_type === "channel" ? "📢" : "👥";
        const uname = c.username ? `@${c.username} • ` : "";
        const actionBtn = c.is_subscribed
          ? `<span class="avail-pill avail-open">عضو فعال ✓</span>`
          : `<button type="button" class="btn btn-accent btn-sm js-quick-add"
               data-cid="${escapeHtml(c.chat_id)}"
               data-name="${escapeHtml(c.display_name)}"
               data-uname="${escapeHtml(c.username || "")}"
               data-ctype="${escapeHtml(c.chat_type || "private")}">➕ افزودن</button>`;
        return `
          <div class="contact-card">
            <div class="contact-meta">
              <strong>${icon} ${escapeHtml(c.display_name)}</strong>
              <small>${escapeHtml(uname)}ID: <code>${escapeHtml(c.chat_id)}</code></small>
              <small>آخرین پیام: ${escapeHtml(c.last_message || "—")}</small>
            </div>
            <div>${actionBtn}</div>
          </div>
        `;
      })
      .join("");
  }

  // ۲. جدول گیرندگان ثبت‌شده
  const subs = data.subscribers || [];
  const enabledCnt = subs.filter((s) => s.enabled).length;
  document.getElementById("subscribers-count-label").textContent =
    `${toFaDigits(enabledCnt)} فعال از ${toFaDigits(subs.length)} نفر`;
  const subTabBtn = document.getElementById("tab-btn-subscribers");
  if (subTabBtn) {
    subTabBtn.textContent = enabledCnt > 0
      ? `👥 مشترکین ربات تلگرام (${toFaDigits(enabledCnt)})`
      : "👥 مشترکین ربات تلگرام";
  }

  const tbody = document.getElementById("subscribers-tbody");
  if (subs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted-center">هنوز هیچ گیرنده‌ای ثبت نشده است. از فرم بالا دوست خود را اضافه کنید یا به او بگویید به ربات Start بزند!</td></tr>`;
  } else {
    tbody.innerHTML = subs
      .map((s) => {
        const icon = s.chat_type === "private" ? "👤" : s.chat_type === "channel" ? "📢" : "👥";
        const unameTag = s.username ? ` <small class="muted-text">(@${escapeHtml(s.username)})</small>` : "";
        const isAdmin = s.role === "admin";
        const roleHtml = isAdmin
          ? `<span class="role-pill role-admin">مدیر (منو + سشن)</span>`
          : `<span class="role-pill role-sub">مشترک منوی غذا</span>`;
        const statusHtml = s.enabled
          ? `<span class="avail-pill avail-open">فعال ✅</span>`
          : `<span class="avail-pill avail-closed">متوقف ⏸</span>`;
        const toggleLabel = s.enabled ? "⏸ توقف" : "▶️ فعال";
        const nextRole = isAdmin ? "subscriber" : "admin";
        const roleBtnLabel = isAdmin ? "تبدیل به مشترک" : "تبدیل به مدیر";

        return `
          <tr>
            <td><strong>${icon} ${escapeHtml(s.display_name)}</strong>${unameTag}</td>
            <td><code dir="ltr">${escapeHtml(s.chat_id)}</code></td>
            <td>${roleHtml}</td>
            <td>${statusHtml}</td>
            <td>
              <div class="row-actions">
                <button type="button" class="btn btn-outline btn-sm js-sub-test" data-cid="${escapeHtml(s.chat_id)}">📨 تست</button>
                <button type="button" class="btn btn-ghost btn-sm js-sub-toggle" data-cid="${escapeHtml(s.chat_id)}" data-enabled="${s.enabled ? "1" : "0"}">${toggleLabel}</button>
                <button type="button" class="btn btn-ghost btn-sm js-sub-role" data-cid="${escapeHtml(s.chat_id)}" data-next-role="${nextRole}">${roleBtnLabel}</button>
                <button type="button" class="btn btn-ghost btn-sm js-sub-del" style="color:#dc2626;" data-cid="${escapeHtml(s.chat_id)}" data-name="${escapeHtml(s.display_name)}">🗑 حذف</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }
}

async function loadSubscribersTab() {
  const data = await apiGet("api/subscribers");
  renderSubscribersState(data);
}

/* ==================== رفرش وضعیت ==================== */
async function refreshState() {
  const qs = selectedWeekIso ? `?week=${encodeURIComponent(selectedWeekIso)}` : "";
  const state = await apiGet(`api/state${qs}`);
  renderDashboard(state);
}

/* ==================== رویدادهای صفحه ==================== */
document.addEventListener("DOMContentLoaded", async () => {
  // مدیریت تم روشن / تاریک
  const savedTheme = localStorage.getItem("samad_ui_theme") || "light";
  document.documentElement.setAttribute("data-theme", savedTheme);
  document.getElementById("theme-icon").textContent = savedTheme === "dark" ? "☀️" : "🌙";

  document.getElementById("theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("samad_ui_theme", next);
    document.getElementById("theme-icon").textContent = next === "dark" ? "☀️" : "🌙";
  });

  // جابه‌جایی تب‌ها
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      const targetId = btn.getAttribute("data-tab");
      document.getElementById(targetId).classList.add("active");

      if (targetId === "tab-subscribers") await loadSubscribersTab();
      if (targetId === "tab-settings") await loadConfigForm();
      if (targetId === "tab-logs") await loadLogsAndHistory();
    });
  });

  // دکمه بررسی فوری
  const btnCheck = document.getElementById("btn-check-now");
  btnCheck.addEventListener("click", async () => {
    btnCheck.disabled = true;
    document.getElementById("btn-check-label").textContent = "در حال اتصال به سماد...";
    try {
      demoMenuData = null;
      const res = await apiPost("api/check-now");
      showToast(res.message || "بررسی انجام شد.", !res.ok);
      await refreshState();
    } catch (err) {
      showToast("خطا در ارتباط با سرور پنل: " + err, true);
    } finally {
      btnCheck.disabled = false;
      document.getElementById("btn-check-label").textContent = "بررسی فوری منوی هفته آینده";
    }
  });

  // دکمه روشن/خاموش پایش خودکار
  const btnSched = document.getElementById("btn-scheduler-toggle");
  btnSched.addEventListener("click", async () => {
    btnSched.disabled = true;
    try {
      const running = currentState?.scheduler?.running_in_panel;
      const endpoint = running ? "api/scheduler/stop" : "api/scheduler/start";
      const res = await apiPost(endpoint);
      showToast(res.message, !res.ok);
      setTimeout(refreshState, 600);
    } finally {
      btnSched.disabled = false;
    }
  });

  // دکمه ورود دستی با مرورگر
  const btnLogin = document.getElementById("btn-login");
  btnLogin.addEventListener("click", async () => {
    btnLogin.disabled = true;
    showToast("در حال باز کردن مرورگر کرومیوم برای ورود امن به دانشگاه شهید بهشتی...");
    try {
      const res = await apiPost("api/login");
      showToast(res.message, !res.ok);
      await refreshState();
    } finally {
      btnLogin.disabled = false;
    }
  });

  // دکمه تست اعلان
  document.getElementById("btn-test-notify").addEventListener("click", async () => {
    const res = await apiPost("api/test-notify");
    showToast(res.message, !res.ok);
  });

  // دکمه عیب‌یاب سلامت (Doctor)
  document.getElementById("btn-doctor").addEventListener("click", async () => {
    const drawer = document.getElementById("doctor-drawer");
    const grid = document.getElementById("doctor-results");
    drawer.classList.remove("hidden");
    grid.innerHTML = "در حال بررسی سلامت اجزای سیستم...";
    const res = await apiGet("api/doctor");
    grid.innerHTML = (res.checks || [])
      .map(
        (c) => `
        <div class="doctor-item">
          <strong>${c.ok ? "✅" : "⚠️"} ${escapeHtml(c.title)}</strong>
          <span class="muted-text">${escapeHtml(c.detail)}</span>
        </div>
      `
      )
      .join("");
  });

  document.getElementById("btn-close-doctor").addEventListener("click", () => {
    document.getElementById("doctor-drawer").classList.add("hidden");
  });

  // دکمه مشاهده پیش‌نمایش آزمایشی منو
  document.getElementById("btn-preview-demo").addEventListener("click", async () => {
    demoMenuData = await apiGet("api/demo-menu");
    renderMenuBoard(demoMenuData, demoMenuData.week_bilingual);
  });

  document.getElementById("btn-exit-demo").addEventListener("click", () => {
    demoMenuData = null;
    if (currentState) {
      renderMenuBoard(currentState.menu, currentState.weeks.selected.bilingual);
    }
  });

  // فیلترها و جستجوی منو
  const rerenderActiveBoard = () => {
    if (demoMenuData) {
      renderMenuBoard(demoMenuData, demoMenuData.week_bilingual);
    } else if (currentState) {
      renderMenuBoard(currentState.menu, currentState.weeks.selected.bilingual);
    }
  };
  document.getElementById("food-search").addEventListener("input", rerenderActiveBoard);
  document.getElementById("meal-type-filter").addEventListener("change", rerenderActiveBoard);
  document.getElementById("self-scope-filter").addEventListener("change", rerenderActiveBoard);

  // دکمه واکشی سریع منوی هفته قبل در داشبورد
  const btnPrevWeek = document.getElementById("btn-fetch-prev-week");
  if (btnPrevWeek) {
    btnPrevWeek.addEventListener("click", async () => {
      btnPrevWeek.disabled = true;
      btnPrevWeek.textContent = "⏳ در حال دریافت هفته قبل...";
      try {
        demoMenuData = null;
        const res = await apiPost("api/fetch-week", { week_key: "prev" });
        showToast(res.message || "بررسی هفته قبل انجام شد.", !res.ok);
        if (res.week_start) {
          selectedWeekIso = res.week_start;
        }
        await refreshState();
      } finally {
        btnPrevWeek.disabled = false;
        btnPrevWeek.textContent = "📅 منوی هفته قبل";
      }
    });
  }

  document.getElementById("saved-weeks-select").addEventListener("change", async (e) => {
    selectedWeekIso = e.target.value;
    demoMenuData = null;
    await refreshState();
  });

  // چیپ‌های انتخاب سریع پروکسی تلگرام + تشخیص خودکار
  document.querySelectorAll(".chip-btn[data-proxy]").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.getElementById("cfg-tg-proxy").value = chip.getAttribute("data-proxy") || "";
    });
  });

  const btnDetectProxy = document.getElementById("btn-detect-proxy");
  if (btnDetectProxy) {
    btnDetectProxy.addEventListener("click", async () => {
      const res = await apiGet("api/detect-proxy");
      if (res.ok && res.proxy_url) {
        document.getElementById("cfg-tg-proxy").value = res.proxy_url;
        showToast(res.message, false);
      } else {
        showToast(res.message || "پورت فعالی شناسایی نشد.", true);
      }
    });
  }

  // ذخیره فرم تنظیمات
  document.getElementById("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("btn-save-settings");
    btn.disabled = true;
    try {
      const payload = {
        telegram_enabled: document.getElementById("cfg-tg-enabled").checked,
        telegram_bot_token: document.getElementById("cfg-tg-token").value.trim(),
        telegram_chat_id: document.getElementById("cfg-tg-chat").value.trim(),
        telegram_proxy_url: document.getElementById("cfg-tg-proxy").value.trim(),
        windows_notification_enabled: document.getElementById("cfg-win-enabled").checked,
        windows_notification_sound: document.getElementById("cfg-win-sound").checked,
        check_interval_normal_minutes: Number(document.getElementById("cfg-normal-min").value || 60),
        check_interval_peak_minutes: Number(document.getElementById("cfg-peak-min").value || 12),
        samad_self_name_filter: document.getElementById("cfg-self-filter").value.trim(),
        samad_fetch_strategy: document.getElementById("cfg-strategy").value,
        log_level: document.getElementById("cfg-log-level").value,
        samad_ignore_env_proxy: document.getElementById("cfg-ignore-proxy").checked,
      };
      const res = await apiPost("api/config", payload);
      showToast(res.message, !res.ok);
      await loadConfigForm();
      await refreshState();
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("btn-refresh-logs").addEventListener("click", loadLogsAndHistory);

  // ==================== رویدادهای تب مشترکین ربات تلگرام ====================
  document.getElementById("btn-copy-invite").addEventListener("click", async () => {
    const val = document.getElementById("sub-invite-link").value.trim();
    if (!val.startsWith("https://t.me/")) {
      showToast("ابتدا روی «🔄 بروزرسانی از تلگرام» کلیک کنید تا یوزرنیم ربات دریافت شود.", true);
      return;
    }
    try {
      await navigator.clipboard.writeText(val);
      showToast("لینک دعوت کپی شد! آن را برای دوستان خود بفرستید.");
    } catch (_) {
      document.getElementById("sub-invite-link").select();
      showToast("لینک انتخاب شد؛ با Ctrl+C کپی کنید.");
    }
  });

  document.getElementById("sub-invite-code").addEventListener("input", (e) => {
    const uname = lastSubscribersState?.bot_username;
    if (!uname) return;
    const code = e.target.value.trim();
    document.getElementById("sub-invite-link").value = code
      ? `https://t.me/${uname}?start=${code}`
      : `https://t.me/${uname}`;
  });

  document.getElementById("btn-save-auto-sub").addEventListener("click", async () => {
    const res = await apiPost("api/subscribers/auto-settings", {
      auto_subscribe: document.getElementById("sub-auto-enabled").checked,
      invite_code: document.getElementById("sub-invite-code").value.trim(),
    });
    showToast(res.message, !res.ok);
    if (res.subscribers) renderSubscribersState(res);
  });

  document.getElementById("btn-sync-contacts").addEventListener("click", async () => {
    const btn = document.getElementById("btn-sync-contacts");
    btn.disabled = true;
    try {
      const res = await apiPost("api/subscribers/sync");
      showToast(res.message, !res.ok);
      if (res.subscribers) renderSubscribersState(res);
      await refreshState();
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("add-subscriber-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("btn-add-sub-submit");
    btn.disabled = true;
    try {
      const res = await apiPost("api/subscribers/add", {
        display_name: document.getElementById("sub-add-name").value.trim(),
        chat_id: document.getElementById("sub-add-chat-id").value.trim(),
        role: document.getElementById("sub-add-role").value,
        send_welcome: document.getElementById("sub-add-welcome").checked,
      });
      showToast(res.message, !res.ok);
      if (res.ok) {
        document.getElementById("sub-add-name").value = "";
        document.getElementById("sub-add-chat-id").value = "";
        renderSubscribersState(res);
        await refreshState();
      }
    } finally {
      btn.disabled = false;
    }
  });

  // کلیک یک‌کلیکی افزودن از لیست پیام‌دهندگان اخیر
  document.getElementById("recent-contacts-list").addEventListener("click", async (e) => {
    const btn = e.target.closest(".js-quick-add");
    if (!btn) return;
    btn.disabled = true;
    const res = await apiPost("api/subscribers/add", {
      chat_id: btn.getAttribute("data-cid"),
      display_name: btn.getAttribute("data-name"),
      username: btn.getAttribute("data-uname"),
      chat_type: btn.getAttribute("data-ctype"),
      role: "subscriber",
      added_via: "bot_discovery",
      send_welcome: true,
    });
    showToast(res.message, !res.ok);
    if (res.subscribers) renderSubscribersState(res);
    await refreshState();
  });

  // عملیات ردیف‌های جدول مشترکین (تست / فعال‌سازی / نقش / حذف)
  document.getElementById("subscribers-tbody").addEventListener("click", async (e) => {
    const testBtn = e.target.closest(".js-sub-test");
    if (testBtn) {
      testBtn.disabled = true;
      try {
        const res = await apiPost("api/subscribers/test", {
          chat_id: testBtn.getAttribute("data-cid"),
        });
        showToast(res.message, !res.ok);
      } finally {
        testBtn.disabled = false;
      }
      return;
    }

    const toggleBtn = e.target.closest(".js-sub-toggle");
    if (toggleBtn) {
      const currentlyEnabled = toggleBtn.getAttribute("data-enabled") === "1";
      const res = await apiPost("api/subscribers/update", {
        chat_id: toggleBtn.getAttribute("data-cid"),
        enabled: !currentlyEnabled,
      });
      showToast(res.message, !res.ok);
      if (res.subscribers) renderSubscribersState(res);
      await refreshState();
      return;
    }

    const roleBtn = e.target.closest(".js-sub-role");
    if (roleBtn) {
      const res = await apiPost("api/subscribers/update", {
        chat_id: roleBtn.getAttribute("data-cid"),
        role: roleBtn.getAttribute("data-next-role"),
      });
      showToast(res.message, !res.ok);
      if (res.subscribers) renderSubscribersState(res);
      return;
    }

    const delBtn = e.target.closest(".js-sub-del");
    if (delBtn) {
      const cid = delBtn.getAttribute("data-cid");
      const name = delBtn.getAttribute("data-name");
      if (!confirm(`آیا از حذف «${name}» (${cid}) از لیست گیرندگان مطمئن هستید؟`)) return;
      const res = await apiPost("api/subscribers/delete", { chat_id: cid });
      showToast(res.message, !res.ok);
      if (res.subscribers) renderSubscribersState(res);
      await refreshState();
    }
  });

  // بارگذاری اولیه و رفرش خودکار هر ۸ ثانیه
  await refreshState();
  await loadConfigForm();
  setInterval(refreshState, 8000);
});
