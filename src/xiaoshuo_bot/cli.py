from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import List, Optional

from .checker import AITicChecker
from .compiler import PromptCompiler
from .engine import NovelGenerationEngine
from .llm import AnthropicProvider, FaultInjectionProvider, FileBridgeProvider, OpenAICompatibleProvider
from .loader import load_project
from .state import WorldStateManager


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
    if args.project:
        project = load_project(args.project)
        characters = project.characters
        if args.chapter is not None:
            state = WorldStateManager(project)
            matching = [s for s in project.scenes if s.chapter == args.chapter]
            if matching:
                scene = matching[0]
                due_fs = state.get_due_foreshadows(scene.chapter)
                plant_fs = state.get_plant_foreshadows(scene.chapter)

    checker = AITicChecker()
    report = checker.scan(
        text=text,
        scene=scene,
        characters=characters,
        due_foreshadows=due_fs,
        plant_foreshadows=plant_fs,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 2


def _cmd_smoke_fault(args: argparse.Namespace) -> int:
    """Verify ProductionIncident detection & automatic rewrite recovery using deterministic fault injection."""
    project = load_project(args.project)
    first_scene = project.scenes[0]
    # Construct valid clean text satisfying first_scene constraints & foreshadows
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
    # Repeat varied realistic blocks to satisfy min_chars gate cleanly
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
    if args.provider == "openai":
        provider = OpenAICompatibleProvider(
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
        )
    elif args.provider == "anthropic":
        provider = AnthropicProvider(
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
        )
    elif args.provider == "bridge":
        bridge_dir = args.bridge_dir or (Path(args.project).resolve() / ".bridge")
        provider = FileBridgeProvider(
            bridge_dir=bridge_dir,
            timeout_sec=args.bridge_timeout,
            reuse_existing=args.resume,
        )
    else:
        raise ValueError(f"Unsupported provider: {args.provider}")

    out_dir = Path(args.output_dir) if args.output_dir else (Path(args.project).resolve() / "output")
    target_chapters: Optional[List[int]] = None
    if args.chapters:
        target_chapters = [int(x.strip()) for x in args.chapters.split(",") if x.strip()]

    engine = NovelGenerationEngine(project=project, provider=provider, output_dir=out_dir)
    manifest = engine.run(target_chapters=target_chapters, resume=args.resume)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xiaoshuo-bot",
        description="Asset-Driven Long-Form Chinese Novel Generation & AI-TIC-CHECK Pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="Validate project YAML schemas & cross-references")
    p_val.add_argument("--project", required=True, help="Path to novel project directory")
    p_val.set_defaults(func=_cmd_validate)

    p_comp = sub.add_parser("compile-prompt", help="Compile and print the dynamic prompt for a chapter")
    p_comp.add_argument("--project", required=True, help="Path to novel project directory")
    p_comp.add_argument("--chapter", type=int, required=True, help="Chapter number")
    p_comp.set_defaults(func=_cmd_compile_prompt)

    p_chk = sub.add_parser("check-text", help="Run AI-TIC-CHECK static scanner on a text file")
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

    p_run = sub.add_parser("run", help="Run end-to-end multi-chapter novel generation pipeline")
    p_run.add_argument("--project", required=True, help="Path to novel project directory")
    p_run.add_argument(
        "--provider",
        choices=["openai", "anthropic", "bridge"],
        default="openai",
        help="LLM backend provider",
    )
    p_run.add_argument("--output-dir", help="Output directory (defaults to <project>/output)")
    p_run.add_argument("--chapters", help="Optional comma-separated chapter numbers, e.g. 1,2,3")
    p_run.add_argument("--resume", action="store_true", help="Resume from verified chapter checkpoints")
    p_run.add_argument("--api-key", help="API key override")
    p_run.add_argument("--base-url", help="Base URL override")
    p_run.add_argument("--model", help="Model name override")
    p_run.add_argument("--bridge-dir", help="IPC directory when --provider=bridge")
    p_run.add_argument("--bridge-timeout", type=int, default=900, help="Per-request bridge timeout in seconds")
    p_run.set_defaults(func=_cmd_run)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
