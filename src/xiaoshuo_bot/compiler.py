from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .genres import GenreRecipe, get_genre
from .memory import MemoryPyramid
from .models import NovelProject, ScanReport, SceneOutline, StyleProfile
from .sanitizers import FICTIONALIZATION_MAP
from .state import WorldStateManager

_KEY_FICTIONALIZATION_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("中国", FICTIONALIZATION_MAP["中国"]),
    ("北京", FICTIONALIZATION_MAP["北京"]),
    ("上海", FICTIONALIZATION_MAP["上海"]),
    ("杭州", FICTIONALIZATION_MAP["杭州"]),
    ("广州", FICTIONALIZATION_MAP["广州"]),
    ("深圳", FICTIONALIZATION_MAP["深圳"]),
    ("清华大学", FICTIONALIZATION_MAP["清华大学"]),
    ("北京大学", FICTIONALIZATION_MAP["北京大学"]),
    ("公安局", FICTIONALIZATION_MAP["公安局"]),
    ("派出所", FICTIONALIZATION_MAP["派出所"]),
    ("检察院", FICTIONALIZATION_MAP["检察院"]),
    ("法院", FICTIONALIZATION_MAP["法院"]),
    ("国务院", FICTIONALIZATION_MAP["国务院"]),
)

_EMOTION_LABELS: Dict[str, str] = {
    "suppress": "压抑蓄力（切忌提前泄气，反派压迫须步步紧逼）",
    "burst": "爽点爆发（反手打脸/破局兑现须干脆狠辣，给足感官反馈）",
    "neutral": "稳健推进（张弛有度，贴合事件因果）",
    "mixed": "先抑后扬（前压后爆，先吃憋屈或危机，后段即时反杀翻盘）",
}



