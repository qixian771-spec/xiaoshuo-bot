from __future__ import annotations

import argparse
import http.server
import json
from pathlib import Path
import shutil
import socketserver
import sqlite3
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple
import unittest
import urllib.request

from xiaoshuo_bot.cli import _build_serve_router
from xiaoshuo_bot.contracts import BookRunStatus, StockpileStatus
from xiaoshuo_bot.llm import (
    LLMConfigurationError,
    RoleEndpointConfig,
    RoleModelRouter,
    WorkBuddyModelCatalog,
    verify_router_models_against_cpa,
)
from xiaoshuo_bot.server.app import create_server
from xiaoshuo_bot.server.stockpile_db import StockpileDB


CFG_MODELS = {
    "hy4_flash": "wb/cfg-hy4-flash",
    "hy4": "wb/cfg-hy4",
    "deepseek_v4_1_flash": "wb/cfg-ds-v41-flash",
    "mimo_v2_6_pro": "wb/cfg-mimo-v26-pro",
}


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start_mock_cpa_models_server(
    models_provider,
    chat_handler=None,
) -> Tuple[_ThreadedHTTPServer, str]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/v1/models":
                status, model_ids = models_provider()
                body = json.dumps(
                    {"object": "list", "data": [{"id": m, "object": "model"} for m in model_ids]},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            if chat_handler is None:
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads((self.rfile.read(length) if length > 0 else b"{}").decode("utf-8"))
            status, body_obj, is_sse = chat_handler(payload)
            if is_sse:
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.end_headers()
                for line in body_obj:
                    self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            else:
                raw = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

    server = _ThreadedHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


class TestWorkBuddyConfigAndStartupSelfCheck(unittest.TestCase):
    def test_catalog_requires_explicit_config_for_all_4_slots_and_rejects_extra_or_forbidden(self) -> None:
        # Missing any slot must raise LLMConfigurationError naming the missing slot
        partial_env = {
            "WORKBUDDY_MODEL_HY4_FLASH": "wb/cfg-hy4-flash",
            "WORKBUDDY_MODEL_HY4": "wb/cfg-hy4",
            "WORKBUDDY_MODEL_DEEPSEEK_V4_1_FLASH": "wb/cfg-ds-v41-flash",
            # WORKBUDDY_MODEL_MIMO_V2_6_PRO intentionally omitted
        }
        with self.assertRaises(LLMConfigurationError) as ctx:
            WorkBuddyModelCatalog.from_env(partial_env)
        self.assertIn("WORKBUDDY_MODEL_MIMO_V2_6_PRO", str(ctx.exception))

        # No env vars at all -> returns None (offline/non-WorkBuddy mode unaffected)
        self.assertIsNone(WorkBuddyModelCatalog.from_env({}))

        # Extra unwhitelisted slot key -> rejected
        with self.assertRaises(LLMConfigurationError) as ctx_extra:
            WorkBuddyModelCatalog.from_mapping({**CFG_MODELS, "deepseek_v4_pro": "wb/deepseek-v4-pro"})
        self.assertIn("deepseek_v4_pro", str(ctx_extra.exception))

        # Forbidden model placed into a valid slot -> rejected
        for forbidden_val in ("wb/deepseek-v4-pro", "wb/auto", "wb/快速", "wb/均衡", "wb/极致"):
            with self.subTest(forbidden=forbidden_val):
                bad = dict(CFG_MODELS)
                bad["hy4_flash"] = forbidden_val
                with self.assertRaises(LLMConfigurationError):
                    WorkBuddyModelCatalog.from_mapping(bad)

    def test_role_defaults_from_catalog_and_cli_router_builder(self) -> None:
        catalog = WorkBuddyModelCatalog.from_mapping(CFG_MODELS)
        router = RoleModelRouter.from_workbuddy_catalog(
            catalog,
            base_url="http://127.0.0.1:8317",
            api_keys=["sk-cpa-key"],
        )
        # Writing roles start at Hy4 flash -> DeepSeek-V4.1-Flash -> Xiaomi MiMo-V2.6-Pro (no Hy4)
        for role in ("writer", "critic", "polisher"):
            cfg = router.get_role_config(role)
            self.assertEqual(cfg.model, CFG_MODELS["hy4_flash"])
            self.assertEqual(
                cfg.rotation_models,
                (CFG_MODELS["hy4_flash"], CFG_MODELS["deepseek_v4_1_flash"], CFG_MODELS["mimo_v2_6_pro"]),
            )
            self.assertNotIn(CFG_MODELS["hy4"], cfg.rotation_models)

        # Planner starts at DeepSeek-V4.1-Flash -> Xiaomi MiMo-V2.6-Pro (no Hy4 flash, no Hy4)
        planner_cfg = router.get_role_config("planner")
        self.assertEqual(planner_cfg.model, CFG_MODELS["deepseek_v4_1_flash"])
        self.assertEqual(
            planner_cfg.rotation_models,
            (CFG_MODELS["deepseek_v4_1_flash"], CFG_MODELS["mimo_v2_6_pro"]),
        )

        # Manual override to Hy4 succeeds with empty auto-rotation
        router_manual = RoleModelRouter.from_workbuddy_catalog(
            catalog,
            base_url="http://127.0.0.1:8317",
            api_keys=["sk-cpa-key"],
            role_model_overrides={"writer": CFG_MODELS["hy4"]},
        )
        self.assertEqual(router_manual.get_role_config("writer").model, CFG_MODELS["hy4"])
        self.assertEqual(router_manual.get_role_config("writer").rotation_models, ())

        # CLI _build_serve_router supports --workbuddy-* overrides and rejects out-of-whitelist role override
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                router_config=None,
                base_url="http://127.0.0.1:8317",
                api_key="sk-cpa-key",
                model=None,
                workbuddy_hy4_flash=CFG_MODELS["hy4_flash"],
                workbuddy_hy4=CFG_MODELS["hy4"],
                workbuddy_deepseek_v4_1_flash=CFG_MODELS["deepseek_v4_1_flash"],
                workbuddy_mimo_v2_6_pro=CFG_MODELS["mimo_v2_6_pro"],
                writer_model=None,
                planner_model=None,
            )
            built = _build_serve_router(args, Path(tmp) / "matrix.db")
            self.assertIsNotNone(built)
            assert built is not None
            self.assertEqual(built.get_role_config("writer").model, CFG_MODELS["hy4_flash"])
            self.assertEqual(built.get_role_config("planner").model, CFG_MODELS["deepseek_v4_1_flash"])

            # Role override outside whitelist fails fast
            bad_args = argparse.Namespace(**vars(args))
            bad_args.writer_model = "wb/gpt-not-allowed"
            with self.assertRaises(LLMConfigurationError) as bad_ctx:
                _build_serve_router(bad_args, Path(tmp) / "matrix.db")
            self.assertIn("wb/gpt-not-allowed", str(bad_ctx.exception))

    def test_startup_self_check_verifies_cpa_v1_models_and_refuses_missing_or_unwhitelisted(self) -> None:
        state = {"models": list(CFG_MODELS.values()) + ["wb/deepseek-v4-pro", "wb/auto"]}
        cpa_srv, cpa_url = _start_mock_cpa_models_server(lambda: (200, state["models"]))
        try:
            catalog = WorkBuddyModelCatalog.from_mapping(CFG_MODELS)
            router = RoleModelRouter.from_workbuddy_catalog(
                catalog,
                base_url=cpa_url,
                api_keys=["sk-cpa-key"],
            )
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                # 1. All 4 whitelisted models exist in CPA /v1/models -> startup succeeds
                srv = create_server(
                    host="127.0.0.1",
                    port=0,
                    db_path=tmp_path / "ok.db",
                    projects_root=tmp_path / "projects",
                    admin_token="a" * 32,
                    provider=router,
                )
                try:
                    self.assertIsNotNone(srv)
                finally:
                    srv.server_close()
                    srv.db.close()

                # 2. Missing model from CPA /v1/models -> refuses startup naming exact missing model
                state["models"] = [
                    CFG_MODELS["hy4_flash"],
                    CFG_MODELS["hy4"],
                    CFG_MODELS["deepseek_v4_1_flash"],
                    # mimo_v2_6_pro missing!
                ]
                with self.assertRaises(LLMConfigurationError) as miss_ctx:
                    create_server(
                        host="127.0.0.1",
                        port=0,
                        db_path=tmp_path / "miss.db",
                        projects_root=tmp_path / "projects",
                        admin_token="a" * 32,
                        provider=router,
                    )
                self.assertIn(CFG_MODELS["mimo_v2_6_pro"], str(miss_ctx.exception))

                # 3. Unwhitelisted / forbidden model configured -> refuses startup naming exact offending model
                state["models"] = list(CFG_MODELS.values()) + ["wb/not-in-whitelist"]
                rogue_router = RoleModelRouter(
                    roles={
                        "writer": RoleEndpointConfig(
                            role="writer",
                            base_url=cpa_url,
                            model="wb/not-in-whitelist",
                            api_keys=["sk-cpa-key"],
                        )
                    },
                    workbuddy_catalog=catalog,
                )
                with self.assertRaises(LLMConfigurationError) as rogue_ctx:
                    create_server(
                        host="127.0.0.1",
                        port=0,
                        db_path=tmp_path / "rogue.db",
                        projects_root=tmp_path / "projects",
                        admin_token="a" * 32,
                        provider=rogue_router,
                    )
                self.assertIn("wb/not-in-whitelist", str(rogue_ctx.exception))

                # 4. Forbidden DeepSeek-V4-Pro even without catalog -> verify_router_models_against_cpa refuses
                forbidden_router = RoleModelRouter(
                    roles={
                        "writer": RoleEndpointConfig(
                            role="writer",
                            base_url=cpa_url,
                            model="wb/deepseek-v4-pro",
                            api_keys=["sk-cpa-key"],
                        )
                    }
                )
                with self.assertRaises(LLMConfigurationError) as forb_ctx:
                    verify_router_models_against_cpa(forbidden_router)
                self.assertIn("DeepSeek-V4-Pro", str(forb_ctx.exception))
        finally:
            cpa_srv.shutdown()
            cpa_srv.server_close()

        # 5. CPA unreachable -> startup refuses cleanly
        unreachable_router = RoleModelRouter.from_workbuddy_catalog(
            WorkBuddyModelCatalog.from_mapping(CFG_MODELS),
            base_url="http://127.0.0.1:1",
            api_keys=["sk-cpa-key"],
            timeout=2,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LLMConfigurationError) as unreach_ctx:
                create_server(
                    host="127.0.0.1",
                    port=0,
                    db_path=Path(tmp) / "unreach.db",
                    projects_root=Path(tmp) / "projects",
                    admin_token="a" * 32,
                    provider=unreachable_router,
                )
            self.assertIn("/v1/models", str(unreach_ctx.exception))


