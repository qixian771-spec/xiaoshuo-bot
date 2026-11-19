"""Admin new-book wizard: one question at a time, the LLM proposes 3 options per step.

Flow (owner requirement, Web UI grill-me Q7=A "分步向导"):
    idea -> direction -> protagonist -> style -> scale(title/length) -> confirm -> auto_create_book

Every step's options come from the planner-role LLM. Invalid LLM output raises WizardError;
there is no silent retry or default option. The admin can explicitly ask for a reroll.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any, Callable, Dict, List, Mapping, Optional
import uuid

from ..contracts import AutoCreateBookRequest
from ..genres import list_genres
from ..llm.base import LLMRequest
from ..models import AUTHOR_LEVELS, SHANGHAI_TZ, ProjectValidationError
from ..planner import _invoke_planner, _parse_json_payload
from .stockpile_db import StockpileDB


logger = logging.getLogger(__name__)


class WizardError(ValueError):
    """Raised for invalid wizard input or invalid LLM proposals (surfaced to the admin as 400)."""


STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_READY = "READY_TO_CONFIRM"
STATUS_CREATED = "CREATED"

OPTIONS_PER_STEP = 3
# Invalid proposals are fed back to the LLM with the exact validation error, at most this many
# times in total; then the admin sees a WizardError (no silent acceptance, no default option).
PROPOSAL_ATTEMPTS = 3
LV0_1_CHAPTER_RANGE = (70, 95)


def _genre_catalog() -> List[Dict[str, str]]:
    return [
        {"genre_id": g.genre_id, "display_name": g.display_name, "channel": g.channel}
        for g in list_genres()
    ]


WIZARD_STEPS: List[Dict[str, Any]] = [
    {
        "key": "direction",
        "title": "故事方向",
        "question": "这本书往哪个方向写？",
        "allow_custom": True,
        "fields": {
            "label": "方向名（不超过 12 字）",
            "logline": "一句话梗概（30~60 字）",
            "genre_id": "必须从给定类目列表中选择一个 genre_id",
            "selling_point": "最大卖点（一句话）",
            "tone": "基调（如：爽快、冷峻、温情）",
        },
    },
    {
        "key": "protagonist",
        "title": "主角与金手指",
        "question": "主角是什么样的人，靠什么翻盘？",
        "allow_custom": True,
        "fields": {
            "label": "主角定位（不超过 12 字，不要起具体姓名）",
            "persona": "性格与处境（一句话）",
            "golden_finger": "金手指或核心能力",
            "cost": "使用能力的代价或限制",
            "arc": "全书成长弧线（一句话）",
        },
    },
    {
        "key": "style",
        "title": "笔风与节奏",
        "question": "用什么笔风来写？",
        "allow_custom": True,
        "fields": {
            "label": "笔风名（不超过 12 字）",
            "description": "叙事特点：视角、句子长短、对话比例、节奏",
            "sample": "40~80 字示范段落，必须是正文，不得有总结腔",
        },
    },
    {
        "key": "scale",
        "title": "篇幅与书名",
        "question": "书名和篇幅定哪个？",
        "allow_custom": False,
        "fields": {
            "label": "方案名（不超过 12 字）",
            "title": "书名（硬性要求：1~15 个字符，含标点在内不得超过 15 个字符，不带书名号）",
            "author_level": "lv0_1（70~95 章紧凑完本）或 lv2_plus（长篇连载）",
            "target_chapters": "lv0_1 时为 70~95 的整数；lv2_plus 时填 0",
            "blurb": "三段式简介合并为一段（80~150 字）",
        },
    },
]


def _validate_option(step_key: str, raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WizardError(f"{step_key}: option must be an object, got {type(raw).__name__}")
    fields = next(s["fields"] for s in WIZARD_STEPS if s["key"] == step_key)
    opt: Dict[str, Any] = {}
    for name in fields:
        if name not in raw:
            raise WizardError(f"{step_key}: option missing field '{name}'")
        opt[name] = raw[name]
    for name, value in opt.items():
        if name == "target_chapters":
            continue
        if not isinstance(value, str) or not value.strip():
            raise WizardError(f"{step_key}: field '{name}' must be a non-empty string")
        opt[name] = value.strip()

    if step_key == "direction":
        valid_ids = {g["genre_id"] for g in _genre_catalog()}
        if opt["genre_id"] not in valid_ids:
            raise WizardError(f"direction: unknown genre_id {opt['genre_id']!r}")
    if step_key == "scale":
        title = opt["title"]
        if not (1 <= len(title) <= 15) or "《" in title or "》" in title:
            raise WizardError(f"scale: title {title!r} must be 1~15 chars without 《》")
        if opt["author_level"] not in AUTHOR_LEVELS:
            raise WizardError(f"scale: author_level {opt['author_level']!r} not in {AUTHOR_LEVELS}")
        try:
            chapters = int(opt["target_chapters"])
        except (TypeError, ValueError) as exc:
            raise WizardError(f"scale: target_chapters {opt['target_chapters']!r} is not an integer") from exc
        if opt["author_level"] == "lv0_1":
            lo, hi = LV0_1_CHAPTER_RANGE
            if not (lo <= chapters <= hi):
                raise WizardError(f"scale: lv0_1 target_chapters must be in {lo}..{hi}, got {chapters}")
        opt["target_chapters"] = chapters
    return opt


def _choices_text(choices: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for c in choices:
        step = next(s for s in WIZARD_STEPS if s["key"] == c["step"])
        body = json.dumps(c["option"], ensure_ascii=False)
        lines.append(f"- {step['title']}：{body}")
    return "\n".join(lines) if lines else "（尚无）"


class NewBookWizard:
    def __init__(
        self,
        *,
        db: StockpileDB,
        provider_fn: Callable[[], Any],
        create_book_fn: Callable[[AutoCreateBookRequest], Any],
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.db = db
        self._provider_fn = provider_fn
        self._create_book_fn = create_book_fn
        self._now_fn = now_fn or (lambda: datetime.now(SHANGHAI_TZ))

    # ---------------------------------------------------------------- LLM
    def _propose(self, idea: str, step_index: int, choices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        step = WIZARD_STEPS[step_index]
        system_prompt = (
            "你是中文网文立项策划。老板会一步一步做选择，你每一步只给出恰好 3 个差异明显的候选方案。"
            "严格输出单个 JSON 对象：{\"options\": [ ... 3 个对象 ... ]}，不要输出任何其他文字。"
            "候选必须与老板已做的选择保持一致，不得推翻前面的选择。"
        )
        field_lines = "\n".join(f"  - {k}: {v}" for k, v in step["fields"].items())
        catalog = ""
        if step["key"] == "direction":
            catalog = "可选类目（genre_id 只能取这里的值）：\n" + json.dumps(
                _genre_catalog(), ensure_ascii=False
            ) + "\n"
        user_prompt = (
            f"老板的原始题材想法：{idea}\n"
            f"老板已经做的选择：\n{_choices_text(choices)}\n"
            f"当前这一步：{step['title']} —— {step['question']}\n"
            f"{catalog}"
            f"每个候选对象必须包含以下字段：\n{field_lines}\n"
        )
        provider = self._provider_fn()
        errors: List[str] = []
        for attempt in range(1, PROPOSAL_ATTEMPTS + 1):
            feedback = (
                "\n[上一次输出不合格，必须全部修正后重新输出]：\n" + "\n".join(f"- {e}" for e in errors) + "\n"
                if errors
                else ""
            )
            req = LLMRequest(
                request_id=f"wizard-{step['key']}-{attempt}-{uuid.uuid4().hex[:8]}",
                chapter=0,
                attempt=attempt,
                purpose="plan",
                system_prompt=system_prompt,
                user_prompt=user_prompt + feedback,
                temperature=0.8,
                max_tokens=3000,
                metadata={"stage": f"wizard_{step['key']}", "attempt": attempt},
            )
            resp = _invoke_planner(provider, req)
            try:
                payload = _parse_json_payload(resp.content, f"wizard_{step['key']}")
                raw_options = payload.get("options")
                if not isinstance(raw_options, list) or len(raw_options) != OPTIONS_PER_STEP:
                    raise WizardError(
                        f"{step['key']}: LLM must return exactly {OPTIONS_PER_STEP} options, got "
                        f"{len(raw_options) if isinstance(raw_options, list) else type(raw_options).__name__}"
                    )
                return [_validate_option(step["key"], o) for o in raw_options]
            except (WizardError, ProjectValidationError) as exc:
                logger.warning("wizard %s proposal attempt %d rejected: %s", step["key"], attempt, exc)
                errors.append(str(exc))
        raise WizardError(
            f"{step['key']}: LLM proposals failed validation {PROPOSAL_ATTEMPTS} times: " + " | ".join(errors)
        )

    # ---------------------------------------------------------------- views
    def _view(self, session_id: str) -> Dict[str, Any]:
        s = self.db.get_wizard_session(session_id)
        step = WIZARD_STEPS[s["step_index"]] if s["status"] == STATUS_IN_PROGRESS else None
        return {
            "session_id": s["session_id"],
            "idea": s["idea"],
            "status": s["status"],
            "step_index": s["step_index"],
            "total_steps": len(WIZARD_STEPS),
            "step": (
                {
                    "key": step["key"],
                    "title": step["title"],
                    "question": step["question"],
                    "allow_custom": step["allow_custom"],
                }
                if step
                else None
            ),
            "options": s["options"],
            "choices": s["choices"],
            "book_id": s["book_id"],
        }

    def get(self, session_id: str) -> Dict[str, Any]:
        return self._view(session_id)

    # ---------------------------------------------------------------- actions
    def start(self, idea: str) -> Dict[str, Any]:
        idea = (idea or "").strip()
        if not (2 <= len(idea) <= 300):
            raise WizardError("题材想法需要 2~300 字")
        options = self._propose(idea, 0, [])
        session_id = uuid.uuid4().hex
        self.db.create_wizard_session(session_id, idea, options)
        return self._view(session_id)

    def answer(self, session_id: str, body: Mapping[str, Any]) -> Dict[str, Any]:
        s = self.db.get_wizard_session(session_id)
        if s["status"] != STATUS_IN_PROGRESS:
            raise WizardError(f"会话状态为 {s['status']}，不能再作答")
        step_index = s["step_index"]
        step = WIZARD_STEPS[step_index]
        choices: List[Dict[str, Any]] = list(s["choices"])

        if body.get("reroll") is True:
            options = self._propose(s["idea"], step_index, choices)
            self.db.update_wizard_session(
                session_id, status=STATUS_IN_PROGRESS, step_index=step_index, choices=choices, options=options
            )
            return self._view(session_id)

        if "custom" in body:
            if not step["allow_custom"]:
                raise WizardError(f"「{step['title']}」这一步只能从候选中选择或重出")
            text = str(body.get("custom") or "").strip()
            if not (2 <= len(text) <= 300):
                raise WizardError("自定义内容需要 2~300 字")
            if step["key"] == "direction":
                raise WizardError("故事方向需要确定类目，请先选一个候选再在后续步骤补充想法")
            chosen: Dict[str, Any] = {"label": "老板自定义", "custom": text}
        elif "choice" in body:
            try:
                idx = int(body["choice"])
            except (TypeError, ValueError) as exc:
                raise WizardError("choice 必须是整数") from exc
            if not (0 <= idx < len(s["options"])):
                raise WizardError(f"choice 超出范围 0..{len(s['options']) - 1}")
            chosen = dict(s["options"][idx])
        else:
            raise WizardError("请求必须包含 choice、custom 或 reroll=true 之一")

        choices.append({"step": step["key"], "option": chosen})
        next_index = step_index + 1
        if next_index >= len(WIZARD_STEPS):
            self.db.update_wizard_session(
                session_id, status=STATUS_READY, step_index=next_index, choices=choices, options=[]
            )
        else:
            options = self._propose(s["idea"], next_index, choices)
            self.db.update_wizard_session(
                session_id, status=STATUS_IN_PROGRESS, step_index=next_index, choices=choices, options=options
            )
        return self._view(session_id)

    def confirm(self, session_id: str, *, pen_name: str = "") -> Dict[str, Any]:
        s = self.db.get_wizard_session(session_id)
        if s["status"] != STATUS_READY:
            raise WizardError(f"会话状态为 {s['status']}，四步都选完才能确认开写")
        by_step = {c["step"]: c["option"] for c in s["choices"]}
        direction = by_step["direction"]
        scale = by_step["scale"]
        project_id = "book_" + self._now_fn().strftime("%Y%m%d_%H%M%S")
        brief = (
            f"原始题材：{s['idea']}\n"
            f"书名必须为：{scale['title']}\n"
            f"简介方向：{scale['blurb']}\n"
            f"{_choices_text(s['choices'])}"
        )
        target = int(scale["target_chapters"]) if scale["author_level"] == "lv0_1" else None
        req = AutoCreateBookRequest(
            project_id=project_id,
            genre_id=direction["genre_id"],
            author_level=scale["author_level"],
            pen_name=pen_name.strip(),
            target_chapters=target,
            creative_brief=brief,
        )
        summary = self._create_book_fn(req)
        book_id = getattr(summary, "book_id", "") or project_id
        self.db.update_wizard_session(
            session_id,
            status=STATUS_CREATED,
            step_index=s["step_index"],
            choices=s["choices"],
            options=[],
            book_id=book_id,
        )
        return self._view(session_id)


__all__ = ["NewBookWizard", "WizardError", "WIZARD_STEPS"]
