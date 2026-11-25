from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from .backup import export_backup, git_incremental_backup, import_backup, verify_backup
from .blueprint import load_blueprint
from .checker import AITicChecker
from .compiler import PromptCompiler
from .contracts import (
    CompanionLeaseRequest,
    CompanionLeaseResponse,
    CompanionTaskType,
    StockpileStatus,
)
from .cover import generate_cover, generate_cover_for_project
from .deconstructor import BestsellerDeconstructor
from .engine import NovelGenerationEngine
from .evolution import MotifWeightStore
from .exporter import export_project_bundle
from .genres import get_genre, list_genres
from .judge import PairwiseJudge, _check_same_or_same_family_models
from .llm import (
    AnthropicProvider,
    CPAManagementClient,
    FaultInjectionProvider,
    FileBridgeProvider,
    LLMConfigurationError,
    LLMProvider,
    OpenAICompatibleProvider,
    RoleEndpointConfig,
    RoleModelRouter,
    UsageBudgetLedger,
    WORKBUDDY_MODEL_COOLDOWN_ENV,
    WORKBUDDY_RATE_LIMIT_BACKOFF_ENV,
    WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH,
    WORKBUDDY_SLOT_HY4,
    WORKBUDDY_SLOT_HY4_FLASH,
    WORKBUDDY_SLOT_MIMO_V2_6_PRO,
    WorkBuddyModelCatalog,
    parse_rate_limit_backoff_sec,
)
from .loader import load_project
from .models import ChapterArtifact, resolve_chapter_path
from .pilot import load_case_summaries, parse_chapters_arg, run_pilot
from .planner import AutoNovelPlanner
from .server import MatrixHTTPServer, create_server
from .state import WorldStateManager
from .telegram_bot import TelegramBotConfig, TelegramControlBot
from .transcreator import (
    WebnovelTranscreator,
    build_glossary,
    export_english_serialization_bundle,
)
from .volume_health import VolumeHealthReport, run_volume_health_check

_CHAPTER_FILE_RE = re.compile(r"^chapter_(\d+)\.txt$")


def _resolve_env_or_arg(arg_val: Optional[str], *env_names: str) -> Optional[str]:
    if arg_val is not None and str(arg_val).strip():
        return str(arg_val).strip()
    for name in env_names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return None


def _build_text_provider(args: argparse.Namespace) -> LLMProvider:
    provider_name = getattr(args, "provider", "openai")
    # Provider-specific env fallback: never send an OPENAI_* credential/endpoint to Anthropic
    # (or vice versa). XIAOSHUO_* stays the provider-agnostic override.
    vendor_prefix = "ANTHROPIC" if provider_name == "anthropic" else "OPENAI"
    api_key = _resolve_env_or_arg(
        getattr(args, "api_key", None),
        "XIAOSHUO_API_KEY",
        f"{vendor_prefix}_API_KEY",
    )
    base_url = _resolve_env_or_arg(
        getattr(args, "base_url", None),
        "XIAOSHUO_BASE_URL",
        f"{vendor_prefix}_BASE_URL",
    )
    model = _resolve_env_or_arg(
        getattr(args, "model", None),
        "XIAOSHUO_MODEL",
        f"{vendor_prefix}_MODEL",
    )

    if provider_name == "openai":
        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
    if provider_name == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
    if provider_name == "bridge":
        project_ref = getattr(args, "project", None) or getattr(args, "output_dir", None) or "."
        bridge_dir = getattr(args, "bridge_dir", None) or (Path(project_ref).resolve() / ".bridge")
        return FileBridgeProvider(
            bridge_dir=bridge_dir,
            timeout_sec=getattr(args, "bridge_timeout", 900),
            reuse_existing=bool(getattr(args, "resume", False)),
        )
    raise ValueError(f"Unsupported provider: {provider_name}")


def _load_benchmark_texts(
    benchmark_files: Optional[List[str]],
    benchmark_dir: Optional[str],
) -> List[str]:
    texts: List[str] = []
    if benchmark_files:
        for fp in benchmark_files:
            p = Path(fp).resolve()
            if not p.is_file():
                raise FileNotFoundError(f"Benchmark file does not exist: '{p}'")
            texts.append(p.read_text(encoding="utf-8"))
    if benchmark_dir:
        d = Path(benchmark_dir).resolve()
        if not d.is_dir():
            raise FileNotFoundError(f"Benchmark directory does not exist: '{d}'")
        for p in sorted(d.glob("*.txt")):
            texts.append(p.read_text(encoding="utf-8"))
    return texts


def _collect_chapter_artifacts(project_dir: Path, chapters_dir: Path) -> List[ChapterArtifact]:
    project = load_project(project_dir)
    scene_map = {s.chapter: s for s in project.scenes}
    if not chapters_dir.is_dir():
        raise FileNotFoundError(f"Chapters directory does not exist: '{chapters_dir}'")

    chapter_nums: set[int] = set()
    for p in chapters_dir.iterdir():
        if p.is_file():
            m = _CHAPTER_FILE_RE.match(p.name)
            if m and int(m.group(1)) > 0:
                chapter_nums.add(int(m.group(1)))

    artifacts: List[ChapterArtifact] = []
    for ch_no in sorted(chapter_nums):
        ch_path = resolve_chapter_path(chapters_dir, ch_no)
        if ch_path is None or not ch_path.is_file():
            continue
        body = ch_path.read_text(encoding="utf-8").strip()
        meta_path = chapters_dir / f"chapter_{ch_no:04d}.meta.json"
        meta_data = {}
        if meta_path.is_file():
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
        sc = scene_map.get(ch_no)
        public_title = str(
            meta_data.get("public_title")
            or (sc.public_title if sc else "")
            or (sc.internal_title if sc else "")
        ).strip()
        artifacts.append(
            ChapterArtifact(
                chapter=ch_no,
                public_title=public_title,
                body=body,
                author_note=str(meta_data.get("author_note", "")),
                sha256=str(meta_data.get("sha256", "")),
                locked=bool(meta_data.get("locked", sc.locked if sc else False)),
                scene_id=str(meta_data.get("scene_id") or (sc.scene_id if sc else "")),
            )
        )
    return artifacts


def _cmd_validate(args: argparse.Namespace) -> int:
    project = load_project(args.project)
    print(f"[OK] Project '{project.meta.title}' ({project.meta.project_id}) validated successfully.")
    print(
        f"     Characters: {len(project.characters)} | Assets: {len(project.assets)} | "
        f"Foreshadows: {len(project.foreshadows)} | Style Profiles: {len(project.style_profiles)} | "
        f"Chapters: {len(project.scenes)}"
    )
    return 0


def _cmd_compile_prompt(args: argparse.Namespace) -> int:
    project = load_project(args.project)
    state = WorldStateManager(project)
    compiler = PromptCompiler(project)
    matching = [s for s in project.scenes if s.chapter == args.chapter]
    if not matching:
        print(f"Error: Chapter {args.chapter} not found.", file=sys.stderr)
        return 1
    scene = matching[0]
    if scene.chapter == 1:
        recent_texts: List[str] = []
    else:
        output_dir_arg = getattr(args, "output_dir", None)
        if not output_dir_arg or not str(output_dir_arg).strip():
            print(
                f"Error: --output-dir is required when compiling chapter {scene.chapter} (> 1) "
                "to load recent chapter texts for anchor cooldown.",
                file=sys.stderr,
            )
            return 1
        chapters_dir = Path(output_dir_arg).resolve() / "chapters"
        if not chapters_dir.is_dir():
            print(
                f"Error: Chapters directory does not exist: '{chapters_dir}'",
                file=sys.stderr,
            )
            return 1
        recent_texts = []
        for prev_ch in range(max(1, scene.chapter - 3), scene.chapter):
            prev_path = resolve_chapter_path(chapters_dir, prev_ch)
            if prev_path is None or not prev_path.is_file():
                print(
                    f"Error: Missing preceding chapter {prev_ch:04d} text in '{chapters_dir}' "
                    f"(required to compile chapter {scene.chapter}).",
                    file=sys.stderr,
                )
                return 1
            recent_texts.append(prev_path.read_text(encoding="utf-8"))
    sys_p, usr_p = compiler.compile_scene_prompt(
        scene,
        state,
        recent_chapter_texts=tuple(recent_texts),
    )
    print(sys_p)
    print()
    print(usr_p)
    return 0


