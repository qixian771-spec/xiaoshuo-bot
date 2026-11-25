"""Offline unit & integration tests for U5: end-of-volume 6-point health hard gate,
pre-volume lock enforcement, CLI `volume-check`, and backward compatibility.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import socketserver
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import unittest

import yaml

from xiaoshuo_bot import cli
from xiaoshuo_bot.blueprint import BookBlueprint, load_blueprint
from xiaoshuo_bot.cli import execute_volume_check
from xiaoshuo_bot.engine import NovelGenerationEngine, ProductionGateFailure
from xiaoshuo_bot.judge import PairwiseJudge, SameModelJudgeError
from xiaoshuo_bot.llm.base import (
    LLMConfigurationError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)
from xiaoshuo_bot.models import (
    AssetState,
    CharacterAnchor,
    ForeshadowItem,
    NovelProject,
    ProjectMeta,
    ScanReport,
    SceneOutline,
    StyleProfile,
)
from xiaoshuo_bot.volume_health import (
    LOCK_STATE_LOCKED,
    LOCK_STATE_PASSED,
    VOLUME_HEALTH_CHECK_IDS,
    VolumeHealthLockError,
    assert_previous_volume_unlocked,
    load_volume_locks,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "blueprint_valid.yaml"


def _make_short_blueprint(tmp_dir: Path) -> Tuple[BookBlueprint, Path]:
    """Create a valid BookBlueprint derived from fixture with compact volume spans:
    Vol 1: ch 1..3, Vol 2: ch 4..6, Vol 3: ch 7..9 (plus Vol 4..6 scaled cleanly).
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(BLUEPRINT_FIXTURE.read_text(encoding="utf-8"))
    spans = [(1, 3), (4, 6), (7, 9), (10, 12), (13, 15), (16, 18)]
    for vol, (start, end) in zip(raw["volumes"], spans):
        vol["chapter_start"] = start
        vol["chapter_end"] = end
    raw["target_total_chars"] = 18 * 2300
    bp_path = tmp_dir / "blueprint.yaml"
    bp_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return load_blueprint(bp_path), bp_path


