"""T4 offline tests: Blueprint-driven chapter expansion, guards, retries, volume transitions, and scale_rules."""

from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.blueprint import BlueprintValidationError, load_blueprint  # noqa: E402
from xiaoshuo_bot.blueprint_planner import (  # noqa: E402
    BlueprintExpansionResult,
    collect_blueprint_scene_errors,
    expand_blueprint_chapters,
)
from xiaoshuo_bot.blueprint_project import materialize_project  # noqa: E402
from xiaoshuo_bot.llm.base import LLMProvider, LLMRequest, LLMResponse  # noqa: E402
from xiaoshuo_bot.llm.router import RoleModelRouter  # noqa: E402
from xiaoshuo_bot.loader import load_project  # noqa: E402
from xiaoshuo_bot.models import ProjectValidationError, VolumeCapLock, WorldStateSnapshot  # noqa: E402
from xiaoshuo_bot.planner import (  # noqa: E402
    AutoNovelPlanner,
    PlanningValidationError,
    _collect_extend_volume_meta_errors,
)
from xiaoshuo_bot.state import WorldStateManager  # noqa: E402


BLUEPRINT_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_valid.yaml"
EXTRAS_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_project_extras.yaml"
OUTLINE_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_project_outline.yaml"


def _make_bp_scene(
    chapter: int,
    *,
    volume: int = 1,
    pov: str = "mc",
    active_characters: Optional[List[str]] = None,
    active_assets: Optional[List[str]] = None,
    style_profile_ids: Optional[List[str]] = None,
    location_id: Optional[str] = None,
    min_chars: int = 2100,
    max_chars: int = 2500,
    emotion_polarity: Optional[str] = None,
    cliffhanger_hook: Optional[str] = None,
    scene_goal: Optional[str] = None,
    scene_obstacle: Optional[str] = None,
    scene_outcome: Optional[str] = None,
    events: Optional[List[str]] = None,
    ledger_mutations: Optional[List[Dict[str, Any]]] = None,
    character_mutations: Optional[List[Dict[str, Any]]] = None,
    post_scene_asset_mutations: Optional[List[Dict[str, Any]]] = None,
    expectation_directives: Optional[List[Dict[str, Any]]] = None,
    beat_type: str = "危机升级与反手破局",
) -> Dict[str, Any]:
    loc = location_id or f"loc_nanqiao_zone_{(chapter - 1) % 3 + 1}"
    pol = emotion_polarity or (
        "burst" if chapter % 3 == 0 else ("suppress" if chapter % 3 == 1 else "mixed")
    )
    chars = active_characters if active_characters is not None else [pov, "detective_ally"]
    assets = active_assets if active_assets is not None else ["stall_wooden_sign", "abacus_bracelet"]
    styles = style_profile_ids if style_profile_ids is not None else ["book_style"]
    return {
        "chapter": chapter,
        "scene_id": f"ch{chapter:04d}_bp_scene",
        "internal_title": f"南桥线索推进{chapter}",
        "public_title": f"扫码看卦反杀局{chapter}",
        "beat_type": beat_type,
        "emotion_polarity": pol,
        "pov_character": pov,
        "active_characters": list(chars),
        "active_assets": list(assets),
        "style_profile_ids": list(styles),
        "location_time": f"第{chapter}日夜，江城南桥老街与古玩巷口",
        "location_id": loc,
        "min_chars": min_chars,
        "max_chars": max_chars,
        "scene_goal": scene_goal if scene_goal is not None else f"第{chapter}章许青川核验假玉器背后账册并稳住摊位",
        "scene_obstacle": scene_obstacle if scene_obstacle is not None else f"第{chapter}章地头蛇暗中派托儿在摊前搅局断流",
        "scene_outcome": scene_outcome if scene_outcome is not None else f"第{chapter}章当众拆穿托儿破绽并拿到新转账凭证，但左眼灼痛加重",
        "events": events
        if events is not None
        else [
            f"第{chapter}章夜市开摊不久，托儿带着做旧铜件上门故意砸场要挟赔钱。",
            f"第{chapter}章许青川转动黄杨木算盘珠摘镜开瞳三秒，当众点破对方袖口收据编号与伪造泥痕。",
            f"第{chapter}章宋海潮带队核验现场物证并将人带走，许青川忍着眼眶灼痛记下收据背面的冷库印记。",
        ],
        "stop_condition": f"第{chapter}章许青川收好旧松木牌，看到陌生号码发来威胁彩信时停笔。",
        "cliffhanger_hook": cliffhanger_hook
        if cliffhanger_hook is not None
        else f"手机突然弹出第{chapter}号陌生监控截屏，画面里旧货行卷闸门正缓缓升起。",
        "segment_comment_cue": "这波三秒拆穿托儿直接反杀太爽了",
        "volume": volume,
        "calendar_days_elapsed": 1,
        "post_scene_asset_mutations": list(post_scene_asset_mutations or []),
        "ledger_mutations": list(ledger_mutations or []),
        "character_mutations": list(character_mutations or []),
        "expectation_directives": list(expectation_directives or []),
    }


