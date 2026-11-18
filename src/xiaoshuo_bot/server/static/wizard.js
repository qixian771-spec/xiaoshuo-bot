(function () {
  "use strict";

  const TOKEN_KEY = "xiaoshuo_admin_token";
  const API = Object.freeze({
    HEALTH: "/api/health",
    SESSIONS: "/api/wizard/sessions",
    ANSWER: "/api/wizard/sessions/{session_id}/answer",
    CONFIRM: "/api/wizard/sessions/{session_id}/confirm",
  });
  const FIELD_NAMES = {
    logline: "梗概", genre_id: "类目", selling_point: "卖点", tone: "基调",
    persona: "性格处境", golden_finger: "金手指", cost: "代价", arc: "成长线",
    description: "叙事特点", sample: "示范段落",
    title: "书名", author_level: "篇幅", target_chapters: "章数", blurb: "简介",
    custom: "老板原话",
  };
  const LEVEL_TEXT = { lv0_1: "紧凑完本", lv2_plus: "长篇连载" };

  const $ = (id) => document.getElementById(id);
  let token = "";
  let session = null;

  try { token = window.sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { token = ""; }

  function route(tpl, params) {
    let out = tpl;
    Object.keys(params).forEach((k) => { out = out.replace("{" + k + "}", encodeURIComponent(String(params[k]))); });
    return out;
  }

  function show(id) {
    ["login-box", "idea-box", "step-box", "confirm-box", "done-box"].forEach((b) => { $(b).hidden = b !== id; });
  }

  function setBusy(on, text) {
    $("busy").hidden = !on;
    if (text) $("busy").textContent = text;
    document.querySelectorAll("button").forEach((b) => { b.disabled = on; });
  }

  function setError(msg) {
    $("error").hidden = !msg;
    $("error").textContent = msg || "";
  }

  async function api(url, body) {
    const headers = { "Content-Type": "application/json", Accept: "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    const resp = await fetch(url, { method: body === undefined ? "GET" : "POST", headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await resp.json();
    if (resp.status === 401 || resp.status === 403) {
      show("login-box");
      throw new Error("令牌无效或已过期，请重新登录");
    }
    if (!resp.ok) throw new Error((data && data.message) || ("HTTP " + resp.status));
    return data;
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function fieldValue(k, v) {
    if (k === "author_level") return LEVEL_TEXT[v] || v;
    if (k === "target_chapters") return v ? v + " 章" : "连载不定";
    return v;
  }

  function optionBlock(opt) {
    const dl = el("dl");
    Object.keys(opt).forEach((k) => {
      if (k === "label" || !FIELD_NAMES[k]) return;
      dl.appendChild(el("dt", "", FIELD_NAMES[k]));
      dl.appendChild(el("dd", "", fieldValue(k, opt[k])));
    });
    return dl;
  }

  function render() {
    if (session.status === "IN_PROGRESS") {
      show("step-box");
      $("step-progress").textContent = "第 " + (session.step_index + 1) + " / " + session.total_steps + " 步 · " + session.step.title;
      $("step-question").textContent = session.step.question;
      const box = $("options");
      box.textContent = "";
      session.options.forEach((opt, i) => {
        const b = el("button", "option");
        b.type = "button";
        b.appendChild(el("span", "l", opt.label));
        b.appendChild(optionBlock(opt));
        b.addEventListener("click", () => answer({ choice: i }));
        box.appendChild(b);
      });
      $("custom-form").hidden = !session.step.allow_custom || session.step.key === "direction";
      $("custom").value = "";
    } else if (session.status === "READY_TO_CONFIRM") {
      show("confirm-box");
      const ol = $("summary");
      ol.textContent = "";
      session.choices.forEach((c) => {
        const li = el("li");
        li.appendChild(el("b", "", c.option.label + "："));
        const parts = Object.keys(c.option).filter((k) => k !== "label" && FIELD_NAMES[k]).map((k) => FIELD_NAMES[k] + " " + fieldValue(k, c.option[k]));
        li.appendChild(document.createTextNode(parts.join("；")));
        ol.appendChild(li);
      });
    } else if (session.status === "CREATED") {
      show("done-box");
      $("done-text").textContent = "新书已立项（编号 " + session.book_id + "），机器人会自动一章一章写下去。";
    }
  }

  async function run(fn, busyText) {
    setError("");
    setBusy(true, busyText);
    try {
      session = await fn();
      render();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function answer(body) {
    return run(() => api(route(API.ANSWER, { session_id: session.session_id }), body), "机器人正在想下一步的方案…");
  }

  $("login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    token = $("login-token").value.trim();
    try { window.sessionStorage.setItem(TOKEN_KEY, token); } catch (err) { setError("浏览器不允许保存登录状态：" + err.message); }
    show("idea-box");
  });
  $("idea-form").addEventListener("submit", (e) => {
    e.preventDefault();
    run(() => api(API.SESSIONS, { idea: $("idea").value }), "机器人正在想 3 个故事方向…");
  });
  $("reroll").addEventListener("click", () => answer({ reroll: true }));
  $("custom-form").addEventListener("submit", (e) => {
    e.preventDefault();
    answer({ custom: $("custom").value });
  });
  $("confirm-form").addEventListener("submit", (e) => {
    e.preventDefault();
    run(() => api(route(API.CONFIRM, { session_id: session.session_id }), { pen_name: $("pen-name").value }), "机器人正在生成整本书的设定和大纲，大约需要一两分钟…");
  });

  fetch(API.HEALTH)
    .then((r) => r.json())
    .then((h) => { show(h.admin_auth_required && !token ? "login-box" : "idea-box"); })
    .catch((err) => setError("无法连接服务器：" + err.message));
})();
