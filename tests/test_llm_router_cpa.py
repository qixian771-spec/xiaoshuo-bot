import base64
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.llm import (
    AnthropicProvider,
    BudgetCircuitBreakerError,
    CPAManagementClient,
    CPAManagementError,
    FileBridgeProvider,
    ImageGenerationRequest,
    LLMConfigurationError,
    LLMRequest,
    LLMRequestError,
    LLMStreamTruncatedError,
    OpenAICompatibleProvider,
    QuotaExhaustedError,
    RoleEndpointConfig,
    RoleModelRouter,
    SHANGHAI_TZ,
    UsageBudgetLedger,
    VALID_ROLES,
)


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now


def _parse_multipart_file(content_type: str, body: bytes) -> tuple[str, bytes]:
    match = re.search(r"boundary=([^\s;]+)", content_type)
    if not match:
        raise ValueError("Missing multipart boundary")
    boundary = match.group(1).strip('"').encode("ascii")
    delimiter = b"--" + boundary
    parts = body.split(delimiter)
    for part in parts:
        if b"Content-Disposition:" not in part:
            continue
        header_blob, _, payload = part.partition(b"\r\n\r\n")
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        header_text = header_blob.decode("utf-8", errors="replace")
        fn_match = re.search(r'filename="([^"]+)"', header_text)
        filename = fn_match.group(1) if fn_match else ""
        return filename, payload
    raise ValueError("No file part found in multipart body")