def target_chapter_chars(scene: SceneOutline) -> int:
    """Explicit length target: midpoint of the outline range, rounded down to 100 characters.

    Telling the model only "min~max, stop when events are done" made Gemini 3.8 Flash land at or
    below the floor (real A/B 2026-11-18: 1033-1926 chars against min 1900). A concrete target
    pulls drafts into the middle of the allowed range; the hard gate itself is unchanged.
    """
    midpoint = (int(scene.min_chars) + int(scene.max_chars)) // 2
    return max(int(scene.min_chars), (midpoint // 100) * 100)

class PromptCompiler:
    """Compiles dynamic 5D-state, memory-pyramid, and Tomato-track-aware prompts & rewrite instructions."""

    def __init__(self, project: NovelProject) -> None:
        self.project = project

    def _resolve_genre_recipe(self) -> Optional[GenreRecipe]:
        genre_id = (self.project.meta.genre_id or "").strip()
        if not genre_id:
            return None
        return get_genre(genre_id)

    def _format_character_section(
        self,
        scene: SceneOutline,
        state: WorldStateManager,
        pov_mode: str,
    ) -> str:
        pov_char = self.project.characters[scene.pov_character]
        char_blocks: List[str] = []
        active_set = set(scene.active_characters)

        for cid in scene.active_characters:
            c = self.project.characters[cid]
            c_state = state.character_states.get(cid)
            if cid == scene.pov_character:
                if pov_mode == "first_person":
                    pov_tag = "【当前唯一第一人称“我”视点角色（POV）】"
                else:
                    pov_tag = "【当前唯一限知视点角色（POV）】"
            else:
                observer = f"“我”（{pov_char.name}）" if pov_mode == "first_person" else pov_char.name
                pov_tag = f"【外部观察角色（严禁直接进入其内心独白，仅能由{observer}的感官观察其言行）】"

            anchors_str = "；".join(f"{k}时表现为「{v}」" for k, v in c.behavioral_anchors.items()) or "无"
            ooc_str = "；".join(c.forbidden_ooc) if c.forbidden_ooc else "无"

            status_val = c_state.status if c_state else c.initial_status
            faction_val = (c_state.faction if c_state else c.faction) or "无/未定"

            relations_map = c_state.relations if c_state else c.relations
            rel_parts: List[str] = []
            for target_id, rel_label in relations_map.items():
                target_char = self.project.characters.get(target_id)
                target_name = target_char.name if target_char else target_id
                rel_parts.append(f"与{target_name}({target_id})={rel_label}")
            relations_str = "；".join(rel_parts) if rel_parts else "无特殊登记关系"

            # Merge forbidden titles from character_states and state.forbidden_titles
            forbidden_list: List[str] = []
            for src in (
                c_state.forbidden_titles if c_state else c.forbidden_titles,
                state.forbidden_titles.get(cid, []),
            ):
                for title in src:
                    if title and title not in forbidden_list:
                        forbidden_list.append(title)
            forbidden_str = (
                f"{'、'.join(forbidden_list)}（关系已变更，正文严禁再称呼上述过期称谓！）"
                if forbidden_list
                else "无"
            )

            status_note = status_val
            if status_val == "deceased":
                deceased_ch = c_state.deceased_chapter if c_state else None
                status_note = f"deceased（已于第 {deceased_ch} 章死亡！仅限明确标记的回忆/遗物提及，严禁现身施动或开口！）"

            char_blocks.append(
                f"- **{c.name}**（{c.role}）{pov_tag}\n"
                f"  - 当前生死/阵营状态：{status_note} ｜ 阵营：{faction_val}\n"
                f"  - 当前人物关系：{relations_str}\n"
                f"  - 过期禁用称谓：{forbidden_str}\n"
                f"  - 性格底色：{c.personality}\n"
                f"  - 语言特征：{c.speech_style}\n"
                f"  - 行为锚点：{anchors_str}\n"
                f"  - OOC 禁区：{ooc_str}"
            )

        # Surface any off-stage forbidden titles or deceased characters so writer never triggers STATE_* gates
        extra_warnings: List[str] = []
        for cid, titles in state.forbidden_titles.items():
            if cid not in active_set and titles:
                cname = self.project.characters[cid].name if cid in self.project.characters else cid
                extra_warnings.append(f"- 场外人物 **{cname}** 禁用过期称谓：{'、'.join(titles)}")
        for cid, cs in state.character_states.items():
            if cid not in active_set and cs.status == "deceased":
                extra_warnings.append(
                    f"- 已故角色 **{cs.name}**（第 {cs.deceased_chapter} 章死亡）：严禁实时出场施动或说话"
                )
        if extra_warnings:
            char_blocks.append("- **全书状态红线提醒**：\n  " + "\n  ".join(extra_warnings))

        return "\n".join(char_blocks)

    def _format_world_state_5d(
        self,
        scene: SceneOutline,
        state: WorldStateManager,
    ) -> str:
        lines: List[str] = []

        # Dimension 1: Numeric balances & volume cap locks
        if state.numeric_balances:
            bal_items = [f"{k}={v:g}" for k, v in state.numeric_balances.items()]
            lines.append(f"- **数值账本当前余额（严禁单笔支出超额透支）**：{', '.join(bal_items)}")
        else:
            lines.append("- **数值账本当前余额**：暂无受控数值条目")

        vol_locks = [
            lock for lock in state.volume_cap_locks.values() if lock.volume == scene.volume
        ]
        if vol_locks:
            lock_strs: List[str] = []
            for lock in vol_locks:
                unit_sfx = f" {lock.unit}" if lock.unit else ""
                status_tag = "已突破解锁" if lock.unlocked else f"硬上限锁死 <= {lock.max_value:g}{unit_sfx}"
                lock_strs.append(f"{lock.ledger_key}（第{lock.volume}卷：{status_tag}）")
            lines.append(f"- **本卷数值/战力上限锁**：{'；'.join(lock_strs)}")

        if scene.ledger_mutations:
            mut_strs = [
                f"{m.ledger_key} {m.delta:+g}（{m.reason or '章纲变更'}"
                f"{'，含突破解锁' if m.breakthrough else ''}）"
                for m in scene.ledger_mutations
            ]
            lines.append(f"- **本章预定账本变动**：{'；'.join(mut_strs)}")

        # Dimension 3: Expectation countdowns
        active_exps = state.get_active_expectations()
        if active_exps:
            exp_strs: List[str] = []
            for exp in active_exps:
                rem = exp.due_chapter - scene.chapter
                if rem < 0 or exp.status == "overdue":
                    urgency = f"【已逾期 {-rem} 章，必须立即在本章兑现！】"
                elif rem == 0:
                    urgency = "【本章到期，必须兑现核销！】"
                else:
                    urgency = f"剩余 {rem} 章（第{exp.due_chapter}章到期）"
                exp_strs.append(f"[{exp.id}] {exp.title}（{urgency}）")
            lines.append(f"- **期待感承诺倒计时**：{'；'.join(exp_strs)}")
        else:
            lines.append("- **期待感承诺倒计时**：当前无挂起未兑现承诺")

        if scene.expectation_directives:
            dir_strs: List[str] = []
            for ed in scene.expectation_directives:
                kw = f"（必含证据词：{'/'.join(ed.evidence_keywords)}）" if ed.evidence_keywords else ""
                dir_strs.append(f"[{ed.id}] action={ed.action}「{ed.title}」到期Ch.{ed.due_chapter}{kw}")
            lines.append(f"- **本章期待感指令**：{'；'.join(dir_strs)}")

        # Dimension 4: Virtual calendar, injuries, and consecutive location count
        lines.append(
            f"- **虚拟时间与季节**：{state.virtual_date_label}（第 {state.virtual_day} 天，"
            f"季节：{state.virtual_season}，本章推进 {scene.calendar_days_elapsed} 天，严禁季节服装穿帮）"
        )

        if state.unhealed_injuries:
            inj_parts: List[str] = []
            for cid, inj_map in state.unhealed_injuries.items():
                cname = self.project.characters[cid].name if cid in self.project.characters else cid
                items = "、".join(f"{name}(还需恢复{rem}章)" for name, rem in inj_map.items())
                inj_parts.append(f"{cname}：{items}")
            lines.append(f"- **未愈伤势冷却锁（未满冷却期严禁无伤痊愈）**：{'；'.join(inj_parts)}")
        else:
            lines.append("- **未愈伤势冷却锁**：全员当前无未愈伤势限制")

        loc_id = (scene.location_id or "").strip()
        if loc_id:
            consec = state.get_consecutive_location_count(loc_id)
            warn = "（警告：已连续驻留>=2章，严禁原地水字，须准备换地图！）" if consec >= 2 else ""
            lines.append(f"- **场景地图坐标**：`{loc_id}`（已连续驻留 {consec} 章）{warn}")

        # Dimension 5: Locked chapters
        if state.locked_chapters:
            locked_preview = ", ".join(str(c) for c in state.locked_chapters[-10:])
            lines.append(f"- **已发布/定稿锁章**：第 {locked_preview} 章已锁死 SHA256，严禁改写或推翻既定事实")

        return "\n".join(lines)

    def _format_compliance_and_genre_section(
        self,
        scene: SceneOutline,
        recipe: Optional[GenreRecipe],
        contract_track: str,
    ) -> str:
        blocks: List[str] = []

        if recipe is not None:
            blocks.append(recipe.common_sense_card.format_prompt_block())
            if scene.chapter == 1 and recipe.opening_300w_directive:
                blocks.append(
                    f"### 第 1 章首屏黄金 300 字开局指令\n- {recipe.opening_300w_directive}"
                )
            if 1 <= scene.chapter <= 3:
                for beat in recipe.golden_three_beats:
                    if beat.chapter == scene.chapter:
                        blocks.append(
                            f"### 黄金三章节拍参照（第 {beat.chapter} 章：{beat.role}）\n"
                            f"- 开篇钩子：{beat.opening_hook}\n"
                            f"- 核心冲突：{beat.core_conflict}\n"
                            f"- 爽点/反转：{beat.payoff_or_twist}\n"
                            f"- 章末断章：{beat.chapter_end_hook}"
                        )
                        break

        # Compliant modern fictionalization hints
        is_ancient_only = recipe is not None and recipe.genre_id in {
            "xuanhuan_brainhole",
            "history_brainhole",
            "ancient_rebirth",
            "ancient_mystery",
        }
        if not is_ancient_only or contract_track != "legacy":
            pair_lines = "；".join(f"「{k}」->「{v}」" for k, v in _KEY_FICTIONALIZATION_PAIRS)
            blocks.append(
                "### 合规架空命名与红线提示（Sanitizer）\n"
                f"- 现代都市/悬疑场景必须使用合规架空地名与机构名，严禁出现真实省市、高校与政法机关全称。常用映射：{pair_lines}。\n"
                "- 严禁使用星号、点号、下划线或空格插字绕审变体（如“警*察”、“肉.体”、“杀-人”），严禁任何涉黄、极端血腥与未成年人红线描写。"
            )

        return "\n\n".join(blocks)

    def compile_scene_prompt(
        self,
        scene: SceneOutline,
        state: WorldStateManager,
        memory_pyramid: Optional[MemoryPyramid] = None,
    ) -> Tuple[str, str]:
        """Return (system_prompt, user_prompt) dynamically adapted to project POV, panel policy, 5D state & memory."""
        meta = self.project.meta
        pov_mode = meta.pov_mode
        panel_policy = meta.panel_policy
        contract_track = meta.contract_track
        is_tomato_track = contract_track != "legacy"

        pov_char = self.project.characters[scene.pov_character]
        recipe = self._resolve_genre_recipe()

        character_traits = self._format_character_section(scene, state, pov_mode)
        world_state_5d = self._format_world_state_5d(scene, state)

        # Format [Current_Asset_State]
        asset_blocks: List[str] = []
        for asset in state.get_scene_assets(scene):
            recent_note = asset.history[-1] if asset.history else "初始状态"
            asset_blocks.append(
                f"- **{asset.name}**（ID: `{asset.id}`）\n"
                f"  - 当前物理位置：{asset.location}\n"
                f"  - 当前持有人/归属：{asset.holder}\n"
                f"  - 物理外观与状态：{asset.condition}（细节：{asset.physical_details}）\n"
                f"  - 最近状态溯源：{recent_note}"
            )
        asset_data = "\n".join(asset_blocks) if asset_blocks else "- 当前场景无特殊受控器物"

        # Format [Memory_Pyramid_L0_L3]
        eff_pyramid = memory_pyramid if memory_pyramid is not None else getattr(state, "memory_pyramid", None)
        memory_block = ""
        if eff_pyramid is not None:
            compiled_mem = eff_pyramid.compile_context(scene)
            if compiled_mem:
                memory_block = f"### [Memory_Pyramid_L0_L3]\n{compiled_mem}\n\n"

        # Format [Foreshadow_Plan]
        due_list = state.get_due_foreshadows(scene.chapter)
        plant_list = state.get_plant_foreshadows(scene.chapter)
        fs_blocks: List[str] = []
        if due_list:
            for fs in due_list:
                cues = "、".join(f"「{k}」" for k in fs.payoff_keywords)
                fs_blocks.append(
                    f"- **[到期必收 · {fs.id}] {fs.title}**（埋设于第{fs.plant_chapter}章，本章必须完成物理回收！）\n"
                    f"  - 回收执行指令：{fs.payoff_instruction}\n"
                    f"  - 必须自然呈现的物理证据词（至少包含其一）：{cues}"
                )
        if plant_list:
            for fs in plant_list:
                cues = "、".join(f"「{k}」" for k in fs.plant_keywords)
                fs_blocks.append(
                    f"- **[本章埋设 · {fs.id}] {fs.title}**（将于第{fs.due_chapter}章回收，本章仅做冷峻白描埋设，严禁点破悬念！）\n"
                    f"  - 埋设执行指令：{fs.plant_instruction}\n"
                    f"  - 必须自然植入的物理细节词（至少包含其一）：{cues}"
                )
        if not fs_blocks:
            fs_blocks.append("- 本章无新增埋设或到期伏笔事项，专注推进当前章纲物理事件。")
        foreshadow_plan = "\n".join(fs_blocks)

        # Format [Scene_Context]
        events_formatted = "\n".join(f"  {idx}. {ev}" for idx, ev in enumerate(scene.events, start=1))
        continuity_block = ""
        if state.previous_chapter_tail:
            continuity_block = (
                f"\n- **前章结尾衔接锚点（仅作时空连续性参考，严禁重复照抄）**：\n"
                f"  > {state.previous_chapter_tail}\n"
            )

        if pov_mode == "first_person":
            pov_scene_line = (
                f"- **唯一限知视点人物**：{pov_char.name}（全程以第一人称“我”的所见、所闻、触觉与判断推进）"
            )
        else:
            pov_scene_line = (
                f"- **唯一限知视点人物**：{pov_char.name}（全文仅限{pov_char.name}的所见、所闻、触觉与判断）"
            )

        tomato_scene_lines: List[str] = []
        if scene.beat_type:
            tomato_scene_lines.append(f"- **本章爽点节拍（beat_type）**：{scene.beat_type}")
        if scene.emotion_polarity != "neutral" or is_tomato_track:
            emo_desc = _EMOTION_LABELS.get(scene.emotion_polarity, scene.emotion_polarity)
            tomato_scene_lines.append(f"- **情绪极性与波形**：{scene.emotion_polarity} — {emo_desc}")
        if scene.segment_comment_cue:
            tomato_scene_lines.append(
                f"- **段评互动槽点埋设要求（1～2 处）**：{scene.segment_comment_cue}"
            )
        elif is_tomato_track:
            tomato_scene_lines.append(
                "- **段评互动槽点埋设要求（1～2 处）**：在关键反转、数字金额、荒诞细节或金句对话处预留 1～2 处读者可吐槽/站队/猜后续的段评引爆点。"
            )
        if scene.cliffhanger_hook:
            tomato_scene_lines.append(
                f"- **章末断章钩子（必须悬在最高张力点）**：{scene.cliffhanger_hook}"
            )
        elif is_tomato_track:
            tomato_scene_lines.append(
                "- **章末断章钩子**：末段必须停在危机逼近、底牌刚掀开一角或新强敌叩门的瞬间，严禁“一夜无话/沉沉睡去/圆满解决”泄气收尾。"
            )
        tomato_scene_block = ("\n" + "\n".join(tomato_scene_lines)) if tomato_scene_lines else ""

        target_chars = target_chapter_chars(scene)
        scene_outline = (
            f"- **时空坐标**：{scene.location_time}\n"
            f"{pov_scene_line}\n"
            f"- **目标篇幅**：约 {target_chars} 汉字（硬性下限 {scene.min_chars}、上限 {scene.max_chars}；"
            "字数靠把章纲里每个事件背后的冲突交锋、信息差揭晓、筹码加码与爽感落地写透来达成，最后一个事件写完即停笔，严禁空泛议论、同义动作复读或反复摆弄道具注水）"
            f"{tomato_scene_block}"
            f"{continuity_block}\n"
            f"- **本章核心物理事件序列**：\n{events_formatted}\n"
            f"- **收束截断条件（写到此处立即停笔，绝不升华总结）**：{scene.stop_condition}"
        )

        # Format [Style_Samples]
        resolved_profiles: List[StyleProfile] = []
        for sp_id in scene.style_profile_ids:
            if sp_id in self.project.style_profiles:
                resolved_profiles.append(self.project.style_profiles[sp_id])
            elif recipe is not None and sp_id == recipe.style_profile.id:
                resolved_profiles.append(recipe.style_profile)
            else:
                resolved_profiles.append(self.project.style_profiles[sp_id])
        if not resolved_profiles and recipe is not None:
            resolved_profiles.append(recipe.style_profile)

        style_blocks: List[str] = []
        for sp in resolved_profiles:
            style_blocks.append(
                f"#### 样本《{sp.name}》\n"
                f"- 颗粒度特征：{sp.granularity_notes}\n"
                f"- 原文切片：\n{sp.sample_text}"
            )
        style_profiles = "\n\n".join(style_blocks) if style_blocks else "- 按对应题材冷峻实体白描推进，禁止空泛形容词。"

        # Build dynamic System Constraints
        if pov_mode == "first_person":
            pov_constraint = (
                f"- **视角**：严格执行第一人称限知视角（唯一视点人物：{pov_char.name}，以“我”自称）。"
                "叙述段自然使用第一人称“我”推进感官与判断；严禁上帝视角（如“殊不知”、“没有人知道”）、"
                "严禁脱离“我”的感知直接切入其他角色内心（严禁出现“XX心想/暗想/心里明白”）。"
            )
        else:
            pov_constraint = (
                f"- **视角**：严格执行第三人称限知视角（唯一视点人物：{pov_char.name}）。"
                "严禁上帝视角（如“殊不知”、“没有人知道”）、严禁切换其他角色内心（严禁出现“XX心想/暗想/心里明白”）、"
                "严禁叙述段出现第一人称“我”。"
            )

        if panel_policy == "allow_system_tag":
            format_constraint = (
                "- **输出格式**：仅输出正文。严禁标题、小标题、章节号、分割线、代码围栏、元信息、创作手记。"
                "允许在正文内穿插受控系统面板/规则提示【...】，但单次【...】严格不超过 65 字且严禁包含竖线“|”或数字进度条斜杠（如“380/500”），以保障有声听书纯净。"
                "输出的第 1 个字符必须是正文的汉字、合法左引号（“「『）或受控系统标签【（不得有前置空格或空行）。"
            )
        else:
            format_constraint = (
                "- **输出格式**：仅输出正文。严禁标题、小标题、章节号、分割线、代码围栏、元信息、创作手记及任何【...】系统面板/提示框。"
                "输出的第 1 个字符必须是正文第 1 句的第一个汉字或合法左引号（“「『），不得有前置空格或空行。"
            )

        tomato_sys_constraints = ""
        if is_tomato_track:
            ch1_hook_rule = (
                "第 1 章黄金首屏 200 字（前 3 段）严禁天气景物起兴（如清晨/雨夜/月色/微风）与户口本式简历介绍，必须首句即落入具体冲突、危机或核心外挂事件；"
                if scene.chapter == 1
                else "每章首屏前 200 字（前 3 段）必须直接切入即时冲突、异常破绽、赌约交锋或上章悬念正面爆发，严禁慢热景物空镜或无意义日常铺垫；"
            )
            solo_quote_rule = (
                "（本章为单人独角戏且禁用面板：每写 200～260 字必须自然穿插一句带双引号“...”的视点人物低声自语/复核、来电短信文字或器物/纸页上的引文，因为纯动作描写无法打断纯旁白计数！）"
                if (len(scene.active_characters) <= 1 and panel_policy != "allow_system_tag")
                else "（每 200～260 字须穿插带引号“...”的人物对话或书面引文，纯动作描写不打断纯旁白计数）"
            )
            tomato_sys_constraints = (
                "\n- **手机竖屏短段呼吸排版（严治碎短句分镜体）**：每段控制在 1～3 句（严禁单段超过 120 字的实心砖头文）；"
                f"连续不含引号的纯旁白绝对不超过 320 字{solo_quote_rule}；"
                "禁止连续超过 4 轮无动作或神态描写的“裸对话”；"
                "严禁连续 4 句以上全是 6～12 字的碎片短句（避免分镜脚本感），必须用 25～45 字的因果推进/感官交锋复句铺开信息与压迫感，"
                "紧接 4～12 字的刀锋短句砸出反转或金句爆点（例：“他把那枚沾着湿泥的铜钱按在红木桌沿，指腹抹过发黑的钱眼，盯着面前脸色发白的中年男人。”接“‘你这单生意，撑不过今晚十二点。’”），自然拉高节奏爆发力（Burstiness σ >= 12）。\n"
                "- **零拖沓与反「给道具做体检」铁律**：同一物件（茶杯、铜钱、罗盘、手机、工具等）在同一场景内严禁无新信息增量地反复拿起、放下、擦拭或摆弄超过 1 次；"
                "严禁把上一段已经说透的条件、金额、威胁或结论换个说法再复读一遍；"
                "每写 250～350 字必须落地至少 1 个实质剧情增量（新破绽暴露 / 信息差反转 / 筹码加码 / 当场打脸或利益易手 / 更高阶威胁逼近）。\n"
                f"- **爆款追读与爽点兑现铁律**：{ch1_hook_rule}"
                "章中必须清晰写出「轻视/压迫 → 主角亮底牌一针见血反击 → 当场见效（震慑、破局、收益或认知反转）」的爽感闭环，并在反转、金额、金句处留出 1～2 处天然段评吐槽点；"
                "严禁章末出现“一夜无话/沉沉睡去/圆满解决/期待明天”泄气收尾，必须在强悬念或新冲突露头时断章；"
                "严禁对死敌或加害者无代价原谅、替加害者求情或大段道德说教（反圣母铁律）。"
            )

        genre_role_label = f"（题材：{recipe.display_name}）" if recipe is not None else ""
        system_prompt = (
            f"# Role: 资深中文长篇小说作家{genre_role_label}\n\n"
            "## 1. 生产环境约束 (System Constraints)\n"
            f"{pov_constraint}\n"
            f"{format_constraint}\n"
            f"- **篇幅**：目标约 {target_chars} 字；低于 {scene.min_chars} 字会被系统直接打回重写，"
            f"超过 {scene.max_chars} 字同样打回。字数必须靠高密度冲突推进、锋利对话交锋、具体证据/收益兑现与爽点反应写足，"
            "严禁靠反复摆弄器物（给道具做体检）、同义台词复读、空泛议论或心理独白堆砌注水。\n"
            "- **风格采样**：参考下方的 [Style_Samples] 进行句子呼吸感、器物颗粒度与动作推进模拟。"
            f"{tomato_sys_constraints}"
        )

        compliance_and_genre_block = self._format_compliance_and_genre_section(
            scene=scene,
            recipe=recipe,
            contract_track=contract_track,
        )
        genre_section_str = f"{compliance_and_genre_block}\n\n" if compliance_and_genre_block else ""

        tomato_task_flow = ""
        if is_tomato_track or scene.beat_type or scene.segment_comment_cue or scene.cliffhanger_hook:
            tomato_task_flow = (
                "- 爽点与互动埋设：严格落实本章 [Scene_Context] 中的节拍（beat_type）、1～2 处段评互动槽点及章末断章钩子，并保证手机竖屏短段呼吸感。\n"
            )

        user_prompt = (
            "## 2. 资产与五维世界状态 (Asset & 5D World State) - 由系统动态注入\n"
            "### [Character_Identity_Anchor]\n"
            f"{character_traits}\n\n"
            "### [Current_Asset_State]\n"
            f"{asset_data}\n\n"
            "### [World_State_5D]\n"
            f"{world_state_5d}\n\n"
            f"{memory_block}"
            "### [Foreshadow_Plan]\n"
            f"{foreshadow_plan}\n\n"
            "## 3. 叙事禁令与题材风控 (Negative Constraints - AI-TIC-CHECK)\n"
            f"{genre_section_str}"
            "- **绝对禁止**：\n"
            "  - 总结腔/升华词（如：“这一刻，命运的齿轮…”、“历史的车轮”、“总而言之”、“由此可见”、“某种意义上”、“不得不说”、“仿佛在诉说着”、“一切才刚刚开始”）。\n"
            "  - 身体反应套路与网文虚词（如：“不禁一愣”、“微微一愣”、“心中一紧”、“心头一震”、“眸光微闪”、“瞳孔微缩”、“眼底闪过一丝”、“嘴角勾起一抹”、“倒吸一口凉气”、“苦笑一声”、“喃喃自语”、独立副词“不禁”，以及“三分薄凉”、“空气仿佛凝固”、“微不可察地”、“犹如实质般的杀意”、“声音淬了冰”、“大提琴般的嗓音”、“神色晦暗不明”）。\n"
            "  - 结构化对称与廉价路人震惊（如：“不是A，而是B”、“并非A，而是B”、“与其说是A，不如说是B”、“是……，是……，是……”三连排比、“没有……，没有……，只有……”；禁止高频堆砌“全场鸦雀无声/一片哗然/恐怖如斯”）。\n"
            "- **强制检查**：写完后，全文扫描是否出现上述禁用表达、过期称谓、死人施动、余额透支或违规地名机构。命中任意 CRITICAL 项，视为生产事故，自动触发重写。\n\n"
            "## 4. 任务执行流 (Task Flow)\n"
            "- 场景推进：必须基于当前的 [Scene_Context] 进行物理动作与对话推进，严格遵守 [Current_Asset_State] 中物件的物理位置与损耗状态，以及 [World_State_5D] 中的余额、伤势与倒计时约束。\n"
            "- 逻辑一致性：对话与微动作必须符合 [Character_Identity_Anchor] 的性格档案与最新关系，严禁角色 OOC 或使用已作废称谓。\n"
            "- 事件回收：若 [Foreshadow_Plan] 或 [World_State_5D] 中存在到期必收伏笔/期待感承诺或本章埋设事项，必须在本章通过具体器物与动作完成物理落实。\n"
            f"{tomato_task_flow}\n"
            "## 5. 任务上下文 (Task Input)\n"
            "### [Scene_Context]\n"
            f"{scene_outline}\n\n"
            "### [Style_Samples]\n"
            f"{style_profiles}\n\n"
            "---\n"
            "[现在开始生成正文]"
        )
        return system_prompt, user_prompt

    def compile_rewrite_prompt(
        self,
        scene: SceneOutline,
        state: WorldStateManager,
        draft_text: str,
        report: ScanReport,
        memory_pyramid: Optional[MemoryPyramid] = None,
    ) -> Tuple[str, str]:
        """Compile a deterministic surgical rewrite prompt when AI-TIC-CHECK detects a production incident."""
        base_sys, base_user = self.compile_scene_prompt(
            scene, state, memory_pyramid=memory_pyramid
        )

        violation_lines: List[str] = []
        for idx, v in enumerate(report.violations, start=1):
            violation_lines.append(
                f"{idx}. [{v.severity} | {v.rule_id} / {v.category}] 第 {v.line_number} 行命中：`{v.matched_text}`\n"
                f"   - 上下文片段：{v.context_snippet}\n"
                f"   - 整改要求：{v.fix_hint}"
            )
        violations_block = "\n".join(violation_lines)

        length_gap_block = ""
        if any(v.rule_id == "LEN_001" for v in report.violations):
            target_chars = target_chapter_chars(scene)
            shortfall = scene.min_chars - report.char_count
            event_lines = "\n".join(f"   {i}. {e}" for i, e in enumerate(scene.events, start=1))
            if shortfall <= 220:
                add_lo = shortfall + 25
                add_hi = max(add_lo + 30, min(scene.max_chars - report.char_count - 30, shortfall + 120))
                length_gap_block += (
                    "### 篇幅缺口（轻量精准补足，严禁大改扩写过头触发超限！）\n"
                    f"上一版只有 {report.char_count} 字，比硬性下限 {scene.min_chars} 字还少 {shortfall} 字。"
                    f"本次必须写到约 {target_chars} 字（安全区间 {scene.min_chars}～{scene.max_chars} 字）。\n"
                    f"做法：请**原样保留上版绝大多数合格段落**，仅在 2～3 处关键对白交锋或现场反应段里**净增约 {add_lo}～{add_hi} 字**；"
                    f"切勿大幅重写或过度扩写导致超过 {scene.max_chars} 字引起反复震荡！\n"
                    "严禁用反复摆弄道具、重复前段已说过的话、空泛议论或心理独白注水。\n"
                    f"{event_lines}\n\n"
                )
            else:
                length_gap_block += (
                    "### 篇幅缺口（本次重写的首要任务）\n"
                    f"上一版只有 {report.char_count} 字，比硬性下限 {scene.min_chars} 字还少 {shortfall} 字。"
                    f"本次必须写到约 {target_chars} 字（严禁超过 {scene.max_chars} 字）。\n"
                    "做法：保留上一版里已经合格的段落，不得删减；逐个把下面的章纲事件展开写透——"
                    "优先补足人物立场博弈、锋利对话交锋、证据/底牌亮出后的当场反应与利益兑现（长短句错落）；"
                    "严禁用反复摆弄道具、重复前段已说过的话、空泛议论或心理独白注水。\n"
                    f"{event_lines}\n\n"
                )
        if any(v.rule_id == "LEN_002" for v in report.violations):
            target_chars = target_chapter_chars(scene)
            excess = report.char_count - scene.max_chars
            if excess <= 280:
                trim_lo = excess + 25
                trim_hi = max(trim_lo + 30, min(report.char_count - scene.min_chars - 30, excess + 120))
                length_gap_block += (
                    "### 篇幅超限裁剪（轻量精准微裁，严禁大删过头跌破下限！）\n"
                    f"上一版共 {report.char_count} 字，仅超出硬性上限 {scene.max_chars} 字共 {excess} 字（安全区间 {scene.min_chars}～{scene.max_chars} 字）。\n"
                    f"本次请**完整保留所有核心剧情段落、全部必保伏笔关键词与引号对话**，仅精准删减 2～3 处冗余修饰/重复短句**净减约 {trim_lo}～{trim_hi} 字**（使终稿稳落在约 {target_chars} 字）；"
                    f"切勿大段删减导致跌破 {scene.min_chars} 字引起反复震荡！\n\n"
                )
            else:
                trim_target = max(excess + 40, report.char_count - target_chars)
                length_gap_block += (
                    "### 篇幅超限裁剪（本次重写的首要任务）\n"
                    f"上一版共 {report.char_count} 字，超出硬性上限 {scene.max_chars} 字共 {excess} 字。"
                    f"本次必须压缩到约 {target_chars} 字（安全区间 {scene.min_chars}～{scene.max_chars} 字，建议净删减约 {trim_target} 字）。\n"
                    "做法：完整保留所有章纲核心事件、全部必保伏笔关键词以及用于打断纯旁白墙的引号句；"
                    "精准删减重复环境铺陈、冗余修饰词与重复动作，严禁删减过度导致跌破下限。\n\n"
                )
        if any(v.rule_id == "RHYTHM_NARRATION_WALL" for v in report.violations):
            solo_hint = (
                "当前为单人独角戏，可通过主角低声自语/复核发现（如“外层新油，内层旧垢。”）、手机来电/短信屏幕引文、账本/刻字引文自然带出双引号。"
                if len(scene.active_characters) <= 1
                else "可在该区间中段穿插简短带引号“...”的人物对白、追问或书面/器物引文。"
            )
            length_gap_block += (
                "### 纯旁白墙切分指引（RHYTHM_NARRATION_WALL）\n"
                "门禁只把包含双引号“...”/「...」（或允许的系统面板【...】）的段落视为打断纯旁白墙；"
                "纯动作或景物描写即使再短也不会清零连续纯旁白字数！\n"
                f"做法：把违规段落区间按每 200～260 字切开，在中间自然嵌入简短引号句。{solo_hint}"
                f"同时注意维持全文总字数在 {scene.min_chars}～{scene.max_chars} 字区间内。\n\n"
            )

        if self.project.meta.panel_policy == "allow_system_tag":
            tail_instruction = (
                "[请直接输出修正后的完整章节正文，第一个字符必须是正文的汉字、合法左引号（“「『）或受控系统标签【，"
                "严禁前置空格或章节标题]"
            )
        else:
            tail_instruction = (
                "[请直接输出修正后的完整章节正文，第一个字符必须是正文的第一个汉字或合法左引号（“「『），"
                "严禁前置空格、章节标题或【...】面板]"
            )

        rewrite_user = (
            f"{base_user}\n\n"
            "================================================================\n"
            "## [PRODUCTION INCIDENT — AI-TIC-CHECK 自动重写指令]\n"
            f"上一版草稿触发了 {len(report.violations)} 项生产门禁违规，已被拦截打回！\n"
            "请在不改变章纲事件推进、人物行为锚点、器物物理状态、五维世界状态与伏笔细节的前提下，"
            "彻底消除以下全部违规项，并通篇复查确保不引入新的禁用词、对称句式或节奏违规：\n\n"
            f"{length_gap_block}"
            f"### 违规清单\n{violations_block}\n\n"
            f"### 被拦截的原草稿（共 {report.char_count} 字）\n"
            f"{draft_text}\n\n"
            "---\n"
            f"{tail_instruction}"
        )
        return base_sys, rewrite_user