class TestPerChapterModelConsistencyAndMigration(unittest.TestCase):
    def test_legacy_db_migrates_chapters_and_production_stats_model_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE books (
                    book_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    genre_id TEXT NOT NULL DEFAULT '',
                    author_level TEXT NOT NULL DEFAULT 'lv0_1',
                    contract_track TEXT NOT NULL DEFAULT 'complete_150k',
                    project_dir TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    paused_reason TEXT NOT NULL DEFAULT '',
                    isolation_detail_json TEXT NOT NULL DEFAULT '{}',
                    account_id TEXT NOT NULL DEFAULT '',
                    author_name TEXT NOT NULL DEFAULT '',
                    fanqie_book_id TEXT NOT NULL DEFAULT '',
                    daily_target_chapters INTEGER NOT NULL DEFAULT 2,
                    max_published_chapter INTEGER NOT NULL DEFAULT 0,
                    total_chapters INTEGER NOT NULL DEFAULT 0,
                    signed INTEGER NOT NULL DEFAULT 0,
                    cover_path TEXT NOT NULL DEFAULT '',
                    alternate_titles_json TEXT NOT NULL DEFAULT '[]',
                    alternate_blurbs_json TEXT NOT NULL DEFAULT '[]',
                    opening_motif TEXT NOT NULL DEFAULT '',
                    golden_finger_type TEXT NOT NULL DEFAULT '',
                    cut_confirmation_version INTEGER NOT NULL DEFAULT 0,
                    cut_pending_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE chapters (
                    book_id TEXT NOT NULL,
                    chapter INTEGER NOT NULL,
                    public_title TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    author_note TEXT NOT NULL DEFAULT '',
                    char_count INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    locked INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'STOCKPILED',
                    task_id TEXT NOT NULL DEFAULT '',
                    leased_by_account TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    lease_expires_epoch REAL NOT NULL DEFAULT 0.0,
                    remote_chapter_id TEXT NOT NULL DEFAULT '',
                    scheduled_at TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    scheduled_cst_date TEXT NOT NULL DEFAULT '',
                    quota_deferred_to_date TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (book_id, chapter)
                );
                CREATE TABLE production_stats (
                    book_id TEXT NOT NULL,
                    chapter INTEGER NOT NULL,
                    duration_sec REAL NOT NULL,
                    attempts_used INTEGER NOT NULL,
                    rewrites_triggered INTEGER NOT NULL,
                    char_count INTEGER NOT NULL,
                    produced_at TEXT NOT NULL,
                    PRIMARY KEY (book_id, chapter)
                );
                INSERT INTO books (book_id, title, project_dir, created_at, updated_at)
                VALUES ('old_book', '旧书', '/tmp/old', '2026-11-01T00:00:00+08:00', '2026-11-01T00:00:00+08:00');
                INSERT INTO chapters (book_id, chapter, public_title, body, char_count, created_at, updated_at)
                VALUES ('old_book', 1, '旧第一章', '旧正文内容', 5, '2026-11-01T00:00:00+08:00', '2026-11-01T00:00:00+08:00');
                """
            )
            conn.commit()
            conn.close()

            db = StockpileDB(db_path)
            try:
                chapters = db.list_chapters("old_book")
                self.assertEqual(len(chapters), 1)
                self.assertEqual(chapters[0].model, "")
                # Writing a new chapter with model persists cleanly
                ch2 = db.add_stockpiled_chapter(
                    "old_book",
                    2,
                    "新第二章",
                    "新正文内容",
                    model="wb/cfg-ds-v41-flash",
                )
                self.assertEqual(ch2.model, "wb/cfg-ds-v41-flash")
                detail = db.get_chapter("old_book", 2)
                self.assertEqual(detail["model"], "wb/cfg-ds-v41-flash")
                db.record_production_stat(
                    "old_book",
                    2,
                    duration_sec=12.5,
                    attempts_used=1,
                    rewrites_triggered=0,
                    char_count=5,
                    model="wb/cfg-ds-v41-flash",
                )
            finally:
                db.close()

    def test_chapter_model_consistent_across_meta_sqlite_and_api_and_ui_contract(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        app_js = (repo_root / "src/xiaoshuo_bot/server/static/app.js").read_text(encoding="utf-8")
        self.assertIn('MODEL_SWITCHES: "/api/model-switches"', app_js)
        self.assertIn("reader-chapter-model", app_js)
        self.assertIn("data-chapter-model", app_js)
        self.assertIn("从 ' + escapeHtml(item.from_model", app_js)
        self.assertIn("切到 ' + escapeHtml(item.to_model", app_js)

        sample_project = repo_root / "projects" / "baiwu_dukou"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "projects" / "baiwu_dukou"
            shutil.copytree(sample_project, proj_dir)

            # Build valid chapter prose passing baiwu_dukou chapter 1 gate checks (FS-01 红铁丹, FS-02 墨渍, >=1900 chars)
            base_paras = [
                "十一月中旬的傍晚，江雾贴着老街青石板漫过门槛。林晚拎着素色帆布袋停在敬山钟表铺外，垂眼看向门口那块发硬的棕垫，边缘正蹭着半枚带桐油腥气的红铁丹防锈漆橡胶鞋印。那颜色暗沉发涩，是新港三号码头船坞修补驳船吃水线才用的专配防锈漆，绝不该沾在即将拆迁的老街门垫上。",
                "屋檐下站着港务局档案科员周叙，他裹着深灰夹克，手里捏一只厚实的牛皮信封，见林晚走近便略显局促地递过来。周叙抬手摘下眼镜用衣角擦拭水汽的工夫，林晚目光掠过他左袖口内侧，清楚瞥见那里蹭着一道陈旧未洗净的蓝黑墨渍，呢料纤维间还挂着半根发黄的纸毛。",
                "推开吱呀作响的木门，铺内暖黄的台灯光线落在木柜台与一排停摆的老挂钟上。陈敬山从里间端出两只骨瓷茶杯，给林晚倒了七分满的热茶，杯沿那处米粒大小的缺口正朝向左下方，一如林昼生前常来串门时的旧习惯。",
                "林晚没有急着端茶，先拆开牛皮信封取出那只铜壳怀表，轻轻搁在柜台绒布垫上。表盘玻璃蒙着薄灰，秒针死死卡在三点十七分，表冠边缘缺了半截齿纹；她翻过表背凑近灯光细看，后盖接缝里除了干涸发灰的江泥，还赫然留着一道不超过三天的新鲜硬物撬痕。",
                "林晚抬头问周叙，这只遗物在移交前究竟经手过哪些人。周叙垂下眼帘摩挲着眼镜腿，支吾说局里最近清仓搬迁，旧库房杂乱得很，登记簿也翻得七零八落，说罢便借口还要赶回局里值夜班，匆匆推门告辞。",
                "木门合拢后，街面上的江雾更浓，周叙的背影几步便融进白茫茫的雾气里。林晚左手拇指无意识地摩挲过食指指腹那道陈年刻刀旧疤，顺手把铜壳怀表与柜台边沿的铜尺拨成严格平行的两条线，请陈敬山关好铺门进里间开盖查验。",
                "陈敬山没有多问，走到门口把沉重的木栓咔嗒一声推紧，又隔着蒙雾的玻璃窗朝空荡荡的巷口望了两眼。老街两侧待拆迁的砖楼早已人去楼空，唯独巷尾电线杆下悬着一盏昏暗路灯，照出一地湿滑反光的落叶与断续水渍。",
                "回到外间柜台内侧，陈敬山弯腰拉开玻璃柜第三格滑轨，伸手去取那只包浆温润的黄杨木修表镊盒。柜门滑开的轻响在寂静铺子里格外清晰，盒盖边缘磨得发亮，里面整整齐齐码着全套弯头铜镊与放大目镜。",
            ]
            # Expand to ~2200 clean narrative chars without repetitive filler or AI tics
            more_paras = [
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
            full_prose = "\n\n".join(base_paras + more_paras)

            def chat_handler(payload: Dict[str, Any]):
                m = payload.get("model")
                if m == CFG_MODELS["hy4_flash"]:
                    return 400, {
                        "error": {
                            "code": "credit_package_exhausted",
                            "message": "WorkBuddy requires an active credit package with available balance (upstream code 14018)",
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
                    reply_text = "怀表停在三点十七分，那半张油纸背后的旧账明晚揭晓。"
                else:
                    reply_text = full_prose
                chunk1 = json.dumps(
                    {
                        "id": "chatcmpl-ch1",
                        "model": m,
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply_text}, "finish_reason": None}],
                    },
                    ensure_ascii=False,
                )
                chunk2 = json.dumps(
                    {
                        "id": "chatcmpl-ch1",
                        "model": m,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 500, "completion_tokens": 1200},
                    },
                    ensure_ascii=False,
                )
                return 200, [f"data: {chunk1}\n\n", f"data: {chunk2}\n\n", "data: [DONE]\n\n"], True

            cpa_srv, cpa_url = _start_mock_cpa_models_server(
                lambda: (200, list(CFG_MODELS.values())),
                chat_handler=chat_handler,
            )
            admin_token = "t" * 32
            try:
                catalog = WorkBuddyModelCatalog.from_mapping(CFG_MODELS)
                router = RoleModelRouter.from_workbuddy_catalog(
                    catalog,
                    base_url=cpa_url,
                    api_keys=["sk-cpa-key"],
                )
                srv = create_server(
                    host="127.0.0.1",
                    port=0,
                    db_path=tmp_path / "matrix.db",
                    projects_root=tmp_path / "projects",
                    admin_token=admin_token,
                    provider=router,
                    export_only=True,
                )
                port = srv.server_address[1]
                srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
                srv_thread.start()
                try:
                    srv.db.register_book(
                        book_id="baiwu_dukou",
                        title="白雾渡口",
                        project_dir=proj_dir,
                        account_id="acct_1",
                        status=BookRunStatus.ACTIVE,
                    )
                    # Produce chapter 1 -> Hy4 flash hits 429 -> switches to DeepSeek-V4.1-Flash
                    res = srv.scheduler.produce_next_chapter("baiwu_dukou")
                    self.assertEqual(res["status"], "PRODUCED")
                    self.assertEqual(res["model"], CFG_MODELS["deepseek_v4_1_flash"])

                    # 1. Check chapter_0001.meta.json
                    meta_path = proj_dir / "output" / "chapters" / "chapter_0001.meta.json"
                    self.assertTrue(meta_path.is_file())
                    meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                    self.assertEqual(meta_data["model"], CFG_MODELS["deepseek_v4_1_flash"])
                    self.assertEqual(meta_data["writer_model"], CFG_MODELS["deepseek_v4_1_flash"])
                    self.assertEqual(meta_data["critic_model"], CFG_MODELS["deepseek_v4_1_flash"])
                    writer_switches = [s for s in meta_data["model_switches"] if s["role"] == "writer"]
                    self.assertEqual(len(writer_switches), 1)
                    self.assertEqual(writer_switches[0]["from_model"], CFG_MODELS["hy4_flash"])
                    self.assertEqual(writer_switches[0]["to_model"], CFG_MODELS["deepseek_v4_1_flash"])

                    # 2. Check SQLite chapters.model and production_stats.model
                    ch_db = srv.db.get_chapter("baiwu_dukou", 1)
                    self.assertEqual(ch_db["model"], CFG_MODELS["deepseek_v4_1_flash"])

                    # 3. Check HTTP Admin API (/api/books/{id}/chapters, /chapters/1, /api/model-switches)
                    headers = {"Authorization": f"Bearer {admin_token}"}
                    with urllib.request.urlopen(
                        urllib.request.Request(f"http://127.0.0.1:{port}/api/books/baiwu_dukou/chapters", headers=headers)
                    ) as r_list:
                        list_payload = json.loads(r_list.read().decode("utf-8"))
                    self.assertEqual(list_payload["chapters"][0]["model"], CFG_MODELS["deepseek_v4_1_flash"])

                    with urllib.request.urlopen(
                        urllib.request.Request(f"http://127.0.0.1:{port}/api/books/baiwu_dukou/chapters/1", headers=headers)
                    ) as r_det:
                        det_payload = json.loads(r_det.read().decode("utf-8"))
                    self.assertEqual(det_payload["model"], CFG_MODELS["deepseek_v4_1_flash"])

                    with urllib.request.urlopen(
                        urllib.request.Request(f"http://127.0.0.1:{port}/api/model-switches", headers=headers)
                    ) as r_sw:
                        sw_payload = json.loads(r_sw.read().decode("utf-8"))
                    writer_sw_api = [e for e in sw_payload["events"] if e["role"] == "writer"]
                    self.assertEqual(len(writer_sw_api), 1)
                    self.assertIn("Hy4 flash", writer_sw_api[0]["from_model"])
                    self.assertIn("DeepSeek-V4.1-Flash", writer_sw_api[0]["to_model"])
                finally:
                    srv.shutdown()
                    srv.server_close()
                    srv.db.close()
            finally:
                cpa_srv.shutdown()
                cpa_srv.server_close()


if __name__ == "__main__":
    unittest.main()
