"""Admin new-book wizard: one question at a time, the LLM proposes 3 options per step.

Flow (owner requirement, Web UI grill-me Q7=A "分步向导"):
    idea -> direction -> protagonist -> style -> scale(title/length) -> confirm -> auto_create_book

Every step's options come from the planner-role LLM. Invalid LLM output raises WizardError;
there is no silent retry or default option. The admin can explicitly ask for a reroll.
"""

from __future__ import annotations

from datetime import datetime
import inspect
import json
import logging
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional
import uuid

from ..contracts import AutoCreateBookRequest
from ..genres import UnknownGenreError, get_genre, list_genres
from ..llm.base import LLMRequest
from ..models import AUTHOR_LEVELS, SHANGHAI_TZ, ProjectValidationError
from ..planner import _invoke_planner, _parse_json_payload
from .stockpile_db import StockpileDB


logger = logging.getLogger(__name__)


class WizardError(ValueError):
    """Raised for invalid wizard input or invalid LLM proposals (surfaced to the admin as 400)."""


class WizardConflictError(WizardError):
    """Raised when a planning job is already running (surfaced to the admin as 409)."""


STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_READY = "READY_TO_CONFIRM"
STATUS_PLANNING = "PLANNING"
STATUS_CREATED = "CREATED"
STATUS_FAILED = "FAILED"

