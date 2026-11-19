(function () {
  "use strict";

  const API = Object.freeze({
    BOOKS: "/api/public/books",
    BOOK: "/api/public/books/{book_id}",
    CHAPTER: "/api/public/books/{book_id}/chapters/{chapter}",
    EVENTS: "/api/public/events/stream",
  });

  const state = { books: [], current: null, book: null, readerChapter: 0 };
  const $ = (id) => document.getElementById(id);

  function route(tpl, params) {
    let out = tpl;
    Object.keys(params).forEach((k) => { out = out.replace("{" + k + "}", encodeURIComponent(String(params[k]))); });
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

  function setLive(ok, text) {
    $("live-dot").classList.toggle("is-live", ok);
    $("live-state").textContent = text;
  }

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
      s.appendChild(el("span", "", "已成稿 " + b.written_chapters + " / " + b.total_chapters + " 章"));
      btn.appendChild(s);
      btn.addEventListener("click", () => openBook(b.book_id));
      li.appendChild(btn);
      list.appendChild(li);
    });
  }

  function renderBook() {
    const b = state.book;
    $("book-panel").hidden = false;
    $("reader").hidden = true;
    $("book-title").textContent = "《" + b.title + "》";
    $("book-meta").textContent = [b.genre, b.status_text].filter(Boolean).join(" · ");
    const pct = b.total_chapters ? Math.round((b.written_chapters / b.total_chapters) * 100) : 0;
    $("book-bar").style.width = pct + "%";
    $("book-progress").textContent = "已成稿 " + b.written_chapters + " / " + b.total_chapters + " 章（" + pct + "%）";
    $("book-blurb").textContent = b.blurb || b.synopsis || "";

    const plan = b.current_plan;
    $("plan").hidden = !plan;
    if (plan) {
      $("plan-title").textContent = "第 " + plan.chapter + " 章 · " + plan.title;
      $("plan-meta").textContent = [plan.location_time, plan.pov ? "视角：" + plan.pov : "", plan.characters.length ? "出场：" + plan.characters.join("、") : ""].filter(Boolean).join(" · ");
      const ol = $("plan-events");
      ol.textContent = "";
      plan.events.forEach((e) => ol.appendChild(el("li", "", e)));
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
      btn.appendChild(el("span", "n", c.char_count + " 字"));
      btn.addEventListener("click", () => openChapter(c.chapter));
      li.appendChild(btn);
      chs.appendChild(li);
    });

    const cast = $("cast");
    cast.textContent = "";
    b.characters.forEach((c) => {
      const card = el("div", "card");
      card.appendChild(el("h4", "", c.name));
      card.appendChild(el("p", "k", c.role));
      card.appendChild(el("p", "", c.personality));
      if (c.speech_style) card.appendChild(el("p", "", "说话方式：" + c.speech_style));
      cast.appendChild(card);
    });
    const items = $("items");
    items.textContent = "";
    b.key_items.forEach((a) => {
      const card = el("div", "card");
      card.appendChild(el("h4", "", a.name));
      card.appendChild(el("p", "", a.details));
      if (a.condition) card.appendChild(el("p", "k", "当前状态：" + a.condition));
      items.appendChild(card);
    });
    const styles = $("styles");
    styles.textContent = "";
    b.styles.forEach((s) => {
      const card = el("div", "card");
      card.appendChild(el("h4", "", "笔风 · " + s.name));
      card.appendChild(el("p", "", s.notes));
      styles.appendChild(card);
    });
  }

  function showError(where, err) {
    setLive(false, where + "失败：" + err.message);
  }

  async function loadShelf() {
    const data = await getJson(API.BOOKS);
    state.books = data.books;
    if (!state.current && state.books.length) {
      const active = state.books.find((b) => b.status === "ACTIVE") || state.books[0];
      state.current = active.book_id;
    }
    renderShelf();
  }

  async function openBook(bookId) {
    state.current = bookId;
    renderShelf();
    try {
      state.book = await getJson(route(API.BOOK, { book_id: bookId }));
      renderBook();
    } catch (err) {
      showError("读取书目", err);
    }
  }

  async function openChapter(n) {
    try {
      const ch = await getJson(route(API.CHAPTER, { book_id: state.current, chapter: n }));
      state.readerChapter = n;
      $("book-panel").hidden = true;
      $("reader").hidden = false;
      $("reader-title").textContent = "第 " + ch.chapter + " 章 " + ch.title;
      $("reader-meta").textContent = "《" + state.book.title + "》 · " + ch.char_count + " 字";
      $("reader-body").textContent = ch.body;
      const nums = state.book.chapters.map((c) => c.chapter);
      const idx = nums.indexOf(n);
      $("reader-prev").disabled = idx <= 0;
      $("reader-next").disabled = idx < 0 || idx >= nums.length - 1;
      window.scrollTo(0, 0);
    } catch (err) {
      showError("读取章节", err);
    }
  }

  function stepChapter(delta) {
    const nums = state.book.chapters.map((c) => c.chapter);
    const idx = nums.indexOf(state.readerChapter);
    const target = nums[idx + delta];
    if (target !== undefined) openChapter(target);
  }

  function pushFeed(evt) {
    const feed = $("feed");
    $("feed-empty").hidden = true;
    Array.prototype.forEach.call(feed.children, (li) => li.classList.remove("is-new"));
    const li = el("li", "is-new");
    const t = el("time", "", (evt.timestamp || "").replace("T", " ").slice(0, 19));
    li.appendChild(t);
    li.appendChild(document.createTextNode(evt.message));
    feed.insertBefore(li, feed.firstChild);
    while (feed.children.length > 50) feed.removeChild(feed.lastChild);
  }

  async function refreshAfterEvent(evt) {
    await loadShelf();
    if (state.current === evt.book_id && !$("book-panel").hidden) {
      await openBook(evt.book_id);
    }
  }

  function connectEvents() {
    const es = new EventSource(API.EVENTS);
    const handler = (e) => {
      const evt = JSON.parse(e.data);
      pushFeed(evt);
      refreshAfterEvent(evt).catch((err) => showError("刷新", err));
    };
    ["chapter_started", "gate_passed", "chapter_stockpiled", "book_isolated"].forEach((name) => es.addEventListener(name, handler));
    es.addEventListener("heartbeat", () => setLive(true, "实时直播中"));
    es.onopen = () => setLive(true, "实时直播中");
    es.onerror = () => setLive(false, "直播连接断开，浏览器正在重连…");
  }

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => {
        const on = t === tab;
        t.classList.toggle("is-active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
      document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = p.dataset.panel !== tab.dataset.tab; });
    });
  });
  $("reader-back").addEventListener("click", () => renderBook());
  $("reader-prev").addEventListener("click", () => stepChapter(-1));
  $("reader-next").addEventListener("click", () => stepChapter(1));

  loadShelf()
    .then(() => (state.current ? openBook(state.current) : null))
    .catch((err) => showError("读取书架", err));
  connectEvents();
})();
