"""Diagnose plan_new_project against the real model: capture the raw planner output once.

Usage: python planner_diag.py <wizard_session_id> <out_dir>
Rebuilds the exact creative brief the wizard would send, calls the planner ONCE
(max_validation_retries=1), and saves the raw LLM response + usage + the validation error.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from xiaoshuo_bot.llm.base import LLMProvider
from xiaoshuo_bot.llm.router import RoleEndpointConfig, RoleModelRouter
from xiaoshuo_bot.planner import AutoNovelPlanner
from xiaoshuo_bot.server.stockpile_db import StockpileDB
from xiaoshuo_bot.server.wizard import _choices_text

sid, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
db = StockpileDB("/opt/xiaoshuo-bot/data/matrix.db")
s = db.get_wizard_session(sid)
by = {c["step"]: c["option"] for c in s["choices"]}
brief = (
    f"原始题材：{s['idea']}\n书名必须为：{by['scale']['title']}\n简介方向：{by['scale']['blurb']}\n"
    f"{_choices_text(s['choices'])}"
)
(out / "brief.txt").write_text(brief, encoding="utf-8")

router = RoleModelRouter({
    "planner": RoleEndpointConfig(
        role="planner",
        base_url=os.environ["XIAOSHUO_BASE_URL"],
        model=os.environ["XIAOSHUO_PLANNER_MODEL"],
        api_keys=[os.environ["XIAOSHUO_API_KEY"]],
        provider_type="openai",
    )
})


class Recording(LLMProvider):
    n = 0

    def generate(self, request):
        Recording.n += 1
        (out / f"request_{Recording.n}.json").write_text(json.dumps({
            "system": request.system_prompt, "user": request.user_prompt, "max_tokens": request.max_tokens,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        t = time.time()
        resp = router.generate("planner", request)
        (out / f"raw_{Recording.n}.txt").write_text(resp.content, encoding="utf-8")
        (out / f"meta_{Recording.n}.json").write_text(json.dumps({
            "seconds": round(time.time() - t, 1), "model": resp.model, "usage": resp.usage, "chars": len(resp.content),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return resp


planner = AutoNovelPlanner(Recording(), max_validation_retries=1)
with tempfile.TemporaryDirectory() as tmp:
    try:
        planner.plan_new_project(
            project_id="diag_book", genre_id=by["direction"]["genre_id"], output_dir=Path(tmp) / "diag_book",
            author_level=by["scale"]["author_level"], pen_name="试跑",
            target_chapters=int(by["scale"]["target_chapters"]) if by["scale"]["author_level"] == "lv0_1" else None,
            creative_brief=brief,
        )
        print("UNEXPECTED: planner succeeded")
    except Exception as exc:
        (out / "error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        print("error:", str(exc)[:300])
print(json.load(open(out / "meta_1.json")))