def _write_disk_project(
    proj_dir: Path,
    bp: BookBlueprint,
    *,
    chapters_to_define: Sequence[int] = (1, 2, 3, 4),
) -> Tuple[NovelProject, Path]:
    """Write a complete valid NovelProject YAML directory + matching blueprint.yaml."""
    proj_dir.mkdir(parents=True, exist_ok=True)
    raw_bp = yaml.safe_load(BLUEPRINT_FIXTURE.read_text(encoding="utf-8"))
    spans = [(1, 3), (4, 6), (7, 9), (10, 12), (13, 15), (16, 18)]
    for vol, (start, end) in zip(raw_bp["volumes"], spans):
        vol["chapter_start"] = start
        vol["chapter_end"] = end
    raw_bp["target_total_chars"] = 18 * 2300
    bp_path = proj_dir / "blueprint.yaml"
    bp_path.write_text(
        yaml.safe_dump(raw_bp, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    (proj_dir / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "project_id": "u5_gate_book",
                "title": "南桥因果录",
                "genre": "都市脑洞",
                "synopsis": "夜市算卦断案，卷末六项体检硬闸门测试书。",
                "default_pov_character": "mc",
                "contract_track": "serial_20k",
                "max_rewrite_retries": 0,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    chars_payload = []
    for member in bp.cast:
        chars_payload.append(
            {
                "id": member.id,
                "name": member.name,
                "role": member.role,
                "personality": "沉稳克制",
                "speech_style": member.voice_card.speech_patterns,
            }
        )
    (proj_dir / "characters.yaml").write_text(
        yaml.safe_dump({"characters": chars_payload}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (proj_dir / "assets.yaml").write_text(
        yaml.safe_dump(
            {
                "assets": [
                    {
                        "id": "scale",
                        "name": "因果秤",
                        "location": "南桥夜市摊位木盒",
                        "holder": "许青川",
                        "condition": "完好",
                        "physical_details": "旧铜小秤",
                    }
                ],
                "initial_balances": {"cash": 5000.0, "followers": 1000.0},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    # Plant the two vol-1 foreshadows in ch 1 and ch 2 so vol-1 snapshot has status='planted'.
    (proj_dir / "foreshadows.yaml").write_text(
        yaml.safe_dump(
            {
                "foreshadows": [
                    {
                        "id": "fs_scale_origin",
                        "title": "因果秤砝码缺失一钱的来历",
                        "plant_chapter": 1,
                        "due_chapter": 12,
                        "plant_instruction": "点出旧铜砝码缺口",
                        "payoff_instruction": "省城旧宅揭示真相",
                        "plant_keywords": ["铜砝码"],
                        "payoff_keywords": ["公证封条"],
                        "cross_volume": True,
                    },
                    {
                        "id": "fs_fake_jade_seal",
                        "title": "假玉器行暗格同模印章",
                        "plant_chapter": 2,
                        "due_chapter": 9,
                        "plant_instruction": "发现暗格印章",
                        "payoff_instruction": "冷库航运账本核对",
                        "plant_keywords": ["同模印章"],
                        "payoff_keywords": ["航运登记簿"],
                        "cross_volume": True,
                    },
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (proj_dir / "style_profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "style_profiles": [
                    {
                        "id": "default_style",
                        "name": "白描快节奏",
                        "granularity_notes": "短句推进",
                        "sample_text": "夜市灯光刚亮。",
                    }
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    scenes_payload = []
    for ch in chapters_to_define:
        vol_idx = 1 if ch <= 3 else 2
        active = ["mc", "detective_ally", "vol1_boss"] if vol_idx == 1 else ["mc", "he_xiaoman"]
        scenes_payload.append(
            {
                "chapter": ch,
                "scene_id": f"v{vol_idx}_ch{ch:04d}",
                "internal_title": f"第{ch}章场景",
                "public_title": f"第{ch}章 夜市断事",
                "pov_character": "mc",
                "active_characters": active,
                "active_assets": ["scale"],
                "style_profile_ids": ["default_style"],
                "location_time": f"傍晚，南桥夜市第{ch}街口",
                "events": [f"第{ch}章推进核心算卦事件"],
                "stop_condition": "止于钩子",
                "location_id": f"loc_{ch}",
                "calendar_days_elapsed": 1,
                "volume": vol_idx,
            }
        )
    (proj_dir / "outline.yaml").write_text(
        yaml.safe_dump({"scenes": scenes_payload}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    from xiaoshuo_bot.loader import load_project

    return load_project(proj_dir), bp_path


class _PassChecker:
    def scan(self, text: str, **_: object) -> ScanReport:
        return ScanReport(
            passed=True,
            char_count=len(text),
            paragraph_count=text.count("\n\n") + 1,
        )


class _ScriptedWriterProvider(LLMProvider):
    def __init__(
        self,
        handler: Callable[[LLMRequest], str],
        *,
        model: str = "wb/deepseek-v4.1-flash",
    ) -> None:
        self._handler = handler
        self.model = model
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            request_id=request.request_id,
            content=self._handler(request),
            model=self.model,
            provider="fake_writer",
        )


class _QueuedJudgeProvider(LLMProvider):
    def __init__(
        self,
        payloads: Sequence[Dict[str, Any]],
        *,
        model: str = "wb/mimo-v2.6-pro",
    ) -> None:
        self._payloads = list(payloads)
        self.model = model
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        idx = min(len(self.requests) - 1, len(self._payloads) - 1)
        return LLMResponse(
            request_id=request.request_id,
            content=json.dumps(self._payloads[idx], ensure_ascii=False),
            model=self.model,
            provider="fake_judge",
        )


def _judge_payload(
    *,
    fail_id: Optional[str] = None,
    fail_chapters: Optional[List[int]] = None,
    fail_hint: str = "返修目标章节补足卷末钩子与节奏变化。",
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    for cid in VOLUME_HEALTH_CHECK_IDS:
        if cid == fail_id:
            checks.append(
                {
                    "id": cid,
                    "status": "FAIL",
                    "chapters": fail_chapters if fail_chapters is not None else [3],
                    "evidence": f"{cid} 语义体检发现明显缺陷。",
                    "repair_hint": fail_hint,
                }
            )
        else:
            checks.append(
                {
                    "id": cid,
                    "status": "PASS",
                    "chapters": [],
                    "evidence": f"{cid} 语义体检达标。",
                    "repair_hint": "",
                }
            )
    verdict = "FAIL" if fail_id is not None else "PASS"
    return {
        "verdict": verdict,
        "checks": checks,
        "summary": "整卷六项体检通过。" if verdict == "PASS" else f"整卷体检未通过（{fail_id}）。",
    }


def _clean_writer_text(req: LLMRequest) -> str:
    ch = req.chapter
    if ch == 1:
        return (
            "第1章雨夜南桥夜市，许青川擦拭因果秤与那枚缺了一钱的铜砝码，"
            "当场算卦观气拆穿假玉局，引出夜市首案线索编号101。"
        )
    if ch == 2:
        return (
            "第2章夜摊直播连麦，许青川在古玩街暗格发现二十年前的同模印章，"
            "顺着观气线索锁住钱万顺的销赃渠道，案号202。"
        )
    if ch == 3:
        return (
            "第3章地下夹层收网，宋海潮带队刑拘钱万顺，许青川算卦通缉线索落地，"
            "旧木牌与新直播间钩子齐发，顺利收束第一卷，编号303。"
        )
    return (
        f"第{ch}章老街茶室直播间启幕，许青川携因果秤与何小满连麦算卦打假，"
        f"开启第二卷全新副本，独立场景编号{ch * 97}。"
    )


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _sse_lines(model: str, content: str) -> List[str]:
    c1 = json.dumps(
        {
            "id": "chatcmpl-u5-vc",
            "model": model,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}
            ],
        },
        ensure_ascii=False,
    )
    c2 = json.dumps(
        {
            "id": "chatcmpl-u5-vc",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        },
        ensure_ascii=False,
    )
    return [f"data: {c1}\n\n", f"data: {c2}\n\n", "data: [DONE]\n\n"]


def _start_loopback_judge_server(
    responder: Callable[[Dict[str, Any], int], Dict[str, Any]],
) -> Tuple[_ThreadedHTTPServer, str, List[Dict[str, Any]]]:
    calls: List[Dict[str, Any]] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            req_body = json.loads(raw.decode("utf-8"))
            calls.append({"path": self.path, "model": req_body.get("model"), "body": req_body})
            verdict_dict = responder(req_body, len(calls))
            content = json.dumps(verdict_dict, ensure_ascii=False)
            model = str(req_body.get("model") or "wb/mimo-v2.6-pro")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            for line in _sse_lines(model, content):
                self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

    server = _ThreadedHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}", calls


class TestU5EngineVolumeHealthGate(unittest.TestCase):
    def test_vol1_end_triggers_health_check_fail_locks_and_halts_before_vol2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bp, _ = _make_short_blueprint(root / "bp_src")
            project, _ = _write_disk_project(root / "proj", bp, chapters_to_define=(1, 2, 3, 4))
            out_dir = root / "out"

            writer_provider = _ScriptedWriterProvider(_clean_writer_text)
            judge_provider = _QueuedJudgeProvider(
                [_judge_payload(fail_id="VH_05_THREE_ACT_AND_HOOK", fail_chapters=[3])]
            )
            vh_judge = PairwiseJudge(
                judge_provider,
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )

            engine = NovelGenerationEngine(
                project,
                writer_provider,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=vh_judge,
            )
            self.assertTrue(engine.enforce_volume_health_gate)
            self.assertIs(engine.volume_health_judge, vh_judge)

            with self.assertRaises(VolumeHealthLockError) as ctx:
                engine.run()

            exc = ctx.exception
            self.assertEqual(exc.volume_index, 1)
            self.assertEqual(exc.failed_checks, ["VH_05_THREE_ACT_AND_HOOK"])
            self.assertTrue(exc.report_file.endswith("volume_health_v0001.json"))
            self.assertTrue(exc.lock_file.endswith("volume_locks.json"))
            self.assertEqual(len(exc.repair_actions), 1)
            self.assertEqual(exc.repair_actions[0]["check_id"], "VH_05_THREE_ACT_AND_HOOK")

            # Vol 1 chapters 1..3 exist; Vol 2 chapter 4 MUST NOT be written or called.
            self.assertEqual([r.chapter for r in writer_provider.requests], [1, 2, 3])
            self.assertTrue((out_dir / "chapters" / "chapter_0003.txt").is_file())
            self.assertFalse((out_dir / "chapters" / "chapter_0004.txt").exists())

            report_path = out_dir / "audit" / "volume_health_v0001.json"
            lock_path = out_dir / "audit" / "volume_locks.json"
            self.assertTrue(report_path.is_file())
            self.assertTrue(lock_path.is_file())
            rep_data = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(rep_data["verdict"], "FAIL")
            self.assertEqual(rep_data["lock_state"], LOCK_STATE_LOCKED)
            locks = load_volume_locks(out_dir / "audit")
            self.assertEqual(locks["volumes"]["1"]["lock_state"], LOCK_STATE_LOCKED)
            self.assertEqual(locks["volumes"]["1"]["verdict"], "FAIL")

    def test_vol2_blocked_with_zero_writer_llm_calls_when_vol1_locked_or_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bp, _ = _make_short_blueprint(root / "bp_src")
            project, _ = _write_disk_project(root / "proj", bp, chapters_to_define=(1, 2, 3, 4))

            # Case A: completely empty audit dir (no vol 1 health check record at all) -> 0 LLM calls
            out_empty = root / "out_empty"
            writer_empty = _ScriptedWriterProvider(_clean_writer_text)
            judge_empty = PairwiseJudge(
                _QueuedJudgeProvider([_judge_payload()]),
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )
            engine_empty = NovelGenerationEngine(
                project,
                writer_empty,
                out_empty,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=judge_empty,
            )
            with self.assertRaises(VolumeHealthLockError) as ctx_empty:
                engine_empty.run(target_chapters=[4])
            self.assertEqual(ctx_empty.exception.volume_index, 1)
            self.assertEqual(len(writer_empty.requests), 0)
            self.assertFalse((out_empty / "chapters" / "chapter_0004.txt").exists())

            # Case B: Vol 1 ran and failed (LOCKED_NEEDS_REPAIR) -> entering vol 2 makes 0 LLM calls
            out_locked = root / "out_locked"
            writer_run1 = _ScriptedWriterProvider(_clean_writer_text)
            judge_fail = PairwiseJudge(
                _QueuedJudgeProvider([_judge_payload(fail_id="VH_01_NO_ROUTINE_REPEAT")]),
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )
            engine1 = NovelGenerationEngine(
                project,
                writer_run1,
                out_locked,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=judge_fail,
            )
            with self.assertRaises(VolumeHealthLockError):
                engine1.run(target_chapters=[1, 2, 3])
            self.assertEqual(len(writer_run1.requests), 3)

            writer_run2 = _ScriptedWriterProvider(_clean_writer_text)
            engine2 = NovelGenerationEngine(
                project,
                writer_run2,
                out_locked,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=judge_fail,
            )
            with self.assertRaises(VolumeHealthLockError) as ctx_locked:
                engine2.run(target_chapters=[4], resume=True)
            self.assertEqual(ctx_locked.exception.volume_index, 1)
            self.assertIn("VH_01_NO_ROUTINE_REPEAT", ctx_locked.exception.failed_checks)
            self.assertEqual(len(writer_run2.requests), 0)
            self.assertFalse((out_locked / "chapters" / "chapter_0004.txt").exists())

    def test_repair_then_execute_volume_check_and_cli_unlock_then_advance_to_vol2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bp, _ = _make_short_blueprint(root / "bp_src")
            proj_dir = root / "proj"
            project, bp_path = _write_disk_project(proj_dir, bp, chapters_to_define=(1, 2, 3, 4))
            out_dir = root / "out"

            # Step 1: Write vol 1 (ch 1..3); semantic FAIL on VH_05 locks vol 1 and halts before ch 4.
            judge_pass_provider = _QueuedJudgeProvider([_judge_payload()])
            vh_judge_pass = PairwiseJudge(
                judge_pass_provider,
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )
            writer_clean = _ScriptedWriterProvider(_clean_writer_text)
            judge_fail_provider = _QueuedJudgeProvider(
                [_judge_payload(fail_id="VH_05_THREE_ACT_AND_HOOK", fail_chapters=[3])]
            )
            vh_judge_fail = PairwiseJudge(
                judge_fail_provider,
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )
            engine1 = NovelGenerationEngine(
                project,
                writer_clean,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=vh_judge_fail,
            )
            with self.assertRaises(VolumeHealthLockError):
                engine1.run(target_chapters=[1, 2, 3, 4])
            self.assertEqual([r.chapter for r in writer_clean.requests], [1, 2, 3])
            self.assertFalse((out_dir / "chapters" / "chapter_0004.txt").exists())

            # Step 2: Inject a prose scale defect ("三亿") into ch 3 and verify execute_volume_check fails deterministically.
            ch3_path = out_dir / "chapters" / "chapter_0003.txt"
            ch3_path.write_text(
                "第3章地下夹层收网，宋海潮带队刑拘钱万顺，许青川直播间在线人数直接突破三亿，编号303。",
                encoding="utf-8",
            )
            rep_det_fail = execute_volume_check(
                project_dir=proj_dir,
                output_dir=out_dir,
                volume=1,
                judge=vh_judge_pass,
            )
            self.assertEqual(rep_det_fail.verdict, "FAIL")
            self.assertEqual(rep_det_fail.lock_state, LOCK_STATE_LOCKED)
            vh6 = [c for c in rep_det_fail.checks if c.id == "VH_06_LEDGER_AND_DEBT_MATH"][0]
            self.assertFalse(vh6.deterministic_passed)

            # Step 3: Run CLI `volume-check` against loopback HTTP server BEFORE fixing -> exit code 2, stays locked.
            server_verdict = {"fail_id": "VH_05_THREE_ACT_AND_HOOK"}
            server, base_url, http_calls = _start_loopback_judge_server(
                lambda _req, _n: _judge_payload(fail_id=server_verdict["fail_id"], fail_chapters=[3])
            )
            try:
                stdout_fail = io.StringIO()
                with contextlib.redirect_stdout(stdout_fail):
                    rc_fail = cli.main(
                        [
                            "volume-check",
                            "--project",
                            str(proj_dir),
                            "--output",
                            str(out_dir),
                            "--volume",
                            "1",
                            "--base-url",
                            base_url,
                            "--api-key",
                            "sk-u5-local-test",
                            "--model",
                            "wb/mimo-v2.6-pro",
                        ]
                    )
                self.assertEqual(rc_fail, 2)
                out_text = stdout_fail.getvalue()
                self.assertIn("VH_05_THREE_ACT_AND_HOOK", out_text)
                self.assertIn("VH_06_LEDGER_AND_DEBT_MATH", out_text)
                self.assertIn(LOCK_STATE_LOCKED, out_text)
                self.assertIn("volume_locks.json", out_text)
                # Confirm writer_model was auto-loaded from chapter_0003.meta.json
                self.assertEqual(len(http_calls), 1)
                self.assertEqual(http_calls[0]["model"], "wb/mimo-v2.6-pro")
                with self.assertRaises(VolumeHealthLockError):
                    assert_previous_volume_unlocked(out_dir / "audit", 2)

                # Step 4: Repair ch 3 text cleanly & flip judge verdict to PASS -> CLI returns 0 with --json & unlocks!
                ch3_path.write_text(
                    "第3章地下夹层收网，宋海潮带队刑拘钱万顺，许青川算卦通缉线索彻底闭环，"
                    "卷末抛出省城旧宅铜砝码新悬念，编号303。",
                    encoding="utf-8",
                )
                server_verdict["fail_id"] = None

                stdout_pass = io.StringIO()
                with contextlib.redirect_stdout(stdout_pass):
                    rc_pass = cli.main(
                        [
                            "volume-check",
                            "--project",
                            str(proj_dir),
                            "--output-dir",
                            str(out_dir),
                            "--volume",
                            "1",
                            "--blueprint",
                            str(bp_path),
                            "--base-url",
                            base_url,
                            "--api-key",
                            "sk-u5-local-test",
                            "--model",
                            "wb/mimo-v2.6-pro",
                            "--json",
                        ]
                    )
                self.assertEqual(rc_pass, 0)
                parsed_json = json.loads(stdout_pass.getvalue())
                self.assertEqual(parsed_json["verdict"], "PASS")
                self.assertEqual(parsed_json["lock_state"], LOCK_STATE_PASSED)
                self.assertEqual(parsed_json["writer_model"], "wb/deepseek-v4.1-flash")
                self.assertEqual(parsed_json["judge_model"], "wb/mimo-v2.6-pro")
                assert_previous_volume_unlocked(out_dir / "audit", 2)
            finally:
                server.shutdown()
                server.server_close()

            # Step 5: Now advance into Volume 2 chapter 4 with resume=True -> succeeds!
            writer2 = _ScriptedWriterProvider(_clean_writer_text)
            engine2 = NovelGenerationEngine(
                project,
                writer2,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=vh_judge_pass,
            )
            manifest = engine2.run(target_chapters=[4], resume=True)
            self.assertEqual(manifest["chapter_count"], 1)
            self.assertEqual(manifest["chapters"][0]["chapter"], 4)
            self.assertTrue(manifest["all_gates_passed"])
            self.assertEqual(manifest["volume_health_reports"], [])
            self.assertEqual([r.chapter for r in writer2.requests], [4])
            self.assertTrue((out_dir / "chapters" / "chapter_0004.txt").is_file())
            self.assertTrue((out_dir / "snapshots" / "chapter_0004_after.json").is_file())

    def test_resume_skips_vol1_end_reuses_passed_report_without_reinvoking_judge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bp, _ = _make_short_blueprint(root / "bp_src")
            project, _ = _write_disk_project(root / "proj", bp, chapters_to_define=(1, 2, 3, 4))
            out_dir = root / "out"

            # First run: write vol 1 (ch 1..3) and PASS health check (1 judge call).
            writer1 = _ScriptedWriterProvider(_clean_writer_text)
            judge_prov1 = _QueuedJudgeProvider([_judge_payload()])
            vh_judge1 = PairwiseJudge(
                judge_prov1,
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )
            engine1 = NovelGenerationEngine(
                project,
                writer1,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=vh_judge1,
            )
            m1 = engine1.run(target_chapters=[1, 2, 3])
            self.assertTrue(m1["all_gates_passed"])
            self.assertEqual(len(m1["volume_health_reports"]), 1)
            self.assertEqual(m1["volume_health_reports"][0]["verdict"], "PASS")
            self.assertEqual(len(judge_prov1.requests), 1)

            # Second run: resume=True across [1, 2, 3, 4] -> skips ch 1..3, reuses PASS report (0 judge calls), writes ch 4!
            writer2 = _ScriptedWriterProvider(_clean_writer_text)
            judge_prov2 = _QueuedJudgeProvider([_judge_payload()])
            vh_judge2 = PairwiseJudge(
                judge_prov2,
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )
            engine2 = NovelGenerationEngine(
                project,
                writer2,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=vh_judge2,
            )
            m2 = engine2.run(target_chapters=[1, 2, 3, 4], resume=True)
            self.assertTrue(m2["all_gates_passed"])
            self.assertEqual(len(m2["volume_health_reports"]), 1)
            self.assertEqual(m2["volume_health_reports"][0]["verdict"], "PASS")
            self.assertEqual(len(judge_prov2.requests), 0)
            self.assertEqual([r.chapter for r in writer2.requests], [4])

    def test_resume_rechecks_when_vol1_is_still_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bp, _ = _make_short_blueprint(root / "bp_src")
            project, _ = _write_disk_project(root / "proj", bp, chapters_to_define=(1, 2, 3, 4))
            out_dir = root / "out"

            # Vol 1 fails health check and locks.
            writer1 = _ScriptedWriterProvider(_clean_writer_text)
            judge_fail = PairwiseJudge(
                _QueuedJudgeProvider([_judge_payload(fail_id="VH_05_THREE_ACT_AND_HOOK")]),
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )
            engine1 = NovelGenerationEngine(
                project,
                writer1,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=judge_fail,
            )
            with self.assertRaises(VolumeHealthLockError):
                engine1.run(target_chapters=[1, 2, 3])

            # Resume across [1, 2, 3, 4] with a passing judge MUST re-run vol 1 health check at ch 3, unlock, and write ch 4!
            writer2 = _ScriptedWriterProvider(_clean_writer_text)
            judge_prov2 = _QueuedJudgeProvider([_judge_payload()])
            judge_pass = PairwiseJudge(
                judge_prov2,
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )
            engine2 = NovelGenerationEngine(
                project,
                writer2,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=judge_pass,
            )
            m2 = engine2.run(target_chapters=[1, 2, 3, 4], resume=True)
            self.assertEqual(len(judge_prov2.requests), 1)
            self.assertEqual([r.chapter for r in writer2.requests], [4])
            self.assertEqual(len(m2["volume_health_reports"]), 1)
            self.assertEqual(m2["volume_health_reports"][0]["verdict"], "PASS")

    def test_cli_volume_check_workbuddy_default_strong_planner_and_strict_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bp, _ = _make_short_blueprint(root / "bp_src")
            proj_dir = root / "proj"
            project, _ = _write_disk_project(proj_dir, bp, chapters_to_define=(1, 2, 3))
            out_dir = root / "out"

            # Generate vol 1 chapters & snapshots first without gate.
            writer = _ScriptedWriterProvider(_clean_writer_text, model="wb/deepseek-v4.1-flash")
            NovelGenerationEngine(
                project,
                writer,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
            ).run(target_chapters=[1, 2, 3])

            # 1. WorkBuddy catalog configured without --model -> defaults to catalog.strong_planner_model() (mimo_v2_6_pro)!
            server, base_url, http_calls = _start_loopback_judge_server(
                lambda _req, _n: _judge_payload()
            )
            try:
                stdout_buf = io.StringIO()
                with contextlib.redirect_stdout(stdout_buf):
                    rc = cli.main(
                        [
                            "volume-check",
                            "--project",
                            str(proj_dir),
                            "--output-dir",
                            str(out_dir),
                            "--volume",
                            "1",
                            "--base-url",
                            base_url,
                            "--api-key",
                            "sk-u5-wb-test",
                            "--workbuddy-hy4-flash",
                            "wb/test-hy4-flash",
                            "--workbuddy-hy4",
                            "wb/test-hy4-manual",
                            "--workbuddy-deepseek-v4-1-flash",
                            "wb/deepseek-v4.1-flash",
                            "--workbuddy-mimo-v2-6-pro",
                            "wb/mimo-v2.6-pro",
                            "--json",
                        ]
                    )
                self.assertEqual(rc, 0)
                self.assertEqual(len(http_calls), 1)
                self.assertEqual(http_calls[0]["model"], "wb/mimo-v2.6-pro")
                rep = json.loads(stdout_buf.getvalue())
                self.assertEqual(rep["judge_model"], "wb/mimo-v2.6-pro")
                self.assertEqual(rep["writer_model"], "wb/deepseek-v4.1-flash")

                # 2. Same-family judge (--judge-slot deepseek_v4_1_flash against deepseek writer) -> SameModelJudgeError!
                with self.assertRaises(SameModelJudgeError):
                    cli.main(
                        [
                            "volume-check",
                            "--project",
                            str(proj_dir),
                            "--output-dir",
                            str(out_dir),
                            "--volume",
                            "1",
                            "--base-url",
                            base_url,
                            "--api-key",
                            "sk-u5-wb-test",
                            "--judge-slot",
                            "deepseek_v4_1_flash",
                            "--workbuddy-hy4-flash",
                            "wb/test-hy4-flash",
                            "--workbuddy-hy4",
                            "wb/test-hy4-manual",
                            "--workbuddy-deepseek-v4-1-flash",
                            "wb/deepseek-v4.1-flash",
                            "--workbuddy-mimo-v2-6-pro",
                            "wb/mimo-v2.6-pro",
                        ]
                    )
            finally:
                server.shutdown()
                server.server_close()

            # 3. Missing writer_model in chapter meta AND omitted --writer-model -> LLMConfigurationError (never guesses!)
            meta3_path = out_dir / "chapters" / "chapter_0003.meta.json"
            orig_meta3 = meta3_path.read_text(encoding="utf-8")
            meta3_stripped = json.loads(orig_meta3)
            meta3_stripped.pop("writer_model", None)
            meta3_stripped.pop("model", None)
            meta3_path.write_text(json.dumps(meta3_stripped, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(LLMConfigurationError):
                execute_volume_check(
                    project_dir=proj_dir,
                    output_dir=out_dir,
                    volume=1,
                    judge_provider=_QueuedJudgeProvider([_judge_payload()]),
                    judge_model="wb/mimo-v2.6-pro",
                )
            meta3_path.write_text(orig_meta3, encoding="utf-8")

            # 4. Missing end-of-volume snapshot -> FileNotFoundError (no silent fallback)
            snap3_path = out_dir / "snapshots" / "chapter_0003_after.json"
            snap3_backup = snap3_path.read_text(encoding="utf-8")
            snap3_path.unlink()
            with self.assertRaises(FileNotFoundError):
                execute_volume_check(
                    project_dir=proj_dir,
                    output_dir=out_dir,
                    volume=1,
                    judge=PairwiseJudge(
                        _QueuedJudgeProvider([_judge_payload()]),
                        judge_model="wb/mimo-v2.6-pro",
                        writer_model="wb/deepseek-v4.1-flash",
                    ),
                )
            snap3_path.write_text(snap3_backup, encoding="utf-8")

            # 5. Missing a chapter file in volume range -> FileNotFoundError
            ch2_path = out_dir / "chapters" / "chapter_0002.txt"
            ch2_backup = ch2_path.read_text(encoding="utf-8")
            ch2_path.unlink()
            with self.assertRaises(FileNotFoundError):
                execute_volume_check(
                    project_dir=proj_dir,
                    output_dir=out_dir,
                    volume=1,
                    judge=PairwiseJudge(
                        _QueuedJudgeProvider([_judge_payload()]),
                        judge_model="wb/mimo-v2.6-pro",
                        writer_model="wb/deepseek-v4.1-flash",
                    ),
                )
            ch2_path.write_text(ch2_backup, encoding="utf-8")

    def test_backward_compatibility_and_strict_init_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bp, _ = _make_short_blueprint(root / "bp_src")
            project, _ = _write_disk_project(root / "proj", bp, chapters_to_define=(1, 2, 3, 4))
            writer = _ScriptedWriterProvider(_clean_writer_text)
            arc_judge_prov = _QueuedJudgeProvider(
                [{"verdict": "PASS", "problems": [], "summary": "ok"}]
            )
            arc_judge = PairwiseJudge(
                arc_judge_prov,
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )

            # 1. Passing blueprint + arc_judge ONLY (no volume_health_judge, no enforce_volume_health_gate)
            # MUST NOT auto-enable volume health gate -> 100% backward compatible with test_t7b2_engine_brakes!
            engine_compat = NovelGenerationEngine(
                project,
                writer,
                root / "out_compat",
                checker=_PassChecker(),
                blueprint=bp,
                arc_judge=arc_judge,
                arc_review_interval=10,
            )
            self.assertFalse(engine_compat.enforce_volume_health_gate)
            self.assertIsNone(engine_compat.volume_health_judge)
            manifest_compat = engine_compat.run()
            self.assertEqual(manifest_compat["chapter_count"], 4)
            self.assertEqual(manifest_compat["volume_health_reports"], [])
            self.assertEqual(len(arc_judge_prov.requests), 0)

            # 2. enforce_volume_health_gate=True with blueprint + arc_judge (no volume_health_judge) reuses arc_judge!
            engine_reuse_arc = NovelGenerationEngine(
                project,
                writer,
                root / "out_reuse_arc",
                checker=_PassChecker(),
                blueprint=bp,
                arc_judge=arc_judge,
                enforce_volume_health_gate=True,
            )
            self.assertTrue(engine_reuse_arc.enforce_volume_health_gate)
            self.assertIs(engine_reuse_arc.volume_health_judge, arc_judge)

            # 3. enforce_volume_health_gate=False explicitly disables even when volume_health_judge is provided.
            engine_off = NovelGenerationEngine(
                project,
                writer,
                root / "out_off",
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=arc_judge,
                enforce_volume_health_gate=False,
            )
            self.assertFalse(engine_off.enforce_volume_health_gate)

            # 4. Strict __init__ validation errors (zero silent skip!)
            with self.assertRaises(ValueError):
                NovelGenerationEngine(
                    project,
                    writer,
                    root / "out_bad1",
                    enforce_volume_health_gate=True,
                    volume_health_judge=arc_judge,
                )  # missing blueprint
            with self.assertRaises(ValueError):
                NovelGenerationEngine(
                    project,
                    writer,
                    root / "out_bad2",
                    blueprint=bp,
                    enforce_volume_health_gate=True,
                )  # missing both judges
            with self.assertRaises(ValueError):
                NovelGenerationEngine(
                    project,
                    writer,
                    root / "out_bad3",
                    blueprint=bp,
                    volume_health_judge="not-a-judge",  # type: ignore[arg-type]
                )
            with self.assertRaises(ValueError):
                NovelGenerationEngine(
                    project,
                    writer,
                    root / "out_bad4",
                    blueprint=bp,
                    volume_health_judge=arc_judge,
                    enforce_volume_health_gate=1,  # type: ignore[arg-type]
                )

    def test_missing_chapter_in_volume_aborts_checkpoint_with_production_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bp, _ = _make_short_blueprint(root / "bp_src")
            project, _ = _write_disk_project(root / "proj", bp, chapters_to_define=(1, 2, 3))
            out_dir = root / "out"

            # Generate ch 1 and ch 2 first without gate, then delete ch 1 and try to finish ch 3 with gate enabled.
            writer1 = _ScriptedWriterProvider(_clean_writer_text)
            NovelGenerationEngine(
                project,
                writer1,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
            ).run(target_chapters=[1, 2])

            # Delete ch 1 AFTER writing ch 2 snapshot (ch 3 will load recent texts [ch1, ch2] if ch1 missing,
            # so test _run_volume_health_checkpoint directly to verify zero partial-volume silent fallback).
            (out_dir / "chapters" / "chapter_0001.txt").unlink()
            judge = PairwiseJudge(
                _QueuedJudgeProvider([_judge_payload()]),
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )
            engine = NovelGenerationEngine(
                project,
                writer1,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=judge,
            )
            with self.assertRaises(ProductionGateFailure) as ctx:
                engine._run_volume_health_checkpoint(
                    vol_spec=bp.volumes[0],
                    chapters_dir=out_dir / "chapters",
                    audit_dir=out_dir / "audit",
                )
            self.assertIn("missing chapter", str(ctx.exception).lower())

    def test_vol2_end_passes_prior_volume_context_and_writes_v0002_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bp, _ = _make_short_blueprint(root / "bp_src")
            project, _ = _write_disk_project(
                root / "proj", bp, chapters_to_define=(1, 2, 3, 4, 5, 6)
            )
            # Plant vol 2 foreshadow fs_river_case in ch 4 so vol 2 snapshot satisfies VH_02.
            project.foreshadows["fs_river_case"] = ForeshadowItem(
                id="fs_river_case",
                title="渡轮船票编号",
                plant_chapter=4,
                due_chapter=18,
                plant_instruction="埋设船票",
                payoff_instruction="终卷揭晓",
                plant_keywords=["渡轮船票"],
                payoff_keywords=["原始底档"],
                cross_volume=True,
            )
            out_dir = root / "out"

            def _gen_two_vols(req: LLMRequest) -> str:
                if req.chapter == 4:
                    return (
                        "第4章老街茶室直播间连麦，许青川端起因果秤算卦观气，"
                        "老太递出半张褪色渡轮船票，案号404。"
                    )
                return _clean_writer_text(req)

            writer = _ScriptedWriterProvider(_gen_two_vols)
            judge_prov = _QueuedJudgeProvider([_judge_payload(), _judge_payload()])
            vh_judge = PairwiseJudge(
                judge_prov,
                judge_model="wb/mimo-v2.6-pro",
                writer_model="wb/deepseek-v4.1-flash",
            )
            engine = NovelGenerationEngine(
                project,
                writer,
                out_dir,
                checker=_PassChecker(),
                blueprint=bp,
                volume_health_judge=vh_judge,
            )
            manifest = engine.run()
            self.assertEqual(manifest["chapter_count"], 6)
            self.assertTrue(manifest["all_gates_passed"])
            self.assertEqual(len(manifest["volume_health_reports"]), 2)
            self.assertEqual([r["volume_index"] for r in manifest["volume_health_reports"]], [1, 2])
            self.assertTrue((out_dir / "audit" / "volume_health_v0001.json").is_file())
            self.assertTrue((out_dir / "audit" / "volume_health_v0002.json").is_file())
            assert_previous_volume_unlocked(out_dir / "audit", 2)
            assert_previous_volume_unlocked(out_dir / "audit", 3)
            # Second judge call must include prior volume summary / window info (chapters 1..3)
            self.assertEqual(len(judge_prov.requests), 2)
            self.assertIn("南桥夜摊初开瞳", judge_prov.requests[1].user_prompt)


if __name__ == "__main__":
    unittest.main()
