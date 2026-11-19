(function () {
  "use strict";

  const API = Object.freeze({
    BOOKS: "/api/public/books",
    BOOK: "/api/public/books/{book_id}",
    CHAPTER: "/api/public/books/{book_id}/chapters/{chapter}",
    PIPELINE: "/api/public/pipeline",
    EVENTS: "/api/public/events/stream",
  });

  const STAGES = ["plan", "outline", "compile", "draft", "gate", "polish", "lock", "export"];
  const STAGE_LABELS = {
    plan: "立项",
    outline: "章纲",
    compile: "组装提示词",
    draft: "写稿",
    gate: "门禁质检",
    polish: "定点精修",
    lock: "锁章",
    export: "导出",
  };
  const FORWARD_EDGES = [
    ["plan", "outline"],
    ["outline", "compile"],
    ["compile", "draft"],
    ["draft", "gate"],
    ["gate", "polish"],
    ["polish", "lock"],
    ["lock", "export"],
  ];

  const state = {
    books: [],
    current: null,
    book: null,
    readerChapter: 0,
    pipeline: null,
    logs: [],
    autoScroll: true,
    motionPaused: false,
    sse: null,
    sseDisconnected: false,
    sseReconnectTimer: null,
    sseReconnectDelayMs: 1500,
    displayedTokens: { total: 0, reason: 0, comp: 0, prompt: 0 },
    targetTokens: { total: 0, reason: 0, comp: 0, prompt: 0 },
    tokenRafId: 0,
    elapsedTimerId: 0,
    dotRafId: 0,
    dotPaths: [],
  };

  const $ = (id) => document.getElementById(id);

  function route(tpl, params) {
    let out = tpl;
    Object.keys(params).forEach((k) => {
      out = out.replace("{" + k + "}", encodeURIComponent(String(params[k])));
    });
    return out;
  }

  async function getJson(url) {
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    const body = await resp.json();
    if (!resp.ok) {
      throw new Error((body && body.message) || ("HTTP " + resp.status));
    }
    return body;
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function prefersReducedMotion() {
    return state.motionPaused || (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function formatShanghaiTime(rawValue, opts) {
    const raw = String(rawValue || "").trim();
    if (!raw) return "—";
    const dt = new Date(raw);
    if (Number.isNaN(dt.getTime())) return raw;
    try {
      return new Intl.DateTimeFormat("zh-CN", Object.assign({
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }, opts || {})).format(dt);
    } catch (_err) {
      return raw;
    }
  }

  function formatHms(rawValue) {
    const raw = String(rawValue || "").trim();
    if (!raw) return "--:--:--";
    const dt = new Date(raw);
    if (!Number.isNaN(dt.getTime())) {
      try {
        return new Intl.DateTimeFormat("zh-CN", {
          timeZone: "Asia/Shanghai",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        }).format(dt);
      } catch (_err) {
        // fall through
      }
    }
    const tIdx = raw.indexOf("T");
    if (tIdx !== -1) return raw.slice(tIdx + 1, tIdx + 9);
    return raw.slice(0, 8);
  }

  function formatDuration(sec) {
    const val = Number(sec);
    if (!Number.isFinite(val) || val <= 0) return "—";
    if (val < 60) return Math.round(val) + "s";
    const m = Math.floor(val / 60);
    const s = Math.round(val % 60);
    return m + "分" + (s < 10 ? "0" : "") + s + "秒";
  }

  function formatInt(n) {
    const num = Math.round(Number(n) || 0);
    return num.toLocaleString("en-US");
  }

  function setLiveStatus(kind, text) {
    const pill = $("live-pill");
    const dot = $("live-dot");
    const label = $("live-state");
    if (!pill || !dot || !label) return;
    pill.className = "status-pill is-" + kind;
    dot.className = "status-dot" + (kind === "live" ? " is-live" : kind === "replay" ? " is-replay" : kind === "warn" ? " is-error" : "");
    label.textContent = text;
  }

  function syncRingCustomProperty(progressEl) {
    // Fallback for browsers without CSS attr(value type(<number>)) support
    const v = progressEl.getAttribute("value");
    progressEl.style.setProperty("--value", v !== null ? v : "0");
  }

  /* ------------------------------------------------------------------
   * Top Telemetry Dashboard: Quota Rings + Production KPIs
   * ------------------------------------------------------------------ */
  function renderQuotaRings(quotas, activeModel) {
    const container = $("quota-rings");
    if (!container) return;
    container.textContent = "";
    const list = Array.isArray(quotas) ? quotas : [];
    if (list.length === 0) {
      container.appendChild(el("div", "quota-empty", "暂无白名单模型额度记录，待调用后自动显示。"));
      return;
    }
    list.forEach((item) => {
      const hasQuota = Boolean(item.has_quota) && item.percent !== null && item.percent !== undefined && Number.isFinite(Number(item.percent));
      const rawPct = hasQuota ? Number(item.percent) : 0;
      const clamped = Math.max(0, Math.min(100, rawPct));
      const pctLabel = hasQuota
        ? ((Number.isInteger(rawPct) ? String(rawPct) : rawPct.toFixed(1)) + "%")
        : "--";
      const levelCls = !hasQuota ? "is-empty" : rawPct >= 90 ? "is-danger" : rawPct >= 70 ? "is-warn" : "is-safe";
      const isCurrent = Boolean(activeModel) && item.model === activeModel;

      const card = el("div", "quota-ring-card" + (isCurrent ? " is-active-model" : ""));
      card.dataset.quotaModel = String(item.model || "");

      const wrap = el("div", "progress-ring-wrapper");
      const prog = document.createElement("progress");
      prog.className = "progress-ring " + levelCls;
      prog.max = 100;
      prog.setAttribute("value", String(clamped));
      prog.setAttribute("aria-label", (item.label || item.model || "模型") + " 已用额度");
      prog.setAttribute("aria-valuetext", hasQuota ? ("已用 " + pctLabel) : "暂未上报");
      syncRingCustomProperty(prog);

      const center = el("div", "progress-ring-content mono", pctLabel);
      center.setAttribute("aria-hidden", "true");
      wrap.appendChild(prog);
      wrap.appendChild(center);

      card.appendChild(wrap);
      card.appendChild(el("div", "quota-ring-name", item.label || item.model || "模型"));
      const subText = item.manual_only
        ? "仅手动指定"
        : (hasQuota && item.reset_at ? ("重置 " + formatShanghaiTime(item.reset_at, { year: undefined, second: undefined })) : "暂未上报");
      card.appendChild(el("div", "quota-ring-sub mono", subText));
      container.appendChild(card);
    });
  }

  function renderTelemetryKpis(p) {
    const m = (p && p.metrics) || {};
    const curModel = String(p.current_model || m.current_model || "").trim();
    const curLabel = String(p.current_model_label || "").trim();
    $("kpi-model").textContent = curModel || "—";
    $("kpi-model-sub").textContent = curLabel ? (curLabel + (p.state === "running" ? " · 运行中" : "")) : (curModel ? "当前配置写稿模型" : "等待真实调用");

    const written = Number(m.book_written_chapters || 0);
    const total = Number(m.book_total_chapters || 0);
    const pct = total > 0 ? Math.min(100, Math.round((written / total) * 100)) : 0;
    $("kpi-progress").textContent = written + " / " + total;
    $("kpi-progress-bar").style.width = pct + "%";
    $("kpi-progress-sub").textContent = p.active_book_title
      ? ("《" + p.active_book_title + "》 · " + pct + "%")
      : (total > 0 ? ("已完成 " + pct + "%") : "暂无在写书目");

    $("kpi-today").textContent = String(m.today_chapters || 0) + " 章";
    $("kpi-today-date").textContent = (m.today_date || "—") + " (北京时间)";

    const totalProduced = Number(m.total_chapters_written || 0);
    $("kpi-avg-dur").textContent = totalProduced > 0 ? formatDuration(m.avg_duration_sec) : "—";
    $("kpi-avg-att").textContent = totalProduced > 0
      ? ("共 " + totalProduced + " 章 · 均 " + Number(m.avg_attempts || 0).toFixed(1) + " 次/章")
      : "暂无成稿耗时统计";

    const fpRate = Number(m.first_pass_rate || 0);
    $("kpi-first-pass").textContent = totalProduced > 0 ? ((fpRate * 100).toFixed(1) + "%") : "—";
    $("kpi-first-pass-sub").textContent = totalProduced > 0
      ? ("一次过审 " + Number(m.first_pass_count || 0) + " / " + totalProduced + " 章")
      : "无重写直通占比";
  }

  /* ------------------------------------------------------------------
   * Model Switch / Rate-Limit Backoff / Circuit Breaker Banner
   * ------------------------------------------------------------------ */
  function renderModelAlertBanner(alert) {
    const banner = $("model-event-banner");
    if (!banner) return;
    if (!alert || !alert.kind) {
      banner.hidden = true;
      banner.textContent = "";
      return;
    }
    banner.hidden = false;
    banner.className = "model-event-banner kind-" + String(alert.kind);
    banner.textContent = "";

    const left = el("div", "meb-left");
    const kindMeta = {
      model_switched: { pill: "模型已轮换", cls: "" },
      rate_limit_retry: { pill: "429 限流退避重试", cls: "" },
      model_restored: { pill: "首选模型已恢复", cls: "" },
      quota_exhausted: { pill: "全部额度用尽 · 熔断停机", cls: "" },
    }[alert.kind] || { pill: "模型事件", cls: "" };

    left.appendChild(el("span", "meb-pill", kindMeta.pill));

    if (alert.kind === "model_switched" || alert.kind === "model_restored") {
      const routeEl = el("span", "meb-route mono");
      routeEl.appendChild(el("span", "", alert.from_model || "原模型"));
      routeEl.appendChild(el("span", "meb-arrow", "━━▶"));
      routeEl.appendChild(el("span", "", alert.to_model || "目标模型"));
      left.appendChild(routeEl);
    } else if (alert.kind === "rate_limit_retry") {
      left.appendChild(el(
        "span",
        "meb-route mono",
        (alert.model || "当前模型") + " · 第 " + (alert.retry_attempt || 1) + "/" + (alert.max_retries || 3) + " 次退避（" + (alert.wait_seconds || 0) + "s）"
      ));
    }
    if (alert.reason) {
      left.appendChild(el("span", "meb-reason", alert.reason));
    }

    banner.appendChild(left);
    if (alert.timestamp) {
      banner.appendChild(el("span", "meb-time mono", formatShanghaiTime(alert.timestamp, { year: undefined })));
    }
  }

  /* ------------------------------------------------------------------
   * Real Usage Token Counter Animation (Strictly Between Real Measurements!)
   * ------------------------------------------------------------------ */
  function syncTokenCounter(tok) {
    const hasUsage = Boolean(tok && tok.has_usage);
    if (!hasUsage) {
      state.displayedTokens = { total: 0, reason: 0, comp: 0, prompt: 0 };
      state.targetTokens = { total: 0, reason: 0, comp: 0, prompt: 0 };
      if (state.tokenRafId) {
        window.cancelAnimationFrame(state.tokenRafId);
        state.tokenRafId = 0;
      }
      $("tok-total").textContent = "—";
      $("tok-reason").textContent = "—";
      $("tok-comp").textContent = "—";
      $("tok-prompt").textContent = "—";
      return;
    }

    const next = {
      total: Number(tok.total_tokens || 0),
      reason: Number(tok.reasoning_tokens || 0),
      comp: Number(tok.completion_tokens || 0),
      prompt: Number(tok.prompt_tokens || 0),
    };
    state.targetTokens = next;

    if (prefersReducedMotion()) {
      state.displayedTokens = Object.assign({}, next);
      paintTokens(state.displayedTokens);
      return;
    }

    const start = Object.assign({}, state.displayedTokens);
    if (start.total > next.total) {
      start.total = 0;
      start.reason = 0;
      start.comp = 0;
      start.prompt = 0;
    }
    if (state.tokenRafId) {
      window.cancelAnimationFrame(state.tokenRafId);
      state.tokenRafId = 0;
    }
    const t0 = performance.now();
    const duration = 620;
    function step(now) {
      const p = Math.min(1, (now - t0) / duration);
      const eased = 1 - Math.pow(1 - p, 3);
      state.displayedTokens = {
        total: Math.round(start.total + (next.total - start.total) * eased),
        reason: Math.round(start.reason + (next.reason - start.reason) * eased),
        comp: Math.round(start.comp + (next.comp - start.comp) * eased),
        prompt: Math.round(start.prompt + (next.prompt - start.prompt) * eased),
      };
      paintTokens(state.displayedTokens);
      if (p < 1) {
        state.tokenRafId = window.requestAnimationFrame(step);
      } else {
        state.tokenRafId = 0;
      }
    }
    state.tokenRafId = window.requestAnimationFrame(step);
  }

  function paintTokens(t) {
    $("tok-total").textContent = formatInt(t.total);
    $("tok-reason").textContent = t.reason > 0 ? formatInt(t.reason) : "0";
    $("tok-comp").textContent = formatInt(t.comp);
    $("tok-prompt").textContent = formatInt(t.prompt);
  }

  function syncThinkingTimer(p) {
    const row = $("draft-thinking-row");
    const elapsedEl = $("draft-elapsed");
    const textEl = $("draft-thinking-text");
    if (state.elapsedTimerId) {
      window.clearInterval(state.elapsedTimerId);
      state.elapsedTimerId = 0;
    }
    const isThinking = Boolean(p && p.thinking && p.active_stage === "draft" && p.active_stage_status === "running");
    row.hidden = !isThinking;
    if (!isThinking) return;

    const modelName = p.current_model || "模型";
    textEl.textContent = "思考中 · " + modelName;
    const startedMs = p.thinking_started_at ? Date.parse(p.thinking_started_at) : Date.now();
    const baseMs = Number.isNaN(startedMs) ? Date.now() : startedMs;
    function updateElapsed() {
      const diff = Math.max(0, Math.floor((Date.now() - baseMs) / 1000));
      elapsedEl.textContent = diff + "s";
    }
    updateElapsed();
    state.elapsedTimerId = window.setInterval(updateElapsed, 1000);
  }

  /* ------------------------------------------------------------------
   * Workflow Canvas Rendering + SVG Bezier Edges & Flowing Light Dots
   * ------------------------------------------------------------------ */
  function stageTagText(status, stage) {
    if (status === "running") return stage === "draft" ? "思考中" : "运行中";
    if (status === "done") return "已完成";
    if (status === "failed") return "未通过";
    if (status === "rewriting") return "打回重写";
    if (status === "skipped") return "已跳过";
    return "待机";
  }

  function renderWorkflowNodes(p) {
    const panel = $("canvas-panel");
    const pState = String(p.state || "idle");
    panel.dataset.pipelineState = pState;

    const isReplay = p.mode === "replay";
    const replayBadge = $("replay-badge");
    if (replayBadge) {
      replayBadge.hidden = !isReplay;
      if (isReplay && p.replay_label) {
        replayBadge.textContent = p.replay_label.indexOf("回放") !== -1 ? p.replay_label : ("回放 · " + p.replay_label);
      }
    }
    const termTitle = $("term-title");
    if (termTitle) {
      termTitle.textContent = isReplay ? "REPLAY LOG // 回放真实历史事件" : "LIVE STREAM // xiaoshuo-bot";
    }

    const stateBadgeMap = {
      running: "运行中",
      rewriting: "打回重写",
      idle: "待机",
      isolated: "门禁隔离",
      paused_quota: "额度熔断",
      paused_manual: "手动暂停",
    };
    $("pipeline-state-badge").textContent = stateBadgeMap[pState] || "待机";

    const isIdleLike = pState === "idle" || pState === "paused_manual" || pState === "paused_quota" || pState === "isolated";
    const idleStrip = $("idle-strip");
    idleStrip.hidden = !isIdleLike;
    if (isIdleLike) {
      const reason = p.idle_reason || "待机 / 等待下一章";
      $("pipeline-headline").textContent = reason;
      $("idle-strip-text").textContent = reason;
      $("idle-strip-ts").textContent = p.last_event_at
        ? ("上次事件：" + formatShanghaiTime(p.last_event_at))
        : "尚无历史事件";
    } else {
      const stageLabel = STAGE_LABELS[p.active_stage] || p.active_stage || "执行中";
      const chPart = p.active_chapter > 0 ? ("第 " + p.active_chapter + " 章") : "立项流程";
      const titlePart = p.active_book_title ? ("《" + p.active_book_title + "》 ") : "";
      $("pipeline-headline").textContent = titlePart + chPart + " · " + stageLabel;
    }

    $("pipeline-last-event").textContent = "上次事件：" + (p.last_event_at ? formatShanghaiTime(p.last_event_at, { year: undefined }) : "—");

    const attemptPill = $("pipeline-attempt-pill");
    if (!isIdleLike && p.attempt > 0 && p.max_attempts > 0) {
      attemptPill.hidden = false;
      attemptPill.textContent = "第 " + p.attempt + "/" + p.max_attempts + " 次";
      attemptPill.classList.toggle("is-rewrite", p.attempt > 1 || pState === "rewriting");
    } else {
      attemptPill.hidden = true;
    }

    const rewriteBadge = $("rewrite-loop-badge");
    const isRewriting = pState === "rewriting" || (p.from_stage === "gate" && p.active_stage === "draft");
    rewriteBadge.hidden = !isRewriting;
    if (isRewriting) {
      $("rewrite-loop-attempt").textContent = (p.attempt || 2) + "/" + (p.max_attempts || 4);
    }

    const stagesMap = p.stage_states || {};
    STAGES.forEach((key) => {
      const nodeEl = $("node-" + key);
      if (!nodeEl) return;
      const st = stagesMap[key] || { status: "idle", detail: "" };
      const status = isIdleLike && (st.status === "running" || st.status === "rewriting") ? "idle" : String(st.status || "idle");
      nodeEl.dataset.status = status;
      const tag = nodeEl.querySelector('[data-role="state"]');
      if (tag) tag.textContent = stageTagText(status, key);
      const detail = nodeEl.querySelector('[data-role="detail"]');
      if (detail) {
        if (st.detail) {
          detail.textContent = st.detail;
        } else if (status === "idle") {
          const defaults = {
            plan: "题材配方与全书总纲",
            outline: "节拍、伏笔与字数区间",
            compile: "世界状态与记忆金字塔",
            draft: "等待上游调度",
            gate: "字数 / 伏笔 / 节奏 / 世界状态",
            polish: "四维评审与局部手术修稿",
            lock: "SHA-256 存证与世界快照",
            export: "成稿入库 · 可打包导出",
          };
          detail.textContent = defaults[key] || "";
        }
      }
    });

    syncTokenCounter(p.chapter_tokens);
    syncThinkingTimer(p);
    renderGateRules(p.gate_rules, p.gate_summary);
    drawWorkflowEdges(p);
  }

  function renderGateRules(rules, summary) {
    const grid = $("gate-rules-grid");
    const note = $("gate-summary-note");
    if (!grid || !note) return;
    grid.textContent = "";
    const list = Array.isArray(rules) ? rules : [];
    if (list.length === 0) {
      note.textContent = "等待质检结果";
      grid.appendChild(el("div", "gate-empty", "尚无本次运行质检记录，执行至门禁节点时逐条亮灯展示。"));
      return;
    }

    const passCount = list.filter((r) => r.passed).length;
    if (summary && summary.chapter) {
      note.textContent =
        "ch" + String(summary.chapter).padStart(4, "0") +
        " · 第 " + (summary.attempt || 1) + "/" + (summary.max_attempts || 4) + " 次 · " +
        (summary.passed ? "全部通过" : "门禁拦截") + " (" + passCount + "/" + list.length + ")";
    } else {
      note.textContent = "已检查 " + list.length + " 项规则";
    }

    list.forEach((rule) => {
      const isWarn = !rule.passed && rule.severity === "WARNING";
      const cls = rule.passed ? "is-pass" : (isWarn ? "is-warn" : "is-fail");
      const chip = el("div", "gate-rule-chip " + cls);
      chip.appendChild(el("span", "gate-mark", rule.passed ? "✓" : (isWarn ? "!" : "✗")));
      const info = el("div", "gate-rule-info");
      info.appendChild(el("span", "gate-rule-id mono", rule.rule_id + " · " + (rule.label || "")));
      info.appendChild(el("span", "gate-rule-metric mono", rule.metric || (rule.passed ? "通过" : "未通过")));
      chip.appendChild(info);
      grid.appendChild(chip);
    });
  }

  function drawWorkflowEdges(p) {
    const stageEl = $("workflow-stage");
    const svg = $("workflow-svg");
    if (!stageEl || !svg) return;
    const stageRect = stageEl.getBoundingClientRect();
    if (stageRect.width <= 0 || stageRect.height <= 0) return;

    const svgNS = "http://www.w3.org/2000/svg";
    svg.setAttribute("viewBox", "0 0 " + stageRect.width + " " + stageRect.height);
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const defs = document.createElementNS(svgNS, "defs");
    function addMarker(id, color) {
      const mk = document.createElementNS(svgNS, "marker");
      mk.setAttribute("id", id);
      mk.setAttribute("viewBox", "0 0 10 10");
      mk.setAttribute("refX", "8");
      mk.setAttribute("refY", "5");
      mk.setAttribute("markerWidth", "6");
      mk.setAttribute("markerHeight", "6");
      mk.setAttribute("orient", "auto");
      const poly = document.createElementNS(svgNS, "path");
      poly.setAttribute("d", "M 0 1.5 L 8.5 5 L 0 8.5 z");
      poly.setAttribute("fill", color);
      mk.appendChild(poly);
      defs.appendChild(mk);
    }
    addMarker("arr-dim", "rgba(148,163,184,0.4)");
    addMarker("arr-done", "rgba(16,185,129,0.75)");
    addMarker("arr-active", "#22d3ee");
    addMarker("arr-rewrite", "#f59e0b");
    svg.appendChild(defs);

    function nodeRect(stageKey) {
      const n = $("node-" + stageKey);
      if (!n) return null;
      const r = n.getBoundingClientRect();
      return {
        left: r.left - stageRect.left,
        right: r.right - stageRect.left,
        top: r.top - stageRect.top,
        bottom: r.bottom - stageRect.top,
        cx: r.left - stageRect.left + r.width / 2,
        cy: r.top - stageRect.top + r.height / 2,
        w: r.width,
        h: r.height,
      };
    }

    const stagesMap = (p && p.stage_states) || {};
    const pState = p ? String(p.state || "idle") : "idle";
    const isRunningOrRewriting = pState === "running" || pState === "rewriting";
    const activeStage = p ? String(p.active_stage || "") : "";
    const isMobile = window.innerWidth <= 760;

    state.dotPaths = [];

    FORWARD_EDGES.forEach((pair) => {
      const fromKey = pair[0];
      const toKey = pair[1];
      const a = nodeRect(fromKey);
      const b = nodeRect(toKey);
      if (!a || !b) return;

      let d = "";
      if (isMobile) {
        d = "M " + a.cx + " " + a.bottom + " L " + b.cx + " " + (b.top - 4);
      } else if (Math.abs(a.cy - b.cy) < 20) {
        // Horizontal connection (Row 1 L->R or Row 2 R->L)
        if (b.cx > a.cx) {
          d = "M " + a.right + " " + a.cy + " L " + (b.left - 4) + " " + b.cy;
        } else {
          d = "M " + a.left + " " + a.cy + " L " + (b.right + 4) + " " + b.cy;
        }
      } else {
        // Vertical turn draft -> gate on column 4
        const xOff = a.cx - 18;
        d = "M " + xOff + " " + a.bottom + " L " + xOff + " " + (b.top - 4);
      }

      const fromSt = (stagesMap[fromKey] && stagesMap[fromKey].status) || "idle";
      const toSt = (stagesMap[toKey] && stagesMap[toKey].status) || "idle";
      const edgeActive = isRunningOrRewriting && (
        (toKey === activeStage && (toSt === "running" || toSt === "done")) ||
        (fromKey === activeStage && fromSt === "done")
      );
      const edgeDone = !edgeActive && fromSt === "done" && (toSt === "done" || toSt === "running" || toSt === "failed");

      const base = document.createElementNS(svgNS, "path");
      base.setAttribute("d", d);
      base.setAttribute("class", "wf-edge-base" + (edgeActive ? " is-active" : edgeDone ? " is-done" : ""));
      base.setAttribute("marker-end", edgeActive ? "url(#arr-active)" : edgeDone ? "url(#arr-done)" : "url(#arr-dim)");
      svg.appendChild(base);

      if (edgeActive) {
        const flow = document.createElementNS(svgNS, "path");
        flow.setAttribute("d", d);
        flow.setAttribute("class", "wf-edge-flow is-active");
        svg.appendChild(flow);

        const dot = document.createElementNS(svgNS, "circle");
        dot.setAttribute("r", "4");
        dot.setAttribute("class", "wf-edge-dot is-active");
        svg.appendChild(dot);
        state.dotPaths.push({ path: base, dot: dot, speed: 0.9, phase: 0 });
      }
    });

    // Curved Rewrite Return Edge: gate -> draft (backward light-dot loop!)
    const gateR = nodeRect("gate");
    const draftR = nodeRect("draft");
    if (gateR && draftR) {
      const isRewriteEdgeActive = pState === "rewriting" || (p && p.from_stage === "gate" && activeStage === "draft");
      let dBack = "";
      if (isMobile) {
        const rightX = Math.min(stageRect.width - 6, Math.max(gateR.right, draftR.right) + 12);
        dBack =
          "M " + gateR.right + " " + gateR.cy +
          " C " + rightX + " " + gateR.cy + ", " + rightX + " " + draftR.cy + ", " + (draftR.right + 4) + " " + draftR.cy;
      } else {
        const xLoop = gateR.cx + 26;
        const bulge = Math.min(stageRect.width - 6, gateR.right + 22);
        dBack =
          "M " + xLoop + " " + gateR.top +
          " C " + bulge + " " + (gateR.top - 10) + ", " + bulge + " " + (draftR.bottom + 10) + ", " + xLoop + " " + (draftR.bottom + 4);
      }

      const backBase = document.createElementNS(svgNS, "path");
      backBase.setAttribute("d", dBack);
      backBase.setAttribute("class", "wf-edge-base is-rewrite" + (isRewriteEdgeActive ? " is-active" : ""));
      backBase.setAttribute("marker-end", isRewriteEdgeActive ? "url(#arr-rewrite)" : "url(#arr-dim)");
      svg.appendChild(backBase);

      const rewriteBadge = $("rewrite-loop-badge");
      if (rewriteBadge && !isMobile) {
        const gapMidY = (draftR.bottom + gateR.top) / 2;
        rewriteBadge.style.top = gapMidY + "px";
        rewriteBadge.style.right = Math.max(8, stageRect.width - gateR.right) + "px";
      } else if (rewriteBadge && isMobile) {
        rewriteBadge.style.top = "";
        rewriteBadge.style.right = "";
      }

      if (isRewriteEdgeActive) {
        const backFlow = document.createElementNS(svgNS, "path");
        backFlow.setAttribute("d", dBack);
        backFlow.setAttribute("class", "wf-edge-flow is-rewrite is-active");
        svg.appendChild(backFlow);

        for (let i = 0; i < 2; i += 1) {
          const dot = document.createElementNS(svgNS, "circle");
          dot.setAttribute("r", "4.5");
          dot.setAttribute("class", "wf-edge-dot is-rewrite is-active");
          svg.appendChild(dot);
          state.dotPaths.push({ path: backBase, dot: dot, speed: 1.05, phase: i * 0.5 });
        }
      }
    }

    ensureDotAnimationLoop();
  }

  function ensureDotAnimationLoop() {
    if (state.dotRafId) {
      window.cancelAnimationFrame(state.dotRafId);
      state.dotRafId = 0;
    }
    if (state.dotPaths.length === 0) return;

    if (prefersReducedMotion()) {
      // Under reduced motion, pin dot cleanly at midpoint (static visual clarity, zero motion)
      state.dotPaths.forEach((item) => {
        const len = item.path.getTotalLength();
        const pt = item.path.getPointAtLength(len * 0.5);
        item.dot.setAttribute("cx", String(pt.x));
        item.dot.setAttribute("cy", String(pt.y));
      });
      return;
    }

    function tick(now) {
      if (prefersReducedMotion() || state.dotPaths.length === 0) {
        state.dotRafId = 0;
        return;
      }
      const sec = now / 1000;
      state.dotPaths.forEach((item) => {
        const len = item.path.getTotalLength();
        const t = ((sec * item.speed) + item.phase) % 1;
        const pt = item.path.getPointAtLength(len * t);
        item.dot.setAttribute("cx", String(pt.x));
        item.dot.setAttribute("cy", String(pt.y));
      });
      state.dotRafId = window.requestAnimationFrame(tick);
    }
    state.dotRafId = window.requestAnimationFrame(tick);
  }

  /* ------------------------------------------------------------------
   * Machine Log Terminal
   * ------------------------------------------------------------------ */
  function renderTerminal() {
    const body = $("terminal-body");
    const countEl = $("term-count");
    if (!body) return;
    body.textContent = "";
    countEl.textContent = state.logs.length + " 行";
    if (state.logs.length === 0) {
      body.appendChild(el("div", "term-empty", "等待真实流水线元数据事件…"));
      return;
    }
    const lastIdx = state.logs.length - 1;
    state.logs.forEach((item, idx) => {
      const row = el("div", "term-line lvl-" + (item.level || "info") + (idx === lastIdx ? " is-latest" : ""));
      row.appendChild(el("span", "term-ts", "[" + formatHms(item.timestamp) + "]"));
      row.appendChild(el("span", "term-tag", item.tag || "LOG"));
      row.appendChild(el("span", "term-msg", item.text || ""));
      body.appendChild(row);
    });
    if (state.autoScroll) {
      body.scrollTop = body.scrollHeight;
      $("btn-term-bottom").hidden = true;
    }
  }

  function appendLogEntry(entry) {
    if (!entry || !entry.text) return;
    state.logs.push(entry);
    while (state.logs.length > 100) state.logs.shift();
    renderTerminal();
  }

  /* ------------------------------------------------------------------
   * Apply Authoritative Snapshot & Live Incremental Events
   * ------------------------------------------------------------------ */
  function applyPipelineSnapshot(snap) {
    state.pipeline = snap;
    state.logs = Array.isArray(snap.recent_logs) ? snap.recent_logs.slice(-100) : [];
    renderQuotaRings(snap.model_quotas, snap.current_model);
    renderTelemetryKpis(snap);
    renderModelAlertBanner(snap.model_alert);
    renderWorkflowNodes(snap);
    renderTerminal();

    if (snap.mode === "replay") {
      setLiveStatus("replay", "回放模式 · " + (snap.state === "idle" ? "待机" : "真实历史事件"));
    } else if (state.sseDisconnected) {
      setLiveStatus("warn", "直播连接断开，正在重连…");
    } else if (snap.state === "running" || snap.state === "rewriting") {
      setLiveStatus("live", "实时直播 · 运行中");
    } else {
      setLiveStatus("idle", "实时直播 · 待机");
    }
  }

  function handlePipelineEvent(evt) {
    if (!state.pipeline) {
      syncFullState();
      return;
    }
    const p = state.pipeline;
    if (evt.replay) p.mode = "replay";
    if (evt.timestamp) p.last_event_at = evt.timestamp;
    if (evt.book_id) p.active_book_id = evt.book_id;
    if (evt.book_title) p.active_book_title = evt.book_title;
    if (evt.log) appendLogEntry(evt.log);

    const name = evt.event;
    if (name === "chapter_started") {
      p.active_chapter = Number(evt.chapter || 0);
      p.state = "running";
      p.attempt = 1;
      p.rewrite_count = 0;
      p.thinking = false;
      p.from_stage = "";
      p.gate_rules = [];
      p.gate_summary = {};
      p.chapter_tokens = { prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0, total_tokens: 0, calls: 0, has_usage: false };
      STAGES.forEach((s) => {
        p.stage_states[s] = { stage: s, label: STAGE_LABELS[s], status: s === "plan" ? "done" : "idle", detail: "" };
      });
    } else if (name === "pipeline_stage") {
      const stage = String(evt.stage || "");
      const status = String(evt.status || "running");
      if (evt.chapter > 0 && evt.chapter !== p.active_chapter && (stage === "outline" || stage === "compile" || stage === "draft")) {
        p.active_chapter = Number(evt.chapter);
        p.gate_rules = [];
        p.gate_summary = {};
        p.chapter_tokens = { prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0, total_tokens: 0, calls: 0, has_usage: false };
        STAGES.forEach((s) => {
          p.stage_states[s] = { stage: s, label: STAGE_LABELS[s], status: s === "plan" ? "done" : "idle", detail: "" };
        });
      }
      if (stage === "idle") {
        p.state = "idle";
        p.idle_reason = evt.detail || "待机 / 等待下一章";
        p.active_stage = "";
        p.active_stage_status = "idle";
        p.from_stage = "";
        p.thinking = false;
        STAGES.forEach((s) => {
          if (p.stage_states[s] && (p.stage_states[s].status === "running" || p.stage_states[s].status === "rewriting")) {
            p.stage_states[s].status = "idle";
          }
        });
      } else if (STAGES.indexOf(stage) !== -1) {
        if (stage !== "plan" && p.stage_states.plan && p.stage_states.plan.status === "idle") {
          p.stage_states.plan.status = "done";
        }
        p.active_stage = stage;
        p.active_stage_status = status;
        p.from_stage = String(evt.from_stage || "");
        if (evt.attempt) p.attempt = Number(evt.attempt);
        if (evt.max_attempts) p.max_attempts = Number(evt.max_attempts);
        if (evt.model) p.current_model = String(evt.model);
        p.state = status === "rewriting" ? "rewriting" : "running";
        if (stage === "draft" && status === "running" && evt.thinking) {
          p.thinking = true;
          p.thinking_started_at = evt.timestamp || new Date().toISOString();
        } else if (stage === "draft") {
          p.thinking = false;
        }
        p.stage_states[stage] = {
          stage: stage,
          label: STAGE_LABELS[stage],
          status: status,
          detail: String(evt.detail || ""),
          model: String(evt.model || p.current_model || ""),
          attempt: Number(evt.attempt || p.attempt || 0),
          max_attempts: Number(evt.max_attempts || p.max_attempts || 0),
          updated_at: evt.timestamp || "",
        };
      }
    } else if (name === "pipeline_token_usage") {
      if (evt.model) p.current_model = String(evt.model);
      if (evt.model_label) p.current_model_label = String(evt.model_label);
      if (!p.chapter_tokens) {
        p.chapter_tokens = { prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0, total_tokens: 0, calls: 0, has_usage: false };
      }
      p.chapter_tokens.prompt_tokens += Number(evt.prompt_tokens || 0);
      p.chapter_tokens.completion_tokens += Number(evt.completion_tokens || 0);
      p.chapter_tokens.reasoning_tokens += Number(evt.reasoning_tokens || 0);
      p.chapter_tokens.total_tokens += Number(evt.total_tokens || 0);
      p.chapter_tokens.calls += 1;
      p.chapter_tokens.has_usage = true;
    } else if (name === "pipeline_gate_report") {
      p.gate_rules = Array.isArray(evt.rules) ? evt.rules : [];
      p.gate_summary = {
        chapter: Number(evt.chapter || p.active_chapter || 0),
        attempt: Number(evt.attempt || 0),
        max_attempts: Number(evt.max_attempts || 0),
        passed: Boolean(evt.passed),
        char_count: Number(evt.char_count || 0),
        min_chars: Number(evt.min_chars || 0),
        max_chars: Number(evt.max_chars || 0),
        will_rewrite: Boolean(evt.will_rewrite),
      };
    } else if (name === "pipeline_model_event") {
      if (evt.kind === "quota_updated" && evt.quota) {
        const list = Array.isArray(p.model_quotas) ? p.model_quotas.slice() : [];
        let matched = false;
        for (let i = 0; i < list.length; i += 1) {
          if (list[i].model === evt.quota.model) {
            list[i] = Object.assign({}, list[i], evt.quota, { has_quota: true });
            matched = true;
            break;
          }
        }
        if (!matched) list.push(evt.quota);
        p.model_quotas = list;
      } else {
        p.model_alert = {
          kind: evt.kind,
          role: evt.role || "",
          model: evt.model || "",
          from_model: evt.from_model || "",
          to_model: evt.to_model || "",
          retry_attempt: Number(evt.retry_attempt || 0),
          max_retries: Number(evt.max_retries || 0),
          wait_seconds: Number(evt.wait_seconds || 0),
          reason: evt.reason || "",
          timestamp: evt.timestamp || "",
        };
        if ((evt.kind === "model_switched" || evt.kind === "model_restored") && evt.to_model) {
          p.current_model = String(evt.to_model);
          const matchedQ = (Array.isArray(p.model_quotas) ? p.model_quotas : []).find((q) => q.model === p.current_model);
          p.current_model_label = matchedQ && matchedQ.label ? String(matchedQ.label) : "";
        }
        if (evt.kind === "quota_exhausted") {
          p.state = "paused_quota";
          p.thinking = false;
          p.idle_reason = "全部白名单额度用尽 / 熔断停机";
        }
      }
    } else if (name === "pipeline_chapter_locked" || name === "chapter_stockpiled") {
      if (p.stage_states && p.stage_states.lock) {
        p.stage_states.lock.status = "done";
        if (evt.sha256_prefix) p.stage_states.lock.detail = "sha256:" + evt.sha256_prefix;
      }
      p.thinking = false;
      refreshWorksAndMetrics(evt.book_id).catch(() => {});
    } else if (name === "book_isolated") {
      p.state = "isolated";
      p.thinking = false;
      p.idle_reason = "单书门禁隔离，等待检修";
      refreshWorksAndMetrics(evt.book_id).catch(() => {});
    }

    renderQuotaRings(p.model_quotas, p.current_model);
    renderTelemetryKpis(p);
    renderModelAlertBanner(p.model_alert);
    renderWorkflowNodes(p);
    if (p.mode === "replay") {
      setLiveStatus("replay", "回放模式 · " + (p.state === "idle" ? "待机" : "真实历史事件"));
    } else if (p.state === "running" || p.state === "rewriting") {
      setLiveStatus("live", "实时直播 · 运行中");
    } else {
      setLiveStatus("idle", "实时直播 · 待机");
    }
  }

  /* ------------------------------------------------------------------
   * Works Shelf, Book Details, Current Plan & Approved Chapter Reader
   * ------------------------------------------------------------------ */
  function badgeClass(status) {
    if (status === "ACTIVE") return "badge is-active";
    if (status === "COMPLETED" || status === "PAUSED_MANUAL") return "badge";
    return "badge is-paused";
  }

  function renderShelf() {
    const list = $("shelf");
    list.textContent = "";
    $("shelf-empty").hidden = state.books.length > 0;
    state.books.forEach((b) => {
      const li = el("li");
      const btn = el("button");
      btn.type = "button";
      if (state.current === b.book_id) btn.classList.add("is-active");
      btn.appendChild(el("span", "t", "《" + b.title + "》"));
      const s = el("span", "s");
      s.appendChild(el("span", badgeClass(b.status), b.status_text));
      if (b.genre) s.appendChild(el("span", "", b.genre));
      s.appendChild(el("span", "mono", b.written_chapters + " / " + b.total_chapters + " 章"));
      btn.appendChild(s);
      btn.addEventListener("click", () => openBook(b.book_id));
      li.appendChild(btn);
      list.appendChild(li);
    });
  }

  function renderBook() {
    const b = state.book;
    if (!b) return;
    $("book-panel").hidden = false;
    $("reader").hidden = true;
    $("book-title").textContent = "《" + b.title + "》";
    $("book-meta").textContent = [b.genre, b.status_text].filter(Boolean).join(" · ");
    const pct = b.total_chapters ? Math.round((b.written_chapters / b.total_chapters) * 100) : 0;
    $("book-bar").style.width = pct + "%";
    $("book-progress").textContent = "已过审 " + b.written_chapters + " / " + b.total_chapters + " 章（" + pct + "%）";
    $("book-blurb").textContent = b.blurb || b.synopsis || "";

    const plan = b.current_plan;
    $("plan").hidden = !plan;
    if (plan) {
      $("plan-title").textContent = "第 " + plan.chapter + " 章 · " + plan.title;
      $("plan-meta").textContent = [
        plan.location_time,
        plan.pov ? "视角：" + plan.pov : "",
        plan.characters && plan.characters.length ? "出场：" + plan.characters.join("、") : "",
      ].filter(Boolean).join(" · ");
      const ol = $("plan-events");
      ol.textContent = "";
      (plan.events || []).forEach((e) => ol.appendChild(el("li", "", e)));
      const hookEl = $("plan-hook");
      const hookText = String(plan.hook || "").trim();
      hookEl.hidden = !hookText;
      hookEl.textContent = hookText ? "章末钩子：" + hookText : "";
    }

    const chs = $("chapters");
    chs.textContent = "";
    $("chapters-empty").hidden = b.chapters.length > 0;
    b.chapters.forEach((c) => {
      const li = el("li");
      const btn = el("button");
      btn.type = "button";
      btn.appendChild(el("span", "", "第 " + c.chapter + " 章 " + c.title));
      btn.appendChild(el("span", "n mono", c.char_count + " 字"));
      btn.addEventListener("click", () => openChapter(c.chapter));
      li.appendChild(btn);
      chs.appendChild(li);
    });

    const cast = $("cast");
    cast.textContent = "";
    (b.characters || []).forEach((c) => {
      const card = el("div", "card");
      card.appendChild(el("h4", "", c.name));
      card.appendChild(el("p", "k", c.role));
      card.appendChild(el("p", "", c.personality));
      if (c.speech_style) card.appendChild(el("p", "", "说话方式：" + c.speech_style));
      cast.appendChild(card);
    });

    const items = $("items");
    items.textContent = "";
    (b.key_items || []).forEach((a) => {
      const card = el("div", "card");
      card.appendChild(el("h4", "", a.name));
      card.appendChild(el("p", "", a.details));
      if (a.condition) card.appendChild(el("p", "k", "当前状态：" + a.condition));
      items.appendChild(card);
    });

    const styles = $("styles");
    styles.textContent = "";
    (b.styles || []).forEach((s) => {
      const card = el("div", "card");
      card.appendChild(el("h4", "", "笔风 · " + s.name));
      card.appendChild(el("p", "", s.notes));
      styles.appendChild(card);
    });
  }

  async function loadShelf() {
    const data = await getJson(API.BOOKS);
    state.books = data.books || [];
    if (!state.current && state.books.length) {
      const active = state.books.find((b) => b.status === "ACTIVE") || state.books[0];
      state.current = active.book_id;
    }
    renderShelf();
  }

  async function openBook(bookId) {
    state.current = bookId;
    renderShelf();
    const data = await getJson(route(API.BOOK, { book_id: bookId }));
    state.book = data;
    renderBook();
  }

  async function openChapter(n) {
    const ch = await getJson(route(API.CHAPTER, { book_id: state.current, chapter: n }));
    state.readerChapter = n;
    $("book-panel").hidden = true;
    $("reader").hidden = false;
    $("reader-title").textContent = "第 " + ch.chapter + " 章 " + ch.title;
    const metaParts = ["《" + state.book.title + "》", ch.char_count + " 字"];
    if (ch.model) metaParts.push(ch.model);
    $("reader-meta").textContent = metaParts.join(" · ");

    const bodyEl = $("reader-body");
    bodyEl.textContent = "";
    String(ch.body || "")
      .split(/\n+/)
      .map((line) => line.trim())
      .filter(Boolean)
      .forEach((para) => bodyEl.appendChild(el("p", "", para)));

    const nums = state.book.chapters.map((c) => c.chapter);
    const idx = nums.indexOf(n);
    $("reader-prev").disabled = idx <= 0;
    $("reader-next").disabled = idx < 0 || idx >= nums.length - 1;
    $("reader").scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
  }

  function stepChapter(delta) {
    if (!state.book) return;
    const nums = state.book.chapters.map((c) => c.chapter);
    const idx = nums.indexOf(state.readerChapter);
    const target = nums[idx + delta];
    if (target !== undefined) openChapter(target);
  }

  async function refreshWorksAndMetrics(bookId) {
    const results = await Promise.allSettled([
      getJson(API.PIPELINE),
      loadShelf(),
    ]);
    if (results[0].status === "fulfilled" && results[0].value) {
      applyPipelineSnapshot(results[0].value);
    }
    if (bookId && state.current === bookId && !$("book-panel").hidden) {
      await openBook(bookId);
    }
  }

  async function syncFullState() {
    try {
      const results = await Promise.all([
        getJson(API.PIPELINE),
        loadShelf(),
      ]);
      applyPipelineSnapshot(results[0]);
      if (state.current && $("reader").hidden) {
        await openBook(state.current);
      }
    } catch (err) {
      setLiveStatus("warn", "同步状态失败：" + err.message);
    }
  }

  /* ------------------------------------------------------------------
   * SSE Stream with Automatic Reconnect + Authoritative Full State Resync
   * ------------------------------------------------------------------ */
  function scheduleReconnect() {
    if (state.sseReconnectTimer) return;
    const delay = state.sseReconnectDelayMs;
    state.sseReconnectDelayMs = Math.min(8000, Math.round(state.sseReconnectDelayMs * 1.5));
    state.sseReconnectTimer = window.setTimeout(() => {
      state.sseReconnectTimer = null;
      connectEvents();
    }, delay);
  }

  function connectEvents() {
    if (state.sse) {
      try { state.sse.close(); } catch (_err) {}
      state.sse = null;
    }
    const es = new EventSource(API.EVENTS);
    state.sse = es;

    const eventNames = [
      "chapter_started",
      "gate_passed",
      "chapter_stockpiled",
      "book_isolated",
      "pipeline_stage",
      "pipeline_token_usage",
      "pipeline_gate_report",
      "pipeline_model_event",
      "pipeline_chapter_locked",
    ];
    eventNames.forEach((name) => {
      es.addEventListener(name, (e) => {
        try {
          const evt = JSON.parse(e.data);
          handlePipelineEvent(evt);
        } catch (_err) {
          // ignore malformed frame
        }
      });
    });

    es.addEventListener("heartbeat", () => {
      if (state.sseDisconnected) {
        state.sseDisconnected = false;
        state.sseReconnectDelayMs = 1500;
        syncFullState();
      }
    });

    es.onopen = () => {
      const wasDown = state.sseDisconnected;
      state.sseDisconnected = false;
      state.sseReconnectDelayMs = 1500;
      if (wasDown) {
        // Reconnect rule: pull complete authoritative state first before continuing live stream
        syncFullState();
      } else if (state.pipeline) {
        if (state.pipeline.mode === "replay") {
          setLiveStatus("replay", "回放模式 · 真实历史事件");
        } else if (state.pipeline.state === "running" || state.pipeline.state === "rewriting") {
          setLiveStatus("live", "实时直播 · 运行中");
        } else {
          setLiveStatus("idle", "实时直播 · 待机");
        }
      }
    };

    es.onerror = () => {
      state.sseDisconnected = true;
      setLiveStatus("warn", "直播连接断开，正在重连…");
      if (es.readyState === EventSource.CLOSED) {
        scheduleReconnect();
      }
    };
  }

  /* ------------------------------------------------------------------
   * Controls & Event Listeners
   * ------------------------------------------------------------------ */
  function bindControls() {
    const motionBtn = $("btn-motion-toggle");
    if (motionBtn) {
      motionBtn.addEventListener("click", () => {
        state.motionPaused = !state.motionPaused;
        document.body.classList.toggle("motion-paused", state.motionPaused);
        motionBtn.textContent = state.motionPaused ? "恢复动效" : "暂停动效";
        ensureDotAnimationLoop();
      });
    }

    const autoBtn = $("btn-term-autoscroll");
    const termBody = $("terminal-body");
    const jumpBtn = $("btn-term-bottom");
    if (autoBtn) {
      autoBtn.classList.add("is-on");
      autoBtn.addEventListener("click", () => {
        state.autoScroll = !state.autoScroll;
        autoBtn.classList.toggle("is-on", state.autoScroll);
        autoBtn.textContent = state.autoScroll ? "锁定滚屏" : "已暂停滚屏";
        if (state.autoScroll && termBody) {
          termBody.scrollTop = termBody.scrollHeight;
          jumpBtn.hidden = true;
        }
      });
    }
    if (termBody && jumpBtn) {
      termBody.addEventListener("scroll", () => {
        const nearBottom = (termBody.scrollHeight - termBody.scrollTop - termBody.clientHeight) < 28;
        jumpBtn.hidden = nearBottom;
        if (!nearBottom && state.autoScroll) {
          state.autoScroll = false;
          if (autoBtn) {
            autoBtn.classList.remove("is-on");
            autoBtn.textContent = "已暂停滚屏";
          }
        }
      });
      jumpBtn.addEventListener("click", () => {
        state.autoScroll = true;
        if (autoBtn) {
          autoBtn.classList.add("is-on");
          autoBtn.textContent = "锁定滚屏";
        }
        termBody.scrollTop = termBody.scrollHeight;
        jumpBtn.hidden = true;
      });
    }

    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((t) => {
          const on = t === tab;
          t.classList.toggle("is-active", on);
          t.setAttribute("aria-selected", on ? "true" : "false");
        });
        document.querySelectorAll(".tab-panel").forEach((p) => {
          p.hidden = p.dataset.panel !== tab.dataset.tab;
        });
      });
    });

    document.querySelectorAll(".font-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".font-btn").forEach((b) => b.classList.toggle("is-active", b === btn));
        const prose = $("reader-body");
        if (prose) prose.className = "prose prose-" + (btn.dataset.font || "md");
      });
    });

    $("reader-back").addEventListener("click", () => renderBook());
    $("reader-prev").addEventListener("click", () => stepChapter(-1));
    $("reader-next").addEventListener("click", () => stepChapter(1));

    const stageEl = $("workflow-stage");
    if (typeof ResizeObserver !== "undefined" && stageEl) {
      const ro = new ResizeObserver(() => {
        if (state.pipeline) drawWorkflowEdges(state.pipeline);
      });
      ro.observe(stageEl);
    } else {
      window.addEventListener("resize", () => {
        if (state.pipeline) drawWorkflowEdges(state.pipeline);
      });
    }
  }

  bindControls();
  syncFullState().then(() => connectEvents());
})();