class _SimulatedCPAServer:
    """In-process 127.0.0.1:0 HTTP/SSE server simulating CLIProxyAPI gateway & management API."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.auth_files: dict[str, bytes] = {
            "seed-codex.json": b'{"type":"codex","email":"seed@example.com"}'
        }
        self.rate_limited_keys: set[str] = set()
        self.sse_mode: str = "normal"  # "normal" | "truncate_length" | "premature_eof" | "echo_secret_500"
        self.management_key: str = "mgmt-secret-token-xyz789"
        self.sample_png_bytes: bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc`\x00\x00"
            b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:
                return

            def _send_json(self, status: int, payload: object) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or "0")
                return self.rfile.read(length) if length > 0 else b""

            def _bearer_token(self) -> str:
                auth = self.headers.get("Authorization") or ""
                if auth.startswith("Bearer "):
                    return auth[7:].strip()
                return ""

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)
                token = self._bearer_token()
                outer.requests.append({"method": "GET", "path": path, "query": query, "token": token})

                if path == "/v1/models":
                    if token in outer.rate_limited_keys:
                        self._send_json(429, {"error": {"message": f"rate limited for {token}"}})
                        return
                    self._send_json(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {"id": "gpt-5"},
                                {"id": "claude-sonnet-4-5"},
                                {"id": "gemini-2.5-flash-image"},
                            ],
                        },
                    )
                    return

                if path.startswith("/v0/management/"):
                    if token != outer.management_key:
                        self._send_json(401, {"error": f"unauthorized token {token}"})
                        return
                    sub = path[len("/v0/management/") :]
                    if sub == "auth-files":
                        items = [
                            {"name": name, "size": len(data), "status": "active"}
                            for name, data in sorted(outer.auth_files.items())
                        ]
                        self._send_json(200, {"files": items})
                        return
                    if sub == "usage":
                        self._send_json(
                            200,
                            {"total_requests": len(outer.requests), "active_files": len(outer.auth_files)},
                        )
                        return
                    if sub == "anthropic-auth-url":
                        self._send_json(
                            200,
                            {
                                "status": "ok",
                                "url": "https://claude.ai/oauth/authorize?state=st_123",
                                "state": "st_123",
                            },
                        )
                        return
                    if sub == "get-auth-status":
                        state = (query.get("state") or [""])[0]
                        self._send_json(200, {"status": "ok", "state": state})
                        return

                self._send_json(404, {"error": f"not found: {path}"})

            def do_POST(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                body = self._read_body()
                token = self._bearer_token()

                if path.startswith("/v0/management/"):
                    if token != outer.management_key:
                        self._send_json(401, {"error": f"unauthorized token {token}"})
                        return
                    if path == "/v0/management/auth-files":
                        ctype = self.headers.get("Content-Type") or ""
                        filename, file_bytes = _parse_multipart_file(ctype, body)
                        outer.auth_files[filename] = file_bytes
                        self._send_json(200, {"status": "ok", "name": filename, "size": len(file_bytes)})
                        return

                payload = json.loads(body.decode("utf-8")) if body else {}
                outer.requests.append(
                    {"method": "POST", "path": path, "token": token, "payload": payload}
                )

                if outer.sse_mode == "echo_secret_500":
                    self._send_json(
                        500,
                        {"error": {"message": f"upstream failure with Authorization: Bearer {token}"}},
                    )
                    return

                if token in outer.rate_limited_keys:
                    self._send_json(
                        429,
                        {"error": {"message": f"quota exceeded for Bearer {token}"}},
                    )
                    return

                if path == "/v1/chat/completions":
                    if payload.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.end_headers()

                        def _emit(line: str) -> None:
                            self.wfile.write((line + "\n").encode("utf-8"))
                            self.wfile.flush()

                        _emit(": keepalive")
                        _emit("")
                        _emit(": ping")
                        chunk1 = {
                            "id": "chatcmpl-1",
                            "model": payload.get("model"),
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "reasoning_content": "internal thought should not enter prose",
                                        "content": "雾色压低了江面，",
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                        _emit(f"data: {json.dumps(chunk1, ensure_ascii=False)}")
                        _emit("")

                        if outer.sse_mode == "premature_eof":
                            self.close_connection = True
                            return

                        _emit(": keepalive")
                        _emit("")
                        chunk2 = {
                            "id": "chatcmpl-1",
                            "model": payload.get("model"),
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "铁门上的铜铃忽然响了一声。"},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        _emit(f"data: {json.dumps(chunk2, ensure_ascii=False)}")
                        _emit("")

                        final_reason = "length" if outer.sse_mode == "truncate_length" else "stop"
                        chunk_end = {
                            "id": "chatcmpl-1",
                            "model": payload.get("model"),
                            "choices": [{"index": 0, "delta": {}, "finish_reason": final_reason}],
                        }
                        _emit(f"data: {json.dumps(chunk_end, ensure_ascii=False)}")
                        _emit("")
                        usage_chunk = {
                            "id": "chatcmpl-1",
                            "model": payload.get("model"),
                            "choices": [],
                            "usage": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165},
                        }
                        _emit(f"data: {json.dumps(usage_chunk, ensure_ascii=False)}")
                        _emit("")
                        _emit("data: [DONE]")
                        _emit("")
                        return

                    self._send_json(
                        200,
                        {
                            "model": payload.get("model"),
                            "choices": [
                                {"message": {"content": "非流式正文"}, "finish_reason": "stop"}
                            ],
                            "usage": {"prompt_tokens": 50, "completion_tokens": 20},
                        },
                    )
                    return

                if path == "/v1/messages":
                    self._send_json(
                        200,
                        {
                            "model": payload.get("model"),
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "Anthropic 测试正文"}],
                            "usage": {"input_tokens": 80, "output_tokens": 30},
                        },
                    )
                    return

                if path.startswith("/v1beta/models/") and path.endswith(":generateContent"):
                    b64 = base64.b64encode(outer.sample_png_bytes).decode("ascii")
                    self._send_json(
                        200,
                        {
                            "candidates": [
                                {
                                    "content": {
                                        "parts": [
                                            {"text": "Generated dark thriller cover base."},
                                            {"inlineData": {"mimeType": "image/png", "data": b64}},
                                        ]
                                    }
                                }
                            ],
                            "usageMetadata": {"promptTokenCount": 35, "candidatesTokenCount": 250},
                        },
                    )
                    return

                if path == "/v1/images/generations":
                    b64 = base64.b64encode(outer.sample_png_bytes).decode("ascii")
                    self._send_json(
                        200,
                        {
                            "data": [{"b64_json": b64, "revised_prompt": "neon rainy alley"}],
                            "usage": {"prompt_tokens": 25, "completion_tokens": 0},
                        },
                    )
                    return

                self._send_json(404, {"error": f"unknown POST path: {path}"})

            def do_DELETE(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)
                token = self._bearer_token()
                outer.requests.append({"method": "DELETE", "path": path, "query": query, "token": token})

                if path == "/v0/management/auth-files":
                    if token != outer.management_key:
                        self._send_json(401, {"error": "unauthorized"})
                        return
                    name = (query.get("name") or [""])[0]
                    if name not in outer.auth_files:
                        self._send_json(404, {"error": f"file not found: {name}"})
                        return
                    del outer.auth_files[name]
                    self._send_json(200, {"status": "deleted", "name": name})
                    return

                self._send_json(404, {"error": f"unknown DELETE path: {path}"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=3)


class TestLLMRouterAndCPA(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _SimulatedCPAServer()
        self.server.start()
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "XIAOSHUO_API_KEY",
                "OPENAI_API_KEY",
                "XIAOSHUO_API_BASE",
                "OPENAI_BASE_URL",
                "XIAOSHUO_MODEL",
                "OPENAI_MODEL",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_MODEL",
                "CPA_MANAGEMENT_URL",
                "CPA_MANAGEMENT_KEY",
            )
        }
        for k in self._saved_env:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        self.server.stop()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_config_raises_llm_configuration_error(self) -> None:
        with self.assertRaises(LLMConfigurationError):
            OpenAICompatibleProvider()
        with self.assertRaises(LLMConfigurationError):
            OpenAICompatibleProvider(api_key="sk-test-key-1234")
        with self.assertRaises(LLMConfigurationError):
            OpenAICompatibleProvider(api_key="sk-test-key-1234", base_url=f"{self.server.base_url}/v1")

        with self.assertRaises(LLMConfigurationError):
            AnthropicProvider()
        with self.assertRaises(LLMConfigurationError):
            AnthropicProvider(api_key="sk-ant-1234", base_url=self.server.base_url)

        with self.assertRaises(LLMConfigurationError):
            CPAManagementClient()
        with self.assertRaises(LLMConfigurationError):
            CPAManagementClient(base_url=self.server.base_url)

        with self.assertRaises(LLMConfigurationError):
            RoleEndpointConfig(role="unknown_role", base_url=self.server.base_url, model="m", api_keys=["k"])
        with self.assertRaises(LLMConfigurationError):
            RoleEndpointConfig(role="writer", base_url=self.server.base_url, model="m", api_keys=["   "])

    def test_sse_streaming_keepalive_reasoning_suffix_and_list_models(self) -> None:
        provider = OpenAICompatibleProvider(
            api_key="sk-live-test-0001",
            base_url=f"{self.server.base_url}/v1",
            model="gpt-5(high)",
            timeout=30,
        )
        models = provider.list_models()
        self.assertEqual(models, ["gpt-5", "claude-sonnet-4-5", "gemini-2.5-flash-image"])

        req = LLMRequest(
            request_id="req-sse-1",
            chapter=1,
            attempt=1,
            purpose="draft",
            system_prompt="sys",
            user_prompt="usr",
        )
        resp = provider.generate(req)
        self.assertEqual(resp.content, "雾色压低了江面，铁门上的铜铃忽然响了一声。")
        self.assertNotIn("internal thought", resp.content)
        self.assertEqual(resp.usage.get("prompt_tokens"), 120)
        self.assertEqual(resp.usage.get("completion_tokens"), 45)

        chat_reqs = [r for r in self.server.requests if r["path"] == "/v1/chat/completions"]
        self.assertEqual(len(chat_reqs), 1)
        sent = chat_reqs[0]["payload"]
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["model"], "gpt-5(high)")
        self.assertEqual(sent["reasoning_effort"], "high")

    def test_sse_truncated_length_and_premature_eof_raise_error(self) -> None:
        provider = OpenAICompatibleProvider(
            api_key="sk-live-test-0002",
            base_url=f"{self.server.base_url}/v1",
            model="claude-sonnet-4-5",
        )
        req = LLMRequest(
            request_id="req-trunc",
            chapter=1,
            attempt=1,
            purpose="draft",
            system_prompt="sys",
            user_prompt="usr",
        )

        self.server.sse_mode = "truncate_length"
        with self.assertRaises(LLMStreamTruncatedError):
            provider.generate(req)

        self.server.sse_mode = "premature_eof"
        with self.assertRaises(LLMStreamTruncatedError):
            provider.generate(req)

    def test_router_records_usage_on_stream_truncation(self) -> None:
        ledger = UsageBudgetLedger()
        router = RoleModelRouter(
            roles={
                "writer": {
                    "base_url": f"{self.server.base_url}/v1",
                    "model": "claude-sonnet-4-5",
                    "api_keys": ["sk-trunc-key-1"],
                    "input_cost_per_1m": 100.0,
                    "output_cost_per_1m": 400.0,
                }
            },
            ledger=ledger,
        )
        self.server.sse_mode = "truncate_length"
        with self.assertRaises(LLMStreamTruncatedError) as ctx:
            router.generate("writer", LLMRequest("t1", 3, 1, "draft", "sys", "usr"))
        self.assertEqual(ctx.exception.usage.get("completion_tokens"), 45)
        self.assertTrue(ctx.exception.partial_content)
        summary = ledger.get_summary()
        self.assertEqual(summary["request_count"], 1)
        self.assertEqual(summary["input_tokens"], 120)
        self.assertEqual(summary["output_tokens"], 45)
        self.assertAlmostEqual(summary["estimated_cost_usd"], 0.03, places=6)

    def test_five_roles_429_rotation_no_downgrade_and_zero_key_leak(self) -> None:
        clock = _FakeClock(1000.0)
        primary_key = "sk-primary-secret-alpha999"
        backup_key = "sk-backup-secret-beta888"
        self.server.rate_limited_keys.add(primary_key)

        router = RoleModelRouter(
            roles={
                "planner": {
                    "base_url": f"{self.server.base_url}/v1",
                    "model": "gpt-5(medium)",
                    "api_keys": [backup_key],
                },
                "writer": {
                    "base_url": f"{self.server.base_url}/v1",
                    "model": "claude-sonnet-4-5",
                    "api_keys": [primary_key, backup_key],
                    "cooldown_sec": 60.0,
                },
                "critic": {
                    "base_url": self.server.base_url,
                    "model": "claude-sonnet-4-5",
                    "api_keys": [backup_key],
                    "provider_type": "anthropic",
                },
                "polisher": {
                    "base_url": f"{self.server.base_url}/v1",
                    "model": "gpt-5(high)",
                    "api_keys": [backup_key],
                },
                "image": {
                    "base_url": f"{self.server.base_url}/v1",
                    "model": "gemini-2.5-flash-image",
                    "api_keys": [backup_key],
                },
            },
            monotonic_fn=clock,
        )
        for role in VALID_ROLES:
            self.assertTrue(router.has_role(role))

        # Primary key hits 429 -> seamlessly rotates to backup key with exact same model
        writer_provider = router.as_provider("writer")
        resp = writer_provider.generate(
            LLMRequest("r1", 1, 1, "draft", "sys", "usr")
        )
        self.assertEqual(resp.content, "雾色压低了江面，铁门上的铜铃忽然响了一声。")

        # Verify critic Anthropic role works too
        critic_resp = router.generate("critic", LLMRequest("r2", 1, 1, "critic", "sys", "usr"))
        self.assertEqual(critic_resp.content, "Anthropic 测试正文")

        # Now exhaust ALL keys for writer -> must raise QuotaExhaustedError, never downgrade model
        self.server.rate_limited_keys.add(backup_key)
        with self.assertRaises(QuotaExhaustedError) as ctx:
            router.generate("writer", LLMRequest("r3", 2, 1, "draft", "sys", "usr"))
        err_msg = str(ctx.exception)
        self.assertNotIn(primary_key, err_msg)
        self.assertNotIn(backup_key, err_msg)
        self.assertNotIn("alpha999", err_msg)
        self.assertNotIn("beta888", err_msg)

        # Verify no model downgrade occurred across all requests
        writer_calls = [
            r for r in self.server.requests
            if r["path"] == "/v1/chat/completions"
        ]
        for call in writer_calls:
            self.assertEqual(call["payload"]["model"], "claude-sonnet-4-5")

        # While still in cooldown, even if server recovers, router skips cooled keys until cooldown expires
        self.server.rate_limited_keys.clear()
        with self.assertRaises(QuotaExhaustedError):
            router.generate("writer", LLMRequest("r4", 2, 1, "draft", "sys", "usr"))

        clock.now += 61.0
        recovered = router.generate("writer", LLMRequest("r5", 2, 1, "draft", "sys", "usr"))
        self.assertIn("雾色压低了江面", recovered.content)

    def test_secret_redaction_in_upstream_errors(self) -> None:
        secret = "sk-ultra-sensitive-token-776655"
        self.server.sse_mode = "echo_secret_500"
        provider = OpenAICompatibleProvider(
            api_key=secret,
            base_url=f"{self.server.base_url}/v1",
            model="gpt-5",
            max_retries=1,
        )
        with self.assertRaises(LLMRequestError) as ctx:
            provider.generate(LLMRequest("r-err", 1, 1, "draft", "sys", "usr"))
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn("776655", str(ctx.exception))
        self.assertIn("[REDACTED]", str(ctx.exception))

    def test_dual_protocol_image_generation(self) -> None:
        # 1. Gemini native :generateContent via CPA /v1 root
        router_gemini = RoleModelRouter(
            roles={
                "image": {
                    "base_url": f"{self.server.base_url}/v1",
                    "model": "gemini-2.5-flash-image",
                    "api_keys": ["sk-img-key-1", "sk-img-key-2"],
                    "image_cost_per_call": 0.04,
                }
            }
        )
        self.server.rate_limited_keys.add("sk-img-key-1")
        img1 = router_gemini.generate_image(
            ImageGenerationRequest(prompt="rainy cyberpunk street, no text", aspect_ratio="3:4")
        )
        self.assertEqual(img1.protocol, "gemini_native")
        self.assertEqual(img1.mime_type, "image/png")
        self.assertEqual(img1.image_bytes, self.server.sample_png_bytes)

        gemini_reqs = [
            r for r in self.server.requests
            if r["path"] == "/v1beta/models/gemini-2.5-flash-image:generateContent"
        ]
        self.assertEqual(len(gemini_reqs), 2)  # 429 on key-1, success on key-2
        self.assertEqual(
            gemini_reqs[1]["payload"]["generationConfig"]["responseModalities"],
            ["TEXT", "IMAGE"],
        )

        # 2. OpenAI-compatible /v1/images/generations
        router_oai = RoleModelRouter(
            roles={
                "image": {
                    "base_url": f"{self.server.base_url}/v1",
                    "model": "gpt-image-1",
                    "api_keys": ["sk-img-key-2"],
                    "provider_type": "openai_images",
                }
            }
        )
        img2 = router_oai.generate_image("ink wash mountain cover art")
        self.assertEqual(img2.protocol, "openai_images")
        self.assertEqual(img2.image_bytes, self.server.sample_png_bytes)

    def test_usage_budget_ledger_shanghai_tz_and_circuit_breakers(self) -> None:
        # 2026-11-18 23:30 UTC+9 (KST) == 2026-11-18 22:30 UTC+8 (CST)
        kst_tz = timezone(timedelta(hours=9))
        fixed_kst_time = datetime(2026, 11, 18, 23, 30, tzinfo=kst_tz)
        self.assertEqual(fixed_kst_time.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d"), "2026-11-18")

        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "ledger.json"
            ledger = UsageBudgetLedger(
                daily_budget_limit=0.05,
                daily_chapter_limit=2,
                state_path=ledger_path,
                now_fn=lambda: fixed_kst_time,
            )
            router = RoleModelRouter(
                roles={
                    "writer": {
                        "base_url": f"{self.server.base_url}/v1",
                        "model": "claude-sonnet-4-5",
                        "api_keys": ["sk-budget-key-1"],
                        "input_cost_per_1m": 100.0,   # 120 tok = $0.012
                        "output_cost_per_1m": 400.0,  # 45 tok  = $0.018 => $0.030/call
                    }
                },
                ledger=ledger,
            )

            # Call 1 (chapter 1): $0.030 <= $0.05 -> passes
            router.generate("writer", LLMRequest("b1", 1, 1, "draft", "sys", "usr"))
            summary = ledger.get_summary()
            self.assertEqual(summary["date"], "2026-11-18")
            self.assertEqual(summary["chapters"], [1])
            self.assertAlmostEqual(summary["estimated_cost_usd"], 0.03, places=6)

            # Call 2 (chapter 2): $0.060 > $0.05 -> paid response is still returned & recorded,
            # ledger is marked tripped, and the NEXT call trips BudgetCircuitBreakerError.
            resp2 = router.generate("writer", LLMRequest("b2", 2, 1, "draft", "sys", "usr"))
            self.assertIn("雾色压低了江面", resp2.content)
            summary2 = ledger.get_summary()
            self.assertAlmostEqual(summary2["estimated_cost_usd"], 0.06, places=6)
            self.assertTrue(summary2.get("tripped"))
            calls_before = len(self.server.requests)
            with self.assertRaises(BudgetCircuitBreakerError):
                router.generate("writer", LLMRequest("b3", 2, 2, "rewrite", "sys", "usr"))
            self.assertEqual(len(self.server.requests), calls_before)

            # Reload persisted ledger from disk and verify chapter limit enforcement
            chapter_ledger = UsageBudgetLedger(
                daily_budget_limit=100.0,
                daily_chapter_limit=1,
                state_path=ledger_path,
                now_fn=lambda: fixed_kst_time,
            )
            # ledger already recorded chapter 1 & 2 before tripping budget; test fresh chapter cap:
            fresh_ledger = UsageBudgetLedger(
                daily_chapter_limit=1,
                now_fn=lambda: fixed_kst_time,
            )
            fresh_ledger.record_chapter(1)
            fresh_ledger.check_can_proceed(chapter=1)  # same chapter rewrite allowed
            with self.assertRaises(BudgetCircuitBreakerError):
                fresh_ledger.check_can_proceed(chapter=2)

    def test_cpa_management_client_crud_usage_and_oauth(self) -> None:
        client = CPAManagementClient(
            base_url=self.server.base_url,
            management_key=self.server.management_key,
        )

        files = client.list_auth_files()
        self.assertEqual([f["name"] for f in files], ["seed-codex.json"])

        with tempfile.TemporaryDirectory() as tmpdir:
            cred_path = Path(tmpdir) / "claude-sub-01.json"
            cred_path.write_text('{"type":"claude","access_token":"tok"}', encoding="utf-8")
            up_res = client.upload_auth_file(cred_path)
            self.assertEqual(up_res["name"], "claude-sub-01.json")

            bad_ext = Path(tmpdir) / "not-json.txt"
            bad_ext.write_text("{}", encoding="utf-8")
            with self.assertRaises(LLMConfigurationError):
                client.upload_auth_file(bad_ext)

            bad_json = Path(tmpdir) / "broken.json"
            bad_json.write_text("{not-valid-json", encoding="utf-8")
            with self.assertRaises(LLMConfigurationError):
                client.upload_auth_file(bad_json)

        files_after_upload = client.list_auth_files()
        self.assertEqual(
            [f["name"] for f in files_after_upload],
            ["claude-sub-01.json", "seed-codex.json"],
        )

        del_res = client.delete_auth_file("claude-sub-01.json")
        self.assertEqual(del_res["status"], "deleted")
        self.assertEqual([f["name"] for f in client.list_auth_files()], ["seed-codex.json"])

        usage = client.get_usage()
        self.assertIn("total_requests", usage)

        oauth = client.get_oauth_url("claude")
        self.assertEqual(oauth["state"], "st_123")
        status = client.get_auth_status("st_123")
        self.assertEqual(status["status"], "ok")

        # Bad management key must redact key in CPAManagementError
        bad_client = CPAManagementClient(
            base_url=self.server.base_url,
            management_key="wrong-mgmt-secret-000",
        )
        with self.assertRaises(CPAManagementError) as ctx:
            bad_client.list_auth_files()
        self.assertNotIn("wrong-mgmt-secret-000", str(ctx.exception))

    def test_bridge_archives_instead_of_unlink_and_zero_unlink_in_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "DONE").write_text("COMPLETED\n", encoding="utf-8")
            resp_dir = root / "responses"
            resp_dir.mkdir(parents=True, exist_ok=True)
            stale_resp = resp_dir / "req-1.txt"
            stale_resp.write_text("stale response", encoding="utf-8")

            bridge = FileBridgeProvider(root, poll_interval=0.05, timeout_sec=2, reuse_existing=False)
            self.assertFalse((root / "DONE").exists())
            archived = list((root / "_archive").rglob("DONE"))
            self.assertEqual(len(archived), 1)

            def _writer() -> None:
                # Wait until stale response is archived and request file is created
                req_md = root / "requests" / "req-1.md"
                for _ in range(40):
                    if req_md.exists():
                        break
                    import time as _t
                    _t.sleep(0.02)
                stale_resp.write_text("fresh bridged response", encoding="utf-8")

            t = threading.Thread(target=_writer, daemon=True)
            t.start()
            res = bridge.generate(LLMRequest("req-1", 1, 1, "draft", "sys", "usr"))
            t.join(timeout=2)
            self.assertEqual(res.content, "fresh bridged response")
            archived_resps = list((root / "_archive").rglob("req-1.txt"))
            self.assertEqual(len(archived_resps), 1)

        # Static audit: zero '.unlink(' anywhere under src/xiaoshuo_bot/llm/
        llm_dir = ROOT / "src" / "xiaoshuo_bot" / "llm"
        for py_file in llm_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            self.assertNotIn(".unlink(", content, f"Forbidden .unlink( found in {py_file}")


if __name__ == "__main__":
    unittest.main()
