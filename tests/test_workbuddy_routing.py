from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import socketserver
import sqlite3
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple
import unittest
from unittest import mock

from xiaoshuo_bot.cli import _build_serve_router, build_parser
from xiaoshuo_bot.contracts import (
    BookRunStatus,
    SSE_CPA_QUOTA_ALERT,
)
from xiaoshuo_bot.llm import (
    BudgetCircuitBreakerError,
    DEFAULT_MODEL_QUOTA_COOLDOWN_SEC,
    DEFAULT_RATE_LIMIT_BACKOFF_SEC,
    LLMConfigurationError,
    LLMRequest,
    LLMRequestError,
    LLMStreamTruncatedError,
    ModelSwitchEvent,
    QuotaExhaustedError,
    RateLimitRetryEvent,
    RoleEndpointConfig,
    RoleModelRouter,
    UsageBudgetLedger,
    WorkBuddyModelCatalog,
    classify_workbuddy_failure_kind,
    classify_workbuddy_quota_exhausted,
    find_forbidden_model_reason,
    validate_workbuddy_model_allowed,
)
from xiaoshuo_bot.server.matrix_scheduler import MatrixScheduler
from xiaoshuo_bot.server.stockpile_db import StockpileDB


SAMPLE_WHITELIST: Tuple[str, ...] = (
    "wb/test-hy4-flash",
    "wb/test-hy4-manual",
    "wb/test-ds-v41-flash",
    "wb/test-mimo-v26-pro",
)
SAMPLE_LABELS: Dict[str, str] = {
    "wb/test-hy4-flash": "Hy4 flash",
    "wb/test-hy4-manual": "Hy4",
    "wb/test-ds-v41-flash": "DeepSeek-V4.1-Flash",
    "wb/test-mimo-v26-pro": "Xiaomi MiMo-V2.6-Pro",
}


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start_scripted_cpa_server(
    handler_fn,
) -> Tuple[_ThreadedHTTPServer, str, List[Dict[str, Any]]]:
    calls: List[Dict[str, Any]] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            calls.append({"path": self.path, "model": payload.get("model"), "payload": payload})
            status, body_obj, is_sse = handler_fn(payload, len(calls))
            if is_sse:
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.end_headers()
                for line in body_obj:
                    self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            else:
                raw_out = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw_out)))
                self.end_headers()
                self.wfile.write(raw_out)

    server = _ThreadedHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}", calls


def _sse_ok(model: str, text: str) -> List[str]:
    chunk1 = json.dumps(
        {
            "id": "chatcmpl-test",
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
        },
        ensure_ascii=False,
    )
    chunk2 = json.dumps(
        {
            "id": "chatcmpl-test",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80},
        },
        ensure_ascii=False,
    )
    return [f"data: {chunk1}\n\n", f"data: {chunk2}\n\n", "data: [DONE]\n\n"]


def _make_req(chapter: int = 1, book_id: str = "book_a") -> LLMRequest:
    return LLMRequest(
        request_id=f"req-ch{chapter}",
        chapter=chapter,
        attempt=1,
        purpose="draft",
        system_prompt="sys",
        user_prompt="user",
        metadata={"book_id": book_id},
    )


class TestWorkBuddyPolicyAndClassifier(unittest.TestCase):
    def test_forbidden_models_and_whitelist_default_deny(self) -> None:
        forbidden_cases = [
            "deepseek-v4-pro",
            "wb/deepseek-v4-pro",
            "wb-ai/DeepSeek-V4-Pro",
            "wb/deepseek-v4-pro-preview",
            "auto",
            "wb/auto",
            "wb-ai/auto-fast",
            "wb/快速",
            "wb/均衡",
            "wb/极致",
        ]
        for bad in forbidden_cases:
            with self.subTest(bad_model=bad):
                self.assertIsNotNone(find_forbidden_model_reason(bad))
                with self.assertRaises(LLMConfigurationError):
                    validate_workbuddy_model_allowed(bad, SAMPLE_WHITELIST)

        # Allowed models must pass cleanly (including DeepSeek-V4.1-Flash which is NOT V4-Pro)
        self.assertIsNone(find_forbidden_model_reason("wb/deepseek-v4.1-flash"))
        for ok in SAMPLE_WHITELIST:
            self.assertEqual(validate_workbuddy_model_allowed(ok, SAMPLE_WHITELIST), ok)

        # Any unwhitelisted model is rejected by default-deny
        with self.assertRaises(LLMConfigurationError):
            validate_workbuddy_model_allowed("wb/gpt-5-mini", SAMPLE_WHITELIST)

    def test_hy4_manual_only_cannot_enter_rotation_chain(self) -> None:
        # Putting Hy4 into rotation_models must fail configuration immediately
        with self.assertRaises(LLMConfigurationError) as ctx:
            RoleEndpointConfig(
                role="writer",
                base_url="http://127.0.0.1:8317",
                model="wb/test-hy4-flash",
                api_keys=["sk-test-key"],
                model_whitelist=SAMPLE_WHITELIST,
                rotation_models=("wb/test-hy4-flash", "wb/test-hy4-manual"),
                manual_only_models=("wb/test-hy4-manual",),
            )
        self.assertIn("Hy4", str(ctx.exception))

        # Selecting Hy4 as primary WITH rotation_models must also fail
        with self.assertRaises(LLMConfigurationError):
            RoleEndpointConfig(
                role="writer",
                base_url="http://127.0.0.1:8317",
                model="wb/test-hy4-manual",
                api_keys=["sk-test-key"],
                model_whitelist=SAMPLE_WHITELIST,
                rotation_models=("wb/test-ds-v41-flash",),
                manual_only_models=("wb/test-hy4-manual",),
            )

        # Selecting Hy4 manually with empty rotation_models IS allowed
        cfg = RoleEndpointConfig(
            role="writer",
            base_url="http://127.0.0.1:8317",
            model="wb/test-hy4-manual",
            api_keys=["sk-test-key"],
            model_whitelist=SAMPLE_WHITELIST,
            rotation_models=(),
            manual_only_models=("wb/test-hy4-manual",),
        )
        self.assertEqual(cfg.model, "wb/test-hy4-manual")
        self.assertEqual(cfg.rotation_models, ())

    def test_quota_exhaustion_classifier_matches_cpa_workbuddy_2a1a608_evidence(self) -> None:
        # Verified quota errors from cpa-workbuddy/internal/workbuddy/chat_errors.go:95-96,107-108
        quota_cases = [
            LLMRequestError(
                "OpenAI-compatible HTTP 429: WorkBuddy rate limit exceeded",
                status_code=429,
                error_code="rate_limit_exceeded",
            ),
            LLMRequestError(
                "OpenAI-compatible HTTP 400: WorkBuddy requires an active credit package with available balance (upstream code 14018)",
                status_code=400,
                error_code="credit_package_exhausted",
                upstream_code="14018",
            ),
            LLMRequestError(
                'OpenAI-compatible HTTP 400: {"error":{"message":"WorkBuddy requires an active credit package with available balance (upstream code 14018)"}}',
                status_code=400,
                response_body='{"error":{"message":"WorkBuddy requires an active credit package with available balance (upstream code 14018)"}}',
            ),
            LLMRequestError(
                'OpenAI-compatible HTTP 400: {"error":{"data":{"code":"14018"}}}',
                status_code=400,
                response_body='{"error":{"data":{"code":"14018"}}}',
            ),
            LLMRequestError(
                "OpenAI-compatible HTTP 400: 额度已用尽",
                status_code=400,
                response_body='{"msg":"额度已用尽"}',
            ),
        ]
        for err in quota_cases:
            with self.subTest(err=str(err)):
                matched, reason = classify_workbuddy_quota_exhausted(err)
                self.assertTrue(matched)
                self.assertTrue(reason)

        # Non-quota errors MUST NEVER trigger model rotation
        non_quota_cases = [
            LLMRequestError("OpenAI-compatible transport error: Connection refused", status_code=None),
            LLMRequestError("OpenAI-compatible HTTP 408: WorkBuddy request timed out", status_code=408, error_code="upstream_timeout"),
            LLMRequestError("OpenAI-compatible HTTP 500: internal error", status_code=500),
            LLMRequestError("OpenAI-compatible HTTP 502: WorkBuddy rejected the client version (upstream code 10085)", status_code=502, error_code="client_version_rejected", upstream_code="10085"),
            LLMRequestError("OpenAI-compatible HTTP 503: WorkBuddy service is temporarily unavailable", status_code=503, error_code="upstream_unavailable"),
            LLMRequestError("OpenAI-compatible HTTP 504: Gateway Timeout", status_code=504, error_code="upstream_timeout"),
            LLMRequestError("OpenAI-compatible HTTP 401: invalid_token", status_code=401, error_code="invalid_token"),
            LLMRequestError("OpenAI-compatible HTTP 403: WorkBuddy restricted this account (upstream code 11140)", status_code=403, error_code="account_restricted", upstream_code="11140"),
            LLMRequestError("OpenAI-compatible HTTP 403: WorkBuddy denied access to this model", status_code=403, error_code="model_permission_denied"),
            LLMRequestError("OpenAI-compatible HTTP 400: context_length_exceeded", status_code=400, error_code="context_length_exceeded"),
            LLMRequestError("OpenAI-compatible HTTP 400: WorkBuddy rejected the client prompt shape (upstream code 11128)", status_code=400, error_code="channel_rejected", upstream_code="11128"),
            # Code-boundary check from cpa-workbuddy/internal/workbuddy/chat_faults_test.go:28 (140185 != 14018!)
            LLMRequestError('OpenAI-compatible HTTP 400: {"code":140185,"msg":"private upstream"}', status_code=400, upstream_code="140185", response_body='{"code":140185,"msg":"private upstream"}'),
        ]
        for err in non_quota_cases:
            with self.subTest(non_quota=str(err)):
                matched, _ = classify_workbuddy_quota_exhausted(err)
                self.assertFalse(matched)


