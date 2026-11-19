from __future__ import annotations

from pathlib import Path
import re
import unittest

from xiaoshuo_bot import contracts


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "xiaoshuo_bot" / "server" / "static"
INDEX_HTML = STATIC_DIR / "index.html"
APP_JS = STATIC_DIR / "app.js"
STYLES_CSS = STATIC_DIR / "styles.css"


class TestWebStaticContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index_text = INDEX_HTML.read_text(encoding="utf-8")
        cls.app_text = APP_JS.read_text(encoding="utf-8")
        cls.css_text = STYLES_CSS.read_text(encoding="utf-8")

    def test_static_assets_exist_and_non_empty(self) -> None:
        for path in (INDEX_HTML, APP_JS, STYLES_CSS):
            self.assertTrue(path.is_file(), f"Missing static asset: {path}")
            self.assertGreater(path.stat().st_size, 100, f"Static asset too small: {path}")

    def test_all_api_routes_in_app_js_match_contracts(self) -> None:
        found_routes = set(re.findall(r"""["'](/api/[a-zA-Z0-9_{}/\-]+)["']""", self.app_text))
        self.assertGreaterEqual(
            len(found_routes),
            20,
            f"Expected at least 20 /api/... routes in app.js, found {len(found_routes)}",
        )
        contract_routes = set(contracts.ALL_API_ROUTES)
        unknown_routes = found_routes - contract_routes
        self.assertEqual(
            unknown_routes,
            set(),
            f"Found /api/... routes in app.js not declared in contracts.ALL_API_ROUTES: {sorted(unknown_routes)}",
        )

        # Also check index.html has zero stray /api/ references outside contracts
        html_routes = set(re.findall(r"""["'](/api/[a-zA-Z0-9_{}/\-]+)["']""", self.index_text))
        self.assertEqual(
            html_routes - contract_routes,
            set(),
            f"Found unknown /api/... routes in index.html: {sorted(html_routes - contract_routes)}",
        )

        required_console_routes = {
            contracts.API_HEALTH,
            contracts.API_GENRES,
            contracts.API_BOOKS,
            contracts.API_BOOKS_AUTO_CREATE,
            contracts.API_BOOK_STATUS,
            contracts.API_BOOK_CHAPTERS,
            contracts.API_BOOK_CHAPTER_DETAIL,
            contracts.API_BOOK_CHAPTER_LOCK,
            contracts.API_BOOK_EXPORT,
            contracts.API_BOOK_COVER,
            contracts.API_BOOK_CUT,
            contracts.API_BOOK_PROMOTE,
            contracts.API_BOOK_EXTEND,
            contracts.API_BOOK_RESUME,
            contracts.API_CPA_AUTH_FILES,
            contracts.API_CPA_AUTH_FILE_DETAIL,
            contracts.API_CPA_MODELS,
            contracts.API_CPA_USAGE,
            contracts.API_CPA_OAUTH,
            contracts.API_CPA_BINDINGS,
            contracts.API_COMPANION_RESUME,
            contracts.API_EVENTS_STREAM,
        }
        missing = required_console_routes - found_routes
        self.assertEqual(missing, set(), f"Console app.js missing required API routes: {sorted(missing)}")

    def test_all_sse_events_match_contracts(self) -> None:
        sse_block_match = re.search(
            r"const\s+SSE_EVENTS\s*=\s*Object\.freeze\(\{(.*?)\}\);",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(sse_block_match, "SSE_EVENTS constant block missing in app.js")
        block = sse_block_match.group(1)
        js_events = set(re.findall(r':\s*"([a-z0-9_]+)"', block))
        contract_events = set(contracts.ALL_SSE_EVENTS)

        self.assertEqual(
            js_events - contract_events,
            set(),
            f"Unknown SSE events in app.js: {sorted(js_events - contract_events)}",
        )
        self.assertEqual(
            contract_events - js_events,
            set(),
            f"Missing SSE events in app.js: {sorted(contract_events - js_events)}",
        )

        # Ensure every SSE_EVENTS key has a registered handler in SSE_HANDLERS
        for key in re.findall(r"([A-Z0-9_]+)\s*:\s*\"[a-z0-9_]+\"", block):
            self.assertIn(
                f"SSE_HANDLERS[SSE_EVENTS.{key}]",
                self.app_text,
                f"Missing SSE_HANDLERS registration for SSE_EVENTS.{key}",
            )

    def test_zero_external_cdn_or_http_links(self) -> None:
        forbidden_patterns = (
            re.compile(r"https?://", re.IGNORECASE),
            re.compile(r"//cdn", re.IGNORECASE),
            re.compile(r"unpkg\.com", re.IGNORECASE),
            re.compile(r"jsdelivr\.net", re.IGNORECASE),
            re.compile(r"fonts\.googleapis\.com", re.IGNORECASE),
            re.compile(r"fonts\.gstatic\.com", re.IGNORECASE),
            re.compile(r"@import\s+url\(", re.IGNORECASE),
        )
        for name, content in (
            ("index.html", self.index_text),
            ("styles.css", self.css_text),
            ("app.js", self.app_text),
        ):
            for pat in forbidden_patterns:
                m = pat.search(content)
                self.assertIsNone(
                    m,
                    f"Forbidden external link/CDN pattern {pat.pattern!r} found in {name}: {m.group(0) if m else ''}",
                )

    def test_xss_escaping_and_token_transport_security(self) -> None:
        # 1. escapeHtml must be defined and cover &, <, >, ", '
        self.assertRegex(self.app_text, r"function\s+escapeHtml\s*\(")
        for char_entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(char_entity, self.app_text)

        # 2. escapeHtml must actually be used across dynamic HTML rendering
        escape_calls = len(re.findall(r"\bescapeHtml\s*\(", self.app_text))
        self.assertGreaterEqual(escape_calls, 30, "escapeHtml must guard dynamic HTML interpolations")

        # 3. Every .innerHTML assignment (single-line or multi-line) that concatenates
        # dynamic expressions outside string literals must be protected by escapeHtml().
        string_lit_re = re.compile(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*""" + '"')
        statements: list[tuple[int, str]] = []
        app_text = self.app_text
        idx = 0
        while True:
            pos = app_text.find(".innerHTML", idx)
            if pos == -1:
                break
            after = app_text[pos + len(".innerHTML") :]
            eq_match = re.match(r"\s*=\s*", after)
            if not eq_match:
                idx = pos + len(".innerHTML")
                continue
            start_rhs = pos + len(".innerHTML") + eq_match.end()
            lineno = app_text.count("\n", 0, pos) + 1
            # Scan forward respecting quotes and paren/brace depth until terminating ';'
            cur = start_rhs
            depth = 0
            in_quote: str | None = None
            escaped = False
            while cur < len(app_text):
                ch = app_text[cur]
                if in_quote is not None:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == in_quote:
                        in_quote = None
                else:
                    if ch in ("'", '"'):
                        in_quote = ch
                    elif ch in ("(", "[", "{"):
                        depth += 1
                    elif ch in (")", "]", "}"):
                        depth = max(0, depth - 1)
                    elif ch == ";" and depth == 0:
                        break
                cur += 1
            rhs = app_text[start_rhs:cur].strip()
            statements.append((lineno, rhs))
            idx = cur + 1

        self.assertGreaterEqual(len(statements), 20, "Expected >=20 .innerHTML assignments in app.js")

        # Pre-escaped helper variables whose own definitions must contain escapeHtml(
        preescaped_helpers = {
            "html": r"const\s+html\s*=\s*filtered\.map\(.*?\)\.join\(\"\"\);",
            "diffHtml": r"const\s+diffHtml\s*=\s*\(ev\.before \|\| ev\.after\).*?: \"\";",
            "optionsHtml": r"const\s+optionsHtml\s*=\s*\['<option value=\"\">.*?\)\.join\(\"\"\);",
        }
        for helper_name, helper_pat in preescaped_helpers.items():
            m_helper = re.search(helper_pat, app_text, re.DOTALL)
            self.assertIsNotNone(m_helper, f"Missing pre-escaped helper definition for {helper_name}")
            self.assertIn(
                "escapeHtml(",
                m_helper.group(0),
                f"Helper {helper_name} used in innerHTML must call escapeHtml()",
            )

        for lineno, rhs in statements:
            outside_literals = string_lit_re.sub("", rhs).strip()
            if not outside_literals or re.fullmatch(r"[+\s]+", outside_literals):
                continue
            if "escapeHtml(" in rhs:
                continue
            # Only allow pure identifier `html` (verified above) or pre-escaped defaultOpt+books.map
            if outside_literals == "html":
                continue
            if "defaultOpt + state.books.map" in rhs:
                # Verify the map callback itself uses escapeHtml
                self.assertIn("escapeHtml(b.book_id)", rhs)
                continue
            self.fail(f"Potential unescaped innerHTML assignment at app.js:{lineno}: {rhs[:140]}")

        # 4. Zero token-in-URL patterns and zero EventSource usage
        forbidden_auth_patterns = (
            re.compile(r"\?token=", re.IGNORECASE),
            re.compile(r"access_token=", re.IGNORECASE),
            re.compile(r"\bnew\s+EventSource\b"),
            re.compile(r"localStorage", re.IGNORECASE),
        )
        for pat in forbidden_auth_patterns:
            self.assertIsNone(
                pat.search(self.app_text),
                f"Forbidden auth/SSE transport pattern {pat.pattern!r} found in app.js",
            )

        # 5. Must use sessionStorage + Authorization Bearer + ReadableStream SSE
        self.assertIn("sessionStorage", self.app_text)
        self.assertIn('"Bearer "', self.app_text)
        self.assertIn("getReader()", self.app_text)

    def test_cpa_usage_parses_backend_ledger_schema(self) -> None:
        m = re.search(
            r"async\s+function\s+loadCpaUsage\s*\(\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "loadCpaUsage() missing in app.js")
        body = m.group(1)
        for token in (
            "usage.ledger",
            "usage.cpa_usage",
            "request_count",
            "input_tokens",
            "output_tokens",
            "estimated_cost_usd",
        ):
            self.assertIn(token, body, f"loadCpaUsage() must support backend field {token!r}")

    def test_export_buttons_only_use_supported_platforms(self) -> None:
        html_platforms = set(re.findall(r'data-platform="([^"]+)"', self.index_text))
        supported = {"tomato", "qimao", "zhihu"}
        self.assertEqual(
            html_platforms,
            supported,
            f"index.html export buttons must match backend-supported platforms {sorted(supported)}, got {sorted(html_platforms)}",
        )
        self.assertIn('["tomato", "qimao", "zhihu"]', self.app_text)

    def test_reader_book_switch_and_critic_and_cushion_contracts(self) -> None:
        # 1. Reader resets stale chapter selection & lock button when switching books
        m_reset = re.search(
            r"function\s+resetReaderChapterSelection\s*\(\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_reset, "resetReaderChapterSelection() missing in app.js")
        reset_body = m_reset.group(1)
        self.assertIn("state.readerSelectedChapter = null", reset_body)
        self.assertIn("state.readerChapterDetail = null", reset_body)
        self.assertIn("lockBtn.disabled = true", reset_body)
        self.assertIn("noteBox.hidden = true", reset_body)

        m_load_ch = re.search(
            r"async\s+function\s+loadBookChapters\s*\(bookId\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_load_ch, "loadBookChapters() missing in app.js")
        self.assertIn("resetReaderChapterSelection()", m_load_ch.group(1))

        # 2. handleBookAction("read") should not duplicate loadBookChapters() call
        m_action = re.search(
            r"async\s+function\s+handleBookAction\s*\(action,\s*bookId\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_action, "handleBookAction() missing in app.js")
        read_branch = m_action.group(1).split('if (action === "detail")')[0]
        self.assertNotIn(
            "loadBookChapters(",
            read_branch,
            'handleBookAction("read") must rely on switchView("reader") without duplicate loadBookChapters()',
        )

        # 3. SSE CRITIC_SCORED handles overall_score and weak_paragraphs from engine.py
        m_critic = re.search(
            r"SSE_HANDLERS\[SSE_EVENTS\.CRITIC_SCORED\]\s*=\s*function\s*\(payload\)\s*\{(.*?)\n  \};",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_critic, "SSE_HANDLERS[SSE_EVENTS.CRITIC_SCORED] missing in app.js")
        self.assertIn("overall_score", m_critic.group(1))
        self.assertIn("weak_paragraphs", m_critic.group(1))

        # 4. Critical cushion (<6 & ACTIVE) separated from <10 low_cushion_alert
        m_crit_fn = re.search(
            r"function\s+isCriticalCushionBook\s*\(book\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_crit_fn, "isCriticalCushionBook() missing in app.js")
        self.assertIn('book.status === "ACTIVE"', m_crit_fn.group(1))
        self.assertIn("< 6", m_crit_fn.group(1))

    def test_five_views_and_cpa_roles_present(self) -> None:
        required_ids = (
            "view-overview",
            "view-create",
            "view-cpa",
            "view-live",
            "view-reader",
            "captcha-global-banner",
            "cpa-dropzone",
            "cpa-bindings-form",
            "live-stream-feed",
            "reader-chapter-list",
            "reader-author-note-box",
            "auth-modal",
            "cut-modal",
        )
        for el_id in required_ids:
            self.assertIn(f'id="{el_id}"', self.index_text, f"Missing element id={el_id!r} in index.html")

        for role in ("planner", "writer", "critic", "polisher", "image"):
            self.assertIn(f'role: "{role}"', self.app_text, f"Missing CPA role {role!r} in app.js")

    def test_toggle_chapter_lock_async_race_guard(self) -> None:
        m_toggle = re.search(
            r"async\s+function\s+toggleChapterLock\s*\(\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_toggle, "toggleChapterLock() missing in app.js")
        body = m_toggle.group(1)

        # Captures targetBookId and targetChapter before any await
        self.assertIn("const targetBookId = state.readerBookId;", body)
        self.assertIn("const targetChapter = state.readerSelectedChapter;", body)
        self.assertLess(body.index("targetBookId"), body.index("await "))
        self.assertLess(body.index("targetChapter"), body.index("await "))

        # Disables #btn-toggle-lock while in flight
        self.assertIn("lockBtn.disabled = true;", body)

        # Guards before loadChapterDetail and restores disabled only if still on target
        self.assertIn("state.readerBookId !== targetBookId || state.readerSelectedChapter !== targetChapter", body)
        self.assertIn("loadChapterDetail(targetBookId, targetChapter)", body)
        self.assertIn("state.readerBookId === targetBookId && state.readerSelectedChapter === targetChapter", body)

        # loadBookChapters and loadChapterDetail must also discard stale responses after await
        m_load_ch = re.search(
            r"async\s+function\s+loadBookChapters\s*\(bookId\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_load_ch)
        self.assertIn("state.readerBookId !== bookId", m_load_ch.group(1))

        m_detail = re.search(
            r"async\s+function\s+loadChapterDetail\s*\(bookId,\s*chapterNo\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_detail)
        self.assertIn("state.readerBookId !== bookId || state.readerSelectedChapter !== targetChapter", m_detail.group(1))

    def test_cover_preview_avoids_doomed_404_and_uses_ai_image_without_fallback(self) -> None:
        # Zero silent fallback to procedural in frontend
        self.assertNotIn("procedural", self.app_text, "Frontend must not silently fallback ai_image to procedural")

        m_preview = re.search(
            r"async\s+function\s+renderBookPreviewInto\s*\(book,\s*coverContainer,\s*titlesContainer\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_preview, "renderBookPreviewInto() missing in app.js")
        preview_body = m_preview.group(1)
        self.assertIn("if (!book.cover_path)", preview_body)
        self.assertIn("尚未生成封面", preview_body)
        self.assertIn("🎨 生成 AI 封面", preview_body)
        # Must bail before fetchAuthenticatedImageUrl when cover_path is absent
        self.assertLess(preview_body.index("if (!book.cover_path)"), preview_body.index("fetchAuthenticatedImageUrl("))

        m_gen = re.search(
            r"async\s+function\s+generateAiCoverForBook\s*\(book,\s*coverContainer,\s*titlesContainer\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_gen, "generateAiCoverForBook() missing in app.js")
        gen_body = m_gen.group(1)
        self.assertIn("API_ROUTES.BOOK_COVER", gen_body)
        self.assertIn('method: "POST"', gen_body)
        self.assertIn('base_mode: "ai_image"', gen_body)
        self.assertIn("book.cover_path =", gen_body)
        self.assertIn("请先在 CPA 面板配置 image 生图模型后再生成封面", gen_body)
        self.assertIn("🎨 重试生成 AI 封面", gen_body)

        m_create = re.search(
            r"async\s+function\s+handleAutoCreateSubmit\s*\(evt\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_create, "handleAutoCreateSubmit() missing in app.js")
        create_body = m_create.group(1)
        self.assertIn("!createdBook.cover_path", create_body)
        self.assertIn("generateAiCoverForBook(", create_body)

    def test_pre_login_auth_deferral_and_toast_cleanup(self) -> None:
        m_init = re.search(
            r"async\s+function\s+init\s*\(\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_init, "init() missing in app.js")
        init_body = m_init.group(1)
        self.assertIn("await loadHealth()", init_body)
        self.assertIn("admin_auth_required !== false", init_body)
        self.assertIn("!state.authToken", init_body)
        self.assertIn("state.authRequired = true", init_body)
        # loadHealth and auth guard must run before refreshAll and connectSse
        self.assertLess(init_body.index("await loadHealth()"), init_body.index("refreshAll("))
        self.assertLess(init_body.index("if (!state.authToken"), init_body.index("connectSse()"))

        m_refresh = re.search(
            r"async\s+function\s+refreshAll\s*\(opts\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_refresh, "refreshAll() missing in app.js")
        self.assertIn("admin_auth_required !== false", m_refresh.group(1))

        # #btn-save-token clears stale error toasts
        self.assertIn("clearErrorToasts()", self.app_text)
        m_clear = re.search(
            r"function\s+clearErrorToasts\s*\(\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_clear, "clearErrorToasts() missing in app.js")
        self.assertIn(".toast.is-err", m_clear.group(1))

    def test_cpa_bind_provider_select_css_prevents_clipping(self) -> None:
        m_sel = re.search(r"\.bind-provider-select\s*\{([^}]*)\}", self.css_text, re.DOTALL)
        self.assertIsNotNone(m_sel, ".bind-provider-select rule missing in styles.css")
        m_minw = re.search(r"min-width\s*:\s*(\d+)px", m_sel.group(1))
        self.assertIsNotNone(m_minw, ".bind-provider-select must define min-width in px")
        self.assertGreaterEqual(int(m_minw.group(1)), 138)
        self.assertIn("#cpa-bindings-form .binding-fields", self.css_text)
        self.assertIn("#cpa-bindings-form .input-group", self.css_text)

    def test_p1_1_admin_hides_racing_matrix_and_companion_entries_while_keeping_core_entries(self) -> None:
        from html.parser import HTMLParser

        void_tags = {
            "area", "base", "br", "col", "embed", "hr", "img",
            "input", "link", "meta", "param", "source", "track", "wbr",
        }

        class _VisibilityParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.stack: list[tuple[str, bool]] = []
                self.in_script_or_style = False
                self.elements: list[dict[str, object]] = []
                self.visible_texts: list[str] = []

            @property
            def currently_hidden(self) -> bool:
                return any(is_h for _, is_h in self.stack)

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                attr_map = {k: ("" if v is None else v) for k, v in attrs}
                own_hidden = "hidden" in attr_map
                eff_hidden = self.currently_hidden or own_hidden
                self.elements.append(
                    {"tag": tag, "attrs": attr_map, "own_hidden": own_hidden, "hidden": eff_hidden}
                )
                if tag in ("script", "style"):
                    self.in_script_or_style = True
                if tag not in void_tags:
                    self.stack.append((tag, own_hidden))

            def handle_endtag(self, tag: str) -> None:
                if tag in ("script", "style"):
                    self.in_script_or_style = False
                for i in range(len(self.stack) - 1, -1, -1):
                    if self.stack[i][0] == tag:
                        del self.stack[i:]
                        break

            def handle_data(self, data: str) -> None:
                if not self.in_script_or_style and not self.currently_hidden:
                    text = data.strip()
                    if text:
                        self.visible_texts.append(text)

        parser = _VisibilityParser()
        parser.feed(self.index_text)

        def _find_one(**criteria: str) -> dict[str, object]:
            matches = [
                el
                for el in parser.elements
                if all(
                    (el["tag"] == val if key == "tag" else el["attrs"].get(key) == val)
                    for key, val in criteria.items()
                )
            ]
            self.assertEqual(len(matches), 1, f"Expected exactly 1 element for {criteria}, found {len(matches)}")
            return matches[0]

        # 1. Racing-matrix and publishing-companion entries MUST be hidden (DOM preserved, UI hidden)
        hidden_targets = (
            {"tag": "button", "data-view": "create"},
            {"id": "btn-goto-create"},
            {"id": "kpi-card-low-cushion"},
            {"tag": "button", "data-filter": "LOW_CUSHION"},
            {"tag": "button", "data-filter": "PAUSED_AUTH_CAPTCHA"},
            {"tag": "button", "data-filter": "CUT_LOSS"},
            {"id": "captcha-global-banner"},
            {"id": "view-create"},
            {"id": "cut-modal"},
            {"id": "create-account-id"},
        )
        for target in hidden_targets:
            el = _find_one(**target)
            self.assertTrue(el["hidden"], f"Expected {target} to be hidden in /admin/ index.html")

        # 2. Core entries MUST stay visible (CPA config, Wizard, Public studio, Live, Reader/Export)
        visible_targets = (
            {"tag": "button", "data-view": "overview"},
            {"tag": "button", "data-view": "cpa"},
            {"tag": "button", "data-view": "live"},
            {"tag": "button", "data-view": "reader"},
            {"tag": "a", "href": "/admin/wizard"},
            {"tag": "a", "href": "/"},
            {"id": "kpi-today-tokens"},
            {"id": "kpi-production"},
            {"id": "kpi-paused-isolated"},
            {"tag": "button", "data-filter": "all"},
            {"tag": "button", "data-filter": "ACTIVE"},
            {"tag": "button", "data-filter": "ISOLATED_GATE_FAILURE"},
        )
        for target in visible_targets:
            el = _find_one(**target)
            self.assertFalse(el["hidden"], f"Expected core entry {target} to stay visible in /admin/ index.html")

        # 3. No racing-matrix / companion wording in visible index.html text
        visible_blob = "\n".join(parser.visible_texts)
        for forbidden_term in (
            "番茄爆款矩阵",
            "安全垫",
            "伴侣",
            "马甲",
            "止损切书",
            "三源爆款立项",
            "三源自动立项",
            "验证码挂起",
        ):
            self.assertNotIn(
                forbidden_term,
                visible_blob,
                f"Forbidden racing-matrix/companion term {forbidden_term!r} still visible in index.html",
            )

        # 4. CSS [hidden] rule must enforce display: none !important so flex/grid components cannot override it
        self.assertRegex(self.css_text, r"\[hidden\]\s*\{\s*display\s*:\s*none\s*!important\s*;")

    def test_p1_1_app_js_enforces_private_studio_mode_gates(self) -> None:
        self.assertRegex(self.app_text, r"\bconst\s+PRIVATE_STUDIO_MODE\s*=\s*true\s*;")

        m_meta = re.search(
            r"const\s+VIEW_META\s*=\s*Object\.freeze\(\{(.*?)\}\);",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_meta, "VIEW_META missing in app.js")
        m_ov_meta = re.search(r"overview\s*:\s*\{([^}]*)\}", m_meta.group(1), re.DOTALL)
        self.assertIsNotNone(m_ov_meta, "VIEW_META.overview missing in app.js")
        for forbidden in ("矩阵", "安全垫", "马甲"):
            self.assertNotIn(forbidden, m_ov_meta.group(1))

        m_switch = re.search(
            r"function\s+switchView\s*\(viewName\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_switch, "switchView() missing in app.js")
        self.assertIn('if (PRIVATE_STUDIO_MODE && viewName === "create")', m_switch.group(1))
        self.assertIn('viewName = "overview";', m_switch.group(1))
        self.assertIn("document.title =", m_switch.group(1))

        m_captcha = re.search(
            r"function\s+renderCaptchaBanner\s*\(\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_captcha, "renderCaptchaBanner() missing in app.js")
        self.assertIn("if (PRIVATE_STUDIO_MODE)", m_captcha.group(1))
        self.assertLess(
            m_captcha.group(1).index("if (PRIVATE_STUDIO_MODE)"),
            m_captcha.group(1).index("banner.hidden = false;"),
        )

        m_grid = re.search(
            r"function\s+renderBooksGrid\s*\(\)\s*\{(.*?)\n  \}",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_grid, "renderBooksGrid() missing in app.js")
        grid_body = m_grid.group(1)
        self.assertIn('PRIVATE_STUDIO_MODE ? "" : \'<button type="button" class="btn btn-xs btn-ghost" data-book-action="promote"', grid_body)
        self.assertIn('PRIVATE_STUDIO_MODE ? "" : \'<button type="button" class="btn btn-xs btn-danger" data-book-action="cut"', grid_body)
        self.assertIn("!PRIVATE_STUDIO_MODE && book.account_id", grid_body)
        self.assertIn('data-book-action="read"', grid_body)
        self.assertIn('data-book-action="detail"', grid_body)
        self.assertIn('data-book-action="extend"', grid_body)

    def test_p1_2_reusable_model_quota_alert_and_paused_manual_ui_contracts(self) -> None:
        # 1. Prominent alert region & distinct filter chips in index.html
        self.assertIn('id="model-quota-alert-region"', self.index_text)
        self.assertIn('data-filter="PAUSED_MANUAL"', self.index_text)
        self.assertIn('data-filter="PAUSED_QUOTA_EXHAUSTED"', self.index_text)

        # 2. Reusable alert kinds support BOTH quota_exhausted and model_switched
        self.assertIn('QUOTA_EXHAUSTED: "quota_exhausted"', self.app_text)
        self.assertIn('MODEL_SWITCHED: "model_switched"', self.app_text)
        self.assertRegex(self.app_text, r"function\s+upsertModelQuotaAlert\s*\(")
        self.assertRegex(self.app_text, r"function\s+renderModelQuotaAlerts\s*\(")
        self.assertRegex(self.app_text, r"function\s+syncQuotaAlertsFromBooks\s*\(")

        # 3. CPA_QUOTA_ALERT SSE handler triggers prominent alert + loadBooks()
        m_quota = re.search(
            r"SSE_HANDLERS\[SSE_EVENTS\.CPA_QUOTA_ALERT\]\s*=\s*function\s*\(payload\)\s*\{(.*?)\n  \};",
            self.app_text,
            re.DOTALL,
        )
        self.assertIsNotNone(m_quota, "SSE_HANDLERS[SSE_EVENTS.CPA_QUOTA_ALERT] missing in app.js")
        self.assertIn("upsertModelQuotaAlert(", m_quota.group(1))
        self.assertIn("loadBooks()", m_quota.group(1))
        self.assertIn("MODEL_SWITCHED", m_quota.group(1))

        # 4. PAUSED_MANUAL has distinct label/style from PAUSED_QUOTA_EXHAUSTED
        self.assertIn('PAUSED_MANUAL: { text: "手动暂停", cls: "tag-slate" }', self.app_text)
        self.assertIn('PAUSED_QUOTA_EXHAUSTED: { text: "超预算 / 额度熔断", cls: "tag-danger" }', self.app_text)
        self.assertIn("is-quota-exhausted", self.app_text)
        self.assertIn("is-paused-manual", self.app_text)
        self.assertIn(".model-alert-card.is-danger", self.css_text)
        self.assertIn(".model-alert-card.is-warn", self.css_text)
        self.assertIn(".book-card.is-quota-exhausted", self.css_text)
        self.assertIn(".book-card.is-paused-manual", self.css_text)


if __name__ == "__main__":
    unittest.main()
