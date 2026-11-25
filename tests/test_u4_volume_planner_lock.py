"""U4 offline tests: Volume health lock gate, strong-model planner routing, anti-repetition blacklist, and cast growth arc injection."""

from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoshuo_bot.blueprint import BookBlueprint, MAX_VOLUMES, load_blueprint  # noqa: E402
from xiaoshuo_bot.blueprint_planner import (  # noqa: E402
    BlueprintExpansionResult,
    expand_blueprint_chapters,
)
from xiaoshuo_bot.blueprint_project import materialize_project  # noqa: E402
from xiaoshuo_bot.genres import get_genre  # noqa: E402
from xiaoshuo_bot.judge import PairwiseJudge  # noqa: E402
from xiaoshuo_bot.llm import (  # noqa: E402
    LLMProvider,
    LLMRequest,
    LLMResponse,
    RoleModelRouter,
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
    WORKBUDDY_SLOT_HY4,
    WORKBUDDY_SLOT_HY4_FLASH,
    WORKBUDDY_SLOT_MIMO_V2_6_PRO,
    WorkBuddyModelCatalog,
)
from xiaoshuo_bot.loader import load_project  # noqa: E402
from xiaoshuo_bot.story_architect import StoryArchitect  # noqa: E402
from xiaoshuo_bot.volume_health import (  # noqa: E402
    LOCK_STATE_LOCKED,
    LOCK_STATE_PASSED,
    VOLUME_HEALTH_CHECK_IDS,
    VolumeHealthCheckItem,
    VolumeHealthLockError,
    VolumeHealthReport,
    run_volume_health_check,
    save_volume_health_result,
)


BLUEPRINT_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_valid.yaml"
BLUEPRINT_30VOLS_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_30vols_valid.yaml"
EXTRAS_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_project_extras.yaml"
OUTLINE_FIXTURE = ROOT / "tests" / "fixtures" / "blueprint_project_outline.yaml"

CATALOG_MODELS: Dict[str, str] = {
    WORKBUDDY_SLOT_HY4_FLASH: "wb/test-hy4-flash",
    WORKBUDDY_SLOT_HY4: "wb/test-hy4-manual",
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH: "wb/test-ds-v41-flash",
    WORKBUDDY_SLOT_MIMO_V2_6_PRO: "wb/test-mimo-v26-pro",
}

KEY_ENV_VARS = (
    "XIAOSHUO_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
)


def _make_bp_scene(
    chapter: int,
    *,
    volume: int = 2,
    pov: str = "mc",
    active_characters: Optional[List[str]] = None,
    location_id: Optional[str] = None,
) -> Dict[str, Any]:
    loc = location_id or f"loc_teahouse_room_{(chapter - 1) % 3 + 1}"
    pol = "burst" if chapter % 3 == 0 else ("suppress" if chapter % 3 == 1 else "mixed")
    chars = active_characters if active_characters is not None else [pov, "he_xiaoman", "detective_ally"]
    return {
        "chapter": chapter,
        "scene_id": f"ch{chapter:04d}_u4_scene",
        "internal_title": f"茶室连麦破局{chapter}",
        "public_title": f"隔屏拆穿伪大师{chapter}",
        "beat_type": "反手打脸与线索推进",
        "emotion_polarity": pol,
        "pov_character": pov,
        "active_characters": list(chars),
        "active_assets": ["stall_wooden_sign", "abacus_bracelet"],
        "style_profile_ids": ["book_style"],
        "location_time": f"第{chapter}日夜，老街二楼茶室直播间",
        "location_id": loc,
        "min_chars": 2100,
        "max_chars": 2500,
        "scene_goal": f"第{chapter}章许青川与何小满借连麦拆穿齐景明磁控骗局并固定转账流水",
        "scene_obstacle": f"第{chapter}章齐景明买通水军疯狂举报企图封禁直播间",
        "scene_outcome": f"第{chapter}章当众卸下磁铁机关并拿下受害人银行回执，但左眼灼痛袭来",
        "events": [
            f"第{chapter}章何小满调整补光灯与公证录屏机位，稳住直播间弹幕节奏并核对账单。",
            f"第{chapter}章许青川开启隔屏微表情观气，点破齐景明袖口暗扣里的强磁开关位置。",
            f"第{chapter}章宋海潮凭固定好的转账流水带队出击，截获齐景明后台财务平板。",
        ],
        "stop_condition": f"第{chapter}章齐景明被带离时冷笑说出渡江船票编号，画面戛然而止。",
        "cliffhanger_hook": f"第{chapter}号后台平板突然弹出一条加密通知：渡江沉案旧证人正在失联。",
        "segment_comment_cue": "隔屏拆穿伪大师袖口磁铁机关全场沸腾",
        "volume": volume,
        "calendar_days_elapsed": 1,
        "post_scene_asset_mutations": [],
        "ledger_mutations": [],
        "character_mutations": [],
        "expectation_directives": [],
    }