class ScriptedBlueprintProvider(LLMProvider):
    """Offline fake LLMProvider for deterministic blueprint expansion tests."""

    def __init__(
        self,
        handler: Callable[[LLMRequest], Any],
    ) -> None:
        self.handler = handler
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        result = self.handler(request)
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return LLMResponse(
            request_id=request.request_id,
            content=content,
            model="fake-blueprint-planner",
            provider="fake",
            latency_sec=0.01,
        )


def _materialize_fixture_project(base_tmp: Path, name: str = "nanqiao_proj") -> Path:
    out_dir = base_tmp / name
    return materialize_project(
        blueprint_path=BLUEPRINT_FIXTURE,
        extras_path=EXTRAS_FIXTURE,
        outline_path=OUTLINE_FIXTURE,
        out_dir=out_dir,
        existing_cast_names=[],
    )


def _start_mock_sse_server(
    handler_fn: Callable[[Dict[str, Any]], str],
) -> tuple[ThreadingHTTPServer, str]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            body = json.loads(raw)
            content_str = handler_fn(body)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()

            chunk_1 = {
                "id": "chatcmpl-bp-test",
                "object": "chat.completion.chunk",
                "model": body.get("model", "planner-bp-model"),
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": content_str},
                        "finish_reason": None,
                    }
                ],
            }
            chunk_2 = {
                "id": "chatcmpl-bp-test",
                "object": "chat.completion.chunk",
                "model": body.get("model", "planner-bp-model"),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 200},
            }
            sse = (
                f"data: {json.dumps(chunk_1, ensure_ascii=False)}\n\n"
                f"data: {json.dumps(chunk_2, ensure_ascii=False)}\n\n"
                "data: [DONE]\n\n"
            ).encode("utf-8")
            self.wfile.write(sse)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/v1"