OPTIONS_PER_STEP = 3
# Invalid proposals are fed back to the LLM with the exact validation error, at most this many
# times in total; then the admin sees a WizardError (no silent acceptance, no default option).
PROPOSAL_ATTEMPTS = 3
LV0_1_CHAPTER_RANGE = (70, 95)
MIN_TARGET_TOTAL_CHARS = 100_000


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
        "question": "书名和目标总字数定哪个？",
        "allow_custom": False,
        "fields": {
            "label": "方案名（不超过 12 字，须标明目标总字数，如「80万字长篇」「120万字长篇」或「18万字完本」）",
            "title": "高点击商业书名（硬性要求：1~15 个字符且建议 7~15 字，不带书名号；必须露出主角身份/外挂/冲突反差至少两项，严禁《三点十七分》《白雾渡口》类纯时间/纯地名文艺短名）",
            "author_level": "按目标总字数选择：80万~150万字有明确大结局的完整长篇选 lv2_plus，最多 1 个备选用 lv0_1（15万~20万字紧凑完本）",
            "target_chapters": "lv2_plus 时填 0（具体卷数与章数由全书骨架按目标总字数规划）；lv0_1 时为 70~95 的整数",
            "blurb": "三段式简介合并为一段（80~150 字，突出开篇压迫→外挂破局→阶梯爽点）",
        },
        "optional_fields": {
            "target_total_chars": "目标总字数（正整数，至少 100000；如 800000、1200000、1500000 表示有明确结局的完整长篇，或 150000~200000 表示紧凑完本）",
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
        raw_chapters = opt["target_chapters"]
        if isinstance(raw_chapters, bool):
            raise WizardError(f"scale: target_chapters {raw_chapters!r} is not an integer")
        try:
            chapters = int(raw_chapters)
        except (TypeError, ValueError) as exc:
            raise WizardError(f"scale: target_chapters {raw_chapters!r} is not an integer") from exc
        if opt["author_level"] == "lv0_1":
            lo, hi = LV0_1_CHAPTER_RANGE
            if not (lo <= chapters <= hi):
                raise WizardError(f"scale: lv0_1 target_chapters must be in {lo}..{hi}, got {chapters}")
        opt["target_chapters"] = chapters
        if "target_total_chars" in raw:
            raw_total_chars = raw["target_total_chars"]
            if not isinstance(raw_total_chars, int) or isinstance(raw_total_chars, bool):
                raise WizardError(
                    f"scale: target_total_chars must be an integer >= {MIN_TARGET_TOTAL_CHARS}, "
                    f"got {raw_total_chars!r}"
                )
            if raw_total_chars < MIN_TARGET_TOTAL_CHARS:
                raise WizardError(
                    f"scale: target_total_chars must be >= {MIN_TARGET_TOTAL_CHARS}, got {raw_total_chars}"
                )
            opt["target_total_chars"] = raw_total_chars
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
        self._planning_thread: Optional[threading.Thread] = None
        interrupted = self.db.fail_interrupted_planning_sessions()
        if interrupted > 0:
            logger.warning(
                "Marked %d orphaned PLANNING wizard session(s) as FAILED on startup",
                interrupted,
            )

    # ---------------------------------------------------------------- LLM
    def _propose(self, idea: str, step_index: int, choices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        step = WIZARD_STEPS[step_index]
        system_prompt = (
            "你是中文网文立项策划。老板会一步一步做选择，你每一步只给出恰好 3 个差异明显的候选方案。"
            "严格输出单个 JSON 对象：{\"options\": [ ... 3 个对象 ... ]}，不要输出任何其他文字。"
            "候选必须与老板已做的选择保持一致，不得推翻前面的选择。"
        )
        field_lines = "\n".join(f"  - {k}: {v}" for k, v in step["fields"].items())
        optional_fields = step.get("optional_fields") or {}
        optional_block = ""
        if optional_fields:
            optional_lines = "\n".join(f"  - {k}: {v}" for k, v in optional_fields.items())
            optional_block = f"推荐提供的扩展字段（提供时须严格满足类型和范围校验）：\n{optional_lines}\n"
        catalog = ""
        if step["key"] == "direction":
            catalog = "可选类目（genre_id 只能取这里的值）：\n" + json.dumps(
                _genre_catalog(), ensure_ascii=False
            ) + "\n"
        elif step["key"] == "scale":
            scale_lines: List[str] = [
                "【高点击书名与目标总字数规划硬要求】：",
                "1. 篇幅必须按目标总字数规划，严禁设计无结局的无限续卷：3 个候选请给出差异化的目标总字数方案（例如 80 万字 / 120 万字 / 150 万字有清晰大结局的完整长篇，对应 author_level='lv2_plus'、target_chapters=0；最多 1 个备选为 15~20 万字紧凑完本，对应 author_level='lv0_1'、target_chapters 为 70~95），并请填写正整数 target_total_chars（>= 100000，例如 800000、1200000、1500000、180000）。",
                "2. 3 个候选书名必须各自采用不同高点击商业套路：",
                "   - 方案 1（强反差/破格打脸型）：把日常身份与离谱战绩直接撞在一起（例：让你算命，你全网连麦抓通缉犯？）；",
                "   - 方案 2（核心金手指/利益直给型）：一眼亮出绝活与爽感回报（例：一卦千金：首富跪求我别闭麦）；",
                "   - 方案 3（身份错位/禁忌悬念型）：用禁忌反差制造强好奇（例：下山摆摊算卦，巡捕房连夜借人）。",
                "3. 严禁起《三点十七分》《白雾渡口》《雨夜孤灯》这类纯时间、纯地名、纯景物或抽象文艺短名！每个书名必须一眼看出核心看点，7~15 字为佳，严禁超过 15 字，严禁书名号《》。",
            ]
            chosen_genre_id = ""
            for c in choices:
                if c.get("step") == "direction" and isinstance(c.get("option"), Mapping):
                    chosen_genre_id = str(c["option"].get("genre_id") or "").strip()
            if chosen_genre_id:
                try:
                    recipe = get_genre(chosen_genre_id)
                except UnknownGenreError:
                    recipe = None
                if recipe is not None:
                    if recipe.title_templates:
                        scale_lines.append("4. 当前赛道爆款句式参考：" + " ｜ ".join(recipe.title_templates))
                    if recipe.title_examples:
                        scale_lines.append("5. 同赛道书名结构范例（严禁照抄）：" + " / ".join(recipe.title_examples))
            catalog = "\n".join(scale_lines) + "\n"
        user_prompt = (
            f"老板的原始题材想法：{idea}\n"
            f"老板已经做的选择：\n{_choices_text(choices)}\n"
            f"当前这一步：{step['title']} —— {step['question']}\n"
            f"{catalog}"
            f"每个候选对象必须包含以下字段：\n{field_lines}\n"
            f"{optional_block}"
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
    @staticmethod
    def _format_view(s: Mapping[str, Any]) -> Dict[str, Any]:
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
            "progress": s["progress"],
            "error_message": s["error_message"],
        }

    def _view(self, session_id: str) -> Dict[str, Any]:
        return self._format_view(self.db.get_wizard_session(session_id))

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
            if step["key"] == "scale" and body.get("title_override") is not None:
                override_title = str(body.get("title_override") or "").strip()
                if override_title:
                    if not (1 <= len(override_title) <= 15) or "《" in override_title or "》" in override_title:
                        raise WizardError(
                            f"scale: title_override {override_title!r} must be 1~15 chars without 《》"
                        )
                    chosen["title"] = override_title
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

    def _begin_planning(self, session_id: str) -> Dict[str, Any]:
        initial_progress = {"stage": "bible", "batch": 1, "batches": 1}
        code, session = self.db.try_start_wizard_planning(
            session_id, initial_progress=initial_progress
        )
        if code == "BUSY_SELF":
            raise WizardConflictError("该会话正在规划中，请勿重复确认")
        if code == "BUSY_OTHER":
            raise WizardConflictError("已有另一本书正在规划中，请等当前规划完成后再确认")
        if code.startswith("INVALID_STATUS:"):
            st = code.split(":", 1)[1]
            raise WizardError(f"会话状态为 {st}，四步都选完才能确认开写")
        assert session is not None
        return session

    def _build_create_request(self, s: Mapping[str, Any], pen_name: str) -> AutoCreateBookRequest:
        by_step = {c["step"]: c["option"] for c in s["choices"]}
        direction = by_step["direction"]
        scale = by_step["scale"]
        sid_suffix = str(s["session_id"])[:6]
        project_id = f"book_{self._now_fn().strftime('%Y%m%d_%H%M%S')}_{sid_suffix}"
        target_total_line = (
            f"目标总字数：{scale['target_total_chars']} 字\n"
            if "target_total_chars" in scale
            else ""
        )
        brief = (
            f"原始题材：{s['idea']}\n"
            f"书名必须为：{scale['title']}\n"
            f"{target_total_line}"
            f"简介方向：{scale['blurb']}\n"
            "人名要求：向导中出现的人物姓名只是示意，正式姓名必须从推荐安全角色名池中选取，"
            "其余主角设定（身份、性格、金手指、代价、成长线）必须保留。\n"
            f"{_choices_text(s['choices'])}"
        )
        target = int(scale["target_chapters"]) if scale["author_level"] == "lv0_1" else None
        return AutoCreateBookRequest(
            project_id=project_id,
            genre_id=direction["genre_id"],
            author_level=scale["author_level"],
            pen_name=pen_name.strip(),
            target_chapters=target,
            creative_brief=brief,
        )

    def _invoke_create_book(
        self,
        req: AutoCreateBookRequest,
        on_progress: Callable[[Dict[str, Any]], None],
    ) -> Any:
        sig = inspect.signature(self._create_book_fn)
        accepts_progress = "on_progress" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if accepts_progress:
            return self._create_book_fn(req, on_progress=on_progress)
        return self._create_book_fn(req)

    def _run_planning(
        self,
        session_id: str,
        session_snapshot: Dict[str, Any],
        pen_name: str,
        *,
        reraise: bool,
    ) -> None:
        def _record_progress(prog: Dict[str, Any]) -> None:
            self.db.update_wizard_progress(session_id, prog)

        try:
            req = self._build_create_request(session_snapshot, pen_name)
            summary = self._invoke_create_book(req, _record_progress)
            book_id = getattr(summary, "book_id", "") or req.project_id
            self.db.update_wizard_session(
                session_id,
                status=STATUS_CREATED,
                step_index=session_snapshot["step_index"],
                choices=session_snapshot["choices"],
                options=[],
                book_id=book_id,
                error_message="",
                progress={"stage": "done"},
            )
        except Exception as exc:
            logger.exception("Wizard planning failed for session %s: %s", session_id, exc)
            self.db.update_wizard_session(
                session_id,
                status=STATUS_FAILED,
                step_index=session_snapshot["step_index"],
                choices=session_snapshot["choices"],
                options=[],
                book_id="",
                error_message=str(exc),
            )
            if reraise:
                raise

    def confirm(self, session_id: str, *, pen_name: str = "") -> Dict[str, Any]:
        """Run book planning synchronously and return the final session view."""
        snap = self._begin_planning(session_id)
        self._run_planning(session_id, snap, pen_name, reraise=True)
        return self._view(session_id)

    def confirm_async(self, session_id: str, *, pen_name: str = "") -> Dict[str, Any]:
        """Transition session to PLANNING and run staged book planning in a background thread."""
        snap = self._begin_planning(session_id)
        accepted_view = self._format_view(snap)
        worker = threading.Thread(
            target=self._run_planning,
            args=(session_id, snap, pen_name),
            kwargs={"reraise": False},
            name=f"wizard-plan-{session_id[:8]}",
            daemon=True,
        )
        self._planning_thread = worker
        worker.start()
        return accepted_view

    def close(self, timeout: float = 5.0) -> None:
        """Wait for any in-flight background planning thread to finish (idempotent)."""
        worker = self._planning_thread
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=timeout)
        if worker is not None and not worker.is_alive():
            self._planning_thread = None


__all__ = ["MIN_TARGET_TOTAL_CHARS", "NewBookWizard", "WizardConflictError", "WizardError", "WIZARD_STEPS"]