def _load_u1_enriched_blueprint_raw() -> Dict[str, Any]:
    raw = yaml.safe_load(BLUEPRINT_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    # Populate Volume 1 with U1 fields + explicit Vol 1 map so anti-repetition blacklist covers every field.
    vol1 = raw["volumes"][0]
    vol1["map"] = {"location_id": "loc_nanqiao_night_market", "name": "南桥老街古玩夜市"}
    vol1["antagonist_motive"] = "借夜市摊霸地位垄断古玩假货销赃并强吞沿街铺位"
    vol1["new_gameplay"] = "线下夜市支摊近距看卦+暗巷仓库查封假玉器"
    vol1["mainline_step"] = "在假玉器行暗格里发现二十年前同模印章线索"
    vol1["daily_case_quota"] = 3
    vol1["fugitive_case_quota"] = 1
    vol1["forbidden_from_prev_volume"] = []

    # Populate Volume 2 with distinct U1 fields & explicit forbidden_from_prev_volume.
    vol2 = raw["volumes"][1]
    vol2["antagonist_motive"] = "包装国学大师人设在直播间兜售天价磁控转运珠收割老人养老钱"
    vol2["new_gameplay"] = "茶室高清连麦隔屏观气+公证镜头实时拆穿机关"
    vol2["mainline_step"] = "通过连麦受害老太手中半张渡轮船票撕开二十年前渡江沉案缺口"
    vol2["daily_case_quota"] = 4
    vol2["fugitive_case_quota"] = 2
    vol2["forbidden_from_prev_volume"] = [
        "禁止复用线下古玩夜市砸摊栽赃桥段",
        "禁止再用地下夹墙暗格搜赃作为高潮手段",
    ]

    # Populate Volume 6 with daily_case_quota == 0 to verify zero quota is injected (not skipped as falsy).
    vol6 = raw["volumes"][5]
    vol6["daily_case_quota"] = 0
    vol6["fugitive_case_quota"] = 1

    # Populate cast members with U1 independent growth arc fields (including exited vol1_boss to verify exclusion).
    cast_by_id = {c["id"]: c for c in raw["cast"]}
    cast_by_id["detective_ally"]["independent_goal"] = "查清当年老队长因渡江冷案被迫提前病退的内情"
    cast_by_id["detective_ally"]["flaw_or_setback"] = "过度坚持单兵硬扛，曾因越级私查被停职处分"
    cast_by_id["detective_ally"]["stage_arcs"] = [
        "第1卷：从怀疑许青川到默认编外协查",
        "第2卷：建立经侦与直播反诈联合取证机制",
    ]
    cast_by_id["he_xiaoman"]["independent_goal"] = "还清家中医疗欠债并打造独立可信的公益反诈直播矩阵"
    cast_by_id["he_xiaoman"]["flaw_or_setback"] = "急于扩粉时轻信MCN流量合同险遭违约索赔"
    cast_by_id["he_xiaoman"]["stage_arcs"] = [
        "第2卷：扛住水军封号危机完成首场万人公证直播",
        "第3卷：独当一面主持证据回放与跨区苦主对接",
    ]
    cast_by_id["vol1_boss"]["independent_goal"] = "独霸南桥夜市所有古玩摊位抽成"
    cast_by_id["vol1_boss"]["flaw_or_setback"] = "刚愎自用低估监控取证威力"
    cast_by_id["vol1_boss"]["stage_arcs"] = ["第1卷：机关算尽终入狱退场"]
    return raw


def _materialize_u1_project(tmp_root: Path, name: str = "u4_proj") -> Path:
    bp_path = tmp_root / f"{name}_bp.yaml"
    bp_path.write_text(
        yaml.safe_dump(_load_u1_enriched_blueprint_raw(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    return materialize_project(
        blueprint_path=bp_path,
        extras_path=EXTRAS_FIXTURE,
        outline_path=OUTLINE_FIXTURE,
        out_dir=tmp_root / name,
        existing_cast_names=[],
    )


class _CountingStubProvider(LLMProvider):
    def __init__(
        self,
        handler: Callable[[LLMRequest], Any],
        *,
        model: str = "stub-mimo-pro",
    ) -> None:
        self.handler = handler
        self.model = model
        self.requests: List[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        res = self.handler(request)
        content = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
        return LLMResponse(
            request_id=request.request_id,
            content=content,
            model=self.model,
            provider="stub",
            latency_sec=0.01,
        )


def _write_locked_v1_health(audit_dir: Path) -> None:
    checks = [
        VolumeHealthCheckItem(
            id=cid,
            title=cid,
            status="FAIL" if cid == "VH_01_NO_ROUTINE_REPEAT" else "PASS",
            deterministic_passed=cid != "VH_01_NO_ROUTINE_REPEAT",
            semantic_passed=cid != "VH_01_NO_ROUTINE_REPEAT",
            chapters=[38, 39] if cid == "VH_01_NO_ROUTINE_REPEAT" else [],
            evidence="套路复读未通过" if cid == "VH_01_NO_ROUTINE_REPEAT" else "通过",
            repair_hint="重写第38~39章冲突玩法" if cid == "VH_01_NO_ROUTINE_REPEAT" else "",
        )
        for cid in VOLUME_HEALTH_CHECK_IDS
    ]
    report = VolumeHealthReport(
        volume_index=1,
        volume_title="南桥夜摊初开瞳",
        chapter_start=1,
        chapter_end=40,
        verdict="FAIL",
        lock_state=LOCK_STATE_LOCKED,
        checks=checks,
        repair_actions=[
            {
                "check_id": "VH_01_NO_ROUTINE_REPEAT",
                "chapters": [38, 39],
                "repair_hint": "重写第38~39章冲突玩法",
            }
        ],
        summary="第 1 卷体检未通过，锁定禁止进第 2 卷。",
        judge_model="wb/test-mimo-v26-pro",
        writer_model="wb/test-ds-v41-flash",
        report_file="",
        lock_file="",
        recorded_at="",
    )
    save_volume_health_result(audit_dir, report)


def _pass_v1_health_via_real_runner(proj_dir: Path, audit_dir: Path) -> VolumeHealthReport:
    project = load_project(proj_dir)
    blueprint = load_blueprint(proj_dir / "blueprint.yaml")
    vol1 = blueprint.volumes[0]

    judge_payload = json.dumps(
        {
            "verdict": "PASS",
            "checks": [
                {
                    "id": cid,
                    "status": "PASS",
                    "chapters": [],
                    "evidence": f"{cid} 达标。",
                    "repair_hint": "",
                }
                for cid in VOLUME_HEALTH_CHECK_IDS
            ],
            "summary": "第 1 卷六项硬体检全部通过。",
        },
        ensure_ascii=False,
    )
    judge_provider = _CountingStubProvider(lambda _req: judge_payload, model="wb/test-mimo-v26-pro")
    judge = PairwiseJudge(
        judge_provider,
        judge_model="wb/test-mimo-v26-pro",
        writer_model="wb/test-ds-v41-flash",
    )

    fs_map: Dict[str, Dict[str, Any]] = {}
    for fs in blueprint.foreshadow_plan:
        status = "planted" if fs.id in vol1.foreshadows_plant else "pending"
        fs_map[fs.id] = {
            "id": fs.id,
            "title": fs.title,
            "status": status,
            "plant_chapter": 1,
            "due_chapter": 40,
        }
    snapshot = {
        "current_volume": 1,
        "foreshadows": fs_map,
        "numeric_balances": {"cash": 50000.0, "followers": 100000.0, "merit_aura": 120.0},
        "volume_cap_locks": {},
    }

    chapters: List[Tuple[int, str]] = [
        (1, "梅雨刚停的南桥夜市，许青川摆正旧木牌，靠算卦与观气一眼认出妇人袖里发卡。宋海潮记录车牌并核查监控。"),
        (2, "古玩巷口人声嘈杂，因果秤微光闪过，许青川揭穿假玉印砂轮痕，夜市摊主纷纷围观。便衣随即封锁库房。"),
        (3, "探照灯照进夹墙暗格，直播镜头同步定格失踪登记簿，通缉线人落网。宋海潮签收整箱物证。"),
        (4, "街口茶棚清晨对账，许青川复盘算卦收据与因果秤残影，锁定下游黑车接头点。重案队连夜布控。"),
        (5, "收摊时夜市灯火通明，观气余波渐散，直播回放突破十万点赞，通缉犯同伙全部移交刑侦归档。"),
    ]

    report = run_volume_health_check(
        volume_spec=vol1,
        chapters=chapters,
        world_state_snapshot=snapshot,
        project=project,
        blueprint=blueprint,
        judge=judge,
        audit_dir=audit_dir,
    )
    assert report.verdict == "PASS", f"Expected PASS, got {report.to_dict()}"
    assert report.lock_state == LOCK_STATE_PASSED
    return report


def _start_mock_workbuddy_sse_server(
    handler_fn: Callable[[Dict[str, Any]], Tuple[str, str]],
) -> Tuple[ThreadingHTTPServer, str, List[Dict[str, Any]]]:
    calls: List[Dict[str, Any]] = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            body = json.loads(raw)
            calls.append({"path": self.path, "model": body.get("model"), "body": body})
            resp_model, content_str = handler_fn(body)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()

            chunk_1 = {
                "id": "chatcmpl-u4-test",
                "object": "chat.completion.chunk",
                "model": resp_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": content_str},
                        "finish_reason": None,
                    }
                ],
            }
            chunk_2 = {
                "id": "chatcmpl-u4-test",
                "object": "chat.completion.chunk",
                "model": resp_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 240, "total_tokens": 360},
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
    return server, f"http://{host}:{port}/v1", calls


class TestU4VolumeHealthLockGate(unittest.TestCase):
    def test_vol2_blocked_when_audit_dir_missing_record_or_locked_with_zero_llm_calls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_u4_lock_") as tmp:
            proj_dir = _materialize_u1_project(Path(tmp))
            provider = _CountingStubProvider(lambda _req: {"scenes": []})

            # Case A: explicit audit_dir has no Vol 1 record -> VolumeHealthLockError, 0 LLM calls
            empty_audit = Path(tmp) / "audit_empty"
            empty_audit.mkdir()
            with self.assertRaises(VolumeHealthLockError) as ctx_missing:
                expand_blueprint_chapters(
                    project_dir=proj_dir,
                    start_chapter=41,
                    end_chapter=43,
                    provider=provider,
                    audit_dir=empty_audit,
                )
            self.assertEqual(ctx_missing.exception.volume_index, 1)
            self.assertIn("MISSING_VOLUME_HEALTH_RECORD", ctx_missing.exception.failed_checks)
            self.assertEqual(len(provider.requests), 0)

            # Case B: explicit audit_dir has Vol 1 in LOCKED_NEEDS_REPAIR -> VolumeHealthLockError, 0 LLM calls
            locked_audit = Path(tmp) / "audit_locked"
            _write_locked_v1_health(locked_audit)
            with self.assertRaises(VolumeHealthLockError) as ctx_locked:
                expand_blueprint_chapters(
                    project_dir=proj_dir,
                    start_chapter=41,
                    end_chapter=43,
                    provider=provider,
                    audit_dir=locked_audit,
                )
            self.assertEqual(ctx_locked.exception.volume_index, 1)
            self.assertIn("VH_01_NO_ROUTINE_REPEAT", ctx_locked.exception.failed_checks)
            self.assertEqual(len(provider.requests), 0)

            # Case C: non-bool require_prev_volume_health rejected strictly with ValueError, 0 LLM calls
            for bad_flag in (1, 0, "true", "false", None):
                with self.subTest(require_prev_volume_health=bad_flag):
                    with self.assertRaises(ValueError):
                        expand_blueprint_chapters(
                            project_dir=proj_dir,
                            start_chapter=41,
                            end_chapter=43,
                            provider=provider,
                            audit_dir=locked_audit,
                            require_prev_volume_health=bad_flag,  # type: ignore[arg-type]
                        )
            self.assertEqual(len(provider.requests), 0)

    def test_vol2_blocked_with_value_error_on_corrupted_volume_locks_json_zero_llm_calls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_u4_corrupt_") as tmp:
            proj_dir = _materialize_u1_project(Path(tmp))
            provider = _CountingStubProvider(lambda _req: {"scenes": []})

            corrupt_audit = Path(tmp) / "audit_corrupt"
            corrupt_audit.mkdir()
            (corrupt_audit / "volume_locks.json").write_text("{not valid json!!!", encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                expand_blueprint_chapters(
                    project_dir=proj_dir,
                    start_chapter=41,
                    end_chapter=43,
                    provider=provider,
                    audit_dir=corrupt_audit,
                )
            self.assertIn("Corrupted volume_locks.json", str(ctx.exception))
            self.assertEqual(len(provider.requests), 0)

    def test_vol2_blocked_by_default_project_audit_dir_when_locks_file_exists_zero_llm_calls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_u4_default_audit_") as tmp:
            proj_dir = _materialize_u1_project(Path(tmp))
            default_audit = proj_dir / "output" / "audit"
            _write_locked_v1_health(default_audit)

            provider = _CountingStubProvider(lambda _req: {"scenes": []})
            # Do NOT pass audit_dir explicitly; default <project>/output/audit/volume_locks.json must enforce lock!
            with self.assertRaises(VolumeHealthLockError) as ctx:
                expand_blueprint_chapters(
                    project_dir=proj_dir,
                    start_chapter=41,
                    end_chapter=43,
                    provider=provider,
                )
            self.assertEqual(ctx.exception.volume_index, 1)
            self.assertIn("VH_01_NO_ROUTINE_REPEAT", ctx.exception.failed_checks)
            self.assertEqual(len(provider.requests), 0)

            # Corrupting default volume_locks.json must also raise ValueError with 0 LLM calls.
            (default_audit / "volume_locks.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                expand_blueprint_chapters(
                    project_dir=proj_dir,
                    start_chapter=41,
                    end_chapter=43,
                    provider=provider,
                )
            self.assertEqual(len(provider.requests), 0)


class TestU4PromptInjectionAndUnlockedExpansion(unittest.TestCase):
    def test_unlocked_vol1_allows_vol2_and_injects_u1_fields_blacklist_and_cast_growth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xsb_u4_unlocked_") as tmp:
            proj_dir = _materialize_u1_project(Path(tmp))
            audit_dir = proj_dir / "output" / "audit"
            _pass_v1_health_via_real_runner(proj_dir, audit_dir)

            def _vol2_handler(req: LLMRequest) -> Dict[str, Any]:
                s_ch = int(req.metadata["start_chapter"])
                e_ch = int(req.metadata["end_chapter"])
                scenes = []
                for ch in range(s_ch, e_ch + 1):
                    loc = "loc_old_teahouse_studio" if ch == 41 else f"loc_teahouse_side_{ch % 3}"
                    scenes.append(_make_bp_scene(ch, volume=2, location_id=loc))
                return {"scenes": scenes}

            provider = _CountingStubProvider(_vol2_handler, model="wb/test-mimo-v26-pro")
            result = expand_blueprint_chapters(
                project_dir=proj_dir,
                start_chapter=41,
                end_chapter=43,
                provider=provider,
                audit_dir=audit_dir,
            )

            self.assertIsInstance(result, BlueprintExpansionResult)
            self.assertEqual(result.volume_index, 2)
            self.assertEqual(result.added_chapters, [41, 42, 43])
            self.assertEqual(result.planner_model, "wb/test-mimo-v26-pro")
            self.assertEqual(result.to_dict()["planner_model"], "wb/test-mimo-v26-pro")
            self.assertEqual(len(provider.requests), 1)

            req = provider.requests[0]
            self.assertEqual(req.metadata["planner_role"], "volume_planner")
            prompt = req.user_prompt

            # 1. Current volume U1 fields injected in ### 当前卷蓝图
            self.assertIn("包装国学大师人设在直播间兜售天价磁控转运珠收割老人养老钱", prompt)
            self.assertIn("茶室高清连麦隔屏观气+公证镜头实时拆穿机关", prompt)
            self.assertIn("通过连麦受害老太手中半张渡轮船票撕开二十年前渡江沉案缺口", prompt)
            self.assertIn("daily_case_quota=4", prompt)
            self.assertIn("fugitive_case_quota=2", prompt)
            self.assertIn("禁止复用线下古玩夜市砸摊栽赃桥段", prompt)
            self.assertIn("禁止再用地下夹墙暗格搜赃作为高潮手段", prompt)

            # 2. Previous volume anti-repetition blacklist block injected cleanly
            self.assertIn("### 【防套路重复黑名单（严禁复刻上一卷）】", prompt)
            self.assertIn("南桥夜摊初开瞳", prompt)
            self.assertIn("摆摊精准断事打脸", prompt)
            self.assertIn("线下夜市支摊近距看卦+暗巷仓库查封假玉器", prompt)
            self.assertIn("借夜市摊霸地位垄断古玩假货销赃并强吞沿街铺位", prompt)
            self.assertIn("loc_nanqiao_night_market", prompt)
            self.assertIn("南桥老街古玩夜市", prompt)

            # 3. Active supporting cast growth arcs injected; exited Vol 1 boss growth arc NOT injected
            self.assertIn("查清当年老队长因渡江冷案被迫提前病退的内情", prompt)
            self.assertIn("过度坚持单兵硬扛，曾因越级私查被停职处分", prompt)
            self.assertIn("建立经侦与直播反诈联合取证机制", prompt)
            self.assertIn("还清家中医疗欠债并打造独立可信的公益反诈直播矩阵", prompt)
            self.assertIn("急于扩粉时轻信MCN流量合同险遭违约索赔", prompt)
            self.assertIn("扛住水军封号危机完成首场万人公证直播", prompt)
            self.assertIn("严禁写成纯工具人", prompt)
            self.assertNotIn("独霸南桥夜市所有古玩摊位抽成", prompt)

            # 4. Also verify daily_case_quota == 0 is explicitly injected (not skipped as falsy)
            result_v6 = expand_blueprint_chapters(
                project_dir=proj_dir,
                start_chapter=331,
                end_chapter=331,
                provider=_CountingStubProvider(
                    lambda _req: {
                        "scenes": [
                            _make_bp_scene(
                                331,
                                volume=6,
                                active_characters=["mc", "detective_ally", "yuan_boheng"],
                                location_id="loc_old_river_ferry",
                            )
                        ]
                    },
                    model="wb/test-mimo-v26-pro",
                ),
                require_prev_volume_health=False,
            )
            self.assertEqual(result_v6.volume_index, 6)
            # Re-verify prompt directly via a fresh capture on ch 332 with quota==0
            v6_cap = _CountingStubProvider(
                lambda _req: {
                    "scenes": [
                        _make_bp_scene(
                            332,
                            volume=6,
                            active_characters=["mc", "detective_ally", "yuan_boheng"],
                            location_id="loc_ferry_pier_east",
                        )
                    ]
                },
                model="wb/test-mimo-v26-pro",
            )
            expand_blueprint_chapters(
                project_dir=proj_dir,
                start_chapter=332,
                end_chapter=332,
                provider=v6_cap,
                require_prev_volume_health=False,
            )
            self.assertIn("daily_case_quota=0", v6_cap.requests[0].user_prompt)


class TestU4StrongModelRoutingLoopback(unittest.TestCase):
    def test_architect_and_blueprint_expand_route_to_mimo_pro_never_flash_or_hy4(self) -> None:
        bp_payload = _load_u1_enriched_blueprint_raw()

        def _server_handler(body: Dict[str, Any]) -> Tuple[str, str]:
            requested_model = str(body.get("model") or "")
            messages = body.get("messages") or []
            user_msg = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
            if "阶段: blueprint_expand" in user_msg:
                scenes = [
                    _make_bp_scene(
                        ch,
                        volume=2,
                        location_id="loc_old_teahouse_studio" if ch == 41 else f"loc_teahouse_sub_{ch % 3}",
                    )
                    for ch in (41, 42)
                ]
                return requested_model, json.dumps({"scenes": scenes}, ensure_ascii=False)
            return requested_model, json.dumps(bp_payload, ensure_ascii=False)

        server, base_url, calls = _start_mock_workbuddy_sse_server(_server_handler)
        clean_env = {k: v for k, v in os.environ.items() if k not in KEY_ENV_VARS}
        try:
            with patch.dict(os.environ, clean_env, clear=True):
                catalog = WorkBuddyModelCatalog.from_mapping(CATALOG_MODELS)
                router = RoleModelRouter.from_workbuddy_catalog(
                    catalog,
                    base_url=base_url,
                    api_keys=["sk-u4-offline-loopback-key"],
                    sleep_fn=lambda _s: None,
                )

                # 1. StoryArchitect.draft_blueprint must route to mimo_v2_6_pro
                with tempfile.TemporaryDirectory(prefix="xsb_u4_router_") as tmp:
                    bp_out = Path(tmp) / "drafted_blueprint.yaml"
                    architect = StoryArchitect()
                    bp = architect.draft_blueprint(
                        idea="南桥夜市独眼摊主靠因果秤瞳一卷一副本直播破案",
                        genre_id="urban_brainhole",
                        target_total_chars=900000,
                        existing_cast_names=["唐子墨", "冯念慈"],
                        provider=router,
                        output_path=bp_out,
                    )
                    self.assertIsInstance(bp, BookBlueprint)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0]["model"], CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])

                    # 2. expand_blueprint_chapters must also hit mimo_v2_6_pro and record planner_model
                    proj_dir = _materialize_u1_project(Path(tmp), name="routed_proj")
                    audit_dir = proj_dir / "output" / "audit"
                    _pass_v1_health_via_real_runner(proj_dir, audit_dir)

                    res = expand_blueprint_chapters(
                        project_dir=proj_dir,
                        start_chapter=41,
                        end_chapter=42,
                        provider=router,
                        audit_dir=audit_dir,
                    )
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[1]["model"], CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])
                    self.assertEqual(res.planner_model, CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO])
                    self.assertEqual(
                        res.to_dict()["planner_model"],
                        CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                    )

                    # Assert neither hy4_flash, deepseek_v4_1_flash, nor manual-only hy4 was hit as primary
                    called_models = [c["model"] for c in calls]
                    self.assertNotIn(CATALOG_MODELS[WORKBUDDY_SLOT_HY4_FLASH], called_models)
                    self.assertNotIn(CATALOG_MODELS[WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH], called_models)
                    self.assertNotIn(CATALOG_MODELS[WORKBUDDY_SLOT_HY4], called_models)

                    # 3. Verify fallback to router.get_active_model("planner") when resp.model is empty
                    def _empty_model_generate(role: str, request: LLMRequest) -> LLMResponse:
                        self.assertEqual(role, "planner")
                        self.assertEqual(request.metadata["planner_role"], "volume_planner")
                        return LLMResponse(
                            request_id=request.request_id,
                            content=json.dumps(
                                {"scenes": [_make_bp_scene(43, volume=2, location_id="loc_teahouse_floor3")]},
                                ensure_ascii=False,
                            ),
                            model="",
                            provider="stub",
                        )

                    with patch.object(router, "generate", side_effect=_empty_model_generate):
                        res_fallback = expand_blueprint_chapters(
                            project_dir=proj_dir,
                            start_chapter=43,
                            end_chapter=43,
                            provider=router,
                            audit_dir=audit_dir,
                        )
                    self.assertEqual(
                        res_fallback.planner_model,
                        CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
                    )
        finally:
            server.shutdown()
            server.server_close()