def _cmd_check_text(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8").rstrip()
    scene = None
    characters = None
    due_fs = None
    plant_fs = None
    project_meta = None
    if args.project:
        project = load_project(args.project)
        characters = project.characters
        project_meta = project.meta
        if args.chapter is not None:
            state = WorldStateManager(project)
            matching = [s for s in project.scenes if s.chapter == args.chapter]
            if matching:
                scene = matching[0]
                due_fs = state.get_due_foreshadows(scene.chapter)
                plant_fs = state.get_plant_foreshadows(scene.chapter)

    checker = AITicChecker(
        pov_mode=project_meta.pov_mode if project_meta else "third_limited",
        panel_policy=project_meta.panel_policy if project_meta else "forbid",
        contract_track=project_meta.contract_track if project_meta else "legacy",
    )
    report = checker.scan(
        text=text,
        scene=scene,
        characters=characters,
        due_foreshadows=due_fs,
        plant_foreshadows=plant_fs,
        project_meta=project_meta,
        chapter=args.chapter,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 2


def _cmd_smoke_fault(args: argparse.Namespace) -> int:
    """Verify ProductionIncident detection & automatic rewrite recovery using deterministic fault injection."""
    project = load_project(args.project)
    first_scene = project.scenes[0]
    plant_cues = []
    for fs in project.foreshadows.values():
        if fs.plant_chapter == first_scene.chapter and fs.plant_keywords:
            plant_cues.append(fs.plant_keywords[0])
    cue_sentence = "。".join(f"门垫边缘沾着半枚{kw}" for kw in plant_cues)
    base_para = (
        "雾气从江面漫上石阶时，林晚把左手拇指抵在食指侧面的旧刻刀疤痕上，推开了敬山钟表铺虚掩的玻璃木门。"
        "柜台里的煤油灯芯压得很低，陈敬山低头用镊子拨正一只旧摆钟的擒纵叉，没有抬头。"
        "周叙站在屋檐下，把眼镜摘下来用衣角慢慢擦了一遍，递过一只装在牛皮信封里的铜壳怀表。"
        f"{cue_sentence}。"
    )
    filler_blocks = [
        "林晚接过信封，把怀表平放在绒布垫上，让它的边沿与柜台铜尺对齐成一条直线。秒针卡在三点十七分的位置，表冠右侧缺了半截齿纹。她用放大镜贴近表壳接缝，金属缝隙里嵌着一点干涸的暗褐色江泥。",
        "“港务局清退旧宿舍时从排水槽底下翻出来的。”周叙右肩微塌，话说到一半停了一下，手指蹭过袖口，“名册上写着林昼的名字。”",
        "陈敬山端起那只杯沿有个米粒大缺口的骨瓷茶杯，往里倒了七分满的热茶，推到林晚手边。壶嘴冒出的白气很快散在潮冷的穿堂风里。老人的视线落在表冠断口上，手指在玻璃柜台第三格的黄杨木镊盒上方停住。",
        "“这表后盖被人拿硬撬刀动过。”林晚放下放大镜，声音很轻，“撬痕是新的，不超过三天。”",
        "街对面拆迁办的铁皮广播响了两声电流杂音，雨水顺着钟表铺老旧的木招牌滴在青石板上。周叙把湿透的登记表折成两折塞回公文包，退后半步让开了门口的光线。",
    ]
    clean_paragraphs = [base_para]
    idx = 0
    while len("".join(clean_paragraphs)) < first_scene.min_chars + 150:
        clean_paragraphs.append(filler_blocks[idx % len(filler_blocks)])
        idx += 1
    clean_text = "\n\n".join(clean_paragraphs)

    out_dir = Path(args.output_dir) if args.output_dir else Path(tempfile.mkdtemp(prefix="xiaoshuo_fault_"))
    provider = FaultInjectionProvider(clean_text=clean_text)
    engine = NovelGenerationEngine(project=project, provider=provider, output_dir=out_dir)
    manifest = engine.run(target_chapters=[first_scene.chapter])

    assert len(manifest["incidents"]) == 1, "Expected exactly 1 ProductionIncident on attempt 1"
    assert manifest["chapters"][0]["attempts_used"] == 2, "Expected rewrite to succeed on attempt 2"
    print(
        f"[PASS] Fault-Injection Rewrite Smoke Test succeeded! "
        f"Intercepted {manifest['incidents'][0]['violation_count']} deliberate violations on Attempt 1 "
        f"and auto-rewrote cleanly on Attempt 2. Audit saved to: {out_dir}"
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    project = load_project(args.project)
    provider = _build_text_provider(args)
    out_dir = Path(args.output_dir) if args.output_dir else (Path(args.project).resolve() / "output")
    target_chapters: Optional[List[int]] = None
    if getattr(args, "chapters", None):
        target_chapters = [int(x.strip()) for x in args.chapters.split(",") if x.strip()]

    resume_flag = bool(getattr(args, "resume", False)) or args.command == "resume"
    unlock_flag = bool(getattr(args, "unlock", False))
    engine = NovelGenerationEngine(project=project, provider=provider, output_dir=out_dir)
    manifest = engine.run(
        target_chapters=target_chapters,
        resume=resume_flag,
        unlock=unlock_flag,
    )
    print(
        f"\n[COMPLETED] Novel '{project.meta.title}' generated successfully!\n"
        f"  Chapters: {manifest['chapter_count']} | Total Chars: {manifest['total_chars']:,} | "
        f"Incidents Auto-Resolved: {manifest['total_incidents_detected']}\n"
        f"  Pure Manuscript:  {manifest['artifacts']['manuscript_pure']}\n"
        f"  Reader Edition:   {manifest['artifacts']['manuscript_reader']}\n"
        f"  Audit Report:     {manifest['artifacts']['smoke_test_report']}\n"
        f"  HTML Dashboard:   {manifest['artifacts']['dashboard_html']}"
    )
    return 0


def _cmd_genres(args: argparse.Namespace) -> int:
    if args.id:
        recipe = get_genre(args.id)
        if args.json:
            print(json.dumps(recipe.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"[{recipe.genre_id}] {recipe.display_name} ({recipe.channel})")
            print(f"  Category: {recipe.tomato_primary_category} | Tags: {', '.join(recipe.tomato_tags)}")
            print(f"  POV: {recipe.pov_mode} | Panel: {recipe.panel_policy} | Words: {recipe.min_chapter_words}~{recipe.max_chapter_words}")
            print(recipe.common_sense_card.format_prompt_block())
        return 0

    recipes = list_genres(channel=args.channel)
    if args.json:
        print(json.dumps([r.to_dict() for r in recipes], ensure_ascii=False, indent=2))
    else:
        for r in recipes:
            print(
                f"- {r.genre_id:<22} | {r.channel:<7} | {r.display_name} "
                f"({r.pov_mode}, {r.panel_policy}, {r.min_chapter_words}~{r.max_chapter_words}w)"
            )
    return 0


def _cmd_auto_create(args: argparse.Namespace) -> int:
    provider = _build_text_provider(args)
    planner = AutoNovelPlanner(provider, max_consecutive_same_location=2)
    motif_store = MotifWeightStore(args.motif_store) if args.motif_store else None
    hot_keywords = [k.strip() for k in args.hot_keywords.split(",") if k.strip()] if args.hot_keywords else []
    benchmark_texts = _load_benchmark_texts(args.benchmark_file, args.benchmark_dir)
    result = planner.plan_new_project(
        project_id=args.project_id,
        genre_id=args.genre,
        output_dir=args.output_dir,
        author_level=args.author_level,
        projects_root=args.projects_root,
        pen_name=args.pen_name,
        hot_keywords=hot_keywords,
        motif_store=motif_store,
        benchmark_chapters=benchmark_texts or None,
        target_chapters=args.target_chapters,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_deconstruct(args: argparse.Namespace) -> int:
    benchmark_texts = _load_benchmark_texts(args.benchmark_file, args.benchmark_dir)
    if not benchmark_texts:
        raise ValueError("Provide at least one --benchmark-file or --benchmark-dir")
    provider = _build_text_provider(args)
    deconstructor = BestsellerDeconstructor(provider)
    hot_keywords = [k.strip() for k in args.hot_keywords.split(",") if k.strip()] if args.hot_keywords else []
    blueprint = deconstructor.deconstruct(
        benchmark_texts,
        target_genre_id=args.target_genre,
        source_domain=args.source_domain or "",
        target_domain=args.target_domain or "",
        hot_keywords=hot_keywords,
    )
    rendered = json.dumps(blueprint.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        out_p = Path(args.output).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        tmp_p = out_p.with_name(f"{out_p.name}.tmp")
        tmp_p.write_text(rendered, encoding="utf-8")
        os.replace(tmp_p, out_p)
    print(rendered)
    return 0


def _cmd_extend(args: argparse.Namespace) -> int:
    provider = _build_text_provider(args)
    planner = AutoNovelPlanner(provider, max_consecutive_same_location=2)
    if args.finale:
        res = planner.plan_finale_volume(
            args.project,
            snapshot_path=args.snapshot,
            num_chapters=args.num_chapters,
        )
    else:
        res = planner.extend_volume(
            args.project,
            snapshot_path=args.snapshot,
            batch_size=args.batch_size,
            map_change_interval=args.map_change_interval,
        )
    print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_cover(args: argparse.Namespace) -> int:
    project = load_project(args.project)
    router: Optional[RoleModelRouter] = None
    if args.base_mode == "ai_image":
        api_key = _resolve_env_or_arg(args.api_key, "XIAOSHUO_IMAGE_API_KEY", "XIAOSHUO_API_KEY", "OPENAI_API_KEY")
        base_url = _resolve_env_or_arg(args.base_url, "XIAOSHUO_IMAGE_BASE_URL", "XIAOSHUO_BASE_URL", "OPENAI_BASE_URL")
        model = _resolve_env_or_arg(args.model, "XIAOSHUO_IMAGE_MODEL", "XIAOSHUO_MODEL")
        if api_key and base_url and model:
            router = RoleModelRouter(
                {
                    "image": RoleEndpointConfig(
                        role="image",
                        base_url=base_url,
                        model=model,
                        api_keys=[api_key],
                        provider_type=args.provider_type,
                    )
                }
            )
    pen_name_override = getattr(args, "pen_name", None)
    if pen_name_override is not None:
        result = generate_cover(
            title=project.meta.title,
            pen_name=str(pen_name_override),
            output_path=args.output,
            genre=project.meta.genre,
            synopsis=project.meta.synopsis,
            base_mode=args.base_mode,
            router=router,
            output_format=args.format,
        )
    else:
        result = generate_cover_for_project(
            project,
            args.output,
            base_mode=args.base_mode,
            router=router,
            output_format=args.format,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    chapters_dir = Path(args.chapters_dir).resolve() if args.chapters_dir else (project_dir / "output" / "chapters")
    artifacts = _collect_chapter_artifacts(project_dir, chapters_dir)
    if not artifacts:
        raise ValueError(f"No completed chapter files found in '{chapters_dir}'")

    platform = args.platform.strip().lower()
    if platform == "webnovel":
        provider = _build_text_provider(args)
        glossary = build_glossary(project, title_en=args.title_en)
        transcreator = WebnovelTranscreator(
            glossary,
            provider=provider,
            auto_normalize=bool(args.auto_normalize),
        )
        translated = [transcreator.transcreate_chapter(art) for art in artifacts]
        res = export_english_serialization_bundle(translated, glossary, args.output_dir)
        print(
            json.dumps(
                {
                    "platform": "webnovel",
                    "output_dir": str(res.output_dir),
                    "glossary_path": str(res.glossary_path),
                    "manuscript_path": str(res.manuscript_path),
                    "bundle_json_path": str(res.bundle_json_path),
                    "chapter_count": res.chapter_count,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if platform == "tomato":
        res = export_project_bundle(
            project,
            artifacts,
            args.output_dir,
            platform="tomato",
            cover_path=args.cover,
        )
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        return 0

    res = export_project_bundle(
        project,
        artifacts,
        args.output_dir,
        platform=platform,
    )
    print(
        json.dumps(
            {
                "platform": platform,
                "output_dir": str(res.output_dir),
                "manuscript_path": str(res.manuscript_path),
                "metadata_path": str(res.metadata_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_backup_export(args: argparse.Namespace) -> int:
    res = export_backup(
        output_zip=args.output,
        projects_dir=args.projects_dir,
        data_dir=args.data_dir,
        motif_store_path=args.motif_store,
        ledger_path=args.ledger,
        extra_files=args.extra,
    )
    print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_backup_verify(args: argparse.Namespace) -> int:
    manifest = verify_backup(args.input)
    print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_backup_import(args: argparse.Namespace) -> int:
    res = import_backup(
        args.input,
        args.target_dir,
        overwrite=not bool(args.no_overwrite),
    )
    print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_backup_git(args: argparse.Namespace) -> int:
    paths = [p.strip() for p in args.paths.split(",") if p.strip()] if args.paths else None
    res = git_incremental_backup(
        args.repo,
        paths=paths,
        remote=args.remote,
        branch=args.branch,
        commit_message=args.message,
        push=bool(args.push),
    )
    print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _split_csv(raw: Optional[str]) -> List[str]:
    if not raw or not str(raw).strip():
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _build_serve_cpa_client(args: argparse.Namespace) -> Optional[CPAManagementClient]:
    cpa_url = _resolve_env_or_arg(
        getattr(args, "cpa_url", None),
        "CPA_MANAGEMENT_URL",
        "XIAOSHUO_CPA_URL",
        "CPA_BASE_URL",
    )
    cpa_key = _resolve_env_or_arg(
        getattr(args, "cpa_management_key", None),
        "CPA_MANAGEMENT_KEY",
        "XIAOSHUO_CPA_MANAGEMENT_KEY",
        "CPA_MANAGEMENT_SECRET",
    )
    if not cpa_url and not cpa_key:
        return None
    return CPAManagementClient(base_url=cpa_url, management_key=cpa_key)


def _build_serve_router(
    args: argparse.Namespace,
    db_path: Path,
) -> Optional[RoleModelRouter]:
    budget_raw = getattr(args, "daily_budget_limit", None)
    if budget_raw is None:
        env_budget = os.environ.get("XIAOSHUO_DAILY_BUDGET_LIMIT", "").strip()
        budget_limit = float(env_budget) if env_budget else None
    else:
        budget_limit = float(budget_raw)

    chapter_raw = getattr(args, "daily_chapter_limit", None)
    if chapter_raw is None:
        env_chap = os.environ.get("XIAOSHUO_DAILY_CHAPTER_LIMIT", "").strip()
        chapter_limit = int(env_chap) if env_chap else None
    else:
        chapter_limit = int(chapter_raw)

    ledger_path = _resolve_env_or_arg(
        getattr(args, "ledger_path", None),
        "XIAOSHUO_LEDGER_PATH",
    ) or str(db_path.with_name("usage_ledger.json"))
    ledger = UsageBudgetLedger(
        daily_budget_limit=budget_limit,
        daily_chapter_limit=chapter_limit,
        state_path=ledger_path,
    )

    router_cfg_path = _resolve_env_or_arg(
        getattr(args, "router_config", None),
        "XIAOSHUO_ROUTER_CONFIG",
    )
    if router_cfg_path:
        cfg_file = Path(router_cfg_path).resolve()
        if not cfg_file.is_file():
            raise LLMConfigurationError(f"Router configuration file not found: '{cfg_file}'")
        raw_text = cfg_file.read_text(encoding="utf-8")
        loaded = json.loads(raw_text) if cfg_file.suffix.lower() == ".json" else yaml.safe_load(raw_text)
        if not isinstance(loaded, Mapping):
            raise LLMConfigurationError(f"Router configuration in '{cfg_file}' must be a mapping.")
        return RoleModelRouter.from_dict(loaded, ledger=ledger)

    default_url = _resolve_env_or_arg(
        getattr(args, "base_url", None),
        "XIAOSHUO_BASE_URL",
        "OPENAI_BASE_URL",
    )
    default_keys_raw = _resolve_env_or_arg(
        getattr(args, "api_key", None),
        "XIAOSHUO_API_KEY",
        "OPENAI_API_KEY",
    )
    default_model = _resolve_env_or_arg(
        getattr(args, "model", None),
        "XIAOSHUO_MODEL",
        "OPENAI_MODEL",
    )

    wb_catalog = WorkBuddyModelCatalog.from_env(
        overrides={
            WORKBUDDY_SLOT_HY4_FLASH: getattr(args, "workbuddy_hy4_flash", None),
            WORKBUDDY_SLOT_HY4: getattr(args, "workbuddy_hy4", None),
            WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH: getattr(args, "workbuddy_deepseek_v4_1_flash", None),
            WORKBUDDY_SLOT_MIMO_V2_6_PRO: getattr(args, "workbuddy_mimo_v2_6_pro", None),
        }
    )

    text_roles = ("planner", "writer", "critic", "polisher")
    role_overrides: Dict[str, Dict[str, Optional[str]]] = {}
    any_llm_setting = bool(default_url or default_keys_raw or default_model or wb_catalog is not None)
    for role in (*text_roles, "image"):
        env_prefix = f"XIAOSHUO_{role.upper()}"
        r_url = _resolve_env_or_arg(getattr(args, f"{role}_base_url", None), f"{env_prefix}_BASE_URL")
        r_keys = _resolve_env_or_arg(getattr(args, f"{role}_api_key", None), f"{env_prefix}_API_KEY")
        r_model = _resolve_env_or_arg(getattr(args, f"{role}_model", None), f"{env_prefix}_MODEL")
        r_ptype = _resolve_env_or_arg(
            getattr(args, f"{role}_provider_type", None),
            f"{env_prefix}_PROVIDER_TYPE",
        )
        role_overrides[role] = {
            "base_url": r_url,
            "api_keys": r_keys,
            "model": r_model,
            "provider_type": r_ptype,
        }
        if any((r_url, r_keys, r_model)):
            any_llm_setting = True

    if not any_llm_setting:
        return None

    if wb_catalog is not None:
        wb_base_url = default_url or _resolve_env_or_arg(
            getattr(args, "cpa_url", None),
            "CPA_BASE_URL",
            "XIAOSHUO_CPA_URL",
            "CPA_MANAGEMENT_URL",
        )
        wb_keys = _split_csv(default_keys_raw)
        role_model_ov = {role: role_overrides[role]["model"] for role in text_roles}
        role_url_ov = {role: role_overrides[role]["base_url"] for role in text_roles}
        role_key_ov = {
            role: _split_csv(role_overrides[role]["api_keys"])
            for role in text_roles
            if role_overrides[role]["api_keys"]
        }
        img_ov = role_overrides["image"]
        image_cfg: Optional[RoleEndpointConfig] = None
        if any((img_ov["base_url"], img_ov["api_keys"], img_ov["model"])):
            image_cfg = RoleEndpointConfig(
                role="image",
                base_url=(img_ov["base_url"] or wb_base_url) or "",
                model=img_ov["model"] or "",
                api_keys=_split_csv(img_ov["api_keys"] or default_keys_raw),
                provider_type=img_ov["provider_type"] or "auto",
            )
        raw_wb_backoff = _resolve_env_or_arg(
            getattr(args, "workbuddy_rate_limit_backoff", None),
            WORKBUDDY_RATE_LIMIT_BACKOFF_ENV,
        )
        wb_backoff = (
            parse_rate_limit_backoff_sec(raw_wb_backoff)
            if raw_wb_backoff is not None
            else None
        )
        raw_wb_cooldown = _resolve_env_or_arg(
            getattr(args, "workbuddy_model_cooldown_sec", None),
            WORKBUDDY_MODEL_COOLDOWN_ENV,
        )
        wb_model_cooldown: Optional[float] = None
        if raw_wb_cooldown is not None:
            try:
                wb_model_cooldown = float(raw_wb_cooldown)
            except ValueError as exc:
                raise LLMConfigurationError(
                    f"Invalid {WORKBUDDY_MODEL_COOLDOWN_ENV}={raw_wb_cooldown!r}: must be a positive number."
                ) from exc
        return RoleModelRouter.from_workbuddy_catalog(
            wb_catalog,
            base_url=wb_base_url or "",
            api_keys=wb_keys,
            role_model_overrides=role_model_ov,
            role_base_url_overrides=role_url_ov,
            role_api_key_overrides=role_key_ov,
            image_role=image_cfg,
            ledger=ledger,
            model_cooldown_sec=wb_model_cooldown,
            rate_limit_backoff_sec=wb_backoff,
        )

    roles_cfg: Dict[str, RoleEndpointConfig] = {}
    for role in text_roles:
        ov = role_overrides[role]
        eff_url = ov["base_url"] or default_url
        eff_keys = _split_csv(ov["api_keys"] or default_keys_raw)
        eff_model = ov["model"] or default_model
        eff_ptype = ov["provider_type"] or "openai"
        roles_cfg[role] = RoleEndpointConfig(
            role=role,
            base_url=eff_url or "",
            model=eff_model or "",
            api_keys=eff_keys,
            provider_type=eff_ptype,
        )

    img_ov = role_overrides["image"]
    if any((img_ov["base_url"], img_ov["api_keys"], img_ov["model"])):
        roles_cfg["image"] = RoleEndpointConfig(
            role="image",
            base_url=(img_ov["base_url"] or default_url) or "",
            model=img_ov["model"] or "",
            api_keys=_split_csv(img_ov["api_keys"] or default_keys_raw),
            provider_type=img_ov["provider_type"] or "auto",
        )

    return RoleModelRouter(roles_cfg, ledger=ledger)


def _resolve_export_only(args: argparse.Namespace) -> bool:
    if bool(getattr(args, "export_only", False)):
        return True
    raw = os.environ.get("XIAOSHUO_EXPORT_ONLY", "").strip()
    if raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise ValueError(f"XIAOSHUO_EXPORT_ONLY must be '0' or '1', got {raw!r}")


def build_serve_server(args: argparse.Namespace) -> MatrixHTTPServer:
    host = _resolve_env_or_arg(getattr(args, "host", None), "XIAOSHUO_HOST") or "127.0.0.1"
    raw_port = getattr(args, "port", None)
    if raw_port is None:
        env_port = os.environ.get("XIAOSHUO_PORT", "").strip()
        port = int(env_port) if env_port else 8765
    else:
        port = int(raw_port)

    projects_root = Path(
        _resolve_env_or_arg(getattr(args, "projects_root", None), "XIAOSHUO_PROJECTS_ROOT") or "projects"
    ).resolve()
    db_path = Path(
        _resolve_env_or_arg(getattr(args, "db_path", None), "XIAOSHUO_DB_PATH") or "data/matrix.db"
    ).resolve()
    admin_token = _resolve_env_or_arg(getattr(args, "admin_token", None), "XIAOSHUO_ADMIN_TOKEN")
    companion_secret = _resolve_env_or_arg(
        getattr(args, "companion_hmac_secret", None),
        "XIAOSHUO_COMPANION_HMAC_SECRET",
        "XIAOSHUO_COMPANION_SECRET",
    )

    tg_env_present = bool(
        (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        or (os.environ.get("TELEGRAM_OWNER_USER_ID") or "").strip()
        or getattr(args, "tg_bot_token", None)
        or getattr(args, "tg_owner_user_id", None)
    )
    with_tg = (
        not bool(getattr(args, "no_tg_bot", False))
        and (
            bool(getattr(args, "with_tg_bot", False))
            or (_resolve_env_or_arg(None, "XIAOSHUO_WITH_TG_BOT") or "").lower() in ("1", "true", "yes")
            or tg_env_present
        )
    )
    tg_config: Optional[TelegramBotConfig] = None
    if with_tg:
        tg_state = _resolve_env_or_arg(getattr(args, "tg_state_path", None), "TELEGRAM_STATE_PATH")
        if tg_state is None:
            tg_state = str(db_path.with_name("tg_bot_state.json"))
        tg_config = TelegramBotConfig.from_env(
            bot_token=getattr(args, "tg_bot_token", None),
            owner_user_id=getattr(args, "tg_owner_user_id", None),
            api_base_url=getattr(args, "tg_api_base_url", None),
            state_path=tg_state,
        )

    cpa_client = _build_serve_cpa_client(args)
    router = _build_serve_router(args, db_path)
    motif_path = _resolve_env_or_arg(
        getattr(args, "motif_store", None),
        "XIAOSHUO_MOTIF_STORE",
    ) or str(db_path.with_name("motif_weights.json"))
    motif_store = MotifWeightStore(motif_path)

    server = create_server(
        host=host,
        port=port,
        db_path=db_path,
        projects_root=projects_root,
        admin_token=admin_token,
        companion_secret=companion_secret,
        provider=router,
        cpa_client=cpa_client,
        motif_store=motif_store,
        static_dir=getattr(args, "static_dir", None),
        sse_heartbeat_sec=float(getattr(args, "sse_heartbeat_sec", 15.0)),
        export_only=_resolve_export_only(args),
        planner_max_consecutive_same_location=2,
    )
    if tg_config is not None:
        try:
            server.telegram_bot = TelegramControlBot(server.scheduler, config=tg_config)
        except Exception:
            server.server_close()
            server.db.close()
            raise
    return server


def _cmd_serve(args: argparse.Namespace) -> int:
    server = build_serve_server(args)
    stop_event = threading.Event()
    bound_host, bound_port = server.server_address[:2]

    def _request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    is_main_thread = threading.current_thread() is threading.main_thread()
    prev_sigint = signal.getsignal(signal.SIGINT) if is_main_thread else None
    prev_sigterm = (
        signal.getsignal(signal.SIGTERM)
        if is_main_thread and hasattr(signal, "SIGTERM")
        else None
    )
    if is_main_thread:
        signal.signal(signal.SIGINT, _request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _request_stop)

    http_thread: Optional[threading.Thread] = None
    try:
        if not bool(getattr(args, "no_scheduler_loop", False)):
            server.scheduler.start_background_loop(
                poll_interval_sec=float(getattr(args, "scheduler_interval", 10.0))
            )
        http_thread = threading.Thread(
            target=server.serve_forever,
            name="xiaoshuo-http-server",
            daemon=True,
        )
        http_thread.start()
        tg_flag = "enabled" if server.telegram_bot is not None else "disabled"
        cpa_flag = "configured" if server.cpa_client is not None else "unconfigured"
        print(
            f"[xiaoshuo-bot] Cloud matrix server listening on http://{bound_host}:{bound_port} "
            f"(projects={server.scheduler.projects_root}, tg_bot={tg_flag}, cpa={cpa_flag})",
            flush=True,
        )
        while not stop_event.is_set() and http_thread.is_alive():
            stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if is_main_thread and prev_sigint is not None:
            signal.signal(signal.SIGINT, prev_sigint)
        if is_main_thread and prev_sigterm is not None and hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, prev_sigterm)
        if http_thread is not None and http_thread.is_alive():
            server.shutdown_and_close()
            http_thread.join(timeout=5.0)
        else:
            server.stop_event.set()
            server.stop_telegram_bot()
            server.scheduler.stop_background_loop()
            server.server_close()
        server.db.close()
    return 0


def _build_non_chapter_lease(
    task_dict: Mapping[str, Any],
    *,
    account_id: str,
    default_author_name: str,
) -> CompanionLeaseResponse:
    known_top = {
        "task_id",
        "task_type",
        "book_id",
        "book_title",
        "author_name",
        "fanqie_book_id",
        "chapter",
        "public_title",
        "body",
        "author_note",
        "sha256",
        "status",
        "lease_expires_at",
        "scheduled_publish_at",
        "metadata",
    }
    merged_meta: Dict[str, Any] = {}
    if isinstance(task_dict.get("metadata"), Mapping):
        merged_meta.update(dict(task_dict["metadata"]))
    for k, v in task_dict.items():
        if k not in known_top:
            merged_meta[k] = v

    return CompanionLeaseResponse(
        task_id=str(task_dict["task_id"]),
        task_type=str(task_dict["task_type"]),
        book_id=str(task_dict["book_id"]),
        book_title=str(task_dict.get("book_title") or task_dict.get("old_title") or ""),
        author_name=str(task_dict.get("author_name") or default_author_name or ""),
        fanqie_book_id=str(task_dict.get("fanqie_book_id") or ""),
        chapter=int(task_dict.get("chapter") or 0),
        public_title=str(task_dict.get("public_title") or task_dict.get("new_title") or ""),
        body=str(task_dict.get("body") or ""),
        author_note=str(task_dict.get("author_note") or ""),
        sha256=str(task_dict.get("sha256") or ""),
        status=StockpileStatus.LEASED.value,
        scheduled_publish_at=str(task_dict.get("scheduled_publish_at") or ""),
        metadata=merged_meta,
    )


def _select_companion_target_url(
    task_type: str,
    task_metadata: Mapping[str, Any],
    *,
    default_url: str,
    metrics_url: Optional[str] = None,
    star_cards_url: Optional[str] = None,
    signing_url: Optional[str] = None,
    rotate_url: Optional[str] = None,
) -> str:
    meta_url = str(task_metadata.get("target_url") or "").strip()
    if meta_url:
        return meta_url
    if task_type == CompanionTaskType.COLLECT_METRICS.value and metrics_url:
        return metrics_url
    if task_type == CompanionTaskType.CREATE_STAR_CARDS.value and star_cards_url:
        return star_cards_url
    if task_type == CompanionTaskType.SUBMIT_SIGNING_DRAFT.value and signing_url:
        return signing_url
    if task_type == CompanionTaskType.ROTATE_PACKAGING.value and rotate_url:
        return rotate_url
    return default_url


def run_companion_daemon(
    args: argparse.Namespace,
    *,
    stop_event: Optional[threading.Event] = None,
) -> int:
    import logging
    import random

    from .companion import (
        AccountBookMismatchError,
        BrowserLaunchConfig,
        CaptchaAuthPauseError,
        CompanionConfigError,
        CompanionHttpClient,
        TomatoBrowserWorker,
        Woff2FontDecoder,
    )
    from .contracts import CompanionReceiptRequest

    daemon_log = logging.getLogger("xiaoshuo_bot.companion.daemon")

    server_url = _resolve_env_or_arg(getattr(args, "server_url", None), "XIAOSHUO_SERVER_URL")
    if not server_url:
        raise CompanionConfigError("Missing --server-url (or XIAOSHUO_SERVER_URL)")

    hmac_secret = _resolve_env_or_arg(
        getattr(args, "hmac_secret", None),
        "XIAOSHUO_COMPANION_HMAC_SECRET",
        "XIAOSHUO_COMPANION_SECRET",
    )
    if not hmac_secret:
        raise CompanionConfigError(
            "Missing --hmac-secret (or XIAOSHUO_COMPANION_HMAC_SECRET / XIAOSHUO_COMPANION_SECRET)"
        )

    account_id = _resolve_env_or_arg(
        getattr(args, "account_id", None),
        "XIAOSHUO_COMPANION_ACCOUNT_ID",
    )
    if not account_id:
        raise CompanionConfigError("Missing --account-id (or XIAOSHUO_COMPANION_ACCOUNT_ID)")

    target_url = _resolve_env_or_arg(
        getattr(args, "target_url", None),
        "XIAOSHUO_COMPANION_TARGET_URL",
    )
    if not target_url:
        raise CompanionConfigError("Missing --target-url (or XIAOSHUO_COMPANION_TARGET_URL)")

    author_name = _resolve_env_or_arg(
        getattr(args, "author_name", None),
        "XIAOSHUO_COMPANION_AUTHOR_NAME",
    ) or ""
    book_ids = _split_csv(
        _resolve_env_or_arg(getattr(args, "book_ids", None), "XIAOSHUO_COMPANION_BOOK_IDS")
    )
    profile_root = Path(
        _resolve_env_or_arg(getattr(args, "profile_root", None), "XIAOSHUO_COMPANION_PROFILE_ROOT")
        or "chrome_profiles"
    ).resolve()
    outbox_db = _resolve_env_or_arg(
        getattr(args, "outbox_db", None),
        "XIAOSHUO_COMPANION_OUTBOX_DB",
    ) or str(profile_root / f"{account_id}_outbox.db")

    raw_channel = getattr(args, "chrome_channel", "chrome")
    if raw_channel is None or str(raw_channel).strip().lower() in ("", "none"):
        chrome_channel: Optional[str] = None
    else:
        chrome_channel = str(raw_channel).strip()

    headless = bool(getattr(args, "headless", False))
    once = bool(getattr(args, "once", False))
    poll_interval = max(0.1, float(getattr(args, "poll_interval", 2.0)))
    poll_timeout = max(0, min(25, int(getattr(args, "poll_timeout", 25))))
    review_pause_sec = float(getattr(args, "review_pause_sec", 0.8))
    woff2_cache_dir = getattr(args, "woff2_cache_dir", None)
    screenshot_dir = getattr(args, "screenshot_dir", None)
    client_id = getattr(args, "client_id", None) or f"companion-{account_id}"
    cover_root_raw = _resolve_env_or_arg(
        getattr(args, "cover_root", None),
        "XIAOSHUO_COMPANION_COVER_ROOT",
    )

    metrics_url = getattr(args, "metrics_url", None)
    star_cards_url = getattr(args, "star_cards_url", None)
    signing_url = getattr(args, "signing_url", None)
    rotate_url = getattr(args, "rotate_url", None)

    decoder = Woff2FontDecoder(cache_dir=Path(woff2_cache_dir).resolve() if woff2_cache_dir else None)
    launch_cfg = BrowserLaunchConfig(
        profile_root=profile_root,
        channel=chrome_channel,
        headless=headless,
        review_pause_sec=review_pause_sec,
        screenshot_dir=Path(screenshot_dir).resolve() if screenshot_dir else None,
        cover_root=Path(cover_root_raw).resolve() if cover_root_raw else None,
    )

    client = CompanionHttpClient(
        base_url=server_url,
        hmac_secret=hmac_secret,
        client_id=client_id,
        outbox_db_path=outbox_db,
        poll_timeout_sec=poll_timeout,
    )
    signal_stop = stop_event or threading.Event()
    try:
        with TomatoBrowserWorker(
            account_id,
            launch_cfg,
            client=client,
            font_decoder=decoder,
        ) as worker:
            backoff_base_sec = 2.0
            backoff_max_sec = 300.0
            backoff_sec = 0.0

            def _backoff_wait(stage: str, exc: Exception) -> None:
                nonlocal backoff_sec
                backoff_sec = backoff_base_sec if backoff_sec <= 0 else min(backoff_max_sec, backoff_sec * 2)
                delay = backoff_sec * random.uniform(0.5, 1.0)
                daemon_log.warning(
                    "Companion %s failed (%s: %s); retrying in %.1fs", stage, type(exc).__name__, exc, delay
                )
                print(
                    json.dumps(
                        {"event": "companion_backoff", "stage": stage, "error": str(exc), "retry_in_sec": round(delay, 2)},
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                signal_stop.wait(timeout=delay)

            while not signal_stop.is_set():
                try:
                    client.flush_outbox()
                    tasks = client.poll_tasks(
                        account_id,
                        author_name=author_name,
                        book_ids=book_ids or None,
                        timeout_sec=0 if once else poll_timeout,
                    )
                except Exception as exc:
                    if once:
                        raise
                    _backoff_wait("poll", exc)
                    continue
                backoff_sec = 0.0

                for task_dict in tasks:
                    if signal_stop.is_set():
                        break
                    task_type = str(task_dict.get("task_type") or CompanionTaskType.PUBLISH_CHAPTER.value)
                    task_id = str(task_dict.get("task_id") or "")
                    book_id = str(task_dict.get("book_id") or "")
                    try:
                        chapter = int(task_dict.get("chapter") or 0)
                    except (TypeError, ValueError):
                        chapter = 0

                    try:
                        if task_type == CompanionTaskType.PUBLISH_CHAPTER.value:
                            lease = client.lease_task(
                                CompanionLeaseRequest(
                                    task_id=task_id,
                                    book_id=book_id,
                                    chapter=chapter,
                                    account_id=account_id,
                                )
                            )
                        else:
                            lease = _build_non_chapter_lease(
                                task_dict,
                                account_id=account_id,
                                default_author_name=author_name,
                            )
                    except Exception as exc:
                        if once:
                            raise
                        _backoff_wait(f"lease:{task_id}", exc)
                        continue
                    backoff_sec = 0.0

                    try:
                        eff_url = _select_companion_target_url(
                            lease.task_type,
                            lease.metadata,
                            default_url=target_url,
                            metrics_url=metrics_url,
                            star_cards_url=star_cards_url,
                            signing_url=signing_url,
                            rotate_url=rotate_url,
                        )
                        outcome = worker.execute_task(lease, target_url=eff_url)
                        print(
                            json.dumps(outcome.to_dict(), ensure_ascii=False),
                            flush=True,
                        )
                    except (KeyboardInterrupt, CaptchaAuthPauseError):
                        raise
                    except Exception as exc:
                        daemon_log.exception(
                            "Companion task %s (%s, book=%s, ch=%s) failed", lease.task_id, lease.task_type, lease.book_id, lease.chapter
                        )
                        receipt_sent = False
                        # AccountBookMismatchError already emitted FAILED_HARD_MISMATCH inside execute_task.
                        if not isinstance(exc, AccountBookMismatchError) and lease.task_id:
                            try:
                                client.send_receipt(
                                    CompanionReceiptRequest(
                                        task_id=lease.task_id,
                                        book_id=lease.book_id,
                                        chapter=lease.chapter,
                                        account_id=account_id,
                                        status=StockpileStatus.FAILED_HARD_MISMATCH.value,
                                        error_code="COMPANION_TASK_ERROR",
                                        error_message=f"{type(exc).__name__}: {exc}"[:2000],
                                        sha256=lease.sha256,
                                    )
                                )
                                receipt_sent = True
                            except Exception as send_exc:
                                daemon_log.error(
                                    "Failed to send failure receipt for task %s: %s", lease.task_id, send_exc
                                )
                        print(
                            json.dumps(
                                {
                                    "task_id": lease.task_id,
                                    "task_type": lease.task_type,
                                    "book_id": lease.book_id,
                                    "account_id": account_id,
                                    "status": "ERROR",
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "failure_receipt_sent": receipt_sent,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )

                if once:
                    break
                if not tasks:
                    signal_stop.wait(timeout=poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
    return 0


def _cmd_companion(args: argparse.Namespace) -> int:
    return run_companion_daemon(args)


_parse_chapters_arg = parse_chapters_arg


def _build_pilot_workbuddy_catalog(args: argparse.Namespace) -> WorkBuddyModelCatalog:
    wb_catalog = WorkBuddyModelCatalog.from_env(
        overrides={
            WORKBUDDY_SLOT_HY4_FLASH: getattr(args, "workbuddy_hy4_flash", None),
            WORKBUDDY_SLOT_HY4: getattr(args, "workbuddy_hy4", None),
            WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH: getattr(args, "workbuddy_deepseek_v4_1_flash", None),
            WORKBUDDY_SLOT_MIMO_V2_6_PRO: getattr(args, "workbuddy_mimo_v2_6_pro", None),
        }
    )
    if wb_catalog is None:
        raise LLMConfigurationError(
            "pilot requires WorkBuddy model catalog configuration "
            "(set WORKBUDDY_MODEL_* environment variables or --workbuddy-* flags)."
        )
    return wb_catalog


def _build_workbuddy_slot_provider(
    args: argparse.Namespace,
    slot_name: str,
    *,
    role: str,
    catalog: Optional[WorkBuddyModelCatalog] = None,
    ledger: Optional[UsageBudgetLedger] = None,
) -> tuple[LLMProvider, str]:
    wb_catalog = catalog if catalog is not None else _build_pilot_workbuddy_catalog(args)
    slot_map = wb_catalog.slot_to_model()
    cleaned_slot = str(slot_name or "").strip()
    if cleaned_slot not in slot_map:
        raise LLMConfigurationError(
            f"Unknown WorkBuddy slot {slot_name!r}; expected one of {', '.join(slot_map.keys())}."
        )
    upstream_model = slot_map[cleaned_slot]

    default_url = _resolve_env_or_arg(
        getattr(args, "base_url", None),
        "XIAOSHUO_BASE_URL",
        "OPENAI_BASE_URL",
    )
    wb_base_url = default_url or _resolve_env_or_arg(
        getattr(args, "cpa_url", None),
        "CPA_BASE_URL",
        "XIAOSHUO_CPA_URL",
        "CPA_MANAGEMENT_URL",
    )
    default_keys_raw = _resolve_env_or_arg(
        getattr(args, "api_key", None),
        "XIAOSHUO_API_KEY",
        "OPENAI_API_KEY",
    )
    wb_keys = _split_csv(default_keys_raw)

    raw_wb_backoff = _resolve_env_or_arg(
        getattr(args, "workbuddy_rate_limit_backoff", None),
        WORKBUDDY_RATE_LIMIT_BACKOFF_ENV,
    )
    wb_backoff = (
        parse_rate_limit_backoff_sec(raw_wb_backoff)
        if raw_wb_backoff is not None
        else None
    )
    raw_wb_cooldown = _resolve_env_or_arg(
        getattr(args, "workbuddy_model_cooldown_sec", None),
        WORKBUDDY_MODEL_COOLDOWN_ENV,
    )
    wb_model_cooldown: Optional[float] = None
    if raw_wb_cooldown is not None:
        try:
            wb_model_cooldown = float(raw_wb_cooldown)
        except ValueError as exc:
            raise LLMConfigurationError(
                f"Invalid {WORKBUDDY_MODEL_COOLDOWN_ENV}={raw_wb_cooldown!r}: must be a positive number."
            ) from exc

    endpoint = RoleEndpointConfig(
        role=role,
        base_url=wb_base_url or "",
        model=upstream_model,
        api_keys=wb_keys,
        provider_type="openai",
        model_whitelist=wb_catalog.whitelist(),
        rotation_models=(),
        manual_only_models=wb_catalog.manual_only(),
        model_cooldown_sec=wb_model_cooldown,
        rate_limit_backoff_sec=wb_backoff,
        model_labels=wb_catalog.labels(),
    )
    router = RoleModelRouter(
        {role: endpoint},
        ledger=ledger,
        workbuddy_catalog=wb_catalog,
    )
    return router.as_provider(role), upstream_model


def _cmd_pilot(args: argparse.Namespace) -> int:
    chapters = _parse_chapters_arg(args.chapters)
    case_summaries = load_case_summaries(args.case_summaries)
    wb_catalog = _build_pilot_workbuddy_catalog(args)

    out_dir = Path(args.out).resolve()
    budget_raw = getattr(args, "daily_budget_limit", None)
    if budget_raw is None:
        env_budget = os.environ.get("XIAOSHUO_DAILY_BUDGET_LIMIT", "").strip()
        budget_limit = float(env_budget) if env_budget else None
    else:
        budget_limit = float(budget_raw)

    chapter_raw = getattr(args, "daily_chapter_limit", None)
    if chapter_raw is None:
        env_chap = os.environ.get("XIAOSHUO_DAILY_CHAPTER_LIMIT", "").strip()
        chapter_limit = int(env_chap) if env_chap else None
    else:
        chapter_limit = int(chapter_raw)

    ledger_path = _resolve_env_or_arg(
        getattr(args, "ledger_path", None),
        "XIAOSHUO_LEDGER_PATH",
    ) or str(out_dir / "pilot_usage_ledger.json")
    ledger = UsageBudgetLedger(
        daily_budget_limit=budget_limit,
        daily_chapter_limit=chapter_limit,
        state_path=ledger_path,
    )

    judge_slot = str(args.judge or "").strip()
    judge_provider, judge_upstream = _build_workbuddy_slot_provider(
        args,
        judge_slot,
        role="critic",
        catalog=wb_catalog,
        ledger=ledger,
    )

    writers: Dict[str, LLMProvider] = {}
    for raw_slot in args.writer:
        slot = str(raw_slot or "").strip()
        if slot in writers:
            raise LLMConfigurationError(f"Duplicate --writer slot {slot!r}.")
        writer_provider, _ = _build_workbuddy_slot_provider(
            args,
            slot,
            role="writer",
            catalog=wb_catalog,
            ledger=ledger,
        )
        _check_same_or_same_family_models(judge_slot, slot)
        writers[slot] = writer_provider

    payload = run_pilot(
        project_dir=args.project,
        chapters=chapters,
        writers=writers,
        judge_provider=judge_provider,
        judge_model=judge_upstream,
        benchmark_path=args.benchmark_text,
        case_summaries=case_summaries,
        out_dir=out_dir,
        blueprint_path=getattr(args, "blueprint", None),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _resolve_volume_writer_model(
    chapters_dir: Path,
    chapter_end: int,
    explicit_writer_model: Optional[str] = None,
) -> str:
    if explicit_writer_model is not None and str(explicit_writer_model).strip():
        return str(explicit_writer_model).strip()
    meta_path = chapters_dir / f"chapter_{chapter_end:04d}.meta.json"
    if not meta_path.is_file():
        raise LLMConfigurationError(
            f"Missing --writer-model and end-of-volume chapter metadata file '{meta_path}' "
            f"does not exist; refusing to guess writer_model."
        )
    try:
        meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LLMConfigurationError(
            f"Failed to read writer_model from '{meta_path}': {exc}"
        ) from exc
    if not isinstance(meta_data, Mapping):
        raise LLMConfigurationError(
            f"Invalid chapter metadata JSON at '{meta_path}': expected a JSON object."
        )
    candidate = str(meta_data.get("writer_model") or meta_data.get("model") or "").strip()
    if not candidate or candidate.lower() == "checkpoint":
        raise LLMConfigurationError(
            f"Missing writer_model/model in '{meta_path}' and --writer-model was not provided; "
            f"refusing to guess writer_model."
        )
    return candidate


def execute_volume_check(
    *,
    project_dir: str | Path,
    output_dir: str | Path,
    volume: int,
    blueprint_path: Optional[str | Path] = None,
    writer_model: Optional[str] = None,
    judge: Optional[PairwiseJudge] = None,
    judge_provider: Optional[LLMProvider] = None,
    judge_model: Optional[str] = None,
    core_selling_point_keywords: Optional[Sequence[str]] = None,
) -> VolumeHealthReport:
    """Strictly validate volume artifacts and execute the 6-point end-of-volume health check."""
    if isinstance(volume, bool) or not isinstance(volume, int) or volume < 1:
        raise ValueError(f"volume must be an int >= 1, got {volume!r}.")

    proj_path = Path(project_dir).resolve()
    out_path = Path(output_dir).resolve()
    if not out_path.is_dir():
        raise FileNotFoundError(f"Output directory does not exist: '{out_path}'.")

    bp_path = (
        Path(blueprint_path).resolve()
        if blueprint_path is not None and str(blueprint_path).strip()
        else (proj_path / "blueprint.yaml")
    )
    if not bp_path.is_file():
        raise FileNotFoundError(f"Blueprint file does not exist: '{bp_path}'.")

    project = load_project(proj_path)
    blueprint = load_blueprint(bp_path)

    vol_spec = next((v for v in blueprint.volumes if v.index == volume), None)
    if vol_spec is None:
        available = [v.index for v in blueprint.volumes]
        raise ValueError(
            f"Volume {volume} not found in blueprint '{bp_path}' (available volumes: {available})."
        )
    prev_vol = (
        next((v for v in blueprint.volumes if v.index == volume - 1), None)
        if volume > 1
        else None
    )

    chapters_dir = out_path / "chapters"
    snapshots_dir = out_path / "snapshots"
    audit_dir = out_path / "audit"
    if not chapters_dir.is_dir():
        raise FileNotFoundError(f"Chapters directory does not exist: '{chapters_dir}'.")
    if not snapshots_dir.is_dir():
        raise FileNotFoundError(f"Snapshots directory does not exist: '{snapshots_dir}'.")

    missing_chapters: List[int] = []
    vol_chapters: List[Tuple[int, str]] = []
    for ch_no in range(vol_spec.chapter_start, vol_spec.chapter_end + 1):
        ch_path = resolve_chapter_path(chapters_dir, ch_no)
        if ch_path is None or not ch_path.is_file():
            missing_chapters.append(ch_no)
            continue
        text = ch_path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"Chapter {ch_no:04d} file is empty: '{ch_path}'.")
        vol_chapters.append((ch_no, text))
    if missing_chapters:
        raise FileNotFoundError(
            f"Volume {volume} is incomplete: missing chapter file(s) {missing_chapters} "
            f"in '{chapters_dir}' (required range {vol_spec.chapter_start}..{vol_spec.chapter_end})."
        )

    snap_path = snapshots_dir / f"chapter_{vol_spec.chapter_end:04d}_after.json"
    if not snap_path.is_file():
        raise FileNotFoundError(
            f"Missing end-of-volume world-state snapshot for volume {volume}: '{snap_path}'."
        )
    try:
        world_state_snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Invalid world-state snapshot JSON at '{snap_path}': {exc}"
        ) from exc
    if not isinstance(world_state_snapshot, Mapping):
        raise ValueError(
            f"World-state snapshot at '{snap_path}' must be a JSON object, "
            f"got {type(world_state_snapshot).__name__}."
        )

    prior_chapters: List[Tuple[int, str]] = []
    if prev_vol is not None:
        for ch_no in range(prev_vol.chapter_start, prev_vol.chapter_end + 1):
            prev_path = resolve_chapter_path(chapters_dir, ch_no)
            if prev_path is not None and prev_path.is_file():
                prev_text = prev_path.read_text(encoding="utf-8").strip()
                if prev_text:
                    prior_chapters.append((ch_no, prev_text))

    if judge is not None:
        if not isinstance(judge, PairwiseJudge):
            raise ValueError(
                f"judge must be a PairwiseJudge instance, got {type(judge).__name__}."
            )
        effective_judge = judge
    else:
        resolved_writer_model = _resolve_volume_writer_model(
            chapters_dir,
            vol_spec.chapter_end,
            explicit_writer_model=writer_model,
        )
        if judge_provider is None:
            raise LLMConfigurationError(
                "Either judge (PairwiseJudge) or judge_provider + judge_model must be provided."
            )
        resolved_judge_model = str(
            judge_model or getattr(judge_provider, "model", "") or ""
        ).strip()
        if not resolved_judge_model:
            raise LLMConfigurationError(
                "Missing judge_model for volume-check; specify --model, --judge-slot, "
                "or configure the WorkBuddy model catalog."
            )
        effective_judge = PairwiseJudge(
            judge_provider,
            judge_model=resolved_judge_model,
            writer_model=resolved_writer_model,
        )

    return run_volume_health_check(
        volume_spec=vol_spec,
        chapters=vol_chapters,
        world_state_snapshot=world_state_snapshot,
        project=project,
        blueprint=blueprint,
        judge=effective_judge,
        audit_dir=audit_dir,
        prev_volume_spec=prev_vol,
        core_selling_point_keywords=core_selling_point_keywords,
        prior_volume_chapters=prior_chapters or None,
    )


def _format_volume_health_table(report: VolumeHealthReport) -> str:
    lines: List[str] = [
        f"=== 卷末六项体检报告 · 第 {report.volume_index} 卷《{report.volume_title}》"
        f"（第 {report.chapter_start}-{report.chapter_end} 章） ===",
        f"结论: {report.verdict} | 锁卷状态: {report.lock_state}",
        f"裁判模型: {report.judge_model} | 写作模型: {report.writer_model}",
        "-" * 96,
        f"{'体检项 ID':<28} | {'结论':<4} | {'涉及章号':<14} | 返修建议",
        "-" * 96,
    ]
    for item in report.checks:
        chs_str = ",".join(str(c) for c in item.chapters) if item.chapters else "-"
        hint_str = item.repair_hint.strip() if item.repair_hint else "-"
        lines.append(
            f"{item.id:<28} | {item.status:<4} | {chs_str:<14} | {hint_str}"
        )
    lines.extend(
        [
            "-" * 96,
            f"总结: {report.summary}",
            f"锁卷状态: {report.lock_state}",
            f"体检报告: {report.report_file}",
            f"锁表文件: {report.lock_file}",
        ]
    )
    return "\n".join(lines)


def _cmd_volume_check(args: argparse.Namespace) -> int:
    if isinstance(args.volume, bool) or not isinstance(args.volume, int) or args.volume < 1:
        raise ValueError(f"--volume must be an int >= 1, got {args.volume!r}.")

    raw_core = getattr(args, "core_keywords", None)
    core_keywords: Optional[List[str]] = None
    if raw_core is not None:
        parsed_kw = _split_csv(raw_core)
        if not parsed_kw:
            raise ValueError("--core-keywords must contain at least one non-empty keyword.")
        core_keywords = parsed_kw

    wb_catalog = WorkBuddyModelCatalog.from_env(
        overrides={
            WORKBUDDY_SLOT_HY4_FLASH: getattr(args, "workbuddy_hy4_flash", None),
            WORKBUDDY_SLOT_HY4: getattr(args, "workbuddy_hy4", None),
            WORKBUDDY_SLOT_DEEPSEEK_V4_1_FLASH: getattr(args, "workbuddy_deepseek_v4_1_flash", None),
            WORKBUDDY_SLOT_MIMO_V2_6_PRO: getattr(args, "workbuddy_mimo_v2_6_pro", None),
        }
    )

    judge_slot = str(getattr(args, "judge_slot", None) or "").strip()
    explicit_model = str(getattr(args, "model", None) or "").strip()
    provider_name = str(getattr(args, "provider", "openai") or "openai").strip().lower()

    out_dir = Path(args.output_dir).resolve()
    judge_provider: Optional[LLMProvider] = None
    judge_model: Optional[str] = None

    def _build_vc_ledger() -> UsageBudgetLedger:
        budget_raw = getattr(args, "daily_budget_limit", None)
        if budget_raw is None:
            env_budget = os.environ.get("XIAOSHUO_DAILY_BUDGET_LIMIT", "").strip()
            budget_limit = float(env_budget) if env_budget else None
        else:
            budget_limit = float(budget_raw)
        chapter_raw = getattr(args, "daily_chapter_limit", None)
        if chapter_raw is None:
            env_chap = os.environ.get("XIAOSHUO_DAILY_CHAPTER_LIMIT", "").strip()
            chapter_limit = int(env_chap) if env_chap else None
        else:
            chapter_limit = int(chapter_raw)
        ledger_path = _resolve_env_or_arg(
            getattr(args, "ledger_path", None),
            "XIAOSHUO_LEDGER_PATH",
        ) or str(out_dir / "audit" / "volume_check_usage_ledger.json")
        return UsageBudgetLedger(
            daily_budget_limit=budget_limit,
            daily_chapter_limit=chapter_limit,
            state_path=ledger_path,
        )

    if judge_slot:
        if wb_catalog is None:
            raise LLMConfigurationError(
                "--judge-slot requires WorkBuddy model catalog configuration "
                "(set WORKBUDDY_MODEL_* environment variables or --workbuddy-* flags)."
            )
        judge_provider, judge_model = _build_workbuddy_slot_provider(
            args,
            judge_slot,
            role="critic",
            catalog=wb_catalog,
            ledger=_build_vc_ledger(),
        )
    elif wb_catalog is not None and not explicit_model and provider_name == "openai":
        strong_model = wb_catalog.strong_planner_model()
        slot_map = wb_catalog.slot_to_model()
        strong_slot = next(
            (slot for slot, model_id in slot_map.items() if model_id == strong_model),
            WORKBUDDY_SLOT_MIMO_V2_6_PRO,
        )
        judge_provider, judge_model = _build_workbuddy_slot_provider(
            args,
            strong_slot,
            role="critic",
            catalog=wb_catalog,
            ledger=_build_vc_ledger(),
        )
    else:
        judge_provider = _build_text_provider(args)
        vendor_prefix = "ANTHROPIC" if provider_name == "anthropic" else "OPENAI"
        judge_model = (
            _resolve_env_or_arg(
                getattr(args, "model", None),
                "XIAOSHUO_MODEL",
                f"{vendor_prefix}_MODEL",
            )
            or str(getattr(judge_provider, "model", "") or "").strip()
            or None
        )

    try:
        report = execute_volume_check(
            project_dir=args.project,
            output_dir=out_dir,
            volume=args.volume,
            blueprint_path=getattr(args, "blueprint", None),
            writer_model=getattr(args, "writer_model", None),
            judge_provider=judge_provider,
            judge_model=judge_model,
            core_selling_point_keywords=core_keywords,
        )
    finally:
        if judge_provider is not None and hasattr(judge_provider, "close"):
            judge_provider.close()

    if bool(getattr(args, "json", False)):
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_format_volume_health_table(report))
    return 0 if report.verdict == "PASS" else 2


def _add_provider_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--provider",
        choices=["openai", "anthropic", "bridge"],
        default="openai",
        help="LLM backend provider",
    )
    p.add_argument("--api-key", help="API key override")
    p.add_argument("--base-url", help="Base URL override")
    p.add_argument("--model", help="Model name override")
    p.add_argument("--bridge-dir", help="IPC directory when --provider=bridge")
    p.add_argument("--bridge-timeout", type=int, default=900, help="Per-request bridge timeout in seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xiaoshuo-bot",
        description="Asset-Driven Long-Form Chinese Novel Generation, Matrix Engine & AI-TIC-CHECK Pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="Validate project YAML schemas & cross-references")
    p_val.add_argument("--project", required=True, help="Path to novel project directory")
    p_val.set_defaults(func=_cmd_validate)

    p_comp = sub.add_parser("compile-prompt", help="Compile and print the dynamic prompt for a chapter")
    p_comp.add_argument("--project", required=True, help="Path to novel project directory")
    p_comp.add_argument("--chapter", type=int, required=True, help="Chapter number")
    p_comp.add_argument(
        "--output-dir",
        help="Output directory containing chapters/ (required when --chapter > 1)",
    )
    p_comp.set_defaults(func=_cmd_compile_prompt)

    for chk_name in ("check-text", "check"):
        p_chk = sub.add_parser(chk_name, help="Run AI-TIC-CHECK static scanner on a text file")
        p_chk.add_argument("--file", required=True, help="Path to chapter text file")
        p_chk.add_argument("--project", help="Optional path to novel project directory for POV/foreshadow checks")
        p_chk.add_argument("--chapter", type=int, help="Optional chapter number")
        p_chk.set_defaults(func=_cmd_check_text)

    p_fault = sub.add_parser(
        "smoke-fault",
        help="Run fault-injection smoke test verifying ProductionIncident -> Auto-Rewrite pipeline",
    )
    p_fault.add_argument("--project", required=True, help="Path to novel project directory")
    p_fault.add_argument("--output-dir", help="Optional output directory for fault audit logs")
    p_fault.set_defaults(func=_cmd_smoke_fault)

    for run_name in ("run", "generate", "resume"):
        p_run = sub.add_parser(run_name, help="Run end-to-end multi-chapter novel generation pipeline")
        p_run.add_argument("--project", required=True, help="Path to novel project directory")
        _add_provider_args(p_run)
        p_run.add_argument("--output-dir", help="Output directory (defaults to <project>/output)")
        p_run.add_argument("--chapters", help="Optional comma-separated chapter numbers, e.g. 1,2,3")
        p_run.add_argument("--resume", action="store_true", help="Resume from verified chapter checkpoints")
        p_run.add_argument("--unlock", action="store_true", help="Explicitly unlock & overwrite locked chapters")
        p_run.set_defaults(func=_cmd_run)

    p_genres = sub.add_parser("genres", help="List or inspect registered 10+1 Tomato genre recipes")
    p_genres.add_argument("--channel", choices=["male", "female", "general"], help="Filter by channel")
    p_genres.add_argument("--id", help="Show full details & common-sense card for one genre_id")
    p_genres.add_argument("--json", action="store_true", help="Output JSON")
    p_genres.set_defaults(func=_cmd_genres)

    p_auto = sub.add_parser("auto-create", help="Auto-plan a new validated novel project from 3 sources")
    p_auto.add_argument("--project-id", required=True, help="Project ID slug")
    p_auto.add_argument("--genre", required=True, help="Registered genre_id")
    p_auto.add_argument("--output-dir", required=True, help="Target project directory")
    p_auto.add_argument("--author-level", choices=["lv0_1", "lv2_plus"], default="lv0_1")
    p_auto.add_argument("--pen-name", default="砚山行", help="Author pen name")
    p_auto.add_argument("--projects-root", help="Optional projects root for cross-book collision checks")
    p_auto.add_argument("--hot-keywords", help="Comma-separated hot keywords")
    p_auto.add_argument("--benchmark-file", action="append", help="Benchmark sample chapter file (repeatable)")
    p_auto.add_argument("--benchmark-dir", help="Directory containing benchmark sample .txt files")
    p_auto.add_argument("--target-chapters", type=int, help="Target chapter count for lv0_1 (70..95)")
    p_auto.add_argument("--motif-store", help="Path to MotifWeightStore JSON")
    _add_provider_args(p_auto)
    p_auto.set_defaults(func=_cmd_auto_create)

    p_dec = sub.add_parser("deconstruct", help="Deconstruct benchmark chapters into a sanitized 5D blueprint")
    p_dec.add_argument("--benchmark-file", action="append", help="Benchmark sample chapter file (repeatable)")
    p_dec.add_argument("--benchmark-dir", help="Directory containing benchmark sample .txt files")
    p_dec.add_argument("--target-genre", required=True, help="Target genre_id")
    p_dec.add_argument("--source-domain", help="Optional source domain hint")
    p_dec.add_argument("--target-domain", help="Optional target domain hint")
    p_dec.add_argument("--hot-keywords", help="Comma-separated hot keywords")
    p_dec.add_argument("--output", help="Optional output JSON file path")
    _add_provider_args(p_dec)
    p_dec.set_defaults(func=_cmd_deconstruct)

    p_ext = sub.add_parser("extend", help="Extend volume outline (with 60-80ch map change) or plan finale")
    p_ext.add_argument("--project", required=True, help="Path to novel project directory")
    p_ext.add_argument("--snapshot", help="Optional path to WorldStateSnapshot JSON")
    p_ext.add_argument("--batch-size", type=int, default=15, help="Chapters per extension batch")
    p_ext.add_argument("--map-change-interval", type=int, default=60, help="Chapters before map-change reboot")
    p_ext.add_argument("--finale", action="store_true", help="Plan final volume resolving all foreshadows")
    p_ext.add_argument("--num-chapters", type=int, default=8, help="Chapters for --finale volume")
    _add_provider_args(p_ext)
    p_ext.set_defaults(func=_cmd_extend)

    p_cov = sub.add_parser("cover", help="Generate 600x800 compliant book cover with bundled SIL OFL font")
    p_cov.add_argument("--project", required=True, help="Path to novel project directory")
    p_cov.add_argument("--output", required=True, help="Output PNG or JPEG file path")
    p_cov.add_argument("--pen-name", help="Author pen name override (uses project.yaml pen_name/author_name if omitted)")
    p_cov.add_argument("--base-mode", choices=["ai_image", "procedural"], default="ai_image")
    p_cov.add_argument("--format", choices=["PNG", "JPEG"], default="PNG")
    p_cov.add_argument("--api-key", help="Image API key override")
    p_cov.add_argument("--base-url", help="Image endpoint base URL override")
    p_cov.add_argument("--model", help="Image model override")
    p_cov.add_argument(
        "--provider-type",
        choices=["auto", "gemini_native", "openai_images"],
        default="auto",
    )
    p_cov.set_defaults(func=_cmd_cover)

    p_exp = sub.add_parser("export", help="Export Tomato, Qimao, Zhihu Yanxuan, or English Webnovel bundle")
    p_exp.add_argument("--project", required=True, help="Path to novel project directory")
    p_exp.add_argument(
        "--platform",
        choices=["tomato", "qimao", "zhihu", "webnovel"],
        default="tomato",
    )
    p_exp.add_argument("--output-dir", required=True, help="Export destination directory")
    p_exp.add_argument("--chapters-dir", help="Chapters directory (defaults to <project>/output/chapters)")
    p_exp.add_argument("--cover", help="Optional cover image path for Tomato bundle")
    p_exp.add_argument("--title-en", help="Optional English title for Webnovel export")
    p_exp.add_argument("--auto-normalize", action="store_true", help="Auto-normalize Webnovel units/titles")
    _add_provider_args(p_exp)
    p_exp.set_defaults(func=_cmd_export)

    p_bak = sub.add_parser("backup", help="Portable site-wide backup export/verify/import and Git incremental backup")
    bak_sub = p_bak.add_subparsers(dest="backup_action", required=True)

    p_bak_exp = bak_sub.add_parser("export", help="Export sanitized portable ZIP backup with SHA256 manifest")
    p_bak_exp.add_argument("--output", required=True, help="Destination .zip path")
    p_bak_exp.add_argument("--projects-dir", help="Projects directory to include")
    p_bak_exp.add_argument("--data-dir", help="Runtime data directory (snapshots, ledgers, db)")
    p_bak_exp.add_argument("--motif-store", help="MotifWeightStore JSON path")
    p_bak_exp.add_argument("--ledger", help="UsageBudgetLedger JSON path")
    p_bak_exp.add_argument("--extra", action="append", help="Extra file path to include (repeatable)")
    p_bak_exp.set_defaults(func=_cmd_backup_export)

    p_bak_ver = bak_sub.add_parser("verify", help="Verify backup ZIP SHA256 manifest integrity")
    p_bak_ver.add_argument("--input", required=True, help="Backup .zip path")
    p_bak_ver.set_defaults(func=_cmd_backup_verify)

    p_bak_imp = bak_sub.add_parser("import", help="Verify and restore backup ZIP (archiving displaced files)")
    p_bak_imp.add_argument("--input", required=True, help="Backup .zip path")
    p_bak_imp.add_argument("--target-dir", required=True, help="Target restore directory")
    p_bak_imp.add_argument("--no-overwrite", action="store_true", help="Fail if any destination file exists")
    p_bak_imp.set_defaults(func=_cmd_backup_import)

    p_bak_git = bak_sub.add_parser("git", help="Run explicit-path private Git incremental backup")
    p_bak_git.add_argument("--repo", default=".", help="Git repository root")
    p_bak_git.add_argument("--paths", help="Comma-separated repo-relative paths to stage")
    p_bak_git.add_argument("--remote", default="origin", help="Git remote name")
    p_bak_git.add_argument("--branch", help="Target branch override")
    p_bak_git.add_argument("--message", help="Commit message override")
    p_bak_git.add_argument("--push", action="store_true", help="Push commit to remote")
    p_bak_git.set_defaults(func=_cmd_backup_git)

    p_srv = sub.add_parser("serve", help="Start cloud matrix HTTP/SSE server, scheduler, and optional Telegram bot")
    p_srv.add_argument("--host", help="Bind host (default 127.0.0.1 or XIAOSHUO_HOST)")
    p_srv.add_argument("--port", type=int, help="Bind port (default 8765 or XIAOSHUO_PORT)")
    p_srv.add_argument("--projects-root", help="Projects root directory (default ./projects)")
    p_srv.add_argument(
        "--db-path",
        "--db",
        dest="db_path",
        help="SQLite stockpile DB path (default ./data/matrix.db)",
    )
    p_srv.add_argument("--admin-token", help="Admin Bearer token (env XIAOSHUO_ADMIN_TOKEN; >=24 chars for non-loopback)")
    p_srv.add_argument(
        "--companion-hmac-secret",
        "--companion-secret",
        dest="companion_hmac_secret",
        help="Companion HMAC-SHA256 secret (env XIAOSHUO_COMPANION_HMAC_SECRET)",
    )
    p_srv.add_argument("--cpa-url", help="CPA base/management URL (env CPA_MANAGEMENT_URL)")
    p_srv.add_argument(
        "--cpa-management-key",
        "--cpa-secret",
        dest="cpa_management_key",
        help="CPA management key/secret (env CPA_MANAGEMENT_KEY)",
    )
    p_srv.add_argument("--router-config", help="Path to 5-role RoleModelRouter JSON/YAML config")
    p_srv.add_argument("--workbuddy-hy4-flash", help="Configured CPA upstream ID for WorkBuddy Hy4 flash (env WORKBUDDY_MODEL_HY4_FLASH)")
    p_srv.add_argument("--workbuddy-hy4", help="Configured CPA upstream ID for WorkBuddy Hy4 manual-only (env WORKBUDDY_MODEL_HY4)")
    p_srv.add_argument("--workbuddy-deepseek-v4-1-flash", help="Configured CPA upstream ID for WorkBuddy DeepSeek-V4.1-Flash (env WORKBUDDY_MODEL_DEEPSEEK_V4_1_FLASH)")
    p_srv.add_argument("--workbuddy-mimo-v2-6-pro", help="Configured CPA upstream ID for WorkBuddy Xiaomi MiMo-V2.6-Pro (env WORKBUDDY_MODEL_MIMO_V2_6_PRO)")
    p_srv.add_argument(
        "--workbuddy-rate-limit-backoff",
        help="Comma-separated HTTP 429 backoff retry delays in seconds before switching models (default 30,60,120; env WORKBUDDY_RATE_LIMIT_BACKOFF_SEC)",
    )
    p_srv.add_argument(
        "--workbuddy-model-cooldown-sec",
        type=float,
        help="Cooldown seconds for a quota-exhausted WorkBuddy model before auto-restoring (default 3600 = 1 hour; env WORKBUDDY_MODEL_COOLDOWN_SEC)",
    )
    p_srv.add_argument("--base-url", help="Default CPA/OpenAI base URL for RoleModelRouter")
    p_srv.add_argument("--api-key", help="Default CPA/OpenAI API key (or comma-separated pool)")
    p_srv.add_argument("--model", help="Default model for text roles")
    p_srv.add_argument("--planner-model", help="Model override for planner role")
    p_srv.add_argument("--planner-base-url", help="Base URL override for planner role")
    p_srv.add_argument("--planner-api-key", help="API key override for planner role")
    p_srv.add_argument("--planner-provider-type", help="Provider type override for planner role")
    p_srv.add_argument("--writer-model", help="Model override for writer role")
    p_srv.add_argument("--writer-base-url", help="Base URL override for writer role")
    p_srv.add_argument("--writer-api-key", help="API key override for writer role")
    p_srv.add_argument("--writer-provider-type", help="Provider type override for writer role")
    p_srv.add_argument("--critic-model", help="Model override for critic role")
    p_srv.add_argument("--critic-base-url", help="Base URL override for critic role")
    p_srv.add_argument("--critic-api-key", help="API key override for critic role")
    p_srv.add_argument("--critic-provider-type", help="Provider type override for critic role")
    p_srv.add_argument("--polisher-model", help="Model override for polisher role")
    p_srv.add_argument("--polisher-base-url", help="Base URL override for polisher role")
    p_srv.add_argument("--polisher-api-key", help="API key override for polisher role")
    p_srv.add_argument("--polisher-provider-type", help="Provider type override for polisher role")
    p_srv.add_argument("--image-model", help="Model override for image role")
    p_srv.add_argument("--image-base-url", help="Base URL override for image role")
    p_srv.add_argument("--image-api-key", help="API key override for image role")
    p_srv.add_argument("--image-provider-type", help="Provider type override for image role")
    p_srv.add_argument("--daily-budget-limit", type=float, help="Daily USD budget circuit breaker")
    p_srv.add_argument("--daily-chapter-limit", type=int, help="Daily chapter count circuit breaker")
    p_srv.add_argument("--ledger-path", help="UsageBudgetLedger JSON path")
    p_srv.add_argument("--motif-store", help="MotifWeightStore JSON path")
    p_srv.add_argument("--static-dir", help="Optional static Web console directory override")
    p_srv.add_argument("--sse-heartbeat-sec", type=float, default=15.0, help="SSE heartbeat interval in seconds")
    p_srv.add_argument("--scheduler-interval", type=float, default=10.0, help="Scheduler poll interval in seconds")
    p_srv.add_argument(
        "--export-only",
        action="store_true",
        help="Export-only mode (no publishing companion): write every active book straight through its outline",
    )
    p_srv.add_argument("--no-scheduler-loop", action="store_true", help="Disable background scheduler loop")
    p_srv.add_argument("--with-tg-bot", action="store_true", help="Run embedded TelegramControlBot polling thread")
    p_srv.add_argument("--no-tg-bot", action="store_true", help="Disable embedded TelegramControlBot even if TG env vars are set")
    p_srv.add_argument("--tg-bot-token", help="Telegram Bot token override (env TELEGRAM_BOT_TOKEN)")
    p_srv.add_argument("--tg-owner-user-id", help="Telegram owner user ID override (env TELEGRAM_OWNER_USER_ID)")
    p_srv.add_argument("--tg-api-base-url", help="Telegram Bot API base URL override")
    p_srv.add_argument("--tg-state-path", help="Telegram bot state JSON path")
    p_srv.set_defaults(func=_cmd_serve)

    p_cmp = sub.add_parser("companion", help="Run local outbound Playwright publishing & metrics companion")
    p_cmp.add_argument("--server-url", help="Cloud matrix server URL (env XIAOSHUO_SERVER_URL)")
    p_cmp.add_argument(
        "--hmac-secret",
        help="Cloud-edge HMAC-SHA256 secret (env XIAOSHUO_COMPANION_HMAC_SECRET)",
    )
    p_cmp.add_argument("--account-id", help="Tomato author account ID (env XIAOSHUO_COMPANION_ACCOUNT_ID)")
    p_cmp.add_argument("--author-name", help="Expected Tomato author pen name")
    p_cmp.add_argument("--book-ids", help="Comma-separated book IDs managed by this companion")
    p_cmp.add_argument("--profile-root", help="Root directory for isolated Chrome profiles (default ./chrome_profiles)")
    p_cmp.add_argument("--outbox-db", help="Local SQLite WAL outbox DB path")
    p_cmp.add_argument("--chrome-channel", default="chrome", help="Playwright browser channel (default chrome)")
    p_cmp.add_argument("--headless", action="store_true", help="Run browser headless (default False)")
    p_cmp.add_argument("--target-url", help="Target Tomato writer URL (env XIAOSHUO_COMPANION_TARGET_URL)")
    p_cmp.add_argument("--metrics-url", help="Optional override URL for COLLECT_METRICS tasks")
    p_cmp.add_argument("--star-cards-url", help="Optional override URL for CREATE_STAR_CARDS tasks")
    p_cmp.add_argument("--signing-url", help="Optional override URL for SUBMIT_SIGNING_DRAFT tasks")
    p_cmp.add_argument("--rotate-url", help="Optional override URL for ROTATE_PACKAGING tasks")
    p_cmp.add_argument("--once", action="store_true", help="Execute a single poll-and-run cycle and exit")
    p_cmp.add_argument("--poll-interval", type=float, default=2.0, help="Idle poll interval in seconds")
    p_cmp.add_argument("--poll-timeout", type=int, default=25, help="Server long-poll timeout in seconds (0..25)")
    p_cmp.add_argument("--woff2-cache-dir", help="Optional directory for caching decoded WOFF2 font maps")
    p_cmp.add_argument("--review-pause-sec", type=float, default=0.8, help="Pause after inserting prose (seconds)")
    p_cmp.add_argument("--screenshot-dir", help="Optional directory for captcha sentinel screenshots")
    p_cmp.add_argument("--client-id", help="Optional HMAC client_id override")
    p_cmp.add_argument(
        "--cover-root",
        help="Directory that ROTATE_PACKAGING cover uploads are confined to (env XIAOSHUO_COMPANION_COVER_ROOT); "
        "cover uploads are disabled when unset",
    )
    p_cmp.set_defaults(func=_cmd_companion)

    p_pilot = sub.add_parser(
        "pilot",
        help="Run golden-three-chapters multi-writer pilot benchmark with cross-family judging",
    )
    p_pilot.add_argument("--project", required=True, help="Path to novel project directory")
    p_pilot.add_argument(
        "--blueprint",
        help="Optional path to blueprint.yaml (defaults to <project>/blueprint.yaml)",
    )
    p_pilot.add_argument(
        "--chapters",
        default="1-3",
        help="Target chapter range or CSV, e.g. '1-3' or '1,2,3' (default: '1-3')",
    )
    p_pilot.add_argument(
        "--writer",
        action="append",
        required=True,
        help="WorkBuddy writer slot name (repeatable), e.g. hy4_flash, deepseek_v4_1_flash, mimo_v2_6_pro",
    )
    p_pilot.add_argument(
        "--judge",
        required=True,
        help="WorkBuddy judge slot name (must be cross-family with every --writer)",
    )
    p_pilot.add_argument(
        "--benchmark-text",
        required=True,
        help="Path to benchmark chapter text file (e.g. book1_ch1_3.txt)",
    )
    p_pilot.add_argument(
        "--case-summaries",
        required=True,
        help="Path to YAML mapping chapter number (int) to case summary string",
    )
    p_pilot.add_argument("--out", required=True, help="Output directory for pilot runs and scorecards")
    p_pilot.add_argument("--base-url", help="Default CPA/OpenAI base URL for WorkBuddy router")
    p_pilot.add_argument("--api-key", help="Default CPA/OpenAI API key (or comma-separated pool)")
    p_pilot.add_argument("--cpa-url", help="CPA base/management URL fallback (env CPA_BASE_URL)")
    p_pilot.add_argument(
        "--workbuddy-hy4-flash",
        help="Configured CPA upstream ID for WorkBuddy Hy4 flash (env WORKBUDDY_MODEL_HY4_FLASH)",
    )
    p_pilot.add_argument(
        "--workbuddy-hy4",
        help="Configured CPA upstream ID for WorkBuddy Hy4 manual-only (env WORKBUDDY_MODEL_HY4)",
    )
    p_pilot.add_argument(
        "--workbuddy-deepseek-v4-1-flash",
        help="Configured CPA upstream ID for WorkBuddy DeepSeek-V4.1-Flash (env WORKBUDDY_MODEL_DEEPSEEK_V4_1_FLASH)",
    )
    p_pilot.add_argument(
        "--workbuddy-mimo-v2-6-pro",
        help="Configured CPA upstream ID for WorkBuddy Xiaomi MiMo-V2.6-Pro (env WORKBUDDY_MODEL_MIMO_V2_6_PRO)",
    )
    p_pilot.add_argument(
        "--workbuddy-rate-limit-backoff",
        help="Comma-separated HTTP 429 backoff retry delays in seconds (env WORKBUDDY_RATE_LIMIT_BACKOFF_SEC)",
    )
    p_pilot.add_argument(
        "--workbuddy-model-cooldown-sec",
        type=float,
        help="Cooldown seconds for a quota-exhausted WorkBuddy model (env WORKBUDDY_MODEL_COOLDOWN_SEC)",
    )
    p_pilot.add_argument("--daily-budget-limit", type=float, help="Daily USD budget circuit breaker")
    p_pilot.add_argument("--daily-chapter-limit", type=int, help="Daily chapter count circuit breaker")
    p_pilot.add_argument("--ledger-path", help="UsageBudgetLedger JSON path")
    p_pilot.set_defaults(func=_cmd_pilot)

    p_vc = sub.add_parser(
        "volume-check",
        help="Run end-of-volume 6-point health check and update volume lock registry",
    )
    p_vc.add_argument("--project", required=True, help="Path to novel project directory")
    p_vc.add_argument(
        "--output",
        "--output-dir",
        dest="output_dir",
        required=True,
        help="Output directory containing chapters/, snapshots/, and audit/",
    )
    p_vc.add_argument("--volume", type=int, required=True, help="Volume index (>= 1) to audit")
    p_vc.add_argument(
        "--blueprint",
        help="Optional path to blueprint.yaml (defaults to <project>/blueprint.yaml)",
    )
    p_vc.add_argument(
        "--writer-model",
        help="Writer model ID for cross-family isolation (defaults to chapter_<end>.meta.json writer_model/model)",
    )
    p_vc.add_argument(
        "--judge-slot",
        help="Optional WorkBuddy slot name for judge (e.g. mimo_v2_6_pro; defaults to strong_planner_model when WorkBuddy catalog is configured and --model is omitted)",
    )
    p_vc.add_argument(
        "--core-keywords",
        help="Optional comma-separated core selling point keywords for VH_04 drift check",
    )
    p_vc.add_argument("--json", action="store_true", help="Print full VolumeHealthReport as JSON")
    _add_provider_args(p_vc)
    p_vc.add_argument("--cpa-url", help="CPA base/management URL fallback (env CPA_BASE_URL)")
    p_vc.add_argument(
        "--workbuddy-hy4-flash",
        help="Configured CPA upstream ID for WorkBuddy Hy4 flash (env WORKBUDDY_MODEL_HY4_FLASH)",
    )
    p_vc.add_argument(
        "--workbuddy-hy4",
        help="Configured CPA upstream ID for WorkBuddy Hy4 manual-only (env WORKBUDDY_MODEL_HY4)",
    )
    p_vc.add_argument(
        "--workbuddy-deepseek-v4-1-flash",
        help="Configured CPA upstream ID for WorkBuddy DeepSeek-V4.1-Flash (env WORKBUDDY_MODEL_DEEPSEEK_V4_1_FLASH)",
    )
    p_vc.add_argument(
        "--workbuddy-mimo-v2-6-pro",
        help="Configured CPA upstream ID for WorkBuddy Xiaomi MiMo-V2.6-Pro (env WORKBUDDY_MODEL_MIMO_V2_6_PRO)",
    )
    p_vc.add_argument(
        "--workbuddy-rate-limit-backoff",
        help="Comma-separated HTTP 429 backoff retry delays in seconds (env WORKBUDDY_RATE_LIMIT_BACKOFF_SEC)",
    )
    p_vc.add_argument(
        "--workbuddy-model-cooldown-sec",
        type=float,
        help="Cooldown seconds for a quota-exhausted WorkBuddy model (env WORKBUDDY_MODEL_COOLDOWN_SEC)",
    )
    p_vc.add_argument("--daily-budget-limit", type=float, help="Daily USD budget circuit breaker")
    p_vc.add_argument("--daily-chapter-limit", type=int, help="Daily chapter count circuit breaker")
    p_vc.add_argument("--ledger-path", help="UsageBudgetLedger JSON path")
    p_vc.set_defaults(func=_cmd_volume_check)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