class TestBlueprintExpansionFlow(unittest.TestCase):
    def test_expand_ch4_to_ch10_appends_atomically_preserves_ch1_to_ch3_and_loads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_t4_single_") as tmp:
            proj_dir = _materialize_fixture_project(Path(tmp))
            outline_file = proj_dir / "outline.yaml"

            # Mark Chapter 1 as locked and snapshot exact original Ch 1..3 dicts
            raw_before = yaml.safe_load(outline_file.read_text(encoding="utf-8"))
            raw_before["scenes"][0]["locked"] = True
            outline_file.write_text(
                yaml.safe_dump(raw_before, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
                newline="\n",
            )
            original_ch1_to_3 = copy.deepcopy(raw_before["scenes"])

            provider = ScriptedBlueprintProvider(
                lambda req: {
                    "scenes": [
                        _make_bp_scene(
                            ch,
                            volume=1,
                            ledger_mutations=[{"ledger_key": "cash", "delta": 200, "reason": "卦金收入"}],
                        )
                        for ch in range(
                            int(req.metadata["start_chapter"]),
                            int(req.metadata["end_chapter"]) + 1,
                        )
                    ]
                }
            )

            result = expand_blueprint_chapters(
                project_dir=proj_dir,
                start_chapter=4,
                end_chapter=10,
                provider=provider,
            )

            self.assertIsInstance(result, BlueprintExpansionResult)
            self.assertEqual(result.volume_index, 1)
            self.assertEqual(result.start_chapter, 4)
            self.assertEqual(result.end_chapter, 10)
            self.assertEqual(result.added_chapters, list(range(4, 11)))
            self.assertEqual(result.outline_path, outline_file.resolve())
            self.assertEqual(result.attempts_used, 1)
            res_dict = result.to_dict()
            self.assertEqual(res_dict["added_chapters"], list(range(4, 11)))
            self.assertEqual(res_dict["volume_index"], 1)

            # Verify prompt injected required blueprint context
            self.assertEqual(len(provider.requests), 1)
            prompt = provider.requests[0].user_prompt
            self.assertIn("南桥夜摊初开瞳", prompt)
            self.assertIn("单人近距因果秤显影", prompt)
            self.assertIn("bounded_realistic", prompt)
            self.assertIn("hard_max=5000000", prompt)
            self.assertIn("current_role=", prompt)
            self.assertIn("stall_wooden_sign", prompt)
            self.assertIn("fs_fake_jade_seal", prompt)
            self.assertIn("先扫码后听话", prompt)
            self.assertIn("scene_goal", provider.requests[0].system_prompt)
            self.assertIn("scene_obstacle", provider.requests[0].system_prompt)
            self.assertIn("scene_outcome", provider.requests[0].system_prompt)

            # Verify Ch 1..3 preserved completely untouched in outline.yaml
            raw_after = yaml.safe_load(outline_file.read_text(encoding="utf-8"))
            self.assertEqual(raw_after["scenes"][:3], original_ch1_to_3)
            self.assertTrue(raw_after["scenes"][0]["locked"])
            # Planning-only three-question fields must not pollute standard SceneOutline YAML
            for sc in raw_after["scenes"][3:]:
                self.assertNotIn("scene_goal", sc)
                self.assertNotIn("scene_obstacle", sc)
                self.assertNotIn("scene_outcome", sc)

            # Verify load_project() passes cleanly and fields are intact
            loaded = load_project(proj_dir)
            self.assertEqual([s.chapter for s in loaded.scenes], list(range(1, 11)))
            for sc in loaded.scenes[3:]:
                self.assertGreaterEqual(len(sc.events), 3)
                self.assertTrue(sc.cliffhanger_hook)
                self.assertEqual(sc.volume, 1)
                self.assertEqual(sc.min_chars, 2100)
                self.assertEqual(sc.max_chars, 2500)

            # Verify no .tmp residue files left in project dir
            self.assertEqual(list(proj_dir.glob("*.tmp")), [])

    def test_consecutive_batches_4_10_then_11_20_then_21_30(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_t4_batches_") as tmp:
            proj_dir = _materialize_fixture_project(Path(tmp))

            provider = ScriptedBlueprintProvider(
                lambda req: {
                    "scenes": [
                        _make_bp_scene(
                            ch,
                            volume=1,
                            ledger_mutations=[{"ledger_key": "cash", "delta": 1000, "reason": "摆摊收卦金"}],
                        )
                        for ch in range(
                            int(req.metadata["start_chapter"]),
                            int(req.metadata["end_chapter"]) + 1,
                        )
                    ]
                }
            )

            r1 = expand_blueprint_chapters(
                project_dir=proj_dir, start_chapter=4, end_chapter=10, provider=provider
            )
            r2 = expand_blueprint_chapters(
                project_dir=proj_dir, start_chapter=11, end_chapter=20, provider=provider
            )
            r3 = expand_blueprint_chapters(
                project_dir=proj_dir, start_chapter=21, end_chapter=30, provider=provider
            )

            self.assertEqual(r1.added_chapters, list(range(4, 11)))
            self.assertEqual(r2.added_chapters, list(range(11, 21)))
            self.assertEqual(r3.added_chapters, list(range(21, 31)))

            # Verify batch 2 prompt carries batch 1 summaries & updated cash balance (500 + 7*1000 = 7500)
            self.assertIn("扫码看卦反杀局10", provider.requests[1].user_prompt)
            self.assertIn("cash=7500", provider.requests[1].user_prompt)
            # Verify batch 3 prompt carries batch 2 summaries & updated cash balance (7500 + 10*1000 = 17500)
            self.assertIn("扫码看卦反杀局20", provider.requests[2].user_prompt)
            self.assertIn("cash=17500", provider.requests[2].user_prompt)

            loaded = load_project(proj_dir)
            self.assertEqual([s.chapter for s in loaded.scenes], list(range(1, 31)))

    def test_role_model_router_and_planner_exports(self) -> None:
        from xiaoshuo_bot import planner as planner_mod

        self.assertIs(planner_mod.expand_blueprint_chapters, expand_blueprint_chapters)
        self.assertIs(planner_mod.BlueprintExpansionResult, BlueprintExpansionResult)
        self.assertIs(planner_mod.collect_blueprint_scene_errors, collect_blueprint_scene_errors)

        def _sse_handler(body: Dict[str, Any]) -> str:
            return json.dumps(
                {"scenes": [_make_bp_scene(ch, volume=1) for ch in range(4, 6)]},
                ensure_ascii=False,
            )

        server, base_url = _start_mock_sse_server(_sse_handler)
        try:
            router = RoleModelRouter.from_dict(
                {
                    "roles": {
                        "planner": {
                            "base_url": base_url,
                            "model": "planner-bp-v1",
                            "api_keys": ["test-key-bp-001"],
                            "provider_type": "openai",
                            "stream": True,
                        }
                    }
                }
            )
            with tempfile.TemporaryDirectory(prefix="xsb_t4_router_") as tmp:
                proj_dir = _materialize_fixture_project(Path(tmp))
                res = AutoNovelPlanner(router).expand_blueprint_chapters(
                    proj_dir, start_chapter=4, end_chapter=5
                )
                self.assertEqual(res.added_chapters, [4, 5])
                self.assertEqual(len(load_project(proj_dir).scenes), 5)
        finally:
            server.shutdown()
            server.server_close()


class TestBlueprintValidationAndRetries(unittest.TestCase):
    def test_cross_volume_and_overlap_and_missing_blueprint_rejected_before_llm(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_t4_precheck_") as tmp:
            proj_dir = _materialize_fixture_project(Path(tmp))
            provider = ScriptedBlueprintProvider(lambda req: {"scenes": []})

            # Cross-volume span (Vol 1 ends at 40, Vol 2 starts at 41) -> ValueError
            with self.assertRaises(ValueError) as ctx_cross:
                expand_blueprint_chapters(
                    project_dir=proj_dir,
                    start_chapter=39,
                    end_chapter=42,
                    provider=provider,
                )
            self.assertIn("crosses volume boundary", str(ctx_cross.exception))

            # Overlapping existing chapters Ch 1..3 -> ValueError
            with self.assertRaises(ValueError) as ctx_overlap:
                expand_blueprint_chapters(
                    project_dir=proj_dir,
                    start_chapter=3,
                    end_chapter=5,
                    provider=provider,
                )
            self.assertIn("existing chapters", str(ctx_overlap.exception))

            # Missing explicit blueprint path -> BlueprintValidationError
            with self.assertRaises(BlueprintValidationError):
                expand_blueprint_chapters(
                    project_dir=proj_dir,
                    blueprint_path=proj_dir / "nonexistent_blueprint.yaml",
                    start_chapter=4,
                    end_chapter=6,
                    provider=provider,
                )

            # Zero LLM calls incurred by pre-checks
            self.assertEqual(len(provider.requests), 0)

    def test_collect_errors_catches_three_questions_deflating_hooks_caps_overdraft_stale_loc_exited_and_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_t4_collector_") as tmp:
            proj_dir = _materialize_fixture_project(Path(tmp))
            # Give existing Ch 2 and Ch 3 the same location_id to test cross-boundary stale location
            outline_file = proj_dir / "outline.yaml"
            raw_ol = yaml.safe_load(outline_file.read_text(encoding="utf-8"))
            raw_ol["scenes"][1]["location_id"] = "loc_same_alley"
            raw_ol["scenes"][2]["location_id"] = "loc_same_alley"
            outline_file.write_text(
                yaml.safe_dump(raw_ol, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
                newline="\n",
            )

            project = load_project(proj_dir)
            blueprint = load_blueprint(proj_dir / "blueprint.yaml")
            vol1 = blueprint.volumes[0]

            # 1. Missing three questions + deflating hooks (一夜无话, 圆满解决, 回家休息) + <3 events + bad char range + stale loc
            scenes = [_make_bp_scene(ch, volume=1) for ch in range(4, 10)]
            scenes[0]["scene_goal"] = ""
            scenes[0]["scene_obstacle"] = "   "
            del scenes[0]["scene_outcome"]
            scenes[0]["location_id"] = "loc_same_alley"  # 3rd consecutive with Ch 2, Ch 3!
            scenes[1]["cliffhanger_hook"] = "忙完摊位琐事后一夜无话，第二天照常摆摊。"
            scenes[2]["cliffhanger_hook"] = "两方握手言和，事情圆满解决。"
            scenes[3]["cliffhanger_hook"] = "收摊后主角收拾家伙回家休息。"
            scenes[4]["events"] = ["只有一条事件", "只有两条事件"]
            scenes[4]["min_chars"] = 1800  # below blueprint [2100, 2500]
            scenes[4]["max_chars"] = 3000  # above blueprint [2100, 2500]
            scenes[5]["active_characters"] = ["mc", "ghost_npc"]
            scenes[5]["active_assets"] = ["ghost_asset"]
            scenes[5]["style_profile_ids"] = ["ghost_style"]

            errs = collect_blueprint_scene_errors(
                scenes,
                blueprint=blueprint,
                project=project,
                volume=vol1,
                start_chapter=4,
                end_chapter=9,
            )
            joined = "\n".join(errs)
            self.assertIn("scene_goal", joined)
            self.assertIn("scene_obstacle", joined)
            self.assertIn("scene_outcome", joined)
            self.assertIn("STATE_STALE_LOCATION", joined)
            self.assertIn("一夜无话", joined)
            self.assertIn("圆满解决", joined)
            self.assertIn("回家休息", joined)
            self.assertIn("at least 3", joined)
            self.assertIn("blueprint.chapter_chars_range", joined)
            self.assertIn("ghost_npc", joined)
            self.assertIn("ghost_asset", joined)
            self.assertIn("ghost_style", joined)

            # 2. bounded_realistic hard_max breach (even with breakthrough=True) + overdraft + unbounded_fantasy allowed
            cap_scenes = [_make_bp_scene(ch, volume=1) for ch in range(4, 7)]
            cap_scenes[0]["ledger_mutations"] = [
                {"ledger_key": "cash", "delta": 5_000_000, "reason": "天价横财", "breakthrough": True},
                {"ledger_key": "merit_aura", "delta": 999_999_999, "reason": "玄幻功德无上限"},
            ]
            cap_scenes[1]["ledger_mutations"] = [
                {"ledger_key": "followers", "delta": -10, "reason": "粉丝扣成负数触发透支"},
            ]
            cap_errs = collect_blueprint_scene_errors(
                cap_scenes,
                blueprint=blueprint,
                project=project,
                volume=vol1,
                start_chapter=4,
                end_chapter=6,
            )
            cap_joined = "\n".join(cap_errs)
            self.assertIn("bounded_realistic", cap_joined)
            self.assertIn("cash", cap_joined)
            self.assertIn("hard_max", cap_joined)
            self.assertIn("STATE_OVERDRAFT", cap_joined)
            self.assertIn("followers", cap_joined)
            self.assertNotIn("merit_aura", cap_joined)

            # 3. Deceased character real-time action blocked, flashback exempted
            death_scenes = [_make_bp_scene(ch, volume=1) for ch in range(4, 7)]
            death_scenes[0]["character_mutations"] = [
                {"character_id": "vol1_boss", "status": "deceased", "note": "伏法身亡"}
            ]
            death_scenes[1]["active_characters"] = ["mc", "vol1_boss"]  # illegal real-time action!
            death_scenes[2]["active_characters"] = ["mc", "vol1_boss"]
            death_scenes[2]["beat_type"] = "回忆片段复盘"  # explicit flashback exempt!
            death_errs = collect_blueprint_scene_errors(
                death_scenes,
                blueprint=blueprint,
                project=project,
                volume=vol1,
                start_chapter=4,
                end_chapter=6,
            )
            self.assertTrue(any("Chapter 5" in e and "deceased" in e for e in death_errs), death_errs)
            self.assertFalse(any("Chapter 6" in e and "deceased" in e for e in death_errs), death_errs)

    def test_retry_feedback_fixes_multiple_errors_on_attempt_2(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_t4_retry_ok_") as tmp:
            proj_dir = _materialize_fixture_project(Path(tmp))
            outline_file = proj_dir / "outline.yaml"
            raw_ol = yaml.safe_load(outline_file.read_text(encoding="utf-8"))
            raw_ol["scenes"][1]["location_id"] = "loc_market_stall"
            raw_ol["scenes"][2]["location_id"] = "loc_market_stall"
            outline_file.write_text(
                yaml.safe_dump(raw_ol, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
                newline="\n",
            )

            def _handler(req: LLMRequest) -> Dict[str, Any]:
                attempt = int(req.metadata["attempt"])
                scenes = [_make_bp_scene(ch, volume=1) for ch in range(4, 8)]
                if attempt == 1:
                    # Inject 4 simultaneous errors
                    scenes[0]["location_id"] = "loc_market_stall"  # 3rd consecutive with Ch 2..3
                    scenes[1]["scene_obstacle"] = ""
                    scenes[2]["cliffhanger_hook"] = "事情就此圆满解决，众人各自散去。"
                    scenes[3]["ledger_mutations"] = [
                        {"ledger_key": "cash", "delta": 6_000_000, "reason": "超上限"}
                    ]
                return {"scenes": scenes}

            provider = ScriptedBlueprintProvider(_handler)
            res = expand_blueprint_chapters(
                project_dir=proj_dir,
                start_chapter=4,
                end_chapter=7,
                provider=provider,
                max_retries=3,
            )
            self.assertEqual(res.attempts_used, 2)
            self.assertEqual(len(provider.requests), 2)

            retry_prompt = provider.requests[1].user_prompt
            self.assertIn("STATE_STALE_LOCATION", retry_prompt)
            self.assertIn("scene_obstacle", retry_prompt)
            self.assertIn("圆满解决", retry_prompt)
            self.assertIn("hard_max", retry_prompt)

            loaded = load_project(proj_dir)
            self.assertEqual([s.chapter for s in loaded.scenes], list(range(1, 8)))

    def test_exhausted_retries_raises_planning_validation_error_and_preserves_outline_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_t4_retry_fail_") as tmp:
            proj_dir = _materialize_fixture_project(Path(tmp))
            outline_file = proj_dir / "outline.yaml"
            before_bytes = outline_file.read_bytes()

            def _always_bad(req: LLMRequest) -> Dict[str, Any]:
                scenes = [_make_bp_scene(ch, volume=1) for ch in range(4, 7)]
                scenes[0]["cliffhanger_hook"] = "这一夜沉沉睡去，一夜无话。"
                scenes[1]["scene_goal"] = ""
                return {"scenes": scenes}

            provider = ScriptedBlueprintProvider(_always_bad)
            with self.assertRaises(PlanningValidationError) as ctx:
                expand_blueprint_chapters(
                    project_dir=proj_dir,
                    start_chapter=4,
                    end_chapter=6,
                    provider=provider,
                    max_retries=3,
                )

            self.assertEqual(len(provider.requests), 3)
            self.assertIn("一夜无话", str(ctx.exception))
            self.assertIn("scene_goal", str(ctx.exception))
            self.assertEqual(outline_file.read_bytes(), before_bytes)
            self.assertEqual(list(proj_dir.glob("*.tmp")), [])

    def test_post_write_load_project_failure_rolls_back_outline_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_t4_rollback_") as tmp:
            proj_dir = _materialize_fixture_project(Path(tmp))
            outline_file = proj_dir / "outline.yaml"
            before_bytes = outline_file.read_bytes()

            provider = ScriptedBlueprintProvider(
                lambda req: {"scenes": [_make_bp_scene(ch, volume=1) for ch in range(4, 6)]}
            )

            real_load = load_project
            call_count = {"n": 0}

            def _fail_second_load(p_dir: Any, **kwargs: Any) -> Any:
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise ProjectValidationError("Simulated post-write load_project failure")
                return real_load(p_dir, **kwargs)

            with patch("xiaoshuo_bot.blueprint_planner.load_project", side_effect=_fail_second_load):
                with self.assertRaises(PlanningValidationError) as ctx:
                    expand_blueprint_chapters(
                        project_dir=proj_dir,
                        start_chapter=4,
                        end_chapter=5,
                        provider=provider,
                    )

            self.assertIn("rolled back", str(ctx.exception))
            self.assertEqual(outline_file.read_bytes(), before_bytes)
            self.assertEqual(list(proj_dir.glob("*.tmp")), [])
            self.assertEqual(list(proj_dir.glob(".*.tmp")), [])


class TestBlueprintVolumeTransition(unittest.TestCase):
    def test_volume_2_transition_with_character_role_mutation_and_new_map_location(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_t4_vol2_") as tmp:
            proj_dir = _materialize_fixture_project(Path(tmp))
            new_role = "老街茶室直播间反诈主播兼重案队编外顾问"

            def _vol2_handler(req: LLMRequest) -> Dict[str, Any]:
                s_ch = int(req.metadata["start_chapter"])
                e_ch = int(req.metadata["end_chapter"])
                scenes: List[Dict[str, Any]] = []
                for ch in range(s_ch, e_ch + 1):
                    if ch == 41:
                        scenes.append(
                            _make_bp_scene(
                                ch,
                                volume=2,
                                active_characters=["mc", "he_xiaoman", "qi_jingming"],
                                location_id="loc_old_teahouse_studio",
                                character_mutations=[
                                    {
                                        "character_id": "mc",
                                        "role": new_role,
                                        "note": "第2卷转战茶室直播间连麦打假，身份正式升级",
                                    }
                                ],
                                events=[
                                    "何小满举着补光灯和二维码立牌冲进茶室二楼，架起直播三脚架对准八仙桌。",
                                    "齐景明在万人连麦画面里捻动紫檀佛珠嘲讽许青川是夜市神棍，暗中按动袖口磁控开关。",
                                    "许青川隔屏开启第二卷解锁的隔屏微表情观气，当众报出齐景明袖口暗扣位置与老太转账尾号（埋设 fs_river_case）。",
                                ],
                            )
                        )
                    else:
                        scenes.append(
                            _make_bp_scene(
                                ch,
                                volume=2,
                                active_characters=["mc", "he_xiaoman", "detective_ally"],
                                location_id=f"loc_teahouse_sub_{ch % 3}",
                            )
                        )
                return {"scenes": scenes}

            provider = ScriptedBlueprintProvider(_vol2_handler)
            res = expand_blueprint_chapters(
                project_dir=proj_dir,
                start_chapter=41,
                end_chapter=43,
                provider=provider,
            )
            self.assertEqual(res.volume_index, 2)
            self.assertEqual(res.added_chapters, [41, 42, 43])
            self.assertEqual(res.attempts_used, 1)

            # Prompt must include Vol 2 map, enters/exits, Vol 2 golden finger unlock, and Vol 1 exit ban
            vol2_prompt = provider.requests[0].user_prompt
            self.assertIn("loc_old_teahouse_studio", vol2_prompt)
            self.assertIn("隔屏微表情同步观气", vol2_prompt)
            self.assertIn("he_xiaoman", vol2_prompt)
            self.assertIn("已退场禁实时出场", vol2_prompt)

            loaded = load_project(proj_dir)
            ch1 = next(s for s in loaded.scenes if s.chapter == 1)
            ch41 = next(s for s in loaded.scenes if s.chapter == 41)
            self.assertEqual(ch1.volume, 1)
            self.assertEqual(ch41.volume, 2)
            self.assertEqual(ch41.location_id, "loc_old_teahouse_studio")
            self.assertEqual(len(ch41.character_mutations), 1)
            self.assertEqual(ch41.character_mutations[0].role, new_role)

            # Verify WorldStateManager synchronizes current_role via Ch 41 character_mutation
            wsm = WorldStateManager(loaded)
            wsm.apply_character_mutation(ch41.character_mutations[0], chapter=41)
            self.assertEqual(wsm.character_states["mc"].current_role, new_role)

            # Verify subsequent batch (Ch 44..45) sees updated current_role in prompt!
            res_next = expand_blueprint_chapters(
                project_dir=proj_dir,
                start_chapter=44,
                end_chapter=45,
                provider=provider,
                editorial_notes="本批禁止重复夜市单口相面，转战暗巷仓库查账并推进伏笔。",
            )
            self.assertEqual(res_next.added_chapters, [44, 45])
            self.assertIn(f"current_role={new_role}", provider.requests[1].user_prompt)
            self.assertIn("### 【主编弧线会诊与本批强制纠偏指令】", provider.requests[1].user_prompt)
            self.assertIn("本批禁止重复夜市单口相面", provider.requests[1].user_prompt)

    def test_volume_2_transition_rejects_missing_new_map_and_prior_vol_exited_character(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_t4_vol2_neg_") as tmp:
            proj_dir = _materialize_fixture_project(Path(tmp))
            project = load_project(proj_dir)
            blueprint = load_blueprint(proj_dir / "blueprint.yaml")
            vol2 = blueprint.volumes[1]

            bad_vol2_scenes = [
                _make_bp_scene(
                    41,
                    volume=2,
                    active_characters=["mc", "vol1_boss"],  # vol1_boss exited in Vol 1!
                    location_id="loc_wrong_old_street",  # missing loc_old_teahouse_studio!
                    character_mutations=[{"character_id": "mc", "role": "   "}],  # blank role!
                )
            ]
            errs = collect_blueprint_scene_errors(
                bad_vol2_scenes,
                blueprint=blueprint,
                project=project,
                volume=vol2,
                start_chapter=41,
                end_chapter=41,
            )
            joined = "\n".join(errs)
            self.assertIn("vol1_boss", joined)
            self.assertIn("exited", joined)
            self.assertIn("loc_old_teahouse_studio", joined)
            self.assertIn("role must be a non-empty string", joined)


class TestExtendVolumeScaleRules(unittest.TestCase):
    def test_collect_extend_volume_meta_errors_bounded_realistic_vs_legacy(self) -> None:
        prev_locks = {
            "cash": VolumeCapLock(ledger_key="cash", volume=1, max_value=5_000_000.0, unit="元"),
            "merit_aura": VolumeCapLock(ledger_key="merit_aura", volume=1, max_value=1000.0, unit="分"),
        }
        scale_rules = [
            {"ledger_key": "cash", "policy": "bounded_realistic", "hard_max": 5_000_000, "unit": "元"},
            {"ledger_key": "merit_aura", "policy": "unbounded_fantasy", "hard_max": None, "unit": "分"},
        ]
        base_payload = {
            "map_name": "老街二楼茶室直播间",
            "location_id": "loc_old_teahouse_studio",
            "soft_identity_reboot": "夜市摊主转战全网反诈直播间",
        }

        # 1. bounded_realistic allows flat cap (== prev == hard_max) while unbounded_fantasy increases
        ok_payload = {
            **base_payload,
            "volume_cap_locks": [
                {"ledger_key": "cash", "volume": 2, "max_value": 5_000_000.0, "unit": "元"},
                {"ledger_key": "merit_aura", "volume": 2, "max_value": 5000.0, "unit": "分"},
            ],
        }
        errs_ok, *_ = _collect_extend_volume_meta_errors(
            ok_payload,
            target_vol=2,
            trigger_map_change=True,
            old_locations={"loc_nanqiao_market"},
            prev_locks=prev_locks,
            scale_rules=scale_rules,
        )
        self.assertEqual(errs_ok, [])

        # 2. bounded_realistic rejects cap exceeding hard_max
        over_cap_payload = {
            **base_payload,
            "volume_cap_locks": [
                {"ledger_key": "cash", "volume": 2, "max_value": 6_000_000.0, "unit": "元"},
                {"ledger_key": "merit_aura", "volume": 2, "max_value": 5000.0, "unit": "分"},
            ],
        }
        errs_over, *_ = _collect_extend_volume_meta_errors(
            over_cap_payload,
            target_vol=2,
            trigger_map_change=True,
            old_locations={"loc_nanqiao_market"},
            prev_locks=prev_locks,
            scale_rules=scale_rules,
        )
        self.assertTrue(
            any("cash" in e and "hard_max" in e and "bounded_realistic" in e for e in errs_over),
            errs_over,
        )

        # 3. unbounded_fantasy still enforces strictly greater cap than previous volume
        flat_fantasy_payload = {
            **base_payload,
            "volume_cap_locks": [
                {"ledger_key": "cash", "volume": 2, "max_value": 4_500_000.0, "unit": "元"},
                {"ledger_key": "merit_aura", "volume": 2, "max_value": 1000.0, "unit": "分"},
            ],
        }
        errs_fantasy, *_ = _collect_extend_volume_meta_errors(
            flat_fantasy_payload,
            target_vol=2,
            trigger_map_change=True,
            old_locations={"loc_nanqiao_market"},
            prev_locks=prev_locks,
            scale_rules=scale_rules,
        )
        self.assertTrue(
            any("merit_aura" in e and "must exceed" in e for e in errs_fantasy),
            errs_fantasy,
        )
        self.assertFalse(any("cash" in e for e in errs_fantasy), errs_fantasy)

        # 4. Without scale_rules (legacy call): preserves original strictly-increasing rule on cash
        errs_legacy, *_ = _collect_extend_volume_meta_errors(
            ok_payload,
            target_vol=2,
            trigger_map_change=True,
            old_locations={"loc_nanqiao_market"},
            prev_locks=prev_locks,
        )
        self.assertTrue(
            any("cash" in e and "must exceed" in e for e in errs_legacy),
            errs_legacy,
        )

    def test_auto_novel_planner_extend_volume_loads_blueprint_scale_rules_automatically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_t4_ext_bp_") as tmp:
            proj_dir = _materialize_fixture_project(Path(tmp))
            # Populate Ch 1..60 in outline.yaml so extend_volume triggers Vol 2 map change at Ch 61
            outline_file = proj_dir / "outline.yaml"
            raw_ol = yaml.safe_load(outline_file.read_text(encoding="utf-8"))
            scenes = list(raw_ol["scenes"])
            for ch in range(4, 61):
                sc = _make_bp_scene(ch, volume=1)
                for k in ("scene_goal", "scene_obstacle", "scene_outcome"):
                    sc.pop(k, None)
                scenes.append(sc)
            raw_ol["scenes"] = scenes
            outline_file.write_text(
                yaml.safe_dump(raw_ol, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
                newline="\n",
            )

            class _BoundedExtendProvider(LLMProvider):
                def __init__(self) -> None:
                    self.requests: List[LLMRequest] = []

                def generate(self, request: LLMRequest) -> LLMResponse:
                    self.requests.append(request)
                    meta = request.metadata or {}
                    s_ch, e_ch = int(meta["start_chapter"]), int(meta["end_chapter"])
                    vol = int(meta["target_volume"])
                    attempt = int(meta["attempt"])
                    # Attempt 1 exceeds cash hard_max (5M -> 9M); Attempt 2 holds cash & followers flat at hard_max
                    cash_cap = 9_000_000.0 if attempt == 1 else 5_000_000.0
                    ext_scenes = []
                    for ch in range(s_ch, e_ch + 1):
                        sc = _make_bp_scene(
                            ch,
                            volume=vol,
                            location_id=f"loc_vol2_studio_{(ch - 1) % 3 + 1}",
                        )
                        for k in ("scene_goal", "scene_obstacle", "scene_outcome"):
                            sc.pop(k, None)
                        ext_scenes.append(sc)
                    payload = {
                        "map_name": "老街二楼茶室直播间",
                        "location_id": "loc_old_teahouse_studio",
                        "soft_identity_reboot": "夜市小摊主转型全网连麦反诈主播",
                        "volume_cap_locks": [
                            {"ledger_key": "cash", "volume": vol, "max_value": cash_cap, "unit": "元"},
                            {"ledger_key": "followers", "volume": vol, "max_value": 12_000_000.0, "unit": "人"},
                        ],
                        "scenes": ext_scenes,
                    }
                    return LLMResponse(
                        request_id=request.request_id,
                        content=json.dumps(payload, ensure_ascii=False),
                        model="fake-ext",
                        provider="fake",
                    )

            llm = _BoundedExtendProvider()
            res = AutoNovelPlanner(llm).extend_volume(
                proj_dir,
                batch_size=5,
                map_change_interval=60,
            )
            self.assertTrue(res.triggered_map_change)
            self.assertEqual(res.volume_index, 2)
            self.assertEqual(res.attempts_used, 2)
            self.assertEqual(len(llm.requests), 2)
            self.assertIn("hard_max", llm.requests[1].user_prompt)
            self.assertEqual(res.project.volume_cap_locks["2:cash"].max_value, 5_000_000.0)
            self.assertEqual(res.project.volume_cap_locks["2:followers"].max_value, 12_000_000.0)


if __name__ == "__main__":
    unittest.main()