class TestU4StoryArchitectLongFormAndU1Fields(unittest.TestCase):
    def test_user_prompt_includes_40_volume_cap_anti_repeat_and_cast_growth_fields(self) -> None:
        recipe = get_genre("urban_brainhole")
        prompt = StoryArchitect._build_user_prompt(
            idea="沧江老码头古玩修复师三十卷一卷一副本直播追凶",
            recipe=recipe,
            target_total_chars=2_300_000,
            existing_cast_names=["唐子墨"],
            benchmark_notes="对标直播破案爆款，强调一卷一副本换玩法",
            constraints="千章拆 30 卷，六大配角各有独立成长线",
        )

        self.assertIn(f"{MAX_VOLUMES}", prompt)
        self.assertIn("3..40 卷", prompt)
        self.assertIn("一卷一副本", prompt)
        self.assertIn("30 卷", prompt)
        for vol_field in (
            "antagonist_motive",
            "new_gameplay",
            "mainline_step",
            "daily_case_quota",
            "fugitive_case_quota",
            "forbidden_from_prev_volume",
        ):
            self.assertIn(vol_field, prompt)
        for cast_field in (
            "independent_goal",
            "flaw_or_setback",
            "stage_arcs",
        ):
            self.assertIn(cast_field, prompt)
        self.assertIn("纯工具人", prompt)

    def test_draft_blueprint_produces_valid_30vol_blueprint_with_u1_fields_and_architect_role(self) -> None:
        raw_30 = yaml.safe_load(BLUEPRINT_30VOLS_FIXTURE.read_text(encoding="utf-8"))
        assert isinstance(raw_30, dict)
        provider = _CountingStubProvider(
            lambda _req: json.dumps(raw_30, ensure_ascii=False),
            model=CATALOG_MODELS[WORKBUDDY_SLOT_MIMO_V2_6_PRO],
        )

        with tempfile.TemporaryDirectory(prefix="xsb_u4_arch30_") as tmp:
            out_path = Path(tmp) / "bp_30vols.yaml"
            bp = StoryArchitect().draft_blueprint(
                idea="沧江老码头古玩修复师三十卷一卷一副本直播追凶",
                genre_id="urban_brainhole",
                target_total_chars=2_300_000,
                existing_cast_names=["唐子墨", "冯念慈"],
                provider=provider,
                output_path=out_path,
            )

            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(provider.requests[0].metadata["planner_role"], "architect")
            self.assertEqual(len(bp.volumes), 30)
            self.assertTrue(bp.volumes[1].antagonist_motive.strip())
            self.assertTrue(bp.volumes[1].new_gameplay.strip())
            self.assertTrue(bp.volumes[1].mainline_step.strip())
            self.assertEqual(bp.volumes[1].daily_case_quota, 3)
            self.assertEqual(bp.volumes[1].fugitive_case_quota, 1)
            self.assertGreaterEqual(len(bp.volumes[1].forbidden_from_prev_volume), 1)
            self.assertEqual(bp.volumes[-1].daily_case_quota, 0)

            cast_by_id = {m.id: m for m in bp.cast}
            self.assertTrue(cast_by_id["ally_detective"].independent_goal.strip())
            self.assertTrue(cast_by_id["ally_detective"].flaw_or_setback.strip())
            self.assertGreaterEqual(len(cast_by_id["ally_detective"].stage_arcs), 3)

            reloaded = load_blueprint(out_path)
            self.assertEqual(reloaded.to_dict(), bp.to_dict())


if __name__ == "__main__":
    unittest.main()
