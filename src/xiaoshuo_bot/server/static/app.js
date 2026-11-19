(function () {
  "use strict";

  const SESSION_TOKEN_KEY = "xiaoshuo_admin_token";

  const API_ROUTES = Object.freeze({
    HEALTH: "/api/health",
    GENRES: "/api/genres",
    BOOKS: "/api/books",
    BOOKS_AUTO_CREATE: "/api/books/auto-create",
    BOOK_STATUS: "/api/books/{book_id}/status",
    BOOK_CHAPTERS: "/api/books/{book_id}/chapters",
    BOOK_CHAPTER_DETAIL: "/api/books/{book_id}/chapters/{chapter}",
    BOOK_CHAPTER_LOCK: "/api/books/{book_id}/chapters/{chapter}/lock",
    BOOK_EXPORT: "/api/books/{book_id}/export",
    BOOK_COVER: "/api/books/{book_id}/cover",
    BOOK_CUT: "/api/books/{book_id}/cut",
    BOOK_PROMOTE: "/api/books/{book_id}/promote",
    BOOK_EXTEND: "/api/books/{book_id}/extend",
    BOOK_RESUME: "/api/books/{book_id}/resume",
    CPA_AUTH_FILES: "/api/cpa/auth-files",
    CPA_AUTH_FILE_DETAIL: "/api/cpa/auth-files/{name}",
    CPA_MODELS: "/api/cpa/models",
    CPA_USAGE: "/api/cpa/usage",
    CPA_OAUTH: "/api/cpa/oauth",
    CPA_BINDINGS: "/api/cpa/bindings",
    MODEL_SWITCHES: "/api/model-switches",
    COMPANION_RESUME: "/api/companion/resume",
    EVENTS_STREAM: "/api/events/stream",
    PRODUCTION_KPI: "/api/production/kpi",
  });

  const SSE_EVENTS = Object.freeze({
    HEARTBEAT: "heartbeat",
    CHAPTER_STARTED: "chapter_started",
    CHAPTER_ATTEMPT: "chapter_attempt",
    GATE_FAILED: "gate_failed",
    GATE_PASSED: "gate_passed",
    CRITIC_SCORED: "critic_scored",
    SURGICAL_POLISHED: "surgical_polished",
    CHAPTER_STOCKPILED: "chapter_stockpiled",
    BOOK_ISOLATED: "book_isolated",
    SAFETY_CUSHION_ALERT: "safety_cushion_alert",
    COMPANION_CAPTCHA: "companion_captcha",
    COMPANION_RESUMED: "companion_resumed",
    METRICS_COLLECTED: "metrics_collected",
    RACING_DECISION: "racing_decision",
    CPA_QUOTA_ALERT: "cpa_quota_alert",
  });

  const CPA_ROLES = Object.freeze([
    { role: "planner", label: "策划总纲 (planner)" },
    { role: "writer", label: "正文主笔 (writer)" },
    { role: "critic", label: "毒舌主编 (critic)" },
    { role: "polisher", label: "手术精修 (polisher)" },
    { role: "image", label: "封面生图 (image)" },
  ]);

  // Private studio & export-only UI gate (22A/23A: keep backend & contract code, hide racing-matrix & companion UI)
  const PRIVATE_STUDIO_MODE = true;

  const VIEW_META = Object.freeze({
    overview: {
      title: "书目总览",
      subtitle: "私人创作控制台 · 仅导出人工发布 · 实时门禁监控",
    },
    create: {
      title: "三源爆款立项舱",
      subtitle: "热榜风向 + 男女频 10+1 配方库 + 对标拆书换皮（≤7 字硬防重）",
    },
    cpa: {
      title: "CPA 订阅账号池与五角色路由中心",
      subtitle: "凭据热加载 · 429 健康监控 · 五角色同档模型绑定（严禁跨档降级）",
    },
    live: {
      title: "SSE 实时码字与门禁审计直播间",
      subtitle: "流式推送章节生产、AI-TIC-CHECK 门禁、四维打分与手术刀精修对比",
    },
    reader: {
      title: "分章阅读、锁章与导出中心",
      subtitle: "纯净正文与物理隔离作者有话说 · 锁章防覆盖 · 多平台标准打包",
    },
  });

  const BOOK_STATUS_LABELS = Object.freeze({
    ACTIVE: { text: "写作中", cls: "tag-emerald" },
    ISOLATED_GATE_FAILURE: { text: "门禁故障隔离", cls: "tag-gold" },
    PAUSED_AUTH_CAPTCHA: { text: "验证码/登录挂起", cls: "tag-danger" },
    PAUSED_QUOTA_EXHAUSTED: { text: "超预算 / 额度熔断", cls: "tag-danger" },
    PAUSED_MANUAL: { text: "手动暂停", cls: "tag-slate" },
    CUT_LOSS: { text: "已止损切书", cls: "tag-purple" },
    COMPLETED: { text: "已完结", cls: "tag-cyan" },
    PAUSED: { text: "手动暂停", cls: "tag-slate" },
  });

  // Reusable alert kinds for both Q14 quota/budget circuit-breaker and future WorkBuddy model rotation
  const MODEL_QUOTA_ALERT_KINDS = Object.freeze({
    QUOTA_EXHAUSTED: "quota_exhausted",
    MODEL_SWITCHED: "model_switched",
  });

  const CHAPTER_STATUS_LABELS = Object.freeze({
    STOCKPILED: { text: "已过审成稿", cls: "tag-emerald" },
    LEASED: { text: "伴侣租约中", cls: "tag-cyan" },
    DRAFT_SAVED: { text: "已存草稿箱", cls: "tag-gold" },
    SCHEDULED: { text: "已定时发布", cls: "tag-purple" },
    PUBLISHED: { text: "已正式发布", cls: "tag-emerald" },
    PAUSED_AUTH_CAPTCHA: { text: "验证码挂起", cls: "tag-danger" },
    FAILED_HARD_MISMATCH: { text: "对账不符失败", cls: "tag-danger" },
  });

  const state = {
    activeView: "overview",
    authToken: loadSessionToken(),
    authRequired: false,
    health: null,
    books: [],
    bookFilter: "all",
    genres: [],
    genreChannelFilter: "all",
    selectedGenreId: "",
    cpaAuthFiles: [],
    cpaUsage: null,
    cpaModels: [],
    cpaBindings: {},
    captchaAlerts: new Map(),
    modelQuotaAlerts: new Map(),
    dismissedQuotaAlertIds: new Set(),
    liveEvents: [],
    sseConnected: false,
    sseConnecting: false,
    sseAbortController: null,
    sseReconnectTimer: null,
    sseReconnectAttempts: 0,
    readerBookId: "",
    readerChapters: [],
    readerSelectedChapter: null,
    readerChapterDetail: null,
    coverObjectUrls: [],
    cutTargetBookId: "",
    cutConfirmationVersion: 0,
  };

  function escapeHtml(value) {
    if (value === null || value === undefined) {
      return "";
    }
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function loadSessionToken() {
    try {
      return window.sessionStorage ? window.sessionStorage.getItem(SESSION_TOKEN_KEY) || "" : "";
    } catch (err) {
      console.warn("sessionStorage unavailable:", err);
      return "";
    }
  }

  function persistSessionToken(token) {
    state.authToken = String(token || "").trim();
    try {
      if (window.sessionStorage) {
        if (state.authToken) {
          window.sessionStorage.setItem(SESSION_TOKEN_KEY, state.authToken);
        } else {
          window.sessionStorage.removeItem(SESSION_TOKEN_KEY);
        }
      }
    } catch (err) {
      console.warn("Failed to persist token in sessionStorage:", err);
    }
    updateAuthUi();
  }

  function formatApiRoute(template, params) {
    let result = String(template);
    const entries = Object.entries(params || {});
    for (let i = 0; i < entries.length; i += 1) {
      const key = entries[i][0];
      const rawVal = entries[i][1];
      const val = rawVal === null || rawVal === undefined ? "" : String(rawVal).trim();
      if (!val) {
        throw new Error("路由参数 " + key + " 不能为空");
      }
      result = result.replace("{" + key + "}", encodeURIComponent(val));
    }
    return result;
  }

  function buildAuthHeaders(extra) {
    const headers = Object.assign({}, extra || {});
    if (state.authToken) {
      headers.Authorization = "Bearer " + state.authToken;
    }
    return headers;
  }

  async function apiRequest(url, options) {
    const opts = Object.assign({}, options || {});
    opts.headers = buildAuthHeaders(opts.headers);
    const resp = await fetch(url, opts);

    if (resp.status === 401 || resp.status === 403) {
      state.authRequired = true;
      updateAuthUi("服务端拒绝访问 (HTTP " + resp.status + ")，请输入有效管理员 Bearer Token");
      throw new Error("未授权访问 (HTTP " + resp.status + ")");
    }

    const contentType = (resp.headers.get("content-type") || "").toLowerCase();
    let payload = null;
    if (contentType.indexOf("application/json") !== -1) {
      payload = await resp.json();
    } else {
      const txt = await resp.text();
      if (txt) {
        try {
          payload = JSON.parse(txt);
        } catch (parseErr) {
          payload = { message: txt };
        }
      }
    }

    if (!resp.ok) {
      const errMsg =
        (payload && (payload.message || payload.error || (payload.detail && payload.detail.message))) ||
        ("请求失败 HTTP " + resp.status);
      throw new Error(errMsg);
    }

    state.authRequired = false;
    updateAuthUi();
    return payload;
  }

  function showToast(message, level) {
    const stack = document.getElementById("toast-stack");
    if (!stack) {
      return;
    }
    const item = document.createElement("div");
    item.className = "toast " + (level === "ok" ? "is-ok" : level === "err" ? "is-err" : "");
    item.textContent = String(message || "");
    stack.appendChild(item);
    window.setTimeout(function () {
      if (item.parentNode) {
        item.parentNode.removeChild(item);
      }
    }, 4200);
  }

  function clearErrorToasts() {
    const stack = document.getElementById("toast-stack");
    if (!stack) {
      return;
    }
    stack.querySelectorAll(".toast.is-err, .toast.is-error").forEach(function (node) {
      if (node.parentNode) {
        node.parentNode.removeChild(node);
      }
    });
  }

  function updateAuthUi(warningMsg) {
    const btnLabel = document.getElementById("auth-btn-label");
    const banner = document.getElementById("auth-warning-banner");
    const bannerMsg = document.getElementById("auth-warning-msg");
    if (btnLabel) {
      btnLabel.textContent = state.authToken ? "🔓 已配置管理令牌" : "🔐 设置管理令牌";
    }
    if (banner) {
      banner.hidden = !state.authRequired;
    }
    if (bannerMsg && warningMsg) {
      bannerMsg.textContent = warningMsg;
    }
  }

  function openModal(id) {
    const el = document.getElementById(id);
    if (el) {
      el.hidden = false;
    }
  }

  function closeModal(id) {
    const el = document.getElementById(id);
    if (el) {
      el.hidden = true;
    }
  }

  function switchView(viewName) {
    if (PRIVATE_STUDIO_MODE && viewName === "create") {
      viewName = "overview";
    }
    if (!VIEW_META[viewName]) {
      return;
    }
    state.activeView = viewName;

    const navButtons = document.querySelectorAll(".nav-btn");
    navButtons.forEach(function (btn) {
      const isMatch = btn.getAttribute("data-view") === viewName;
      btn.classList.toggle("is-active", isMatch);
      btn.setAttribute("aria-selected", isMatch ? "true" : "false");
    });

    const panels = document.querySelectorAll("[data-view-panel]");
    panels.forEach(function (panel) {
      const isMatch = panel.getAttribute("data-view-panel") === viewName;
      panel.classList.toggle("is-active", isMatch);
      panel.hidden = !isMatch;
    });

    const meta = VIEW_META[viewName];
    const titleEl = document.getElementById("page-title");
    const subEl = document.getElementById("page-subtitle");
    if (titleEl) {
      titleEl.textContent = meta.title;
    }
    if (subEl) {
      subEl.textContent = meta.subtitle;
    }
    document.title = meta.title + " · xiaoshuo-bot";

    if (viewName === "create" && state.genres.length === 0) {
      loadGenres();
    } else if (viewName === "cpa") {
      loadCpaPanel();
    } else if (viewName === "reader") {
      populateBookSelects();
      if (state.readerBookId) {
        loadBookChapters(state.readerBookId);
      }
    }
  }

  /* ------------------------------------------------------------------
   * Health & Matrix Overview
   * ------------------------------------------------------------------ */
  async function loadHealth() {
    const statusEl = document.getElementById("health-status-text");
    const cpaEl = document.getElementById("health-cpa-text");
    const timeEl = document.getElementById("health-shanghai-time");
    try {
      const data = await apiRequest(API_ROUTES.HEALTH, { method: "GET" });
      state.health = data || {};
      if (statusEl) {
        statusEl.textContent = data && data.status === "ok" ? "在线正常" : String((data && data.status) || "异常");
        statusEl.className = "status-val " + (data && data.status === "ok" ? "is-ok" : "is-err");
      }
      if (cpaEl) {
        const ok = Boolean(data && data.cpa_reachable);
        cpaEl.textContent = ok ? "已连通" : "不可达";
        cpaEl.className = "status-val " + (ok ? "is-ok" : "is-err");
      }
      if (timeEl && data && data.shanghai_time) {
        const t = String(data.shanghai_time);
        const match = t.match(/T(\d{2}:\d{2}:\d{2})/);
        timeEl.textContent = match ? match[1] : t.slice(0, 19);
      }
      return state.health;
    } catch (err) {
      if (statusEl) {
        statusEl.textContent = "连接失败";
        statusEl.className = "status-val is-err";
      }
      console.warn("Health check failed:", err);
      return null;
    }
  }

  function normalizeBooks(payload) {
    if (Array.isArray(payload)) {
      return payload;
    }
    if (payload && Array.isArray(payload.books)) {
      return payload.books;
    }
    if (payload && Array.isArray(payload.items)) {
      return payload.items;
    }
    return [];
  }

  async function loadBooks() {
    try {
      const payload = await apiRequest(API_ROUTES.BOOKS, { method: "GET" });
      state.books = normalizeBooks(payload);
      syncCaptchaAlertsFromBooks();
      renderOverview();
      populateBookSelects();
      renderCaptchaBanner();
    } catch (err) {
      showToast("加载书目失败：" + err.message, "err");
    }
  }

  async function loadProductionKpi() {
    try {
      const kpi = await apiRequest(API_ROUTES.PRODUCTION_KPI, { method: "GET" });
      const value = document.getElementById("kpi-production");
      const hint = document.getElementById("kpi-production-hint");
      if (!kpi.chapters) {
        value.textContent = "— / —";
        hint.textContent = "还没有成稿章节";
        return;
      }
      value.textContent = Math.round(kpi.avg_duration_sec) + "s / " + (kpi.rewrite_rate * 100).toFixed(1) + "%";
      hint.textContent = "共 " + kpi.chapters + " 章，平均每章尝试 " + kpi.avg_attempts + " 次";
    } catch (err) {
      showToast("加载写稿效率看板失败：" + err.message, "err");
    }
  }

  function openIsolationModal(payload) {
    const bookId = String(payload.book_id || "");
    const book = state.books.find(function (b) { return b.book_id === bookId; });
    const data = payload.data || {};
    state.isolationBookId = bookId;
    const chapterNo = Number(payload.chapter || 0);
    document.getElementById("isolation-modal-book").textContent =
      "《" + (book ? book.title : bookId) + "》" + (chapterNo > 0 ? "第 " + chapterNo + " 章" : "") +
      "连续重写仍未通过审校，已熔断停笔。";
    document.getElementById("isolation-modal-reason").textContent =
      "原因：" + String(data.error || data.reason || payload.message || "见后台日志");
    openModal("isolation-modal");
  }

  function syncCaptchaAlertsFromBooks() {
    state.books.forEach(function (book) {
      if (book.status === "PAUSED_AUTH_CAPTCHA") {
        const key = book.account_id || book.book_id;
        if (!state.captchaAlerts.has(key)) {
          state.captchaAlerts.set(key, {
            account_id: book.account_id || "",
            book_id: book.book_id || "",
            reason: book.paused_reason || "触发番茄登录或验证码安全暂停",
            page_url: "",
            screenshot_base64: "",
            screenshot_mime: "image/png",
          });
        }
      }
    });
  }

  function isCriticalCushionBook(book) {
    return Boolean(book) && book.status === "ACTIVE" && Number(book.stockpiled_count || 0) < 6;
  }

  function renderOverview() {
    const books = state.books;
    const activeCount = books.filter(function (b) {
      return b.status === "ACTIVE";
    }).length;
    const criticalCushionBooks = books.filter(isCriticalCushionBook);
    const isolatedCount = books.filter(function (b) {
      return b.status === "ISOLATED_GATE_FAILURE";
    }).length;
    const quotaExhaustedCount = books.filter(function (b) {
      return b.status === "PAUSED_QUOTA_EXHAUSTED";
    }).length;
    const captchaCount = books.filter(function (b) {
      return b.status === "PAUSED_AUTH_CAPTCHA";
    }).length;

    const kpiActive = document.getElementById("kpi-active-books");
    const kpiLow = document.getElementById("kpi-low-cushion");
    const kpiLowCard = document.getElementById("kpi-card-low-cushion");
    const kpiPausedIso = document.getElementById("kpi-paused-isolated");
    const navAlertBadge = document.getElementById("nav-badge-alert");

    if (kpiActive) {
      kpiActive.textContent = activeCount + " / " + books.length;
    }
    if (kpiLow) {
      kpiLow.textContent = String(criticalCushionBooks.length);
    }
    if (kpiLowCard) {
      kpiLowCard.classList.toggle("is-alert", criticalCushionBooks.length > 0);
    }
    if (kpiPausedIso) {
      kpiPausedIso.textContent = PRIVATE_STUDIO_MODE ? String(isolatedCount) : (captchaCount + " / " + isolatedCount);
    }
    const alertTotal = PRIVATE_STUDIO_MODE
      ? (isolatedCount + quotaExhaustedCount)
      : (criticalCushionBooks.length + captchaCount + isolatedCount + quotaExhaustedCount);
    if (navAlertBadge) {
      navAlertBadge.hidden = alertTotal === 0;
      navAlertBadge.textContent = String(alertTotal);
    }

    syncQuotaAlertsFromBooks();
    renderBooksGrid();
  }

  function renderBooksGrid() {
    const grid = document.getElementById("books-grid");
    if (!grid) {
      return;
    }
    const filtered = state.books.filter(function (book) {
      if (state.bookFilter === "all") {
        return true;
      }
      if (state.bookFilter === "LOW_CUSHION") {
        return isCriticalCushionBook(book);
      }
      return book.status === state.bookFilter;
    });

    if (filtered.length === 0) {
      if (PRIVATE_STUDIO_MODE) {
        grid.innerHTML = '<div class="empty-state">当前筛选条件下暂无书目。可点击右上角「✚ 新书向导」创建新书。</div>';
      } else {
        grid.innerHTML = '<div class="empty-state">当前筛选条件下暂无书目。可点击右上角「+ 三源自动立项」全自动开新书。</div>';
      }
      return;
    }

    const html = filtered.map(function (book) {
      const bookId = String(book.book_id || "");
      const title = String(book.title || bookId);
      const stockpiled = Number(book.stockpiled_count || 0);
      const target = Math.max(1, Number(book.safety_cushion_target || 10));
      const pct = Math.min(100, Math.round((stockpiled / target) * 100));
      const isCritical = isCriticalCushionBook(book);
      const isBelowTarget = !isCritical && (Boolean(book.low_cushion_alert) || (book.status === "ACTIVE" && stockpiled < target));
      const fillCls = isCritical ? "is-danger" : isBelowTarget ? "is-warn" : "is-safe";
      const statusMeta = BOOK_STATUS_LABELS[book.status] || { text: String(book.status || "未知"), cls: "" };
      const trackLabel = PRIVATE_STUDIO_MODE
        ? (book.author_level === "lv2_plus" ? "长篇连载" : "紧凑完本")
        : (book.author_level === "lv2_plus" ? "Lv2+ 连载赛马" : "Lv0/1 完本养号");
      const isPaused = book.status === "PAUSED_AUTH_CAPTCHA"
        || book.status === "PAUSED_QUOTA_EXHAUSTED"
        || book.status === "PAUSED_MANUAL"
        || book.status === "ISOLATED_GATE_FAILURE"
        || book.status === "PAUSED";
      const cardCls = "book-card"
        + (!PRIVATE_STUDIO_MODE && isCritical ? " is-low-cushion" : "")
        + (book.status === "ISOLATED_GATE_FAILURE" ? " is-isolated" : "")
        + (book.status === "PAUSED_QUOTA_EXHAUSTED" ? " is-quota-exhausted" : "")
        + (book.status === "PAUSED_MANUAL" || book.status === "PAUSED" ? " is-paused-manual" : "");

      return (
        '<article class="' + escapeHtml(cardCls) + '" data-book-id="' + escapeHtml(bookId) + '">' +
          '<div class="book-card-head">' +
            '<div>' +
              '<h3 class="book-title">' + escapeHtml(title) + '</h3>' +
              '<div class="book-id mono">' + escapeHtml(bookId) + (!PRIVATE_STUDIO_MODE && book.account_id ? " · 马甲 " + escapeHtml(book.account_id) : "") + '</div>' +
            '</div>' +
            '<span class="tag ' + escapeHtml(statusMeta.cls) + '">' + escapeHtml(statusMeta.text) + '</span>' +
          '</div>' +
          '<div class="tag-row">' +
            '<span class="tag tag-gold">' + escapeHtml(book.genre_id || "未分类") + '</span>' +
            '<span class="tag tag-cyan">' + escapeHtml(trackLabel) + '</span>' +
            (PRIVATE_STUDIO_MODE ? "" : (isCritical ? '<span class="tag tag-danger">⚠️ 安全垫 &lt;6 抢产</span>' : isBelowTarget ? '<span class="tag tag-gold">安全垫 &lt;' + escapeHtml(target) + ' 补库中</span>' : "")) +
          '</div>' +
          (PRIVATE_STUDIO_MODE
            ? ""
            : ('<div class="cushion-wrap">' +
                '<div class="cushion-head">' +
                  '<span>囤稿安全垫水位</span>' +
                  '<span class="mono">' + escapeHtml(stockpiled) + " / " + escapeHtml(target) + ' 章</span>' +
                '</div>' +
                '<div class="cushion-track">' +
                  '<div class="cushion-fill ' + escapeHtml(fillCls) + '" style="width:' + escapeHtml(pct) + '%"></div>' +
                '</div>' +
              '</div>')) +
          '<div class="book-metrics">' +
            (PRIVATE_STUDIO_MODE ? "" : '<div class="metric-cell"><div class="metric-k">已发最高章</div><div class="metric-v mono">第 ' + escapeHtml(book.max_published_chapter || 0) + ' 章</div></div>') +
            '<div class="metric-cell"><div class="metric-k">总已产章数</div><div class="metric-v mono">' + escapeHtml(book.total_chapters || 0) + ' 章</div></div>' +
            '<div class="metric-cell"><div class="metric-k">日更目标</div><div class="metric-v mono">' + escapeHtml(book.daily_target_chapters || 2) + ' 章/日</div></div>' +
          '</div>' +
          (book.paused_reason ? '<div class="book-pause-note">说明：' + escapeHtml(book.paused_reason) + '</div>' : "") +
          '<div class="book-actions">' +
            '<button type="button" class="btn btn-xs btn-primary" data-book-action="read" data-book-id="' + escapeHtml(bookId) + '">📖 阅读/锁章</button>' +
            '<button type="button" class="btn btn-xs btn-outline" data-book-action="detail" data-book-id="' + escapeHtml(bookId) + '">🖼 封面/备选名</button>' +
            (PRIVATE_STUDIO_MODE ? "" : '<button type="button" class="btn btn-xs btn-ghost" data-book-action="promote" data-book-id="' + escapeHtml(bookId) + '">⚡ 晋级加更</button>') +
            '<button type="button" class="btn btn-xs btn-ghost" data-book-action="extend" data-book-id="' + escapeHtml(bookId) + '">🗺 续纲</button>' +
            (isPaused
              ? '<button type="button" class="btn btn-xs btn-outline" data-book-action="resume" data-book-id="' + escapeHtml(bookId) + '">▶ 恢复生产</button>'
              : '<button type="button" class="btn btn-xs btn-ghost" data-book-action="pause" data-book-id="' + escapeHtml(bookId) + '">⏸ 暂停</button>') +
            (PRIVATE_STUDIO_MODE ? "" : '<button type="button" class="btn btn-xs btn-danger" data-book-action="cut" data-book-id="' + escapeHtml(bookId) + '">✂ 切书</button>') +
          '</div>' +
        '</article>'
      );
    }).join("");

    grid.innerHTML = html;
  }

  async function handleBookAction(action, bookId) {
    if (!bookId) {
      return;
    }
    const book = state.books.find(function (b) {
      return b.book_id === bookId;
    });
    try {
      if (action === "read") {
        if (state.readerBookId !== bookId) {
          resetReaderChapterSelection();
          state.readerChapters = [];
        }
        state.readerBookId = bookId;
        switchView("reader");
        return;
      }
      if (action === "detail") {
        await openBookDetailModal(book);
        return;
      }
      if (action === "promote") {
        await apiRequest(formatApiRoute(API_ROUTES.BOOK_PROMOTE, { book_id: bookId }), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ book_id: bookId }),
        });
        showToast("已触发晋级加更：" + bookId, "ok");
        await loadBooks();
        return;
      }
      if (action === "extend") {
        await apiRequest(formatApiRoute(API_ROUTES.BOOK_EXTEND, { book_id: bookId }), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ book_id: bookId }),
        });
        showToast("已触发滚动续纲/换地图检查：" + bookId, "ok");
        await loadBooks();
        return;
      }
      if (action === "pause") {
        await apiRequest(formatApiRoute(API_ROUTES.BOOK_STATUS, { book_id: bookId }), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "pause", paused: true, reason: "控制台手动暂停" }),
        });
        showToast("已暂停书目生产：" + bookId, "ok");
        await loadBooks();
        return;
      }
      if (action === "resume") {
        await apiRequest(formatApiRoute(API_ROUTES.BOOK_RESUME, { book_id: bookId }), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ book_id: bookId }),
        });
        showToast("已恢复书目生产：" + bookId, "ok");
        await loadBooks();
        return;
      }
      if (action === "cut") {
        openCutModal(book || { book_id: bookId, title: bookId });
      }
    } catch (err) {
      showToast("操作失败：" + err.message, "err");
    }
  }

  function openCutModal(book) {
    state.cutTargetBookId = book.book_id;
    state.cutConfirmationVersion = 0;
    const summaryEl = document.getElementById("cut-book-summary");
    const stepBox = document.getElementById("cut-step-box");
    const confirmBtn = document.getElementById("btn-cut-confirm");
    if (summaryEl) {
      summaryEl.innerHTML =
        '<div>目标书籍：<strong>' + escapeHtml(book.title || book.book_id) + '</strong> (<span class="mono">' + escapeHtml(book.book_id) + '</span>)</div>' +
        '<div class="muted">状态：' + escapeHtml(book.status || "") + ' · 已囤稿 ' + escapeHtml(book.stockpiled_count || 0) + ' 章</div>';
    }
    if (stepBox) {
      stepBox.innerHTML = '<p class="muted">第一步：点击「1. 申请切书确认码」锁定版本号，再执行第二步最终切书。</p>';
    }
    if (confirmBtn) {
      confirmBtn.disabled = true;
    }
    openModal("cut-modal");
  }

  async function requestCutStep1() {
    const bookId = state.cutTargetBookId;
    const reasonEl = document.getElementById("cut-reason-input");
    const reason = reasonEl ? reasonEl.value.trim() : "";
    const stepBox = document.getElementById("cut-step-box");
    const confirmBtn = document.getElementById("btn-cut-confirm");
    try {
      const res = await apiRequest(formatApiRoute(API_ROUTES.BOOK_CUT, { book_id: bookId }), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reason: reason,
          confirm: false,
          confirmation_version: 0,
        }),
      });
      const ver = Number((res && (res.confirmation_version || res.version)) || 1);
      state.cutConfirmationVersion = ver;
      if (stepBox) {
        stepBox.innerHTML =
          '<div class="tag tag-gold">确认码已生成：v' + escapeHtml(ver) + '</div>' +
          '<p class="mt-sm">请核对书名无误后，点击「2. 🔥 确认止损切书」。</p>';
      }
      if (confirmBtn) {
        confirmBtn.disabled = false;
      }
    } catch (err) {
      showToast("申请切书确认码失败：" + err.message, "err");
    }
  }

  async function confirmCutStep2() {
    const bookId = state.cutTargetBookId;
    const reasonEl = document.getElementById("cut-reason-input");
    const reason = reasonEl ? reasonEl.value.trim() : "";
    try {
      await apiRequest(formatApiRoute(API_ROUTES.BOOK_CUT, { book_id: bookId }), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reason: reason,
          confirm: true,
          confirmation_version: state.cutConfirmationVersion,
        }),
      });
      closeModal("cut-modal");
      showToast("已确认止损切书：" + bookId, "ok");
      await loadBooks();
    } catch (err) {
      showToast("确认切书失败：" + err.message, "err");
    }
  }

  /* ------------------------------------------------------------------
   * Reusable Model Switch & Quota/Budget Alert Component (Q14 / P1-2)
   * Supports both "quota_exhausted" (critical halt) and "model_switched" (rotation notice)
   * ------------------------------------------------------------------ */
  function upsertModelQuotaAlert(entry) {
    if (!entry || typeof entry !== "object") {
      return;
    }
    const kind = entry.kind === MODEL_QUOTA_ALERT_KINDS.MODEL_SWITCHED
      ? MODEL_QUOTA_ALERT_KINDS.MODEL_SWITCHED
      : MODEL_QUOTA_ALERT_KINDS.QUOTA_EXHAUSTED;
    const bookId = String(entry.book_id || "");
    const alertId = String(entry.id || (kind + ":" + (bookId || "global")));
    state.dismissedQuotaAlertIds.delete(alertId);
    state.modelQuotaAlerts.set(alertId, {
      id: alertId,
      kind: kind,
      book_id: bookId,
      book_title: String(entry.book_title || ""),
      from_model: String(entry.from_model || ""),
      to_model: String(entry.to_model || ""),
      reason: String(entry.reason || ""),
      error_type: String(entry.error_type || ""),
      timestamp: String(entry.timestamp || ""),
      source: String(entry.source || "event"),
    });
    renderModelQuotaAlerts();
  }

  function dismissModelQuotaAlert(alertId) {
    if (!alertId) {
      return;
    }
    state.modelQuotaAlerts.delete(alertId);
    state.dismissedQuotaAlertIds.add(alertId);
    renderModelQuotaAlerts();
  }

  async function loadModelSwitches() {
    try {
      const payload = await apiRequest(API_ROUTES.MODEL_SWITCHES + "?limit=5", { method: "GET" });
      const events = (payload && Array.isArray(payload.events)) ? payload.events : [];
      events.slice(0, 3).forEach(function (ev) {
        if (!ev || typeof ev !== "object") {
          return;
        }
        const alertId = MODEL_QUOTA_ALERT_KINDS.MODEL_SWITCHED + ":" + String(ev.id || "");
        if (state.dismissedQuotaAlertIds.has(alertId)) {
          return;
        }
        state.modelQuotaAlerts.set(alertId, {
          id: alertId,
          kind: MODEL_QUOTA_ALERT_KINDS.MODEL_SWITCHED,
          book_id: String(ev.book_id || ""),
          book_title: String(ev.book_title || ev.book_id || ""),
          from_model: String(ev.from_model || ""),
          to_model: String(ev.to_model || ""),
          reason: String(ev.reason || ""),
          error_type: "",
          timestamp: String(ev.created_at || ""),
          source: "db_event",
        });
      });
      renderModelQuotaAlerts();
    } catch (err) {
      if (!state.authRequired) {
        showToast("加载模型切换记录失败：" + err.message, "err");
      }
    }
  }

  function syncQuotaAlertsFromBooks() {
    const activeQuotaIds = new Set();
    state.books.forEach(function (book) {
      if (book.status !== "PAUSED_QUOTA_EXHAUSTED") {
        return;
      }
      const alertId = MODEL_QUOTA_ALERT_KINDS.QUOTA_EXHAUSTED + ":" + String(book.book_id || "");
      activeQuotaIds.add(alertId);
      if (state.dismissedQuotaAlertIds.has(alertId)) {
        return;
      }
      state.modelQuotaAlerts.set(alertId, {
        id: alertId,
        kind: MODEL_QUOTA_ALERT_KINDS.QUOTA_EXHAUSTED,
        book_id: String(book.book_id || ""),
        book_title: String(book.title || book.book_id || ""),
        from_model: "",
        to_model: "",
        reason: String(book.paused_reason || "全部额度用尽或触发预算熔断，已停机保护"),
        error_type: "",
        timestamp: String(book.updated_at || ""),
        source: "book_status",
      });
    });
    const booksById = new Map();
    state.books.forEach(function (b) {
      booksById.set(String(b.book_id || ""), b);
    });
    Array.from(state.modelQuotaAlerts.entries()).forEach(function (pair) {
      const id = pair[0];
      const item = pair[1];
      if (item.kind !== MODEL_QUOTA_ALERT_KINDS.QUOTA_EXHAUSTED) {
        return;
      }
      const matched = item.book_id ? booksById.get(item.book_id) : null;
      const recovered = Boolean(matched) && matched.status !== "PAUSED_QUOTA_EXHAUSTED";
      if ((item.source === "book_status" && !activeQuotaIds.has(id)) || recovered) {
        state.modelQuotaAlerts.delete(id);
        state.dismissedQuotaAlertIds.delete(id);
      }
    });
    renderModelQuotaAlerts();
  }

  function renderModelQuotaAlerts() {
    const region = document.getElementById("model-quota-alert-region");
    if (!region) {
      return;
    }
    const alerts = Array.from(state.modelQuotaAlerts.values());
    if (alerts.length === 0) {
      region.hidden = true;
      region.innerHTML = "";
      return;
    }
    region.hidden = false;
    region.innerHTML = alerts.map(function (item) {
      const isSwitch = item.kind === MODEL_QUOTA_ALERT_KINDS.MODEL_SWITCHED;
      const cls = isSwitch ? "model-alert-card is-warn" : "model-alert-card is-danger";
      const ariaRole = isSwitch ? "status" : "alert";
      const ariaLive = isSwitch ? "polite" : "assertive";
      const heading = isSwitch ? "🔄 写作模型已切换" : "🚨 超预算 / 全部额度用尽 · 熔断停机";
      const bookLabel = item.book_title || item.book_id;
      const modelStep = (item.from_model || item.to_model)
        ? ('<span class="tag tag-gold">从 ' + escapeHtml(item.from_model || "原模型") + ' 切到 ' + escapeHtml(item.to_model || "目标模型") + '</span>')
        : "";
      const detailParts = [];
      if (bookLabel) {
        detailParts.push("书目：" + bookLabel);
      }
      if (item.reason) {
        detailParts.push("原因：" + item.reason);
      }
      const detailText = detailParts.join(" · ") || (isSwitch ? "已按白名单顺序切换写作模型" : "触发超预算或额度熔断，自动写作已停止");
      const hintText = isSwitch
        ? "已记录本次切换原因与对应章节所用模型，可继续观察写作进度。"
        : "严禁悄悄降级：请先前往 CPA 配置页检查额度/预算，处理完成后再点击恢复写作。";
      const resumeBtn = (!isSwitch && item.book_id)
        ? ('<button type="button" class="btn btn-sm btn-primary" data-alert-action="resume-book" data-book-id="' + escapeHtml(item.book_id) + '" data-alert-id="' + escapeHtml(item.id) + '">▶ 恢复写作</button>')
        : "";
      return (
        '<div class="' + escapeHtml(cls) + '" role="' + escapeHtml(ariaRole) + '" aria-live="' + escapeHtml(ariaLive) + '" data-alert-kind="' + escapeHtml(item.kind) + '" data-alert-id="' + escapeHtml(item.id) + '">' +
          '<div class="model-alert-main">' +
            '<div class="model-alert-title">' +
              '<span>' + escapeHtml(heading) + '</span>' +
              modelStep +
            '</div>' +
            '<div class="model-alert-detail">' + escapeHtml(detailText) + '</div>' +
            '<div class="model-alert-hint">' + escapeHtml(hintText) + '</div>' +
          '</div>' +
          '<div class="model-alert-actions">' +
            '<button type="button" class="btn btn-sm btn-outline" data-alert-action="goto-cpa">前往 CPA 配置</button>' +
            resumeBtn +
            '<button type="button" class="btn btn-sm btn-ghost" data-alert-action="dismiss" data-alert-id="' + escapeHtml(item.id) + '">' + escapeHtml(isSwitch ? "知道了" : "暂时收起") + '</button>' +
          '</div>' +
        '</div>'
      );
    }).join("");
  }

  /* ------------------------------------------------------------------
   * Captcha Banner & Companion Resume
   * ------------------------------------------------------------------ */
  function renderCaptchaBanner() {
    const banner = document.getElementById("captcha-global-banner");
    const detailsEl = document.getElementById("captcha-banner-details");
    const actionsEl = document.getElementById("captcha-banner-actions");
    const thumbEl = document.getElementById("captcha-banner-thumb");
    if (!banner || !detailsEl || !actionsEl || !thumbEl) {
      return;
    }
    if (PRIVATE_STUDIO_MODE) {
      banner.hidden = true;
      detailsEl.innerHTML = "";
      actionsEl.innerHTML = "";
      thumbEl.innerHTML = "";
      thumbEl.hidden = true;
      return;
    }
    const alerts = Array.from(state.captchaAlerts.values());
    if (alerts.length === 0) {
      banner.hidden = true;
      detailsEl.innerHTML = "";
      actionsEl.innerHTML = "";
      thumbEl.innerHTML = "";
      thumbEl.hidden = true;
      return;
    }

    const first = alerts[0];
    banner.hidden = false;
    detailsEl.innerHTML = alerts.map(function (item) {
      return (
        '<div>马甲 <strong class="mono">' + escapeHtml(item.account_id || "未知") + '</strong>' +
        (item.book_id ? ' · 书籍 <span class="mono">' + escapeHtml(item.book_id) + '</span>' : "") +
        ' · 原因：' + escapeHtml(item.reason || "CAPTCHA_DETECTED") +
        (item.page_url ? ' · 页面：<code>' + escapeHtml(item.page_url) + '</code>' : "") +
        '</div>'
      );
    }).join("");

    actionsEl.innerHTML = alerts.map(function (item) {
      return (
        '<button type="button" class="btn btn-sm btn-primary" data-resume-account="' + escapeHtml(item.account_id || "") + '" data-resume-book="' + escapeHtml(item.book_id || "") + '">' +
          '✅ 已在 Chrome 手动过码，恢复 ' + escapeHtml(item.account_id || item.book_id || "伴侣") +
        '</button>'
      );
    }).join("");

    const allowedMimes = ["image/png", "image/jpeg", "image/webp"];
    const mime = allowedMimes.indexOf(first.screenshot_mime) !== -1 ? first.screenshot_mime : "image/png";
    const cleanB64 = String(first.screenshot_base64 || "").replace(/[^A-Za-z0-9+/=]/g, "");
    if (cleanB64) {
      thumbEl.hidden = false;
      thumbEl.innerHTML = '<img src="data:' + escapeHtml(mime) + ';base64,' + escapeHtml(cleanB64) + '" alt="验证码挂起截图">';
    } else {
      thumbEl.hidden = true;
      thumbEl.innerHTML = "";
    }
  }

  async function resumeCompanion(accountId, bookId) {
    try {
      await apiRequest(API_ROUTES.COMPANION_RESUME, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id: accountId || "default",
          book_id: bookId || "",
          note: "Web 控制台确认已手动过码恢复",
        }),
      });
      state.captchaAlerts.delete(accountId || bookId);
      renderCaptchaBanner();
      showToast("已下发伴侣恢复运行指令", "ok");
      await loadBooks();
    } catch (err) {
      showToast("恢复伴侣运行失败：" + err.message, "err");
    }
  }

  /* ------------------------------------------------------------------
   * Cover Blob Fetch & Book Detail Modal
   * ------------------------------------------------------------------ */
  async function fetchAuthenticatedImageUrl(url) {
    const resp = await fetch(url, { headers: buildAuthHeaders() });
    if (resp.status === 401 || resp.status === 403) {
      state.authRequired = true;
      updateAuthUi("加载封面需要管理员 Bearer Token");
      throw new Error("未授权加载封面");
    }
    if (!resp.ok) {
      throw new Error("封面尚未生成或不可用 (HTTP " + resp.status + ")");
    }
    const blob = await resp.blob();
    const objUrl = URL.createObjectURL(blob);
    state.coverObjectUrls.push(objUrl);
    if (state.coverObjectUrls.length > 12) {
      const oldest = state.coverObjectUrls.shift();
      if (oldest) {
        URL.revokeObjectURL(oldest);
      }
    }
    return objUrl;
  }

  function renderCoverActionState(coverContainer, book, statusMessage, buttonLabel, titlesContainer) {
    if (!coverContainer || !book) {
      return;
    }
    coverContainer.innerHTML =
      '<div class="cover-placeholder">' +
        '<div class="cover-placeholder-msg">' + escapeHtml(statusMessage) + '</div>' +
        '<button type="button" class="btn btn-xs btn-primary mt-sm" data-gen-cover-book="' + escapeHtml(book.book_id) + '">' +
          escapeHtml(buttonLabel) +
        '</button>' +
      '</div>';
    const genBtn = coverContainer.querySelector("[data-gen-cover-book]");
    if (genBtn) {
      genBtn.addEventListener("click", function () {
        generateAiCoverForBook(book, coverContainer, titlesContainer);
      });
    }
  }

  async function generateAiCoverForBook(book, coverContainer, titlesContainer) {
    if (!book || !book.book_id) {
      return false;
    }
    if (coverContainer) {
      coverContainer.innerHTML = '<div class="cover-placeholder">正在生成 600×800 AI 封面…</div>';
    }
    try {
      const coverRes = await apiRequest(
        formatApiRoute(API_ROUTES.BOOK_COVER, { book_id: book.book_id }),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ base_mode: "ai_image" }),
        }
      );
      const generatedPath = (coverRes && (coverRes.output_path || coverRes.cover_path)) || "generated";
      book.cover_path = generatedPath;
      const inState = state.books.find(function (b) {
        return b.book_id === book.book_id;
      });
      if (inState) {
        inState.cover_path = generatedPath;
      }
      await renderBookPreviewInto(book, coverContainer, titlesContainer);
      return true;
    } catch (err) {
      if (coverContainer) {
        const hint = "请先在 CPA 面板配置 image 生图模型后再生成封面（" + (err && err.message ? err.message : "生图失败") + "）";
        renderCoverActionState(coverContainer, book, hint, "🎨 重试生成 AI 封面", titlesContainer);
      }
      return false;
    }
  }

  async function renderBookPreviewInto(book, coverContainer, titlesContainer) {
    if (!book) {
      return;
    }
    const titles = Array.isArray(book.alternate_titles) ? book.alternate_titles : [];
    if (titlesContainer) {
      titlesContainer.innerHTML =
        '<div><strong>主书名：</strong>' + escapeHtml(book.title || book.book_id) + '</div>' +
        '<div class="muted mt-sm">赛道：' + escapeHtml(book.genre_id || "-") + ' · 轨道：' + escapeHtml(book.contract_track || "-") + '</div>' +
        '<div class="mt-sm"><strong>备选爆款书名池（≤15字）：</strong></div>' +
        (titles.length > 0
          ? '<ul class="alt-title-list">' + titles.map(function (t, idx) {
              return '<li class="alt-title-item">' + escapeHtml((idx + 1) + ". " + t) + '</li>';
            }).join("") + '</ul>'
          : '<div class="muted mt-sm">暂无备选书名列表</div>');
    }

    if (coverContainer) {
      if (!book.cover_path) {
        renderCoverActionState(coverContainer, book, "尚未生成封面", "🎨 生成 AI 封面", titlesContainer);
        return;
      }
      coverContainer.innerHTML = '<div class="cover-placeholder">正在加载 600×800 封面…</div>';
      try {
        const coverUrl = formatApiRoute(API_ROUTES.BOOK_COVER, { book_id: book.book_id });
        const blobUrl = await fetchAuthenticatedImageUrl(coverUrl);
        coverContainer.innerHTML = '<img src="' + escapeHtml(blobUrl) + '" alt="' + escapeHtml(book.title || "书籍封面") + '">';
      } catch (err) {
        renderCoverActionState(
          coverContainer,
          book,
          err && err.message ? err.message : "封面加载失败",
          "🎨 重试生成 AI 封面",
          titlesContainer
        );
      }
    }
  }

  async function openBookDetailModal(book) {
    const body = document.getElementById("detail-modal-body");
    if (!body || !book) {
      return;
    }
    body.innerHTML =
      '<div class="created-preview-body">' +
        '<div class="cover-box" id="modal-cover-box"></div>' +
        '<div id="modal-alt-titles-box"></div>' +
      '</div>';
    openModal("book-detail-modal");
    await renderBookPreviewInto(
      book,
      document.getElementById("modal-cover-box"),
      document.getElementById("modal-alt-titles-box")
    );
  }

  /* ------------------------------------------------------------------
   * View 2: Genres & Three-Source Auto-Create
   * ------------------------------------------------------------------ */
  async function loadGenres() {
    try {
      const payload = await apiRequest(API_ROUTES.GENRES, { method: "GET" });
      state.genres = Array.isArray(payload) ? payload : (payload && Array.isArray(payload.genres) ? payload.genres : []);
      if (!state.selectedGenreId && state.genres.length > 0) {
        state.selectedGenreId = state.genres[0].genre_id;
      }
      renderGenreGrid();
      renderSelectedGenreDetail();
    } catch (err) {
      showToast("加载爆款类目失败：" + err.message, "err");
    }
  }

  function renderGenreGrid() {
    const grid = document.getElementById("genre-selector-grid");
    const hiddenInput = document.getElementById("create-genre-id");
    if (!grid) {
      return;
    }
    const items = state.genres.filter(function (g) {
      return state.genreChannelFilter === "all" || g.channel === state.genreChannelFilter;
    });
    if (items.length === 0) {
      grid.innerHTML = '<div class="empty-state">暂无匹配频道的爆款类目</div>';
      return;
    }
    grid.innerHTML = items.map(function (g) {
      const chName = g.channel === "male" ? "男频" : g.channel === "female" ? "女频" : "通用";
      const isSelected = g.genre_id === state.selectedGenreId;
      return (
        '<button type="button" class="genre-card' + (isSelected ? " is-selected" : "") + '" data-genre-id="' + escapeHtml(g.genre_id) + '">' +
          '<div class="genre-card-name">' + escapeHtml(g.display_name || g.genre_id) + '</div>' +
          '<div class="genre-card-sub">' + escapeHtml(chName + " · " + (g.pov_mode === "first_person" ? "第一人称" : "第三人称")) + '</div>' +
        '</button>'
      );
    }).join("");

    if (hiddenInput) {
      hiddenInput.value = state.selectedGenreId;
    }
  }

  function renderSelectedGenreDetail() {
    const body = document.getElementById("genre-detail-body");
    const tag = document.getElementById("preview-genre-tag");
    if (!body) {
      return;
    }
    const genre = state.genres.find(function (g) {
      return g.genre_id === state.selectedGenreId;
    });
    if (!genre) {
      if (tag) {
        tag.textContent = "未选择类目";
      }
      body.innerHTML = '<p class="muted">请在左侧选择类目。</p>';
      return;
    }
    if (tag) {
      tag.textContent = genre.display_name || genre.genre_id;
    }
    const wordRange = Array.isArray(genre.golden_word_count_range)
      ? genre.golden_word_count_range[0] + "～" + genre.golden_word_count_range[1] + " 字"
      : "2050～2300 字";
    const card = genre.common_sense_card || {};
    const rules = Array.isArray(card.rules) ? card.rules : [];
    const beats = Array.isArray(genre.golden_three_beats) ? genre.golden_three_beats : [];

    body.innerHTML =
      '<div class="tag-row">' +
        '<span class="tag tag-gold">主分类：' + escapeHtml(genre.tomato_primary_category || "-") + '</span>' +
        '<span class="tag tag-cyan">黄金字数：' + escapeHtml(wordRange) + '</span>' +
        '<span class="tag">' + escapeHtml(genre.panel_policy === "allow_system_tag" ? "允许【系统面板】" : "禁用系统面板") + '</span>' +
      '</div>' +
      '<div class="recipe-block">' +
        '<h4>首屏 300 字铁律</h4>' +
        '<div>' + escapeHtml(genre.opening_300w_directive || "-") + '</div>' +
      '</div>' +
      '<div class="recipe-block">' +
        '<h4>黄金三章节拍器</h4>' +
        '<ul>' + beats.map(function (b) {
          return '<li><strong>第' + escapeHtml(b.chapter) + '章（' + escapeHtml(b.role) + '）：</strong>' + escapeHtml(b.payoff_or_twist) + '</li>';
        }).join("") + '</ul>' +
      '</div>' +
      '<div class="recipe-block">' +
        '<h4>题材硬常识防穿帮卡（' + escapeHtml(card.domain || "-") + '）</h4>' +
        '<ul>' + rules.map(function (r) {
          return '<li>' + escapeHtml(r) + '</li>';
        }).join("") + '</ul>' +
      '</div>';
  }

  async function handleAutoCreateSubmit(evt) {
    evt.preventDefault();
    const projectId = (document.getElementById("create-project-id").value || "").trim();
    const genreId = (document.getElementById("create-genre-id").value || "").trim();
    const penName = (document.getElementById("create-pen-name").value || "").trim() || "砚山行";
    const accountId = (document.getElementById("create-account-id").value || "").trim();
    const rawTarget = (document.getElementById("create-target-chapters").value || "").trim();
    const rawKeywords = (document.getElementById("create-hot-keywords").value || "").trim();
    const rawBench = (document.getElementById("create-benchmark-text").value || "").trim();
    const levelRadio = document.querySelector('input[name="author_level"]:checked');
    const authorLevel = levelRadio ? levelRadio.value : "lv0_1";

    if (!projectId) {
      showToast("请填写 project_id", "err");
      return;
    }
    if (!genreId) {
      showToast("请先选择一个爆款赛道类目", "err");
      return;
    }

    const hotKeywords = rawKeywords
      ? rawKeywords.split(/[,，\n]+/).map(function (x) { return x.trim(); }).filter(Boolean)
      : [];
    const benchmarkChapters = rawBench
      ? rawBench.split(/\n\s*===\s*\n/).map(function (x) { return x.trim(); }).filter(Boolean)
      : [];

    const reqBody = {
      project_id: projectId,
      genre_id: genreId,
      author_level: authorLevel,
      account_id: accountId,
      pen_name: penName,
      hot_keywords: hotKeywords,
      benchmark_chapters: benchmarkChapters,
      target_chapters: rawTarget ? parseInt(rawTarget, 10) : null,
    };

    const submitBtn = document.getElementById("btn-submit-create");
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "正在三源策划与落盘校验…";
    }
    try {
      const created = await apiRequest(API_ROUTES.BOOKS_AUTO_CREATE, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(reqBody),
      });
      showToast("立项成功：" + ((created && created.title) || projectId), "ok");
      await loadBooks();
      const createdBook = (created && created.book_id ? created : null) || state.books.find(function (b) {
        return b.book_id === projectId;
      });
      if (createdBook) {
        const coverBox = document.getElementById("cover-preview-box");
        const titlesBox = document.getElementById("alt-titles-box");
        if (!createdBook.cover_path) {
          await renderBookPreviewInto(createdBook, null, titlesBox);
          await generateAiCoverForBook(createdBook, coverBox, titlesBox);
        } else {
          await renderBookPreviewInto(createdBook, coverBox, titlesBox);
        }
      }
    } catch (err) {
      showToast("自动立项失败：" + err.message, "err");
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = "🚀 一键三源自动立项";
      }
    }
  }

  /* ------------------------------------------------------------------
   * View 3: CPA Account Pool & Five-Role Bindings
   * ------------------------------------------------------------------ */
  async function loadCpaPanel() {
    await Promise.all([
      loadCpaAuthFiles(),
      loadCpaUsage(),
      loadCpaModelsAndBindings(),
    ]);
  }

  async function loadCpaAuthFiles() {
    const listEl = document.getElementById("cpa-auth-list");
    try {
      const payload = await apiRequest(API_ROUTES.CPA_AUTH_FILES, { method: "GET" });
      const files = Array.isArray(payload)
        ? payload
        : (payload && (payload.files || payload.auth_files || payload.data)) || [];
      state.cpaAuthFiles = files;
      renderCpaAuthFiles();
    } catch (err) {
      if (listEl) {
        listEl.innerHTML = '<div class="empty-state">读取 CPA 凭据失败：' + escapeHtml(err.message) + '</div>';
      }
    }
  }

  function renderCpaAuthFiles() {
    const listEl = document.getElementById("cpa-auth-list");
    if (!listEl) {
      return;
    }
    if (!state.cpaAuthFiles || state.cpaAuthFiles.length === 0) {
      listEl.innerHTML = '<div class="empty-state">暂无已注册凭据，请拖拽上传 claude-*.json / codex-*.json / gemini-*.json</div>';
      return;
    }
    listEl.innerHTML = state.cpaAuthFiles.map(function (f) {
      const name = String(f.name || f.filename || f.id || "");
      const provider = String(f.provider || f.type || "oauth");
      const status = String(f.status || (f.cooldown ? "429 冷却中" : "正常"));
      const isBad = Boolean(f.cooldown) || /429|error|expired|disabled/i.test(status);
      return (
        '<div class="auth-file-row">' +
          '<div>' +
            '<div class="mono"><strong>' + escapeHtml(name) + '</strong></div>' +
            '<div class="tag-row mt-sm">' +
              '<span class="tag tag-cyan">' + escapeHtml(provider) + '</span>' +
              '<span class="tag ' + (isBad ? "tag-danger" : "tag-emerald") + '">' + escapeHtml(status) + '</span>' +
            '</div>' +
          '</div>' +
          '<button type="button" class="btn btn-xs btn-danger" data-delete-auth="' + escapeHtml(name) + '">删除</button>' +
        '</div>'
      );
    }).join("");
  }

  async function uploadCpaFiles(fileList) {
    const files = Array.from(fileList || []);
    for (let i = 0; i < files.length; i += 1) {
      const file = files[i];
      if (!file.name.toLowerCase().endsWith(".json")) {
        showToast("跳过非 JSON 文件：" + file.name, "err");
        continue;
      }
      try {
        const text = await file.text();
        JSON.parse(text);
        const formData = new FormData();
        formData.append("file", file, file.name);
        await apiRequest(API_ROUTES.CPA_AUTH_FILES, {
          method: "POST",
          body: formData,
        });
        showToast("已热加载凭据：" + file.name, "ok");
      } catch (err) {
        showToast("上传 " + file.name + " 失败：" + err.message, "err");
      }
    }
    await loadCpaAuthFiles();
  }

  async function deleteCpaAuthFile(name) {
    if (!name) {
      return;
    }
    try {
      await apiRequest(formatApiRoute(API_ROUTES.CPA_AUTH_FILE_DETAIL, { name: name }), {
        method: "DELETE",
      });
      showToast("已移除凭据：" + name, "ok");
      await loadCpaAuthFiles();
    } catch (err) {
      showToast("删除凭据失败：" + err.message, "err");
    }
  }

  async function fetchOAuthUrl() {
    const providerEl = document.getElementById("cpa-oauth-provider");
    const projectEl = document.getElementById("cpa-oauth-project");
    const resultEl = document.getElementById("cpa-oauth-result");
    const provider = providerEl ? providerEl.value : "claude";
    const projectId = projectEl ? projectEl.value.trim() : "";
    const qs = "?provider=" + encodeURIComponent(provider) + (projectId ? "&project_id=" + encodeURIComponent(projectId) : "");
    try {
      const res = await apiRequest(API_ROUTES.CPA_OAUTH + qs, { method: "GET" });
      const rawUrl = String((res && (res.url || res.auth_url)) || "");
      let safeHref = "";
      if (rawUrl) {
        try {
          const parsed = new URL(rawUrl);
          if (parsed.protocol === "https:" || (parsed.protocol === "http:" && (parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost"))) {
            safeHref = parsed.toString();
          }
        } catch (urlErr) {
          console.warn("Invalid OAuth URL:", urlErr);
        }
      }
      if (resultEl) {
        resultEl.hidden = false;
        if (safeHref) {
          resultEl.innerHTML = '授权链接：<a href="' + escapeHtml(safeHref) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(safeHref) + '</a>';
        } else {
          resultEl.textContent = JSON.stringify(res);
        }
      }
    } catch (err) {
      showToast("获取 OAuth 链接失败：" + err.message, "err");
    }
  }

  async function loadCpaUsage() {
    const strip = document.getElementById("cpa-usage-summary");
    const kpiTok = document.getElementById("kpi-today-tokens");
    const kpiCost = document.getElementById("kpi-today-cost");
    try {
      const usage = (await apiRequest(API_ROUTES.CPA_USAGE, { method: "GET" })) || {};
      state.cpaUsage = usage;
      const ledger = usage.ledger || {};
      const cpa = usage.cpa_usage || {};
      const ledgerTokens = Number(ledger.input_tokens || 0) + Number(ledger.output_tokens || 0);
      const cpaTokens = Number(cpa.input_tokens || 0) + Number(cpa.output_tokens || 0);
      const topTokens = Number(usage.input_tokens || 0) + Number(usage.output_tokens || 0);
      const reqs = Number(
        ledger.request_count ||
        ledger.requests_today ||
        ledger.total_requests ||
        ledger.requests ||
        cpa.request_count ||
        cpa.requests_today ||
        cpa.total_requests ||
        cpa.requests ||
        usage.request_count ||
        usage.requests_today ||
        usage.total_requests ||
        usage.requests ||
        0
      );
      const tokens = Number(
        ledger.total_tokens ||
        ledgerTokens ||
        cpa.total_tokens ||
        cpaTokens ||
        usage.total_tokens ||
        topTokens ||
        0
      );
      const cost = Number(
        ledger.estimated_cost_usd ||
        ledger.total_cost_usd ||
        cpa.estimated_cost_usd ||
        cpa.total_cost_usd ||
        usage.estimated_cost_usd ||
        usage.total_cost_usd ||
        0
      );
      if (strip) {
        strip.innerHTML =
          '<span>总请求：<strong class="mono">' + escapeHtml(reqs) + '</strong></span> · ' +
          '<span>总 Token：<strong class="mono">' + escapeHtml(tokens) + '</strong></span> · ' +
          '<span>折算估算：<strong class="mono">$' + escapeHtml(cost.toFixed(4)) + '</strong></span>';
      }
      if (kpiTok) {
        kpiTok.textContent = reqs + " / " + tokens;
      }
      if (kpiCost) {
        kpiCost.textContent = "今日折算估算 $" + cost.toFixed(4);
      }
    } catch (err) {
      if (strip) {
        strip.textContent = "读取 CPA 用量统计失败：" + err.message;
      }
    }
  }

  async function loadCpaModelsAndBindings() {
    try {
      const results = await Promise.allSettled([
        apiRequest(API_ROUTES.CPA_MODELS, { method: "GET" }),
        apiRequest(API_ROUTES.CPA_BINDINGS, { method: "GET" }),
      ]);
      if (results[0].status === "fulfilled") {
        const mp = results[0].value;
        const list = Array.isArray(mp) ? mp : (mp && (mp.models || mp.data)) || [];
        state.cpaModels = list.map(function (m) {
          return typeof m === "string" ? m : String(m.id || m.model || "");
        }).filter(Boolean);
      }
      if (results[1].status === "fulfilled") {
        const bp = results[1].value;
        const rawBindings = (bp && bp.bindings) ? bp.bindings : bp;
        const map = {};
        if (Array.isArray(rawBindings)) {
          rawBindings.forEach(function (item) {
            if (item && item.role) {
              map[item.role] = item;
            }
          });
        } else if (rawBindings && typeof rawBindings === "object") {
          Object.keys(rawBindings).forEach(function (k) {
            map[k] = rawBindings[k];
          });
        }
        state.cpaBindings = map;
      }
      renderBindingsForm();
    } catch (err) {
      showToast("加载模型路由配置失败：" + err.message, "err");
    }
  }

  function renderBindingsForm() {
    const form = document.getElementById("cpa-bindings-form");
    if (!form) {
      return;
    }
    const modelOptions = state.cpaModels;
    form.innerHTML = CPA_ROLES.map(function (item) {
      const cfg = state.cpaBindings[item.role] || {};
      const currentModel = String(cfg.model || "");
      const currentUrl = String(cfg.base_url || "");
      const currentProvider = String(cfg.provider_type || (item.role === "image" ? "gemini_native" : "openai"));
      const currentEffort = String(cfg.reasoning_effort || "");

      const optionsHtml = ['<option value="">-- 选择探测到的模型或下方手填 --</option>'].concat(
        modelOptions.map(function (m) {
          return '<option value="' + escapeHtml(m) + '"' + (m === currentModel ? " selected" : "") + '>' + escapeHtml(m) + '</option>';
        })
      ).join("");

      return (
        '<div class="binding-row" data-role="' + escapeHtml(item.role) + '">' +
          '<div class="binding-head">' +
            '<strong>' + escapeHtml(item.label) + '</strong>' +
            '<span class="tag">' + escapeHtml(item.role) + '</span>' +
          '</div>' +
          '<div class="binding-fields">' +
            '<label class="field">' +
              '<span class="field-label">从 /v1/models 选择</span>' +
              '<select class="bind-model-select">' + optionsHtml + '</select>' +
            '</label>' +
            '<label class="field">' +
              '<span class="field-label">生效模型名 (含推理后缀)</span>' +
              '<input type="text" class="bind-model-input" value="' + escapeHtml(currentModel) + '" placeholder="如 claude-sonnet-4-5(high)">' +
            '</label>' +
            '<label class="field">' +
              '<span class="field-label">CPA Base URL</span>' +
              '<input type="text" class="bind-url-input" value="' + escapeHtml(currentUrl) + '" placeholder="例如本机 127.0.0.1:8317/v1">' +
            '</label>' +
            '<label class="field">' +
              '<span class="field-label">协议类型与推理强度</span>' +
              '<div class="input-group">' +
                '<select class="bind-provider-select">' +
                  '<option value="openai"' + (currentProvider === "openai" ? " selected" : "") + '>openai</option>' +
                  '<option value="anthropic"' + (currentProvider === "anthropic" ? " selected" : "") + '>anthropic</option>' +
                  '<option value="gemini_native"' + (currentProvider === "gemini_native" ? " selected" : "") + '>gemini_native</option>' +
                  '<option value="openai_images"' + (currentProvider === "openai_images" ? " selected" : "") + '>openai_images</option>' +
                  '<option value="auto"' + (currentProvider === "auto" ? " selected" : "") + '>auto</option>' +
                '</select>' +
                '<select class="bind-effort-select">' +
                  '<option value=""' + (currentEffort === "" ? " selected" : "") + '>默认</option>' +
                  '<option value="low"' + (currentEffort === "low" ? " selected" : "") + '>low</option>' +
                  '<option value="medium"' + (currentEffort === "medium" ? " selected" : "") + '>medium</option>' +
                  '<option value="high"' + (currentEffort === "high" ? " selected" : "") + '>high</option>' +
                '</select>' +
              '</div>' +
            '</label>' +
          '</div>' +
        '</div>'
      );
    }).join("");

    form.querySelectorAll(".binding-row").forEach(function (row) {
      const sel = row.querySelector(".bind-model-select");
      const inp = row.querySelector(".bind-model-input");
      if (sel && inp) {
        sel.addEventListener("change", function () {
          if (sel.value) {
            inp.value = sel.value;
          }
        });
      }
    });
  }

  async function saveCpaBindings() {
    const form = document.getElementById("cpa-bindings-form");
    if (!form) {
      return;
    }
    const bindings = [];
    form.querySelectorAll(".binding-row").forEach(function (row) {
      const role = row.getAttribute("data-role") || "";
      const model = (row.querySelector(".bind-model-input").value || "").trim();
      const baseUrl = (row.querySelector(".bind-url-input").value || "").trim();
      const providerType = row.querySelector(".bind-provider-select").value;
      const effort = row.querySelector(".bind-effort-select").value || null;
      if (role && model && baseUrl) {
        bindings.push({
          role: role,
          model: model,
          base_url: baseUrl,
          provider_type: providerType,
          reasoning_effort: effort,
        });
      }
    });

    try {
      await apiRequest(API_ROUTES.CPA_BINDINGS, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bindings: bindings }),
      });
      showToast("已保存五角色模型绑定（" + bindings.length + " 项）", "ok");
      await loadCpaModelsAndBindings();
    } catch (err) {
      showToast("保存模型绑定失败：" + err.message, "err");
    }
  }

  /* ------------------------------------------------------------------
   * View 4: fetch() + ReadableStream SSE Client (ALL_SSE_EVENTS)
   * ------------------------------------------------------------------ */
  function updateSseStatusUi() {
    const pill = document.getElementById("sse-pill");
    const pillText = document.getElementById("sse-pill-text");
    const toggleBtn = document.getElementById("btn-toggle-sse");
    const navDot = document.getElementById("nav-sse-dot");
    if (pill) {
      pill.classList.toggle("is-live", state.sseConnected);
      pill.classList.toggle("is-connecting", state.sseConnecting && !state.sseConnected);
    }
    if (pillText) {
      pillText.textContent = state.sseConnected ? "直播已连接" : state.sseConnecting ? "正在重连…" : "直播未连接";
    }
    if (toggleBtn) {
      toggleBtn.textContent = (state.sseConnected || state.sseConnecting) ? "断开直播" : "连接直播";
    }
    if (navDot) {
      navDot.classList.toggle("is-online", state.sseConnected);
    }
  }

  function disconnectSse() {
    if (state.sseReconnectTimer) {
      window.clearTimeout(state.sseReconnectTimer);
      state.sseReconnectTimer = null;
    }
    if (state.sseAbortController) {
      state.sseAbortController.abort();
      state.sseAbortController = null;
    }
    state.sseConnected = false;
    state.sseConnecting = false;
    updateSseStatusUi();
  }

  async function connectSse() {
    disconnectSse();
    const controller = new AbortController();
    state.sseAbortController = controller;
    state.sseConnecting = true;
    updateSseStatusUi();

    try {
      const resp = await fetch(API_ROUTES.EVENTS_STREAM, {
        method: "GET",
        headers: buildAuthHeaders({ Accept: "text/event-stream" }),
        signal: controller.signal,
      });

      if (resp.status === 401 || resp.status === 403) {
        state.authRequired = true;
        updateAuthUi("SSE 直播需要有效管理员 Bearer Token");
        disconnectSse();
        return;
      }
      if (!resp.ok || !resp.body) {
        throw new Error("SSE 连接失败 HTTP " + resp.status);
      }

      state.sseConnected = true;
      state.sseConnecting = false;
      state.sseReconnectAttempts = 0;
      updateSseStatusUi();

      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let eventName = "message";
      let dataLines = [];

      while (true) {
        const chunk = await reader.read();
        if (chunk.done) {
          break;
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() || "";

        for (let i = 0; i < lines.length; i += 1) {
          const line = lines[i];
          if (line === "") {
            if (dataLines.length > 0) {
              dispatchSseFrame(eventName, dataLines.join("\n"));
            }
            eventName = "message";
            dataLines = [];
            continue;
          }
          if (line.charAt(0) === ":") {
            continue;
          }
          const colonIdx = line.indexOf(":");
          const field = colonIdx === -1 ? line : line.slice(0, colonIdx);
          let val = colonIdx === -1 ? "" : line.slice(colonIdx + 1);
          if (val.charAt(0) === " ") {
            val = val.slice(1);
          }
          if (field === "event") {
            eventName = val;
          } else if (field === "data") {
            dataLines.push(val);
          }
        }
      }

      if (!controller.signal.aborted) {
        scheduleSseReconnect();
      }
    } catch (err) {
      if (err && err.name === "AbortError") {
        return;
      }
      console.warn("SSE stream disconnected:", err);
      state.sseConnected = false;
      state.sseConnecting = true;
      updateSseStatusUi();
      scheduleSseReconnect();
    }
  }

  function scheduleSseReconnect() {
    state.sseConnected = false;
    state.sseConnecting = true;
    updateSseStatusUi();
    state.sseReconnectAttempts += 1;
    const delayMs = Math.min(15000, 1000 * Math.pow(2, Math.min(4, state.sseReconnectAttempts - 1)));
    state.sseReconnectTimer = window.setTimeout(function () {
      connectSse();
    }, delayMs);
  }

  const SSE_HANDLERS = {};
  SSE_HANDLERS[SSE_EVENTS.HEARTBEAT] = function (payload) {
    return { level: "info", title: "心跳保活", detail: payload.message || "stream alive" };
  };
  SSE_HANDLERS[SSE_EVENTS.CHAPTER_STARTED] = function (payload) {
    return { level: "info", title: "开始生成章节", detail: payload.message || ("第 " + (payload.chapter || 0) + " 章开始生成") };
  };
  SSE_HANDLERS[SSE_EVENTS.CHAPTER_ATTEMPT] = function (payload) {
    const attempt = payload.data && payload.data.attempt ? "（第 " + payload.data.attempt + " 次尝试）" : "";
    return { level: "info", title: "生成尝试" + attempt, detail: payload.message || "" };
  };
  SSE_HANDLERS[SSE_EVENTS.GATE_FAILED] = function (payload) {
    const violations = payload.data && Array.isArray(payload.data.violations) ? payload.data.violations : [];
    const rules = violations.map(function (v) { return (v.rule_id || v.code || "RULE") + ": " + (v.message || v.detail || ""); }).join("；");
    return { level: "fail", title: "门禁拦截重写", detail: rules || payload.message || "触发静态门禁" };
  };
  SSE_HANDLERS[SSE_EVENTS.GATE_PASSED] = function (payload) {
    return { level: "pass", title: "静态门禁通过", detail: payload.message || "0 违规通过 AI-TIC-CHECK" };
  };
  SSE_HANDLERS[SSE_EVENTS.CRITIC_SCORED] = function (payload) {
    const d = payload.data || {};
    const scores = d.scores || d;
    const parts = [];
    if (d.overall_score !== undefined && d.overall_score !== null) {
      parts.push("总分:" + d.overall_score);
    }
    const hasFourDim =
      scores.opening_hook !== undefined ||
      scores.payoff_density !== undefined ||
      scores.comment_hooks !== undefined ||
      scores.pacing !== undefined;
    if (hasFourDim || parts.length === 0) {
      parts.push(
        "首屏:" + (scores.opening_hook ?? "-") +
        " 爽感:" + (scores.payoff_density ?? "-") +
        " 互动:" + (scores.comment_hooks ?? "-") +
        " 节奏:" + (scores.pacing ?? "-")
      );
    }
    const weakList = Array.isArray(d.weak_paragraphs) ? d.weak_paragraphs : [];
    if (weakList.length > 0) {
      parts.push("待精修段落:[" + weakList.join(", ") + "]");
    } else if (Array.isArray(d.weak_paragraphs)) {
      parts.push("无需局部精修");
    }
    const summary = parts.join(" · ");
    return { level: "info", title: "毒舌主编打分", detail: (payload.message ? payload.message + " · " : "") + summary };
  };
  SSE_HANDLERS[SSE_EVENTS.SURGICAL_POLISHED] = function (payload) {
    const d = payload.data || {};
    return {
      level: "pass",
      title: "手术刀局部精修完成",
      detail: payload.message || "已局部替换低分段落并复检门禁",
      before: d.before || "",
      after: d.after || "",
    };
  };
  SSE_HANDLERS[SSE_EVENTS.CHAPTER_STOCKPILED] = function (payload) {
    loadBooks();
    loadProductionKpi();
    return { level: "pass", title: "章节囤稿入库", detail: payload.message || ("第 " + (payload.chapter || 0) + " 章已入库") };
  };
  SSE_HANDLERS[SSE_EVENTS.BOOK_ISOLATED] = function (payload) {
    loadBooks();
    openIsolationModal(payload);
    return { level: "fail", title: "单书故障隔离", detail: payload.message || "重写超限已隔离，其它书目不受影响" };
  };
  SSE_HANDLERS[SSE_EVENTS.SAFETY_CUSHION_ALERT] = function (payload) {
    loadBooks();
    return { level: "warn", title: "安全垫低水位告警", detail: payload.message || "存稿低于安全阈值，已置顶抢产" };
  };
  SSE_HANDLERS[SSE_EVENTS.COMPANION_CAPTCHA] = function (payload) {
    const d = payload.data || {};
    const accountId = String(d.account_id || payload.book_id || "default");
    state.captchaAlerts.set(accountId, {
      account_id: accountId,
      book_id: payload.book_id || d.book_id || "",
      reason: d.reason || payload.message || "CAPTCHA_DETECTED",
      page_url: d.page_url || "",
      screenshot_base64: d.screenshot_base64 || "",
      screenshot_mime: d.screenshot_mime || "image/png",
    });
    renderCaptchaBanner();
    loadBooks();
    return { level: "fail", title: "伴侣验证码急停挂起", detail: payload.message || ("马甲 " + accountId + " 触发验证码急停") };
  };
  SSE_HANDLERS[SSE_EVENTS.COMPANION_RESUMED] = function (payload) {
    const d = payload.data || {};
    const accountId = String(d.account_id || payload.book_id || "");
    if (accountId) {
      state.captchaAlerts.delete(accountId);
    } else {
      state.captchaAlerts.clear();
    }
    renderCaptchaBanner();
    loadBooks();
    return { level: "pass", title: "伴侣已解冻恢复", detail: payload.message || "伴侣继续运行" };
  };
  SSE_HANDLERS[SSE_EVENTS.METRICS_COLLECTED] = function (payload) {
    return { level: "info", title: "12:30 数据回采完成", detail: payload.message || JSON.stringify(payload.data || {}) };
  };
  SSE_HANDLERS[SSE_EVENTS.RACING_DECISION] = function (payload) {
    loadBooks();
    return { level: "warn", title: "赛马进化决策", detail: payload.message || JSON.stringify(payload.data || {}) };
  };
  SSE_HANDLERS[SSE_EVENTS.CPA_QUOTA_ALERT] = function (payload) {
    const d = payload.data || {};
    const kind = d.kind === MODEL_QUOTA_ALERT_KINDS.MODEL_SWITCHED
      ? MODEL_QUOTA_ALERT_KINDS.MODEL_SWITCHED
      : MODEL_QUOTA_ALERT_KINDS.QUOTA_EXHAUSTED;
    const matchedBook = state.books.find(function (b) {
      return b.book_id === payload.book_id;
    });
    upsertModelQuotaAlert({
      kind: kind,
      book_id: payload.book_id || "",
      book_title: matchedBook ? matchedBook.title : (d.book_title || payload.book_id || ""),
      from_model: d.from_model || "",
      to_model: d.to_model || "",
      reason: d.reason || payload.message || "同档凭据冷却或额度触顶",
      error_type: d.error_type || "",
      timestamp: payload.timestamp || "",
      source: "sse",
    });
    loadBooks();
    if (kind === MODEL_QUOTA_ALERT_KINDS.MODEL_SWITCHED) {
      return {
        level: "warn",
        title: "写作模型已切换",
        detail: (d.from_model && d.to_model ? (d.from_model + " → " + d.to_model + " · ") : "") + (d.reason || payload.message || "白名单模型轮换"),
      };
    }
    return { level: "fail", title: "超预算 / 额度熔断告警", detail: payload.message || "同档凭据冷却或额度触顶，已停机保护" };
  };

  function dispatchSseFrame(eventName, rawData) {
    let payload = {};
    try {
      payload = JSON.parse(rawData);
    } catch (err) {
      payload = { event: eventName, message: rawData };
    }
    const evType = String((payload && payload.event) || eventName || "");
    const handler = SSE_HANDLERS[evType];
    if (!handler) {
      return;
    }
    const rendered = handler(payload);
    state.liveEvents.push({
      event: evType,
      book_id: String(payload.book_id || ""),
      chapter: Number(payload.chapter || 0),
      timestamp: String(payload.timestamp || ""),
      level: rendered.level,
      title: rendered.title,
      detail: rendered.detail,
      before: rendered.before || "",
      after: rendered.after || "",
    });
    if (state.liveEvents.length > 300) {
      state.liveEvents.shift();
    }
    renderLiveStream();
  }

  function renderLiveStream() {
    const feed = document.getElementById("live-stream-feed");
    const counter = document.getElementById("live-event-counter");
    const bookFilterEl = document.getElementById("live-book-filter");
    const showHbEl = document.getElementById("live-show-heartbeat");
    const autoScrollEl = document.getElementById("live-autoscroll");
    if (!feed) {
      return;
    }
    const bookFilter = bookFilterEl ? bookFilterEl.value : "";
    const showHeartbeat = showHbEl ? showHbEl.checked : false;
    const autoScroll = autoScrollEl ? autoScrollEl.checked : true;

    const visible = state.liveEvents.filter(function (ev) {
      if (!showHeartbeat && ev.event === SSE_EVENTS.HEARTBEAT) {
        return false;
      }
      if (bookFilter && ev.book_id !== bookFilter) {
        return false;
      }
      return true;
    });

    if (counter) {
      counter.textContent = visible.length + " 条事件";
    }
    if (visible.length === 0) {
      feed.innerHTML = '<div class="empty-state">等待直播事件推送…</div>';
      return;
    }

    feed.innerHTML = visible.map(function (ev) {
      const diffHtml = (ev.before || ev.after)
        ? '<div class="sse-diff-grid">' +
            '<div class="diff-box"><strong>精修前：</strong>\n' + escapeHtml(ev.before) + '</div>' +
            '<div class="diff-box"><strong>精修后：</strong>\n' + escapeHtml(ev.after) + '</div>' +
          '</div>'
        : "";
      return (
        '<div class="sse-item lvl-' + escapeHtml(ev.level) + '">' +
          '<div class="sse-item-head">' +
            '<div><span class="tag">' + escapeHtml(ev.event) + '</span> <strong>' + escapeHtml(ev.title) + '</strong>' +
              (ev.book_id ? ' · <span class="mono">' + escapeHtml(ev.book_id) + '</span>' : "") +
              (ev.chapter ? ' · 第 ' + escapeHtml(ev.chapter) + ' 章' : "") +
            '</div>' +
            '<span class="muted mono">' + escapeHtml((ev.timestamp || "").slice(11, 19)) + '</span>' +
          '</div>' +
          '<div>' + escapeHtml(ev.detail) + '</div>' +
          diffHtml +
        '</div>'
      );
    }).join("");

    if (autoScroll) {
      feed.scrollTop = feed.scrollHeight;
    }
  }

  /* ------------------------------------------------------------------
   * View 5: Reader, Chapter Lock, & Multi-Platform Export
   * ------------------------------------------------------------------ */
  function populateBookSelects() {
    const selects = [
      document.getElementById("reader-book-select"),
      document.getElementById("preview-book-select"),
      document.getElementById("live-book-filter"),
    ];
    selects.forEach(function (sel, idx) {
      if (!sel) {
        return;
      }
      const prev = sel.value;
      const defaultOpt = idx === 2 ? '<option value="">全部书籍事件</option>' : '<option value="">请选择书籍…</option>';
      sel.innerHTML = defaultOpt + state.books.map(function (b) {
        return '<option value="' + escapeHtml(b.book_id) + '">' + escapeHtml((b.title || b.book_id) + " (" + b.book_id + ")") + '</option>';
      }).join("");
      if (idx === 0 && state.readerBookId) {
        sel.value = state.readerBookId;
      } else if (prev) {
        sel.value = prev;
      }
    });
  }

  function resetReaderChapterSelection() {
    state.readerSelectedChapter = null;
    state.readerChapterDetail = null;
    const titleEl = document.getElementById("reader-chapter-title");
    const metaEl = document.getElementById("reader-chapter-meta");
    const bodyEl = document.getElementById("reader-chapter-body");
    const noteBox = document.getElementById("reader-author-note-box");
    const noteText = document.getElementById("reader-author-note-text");
    const lockBtn = document.getElementById("btn-toggle-lock");
    if (titleEl) {
      titleEl.textContent = "请选择左侧章节";
    }
    if (metaEl) {
      metaEl.innerHTML = "";
    }
    if (bodyEl) {
      bodyEl.innerHTML = '<div class="empty-state">点击左侧任意章节，即可查看纯净正文与物理隔离的「作者有话说」。</div>';
    }
    if (noteBox) {
      noteBox.hidden = true;
    }
    if (noteText) {
      noteText.textContent = "";
    }
    if (lockBtn) {
      lockBtn.disabled = true;
      lockBtn.textContent = "🔒 锁章保护";
    }
  }

  async function loadBookChapters(bookId) {
    const listEl = document.getElementById("reader-chapter-list");
    const countEl = document.getElementById("reader-chapter-count");
    const prevBookId = state.readerBookId;
    if (!bookId) {
      state.readerBookId = "";
      state.readerChapters = [];
      if (countEl) {
        countEl.textContent = "0 章";
      }
      if (listEl) {
        listEl.innerHTML = '<div class="empty-state">请先选择书籍</div>';
      }
      resetReaderChapterSelection();
      return;
    }
    if (bookId !== prevBookId) {
      state.readerChapters = [];
      if (countEl) {
        countEl.textContent = "0 章";
      }
      resetReaderChapterSelection();
    }
    state.readerBookId = bookId;
    try {
      const payload = await apiRequest(formatApiRoute(API_ROUTES.BOOK_CHAPTERS, { book_id: bookId }), {
        method: "GET",
      });
      if (state.readerBookId !== bookId) {
        return;
      }
      const chapters = Array.isArray(payload)
        ? payload
        : (payload && (payload.chapters || payload.items)) || [];
      state.readerChapters = chapters;
      if (countEl) {
        countEl.textContent = chapters.length + " 章";
      }
      renderChapterList();
    } catch (err) {
      if (state.readerBookId === bookId && listEl) {
        listEl.innerHTML = '<div class="empty-state">加载章节失败：' + escapeHtml(err.message) + '</div>';
      }
    }
  }

  function renderChapterList() {
    const listEl = document.getElementById("reader-chapter-list");
    if (!listEl) {
      return;
    }
    if (state.readerChapters.length === 0) {
      listEl.innerHTML = '<div class="empty-state">暂无已生成章节</div>';
      return;
    }
    listEl.innerHTML = state.readerChapters.map(function (ch) {
      const chNo = Number(ch.chapter);
      const isSelected = state.readerSelectedChapter === chNo;
      const stMeta = CHAPTER_STATUS_LABELS[ch.status] || { text: String(ch.status || ""), cls: "" };
      return (
        '<button type="button" class="chapter-item' + (isSelected ? " is-selected" : "") + '" data-chapter-no="' + escapeHtml(chNo) + '">' +
          '<div class="chapter-item-top">' +
            '<span>第 ' + escapeHtml(chNo) + ' 章 · ' + escapeHtml(ch.public_title || "未命名") + '</span>' +
            '<span>' + (ch.locked ? "🔒" : "🔓") + '</span>' +
          '</div>' +
          '<div class="chapter-item-sub">' +
            '<span class="tag ' + escapeHtml(stMeta.cls) + '">' + escapeHtml(stMeta.text) + '</span>' +
            '<span class="mono">' + escapeHtml(ch.char_count || 0) + ' 字</span>' +
            (ch.model ? '<span class="tag tag-model" data-chapter-model="' + escapeHtml(ch.model) + '">模型：' + escapeHtml(ch.model) + '</span>' : "") +
          '</div>' +
        '</button>'
      );
    }).join("");
  }

  async function loadChapterDetail(bookId, chapterNo) {
    const targetChapter = Number(chapterNo);
    state.readerSelectedChapter = targetChapter;
    renderChapterList();
    const titleEl = document.getElementById("reader-chapter-title");
    const metaEl = document.getElementById("reader-chapter-meta");
    const bodyEl = document.getElementById("reader-chapter-body");
    const noteBox = document.getElementById("reader-author-note-box");
    const noteText = document.getElementById("reader-author-note-text");
    const lockBtn = document.getElementById("btn-toggle-lock");

    try {
      const detail = await apiRequest(
        formatApiRoute(API_ROUTES.BOOK_CHAPTER_DETAIL, { book_id: bookId, chapter: targetChapter }),
        { method: "GET" }
      );
      if (state.readerBookId !== bookId || state.readerSelectedChapter !== targetChapter) {
        return;
      }
      state.readerChapterDetail = detail;
      const locked = Boolean(detail && detail.locked);
      if (titleEl) {
        titleEl.textContent = "第 " + targetChapter + " 章 · " + ((detail && detail.public_title) || "未命名");
      }
      if (metaEl) {
        const chapterModel = String((detail && detail.model) || "").trim();
        metaEl.innerHTML =
          '<span>状态：<strong>' + escapeHtml((detail && detail.status) || "-") + '</strong></span>' +
          '<span>字数：<strong class="mono">' + escapeHtml((detail && detail.char_count) || 0) + '</strong></span>' +
          '<span>模型：<strong class="mono chapter-model-value" id="reader-chapter-model">' + escapeHtml(chapterModel || "未记录") + '</strong></span>' +
          (detail && detail.sha256 ? '<span class="mono">SHA256: ' + escapeHtml(String(detail.sha256).slice(0, 12)) + '…</span>' : "") +
          (locked ? '<span class="tag tag-gold">🔒 已锁章保护</span>' : '<span class="tag">🔓 未锁定</span>');
      }
      if (bodyEl) {
        const rawBody = String((detail && detail.body) || "");
        const paras = rawBody.split(/\n+/).map(function (p) { return p.trim(); }).filter(Boolean);
        bodyEl.innerHTML = paras.length > 0
          ? paras.map(function (p) { return '<p>' + escapeHtml(p) + '</p>'; }).join("")
          : '<div class="empty-state">本章暂无正文内容</div>';
      }
      const authorNote = String((detail && detail.author_note) || "").trim();
      if (noteBox && noteText) {
        if (authorNote) {
          noteBox.hidden = false;
          noteText.textContent = authorNote;
        } else {
          noteBox.hidden = true;
          noteText.textContent = "";
        }
      }
      if (lockBtn) {
        lockBtn.disabled = false;
        lockBtn.textContent = locked ? "🔓 解锁章节" : "🔒 锁章保护";
      }
    } catch (err) {
      if (state.readerBookId === bookId && state.readerSelectedChapter === targetChapter) {
        showToast("读取章节详情失败：" + err.message, "err");
      }
    }
  }

  async function toggleChapterLock() {
    const targetBookId = state.readerBookId;
    const targetChapter = state.readerSelectedChapter;
    const detail = state.readerChapterDetail;
    if (!targetBookId || !targetChapter || !detail) {
      return;
    }
    const lockBtn = document.getElementById("btn-toggle-lock");
    if (lockBtn) {
      lockBtn.disabled = true;
    }
    const nextLocked = !Boolean(detail.locked);
    try {
      await apiRequest(
        formatApiRoute(API_ROUTES.BOOK_CHAPTER_LOCK, {
          book_id: targetBookId,
          chapter: targetChapter,
        }),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            locked: nextLocked,
            reason: nextLocked ? "Web 控制台人工锁章保护" : "Web 控制台解锁重跑",
          }),
        }
      );
      showToast(nextLocked ? "已锁章保护，禁止重跑覆盖" : "已解锁该章节", "ok");
      if (state.readerBookId !== targetBookId || state.readerSelectedChapter !== targetChapter) {
        return;
      }
      await loadBookChapters(targetBookId);
      if (state.readerBookId !== targetBookId || state.readerSelectedChapter !== targetChapter) {
        return;
      }
      await loadChapterDetail(targetBookId, targetChapter);
    } catch (err) {
      showToast("切换锁章状态失败：" + err.message, "err");
    } finally {
      if (lockBtn && state.readerBookId === targetBookId && state.readerSelectedChapter === targetChapter) {
        lockBtn.disabled = false;
      }
    }
  }

  const SUPPORTED_EXPORT_PLATFORMS = Object.freeze(["tomato", "qimao", "zhihu"]);

  async function downloadBookExport(platform) {
    const bookId = state.readerBookId;
    if (!bookId) {
      showToast("请先在上方选择要导出的书籍", "err");
      return;
    }
    const cleanPlatform = String(platform || "").trim().toLowerCase();
    if (SUPPORTED_EXPORT_PLATFORMS.indexOf(cleanPlatform) === -1) {
      showToast("不支持的导出平台：" + cleanPlatform, "err");
      return;
    }
    try {
      const baseUrl = formatApiRoute(API_ROUTES.BOOK_EXPORT, { book_id: bookId });
      const url = baseUrl + "?platform=" + encodeURIComponent(cleanPlatform);
      const resp = await fetch(url, { headers: buildAuthHeaders() });
      if (resp.status === 401 || resp.status === 403) {
        state.authRequired = true;
        updateAuthUi("导出资源包需要管理员 Bearer Token");
        throw new Error("未授权导出");
      }
      if (!resp.ok) {
        throw new Error("导出失败 HTTP " + resp.status);
      }
      const blob = await resp.blob();
      const objUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objUrl;
      link.download = bookId + "_" + cleanPlatform + "_bundle.zip";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.setTimeout(function () {
        URL.revokeObjectURL(objUrl);
      }, 2000);
      showToast("已开始下载 " + cleanPlatform + " 导出包", "ok");
    } catch (err) {
      showToast("导出下载失败：" + err.message, "err");
    }
  }

  /* ------------------------------------------------------------------
   * Event Binding & Bootstrap
   * ------------------------------------------------------------------ */
  function bindEvents() {
    document.querySelectorAll(".nav-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchView(btn.getAttribute("data-view"));
      });
    });

    document.querySelectorAll("[data-close-modal]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        closeModal(btn.getAttribute("data-close-modal"));
      });
    });

    const openAuth = function () {
      const inp = document.getElementById("auth-token-input");
      if (inp) {
        inp.value = state.authToken;
      }
      openModal("auth-modal");
    };
    const btnOpenAuth = document.getElementById("btn-open-auth-modal");
    const btnBannerLogin = document.getElementById("btn-banner-login");
    if (btnOpenAuth) {
      btnOpenAuth.addEventListener("click", openAuth);
    }
    if (btnBannerLogin) {
      btnBannerLogin.addEventListener("click", openAuth);
    }

    const btnSaveToken = document.getElementById("btn-save-token");
    if (btnSaveToken) {
      btnSaveToken.addEventListener("click", async function () {
        const inp = document.getElementById("auth-token-input");
        const tokenVal = inp ? inp.value.trim() : "";
        persistSessionToken(tokenVal);
        closeModal("auth-modal");
        if (!tokenVal && (!state.health || state.health.admin_auth_required !== false)) {
          state.authRequired = true;
          updateAuthUi("当前服务端要求 Bearer Token（XIAOSHUO_ADMIN_TOKEN），请点击右侧按钮输入密钥。");
          return;
        }
        clearErrorToasts();
        await refreshAll();
        if (!state.authRequired) {
          clearErrorToasts();
          connectSse();
        }
      });
    }

    const btnClearToken = document.getElementById("btn-clear-token");
    if (btnClearToken) {
      btnClearToken.addEventListener("click", function () {
        persistSessionToken("");
        disconnectSse();
        const inp = document.getElementById("auth-token-input");
        if (inp) {
          inp.value = "";
        }
        closeModal("auth-modal");
        if (!state.health || state.health.admin_auth_required !== false) {
          state.authRequired = true;
          updateAuthUi("已清除本地令牌，请输入有效管理员 Bearer Token");
        }
        showToast("已清除本地令牌", "ok");
      });
    }

    const alertRegion = document.getElementById("model-quota-alert-region");
    if (alertRegion) {
      alertRegion.addEventListener("click", async function (evt) {
        const btn = evt.target.closest("[data-alert-action]");
        if (!btn) {
          return;
        }
        const action = btn.getAttribute("data-alert-action");
        const alertId = btn.getAttribute("data-alert-id") || "";
        const bookId = btn.getAttribute("data-book-id") || "";
        if (action === "goto-cpa") {
          switchView("cpa");
        } else if (action === "dismiss") {
          dismissModelQuotaAlert(alertId);
        } else if (action === "resume-book" && bookId) {
          await handleBookAction("resume", bookId);
          if (alertId) {
            state.modelQuotaAlerts.delete(alertId);
            state.dismissedQuotaAlertIds.delete(alertId);
            renderModelQuotaAlerts();
          }
        }
      });
    }

    const btnRefreshAll = document.getElementById("btn-refresh-all");
    if (btnRefreshAll) {
      btnRefreshAll.addEventListener("click", refreshAll);
    }
    const btnOverviewRefresh = document.getElementById("btn-overview-refresh");
    if (btnOverviewRefresh) {
      btnOverviewRefresh.addEventListener("click", loadBooks);
    }
    const btnGotoCreate = document.getElementById("btn-goto-create");
    if (btnGotoCreate) {
      btnGotoCreate.addEventListener("click", function () {
        switchView("create");
      });
    }

    const btnToggleSse = document.getElementById("btn-toggle-sse");
    if (btnToggleSse) {
      btnToggleSse.addEventListener("click", function () {
        if (state.sseConnected || state.sseConnecting) {
          disconnectSse();
        } else {
          connectSse();
        }
      });
    }

    document.querySelectorAll(".filter-chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        document.querySelectorAll(".filter-chip").forEach(function (c) {
          c.classList.remove("is-active");
        });
        chip.classList.add("is-active");
        state.bookFilter = chip.getAttribute("data-filter") || "all";
        renderBooksGrid();
      });
    });

    const booksGrid = document.getElementById("books-grid");
    if (booksGrid) {
      booksGrid.addEventListener("click", function (evt) {
        const btn = evt.target.closest("[data-book-action]");
        if (!btn) {
          return;
        }
        handleBookAction(btn.getAttribute("data-book-action"), btn.getAttribute("data-book-id"));
      });
    }

    const captchaActions = document.getElementById("captcha-banner-actions");
    if (captchaActions) {
      captchaActions.addEventListener("click", function (evt) {
        const btn = evt.target.closest("[data-resume-account]");
        if (!btn) {
          return;
        }
        resumeCompanion(btn.getAttribute("data-resume-account"), btn.getAttribute("data-resume-book"));
      });
    }

    const btnCutStep1 = document.getElementById("btn-cut-step1");
    const btnCutConfirm = document.getElementById("btn-cut-confirm");
    if (btnCutStep1) {
      btnCutStep1.addEventListener("click", requestCutStep1);
    }
    if (btnCutConfirm) {
      btnCutConfirm.addEventListener("click", confirmCutStep2);
    }

    // View 2 bindings
    const btnGenId = document.getElementById("btn-gen-project-id");
    if (btnGenId) {
      btnGenId.addEventListener("click", function () {
        const inp = document.getElementById("create-project-id");
        if (inp) {
          inp.value = "book_" + Date.now().toString(36);
        }
      });
    }

    const channelTabs = document.getElementById("genre-channel-tabs");
    if (channelTabs) {
      channelTabs.addEventListener("click", function (evt) {
        const btn = evt.target.closest("[data-channel]");
        if (!btn) {
          return;
        }
        channelTabs.querySelectorAll(".mini-tab").forEach(function (t) {
          t.classList.remove("is-active");
        });
        btn.classList.add("is-active");
        state.genreChannelFilter = btn.getAttribute("data-channel") || "all";
        renderGenreGrid();
      });
    }

    const genreGrid = document.getElementById("genre-selector-grid");
    if (genreGrid) {
      genreGrid.addEventListener("click", function (evt) {
        const card = evt.target.closest("[data-genre-id]");
        if (!card) {
          return;
        }
        state.selectedGenreId = card.getAttribute("data-genre-id") || "";
        renderGenreGrid();
        renderSelectedGenreDetail();
      });
    }

    const benchFile = document.getElementById("create-benchmark-file");
    if (benchFile) {
      benchFile.addEventListener("change", async function () {
        const files = Array.from(benchFile.files || []);
        const texts = [];
        for (let i = 0; i < files.length; i += 1) {
          texts.push(await files[i].text());
        }
        const area = document.getElementById("create-benchmark-text");
        if (area && texts.length > 0) {
          area.value = texts.join("\n===\n");
        }
      });
    }

    const autoForm = document.getElementById("auto-create-form");
    if (autoForm) {
      autoForm.addEventListener("submit", handleAutoCreateSubmit);
    }

    const previewSelect = document.getElementById("preview-book-select");
    if (previewSelect) {
      previewSelect.addEventListener("change", function () {
        const book = state.books.find(function (b) {
          return b.book_id === previewSelect.value;
        });
        if (book) {
          renderBookPreviewInto(
            book,
            document.getElementById("cover-preview-box"),
            document.getElementById("alt-titles-box")
          );
        }
      });
    }

    // View 3 CPA bindings
    const dropzone = document.getElementById("cpa-dropzone");
    const cpaFileInput = document.getElementById("cpa-file-input");
    if (dropzone && cpaFileInput) {
      dropzone.addEventListener("click", function () {
        cpaFileInput.click();
      });
      dropzone.addEventListener("keydown", function (evt) {
        if (evt.key === "Enter" || evt.key === " ") {
          evt.preventDefault();
          cpaFileInput.click();
        }
      });
      dropzone.addEventListener("dragover", function (evt) {
        evt.preventDefault();
        dropzone.classList.add("is-dragover");
      });
      dropzone.addEventListener("dragleave", function () {
        dropzone.classList.remove("is-dragover");
      });
      dropzone.addEventListener("drop", function (evt) {
        evt.preventDefault();
        dropzone.classList.remove("is-dragover");
        if (evt.dataTransfer && evt.dataTransfer.files) {
          uploadCpaFiles(evt.dataTransfer.files);
        }
      });
      cpaFileInput.addEventListener("change", function () {
        if (cpaFileInput.files) {
          uploadCpaFiles(cpaFileInput.files);
        }
      });
    }

    const cpaAuthList = document.getElementById("cpa-auth-list");
    if (cpaAuthList) {
      cpaAuthList.addEventListener("click", function (evt) {
        const btn = evt.target.closest("[data-delete-auth]");
        if (!btn) {
          return;
        }
        deleteCpaAuthFile(btn.getAttribute("data-delete-auth"));
      });
    }

    const btnOAuth = document.getElementById("btn-fetch-oauth");
    if (btnOAuth) {
      btnOAuth.addEventListener("click", fetchOAuthUrl);
    }
    const btnRefreshCpa = document.getElementById("btn-refresh-cpa");
    if (btnRefreshCpa) {
      btnRefreshCpa.addEventListener("click", loadCpaPanel);
    }
    const btnRefreshModels = document.getElementById("btn-refresh-models");
    if (btnRefreshModels) {
      btnRefreshModels.addEventListener("click", loadCpaModelsAndBindings);
    }
    const btnSaveBindings = document.getElementById("btn-save-bindings");
    if (btnSaveBindings) {
      btnSaveBindings.addEventListener("click", saveCpaBindings);
    }

    // View 4 Live bindings
    const liveBookFilter = document.getElementById("live-book-filter");
    const liveHeartbeat = document.getElementById("live-show-heartbeat");
    const btnLiveClear = document.getElementById("btn-live-clear");
    if (liveBookFilter) {
      liveBookFilter.addEventListener("change", renderLiveStream);
    }
    if (liveHeartbeat) {
      liveHeartbeat.addEventListener("change", renderLiveStream);
    }
    if (btnLiveClear) {
      btnLiveClear.addEventListener("click", function () {
        state.liveEvents = [];
        renderLiveStream();
      });
    }

    // View 5 Reader bindings
    const readerBookSelect = document.getElementById("reader-book-select");
    if (readerBookSelect) {
      readerBookSelect.addEventListener("change", function () {
        loadBookChapters(readerBookSelect.value);
      });
    }
    const btnReaderRefresh = document.getElementById("btn-reader-refresh");
    if (btnReaderRefresh) {
      btnReaderRefresh.addEventListener("click", function () {
        loadBookChapters(state.readerBookId);
      });
    }
    const chapterList = document.getElementById("reader-chapter-list");
    if (chapterList) {
      chapterList.addEventListener("click", function (evt) {
        const item = evt.target.closest("[data-chapter-no]");
        if (!item) {
          return;
        }
        loadChapterDetail(state.readerBookId, item.getAttribute("data-chapter-no"));
      });
    }
    const btnLock = document.getElementById("btn-toggle-lock");
    if (btnLock) {
      btnLock.addEventListener("click", toggleChapterLock);
    }
    document.querySelectorAll(".export-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        downloadBookExport(btn.getAttribute("data-platform") || "tomato");
      });
    });
  }

  async function refreshAll(opts) {
    const health = (opts && opts.skipHealth) ? state.health : await loadHealth();
    const authRequired = !health || health.admin_auth_required !== false;
    if (!state.authToken && authRequired) {
      state.authRequired = true;
      updateAuthUi("当前服务端要求 Bearer Token（XIAOSHUO_ADMIN_TOKEN），请点击右侧按钮输入密钥。");
      return;
    }
    await loadBooks();
    if (!state.authRequired) {
      loadModelSwitches();
      loadCpaUsage();
      loadProductionKpi();
      const isolated = state.books.find(function (b) { return b.status === "ISOLATED_GATE_FAILURE"; });
      if (isolated) {
        openIsolationModal({
          book_id: isolated.book_id,
          chapter: 0,
          message: isolated.paused_reason || "",
          data: {},
        });
      }
    }
  }

  function bindIsolationModal() {
    document.getElementById("btn-isolation-later").addEventListener("click", function () {
      closeModal("isolation-modal");
      showToast("已保持停笔：" + state.isolationBookId, "ok");
    });
    document.getElementById("btn-isolation-resume").addEventListener("click", async function () {
      const bookId = state.isolationBookId;
      try {
        await apiRequest(formatApiRoute(API_ROUTES.BOOK_RESUME, { book_id: bookId }), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ book_id: bookId }),
        });
        closeModal("isolation-modal");
        showToast("已恢复写作：" + bookId, "ok");
        await loadBooks();
      } catch (err) {
        showToast("恢复失败：" + err.message, "err");
      }
    });
  }

  async function init() {
    updateAuthUi();
    updateSseStatusUi();
    bindEvents();
    bindIsolationModal();
    const health = await loadHealth();
    const authRequired = !health || health.admin_auth_required !== false;
    if (!state.authToken && authRequired) {
      state.authRequired = true;
      updateAuthUi("当前服务端要求 Bearer Token（XIAOSHUO_ADMIN_TOKEN），请点击右侧按钮输入密钥。");
      return;
    }
    await refreshAll({ skipHealth: true });
    if (!state.authRequired) {
      connectSse();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