class TestWorkBuddyRotationRouter(unittest.TestCase):
    def test_writer_and_planner_fixed_rotation_order_and_all_exhausted_halt(self) -> None:
        # Scripted CPA:
        # - wb/test-hy4-flash -> HTTP 429 (rate_limit_exceeded)
        # - wb/test-ds-v41-flash -> HTTP 400 with upstream code 14018 (credit_package_exhausted)
        # - wb/test-mimo-v26-pro -> 1st call succeeds, 2nd call hits HTTP 429
        mimo_hits = {"count": 0}

        def handler(payload: Dict[str, Any], _seq: int):
            m = payload.get("model")
            if m == "wb/test-hy4-flash":
                return 429, {"error": {"code": "rate_limit_exceeded", "message": "WorkBuddy rate limit exceeded"}}, False
            if m == "wb/test-ds-v41-flash":
                return 400, {
                    "error": {
                        "code": "credit_package_exhausted",
                        "message": "WorkBuddy requires an active credit package with available balance (upstream code 14018)",
                    }
                }, False
            if m == "wb/test-mimo-v26-pro":
                mimo_hits["count"] += 1
                if mimo_hits["count"] <= 2:
                    return 200, _sse_ok(m, f"ok-from-{m}"), True
                return 429, {"error": {"code": "rate_limit_exceeded", "message": "WorkBuddy rate limit exceeded"}}, False
            raise AssertionError(f"Unexpected model called: {m!r}")

        server, base_url, calls = _start_scripted_cpa_server(handler)
        try:
            events: List[ModelSwitchEvent] = []
            router = RoleModelRouter(
                roles={
                    "writer": RoleEndpointConfig(
                        role="writer",
                        base_url=base_url,
                        model="wb/test-hy4-flash",
                        api_keys=["sk-cpa-client-key"],
                        model_whitelist=SAMPLE_WHITELIST,
                        rotation_models=(
                            "wb/test-hy4-flash",
                            "wb/test-ds-v41-flash",
                            "wb/test-mimo-v26-pro",
                        ),
                        manual_only_models=("wb/test-hy4-manual",),
                        model_labels=SAMPLE_LABELS,
                    ),
                    "planner": RoleEndpointConfig(
                        role="planner",
                        base_url=base_url,
                        model="wb/test-ds-v41-flash",
                        api_keys=["sk-cpa-client-key"],
                        model_whitelist=SAMPLE_WHITELIST,
                        rotation_models=(
                            "wb/test-ds-v41-flash",
                            "wb/test-mimo-v26-pro",
                        ),
                        manual_only_models=("wb/test-hy4-manual",),
                        model_labels=SAMPLE_LABELS,
                    ),
                },
                on_model_switched=events.append,
                sleep_fn=lambda _s: None,
            )

            # Writer call 1: Hy4 flash (429 -> 1 + 3 backoff retries = 4 calls)
            # -> DeepSeek-V4.1-Flash (400/14018 -> immediate switch) -> Xiaomi MiMo-V2.6-Pro (OK)
            resp_w = router.generate("writer", _make_req(chapter=1))
            self.assertEqual(resp_w.model, "wb/test-mimo-v26-pro")
            self.assertEqual([c["model"] for c in calls], [
                "wb/test-hy4-flash",
                "wb/test-hy4-flash",
                "wb/test-hy4-flash",
                "wb/test-hy4-flash",
                "wb/test-ds-v41-flash",
                "wb/test-mimo-v26-pro",
            ])
            self.assertEqual(len(events), 2)
            self.assertEqual((events[0].from_model, events[0].to_model), ("wb/test-hy4-flash", "wb/test-ds-v41-flash"))
            self.assertEqual(events[0].status_code, 429)
            self.assertEqual((events[1].from_model, events[1].to_model), ("wb/test-ds-v41-flash", "wb/test-mimo-v26-pro"))
            self.assertEqual(events[1].status_code, 400)
            self.assertEqual(events[1].upstream_code, "14018")
            self.assertEqual(router.get_active_model("writer"), "wb/test-mimo-v26-pro")

            # Planner call: starts from DeepSeek-V4.1-Flash (400/14018) -> Xiaomi MiMo-V2.6-Pro (OK), never touches Hy4 flash or Hy4
            calls.clear()
            events.clear()
            resp_p = router.generate("planner", _make_req(chapter=0))
            self.assertEqual(resp_p.model, "wb/test-mimo-v26-pro")
            self.assertEqual([c["model"] for c in calls], ["wb/test-ds-v41-flash", "wb/test-mimo-v26-pro"])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].role, "planner")
            self.assertEqual((events[0].from_model, events[0].to_model), ("wb/test-ds-v41-flash", "wb/test-mimo-v26-pro"))

            # Writer call 2: MiMo-V2.6-Pro now ALSO returns 429 (1 + 3 retries = 4 calls)
            # -> ALL exhausted -> QuotaExhaustedError, NEVER switches to Hy4!
            calls.clear()
            events.clear()
            with self.assertRaises(QuotaExhaustedError) as qctx:
                router.generate("writer", _make_req(chapter=2))
            self.assertIn("熔断停机", str(qctx.exception))
            self.assertEqual([c["model"] for c in calls], ["wb/test-mimo-v26-pro"] * 4)
            self.assertEqual(events, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_non_quota_errors_and_budget_breaker_never_rotate_model(self) -> None:
        scenarios = [
            ("502_upstream_unavailable", 502, {"error": {"code": "upstream_unavailable", "message": "temporary 502"}}),
            ("504_timeout", 504, {"error": {"code": "upstream_timeout", "message": "WorkBuddy request timed out"}}),
            ("403_account_restricted", 403, {"error": {"code": "account_restricted", "message": "restricted (upstream code 11140)"}}),
            ("400_code_boundary_140185", 400, {"code": 140185, "msg": "other error"}),
        ]
        for name, status_code, body in scenarios:
            with self.subTest(scenario=name):
                def handler(_payload: Dict[str, Any], _seq: int, sc=status_code, bd=body):
                    return sc, bd, False

                server, base_url, calls = _start_scripted_cpa_server(handler)
                try:
                    events: List[ModelSwitchEvent] = []
                    router = RoleModelRouter(
                        roles={
                            "writer": RoleEndpointConfig(
                                role="writer",
                                base_url=base_url,
                                model="wb/test-hy4-flash",
                                api_keys=["sk-cpa-client-key"],
                                model_whitelist=SAMPLE_WHITELIST,
                                rotation_models=(
                                    "wb/test-hy4-flash",
                                    "wb/test-ds-v41-flash",
                                    "wb/test-mimo-v26-pro",
                                ),
                                manual_only_models=("wb/test-hy4-manual",),
                            )
                        },
                        on_model_switched=events.append,
                    )
                    with self.assertRaises(LLMRequestError) as err_ctx:
                        router.generate("writer", _make_req(chapter=1))
                    self.assertEqual(err_ctx.exception.status_code, status_code)
                    # Every attempted request stayed on Hy4 flash (OpenAICompatibleProvider retries 5xx on same model, NEVER rotates!)
                    self.assertTrue(all(c["model"] == "wb/test-hy4-flash" for c in calls))
                    self.assertEqual(events, [])
                    self.assertEqual(router.get_active_model("writer"), "wb/test-hy4-flash")
                finally:
                    server.shutdown()
                    server.server_close()

        # Network transport error (unreachable port) NEVER rotates
        events_net: List[ModelSwitchEvent] = []
        router_net = RoleModelRouter(
            roles={
                "writer": RoleEndpointConfig(
                    role="writer",
                    base_url="http://127.0.0.1:1",
                    model="wb/test-hy4-flash",
                    api_keys=["sk-cpa-client-key"],
                    timeout=2,
                    model_whitelist=SAMPLE_WHITELIST,
                    rotation_models=("wb/test-hy4-flash", "wb/test-ds-v41-flash"),
                )
            },
            on_model_switched=events_net.append,
        )
        with self.assertRaises(LLMRequestError) as net_ctx:
            router_net.generate("writer", _make_req(chapter=1))
        self.assertIsNone(net_ctx.exception.status_code)
        self.assertEqual(events_net, [])
        self.assertEqual(router_net.get_active_model("writer"), "wb/test-hy4-flash")

        # Daily budget breaker NEVER rotates
        events_budget: List[ModelSwitchEvent] = []
        ledger = UsageBudgetLedger(daily_budget_limit=0.0)
        router_budget = RoleModelRouter(
            roles={
                "writer": RoleEndpointConfig(
                    role="writer",
                    base_url="http://127.0.0.1:8317",
                    model="wb/test-hy4-flash",
                    api_keys=["sk-cpa-client-key"],
                    model_whitelist=SAMPLE_WHITELIST,
                    rotation_models=("wb/test-hy4-flash", "wb/test-ds-v41-flash"),
                )
            },
            ledger=ledger,
            on_model_switched=events_budget.append,
        )
        with self.assertRaises(BudgetCircuitBreakerError):
            router_budget.generate("writer", _make_req(chapter=1))
        self.assertEqual(events_budget, [])

        # Stream truncation (finish_reason=length) NEVER rotates
        def trunc_handler(payload: Dict[str, Any], _seq: int):
            chunk = json.dumps(
                {
                    "id": "chatcmpl-trunc",
                    "model": payload.get("model"),
                    "choices": [{"index": 0, "delta": {"content": "半截"}, "finish_reason": "length"}],
                },
                ensure_ascii=False,
            )
            return 200, [f"data: {chunk}\n\n", "data: [DONE]\n\n"], True

        server_tr, base_url_tr, calls_tr = _start_scripted_cpa_server(trunc_handler)
        try:
            events_tr: List[ModelSwitchEvent] = []
            router_tr = RoleModelRouter(
                roles={
                    "writer": RoleEndpointConfig(
                        role="writer",
                        base_url=base_url_tr,
                        model="wb/test-hy4-flash",
                        api_keys=["sk-cpa-client-key"],
                        model_whitelist=SAMPLE_WHITELIST,
                        rotation_models=("wb/test-hy4-flash", "wb/test-ds-v41-flash"),
                    )
                },
                on_model_switched=events_tr.append,
            )
            with self.assertRaises(LLMStreamTruncatedError):
                router_tr.generate("writer", _make_req(chapter=1))
            self.assertEqual([c["model"] for c in calls_tr], ["wb/test-hy4-flash"])
            self.assertEqual(events_tr, [])
            self.assertEqual(router_tr.get_active_model("writer"), "wb/test-hy4-flash")
        finally:
            server_tr.shutdown()
            server_tr.server_close()

    def test_manual_hy4_never_rotates_on_quota_exhaustion(self) -> None:
        def handler(_payload: Dict[str, Any], _seq: int):
            return 400, {"error": {"code": "credit_package_exhausted", "message": "upstream code 14018"}}, False

        server, base_url, calls = _start_scripted_cpa_server(handler)
        try:
            events: List[ModelSwitchEvent] = []
            router = RoleModelRouter(
                roles={
                    "writer": RoleEndpointConfig(
                        role="writer",
                        base_url=base_url,
                        model="wb/test-hy4-manual",
                        api_keys=["sk-cpa-client-key"],
                        model_whitelist=SAMPLE_WHITELIST,
                        rotation_models=(),
                        manual_only_models=("wb/test-hy4-manual",),
                    )
                },
                on_model_switched=events.append,
            )
            with self.assertRaises(QuotaExhaustedError):
                router.generate("writer", _make_req(chapter=1))
            self.assertEqual([c["model"] for c in calls], ["wb/test-hy4-manual"])
            self.assertEqual(events, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_scheduler_records_switch_in_db_and_emits_p1_2_sse_alert_and_halts_when_exhausted(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        sample_project = repo_root / "projects" / "baiwu_dukou"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "projects" / "baiwu_dukou"
            shutil.copytree(sample_project, proj_dir)
            db = StockpileDB(tmp_path / "matrix.db")

            mode = {"exhaust_all": False}

            def handler(payload: Dict[str, Any], _seq: int):
                m = payload.get("model")
                if m == "wb/test-hy4-flash":
                    return 429, {"error": {"code": "rate_limit_exceeded", "message": "WorkBuddy rate limit exceeded"}}, False
                if mode["exhaust_all"]:
                    return 400, {"error": {"code": "credit_package_exhausted", "message": "WorkBuddy requires an active credit package with available balance (upstream code 14018)"}}, False
                return 200, _sse_ok(m, "测试正文"), True

            server, base_url, _calls = _start_scripted_cpa_server(handler)
            try:
                router = RoleModelRouter(
                    roles={
                        "writer": RoleEndpointConfig(
                            role="writer",
                            base_url=base_url,
                            model="wb/test-hy4-flash",
                            api_keys=["sk-cpa-client-key"],
                            model_whitelist=SAMPLE_WHITELIST,
                            rotation_models=(
                                "wb/test-hy4-flash",
                                "wb/test-ds-v41-flash",
                                "wb/test-mimo-v26-pro",
                            ),
                            manual_only_models=("wb/test-hy4-manual",),
                            model_labels=SAMPLE_LABELS,
                        ),
                    },
                    sleep_fn=lambda _s: None,
                )
                scheduler = MatrixScheduler(
                    db=db,
                    projects_root=tmp_path / "projects",
                    provider=router,
                    export_only=True,
                )
                db.register_book(
                    book_id="baiwu_dukou",
                    title="白雾渡口",
                    project_dir=proj_dir,
                    account_id="acct_1",
                    status=BookRunStatus.ACTIVE,
                )

                # Trigger writer switch via router (simulating chapter 1 draft):
                # 429 retries 3 times on Hy4 flash before rotating to DeepSeek-V4.1-Flash
                resp = router.generate("writer", _make_req(chapter=1, book_id="baiwu_dukou"))
                self.assertEqual(resp.model, "wb/test-ds-v41-flash")

                # Verify DB event records: 3 rate_limit_retry + 1 model_switched (latest is index 0)
                db_events = db.list_model_switch_events(book_id="baiwu_dukou")
                self.assertEqual(len(db_events), 4)
                self.assertEqual(db_events[0]["event_type"], "model_switched")
                self.assertEqual(db_events[0]["role"], "writer")
                self.assertIn("Hy4 flash", db_events[0]["from_model"])
                self.assertIn("DeepSeek-V4.1-Flash", db_events[0]["to_model"])
                self.assertEqual(db_events[0]["status_code"], 429)
                self.assertEqual(db_events[0]["book_title"], "白雾渡口")
                retry_db = [e for e in db_events if e["event_type"] == "rate_limit_retry"]
                self.assertEqual(len(retry_db), 3)

                # Verify P1-2 SSE alerts emitted: 3 rate_limit_retry followed by 1 model_switched
                sse_events = [
                    ev for ev in scheduler.event_broker.get_recent_events(limit=20)
                    if ev.event == SSE_CPA_QUOTA_ALERT
                ]
                self.assertEqual(len(sse_events), 4)
                self.assertEqual([ev.data["kind"] for ev in sse_events[:3]], ["rate_limit_retry"] * 3)
                self.assertEqual(sse_events[3].data["kind"], "model_switched")
                self.assertIn("Hy4 flash", sse_events[3].data["from_model"])
                self.assertIn("DeepSeek-V4.1-Flash", sse_events[3].data["to_model"])
                self.assertTrue(sse_events[3].data["reason"])

                # Verify update_cpa_bindings blocks forbidden models
                with self.assertRaises(LLMConfigurationError):
                    scheduler.update_cpa_bindings([
                        {"role": "writer", "model": "wb/deepseek-v4-pro", "base_url": base_url, "provider_type": "openai"}
                    ])
                with self.assertRaises(LLMConfigurationError):
                    scheduler.update_cpa_bindings([
                        {"role": "writer", "model": "wb/auto", "base_url": base_url, "provider_type": "openai"}
                    ])

                # Exhaust remaining rotation models -> produce_next_chapter halts book to PAUSED_QUOTA_EXHAUSTED
                mode["exhaust_all"] = True
                res = scheduler.produce_next_chapter("baiwu_dukou")
                self.assertEqual(res["status"], BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value)
                book_after = db.get_book("baiwu_dukou")
                self.assertEqual(book_after["status"], BookRunStatus.PAUSED_QUOTA_EXHAUSTED.value)

                quota_halts = [
                    ev for ev in scheduler.event_broker.get_recent_events(limit=20)
                    if ev.event == SSE_CPA_QUOTA_ALERT and ev.data.get("kind") == "quota_exhausted"
                ]
                self.assertGreaterEqual(len(quota_halts), 1)
            finally:
                server.shutdown()
                server.server_close()
                db.close()


class TestWorkBuddyRateLimitAndCooldownRecovery(unittest.TestCase):
    def test_bug1_rate_limit_429_retries_same_model_and_succeeds_without_switching(self) -> None:
        hy4_hits = {"count": 0}

        def handler(payload: Dict[str, Any], _seq: int):
            m = payload.get("model")
            if m == "wb/test-hy4-flash":
                hy4_hits["count"] += 1
                if hy4_hits["count"] <= 2:
                    return 429, {"error": {"code": "rate_limit_exceeded", "message": "WorkBuddy rate limit exceeded"}}, False
                return 200, _sse_ok(m, "重试成功后成稿"), True
            raise AssertionError(f"Should stay on Hy4 flash after retry succeeds, unexpectedly called {m!r}")

        server, base_url, calls = _start_scripted_cpa_server(handler)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = StockpileDB(tmp_path / "matrix.db")
            try:
                sleeps: List[float] = []
                switches: List[ModelSwitchEvent] = []
                retries: List[RateLimitRetryEvent] = []
                router = RoleModelRouter(
                    roles={
                        "writer": RoleEndpointConfig(
                            role="writer",
                            base_url=base_url,
                            model="wb/test-hy4-flash",
                            api_keys=["sk-cpa-client-key"],
                            model_whitelist=SAMPLE_WHITELIST,
                            rotation_models=(
                                "wb/test-hy4-flash",
                                "wb/test-ds-v41-flash",
                                "wb/test-mimo-v26-pro",
                            ),
                            manual_only_models=("wb/test-hy4-manual",),
                            model_labels=SAMPLE_LABELS,
                        ),
                    },
                    on_model_switched=switches.append,
                    on_rate_limit_retry=retries.append,
                    sleep_fn=sleeps.append,
                )
                scheduler = MatrixScheduler(
                    db=db,
                    projects_root=tmp_path / "projects",
                    provider=router,
                    export_only=True,
                )
                db.register_book(
                    book_id="book_rl_ok",
                    title="限流重试测试",
                    project_dir=tmp_path / "projects" / "book_rl_ok",
                    account_id="acct_1",
                    status=BookRunStatus.ACTIVE,
                )

                resp = router.generate("writer", _make_req(chapter=1, book_id="book_rl_ok"))
                self.assertEqual(resp.model, "wb/test-hy4-flash")
                self.assertEqual([c["model"] for c in calls], ["wb/test-hy4-flash"] * 3)
                self.assertEqual(sleeps, [30.0, 60.0])
                self.assertEqual(switches, [])
                self.assertEqual(router.get_active_model("writer"), "wb/test-hy4-flash")
                self.assertEqual(len(retries), 2)
                self.assertEqual([(r.attempt, r.max_retries, r.wait_seconds) for r in retries], [(1, 3, 30.0), (2, 3, 60.0)])

                db_events = db.list_model_switch_events(book_id="book_rl_ok")
                self.assertEqual(len(db_events), 2)
                self.assertEqual([e["event_type"] for e in db_events], ["rate_limit_retry", "rate_limit_retry"])
                self.assertEqual([e["retry_attempt"] for e in db_events], [2, 1])
                self.assertEqual([e["wait_seconds"] for e in db_events], [60.0, 30.0])

                sse_events = [
                    ev for ev in scheduler.event_broker.get_recent_events(limit=20)
                    if ev.event == SSE_CPA_QUOTA_ALERT
                ]
                self.assertEqual(len(sse_events), 2)
                self.assertEqual([ev.data["kind"] for ev in sse_events], ["rate_limit_retry", "rate_limit_retry"])
                self.assertIn("被限流，第 1 次等待重试", sse_events[0].message)
                self.assertIn("被限流，第 2 次等待重试", sse_events[1].message)
            finally:
                server.shutdown()
                server.server_close()
                db.close()

    def test_bug1_rate_limit_429_switches_only_after_3_consecutive_retries(self) -> None:
        def handler(payload: Dict[str, Any], _seq: int):
            m = payload.get("model")
            if m == "wb/test-hy4-flash":
                return 429, {"error": {"code": "rate_limit_exceeded", "message": "WorkBuddy rate limit exceeded"}}, False
            if m == "wb/test-ds-v41-flash":
                return 200, _sse_ok(m, "三次限流后轮换成功"), True
            raise AssertionError(f"Unexpected model called: {m!r}")

        server, base_url, calls = _start_scripted_cpa_server(handler)
        try:
            # Part A: Default (30, 60, 120) -> 1 initial + 3 retries = 4 calls before rotating
            sleeps_default: List[float] = []
            switches_default: List[ModelSwitchEvent] = []
            retries_default: List[RateLimitRetryEvent] = []
            router_default = RoleModelRouter(
                roles={
                    "writer": RoleEndpointConfig(
                        role="writer",
                        base_url=base_url,
                        model="wb/test-hy4-flash",
                        api_keys=["sk-cpa-client-key"],
                        model_whitelist=SAMPLE_WHITELIST,
                        rotation_models=(
                            "wb/test-hy4-flash",
                            "wb/test-ds-v41-flash",
                            "wb/test-mimo-v26-pro",
                        ),
                        manual_only_models=("wb/test-hy4-manual",),
                        model_labels=SAMPLE_LABELS,
                    ),
                },
                on_model_switched=switches_default.append,
                on_rate_limit_retry=retries_default.append,
                sleep_fn=sleeps_default.append,
            )
            self.assertEqual(
                router_default.get_role_config("writer").effective_rate_limit_backoff_sec(),
                DEFAULT_RATE_LIMIT_BACKOFF_SEC,
            )
            resp = router_default.generate("writer", _make_req(chapter=1))
            self.assertEqual(resp.model, "wb/test-ds-v41-flash")
            self.assertEqual(
                [c["model"] for c in calls],
                ["wb/test-hy4-flash"] * 4 + ["wb/test-ds-v41-flash"],
            )
            self.assertEqual(sleeps_default, [30.0, 60.0, 120.0])
            self.assertEqual(len(retries_default), 3)
            self.assertEqual(len(switches_default), 1)
            self.assertEqual(switches_default[0].event_type, "model_switched")
            self.assertIn("连续 3 次退避重试后仍被限流", switches_default[0].reason)

            # Part B: Configurable backoff delays via RoleEndpointConfig & CLI env var
            calls.clear()
            sleeps_custom: List[float] = []
            retries_custom: List[RateLimitRetryEvent] = []
            router_custom = RoleModelRouter(
                roles={
                    "writer": RoleEndpointConfig(
                        role="writer",
                        base_url=base_url,
                        model="wb/test-hy4-flash",
                        api_keys=["sk-cpa-client-key"],
                        model_whitelist=SAMPLE_WHITELIST,
                        rotation_models=("wb/test-hy4-flash", "wb/test-ds-v41-flash"),
                        rate_limit_backoff_sec=(5.0, 15.0),
                        model_labels=SAMPLE_LABELS,
                    ),
                },
                on_rate_limit_retry=retries_custom.append,
                sleep_fn=sleeps_custom.append,
            )
            resp_custom = router_custom.generate("writer", _make_req(chapter=1))
            self.assertEqual(resp_custom.model, "wb/test-ds-v41-flash")
            self.assertEqual([c["model"] for c in calls], ["wb/test-hy4-flash"] * 3 + ["wb/test-ds-v41-flash"])
            self.assertEqual(sleeps_custom, [5.0, 15.0])
            self.assertEqual(len(retries_custom), 2)
        finally:
            server.shutdown()
            server.server_close()

    def test_bug1_credit_exhausted_14018_switches_immediately_without_backoff(self) -> None:
        scenarios = [
            (
                "http_400_credit_package_exhausted_14018",
                400,
                {
                    "error": {
                        "code": "credit_package_exhausted",
                        "message": "WorkBuddy requires an active credit package with available balance (upstream code 14018)",
                    }
                },
            ),
            (
                "http_429_credit_package_exhausted_14018",
                429,
                {
                    "error": {
                        "code": "credit_package_exhausted",
                        "message": "WorkBuddy credit package exhausted (upstream code 14018)",
                    }
                },
            ),
        ]
        for label, err_status, err_body in scenarios:
            with self.subTest(scenario=label):
                def handler(payload: Dict[str, Any], _seq: int, sc=err_status, bd=err_body):
                    m = payload.get("model")
                    if m == "wb/test-hy4-flash":
                        return sc, bd, False
                    return 200, _sse_ok(m, "积分用尽立刻轮换"), True

                server, base_url, calls = _start_scripted_cpa_server(handler)
                try:
                    sleeps: List[float] = []
                    retries: List[RateLimitRetryEvent] = []
                    switches: List[ModelSwitchEvent] = []
                    router = RoleModelRouter(
                        roles={
                            "writer": RoleEndpointConfig(
                                role="writer",
                                base_url=base_url,
                                model="wb/test-hy4-flash",
                                api_keys=["sk-cpa-client-key"],
                                model_whitelist=SAMPLE_WHITELIST,
                                rotation_models=(
                                    "wb/test-hy4-flash",
                                    "wb/test-ds-v41-flash",
                                    "wb/test-mimo-v26-pro",
                                ),
                                model_labels=SAMPLE_LABELS,
                            ),
                        },
                        on_model_switched=switches.append,
                        on_rate_limit_retry=retries.append,
                        sleep_fn=sleeps.append,
                    )
                    resp = router.generate("writer", _make_req(chapter=1))
                    self.assertEqual(resp.model, "wb/test-ds-v41-flash")
                    self.assertEqual([c["model"] for c in calls], ["wb/test-hy4-flash", "wb/test-ds-v41-flash"])
                    self.assertEqual(sleeps, [])
                    self.assertEqual(retries, [])
                    self.assertEqual(len(switches), 1)
                    self.assertEqual(switches[0].upstream_code, "14018")
                    self.assertEqual(switches[0].event_type, "model_switched")
                finally:
                    server.shutdown()
                    server.server_close()

        # Also verify direct classifier separation
        kind_429, _ = classify_workbuddy_failure_kind(
            LLMRequestError("HTTP 429 rate_limit_exceeded", status_code=429, error_code="rate_limit_exceeded")
        )
        kind_14018, _ = classify_workbuddy_failure_kind(
            LLMRequestError(
                "HTTP 400 credit_package_exhausted (upstream code 14018)",
                status_code=400,
                error_code="credit_package_exhausted",
                upstream_code="14018",
            )
        )
        self.assertEqual(kind_429, "rate_limit")
        self.assertEqual(kind_14018, "credit_exhausted")

    def test_bug2_default_1h_cooldown_and_auto_restore_hy4_flash_before_next_chapter(self) -> None:
        clock = {"now": 1000.0}
        hy4_exhausted = {"flag": True}
        ds_exhausted = {"flag": False}

        def handler(payload: Dict[str, Any], _seq: int):
            m = payload.get("model")
            if m == "wb/test-hy4-flash" and hy4_exhausted["flag"]:
                return 400, {
                    "error": {
                        "code": "credit_package_exhausted",
                        "message": "WorkBuddy credit package exhausted (upstream code 14018)",
                    }
                }, False
            if m == "wb/test-ds-v41-flash" and ds_exhausted["flag"]:
                return 400, {
                    "error": {
                        "code": "credit_package_exhausted",
                        "message": "WorkBuddy credit package exhausted (upstream code 14018)",
                    }
                }, False
            return 200, _sse_ok(m, f"ok-from-{m}"), True

        server, base_url, calls = _start_scripted_cpa_server(handler)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = StockpileDB(tmp_path / "matrix.db")
            try:
                catalog = WorkBuddyModelCatalog.from_mapping(
                    {
                        "hy4_flash": "wb/test-hy4-flash",
                        "hy4": "wb/test-hy4-manual",
                        "deepseek_v4_1_flash": "wb/test-ds-v41-flash",
                        "mimo_v2_6_pro": "wb/test-mimo-v26-pro",
                    }
                )
                router = RoleModelRouter.from_workbuddy_catalog(
                    catalog,
                    base_url=base_url,
                    api_keys=["sk-cpa-client-key"],
                    monotonic_fn=lambda: clock["now"],
                    sleep_fn=lambda _s: None,
                )
                # Default cooldown must be 1 hour (3600s), NOT 60s
                self.assertEqual(DEFAULT_MODEL_QUOTA_COOLDOWN_SEC, 3600.0)
                self.assertEqual(router.get_role_config("writer").effective_model_cooldown_sec(), 3600.0)

                scheduler = MatrixScheduler(
                    db=db,
                    projects_root=tmp_path / "projects",
                    provider=router,
                    export_only=True,
                )
                db.register_book(
                    book_id="book_cooldown",
                    title="自动切回测试",
                    project_dir=tmp_path / "projects" / "book_cooldown",
                    account_id="acct_1",
                    status=BookRunStatus.ACTIVE,
                )

                # Chapter 1 at t=1000: Hy4 flash hits 14018 -> immediately switches to DeepSeek-V4.1-Flash
                resp1 = router.generate("writer", _make_req(chapter=1, book_id="book_cooldown"))
                self.assertEqual(resp1.model, "wb/test-ds-v41-flash")
                self.assertEqual(router.get_active_model("writer"), "wb/test-ds-v41-flash")

                # Chapter 2 at t=2800 (+30 min < 1 hour): Hy4 flash still cooling -> stays on DeepSeek-V4.1-Flash
                hy4_exhausted["flag"] = False
                clock["now"] = 2800.0
                calls.clear()
                resp2 = router.generate("writer", _make_req(chapter=2, book_id="book_cooldown"))
                self.assertEqual(resp2.model, "wb/test-ds-v41-flash")
                self.assertEqual([c["model"] for c in calls], ["wb/test-ds-v41-flash"])
                self.assertEqual(router.get_active_model("writer"), "wb/test-ds-v41-flash")
                restored_so_far = [
                    e for e in db.list_model_switch_events(book_id="book_cooldown")
                    if e["event_type"] == "model_restored"
                ]
                self.assertEqual(restored_so_far, [])

                # Chapter 3 at t=4601 (+1 hour + 1 sec): Hy4 flash cooled off -> auto-restores BEFORE writing chapter 3!
                clock["now"] = 4601.0
                calls.clear()
                resp3 = router.generate("writer", _make_req(chapter=3, book_id="book_cooldown"))
                self.assertEqual(resp3.model, "wb/test-hy4-flash")
                self.assertEqual([c["model"] for c in calls], ["wb/test-hy4-flash"])
                self.assertEqual(router.get_active_model("writer"), "wb/test-hy4-flash")

                # Verify DB event record & SSE alert for model_restored
                db_events = db.list_model_switch_events(book_id="book_cooldown")
                self.assertEqual(db_events[0]["event_type"], "model_restored")
                self.assertEqual(db_events[0]["chapter"], 3)
                self.assertIn("DeepSeek-V4.1-Flash", db_events[0]["from_model"])
                self.assertIn("Hy4 flash", db_events[0]["to_model"])

                restored_sse = [
                    ev for ev in scheduler.event_broker.get_recent_events(limit=20)
                    if ev.event == SSE_CPA_QUOTA_ALERT and ev.data.get("kind") == "model_restored"
                ]
                self.assertEqual(len(restored_sse), 1)
                self.assertIn("已切回", restored_sse[0].message)
                self.assertIn("Hy4 flash", restored_sse[0].message)

                # Multi-hop staged recovery: exhaust Hy4 flash at t=5000, exhaust DeepSeek-V4.1-Flash at t=5600 -> MiMo-V2.6-Pro
                # Wait: DeepSeek cools at t=9200 while Hy4 flash cools at t=8600. Let's exhaust DeepSeek FIRST (at t=5000)
                # and Hy4 flash later (at t=5600) by configuring custom cooldown or checking earliest available:
                # Exhaust Hy4 flash at t=5000 (cools at 8600), wait to t=5500, exhaust DS at t=5500 (cools at 9100),
                # then re-exhaust Hy4 flash at t=8700 (cools at 12300) so at t=9101 DS is cooled while Hy4 flash is still cooling!
                hy4_exhausted["flag"] = True
                clock["now"] = 5000.0
                resp4 = router.generate("writer", _make_req(chapter=4, book_id="book_cooldown"))
                self.assertEqual(resp4.model, "wb/test-ds-v41-flash")

                ds_exhausted["flag"] = True
                clock["now"] = 5500.0
                resp5 = router.generate("writer", _make_req(chapter=5, book_id="book_cooldown"))
                self.assertEqual(resp5.model, "wb/test-mimo-v26-pro")
                self.assertEqual(router.get_active_model("writer"), "wb/test-mimo-v26-pro")

                # At t=8700: Hy4 flash cooled (5000+3600=8600), DS still cooling (5500+3600=9100).
                # Hy4 flash still returns 14018 -> re-cools until 12300, skips cooling DS, falls back to MiMo!
                clock["now"] = 8700.0
                resp6 = router.generate("writer", _make_req(chapter=6, book_id="book_cooldown"))
                self.assertEqual(resp6.model, "wb/test-mimo-v26-pro")

                # At t=9101: DS cooled (9100 < 9101) while Hy4 flash still cooling (12300 > 9101) -> restores earliest available = DS!
                ds_exhausted["flag"] = False
                clock["now"] = 9101.0
                calls.clear()
                resp7 = router.generate("writer", _make_req(chapter=7, book_id="book_cooldown"))
                self.assertEqual(resp7.model, "wb/test-ds-v41-flash")
                self.assertEqual([c["model"] for c in calls], ["wb/test-ds-v41-flash"])
                self.assertEqual(router.get_active_model("writer"), "wb/test-ds-v41-flash")

                # Verify CLI env configuration for both WORKBUDDY_RATE_LIMIT_BACKOFF_SEC and WORKBUDDY_MODEL_COOLDOWN_SEC
                parser = build_parser()
                args = parser.parse_args(["serve"])
                with mock.patch.dict(
                    os.environ,
                    {
                        "XIAOSHUO_BASE_URL": base_url,
                        "XIAOSHUO_API_KEY": "sk-cpa-client-key",
                        "WORKBUDDY_MODEL_HY4_FLASH": "wb/test-hy4-flash",
                        "WORKBUDDY_MODEL_HY4": "wb/test-hy4-manual",
                        "WORKBUDDY_MODEL_DEEPSEEK_V4_1_FLASH": "wb/test-ds-v41-flash",
                        "WORKBUDDY_MODEL_MIMO_V2_6_PRO": "wb/test-mimo-v26-pro",
                        "WORKBUDDY_RATE_LIMIT_BACKOFF_SEC": "10,20,40",
                        "WORKBUDDY_MODEL_COOLDOWN_SEC": "1800",
                    },
                    clear=False,
                ):
                    cli_router = _build_serve_router(args, tmp_path / "cli_matrix.db")
                assert cli_router is not None
                w_cfg = cli_router.get_role_config("writer")
                self.assertEqual(w_cfg.effective_rate_limit_backoff_sec(), (10.0, 20.0, 40.0))
                self.assertEqual(w_cfg.effective_model_cooldown_sec(), 1800.0)
            finally:
                server.shutdown()
                server.server_close()
                db.close()

    def test_bug2_engine_chapter_start_restores_hy4_flash_once_with_meta_and_db_sync(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        sample_project = repo_root / "projects" / "baiwu_dukou"
        clock = {"now": 1000.0}
        hy4_exhausted = {"flag": True}

        clean_paras = [
            "十一月中旬的傍晚，江雾贴着老街青石板漫过门槛。林晚拎着素色帆布袋停在敬山钟表铺外，垂眼看向门口那块发硬的棕垫，边缘正蹭着半枚带桐油腥气的红铁丹防锈漆橡胶鞋印。那颜色暗沉发涩，是新港三号码头船坞修补驳船吃水线才用的专配防锈漆，绝不该沾在即将拆迁的老街门垫上。",
            "屋檐下站着港务局档案科员周叙，他裹着深灰夹克，手里捏一只厚实的牛皮信封，见林晚走近便略显局促地递过来。周叙抬手摘下眼镜用衣角擦拭水汽的工夫，林晚目光掠过他左袖口内侧，清楚瞥见那里蹭着一道陈旧未洗净的蓝黑墨渍，呢料纤维间还挂着半根发黄的纸毛。",
            "推开吱呀作响的木门，铺内暖黄的台灯光线落在木柜台与一排停摆的老挂钟上。陈敬山从里间端出两只骨瓷茶杯，给林晚倒了七分满的热茶，杯沿那处米粒大小的缺口正朝向左下方，一如林昼生前常来串门时的旧习惯。",
            "林晚没有急着端茶，先拆开牛皮信封取出那只铜壳怀表，轻轻搁在柜台绒布垫上。表盘玻璃蒙着薄灰，秒针死死卡在三点十七分，表冠边缘缺了半截齿纹；她翻过表背凑近灯光细看，后盖接缝里除了干涸发灰的江泥，还赫然留着一道不超过三天的新鲜硬物撬痕。",
            "林晚抬头问周叙，这只遗物在移交前究竟经手过哪些人。周叙垂下眼帘摩挲着眼镜腿，支吾说局里最近清仓搬迁，旧库房杂乱得很，登记簿也翻得七零八落，说罢便借口还要赶回局里值夜班，匆匆推门告辞。",
            "木门合拢后，街面上的江雾更浓，周叙的背影几步便融进白茫茫的雾气里。林晚左手拇指无意识地摩挲过食指指腹那道陈年刻刀旧疤，顺手把铜壳怀表与柜台边沿的铜尺拨成严格平行的两条线，请陈敬山关好铺门进里间开盖查验。",
            "陈敬山没有多问，走到门口把沉重的木栓咔嗒一声推紧，又隔着蒙雾的玻璃窗朝空荡荡的巷口望了两眼。老街两侧待拆迁的砖楼早已人去楼空，唯独巷尾电线杆下悬着一盏昏暗路灯，照出一地湿滑反光的落叶与断续水渍。",
            "回到外间柜台内侧，陈敬山弯腰拉开玻璃柜第三格滑轨，伸手去取那只包浆温润的黄杨木修表镊盒。柜门滑开的轻响在寂静铺子里格外清晰，盒盖边缘磨得发亮，里面整整齐齐码着全套弯头铜镊与放大目镜。",
            "林晚站在柜台外侧，借着灯罩垂下的圆光再次审视怀表表链的金属扣环。扣环内壁磨出的亮痕偏左，说明原主人习惯把表挂在左侧马甲袋里，与林昼平日习惯分毫不差，可那道新鲜撬痕的着力角度却生硬得多，像是外行拿普通一字螺丝刀强行顶出来的。",
            "陈敬山把黄杨木镊盒捧到工作台前，轻轻掀开铜扣，从格子里挑出一把刃口极薄的开盖撬刀。他先用麂皮把绒布垫拂拭一遍，又把台灯摇臂往低处压了两寸，暖光直直打在表壳接缝上，那道新鲜划痕立刻折射出一丝极细的金属亮线。",
            "老街远处隐约传来江轮低沉的汽笛声，在浓雾里拖出绵长尾音。铺子里只剩下挂钟摆锤极慢的走动声，与壶里余温将散的微弱水气交织在一起。林晚把牛皮信封翻过来对着灯光照了照，封口胶痕有二次涂抹后重新压平的波纹，显然有人在移交前悄悄拆封看过。",
            "她将发现压在心里没有声张，只把信封工整折好收入帆布袋内侧隔层，随后端起那只缺口骨瓷茶杯抿了一口温茶。茶汤微苦回甘，驱散了袖口沾进来的江边寒气，她目光重新落回陈敬山稳稳捏住表壳的双手上，等待表盖弹开那一刻。",
            "陈敬山手腕微沉，撬刀沿着表盖边缘最细的缝隙稳稳探入，既不硬撬也不打滑，只听极轻的脆响，紧扣多年的铜壳后盖应声弹开一角。内盖防尘圈边沿粘着几粒极细的干泥沙，正中间却卡着一角折叠得方方正正的半透明油纸。",
            "林晚屏住呼吸凑近半步，看清那油纸边角泛黄却质地韧实，正是三年前老港务局用来包裹重要水运单据的防潮腊纸。油纸折痕边缘隐约透出一截蓝色圆珠笔数字，似乎原本是从更厚的一页账册上仓促撕下来的残片。",
            "陈敬山用弯头铜镊尖端极稳地夹住油纸边缘，没有直接展开，而是先放进一只干净的白瓷浅盘里。铺子外忽然掠过一阵湿冷江风，拍得木窗格轻响两声，门外旧巷转角隐约传出一声极轻的鞋底碾过碎石的动静，很快又被潮湿江雾盖了过去。",
            "林晚把铜尺收回原位，目光掠过门垫上那半枚刺眼的红铁丹漆印，心里已经串起第一条线索：无论是冒雾提前进铺翻找的不速之客，还是袖口带着陈年墨渍与纸毛匆忙交表的周叙，都说明这只停在三点十七分的怀表藏着不能见光的旧账。",
            "“这半张油纸先留在里间铁盒里，你明早再来。”陈敬山拧暗工作台灯，将黄杨木修表镊盒重新扣好，“今晚巷口不太平，回程走大道，别抄码头后墙那条近路。”林晚点头收好帆布袋，拉开木栓重新踏进浓重江雾之中。",
            "走出钟表铺十余步，老街石板路在鞋底泛着冷潮水光。林晚经过废旧布庄门口时放慢脚步，借着路灯昏黄光晕扫向墙根排水沟，沟沿水泥面上并无新的脚印，只有几片被夜风卷落的梧桐叶贴在铁栅栏条上。",
            "她拐上通往江堤大道的斜坡，远远听见渡口方向传来晚班拖轮低沉的引擎闷响。风衣左袋里那只铜壳怀表分量沉实，表背那道新鲜撬痕贴着掌心冰凉刺手，林晚回头望了一眼老街深处已熄灯的钟表铺门楼，快步迎着江风走向大道车站。",
        ]
        full_prose = "\n\n".join(clean_paras)

        def handler(payload: Dict[str, Any], _seq: int):
            m = payload.get("model")
            if m == "wb/test-hy4-flash" and hy4_exhausted["flag"]:
                return 400, {
                    "error": {
                        "code": "credit_package_exhausted",
                        "message": "WorkBuddy credit package exhausted (upstream code 14018)",
                    }
                }, False
            msgs = payload.get("messages") or []
            prompt_blob = "\n".join(str(x.get("content") or "") for x in msgs)
            if "first_screen_hook" in prompt_blob or "weak_paragraphs" in prompt_blob:
                reply_text = json.dumps(
                    {
                        "first_screen_hook": 8.8,
                        "cool_point_density": 8.6,
                        "comment_interactivity": 8.5,
                        "pacing_drag": 8.7,
                        "overall_score": 8.65,
                        "weak_paragraphs": [],
                        "critique": "节奏紧凑，伏笔到位",
                    },
                    ensure_ascii=False,
                )
            elif "作者有话说" in prompt_blob:
                reply_text = "怀表停在三点十七分，油纸背后的旧账明晚揭晓。"
            else:
                reply_text = full_prose
            return 200, _sse_ok(m, reply_text), True

        server, base_url, calls = _start_scripted_cpa_server(handler)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "projects" / "baiwu_dukou"
            shutil.copytree(sample_project, proj_dir)
            db = StockpileDB(tmp_path / "matrix.db")
            try:
                catalog = WorkBuddyModelCatalog.from_mapping(
                    {
                        "hy4_flash": "wb/test-hy4-flash",
                        "hy4": "wb/test-hy4-manual",
                        "deepseek_v4_1_flash": "wb/test-ds-v41-flash",
                        "mimo_v2_6_pro": "wb/test-mimo-v26-pro",
                    }
                )
                router = RoleModelRouter.from_workbuddy_catalog(
                    catalog,
                    base_url=base_url,
                    api_keys=["sk-cpa-client-key"],
                    monotonic_fn=lambda: clock["now"],
                    sleep_fn=lambda _s: None,
                )
                scheduler = MatrixScheduler(
                    db=db,
                    projects_root=tmp_path / "projects",
                    provider=router,
                    export_only=True,
                )
                db.register_book(
                    book_id="baiwu_dukou",
                    title="白雾渡口",
                    project_dir=proj_dir,
                    account_id="acct_1",
                    status=BookRunStatus.ACTIVE,
                )

                # Simulate prior exhaustion on Hy4 flash -> router is currently on DeepSeek-V4.1-Flash
                prev_resp = router.generate("writer", _make_req(chapter=0, book_id="baiwu_dukou"))
                self.assertEqual(prev_resp.model, "wb/test-ds-v41-flash")
                self.assertEqual(router.get_active_model("writer"), "wb/test-ds-v41-flash")

                # Cooldown expires (> 1 hour) and Hy4 flash is healthy again before producing chapter 1
                hy4_exhausted["flag"] = False
                clock["now"] = 4601.0
                calls.clear()

                res = scheduler.produce_next_chapter("baiwu_dukou")
                self.assertEqual(res["status"], "PRODUCED")
                self.assertEqual(res["model"], "wb/test-hy4-flash")
                self.assertEqual(router.get_active_model("writer"), "wb/test-hy4-flash")

                # Exactly ONE model_restored DB event and ONE SSE event (no duplicate between engine & router)
                restored_db = [
                    e for e in db.list_model_switch_events(book_id="baiwu_dukou")
                    if e["event_type"] == "model_restored"
                ]
                self.assertEqual(len(restored_db), 1)
                self.assertEqual(restored_db[0]["chapter"], 1)
                self.assertIn("Hy4 flash", restored_db[0]["to_model"])

                restored_sse = [
                    ev for ev in scheduler.event_broker.get_recent_events(limit=30)
                    if ev.event == SSE_CPA_QUOTA_ALERT and ev.data.get("kind") == "model_restored"
                ]
                self.assertEqual(len(restored_sse), 1)

                meta_path = proj_dir / "output" / "chapters" / "chapter_0001.meta.json"
                meta_obj = json.loads(meta_path.read_text(encoding="utf-8"))
                self.assertEqual(meta_obj["model"], "wb/test-hy4-flash")
                restored_meta = [
                    s for s in meta_obj.get("model_switches", [])
                    if s.get("event_type") == "model_restored"
                ]
                self.assertEqual(len(restored_meta), 1)
                self.assertEqual(restored_meta[0]["to_model"], "wb/test-hy4-flash")
            finally:
                server.shutdown()
                server.server_close()
                db.close()

    def test_bug1_and_bug2_admin_ui_non_red_alerts_and_db_legacy_migration(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        app_js = (repo_root / "src" / "xiaoshuo_bot" / "server" / "static" / "app.js").read_text(encoding="utf-8")
        styles_css = (repo_root / "src" / "xiaoshuo_bot" / "server" / "static" / "styles.css").read_text(encoding="utf-8")

        # UI static contract: rate_limit_retry & model_restored use non-red classes (.is-info / .is-ok)
        self.assertIn('RATE_LIMIT_RETRY: "rate_limit_retry"', app_js)
        self.assertIn('MODEL_RESTORED: "model_restored"', app_js)
        self.assertIn("被限流，第 ", app_js)
        self.assertIn(" 次等待重试", app_js)
        self.assertIn("已切回 ", app_js)
        self.assertIn("model-alert-card is-info", app_js)
        self.assertIn("model-alert-card is-ok", app_js)
        self.assertIn(".model-alert-card.is-info", styles_css)
        self.assertIn(".model-alert-card.is-ok", styles_css)

        # Legacy DB migration test: old model_switch_events table without event_type/retry_* columns
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE model_switch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id TEXT NOT NULL DEFAULT '',
                    book_title TEXT NOT NULL DEFAULT '',
                    chapter INTEGER NOT NULL DEFAULT 0,
                    role TEXT NOT NULL,
                    from_model TEXT NOT NULL,
                    to_model TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status_code INTEGER,
                    upstream_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                INSERT INTO model_switch_events
                    (book_id, book_title, chapter, role, from_model, to_model, reason, status_code, upstream_code, created_at)
                VALUES ('b1', '旧书', 1, 'writer', 'Hy4 flash', 'DeepSeek-V4.1-Flash', 'legacy', 400, '14018', '2026-11-19T10:00:00+08:00');
                """
            )
            conn.commit()
            conn.close()

            migrated_db = StockpileDB(db_path)
            try:
                events = migrated_db.list_model_switch_events(book_id="b1")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event_type"], "model_switched")
                self.assertEqual(events[0]["retry_attempt"], 0)
                self.assertEqual(events[0]["max_retries"], 0)
                self.assertEqual(events[0]["wait_seconds"], 0.0)
            finally:
                migrated_db.close()


if __name__ == "__main__":
    unittest.main()
