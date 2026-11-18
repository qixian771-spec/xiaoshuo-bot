from __future__ import annotations

from typing import List, Tuple

from .models import NovelProject, ScanReport, SceneOutline
from .state import WorldStateManager


class PromptCompiler:
    """Compiles dynamic asset-driven writer prompts and surgical incident rewrite prompts."""

    def __init__(self, project: NovelProject) -> None:
        self.project = project

    def compile_scene_prompt(
        self, scene: SceneOutline, state: WorldStateManager
    ) -> Tuple[str, str]:
        """Return (system_prompt, user_prompt) adhering strictly to the 5-section production spec."""
        pov_char = self.project.characters[scene.pov_character]

        # Format [Character_Identity_Anchor]
        char_blocks: List[str] = []
        for cid in scene.active_characters:
            c = self.project.characters[cid]
            pov_tag = (
                "【当前唯一限知视点角色（POV）】"
                if cid == scene.pov_character
                else f"【外部观察角色（严禁直接进入其内心独白，仅能由{pov_char.name}的感官观察其言行）】"
            )
            anchors_str = "；".join(f"{k}时表现为「{v}」" for k, v in c.behavioral_anchors.items())
            ooc_str = "；".join(c.forbidden_ooc) if c.forbidden_ooc else "无"
            char_blocks.append(
                f"- **{c.name}**（{c.role}）{pov_tag}\n"
                f"  - 性格底色：{c.personality}\n"
                f"  - 语言特征：{c.speech_style}\n"
                f"  - 行为锚点：{anchors_str}\n"
                f"  - OOC 禁区：{ooc_str}"
            )
        character_traits = "\n".join(char_blocks)

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

        scene_outline = (
            f"- **时空坐标**：{scene.location_time}\n"
            f"- **唯一限知视点人物**：{pov_char.name}（全文仅限{pov_char.name}的所见、所闻、触觉与判断）\n"
            f"- **目标篇幅区间**：{scene.min_chars}～{scene.max_chars} 汉字（事件写尽即止，严禁注水）\n"
            f"{continuity_block}"
            f"- **本章核心物理事件序列**：\n{events_formatted}\n"
            f"- **收束截断条件（写到此处立即停笔，绝不升华总结）**：{scene.stop_condition}"
        )

        # Format [Style_Samples]
        style_blocks: List[str] = []
        for sp_id in scene.style_profile_ids:
            sp = self.project.style_profiles[sp_id]
            style_blocks.append(
                f"#### 样本《{sp.name}》\n"
                f"- 颗粒度特征：{sp.granularity_notes}\n"
                f"- 原文切片：\n{sp.sample_text}"
            )
        style_profiles = "\n\n".join(style_blocks)

        system_prompt = (
            "# Role: 资深中文长篇小说作家\n\n"
            "## 1. 生产环境约束 (System Constraints)\n"
            f"- **视角**：严格执行第三人称限知视角（唯一视点人物：{pov_char.name}）。严禁上帝视角（如“殊不知”、“没有人知道”）、严禁切换其他角色内心（严禁出现“XX心想/暗想/心里明白”）、严禁叙述段出现第一人称“我”。\n"
            "- **输出格式**：仅输出正文。严禁标题、小标题、章节号、分割线、代码围栏、元信息、创作手记。输出的第 1 个字符必须是正文第 1 句的第 1 个汉字（不得有前置空格或空行）。\n"
            f"- **篇幅**：按章纲执行（控制在 {scene.min_chars}～{scene.max_chars} 字），事件写尽即止，严禁注水。\n"
            "- **风格采样**：参考下方的 [Style_Samples] 进行句子呼吸感、器物颗粒度与冷峻白描模拟。"
        )

        user_prompt = (
            "## 2. 资产状态 (Asset Driven) - 由系统动态注入\n"
            "### [Character_Identity_Anchor]\n"
            f"{character_traits}\n\n"
            "### [Current_Asset_State]\n"
            f"{asset_data}\n\n"
            "### [Foreshadow_Plan]\n"
            f"{foreshadow_plan}\n\n"
            "## 3. 叙事禁令 (Negative Constraints - AI-TIC-CHECK)\n"
            "- **绝对禁止**：\n"
            "  - 总结腔/升华词（如：“这一刻，命运的齿轮…”、“历史的车轮”、“总而言之”、“由此可见”、“某种意义上”、“不得不说”、“仿佛在诉说着”、“一切才刚刚开始”）。\n"
            "  - 身体反应套路（如：“不禁一愣”、“微微一愣”、“心中一紧”、“心头一震”、“眸光微闪”、“瞳孔微缩”、“眼底闪过一丝”、“嘴角勾起一抹”、“倒吸一口凉气”、“苦笑一声”、“喃喃自语”以及独立副词“不禁”）。\n"
            "  - 结构化对称（如：“不是A，而是B”、“并非A，而是B”、“与其说是A，不如说是B”、“是……，是……，是……”三连排比、“没有……，没有……，只有……”）。\n"
            "- **强制检查**：写完后，全文扫描是否出现上述禁用表达。命中任意一项，视为生产事故，自动触发重写。\n\n"
            "## 4. 任务执行流 (Task Flow)\n"
            "- 场景推进：必须基于当前的 [Scene_Context] 进行物理动作与对话推进，严格遵守 [Current_Asset_State] 中物件的物理位置与损耗状态。\n"
            "- 逻辑一致性：对话与微动作必须符合 [Character_Identity_Anchor] 的性格档案，严禁角色 OOC (Out of Character)。\n"
            "- 事件回收：若 [Foreshadow_Plan] 中存在到期必收事项或本章埋设事项，必须在本章通过具体器物与动作完成物理落实。\n\n"
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
    ) -> Tuple[str, str]:
        """Compile a deterministic surgical rewrite prompt when AI-TIC-CHECK detects a production incident."""
        base_sys, base_user = self.compile_scene_prompt(scene, state)

        violation_lines: List[str] = []
        for idx, v in enumerate(report.violations, start=1):
            violation_lines.append(
                f"{idx}. [{v.rule_id} / {v.category}] 第 {v.line_number} 行命中：`{v.matched_text}`\n"
                f"   - 上下文片段：{v.context_snippet}\n"
                f"   - 整改要求：{v.fix_hint}"
            )
        violations_block = "\n".join(violation_lines)

        rewrite_user = (
            f"{base_user}\n\n"
            "================================================================\n"
            "## [PRODUCTION INCIDENT — AI-TIC-CHECK 自动重写指令]\n"
            f"上一版草稿触发了 {len(report.violations)} 项生产门禁违规，已被拦截打回！\n"
            "请在不改变章纲事件推进、人物行为锚点、器物物理状态与伏笔细节的前提下，"
            "彻底消除以下全部违规项，并通篇复查确保不引入新的禁用词或对称句式：\n\n"
            f"### 违规清单\n{violations_block}\n\n"
            f"### 被拦截的原草稿（共 {report.char_count} 字）\n"
            f"{draft_text}\n\n"
            "---\n"
            "[请直接输出修正后的完整章节正文，第一个字符必须是正文的第一个汉字]"
        )
        return base_sys, rewrite_user
