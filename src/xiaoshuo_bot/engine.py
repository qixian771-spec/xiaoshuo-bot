from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .checker import AITicChecker
from .compiler import PromptCompiler
from .llm.base import LLMProvider, LLMRequest
from .models import NovelProject, ProductionIncident, ScanReport, SceneOutline
from .reporter import generate_html_dashboard, generate_markdown_report
from .state import WorldStateManager


class ProductionGateFailure(RuntimeError):
    """Raised when a chapter still fails AI-TIC-CHECK after exhausting rewrite retries."""


class NovelGenerationEngine:
    """End-to-end Asset-Driven Novel Generation, AI-TIC-CHECK Gate, Auto-Rewrite, and State Ledger Engine."""

    def __init__(
        self,
        project: NovelProject,
        provider: LLMProvider,
        output_dir: str | Path,
        checker: Optional[AITicChecker] = None,
    ) -> None:
        self.project = project
        self.provider = provider
        self.output_dir = Path(output_dir).resolve()
        self.checker = checker or AITicChecker()
        self.compiler = PromptCompiler(project)
        self.state = WorldStateManager(project)
        self.incidents: List[ProductionIncident] = []

    def run(
        self,
        target_chapters: Optional[List[int]] = None,
        resume: bool = False,
    ) -> Dict[str, Any]:
        chapters_dir = self.output_dir / "chapters"
        audit_dir = self.output_dir / "audit"
        snapshots_dir = self.output_dir / "snapshots"
        for d in (chapters_dir, audit_dir, snapshots_dir):
            d.mkdir(parents=True, exist_ok=True)

        incidents_jsonl = audit_dir / "incidents.jsonl"
        if not resume and incidents_jsonl.exists():
            incidents_jsonl.unlink()

        # Save initial state snapshot (Chapter 0 baseline)
        self.state.save_snapshot(snapshots_dir / "chapter_00_initial.json")

        scenes = [
            s for s in self.project.scenes
            if target_chapters is None or s.chapter in target_chapters
        ]

        chapter_summaries: List[Dict[str, Any]] = []
        started_at = datetime.now(timezone.utc).isoformat()

        try:
            for scene in scenes:
                existing_ch_file = chapters_dir / f"chapter_{scene.chapter:02d}.txt"
                if resume and existing_ch_file.exists():
                    existing_text = existing_ch_file.read_text(encoding="utf-8").rstrip()
                    due_fs = self.state.get_due_foreshadows(scene.chapter)
                    plant_fs = self.state.get_plant_foreshadows(scene.chapter)
                    chk = self.checker.scan(
                        text=existing_text,
                        scene=scene,
                        characters=self.project.characters,
                        due_foreshadows=due_fs,
                        plant_foreshadows=plant_fs,
                    )
                    if chk.passed:
                        state_diff = self.state.advance_after_chapter(scene, existing_text)
                        print(
                            f"[Engine] Chapter {scene.chapter:02d} resumed from verified checkpoint ({chk.char_count} chars)",
                            flush=True,
                        )
                        chapter_summaries.append(
                            {
                                "chapter": scene.chapter,
                                "scene_id": scene.scene_id,
                                "internal_title": scene.internal_title,
                                "pov_character": self.project.characters[scene.pov_character].name,
                                "attempts_used": 1,
                                "rewrites_triggered": 0,
                                "char_count": chk.char_count,
                                "paragraph_count": chk.paragraph_count,
                                "first_char": existing_text[0],
                                "opening_preview": existing_text[:68].replace("\n", " "),
                                "ending_preview": existing_text[-68:].replace("\n", " "),
                                "model": "agent-bridge-llm (checkpoint)",
                                "latency_sec": 0.0,
                                "chapter_file": str(existing_ch_file),
                                "state_transitions": state_diff,
                            }
                        )
                        continue

                ch_result = self._run_single_chapter(
                    scene=scene,
                    chapters_dir=chapters_dir,
                    audit_dir=audit_dir,
                    snapshots_dir=snapshots_dir,
                    incidents_jsonl=incidents_jsonl,
                )
                chapter_summaries.append(ch_result)
        finally:
            self.provider.close()

        finished_at = datetime.now(timezone.utc).isoformat()

        # Assemble pure prose full manuscript (no titles, no meta — pure prose blocks)
        pure_parts: List[str] = []
        reader_parts: List[str] = [f"# 《{self.project.meta.title}》\n\n> {self.project.meta.synopsis}\n"]
        for ch_meta in chapter_summaries:
            ch_file = Path(ch_meta["chapter_file"])
            body = ch_file.read_text(encoding="utf-8")
            pure_parts.append(body)
            reader_parts.append(f"## 第 {ch_meta['chapter']} 章\n\n{body}\n")

        manuscript_pure_path = self.output_dir / "manuscript_pure.txt"
        manuscript_pure_path.write_text("\n\n".join(pure_parts) + "\n", encoding="utf-8")

        manuscript_reader_path = self.output_dir / "manuscript_reader.md"
        manuscript_reader_path.write_text("\n\n".join(reader_parts), encoding="utf-8")

        total_chars = sum(c["char_count"] for c in chapter_summaries)
        manifest: Dict[str, Any] = {
            "project": self.project.meta.to_dict(),
            "started_at": started_at,
            "finished_at": finished_at,
            "chapter_count": len(chapter_summaries),
            "total_chars": total_chars,
            "total_incidents_detected": len(self.incidents),
            "all_gates_passed": True,
            "chapters": chapter_summaries,
            "incidents": [inc.to_dict() for inc in self.incidents],
            "final_world_state": self.state.snapshot_dict(),
            "artifacts": {
                "manuscript_pure": str(manuscript_pure_path),
                "manuscript_reader": str(manuscript_reader_path),
            },
        }

        manifest_path = self.output_dir / "run_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        report_md_path = self.output_dir / "SMOKE_TEST_REPORT.md"
        report_md_path.write_text(generate_markdown_report(manifest), encoding="utf-8")

        dashboard_html_path = self.output_dir / "smoke_test_dashboard.html"
        dashboard_html_path.write_text(
            generate_html_dashboard(manifest, chapters_dir), encoding="utf-8"
        )

        manifest["artifacts"]["smoke_test_report"] = str(report_md_path)
        manifest["artifacts"]["dashboard_html"] = str(dashboard_html_path)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    def _run_single_chapter(
        self,
        scene: SceneOutline,
        chapters_dir: Path,
        audit_dir: Path,
        snapshots_dir: Path,
        incidents_jsonl: Path,
    ) -> Dict[str, Any]:
        max_attempts = self.project.meta.max_rewrite_retries + 1
        due_fs = self.state.get_due_foreshadows(scene.chapter)
        plant_fs = self.state.get_plant_foreshadows(scene.chapter)

        sys_prompt, user_prompt = self.compiler.compile_scene_prompt(scene, self.state)
        (audit_dir / f"ch{scene.chapter:02d}_compiled_prompt.md").write_text(
            f"{sys_prompt}\n\n{user_prompt}\n", encoding="utf-8"
        )

        last_report: Optional[ScanReport] = None
        last_draft: str = ""

        for attempt in range(1, max_attempts + 1):
            purpose = "draft" if attempt == 1 else "rewrite"
            if attempt > 1 and last_report is not None:
                sys_prompt, user_prompt = self.compiler.compile_rewrite_prompt(
                    scene=scene,
                    state=self.state,
                    draft_text=last_draft,
                    report=last_report,
                )
                (audit_dir / f"ch{scene.chapter:02d}_attempt{attempt}_rewrite_prompt.md").write_text(
                    f"{sys_prompt}\n\n{user_prompt}\n", encoding="utf-8"
                )

            req_id = f"ch{scene.chapter:02d}_attempt{attempt}_{purpose}"
            llm_req = LLMRequest(
                request_id=req_id,
                chapter=scene.chapter,
                attempt=attempt,
                purpose=purpose,
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                temperature=0.65 if attempt == 1 else 0.45,
            )

            print(
                f"[Engine] Chapter {scene.chapter:02d} ({scene.scene_id}) | Attempt {attempt}/{max_attempts} ({purpose})",
                flush=True,
            )
            llm_resp = self.provider.generate(llm_req)
            candidate_text = llm_resp.content.rstrip()

            report = self.checker.scan(
                text=candidate_text,
                scene=scene,
                characters=self.project.characters,
                due_foreshadows=due_fs,
                plant_foreshadows=plant_fs,
            )
            (audit_dir / f"ch{scene.chapter:02d}_attempt{attempt}_scan.json").write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )

            if report.passed:
                print(
                    f"[Engine] Chapter {scene.chapter:02d} PASSED AI-TIC-CHECK "
                    f"({report.char_count} chars, {report.paragraph_count} paragraphs, attempt {attempt})",
                    flush=True,
                )
                ch_path = chapters_dir / f"chapter_{scene.chapter:02d}.txt"
                ch_path.write_text(candidate_text, encoding="utf-8")

                state_diff = self.state.advance_after_chapter(scene, candidate_text)
                self.state.save_snapshot(snapshots_dir / f"chapter_{scene.chapter:02d}_after.json")

                return {
                    "chapter": scene.chapter,
                    "scene_id": scene.scene_id,
                    "internal_title": scene.internal_title,
                    "pov_character": self.project.characters[scene.pov_character].name,
                    "attempts_used": attempt,
                    "rewrites_triggered": attempt - 1,
                    "char_count": report.char_count,
                    "paragraph_count": report.paragraph_count,
                    "first_char": candidate_text[0],
                    "opening_preview": candidate_text[:68].replace("\n", " "),
                    "ending_preview": candidate_text[-68:].replace("\n", " "),
                    "model": llm_resp.model,
                    "latency_sec": llm_resp.latency_sec,
                    "chapter_file": str(ch_path),
                    "state_transitions": state_diff,
                }

            # Record Production Incident & trigger automatic rewrite
            incident = ProductionIncident(
                incident_id=f"INC-CH{scene.chapter:02d}-A{attempt}",
                chapter=scene.chapter,
                scene_id=scene.scene_id,
                attempt=attempt,
                timestamp=datetime.now(timezone.utc).isoformat(),
                violations=report.violations,
                action_taken=(
                    f"Triggered automatic rewrite attempt {attempt + 1}"
                    if attempt < max_attempts
                    else "Exhausted retries — aborting pipeline"
                ),
            )
            self.incidents.append(incident)
            with incidents_jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(incident.to_dict(), ensure_ascii=False) + "\n")

            (audit_dir / f"ch{scene.chapter:02d}_attempt{attempt}_rejected.txt").write_text(
                candidate_text, encoding="utf-8"
            )
            print(
                f"[Engine] PRODUCTION INCIDENT {incident.incident_id}: "
                f"{len(report.violations)} violation(s) -> {incident.action_taken}",
                flush=True,
            )
            last_report = report
            last_draft = candidate_text

        raise ProductionGateFailure(
            f"Chapter {scene.chapter} failed AI-TIC-CHECK after {max_attempts} attempts. "
            f"Last violations: {[v.to_dict() for v in (last_report.violations if last_report else [])]}"
        )
