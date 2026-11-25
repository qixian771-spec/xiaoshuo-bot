from __future__ import annotations

import http.server
import json
import socketserver
import threading
from typing import Any, Dict, List, Tuple
import unittest

from xiaoshuo_bot.llm import (
    LLMConfigurationError,
    LLMRequest,
    ModelSwitchEvent,
    QuotaExhaustedError,
    RoleEndpointConfig,
    RoleModelRouter,
    WORKBUDDY_MANUAL_ONLY_SLOTS,
    WORKBUDDY_PLANNER_ROTATION_SLOTS,
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
    WORKBUDDY_SLOT_HY4,
    WORKBUDDY_SLOT_HY4_FLASH,
    WORKBUDDY_SLOT_MIMO_V2_6_PRO,
    WORKBUDDY_WRITER_ROTATION_SLOTS,
    WorkBuddyModelCatalog,
    validate_workbuddy_rotation_chain,
)
from xiaoshuo_bot.llm.workbuddy_policy import WORKBUDDY_STRONG_PLANNER_ROLES


CATALOG_MODELS: Dict[str, str] = {
    WORKBUDDY_SLOT_HY4_FLASH: "wb/test-hy4-flash",
    WORKBUDDY_SLOT_HY4: "wb/test-hy4-manual",
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH: "wb/test-ds-v41-flash",
    WORKBUDDY_SLOT_MIMO_V2_6_PRO: "wb/test-mimo-v26-pro",
}


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _sse_ok(model: str, text: str) -> List[str]:
    chunk1 = json.dumps(
        {
            "id": "chatcmpl-strong-planner",
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
        },
        ensure_ascii=False,
    )
    chunk2 = json.dumps(
        {
            "id": "chatcmpl-strong-planner",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 60, "total_tokens": 160},
        },
        ensure_ascii=False,
    )
    return [f"data: {chunk1}\n\n", f"data: {chunk2}\n\n", "data: [DONE]\n\n"]


def _start_scripted_server(handler_fn) -> Tuple[_ThreadedHTTPServer, str, List[Dict[str, Any]]]:
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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}", calls


def _make_req(purpose: str = "plan", chapter: int = 0) -> LLMRequest:
    return LLMRequest(
        request_id=f"req-{purpose}-{chapter}",
        chapter=chapter,
        attempt=1,
        purpose=purpose,
        system_prompt="sys",
        user_prompt="user",
        max_tokens=256,
        metadata={"book_id": "book_strong_planner"},
    )


class TestStrongPlannerRoutingPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = WorkBuddyModelCatalog.from_mapping(CATALOG_MODELS)

    def test_strong_planner_constants_and_all_strong_roles_default_to_mimo(self) -> None:
        self.assertEqual(
            WORKBUDDY_PLANNER_ROTATION_SLOTS,
            (WORKBUDDY_SLOT_MIMO_V2_6_PRO, WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH),
        )
        self.assertEqual(
            WORKBUDDY_STRONG_PLANNER_ROLES,
            ("planner", "architect", "volume_planner", "volume_auditor"),
        )
        self.assertEqual(WORKBUDDY_MANUAL_ONLY_SLOTS, (WORKBUDDY_SLOT_HY4,))
        self.assertEqual(self.catalog.strong_planner_model(), CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])
        self.assertEqual(
            self.catalog.planner_rotation(),
            (
                CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
            ),
        )

        expected_primary = CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO]
        expected_chain = (
            CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
            CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
        )
        for role in ("planner", "architect", "volume_planner", "volume_auditor"):
            with self.subTest(role=role):
                primary, chain = self.catalog.default_role_model_and_rotation(role)
                self.assertEqual(primary, expected_primary)
                self.assertEqual(chain, expected_chain)
                self.assertNotIn(CATALOG_MODELS[WORKBUDDY_SLOT_HY4], chain)
                self.assertNotIn(CATALOG_MODELS[WORKBUDDY_SLOT_HY4_FLASH], chain)

        # Trimmed strong role name resolves consistently
        primary_ws, chain_ws = self.catalog.default_role_model_and_rotation("  volume_auditor  ")
        self.assertEqual(primary_ws, expected_primary)
        self.assertEqual(chain_ws, expected_chain)

        # Explicit override on a strong planner role to deepseek_v4_1_flash narrows chain from deepseek forward
        ov_primary, ov_chain = self.catalog.default_role_model_and_rotation(
            "volume_planner",
            override_model=CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
        )
        self.assertEqual(ov_primary, CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH])
        self.assertEqual(ov_chain, (CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],))

    def test_writer_default_chain_and_explicit_deepseek_override(self) -> None:
        expected_writer_chain = (
            CATALOG_MODELS[WORKBUDDY_SLOT_HY4_FLASH],
            CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
            CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
        )
        self.assertEqual(
            WORKBUDDY_WRITER_ROTATION_SLOTS,
            (
                WORKBUDDY_SLOT_HY4_FLASH,
                WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
                WORKBUDDY_SLOT_MIMO_V2_6_PRO,
            ),
        )
        self.assertEqual(self.catalog.writer_rotation(), expected_writer_chain)

        # Default writing/review roles keep writer_rotation() untouched
        for role in ("writer", "critic", "polisher"):
            with self.subTest(role=role):
                primary, chain = self.catalog.default_role_model_and_rotation(role)
                self.assertEqual(primary, CATALOG_MODELS[WORKBUDDY_SLOT_HY4_FLASH])
                self.assertEqual(chain, expected_writer_chain)
                self.assertNotIn(CATALOG_MODELS[WORKBUDDY_SLOT_HY4], chain)

        # Explicit deepseek_v4_1_flash override for writer prose starts at deepseek -> mimo (no hy4)
        ds_primary, ds_chain = self.catalog.default_role_model_and_rotation(
            "writer",
            override_model=CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
        )
        self.assertEqual(ds_primary, CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH])
        self.assertEqual(
            ds_chain,
            (
                CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
            ),
        )
        self.assertNotIn(CATALOG_MODELS[WORKBUDDY_SLOT_HY4], ds_chain)

    def test_hy4_strictly_banned_from_automatic_rotation_across_all_roles(self) -> None:
        hy4_id = CATALOG_MODELS[WORKBUDDY_SLOT_HY4]
        self.assertNotIn(WORKBUDDY_SLOT_HY4, WORKBUDDY_PLANNER_ROTATION_SLOTS)
        self.assertNotIn(WORKBUDDY_SLOT_HY4, WORKBUDDY_WRITER_ROTATION_SLOTS)
        self.assertNotIn(hy4_id, self.catalog.planner_rotation())
        self.assertNotIn(hy4_id, self.catalog.writer_rotation())

        all_roles = ("planner", "architect", "volume_planner", "volume_auditor", "writer", "critic", "polisher")
        for role in all_roles:
            with self.subTest(role=role):
                _, default_chain = self.catalog.default_role_model_and_rotation(role)
                self.assertNotIn(hy4_id, default_chain)

                # Manual selection of Hy4 returns empty auto-rotation chain
                manual_primary, manual_chain = self.catalog.default_role_model_and_rotation(
                    role, override_model=hy4_id
                )
                self.assertEqual(manual_primary, hy4_id)
                self.assertEqual(manual_chain, ())

                # Injecting Hy4 into rotation_models must raise LLMConfigurationError
                with self.assertRaises(LLMConfigurationError) as ctx_fallback:
                    validate_workbuddy_rotation_chain(
                        role=role,
                        primary_model=CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                        rotation_models=(
                            CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                            CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                            hy4_id,
                        ),
                        whitelist=self.catalog.whitelist(),
                        manual_only_models=self.catalog.manual_only(),
                    )
                self.assertIn("Hy4", str(ctx_fallback.exception))

                with self.assertRaises(LLMConfigurationError) as ctx_primary:
                    validate_workbuddy_rotation_chain(
                        role=role,
                        primary_model=hy4_id,
                        rotation_models=(CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],),
                        whitelist=self.catalog.whitelist(),
                        manual_only_models=self.catalog.manual_only(),
                    )
                self.assertIn("Hy4", str(ctx_primary.exception))

        # RoleEndpointConfig rejects Hy4 in planner rotation
        with self.assertRaises(LLMConfigurationError) as ctx_cfg:
            RoleEndpointConfig(
                role="planner",
                base_url="http://127.0.0.1:8317",
                model=CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                api_keys=["sk-test-key"],
                model_whitelist=self.catalog.whitelist(),
                rotation_models=(CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO], hy4_id),
                manual_only_models=self.catalog.manual_only(),
            )
        self.assertIn("Hy4", str(ctx_cfg.exception))

    def test_role_model_router_routes_planner_to_mimo_and_writer_to_deepseek(self) -> None:
        def handler(payload: Dict[str, Any], _seq: int):
            m = str(payload.get("model") or "")
            return 200, _sse_ok(m, f"reply-from-{m}"), True

        server, base_url, calls = _start_scripted_server(handler)
        try:
            # 1. Explicit DeepSeek-V4.1-Flash for writer prose + default strong MiMo for planner
            router = RoleModelRouter.from_workbuddy_catalog(
                self.catalog,
                base_url=base_url,
                api_keys=["sk-cpa-strong-test"],
                role_model_overrides={"writer": CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH]},
                sleep_fn=lambda _s: None,
            )
            self.assertEqual(router.get_role_config("planner").model, CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])
            self.assertEqual(
                router.get_role_config("planner").rotation_models,
                (
                    CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                    CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                ),
            )
            self.assertEqual(
                router.get_role_config("writer").model,
                CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
            )
            self.assertEqual(
                router.get_role_config("writer").rotation_models,
                (
                    CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                    CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                ),
            )

            resp_planner = router.generate("planner", _make_req(purpose="outline", chapter=0))
            resp_writer = router.generate("writer", _make_req(purpose="draft", chapter=1))

            self.assertEqual(resp_planner.model, CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])
            self.assertEqual(resp_writer.model, CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH])
            self.assertEqual(
                [c["model"] for c in calls],
                [
                    CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                    CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                ],
            )

            # 2. Default writer without override still hits hy4_flash while planner hits mimo_v2_6_pro
            calls.clear()
            default_router = RoleModelRouter.from_workbuddy_catalog(
                self.catalog,
                base_url=base_url,
                api_keys=["sk-cpa-strong-test"],
                sleep_fn=lambda _s: None,
            )
            resp_p2 = default_router.generate("planner", _make_req(purpose="volume_audit", chapter=0))
            resp_w2 = default_router.generate("writer", _make_req(purpose="draft", chapter=1))
            self.assertEqual(resp_p2.model, CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])
            self.assertEqual(resp_w2.model, CATALOG_MODELS[WORKBUDDY_SLOT_HY4_FLASH])
            self.assertEqual(
                [c["model"] for c in calls],
                [
                    CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                    CATALOG_MODELS[WORKBUDDY_SLOT_HY4_FLASH],
                ],
            )

            # 3. RoleModelRouter.from_dict with workbuddy_models + writer override routes consistently
            calls.clear()
            dict_router = RoleModelRouter.from_dict(
                {
                    "workbuddy_models": CATALOG_MODELS,
                    "base_url": base_url,
                    "api_keys": ["sk-cpa-strong-test"],
                    "role_model_overrides": {
                        "writer": CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                    },
                },
                sleep_fn=lambda _s: None,
            )
            resp_p3 = dict_router.generate("planner", _make_req(purpose="architect", chapter=0))
            resp_w3 = dict_router.generate("writer", _make_req(purpose="draft", chapter=2))
            self.assertEqual(resp_p3.model, CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])
            self.assertEqual(resp_w3.model, CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH])
            self.assertEqual(
                [c["model"] for c in calls],
                [
                    CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                    CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                ],
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_planner_failover_mimo_to_deepseek_then_halts_never_touching_hy4(self) -> None:
        ds_exhausted = {"flag": False}

        def handler(payload: Dict[str, Any], _seq: int):
            m = str(payload.get("model") or "")
            if m == CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO]:
                return 400, {
                    "error": {
                        "code": "credit_package_exhausted",
                        "message": "WorkBuddy requires an active credit package with available balance (upstream code 14018)",
                    }
                }, False
            if m == CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH]:
                if ds_exhausted["flag"]:
                    return 400, {
                        "error": {
                            "code": "credit_package_exhausted",
                            "message": "WorkBuddy requires an active credit package with available balance (upstream code 14018)",
                        }
                    }, False
                return 200, _sse_ok(m, "planner-failover-ok"), True
            raise AssertionError(f"Planner MUST NEVER call non-planner model: {m!r}")

        server, base_url, calls = _start_scripted_server(handler)
        try:
            switches: List[ModelSwitchEvent] = []
            router = RoleModelRouter.from_workbuddy_catalog(
                self.catalog,
                base_url=base_url,
                api_keys=["sk-cpa-strong-test"],
                on_model_switched=switches.append,
                sleep_fn=lambda _s: None,
            )

            # First call: MiMo hits 14018 -> immediately switches to DeepSeek-V4.1-Flash
            resp = router.generate("planner", _make_req(purpose="outline", chapter=0))
            self.assertEqual(resp.model, CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH])
            self.assertEqual(
                [c["model"] for c in calls],
                [
                    CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                    CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH],
                ],
            )
            self.assertEqual(len(switches), 1)
            self.assertEqual(switches[0].role, "planner")
            self.assertEqual(switches[0].from_model, CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])
            self.assertEqual(switches[0].to_model, CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH])
            self.assertEqual(router.get_active_model("planner"), CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH])

            # Second call: DeepSeek also hits 14018 -> QuotaExhaustedError halt, NEVER calls Hy4 or Hy4 flash
            calls.clear()
            switches.clear()
            ds_exhausted["flag"] = True
            with self.assertRaises(QuotaExhaustedError) as qctx:
                router.generate("planner", _make_req(purpose="outline", chapter=0))
            self.assertIn("熔断停机", str(qctx.exception))
            self.assertEqual([c["model"] for c in calls], [CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH]])
            self.assertEqual(switches, [])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
