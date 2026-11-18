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


if __name__ == "__main__":
    unittest.main()
