"""A/B round 2 (after the length-target fix): writer reasoning low vs high, chapters 1-3.

Usage: python reasoning_ab2.py <project_src_dir> <work_dir> <writer_model> <label>
Only the writer role differs between arms. Per-chapter stats are read from the engine's own
audit files, so a gate failure part-way still reports every attempt that happened.
"""

import glob
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from xiaoshuo_bot.engine import NovelGenerationEngine
from xiaoshuo_bot.llm.router import RoleEndpointConfig, RoleModelRouter
from xiaoshuo_bot.loader import load_project

src, work, writer_model, label = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]
CHAPTERS = [1, 2, 3]
base_url = os.environ["XIAOSHUO_BASE_URL"]
keys = [os.environ["XIAOSHUO_API_KEY"]]
fixed = {
    "planner": os.environ["XIAOSHUO_PLANNER_MODEL"],
    "critic": os.environ["XIAOSHUO_CRITIC_MODEL"],
    "polisher": os.environ["XIAOSHUO_MODEL"],
    "writer": writer_model,
}
router = RoleModelRouter(
    {r: RoleEndpointConfig(role=r, base_url=base_url, model=m, api_keys=keys, provider_type="openai") for r, m in fixed.items()}
)

proj_dir = work / label / "project"
if proj_dir.exists():
    raise SystemExit(f"{proj_dir} already exists; refusing to overwrite")
shutil.copytree(src, proj_dir, ignore=shutil.ignore_patterns("output"))
engine = NovelGenerationEngine(project=load_project(proj_dir), provider=router, output_dir=proj_dir / "output")

started = time.time()
result = {"label": label, "writer_model": writer_model, "roles": fixed}
manifest = None
try:
    manifest = engine.run(target_chapters=CHAPTERS)
    result["status"] = "PASSED"
except Exception as exc:  # the failure is a data point; report it in full
    result["status"] = "FAILED"
    result["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
result["seconds"] = round(time.time() - started, 1)

audit = proj_dir / "output" / "audit"
by_ch = {c["chapter"]: c for c in (manifest or {}).get("chapters", [])}
chapters = []
for n in CHAPTERS:
    scans = sorted(glob.glob(str(audit / f"ch{n:04d}_attempt*_scan.json")),
                   key=lambda p: int(re.search(r"attempt(\d+)_", p).group(1)))
    if not scans:
        continue
    attempts = []
    for p in scans:
        d = json.load(open(p, encoding="utf-8"))
        attempts.append({
            "chars": d.get("char_count"),
            "critical": [v["rule_id"] for v in d.get("violations", []) if v.get("severity") == "CRITICAL"],
        })
    ce = (by_ch.get(n) or {}).get("critic_evaluation") or {}
    chapters.append({
        "chapter": n,
        "passed": n in by_ch,
        "attempts": attempts,
        "final_chars": (by_ch.get(n) or {}).get("char_count"),
        "critic_overall": ce.get("overall_score"),
        "critic_hook": ce.get("first_screen_hook"),
    })
result["chapters"] = chapters
(work / f"{label}.result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"label": label, "status": result["status"], "seconds": result["seconds"]}, ensure_ascii=False))
