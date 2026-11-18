from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import uuid
import yaml

from .deconstructor import BestsellerDeconstructor, DeconstructedBlueprint
from .evolution import (
    MotifWeightEntry,
    MotifWeightStore,
    OptimizationReport,
    OutlineAdjustmentDirective,
    assert_chapter_mutable,
)
from .genres import GenreRecipe, get_genre
from .llm.base import LLMConfigurationError, LLMProvider, LLMRequest, LLMResponse
from .llm.router import RoleModelRouter
from .loader import load_project
from .models import (
    AUTHOR_LEVELS,
    EMOTION_POLARITIES,
    SHANGHAI_TZ,
    NovelProject,
    ProjectValidationError,
    VolumeCapLock,
    WorldStateSnapshot,
)
from .namer import CharacterNamer, NamerError
from .plagiarism_guard import (
    CrossBookCollisionError,
    PlagiarismAndCollisionGuard,
    PlagiarismViolationError,
)
from .sanitizers import InvalidTweetAliasError, validate_tweet_alias


class PlannerError(RuntimeError):
    """Base error for AutoNovelPlanner."""


class PlanningValidationError(PlannerError, ProjectValidationError):
    """Raised when generated project/volume YAML fails load_project() validation after max retries."""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<body>[\s\S]*?)\s*```", re.IGNORECASE)
_CHAPTER_PREFIX_RE = re.compile(r"^\s*第\s*[0-9零一二三四五六七八九十百千万]+\s*[章回节]\s*[:：\-—\s]*")


def _parse_json_payload(raw_text: str, stage: str) -> Dict[str, Any]:
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ProjectValidationError(f"{stage}: LLM returned empty content")
    text = raw_text.strip()
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group("body").strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectValidationError(f"{stage}: invalid JSON from planner LLM ({exc})") from exc
    if not isinstance(parsed, dict):
        raise ProjectValidationError(f"{stage}: expected JSON object, got {type(parsed).__name__}")
    return parsed


def _invoke_planner(
    provider: LLMProvider | RoleModelRouter,
    request: LLMRequest,
) -> LLMResponse:
    if isinstance(provider, RoleModelRouter):
        return provider.generate("planner", request)
    if isinstance(provider, LLMProvider):
        return provider.generate(request)
    raise LLMConfigurationError(
        f"provider must be RoleModelRouter or LLMProvider, got {type(provider).__name__}"
    )


def _dump_yaml(data: Mapping[str, Any]) -> str:
    return yaml.safe_dump(
        dict(data),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _archive_existing_file(path: Path, project_root: Path, stamp: str) -> None:
    if not path.exists():
        return
    try:
        rel = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        rel = Path(path.name)
    archive_target = project_root / "_archive" / stamp / rel
    archive_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(archive_target))


def _write_text_archived(path: Path, content: str, project_root: Path, stamp: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _archive_existing_file(path, project_root, stamp)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8", newline="\n")
    tmp_path.replace(path)


def _strip_chapter_prefix(title: str) -> str:
    cleaned = _CHAPTER_PREFIX_RE.sub("", str(title or "").strip()).strip()
    return cleaned or str(title or "").strip()


@dataclass
class PlannedProjectResult:
    project_dir: Path
    project: NovelProject
    contract_track: str
    author_level: str
    opening_motif: str
    golden_finger_type: str
    attempts_used: int
    signing_application_path: Optional[Path] = None
    master_outline_path: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_dir": str(self.project_dir),
            "project_id": self.project.meta.project_id,
            "title": self.project.meta.title,
            "contract_track": self.contract_track,
            "author_level": self.author_level,
            "opening_motif": self.opening_motif,
            "golden_finger_type": self.golden_finger_type,
            "attempts_used": self.attempts_used,
            "scene_count": len(self.project.scenes),
            "signing_application_path": (
                str(self.signing_application_path) if self.signing_application_path else None
            ),
            "master_outline_path": (
                str(self.master_outline_path) if self.master_outline_path else None
            ),
        }


@dataclass
class VolumeExtensionResult:
    project_dir: Path
    project: NovelProject
    volume_index: int
    start_chapter: int
    end_chapter: int
    triggered_map_change: bool
    new_map_name: str
    new_map_location_id: str
    soft_identity_reboot: str
    new_volume_cap_locks: List[VolumeCapLock]
    volume_file_path: Path
    attempts_used: int


@dataclass
class FinaleVolumeResult:
    project_dir: Path
    project: NovelProject
    volume_index: int
    start_chapter: int
    end_chapter: int
    resolved_foreshadow_ids: List[str]
    fulfilled_expectation_ids: List[str]
    volume_file_path: Path
    attempts_used: int


class AutoNovelPlanner:
    """3-source auto-initiation, dual-track signing, rolling map-change continuation, and finale wrap-up."""

    def __init__(
        self,
        provider: LLMProvider | RoleModelRouter,
        *,
        guard: Optional[PlagiarismAndCollisionGuard] = None,
        deconstructor: Optional[BestsellerDeconstructor] = None,
        max_validation_retries: int = 3,
    ) -> None:
        if provider is None:
            raise LLMConfigurationError("AutoNovelPlanner requires a configured LLM provider/router")
        if max_validation_retries < 1:
            raise ValueError(f"max_validation_retries must be >= 1, got {max_validation_retries}")
        self.provider = provider
        self.guard = guard or PlagiarismAndCollisionGuard()
        self.deconstructor = deconstructor or BestsellerDeconstructor(provider, guard=self.guard)
        self.max_validation_retries = max_validation_retries

    @staticmethod
    def _build_signing_application_md(
        meta: Dict[str, Any],
        characters: Sequence[Mapping[str, Any]],
        scenes: Sequence[Mapping[str, Any]],
        master_outline: Sequence[Mapping[str, Any]],
        recipe: GenreRecipe,
        golden_finger_desc: str,
    ) -> str:
        pov_char = next(
            (c for c in characters if c.get("id") == meta.get("default_pov_character")),
            characters[0] if characters else {},
        )
        antagonist_lines = [
            f"- {c.get('name')}：{c.get('role')}（{c.get('personality')}）"
            for c in characters
            if c.get("id") != pov_char.get("id")
        ]
        golden_three = []
        for s in scenes[:3]:
            events_txt = "；".join(str(e) for e in s.get("events", []))
            golden_three.append(
                f"- 第{s.get('chapter')}章《{s.get('public_title') or s.get('internal_title')}》"
                f"（{s.get('beat_type', '推进')}）：{events_txt}｜章末钩子：{s.get('cliffhanger_hook', '')}"
            )
        debut_summary = []
        for s in scenes[3:10]:
            debut_summary.append(
                f"- 第{s.get('chapter')}章 {s.get('public_title') or s.get('internal_title')}：{s.get('stop_condition', '')}"
            )
        milestone_lines = []
        for item in master_outline:
            ch = item.get("chapter")
            if ch in (1, 15, 30, 60, 80, 100) or len(master_outline) <= 10:
                milestone_lines.append(f"- 第{ch}章：{item.get('title', '')} — {item.get('summary', '')}")

        alt_titles = " / ".join(str(t) for t in meta.get("alternate_titles", []))
        est_debut_words = sum(int((int(s.get("min_chars", 2050)) + int(s.get("max_chars", 2300))) // 2) for s in scenes[:10])
        return "\n".join(
            [
                f"# 番茄小说责编直签申请表 — 《{meta.get('title')}》",
                "",
                "## 一、作品基础信息与备选包装",
                f"- 书名：{meta.get('title')}",
                f"- 备选书名池：{alt_titles}",
                f"- 作者笔名：{meta.get('pen_name') or meta.get('author_name')}",
                f"- 频道与主分类：{recipe.channel} / {meta.get('primary_category') or recipe.tomato_primary_category}",
                f"- 标签：{', '.join(meta.get('tags', []))}",
                f"- 签约轨道：{meta.get('contract_track')}（作者等级：{meta.get('author_level')}）",
                "",
                "## 二、核心卖点与金手指机制",
                f"- 作品简介：\n{meta.get('synopsis', '').strip()}",
                f"- 核心金手指与升级机制：{golden_finger_desc}",
                "",
                "## 三、主角人设与核心矛盾",
                f"- 主角：{pov_char.get('name', '')} — {pov_char.get('role', '')}",
                f"- 性格锚点：{pov_char.get('personality', '')}",
                "- 核心关系网与主要冲突方：",
                *(antagonist_lines or ["- 见 characters.yaml"]),
                "",
                "## 四、黄金三章与前两万字剧情节奏大纲",
                "### 黄金前三章核心节拍",
                *golden_three,
                "### 第4～10章（前2万字直签期）节奏推进",
                *(debut_summary or ["- 按首秀15章节奏推进，确保第10章完成首轮小高潮兑现。"]),
                "",
                "## 五、百章总纲与长篇换地图升级路线",
                *milestone_lines,
                "",
                "## 六、首秀包字数与稳定更新承诺",
                f"- 首卷已规划章数：{len(scenes)} 章（前10章约 {est_debut_words} 字，首秀15章约 {len(scenes) * 2200} 字）",
                f"- 规划目标总字数：{meta.get('target_word_count')} 字",
                "- 更新承诺：每日稳定双更（4400+字/日），开书即自带10章以上安全垫存稿，严守不断更承诺。",
                "",
            ]
        )

    def _validate_and_write_staging(
        self,
        staging_dir: Path,
        *,
        payload: Mapping[str, Any],
        project_id: str,
        recipe: GenreRecipe,
        author_level: str,
        contract_track: str,
        pen_name: str,
        target_chapters: int,
        benchmark_chapters: Optional[Sequence[str]],
        projects_root: Optional[Path],
        namer: CharacterNamer,
    ) -> Tuple[NovelProject, Dict[str, Any]]:
        """Validate LLM JSON payload strictly, write YAML into staging_dir, and run load_project()."""
        raw_meta = payload.get("project")
        if not isinstance(raw_meta, Mapping):
            raise ProjectValidationError("Missing 'project' object in planner payload")

        title = str(raw_meta.get("title") or "").strip()
        alt_titles = [str(t).strip() for t in (raw_meta.get("alternate_titles") or []) if str(t).strip()]
        if len(alt_titles) != 5:
            raise ProjectValidationError(
                f"project.alternate_titles must contain exactly 5 titles, got {len(alt_titles)}"
            )
        alt_blurbs = [str(b).strip() for b in (raw_meta.get("alternate_blurbs") or []) if str(b).strip()]
        if len(alt_blurbs) != 3:
            raise ProjectValidationError(
                f"project.alternate_blurbs must contain exactly 3 blurbs, got {len(alt_blurbs)}"
            )

        tweet_aliases = [str(a).strip() for a in (raw_meta.get("tweet_aliases") or []) if str(a).strip()]
        if not tweet_aliases:
            raise ProjectValidationError("project.tweet_aliases must contain at least 1 validated alias")
        for alias in tweet_aliases:
            try:
                validate_tweet_alias(alias)
            except InvalidTweetAliasError as exc:
                raise ProjectValidationError(f"Invalid tweet_alias {alias!r}: {exc}") from exc

        # Validate characters via T03 CharacterNamer (zero AI names, zero homophones, common hanzi)
        raw_chars = payload.get("characters")
        if not isinstance(raw_chars, list) or not raw_chars:
            raise ProjectValidationError("Missing non-empty 'characters' list in planner payload")
        cast_namer = CharacterNamer()
        validated_chars: List[Dict[str, Any]] = []
        for ch_item in raw_chars:
            if not isinstance(ch_item, Mapping):
                raise ProjectValidationError("Each character entry must be a mapping")
            cname = str(ch_item.get("name") or "").strip()
            try:
                cast_namer.register_name(cname)
            except NamerError as exc:
                raise ProjectValidationError(f"Character name rejected by CharacterNamer: {exc}") from exc
            validated_chars.append(dict(ch_item))

        raw_assets = payload.get("assets")
        if not isinstance(raw_assets, list) or not raw_assets:
            raise ProjectValidationError("Missing non-empty 'assets' list in planner payload")

        raw_foreshadows = payload.get("foreshadows")
        if not isinstance(raw_foreshadows, list) or not raw_foreshadows:
            raise ProjectValidationError("Missing non-empty 'foreshadows' list in planner payload")

        raw_styles = list(payload.get("style_profiles") or [])
        style_ids = {str(sp.get("id")) for sp in raw_styles if isinstance(sp, Mapping) and sp.get("id")}
        if recipe.style_profile.id not in style_ids:
            raw_styles.insert(0, recipe.style_profile.to_dict())

        raw_locks = payload.get("volume_cap_locks")
        if not isinstance(raw_locks, list) or not raw_locks:
            raise ProjectValidationError("Missing non-empty 'volume_cap_locks' list in planner payload")
        parsed_locks = [VolumeCapLock.from_dict(lk) for lk in raw_locks]
        if not any(lk.volume == 1 for lk in parsed_locks):
            raise ProjectValidationError("volume_cap_locks must define at least one lock for volume=1")

        raw_scenes = payload.get("scenes")
        if not isinstance(raw_scenes, list) or not raw_scenes:
            raise ProjectValidationError("Missing non-empty 'scenes' list in planner payload")

        if author_level == "lv0_1":
            if not (70 <= len(raw_scenes) <= 95):
                raise ProjectValidationError(
                    f"lv0_1 complete_150k track requires 70..95 chapters in vol_01, got {len(raw_scenes)}"
                )
        elif author_level == "lv2_plus":
            if len(raw_scenes) != 15:
                raise ProjectValidationError(
                    f"lv2_plus serial_20k debut package requires exactly 15 chapters in vol_01, got {len(raw_scenes)}"
                )
            master_outline = payload.get("master_outline")
            if not isinstance(master_outline, list) or len(master_outline) != 100:
                got_len = len(master_outline) if isinstance(master_outline, list) else 0
                raise ProjectValidationError(
                    f"lv2_plus track requires 100-chapter master_outline, got {got_len}"
                )

        normalized_scenes: List[Dict[str, Any]] = []
        for s_item in raw_scenes:
            if not isinstance(s_item, Mapping):
                raise ProjectValidationError("Each scene entry must be a mapping")
            s_dict = dict(s_item)
            if s_dict.get("public_title"):
                s_dict["public_title"] = _strip_chapter_prefix(str(s_dict["public_title"]))
            s_dict.setdefault("volume", 1)
            s_dict.setdefault("min_chars", recipe.min_chapter_words)
            s_dict.setdefault("max_chars", recipe.max_chapter_words)
            normalized_scenes.append(s_dict)

        opening_motif = str(raw_meta.get("opening_motif") or "").strip()
        golden_finger_type = str(raw_meta.get("golden_finger_type") or "").strip()
        golden_finger_desc = str(
            raw_meta.get("golden_finger_description") or golden_finger_type
        ).strip()

        target_word_count = int(raw_meta.get("target_word_count") or 0)
        if author_level == "lv0_1":
            if target_word_count == 0:
                target_word_count = len(normalized_scenes) * 2200
            if not (150_000 <= target_word_count <= 200_000):
                raise ProjectValidationError(
                    f"lv0_1 target_word_count must be in 150000..200000, got {target_word_count}"
                )
        else:
            if target_word_count <= 0:
                target_word_count = 1_000_000

        initial_balances = {
            str(k): float(v)
            for k, v in dict(raw_meta.get("initial_balances") or {}).items()
        }

        project_yaml_data: Dict[str, Any] = {
            "project_id": project_id,
            "title": title,
            "genre": recipe.display_name,
            "genre_id": recipe.genre_id,
            "synopsis": str(raw_meta.get("synopsis") or "").strip(),
            "default_pov_character": str(raw_meta.get("default_pov_character") or "").strip(),
            "max_rewrite_retries": int(raw_meta.get("max_rewrite_retries", 3)),
            "author_level": author_level,
            "contract_track": contract_track,
            "pov_mode": str(raw_meta.get("pov_mode") or recipe.pov_mode),
            "panel_policy": str(raw_meta.get("panel_policy") or recipe.panel_policy),
            "pen_name": pen_name,
            "author_name": pen_name,
            "primary_category": str(
                raw_meta.get("primary_category") or recipe.tomato_primary_category
            ),
            "tags": list(raw_meta.get("tags") or recipe.tomato_tags),
            "alternate_titles": alt_titles,
            "alternate_blurbs": alt_blurbs,
            "tweet_aliases": tweet_aliases,
            "target_word_count": target_word_count,
            "signed": False,
            "max_published_chapter": 0,
            "volume_index": 1,
            "is_final_volume": False,
            "opening_motif": opening_motif,
            "golden_finger_type": golden_finger_type,
            "golden_finger_description": golden_finger_desc,
            "initial_balances": initial_balances,
            "volume_cap_locks": [lk.to_dict() for lk in parsed_locks],
        }

        # Plagiarism & Cross-book collision checks BEFORE persisting
        all_outline_text_parts = [
            title,
            *alt_titles,
            project_yaml_data["synopsis"],
            *alt_blurbs,
            golden_finger_desc,
        ]
        for s_dict in normalized_scenes:
            all_outline_text_parts.append(str(s_dict.get("public_title", "")))
            all_outline_text_parts.append(str(s_dict.get("internal_title", "")))
            all_outline_text_parts.extend(str(e) for e in s_dict.get("events", []))
            all_outline_text_parts.append(str(s_dict.get("stop_condition", "")))
        all_outline_text = "\n".join(p for p in all_outline_text_parts if p)

        if benchmark_chapters:
            self.guard.check_against_benchmark(
                all_outline_text, benchmark_chapters, raise_on_violation=True
            )

        if projects_root is not None:
            lead_char = next(
                (c for c in validated_chars if c.get("id") == project_yaml_data["default_pov_character"]),
                validated_chars[0],
            )
            opening_ch_texts = [
                " ".join(
                    [
                        str(s.get("public_title", "")),
                        str(s.get("internal_title", "")),
                        " ".join(str(e) for e in s.get("events", [])),
                    ]
                )
                for s in normalized_scenes[:3]
            ]
            self.guard.check_cross_book_collision(
                title=title,
                alternate_titles=alt_titles,
                projects_dir=projects_root,
                genre_id=recipe.genre_id,
                golden_finger=golden_finger_desc or golden_finger_type,
                opening_summary=project_yaml_data["synopsis"],
                opening_chapters=opening_ch_texts,
                protagonist_persona=f"{lead_char.get('role', '')} {lead_char.get('personality', '')}",
                exclude_project_id=project_id,
                raise_on_collision=True,
            )

        # Write YAML files into staging directory and validate strictly via T01 load_project()
        (staging_dir / "outline").mkdir(parents=True, exist_ok=True)
        (staging_dir / "project.yaml").write_text(
            _dump_yaml(project_yaml_data), encoding="utf-8", newline="\n"
        )
        (staging_dir / "characters.yaml").write_text(
            _dump_yaml({"characters": validated_chars}), encoding="utf-8", newline="\n"
        )
        (staging_dir / "assets.yaml").write_text(
            _dump_yaml({"assets": list(raw_assets)}), encoding="utf-8", newline="\n"
        )
        (staging_dir / "foreshadows.yaml").write_text(
            _dump_yaml({"foreshadows": list(raw_foreshadows)}), encoding="utf-8", newline="\n"
        )
        (staging_dir / "style_profiles.yaml").write_text(
            _dump_yaml({"style_profiles": raw_styles}), encoding="utf-8", newline="\n"
        )
        (staging_dir / "outline" / "vol_01.yaml").write_text(
            _dump_yaml(
                {
                    "volume": 1,
                    "is_final_volume": False,
                    "volume_cap_locks": [lk.to_dict() for lk in parsed_locks],
                    "scenes": normalized_scenes,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )

        loaded = load_project(staging_dir)
        prepared = {
            "project_yaml": project_yaml_data,
            "characters_yaml": {"characters": validated_chars},
            "assets_yaml": {"assets": list(raw_assets)},
            "foreshadows_yaml": {"foreshadows": list(raw_foreshadows)},
            "style_profiles_yaml": {"style_profiles": raw_styles},
            "vol_01_yaml": {
                "volume": 1,
                "is_final_volume": False,
                "volume_cap_locks": [lk.to_dict() for lk in parsed_locks],
                "scenes": normalized_scenes,
            },
            "master_outline": list(payload.get("master_outline") or []),
            "golden_finger_description": golden_finger_desc,
        }
        return loaded, prepared

    def plan_new_project(
        self,
        *,
        project_id: str,
        genre_id: str,
        output_dir: str | Path,
        author_level: str = "lv0_1",
        projects_root: Optional[str | Path] = None,
        pen_name: str = "砚山行",
        hot_keywords: Sequence[str] = (),
        motif_store: Optional[MotifWeightStore] = None,
        motif_candidates: Optional[Sequence[Tuple[str, str]]] = None,
        blueprint: Optional[DeconstructedBlueprint] = None,
        benchmark_chapters: Optional[Sequence[str]] = None,
        target_chapters: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> PlannedProjectResult:
        """3-source auto-initiation producing a load_project()-validated dual-track project package."""
        if not isinstance(project_id, str) or not project_id.strip():
            raise ProjectValidationError("project_id must be a non-empty string")
        if author_level not in AUTHOR_LEVELS:
            raise ProjectValidationError(
                f"Unknown author_level {author_level!r}; expected one of {AUTHOR_LEVELS}"
            )
        recipe = get_genre(genre_id)
        contract_track = "complete_150k" if author_level == "lv0_1" else "serial_20k"

        resolved_chapters: int
        if author_level == "lv0_1":
            resolved_chapters = 80 if target_chapters is None else int(target_chapters)
            if not (70 <= resolved_chapters <= 95):
                raise ProjectValidationError(
                    f"lv0_1 target_chapters must be in 70..95, got {resolved_chapters}"
                )
        else:
            resolved_chapters = 15

        # Source 1: Bayesian motif weights from T09
        sampled_motifs: List[MotifWeightEntry] = []
        if motif_store is not None:
            sampled_motifs = motif_store.sample_top_motifs(
                recipe.genre_id,
                top_k=3,
                candidates=motif_candidates,
                mode="deterministic",
            )

        # Source 3: Bestseller deconstruction if benchmark chapters supplied without blueprint
        effective_blueprint = blueprint
        if effective_blueprint is None and benchmark_chapters:
            motif_hint = sampled_motifs[0].opening_motif if sampled_motifs else ""
            effective_blueprint = self.deconstructor.deconstruct(
                benchmark_chapters,
                target_genre_id=recipe.genre_id,
                hot_keywords=hot_keywords,
                motif_hint=motif_hint,
            )
        elif effective_blueprint is not None and not effective_blueprint.sanitized:
            if benchmark_chapters:
                bp_report = self.guard.check_against_benchmark(
                    effective_blueprint.to_summary_text(),
                    benchmark_chapters,
                    raise_on_violation=True,
                )
                effective_blueprint = replace(
                    effective_blueprint,
                    sanitized=True,
                    plagiarism_report=bp_report,
                )
            else:
                raise PlanningValidationError(
                    "External blueprint must have sanitized=True unless benchmark_chapters are supplied for plagiarism verification"
                )

        # Pre-generate clean cast using T03 CharacterNamer
        namer = CharacterNamer()
        suggested_cast = namer.generate_cast(
            count=5,
            era=recipe.genre_id,
            channel=recipe.channel,
            seed=seed,
        )

        motif_lines = [
            f"- {m.opening_motif} | 金手指={m.golden_finger_type} (后验胜率={m.posterior_mean:.3f})"
            for m in sampled_motifs
        ]
        blueprint_block = (
            json.dumps(effective_blueprint.to_dict(), ensure_ascii=False, indent=2)
            if effective_blueprint is not None
            else "无对标拆书蓝图（按本赛道配方与热榜关键词自主立项）"
        )

        track_instruction = (
            f"账号轨=lv0_1（complete_150k）：请在 scenes 中完整规划 {resolved_chapters} 章三幕紧凑完本大纲（15万~20万字，target_word_count 在 150000..200000 之间）。"
            if author_level == "lv0_1"
            else "账号轨=lv2_plus（serial_20k）：请在 scenes 中规划第1卷前 15 章首秀测书细纲（约3.3万字），并额外输出 master_outline（恰好100章总纲数组，每项含 chapter, title, summary）。"
        )

        system_prompt = (
            "你是番茄爆款总策划师。请严格输出单个 JSON 对象，生成可直接落盘通过 schema 校验的完整立项包。\n"
            "硬性约束：\n"
            "1. project.title 与 5 个 project.alternate_titles 均须为 1~15 个字符，严禁包含书名号《》。\n"
            "2. project.alternate_blurbs 必须恰好包含 3 版三段式简介；project.tweet_aliases 含 3 个 4~8 字纯简体常用汉字别名。\n"
            "3. 所有角色姓名必须严格使用推荐的无撞音、非AI黑名单常用简体中文姓名，严禁出现林晚/顾寒洲/陆沉/苏清浅等黑名单人名，严禁同音名。\n"
            "4. scenes 中连续纯压抑 emotion_polarity='suppress' 绝对不超过 2 章；public_title 不得带 '第X章' 前缀。\n"
            "5. volume_cap_locks 必须包含第1卷数值上限锁（如 ledger_key, volume: 1, max_value, unit）。\n"
            "6. JSON 顶层字段：project, characters, assets, foreshadows, style_profiles, volume_cap_locks, scenes"
            + (", master_outline" if author_level == "lv2_plus" else "")
            + "。"
        )

        feedback = ""
        last_error: Optional[Exception] = None
        target_dir = Path(output_dir).resolve()
        projects_root_path = Path(projects_root).resolve() if projects_root is not None else None

        for attempt in range(1, self.max_validation_retries + 1):
            feedback_section = (
                f"\n[第 {attempt - 1} 次校验失败，必须严格修复以下错误，禁止重复犯错]：\n{feedback}\n"
                if feedback
                else ""
            )
            user_prompt = (
                f"project_id: {project_id}\n"
                f"类目: {recipe.genre_id}（{recipe.display_name}，主分类={recipe.tomato_primary_category}，频道={recipe.channel}）\n"
                f"默认视角: {recipe.pov_mode} | 面板策略: {recipe.panel_policy}\n"
                f"黄金单章字数: {recipe.min_chapter_words}~{recipe.max_chapter_words}\n"
                f"样式模板ID（可直接引用）: {recipe.style_profile.id}\n"
                f"推荐安全角色名池（严禁同音或黑名单名）: {', '.join(suggested_cast)}\n"
                f"贝叶斯高权重母题候选:\n{chr(10).join(motif_lines) if motif_lines else '无历史样本，按类目最佳实践'}\n"
                f"热榜风向关键词: {', '.join(hot_keywords) if hot_keywords else '无'}\n"
                f"对标拆书换皮蓝图:\n{blueprint_block}\n"
                f"{track_instruction}\n"
                f"{feedback_section}"
            )
            req = LLMRequest(
                request_id=f"plan-new-{project_id}-{attempt}-{uuid.uuid4().hex[:6]}",
                chapter=0,
                attempt=attempt,
                purpose="plan",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.5,
                max_tokens=12000,
                metadata={
                    "stage": "plan_new_project",
                    "project_id": project_id,
                    "genre_id": recipe.genre_id,
                    "author_level": author_level,
                    "contract_track": contract_track,
                    "target_chapters": resolved_chapters,
                    "suggested_cast": suggested_cast,
                    "attempt": attempt,
                },
            )
            resp = _invoke_planner(self.provider, req)
            try:
                payload = _parse_json_payload(resp.content, "plan_new_project")
                with tempfile.TemporaryDirectory(prefix="xsb_plan_") as tmpdir:
                    loaded, prepared = self._validate_and_write_staging(
                        Path(tmpdir),
                        payload=payload,
                        project_id=project_id,
                        recipe=recipe,
                        author_level=author_level,
                        contract_track=contract_track,
                        pen_name=pen_name,
                        target_chapters=resolved_chapters,
                        benchmark_chapters=benchmark_chapters,
                        projects_root=projects_root_path,
                        namer=namer,
                    )
            except (
                ProjectValidationError,
                NamerError,
                InvalidTweetAliasError,
                PlagiarismViolationError,
                CrossBookCollisionError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                last_error = exc
                feedback = str(exc)
                continue

            # Persist validated project into output_dir (archive any existing files into _archive/)
            stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S_%f")
            target_dir.mkdir(parents=True, exist_ok=True)
            _write_text_archived(
                target_dir / "project.yaml",
                _dump_yaml(prepared["project_yaml"]),
                target_dir,
                stamp,
            )
            _write_text_archived(
                target_dir / "characters.yaml",
                _dump_yaml(prepared["characters_yaml"]),
                target_dir,
                stamp,
            )
            _write_text_archived(
                target_dir / "assets.yaml",
                _dump_yaml(prepared["assets_yaml"]),
                target_dir,
                stamp,
            )
            _write_text_archived(
                target_dir / "foreshadows.yaml",
                _dump_yaml(prepared["foreshadows_yaml"]),
                target_dir,
                stamp,
            )
            _write_text_archived(
                target_dir / "style_profiles.yaml",
                _dump_yaml(prepared["style_profiles_yaml"]),
                target_dir,
                stamp,
            )
            _write_text_archived(
                target_dir / "outline" / "vol_01.yaml",
                _dump_yaml(prepared["vol_01_yaml"]),
                target_dir,
                stamp,
            )

            master_outline_path: Optional[Path] = None
            signing_app_path: Optional[Path] = None
            if author_level == "lv2_plus":
                master_outline_path = target_dir / "master_outline.yaml"
                _write_text_archived(
                    master_outline_path,
                    _dump_yaml({"master_outline": prepared["master_outline"]}),
                    target_dir,
                    stamp,
                )
                signing_app_path = target_dir / "signing_application.md"
                signing_md = self._build_signing_application_md(
                    prepared["project_yaml"],
                    prepared["characters_yaml"]["characters"],
                    prepared["vol_01_yaml"]["scenes"],
                    prepared["master_outline"],
                    recipe,
                    prepared["golden_finger_description"],
                )
                _write_text_archived(signing_app_path, signing_md, target_dir, stamp)

            final_loaded = load_project(target_dir)
            return PlannedProjectResult(
                project_dir=target_dir,
                project=final_loaded,
                contract_track=contract_track,
                author_level=author_level,
                opening_motif=prepared["project_yaml"]["opening_motif"],
                golden_finger_type=prepared["project_yaml"]["golden_finger_type"],
                attempts_used=attempt,
                signing_application_path=signing_app_path,
                master_outline_path=master_outline_path,
            )

        raise PlanningValidationError(
            f"plan_new_project failed load_project() validation after {self.max_validation_retries} attempts: "
            f"{last_error}"
        ) from last_error

    def extend_volume(
        self,
        project_dir: str | Path,
        *,
        snapshot: Optional[WorldStateSnapshot] = None,
        snapshot_path: Optional[str | Path] = None,
        optimization_report: Optional[OptimizationReport] = None,
        directives: Optional[Sequence[OutlineAdjustmentDirective]] = None,
        batch_size: int = 15,
        map_change_interval: int = 60,
    ) -> VolumeExtensionResult:
        """Rolling continuation reading WorldStateSnapshot & ReviewFeedbackOptimizer directives, enforcing 60-80ch map-change soft-reboot."""
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        if map_change_interval < 10:
            raise ValueError(f"map_change_interval must be >= 10, got {map_change_interval}")

        root = Path(project_dir).resolve()
        project = load_project(root)

        effective_snapshot = snapshot
        if effective_snapshot is None and snapshot_path is not None:
            sp = Path(snapshot_path)
            if not sp.is_file():
                raise PlannerError(f"Snapshot file does not exist: {sp}")
            raw_snap = json.loads(sp.read_text(encoding="utf-8"))
            effective_snapshot = WorldStateSnapshot.from_dict(raw_snap)

        all_directives: List[OutlineAdjustmentDirective] = []
        if optimization_report is not None:
            all_directives.extend(optimization_report.directives)
        if directives is not None:
            all_directives.extend(directives)
        for d in all_directives:
            assert_chapter_mutable(d.target_chapter, project.meta.max_published_chapter)

        max_scene_ch = max(s.chapter for s in project.scenes)
        snap_ch = effective_snapshot.current_chapter if effective_snapshot is not None else 0
        start_chapter = max(max_scene_ch, project.meta.max_published_chapter, snap_ch) + 1
        end_chapter = start_chapter + batch_size - 1

        current_vol = max(
            project.meta.volume_index,
            max(s.volume for s in project.scenes),
            effective_snapshot.current_volume if effective_snapshot is not None else 1,
        )
        current_vol_scenes = [s for s in project.scenes if s.volume == current_vol]
        current_vol_start = min((s.chapter for s in current_vol_scenes), default=1)
        chapters_in_vol = start_chapter - current_vol_start
        crossed_boundary = (
            (start_chapter - 1) // map_change_interval
            > (current_vol_start - 1) // map_change_interval
        )
        trigger_map_change = chapters_in_vol >= map_change_interval or crossed_boundary
        target_vol = current_vol + 1 if trigger_map_change else current_vol

        # Collect existing locks for previous volume to verify strictly higher caps on map change
        prev_locks: Dict[str, VolumeCapLock] = {}
        for lk in project.volume_cap_locks.values():
            if lk.volume == current_vol:
                prev_locks[lk.ledger_key] = lk
        if effective_snapshot is not None:
            for lk in effective_snapshot.volume_cap_locks.values():
                if lk.volume == current_vol and lk.ledger_key not in prev_locks:
                    prev_locks[lk.ledger_key] = lk

        old_locations = {
            s.location_id.strip() for s in project.scenes if s.location_id and s.location_id.strip()
        }
        if effective_snapshot is not None:
            old_locations.update(
                loc.strip() for loc in effective_snapshot.recent_locations if loc and loc.strip()
            )

        deceased_ids = (
            [
                cid
                for cid, cs in effective_snapshot.character_states.items()
                if cs.status == "deceased"
            ]
            if effective_snapshot is not None
            else []
        )
        directives_text = "\n".join(
            f"- [目标章>={d.target_chapter} | {d.signal_type}] {d.instruction}"
            for d in all_directives
        )

        system_prompt = (
            "你是番茄长篇滚动续纲策划师。请严格输出单个 JSON 对象：\n"
            "- map_name: 本批次主地图名称\n"
            "- location_id: 本批次地图标识（换地图时必须不同于旧地图）\n"
            "- soft_identity_reboot: 身份软重启/新马甲切入说明（换地图时必须非空）\n"
            "- volume_cap_locks: 数组（包含本卷更高阶数值上限锁：{ledger_key, volume, max_value, unit}）\n"
            "- scenes: 数组（连续章节大纲，符合 SceneOutline 结构，public_title 不带 '第X章' 前缀，连续纯压抑<=2章）"
        )

        feedback = ""
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_validation_retries + 1):
            feedback_section = (
                f"\n[上一轮校验失败，必须修复]：\n{feedback}\n" if feedback else ""
            )
            map_instruction = (
                f"【强制触发 60~80 章换地图与身份软重启】进入第 {target_vol} 卷！\n"
                f"必须切换全新地图（location_id 禁止复用旧地图：{sorted(old_locations)}），"
                f"必须设计主角身份软重启（soft_identity_reboot），并全面抬升 volume_cap_locks（"
                f"前卷上限：{ {k: v.max_value for k, v in prev_locks.items()} }，新卷必须严格更高）。"
                if trigger_map_change
                else f"继续第 {target_vol} 卷连载节奏。"
            )
            user_prompt = (
                f"项目: {project.meta.project_id}《{project.meta.title}》（genre_id={project.meta.genre_id}）\n"
                f"续写范围: 第 {start_chapter} 章 ～ 第 {end_chapter} 章（共 {batch_size} 章，target_volume={target_vol}）\n"
                f"最新世界状态: current_chapter={snap_ch}, balances={effective_snapshot.numeric_balances if effective_snapshot else {}}, "
                f"deceased_characters={deceased_ids}, forbidden_titles={effective_snapshot.get_all_forbidden_titles() if effective_snapshot else []}\n"
                f"章评舆情调优指令（必须严格注入第 {start_chapter} 章及之后，严禁触碰 <={project.meta.max_published_chapter} 的已发章节）:\n"
                f"{directives_text or '无'}\n"
                f"{map_instruction}\n"
                f"{feedback_section}"
            )
            req = LLMRequest(
                request_id=f"extend-{project.meta.project_id}-v{target_vol}-{attempt}-{uuid.uuid4().hex[:6]}",
                chapter=start_chapter,
                attempt=attempt,
                purpose="plan",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.5,
                max_tokens=10000,
                metadata={
                    "stage": "extend_volume",
                    "project_id": project.meta.project_id,
                    "target_volume": target_vol,
                    "start_chapter": start_chapter,
                    "end_chapter": end_chapter,
                    "trigger_map_change": trigger_map_change,
                    "attempt": attempt,
                },
            )
            resp = _invoke_planner(self.provider, req)
            try:
                payload = _parse_json_payload(resp.content, "extend_volume")
                new_map_name = str(payload.get("map_name") or "").strip()
                new_loc_id = str(payload.get("location_id") or "").strip()
                soft_reboot = str(payload.get("soft_identity_reboot") or "").strip()
                raw_scenes = payload.get("scenes")
                if not isinstance(raw_scenes, list) or len(raw_scenes) != batch_size:
                    got_n = len(raw_scenes) if isinstance(raw_scenes, list) else 0
                    raise ProjectValidationError(
                        f"extend_volume expected {batch_size} scenes, got {got_n}"
                    )

                new_locks_raw = payload.get("volume_cap_locks") or []
                new_locks = [VolumeCapLock.from_dict(lk) for lk in new_locks_raw]

                if trigger_map_change:
                    if not new_map_name or not new_loc_id:
                        raise ProjectValidationError(
                            "Map-change volume extension requires non-empty map_name and location_id"
                        )
                    if new_loc_id in old_locations:
                        raise ProjectValidationError(
                            f"New map location_id {new_loc_id!r} collides with prior map locations {sorted(old_locations)}"
                        )
                    if not soft_reboot:
                        raise ProjectValidationError(
                            "Map-change volume extension requires non-empty soft_identity_reboot"
                        )
                    if not new_locks:
                        raise ProjectValidationError(
                            "Map-change volume extension requires higher-tier volume_cap_locks"
                        )
                    for lk in new_locks:
                        if lk.volume != target_vol:
                            raise ProjectValidationError(
                                f"New VolumeCapLock for '{lk.ledger_key}' must have volume={target_vol}, got {lk.volume}"
                            )
                        if lk.ledger_key in prev_locks and lk.max_value <= prev_locks[lk.ledger_key].max_value:
                            raise ProjectValidationError(
                                f"New VolumeCapLock '{lk.ledger_key}' max_value ({lk.max_value}) must exceed "
                                f"previous volume cap ({prev_locks[lk.ledger_key].max_value})"
                            )
                    for prev_key, prev_lk in prev_locks.items():
                        if not any(lk.ledger_key == prev_key for lk in new_locks):
                            raise ProjectValidationError(
                                f"Missing upgraded VolumeCapLock for existing ledger_key '{prev_key}'"
                            )

                normalized_new_scenes: List[Dict[str, Any]] = []
                for offset, item in enumerate(raw_scenes):
                    s_dict = dict(item)
                    expected_ch = start_chapter + offset
                    if int(s_dict.get("chapter", 0)) != expected_ch:
                        raise ProjectValidationError(
                            f"Expected scene chapter {expected_ch}, got {s_dict.get('chapter')!r}"
                        )
                    if int(s_dict["chapter"]) <= project.meta.max_published_chapter:
                        raise ProjectValidationError(
                            f"Cannot overwrite published chapter {s_dict['chapter']} <= {project.meta.max_published_chapter}"
                        )
                    if s_dict.get("public_title"):
                        s_dict["public_title"] = _strip_chapter_prefix(str(s_dict["public_title"]))
                    s_dict["volume"] = target_vol
                    if trigger_map_change and not s_dict.get("location_id"):
                        s_dict["location_id"] = new_loc_id
                    normalized_new_scenes.append(s_dict)

                # Stage full project copy to validate with load_project()
                with tempfile.TemporaryDirectory(prefix="xsb_ext_") as tmpdir:
                    staging = Path(tmpdir)
                    shutil.copytree(root, staging, dirs_exist_ok=True, ignore=shutil.ignore_patterns("_archive", "*.tmp"))
                    self._prepare_multi_volume_dir(staging)

                    meta_raw = yaml.safe_load((staging / "project.yaml").read_text(encoding="utf-8"))
                    meta_raw["volume_index"] = target_vol
                    (staging / "project.yaml").write_text(
                        _dump_yaml(meta_raw), encoding="utf-8", newline="\n"
                    )

                    vol_path = staging / "outline" / f"vol_{target_vol:02d}.yaml"
                    if target_vol == current_vol and vol_path.exists():
                        vol_raw = yaml.safe_load(vol_path.read_text(encoding="utf-8")) or {}
                        existing_scenes = list(vol_raw.get("scenes", []))
                        existing_scenes.extend(normalized_new_scenes)
                        vol_raw["scenes"] = existing_scenes
                        if new_locks:
                            vol_raw["volume_cap_locks"] = [lk.to_dict() for lk in new_locks]
                    else:
                        vol_raw = {
                            "volume": target_vol,
                            "is_final_volume": False,
                            "map_name": new_map_name,
                            "location_id": new_loc_id,
                            "soft_identity_reboot": soft_reboot,
                            "volume_cap_locks": [lk.to_dict() for lk in new_locks],
                            "scenes": normalized_new_scenes,
                        }
                    vol_path.write_text(_dump_yaml(vol_raw), encoding="utf-8", newline="\n")
                    load_project(staging)

            except (
                ProjectValidationError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                last_error = exc
                feedback = str(exc)
                continue

            # Persist validated extension into project_dir (archiving overwritten files into _archive/)
            stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S_%f")
            self._migrate_single_outline_if_needed(root, stamp)

            meta_raw = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
            meta_raw["volume_index"] = target_vol
            _write_text_archived(root / "project.yaml", _dump_yaml(meta_raw), root, stamp)

            target_vol_path = root / "outline" / f"vol_{target_vol:02d}.yaml"
            if target_vol == current_vol and target_vol_path.exists():
                vol_raw = yaml.safe_load(target_vol_path.read_text(encoding="utf-8")) or {}
                existing_scenes = list(vol_raw.get("scenes", []))
                existing_scenes.extend(normalized_new_scenes)
                vol_raw["scenes"] = existing_scenes
                if new_locks:
                    vol_raw["volume_cap_locks"] = [lk.to_dict() for lk in new_locks]
            else:
                vol_raw = {
                    "volume": target_vol,
                    "is_final_volume": False,
                    "map_name": new_map_name,
                    "location_id": new_loc_id,
                    "soft_identity_reboot": soft_reboot,
                    "volume_cap_locks": [lk.to_dict() for lk in new_locks],
                    "scenes": normalized_new_scenes,
                }
            _write_text_archived(target_vol_path, _dump_yaml(vol_raw), root, stamp)

            final_project = load_project(root)
            return VolumeExtensionResult(
                project_dir=root,
                project=final_project,
                volume_index=target_vol,
                start_chapter=start_chapter,
                end_chapter=end_chapter,
                triggered_map_change=trigger_map_change,
                new_map_name=new_map_name,
                new_map_location_id=new_loc_id,
                soft_identity_reboot=soft_reboot,
                new_volume_cap_locks=new_locks,
                volume_file_path=target_vol_path,
                attempts_used=attempt,
            )

        raise PlanningValidationError(
            f"extend_volume failed validation after {self.max_validation_retries} attempts: {last_error}"
        ) from last_error

    @staticmethod
    def _prepare_multi_volume_dir(root: Path) -> None:
        outline_file = root / "outline.yaml"
        outline_dir = root / "outline"
        if outline_file.exists() and not outline_dir.exists():
            outline_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(outline_file), str(outline_dir / "vol_01.yaml"))

    @staticmethod
    def _migrate_single_outline_if_needed(root: Path, stamp: str) -> None:
        outline_file = root / "outline.yaml"
        outline_dir = root / "outline"
        if outline_file.exists():
            outline_dir.mkdir(parents=True, exist_ok=True)
            vol1 = outline_dir / "vol_01.yaml"
            if not vol1.exists():
                content = outline_file.read_text(encoding="utf-8")
                _archive_existing_file(outline_file, root, stamp)
                vol1.write_text(content, encoding="utf-8", newline="\n")

    @staticmethod
    def _build_finale_foreshadows_list(
        fs_raw: Mapping[str, Any],
        *,
        fs_resolutions: Mapping[str, Any],
        unresolved_fs: Mapping[str, Any],
        effective_snapshot: Optional[WorldStateSnapshot],
    ) -> List[Dict[str, Any]]:
        updated_fs_list: List[Dict[str, Any]] = []
        seen_fs_ids: set[str] = set()
        snap_foreshadows = (
            effective_snapshot.foreshadows if effective_snapshot is not None else {}
        )
        for fs_item in fs_raw.get("foreshadows", []):
            item_copy = dict(fs_item)
            fid = str(item_copy["id"])
            seen_fs_ids.add(fid)
            if fid in fs_resolutions:
                item_copy["due_chapter"] = int(fs_resolutions[fid])
            if fid in snap_foreshadows:
                snap_fs = snap_foreshadows[fid]
                if snap_fs.status == "resolved":
                    item_copy["status"] = "resolved"
                    if snap_fs.planted_evidence:
                        item_copy["planted_evidence"] = snap_fs.planted_evidence
                    if snap_fs.payoff_evidence:
                        item_copy["payoff_evidence"] = snap_fs.payoff_evidence
            updated_fs_list.append(item_copy)
        for fid, fs_obj in unresolved_fs.items():
            if fid not in seen_fs_ids and fid in fs_resolutions:
                fs_copy = fs_obj.to_dict()
                fs_copy["due_chapter"] = int(fs_resolutions[fid])
                updated_fs_list.append(fs_copy)
        return updated_fs_list

    def plan_finale_volume(
        self,
        project_dir: str | Path,
        *,
        snapshot: Optional[WorldStateSnapshot] = None,
        snapshot_path: Optional[str | Path] = None,
        num_chapters: int = 8,
    ) -> FinaleVolumeResult:
        """Inventory all unresolved foreshadows & active expectations and generate is_final_volume=True closing outline."""
        if num_chapters < 1:
            raise ValueError(f"num_chapters must be >= 1, got {num_chapters}")

        root = Path(project_dir).resolve()
        project = load_project(root)

        effective_snapshot = snapshot
        if effective_snapshot is None and snapshot_path is not None:
            sp = Path(snapshot_path)
            if not sp.is_file():
                raise PlannerError(f"Snapshot file does not exist: {sp}")
            raw_snap = json.loads(sp.read_text(encoding="utf-8"))
            effective_snapshot = WorldStateSnapshot.from_dict(raw_snap)

        # Inventory unresolved foreshadows and active expectations
        unresolved_fs: Dict[str, Any] = {}
        for fid, fs in project.foreshadows.items():
            if fs.status != "resolved":
                unresolved_fs[fid] = fs
        if effective_snapshot is not None:
            for fid, fs in effective_snapshot.foreshadows.items():
                if fs.status == "resolved":
                    unresolved_fs.pop(fid, None)
                else:
                    unresolved_fs[fid] = fs

        active_exps: Dict[str, Any] = {}
        if effective_snapshot is not None:
            for eid, exp in effective_snapshot.active_expectations.items():
                if exp.status != "fulfilled":
                    active_exps[eid] = exp

        start_chapter = max(
            max(s.chapter for s in project.scenes),
            project.meta.max_published_chapter,
            effective_snapshot.current_chapter if effective_snapshot is not None else 0,
        ) + 1
        end_chapter = start_chapter + num_chapters - 1
        finale_vol = max(
            project.meta.volume_index,
            max(s.volume for s in project.scenes),
            effective_snapshot.current_volume if effective_snapshot is not None else 1,
        ) + 1

        fs_summary = [
            {
                "id": fs.id,
                "title": fs.title,
                "plant_chapter": fs.plant_chapter,
                "current_due_chapter": fs.due_chapter,
                "payoff_instruction": fs.payoff_instruction,
                "payoff_keywords": list(fs.payoff_keywords),
            }
            for fs in unresolved_fs.values()
        ]
        exp_summary = [
            {
                "id": exp.id,
                "title": exp.title,
                "planted_chapter": exp.planted_chapter,
                "due_chapter": exp.due_chapter,
                "description": exp.description,
            }
            for exp in active_exps.values()
        ]

        system_prompt = (
            "你是番茄长篇完结总收官策划师。请将全部未收伏笔与未兑现期待感承诺全部清账收官。\n"
            "必须严格输出单个 JSON 对象：\n"
            "- foreshadow_resolutions: 对象 {foreshadow_id: payoff_chapter}（全部未收伏笔必须落入本终卷章号范围内）\n"
            "- expectation_fulfillments: 对象 {expectation_id: fulfill_chapter}\n"
            "- scenes: 数组（共 num_chapters 章终卷大纲，对应章必须包含兑现对应伏笔关键词的事件及 expectation_directives action='fulfill'）"
        )

        feedback = ""
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_validation_retries + 1):
            feedback_section = (
                f"\n[上一轮校验失败，必须修复]：\n{feedback}\n" if feedback else ""
            )
            user_prompt = (
                f"项目: {project.meta.project_id}《{project.meta.title}》\n"
                f"终卷编号: 第 {finale_vol} 卷 (is_final_volume=True)\n"
                f"终卷章号范围: 第 {start_chapter} 章 ～ 第 {end_chapter} 章（共 {num_chapters} 章）\n"
                f"待清账未收伏笔清单:\n{json.dumps(fs_summary, ensure_ascii=False, indent=2)}\n"
                f"待兑现期待感承诺清单:\n{json.dumps(exp_summary, ensure_ascii=False, indent=2)}\n"
                f"{feedback_section}"
            )
            req = LLMRequest(
                request_id=f"finale-{project.meta.project_id}-v{finale_vol}-{attempt}-{uuid.uuid4().hex[:6]}",
                chapter=start_chapter,
                attempt=attempt,
                purpose="plan",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.4,
                max_tokens=10000,
                metadata={
                    "stage": "plan_finale_volume",
                    "project_id": project.meta.project_id,
                    "finale_volume": finale_vol,
                    "start_chapter": start_chapter,
                    "end_chapter": end_chapter,
                    "unresolved_foreshadow_ids": list(unresolved_fs.keys()),
                    "active_expectation_ids": list(active_exps.keys()),
                    "attempt": attempt,
                },
            )
            resp = _invoke_planner(self.provider, req)
            try:
                payload = _parse_json_payload(resp.content, "plan_finale_volume")
                raw_scenes = payload.get("scenes")
                if not isinstance(raw_scenes, list) or len(raw_scenes) != num_chapters:
                    got_n = len(raw_scenes) if isinstance(raw_scenes, list) else 0
                    raise ProjectValidationError(
                        f"plan_finale_volume expected {num_chapters} scenes, got {got_n}"
                    )

                fs_resolutions = dict(payload.get("foreshadow_resolutions") or {})
                exp_fulfillments = dict(payload.get("expectation_fulfillments") or {})

                finale_chapters = set(range(start_chapter, end_chapter + 1))
                for fid in unresolved_fs:
                    if fid not in fs_resolutions:
                        raise ProjectValidationError(
                            f"Finale missing foreshadow_resolutions mapping for '{fid}'"
                        )
                    ch_val = int(fs_resolutions[fid])
                    if ch_val not in finale_chapters:
                        raise ProjectValidationError(
                            f"Finale foreshadow '{fid}' due_chapter {ch_val} outside finale range "
                            f"{start_chapter}..{end_chapter}"
                        )

                normalized_scenes: List[Dict[str, Any]] = []
                fulfilled_exp_in_scenes: set[str] = set()
                for offset, item in enumerate(raw_scenes):
                    s_dict = dict(item)
                    expected_ch = start_chapter + offset
                    if int(s_dict.get("chapter", 0)) != expected_ch:
                        raise ProjectValidationError(
                            f"Expected finale scene chapter {expected_ch}, got {s_dict.get('chapter')!r}"
                        )
                    if s_dict.get("public_title"):
                        s_dict["public_title"] = _strip_chapter_prefix(str(s_dict["public_title"]))
                    s_dict["volume"] = finale_vol
                    for ed in s_dict.get("expectation_directives") or []:
                        if isinstance(ed, Mapping) and ed.get("action") == "fulfill" and ed.get("id"):
                            fulfilled_exp_in_scenes.add(str(ed["id"]))
                    normalized_scenes.append(s_dict)

                for eid in active_exps:
                    if eid not in exp_fulfillments and eid not in fulfilled_exp_in_scenes:
                        raise ProjectValidationError(
                            f"Finale missing fulfillment for active expectation '{eid}'"
                        )
                    if eid in exp_fulfillments:
                        f_ch = int(exp_fulfillments[eid])
                        if f_ch not in finale_chapters:
                            raise ProjectValidationError(
                                f"Expectation '{eid}' fulfill_chapter {f_ch} outside finale range"
                            )
                        if eid not in fulfilled_exp_in_scenes:
                            target_scene = normalized_scenes[f_ch - start_chapter]
                            directives_list = list(target_scene.get("expectation_directives") or [])
                            directives_list.append(
                                {
                                    "id": eid,
                                    "title": active_exps[eid].title,
                                    "due_chapter": f_ch,
                                    "description": active_exps[eid].description,
                                    "action": "fulfill",
                                    "evidence_keywords": [active_exps[eid].title[:4] or "兑现"],
                                }
                            )
                            target_scene["expectation_directives"] = directives_list
                            fulfilled_exp_in_scenes.add(eid)

                # Validate full project in staging with is_final_volume=True
                with tempfile.TemporaryDirectory(prefix="xsb_fin_") as tmpdir:
                    staging = Path(tmpdir)
                    shutil.copytree(root, staging, dirs_exist_ok=True, ignore=shutil.ignore_patterns("_archive", "*.tmp"))
                    self._prepare_multi_volume_dir(staging)

                    meta_raw = yaml.safe_load((staging / "project.yaml").read_text(encoding="utf-8"))
                    meta_raw["volume_index"] = finale_vol
                    meta_raw["is_final_volume"] = True
                    (staging / "project.yaml").write_text(
                        _dump_yaml(meta_raw), encoding="utf-8", newline="\n"
                    )

                    fs_raw = yaml.safe_load((staging / "foreshadows.yaml").read_text(encoding="utf-8")) or {}
                    updated_fs_list = self._build_finale_foreshadows_list(
                        fs_raw,
                        fs_resolutions=fs_resolutions,
                        unresolved_fs=unresolved_fs,
                        effective_snapshot=effective_snapshot,
                    )
                    (staging / "foreshadows.yaml").write_text(
                        _dump_yaml({"foreshadows": updated_fs_list}),
                        encoding="utf-8",
                        newline="\n",
                    )

                    finale_vol_data = {
                        "volume": finale_vol,
                        "is_final_volume": True,
                        "scenes": normalized_scenes,
                    }
                    (staging / "outline" / f"vol_{finale_vol:02d}.yaml").write_text(
                        _dump_yaml(finale_vol_data),
                        encoding="utf-8",
                        newline="\n",
                    )

                    load_project(staging, is_final_volume=True)

            except (
                ProjectValidationError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                last_error = exc
                feedback = str(exc)
                continue

            # Persist validated finale volume into project_dir
            stamp = datetime.now(SHANGHAI_TZ).strftime("%Y%m%dT%H%M%S_%f")
            self._migrate_single_outline_if_needed(root, stamp)

            meta_raw = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
            meta_raw["volume_index"] = finale_vol
            meta_raw["is_final_volume"] = True
            _write_text_archived(root / "project.yaml", _dump_yaml(meta_raw), root, stamp)

            fs_raw = yaml.safe_load((root / "foreshadows.yaml").read_text(encoding="utf-8")) or {}
            updated_fs_list = self._build_finale_foreshadows_list(
                fs_raw,
                fs_resolutions=fs_resolutions,
                unresolved_fs=unresolved_fs,
                effective_snapshot=effective_snapshot,
            )
            _write_text_archived(
                root / "foreshadows.yaml",
                _dump_yaml({"foreshadows": updated_fs_list}),
                root,
                stamp,
            )

            finale_vol_path = root / "outline" / f"vol_{finale_vol:02d}.yaml"
            finale_vol_data = {
                "volume": finale_vol,
                "is_final_volume": True,
                "scenes": normalized_scenes,
            }
            _write_text_archived(finale_vol_path, _dump_yaml(finale_vol_data), root, stamp)

            final_project = load_project(root, is_final_volume=True)
            return FinaleVolumeResult(
                project_dir=root,
                project=final_project,
                volume_index=finale_vol,
                start_chapter=start_chapter,
                end_chapter=end_chapter,
                resolved_foreshadow_ids=sorted(unresolved_fs.keys()),
                fulfilled_expectation_ids=sorted(active_exps.keys()),
                volume_file_path=finale_vol_path,
                attempts_used=attempt,
            )

        raise PlanningValidationError(
            f"plan_finale_volume failed validation after {self.max_validation_retries} attempts: {last_error}"
        ) from last_error


__all__ = [
    "PlannerError",
    "PlanningValidationError",
    "PlannedProjectResult",
    "VolumeExtensionResult",
    "FinaleVolumeResult",
    "AutoNovelPlanner",
]
