from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import List, Optional

from .backup import export_backup, git_incremental_backup, import_backup, verify_backup
from .checker import AITicChecker
from .compiler import PromptCompiler
from .cover import generate_cover_for_project
from .deconstructor import BestsellerDeconstructor
from .engine import NovelGenerationEngine
from .evolution import MotifWeightStore
from .exporter import export_project_bundle
from .genres import get_genre, list_genres
from .llm import (
    AnthropicProvider,
    FaultInjectionProvider,
    FileBridgeProvider,
    LLMProvider,
    OpenAICompatibleProvider,
    RoleEndpointConfig,
    RoleModelRouter,
)
from .loader import load_project
from .models import ChapterArtifact, resolve_chapter_path
from .planner import AutoNovelPlanner
from .state import WorldStateManager
from .transcreator import (
    WebnovelTranscreator,
    build_glossary,
    export_english_serialization_bundle,
)

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
    api_key = _resolve_env_or_arg(
        getattr(args, "api_key", None),
        "XIAOSHUO_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    )
    base_url = _resolve_env_or_arg(
        getattr(args, "base_url", None),
        "XIAOSHUO_BASE_URL",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
    )
    model = _resolve_env_or_arg(
        getattr(args, "model", None),
        "XIAOSHUO_MODEL",
        "OPENAI_MODEL",
        "ANTHROPIC_MODEL",
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
    sys_p, usr_p = compiler.compile_scene_prompt(matching[0], state)
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
    planner = AutoNovelPlanner(provider)
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
    planner = AutoNovelPlanner(provider)
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

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
