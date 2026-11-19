from __future__ import annotations

import http.server
import json
from pathlib import Path
import shutil
import socketserver
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple
import unittest

from xiaoshuo_bot.contracts import (
    BookRunStatus,
    SSE_CPA_QUOTA_ALERT,
)
from xiaoshuo_bot.llm import (
    BudgetCircuitBreakerError,
    LLMConfigurationError,
    LLMRequest,
    LLMRequestError,
    LLMStreamTruncatedError,
    ModelSwitchEvent,
    QuotaExhaustedError,
    RoleEndpointConfig,
    RoleModelRouter,
    UsageBudgetLedger,
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
            )

            # Writer call 1: Hy4 flash (429) -> DeepSeek-V4.1-Flash (400/14018) -> Xiaomi MiMo-V2.6-Pro (OK)
            resp_w = router.generate("writer", _make_req(chapter=1))
            self.assertEqual(resp_w.model, "wb/test-mimo-v26-pro")
            self.assertEqual([c["model"] for c in calls], [
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

            # Writer call 2: MiMo-V2.6-Pro now ALSO returns 429 -> ALL exhausted -> QuotaExhaustedError, NEVER switches to Hy4!
            calls.clear()
            events.clear()
            with self.assertRaises(QuotaExhaustedError) as qctx:
                router.generate("writer", _make_req(chapter=2))
            self.assertIn("熔断停机", str(qctx.exception))
            self.assertEqual([c["model"] for c in calls], ["wb/test-mimo-v26-pro"])
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
                    }
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

                # Trigger writer switch via router (simulating chapter 1 draft)
                resp = router.generate("writer", _make_req(chapter=1, book_id="baiwu_dukou"))
                self.assertEqual(resp.model, "wb/test-ds-v41-flash")

                # Verify DB event record
                db_events = db.list_model_switch_events(book_id="baiwu_dukou")
                self.assertEqual(len(db_events), 1)
                self.assertEqual(db_events[0]["role"], "writer")
                self.assertIn("Hy4 flash", db_events[0]["from_model"])
                self.assertIn("DeepSeek-V4.1-Flash", db_events[0]["to_model"])
                self.assertEqual(db_events[0]["status_code"], 429)
                self.assertEqual(db_events[0]["book_title"], "白雾渡口")

                # Verify P1-2 SSE alert emitted with kind="model_switched"
                sse_events = [
                    ev for ev in scheduler.event_broker.get_recent_events(limit=20)
                    if ev.event == SSE_CPA_QUOTA_ALERT
                ]
                self.assertEqual(len(sse_events), 1)
                self.assertEqual(sse_events[0].data["kind"], "model_switched")
                self.assertIn("Hy4 flash", sse_events[0].data["from_model"])
                self.assertIn("DeepSeek-V4.1-Flash", sse_events[0].data["to_model"])
                self.assertTrue(sse_events[0].data["reason"])

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


if __name__ == "__main__":
    unittest.main()
