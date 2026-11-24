# 新写书产线（Pipeline v2）任务卡与基线规范

> 开发基线：`fd9d58e6ea2ec59997b418929f0a539c8f810d85`（`origin/ui-command-center`，已完整包含 `origin/main` `5c439b1`，且与韩国服务器线上 56 个包文件 SHA256 100% 一致）。

---

## 零、总体方案与核心原则

### 0.1 平台定位与试点验收 5 条硬标准
- **平台与改编定位**：番茄付费/广告长篇签约赚钱 → 有人读有热度 → 导出漫剧改编素材包（形象卡、名场面、分集梗概）；上线时按平台规定如实勾选 AI 使用声明，按日稳态更新，拒绝一次囤千章的 AI 水文模式。
- **试点（用“夜市算卦”点子重开第二版，写至前 30 章）验收 5 标准**：
  1. **盲比不输热门书**：跨家族强裁判拿 3 本对标热门书同位置前 3 章两两盲比，至少 2 次赢或打平；
  2. **把关人愿意追读**：`agy` 在第 3、10、30 章关键卡点把关放行，老公收简洁结论；
  3. **全书骨架不跑偏**：30 章里书名卖点与金手指持续在位，每章大纲三问（主角要什么、谁挡着、结果得失）全答得出；
  4. **不复读、不崩盘**：招牌动作在任意 10 章里 ≤2 次；数值符合现实量级上限；跨书 0 撞名；
  5. **可漫剧改编**：每卷 ≥2 个可画面化名场面，主要角色配外形卡。

### 0.2 新产线 6 环节编辑部流程
1. **选题立项（`wizard`）**：每步出 3 选，附对标热门书与漫剧适配卡，由把关人挑选。
2. **全书骨架（`book_blueprint.yaml`）**：总字数、6–8 卷卷卡（每卷目标/反派/爽点玩法/高潮/收获/进退场/名场面）、角色说话方式卡、数值上限策略、跨卷伏笔、写死结局、题材文风卡。**先批骨架再动笔**。
3. **卷纲与章纲（`planner`）**：严格在当前卷卡边界内展开章纲，答出三问、爽点类型、情绪波形、事件型钩子；换卷时同步更新角色、道具、伏笔。
4. **正文撰写（`compiler`）**：读全书骨架文风卡，废除写死铜钱范文；招牌动作近 3 章冷却；按角色说话方式卡区分腔调；字数用区间容差；句长波动检查仅限悬疑类。
5. **记忆状态（`memory` / `state`）**：修复永远保留第 1 章记忆的 Bug，改为当前角色身份卡（`current_role`）+ 虚拟时间锚点（第 d 天、距今 D−d 天）。
6. **三层检查与刹车（`quality_rules` / `judge` / `engine` / `matrix_scheduler`）**：L1 纯算法硬刹车 + L2/L3 跨模型两两盲比与卷级弧线会诊 + 不合格不落盘、连错 2 章自动停书、试跑上限（每 10 章暂停把关）。

### 0.3 13 处写死参数与 3 处静默默认值处置表

| # | 位置 | 原写死内容 | 处置方案 | 归属卡 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `planner.py` (`MASTER_OUTLINE_TOTAL/BATCH_SIZE`) | 总纲只规划 100 章，每批 25 章 | 替换为全书骨架（`BookBlueprint`） | T2 / T4 |
| 2 | `planner.py` (`extend_volume`) | 每次续 15 章，每 60 章强制换地图 | 卷长与换地图由卷卡决定 | T2 / T4 |
| 3 | `planner.py` (`_collect_extend_volume_meta_errors`) | 每换一卷数值上限必须严格高于上一卷 | 区分 `bounded_realistic` 硬上限与 `unbounded_fantasy` | T2 / T4 |
| 4 | `namer.py` (`generate_cast`) | 默认随机种子固定 `42` | 按项目高熵/显式随机并跨书排重 | T3 |
| 5 | `memory.py` (`EntityMemoryRecord`) | 永久保留 `appearances[0]` 第 1 章事实 | 改为保留最近 N 条 + 当前状态卡 + 时间锚点 | T6 |
| 6 | `critic_polisher.py` (`pass_score_threshold=75`) | 有及格线但引擎从未拦停 | 替换为硬门禁（L1 + 两两盲比 + 弧线会诊） | T7a / T7b |
| 7 | `compiler.py` | 写作提示内写死“铜钱”范文与 4–12 字刀锋短句 | 删掉写死范文，改读本项目文风卡 | T5b |
| 8 | `checker.py` (`_scan_rhythm_and_diagnostics`) | 全类目一律警告 Burstiness σ < 10 | 按题材 `rhythm_burstiness_check` 开关决定 | T5a |
| 9 | `compiler.py` (`_format_character_section`) | 每章原样贴全套 `behavioral_anchors` | 近 3 章用过的锚点标为冷却禁用 | T5a / T5b |
| 10 | `genres.py` / `checker.py` (`LEN_001`) | `min_chars` 少 1 字即打回重写 | 85%–100% 改为 `LEN_001W` 警告，<85% 才 CRITICAL | T5a |
| 11 | `wizard.py` (`scale` 步骤) | 优先推荐 300–700+ 章无限续卷 | 改为按目标总字数选（如 80 万 / 120 万 / 150 万） | T3 |
| 12 | `planner.py` (`plan_new_project`) | `pen_name` 默认值 `"砚山行"` | 静默默认值清零，缺项直接报错 | T3 |
| 13 | `loader.py` (`max_rewrite_retries`) | 缺省默认 `3` | 静默默认值清零，缺项直接报错（迁移路径除外） | T3 |

### 0.4 Windows 已知环境测试基线（决定 1）
在 Windows 11/10（Python 3.11.9，git 默认检出 CRLF）上，对干净基线 `fd9d58e` 运行全量测试：
- **总计 298 个测试**：272 通过，**26 个已知 Windows 环境报错**（24 个 `PermissionError [WinError 32]` 临时 SQLite 文件清理锁 + 2 个 `deploy/*.sh` CRLF 行尾断言），**零代码逻辑失败**。
- **验收铁律**：接受以下 26 个 ID 为本轮已知基线（不改既有基线测试）。**后续每张卡、每次合并的全量测试，失败/错误 ID 集合必须 ⊆ 下表 26 项；出现任何基线外失败/错误立即判 ❌ 不准提交。**

<details>
<summary>点击展开：26 个 Windows 已知基线失败/错误完整 ID 清单（按字母序）</summary>

1. `test_deploy_configs.TestDeployConfigs.test_deploy_remote_sh_strict_no_fallback_and_permissions`
2. `test_factory_v2_upgrades.TestPillar2CommercialTitlesAndWizardLongForm.test_wizard_scale_prompt_injects_genre_templates_long_form_and_supports_title_override`
3. `test_factory_v2_upgrades.TestSelfHealingAndAntiPingPongLength.test_scheduler_self_healing_recovers_stochastic_prose_and_blocks_structural_deadlocks`
4. `test_public_and_wizard.TestPublicCommandCenter.test_command_center_pipeline_event_schemas_and_real_usage_only`
5. `test_public_and_wizard.TestPublicCommandCenter.test_public_command_center_never_leaks_drafts_secrets_paths_ips_or_admin_routes`
6. `test_public_and_wizard.TestPublicCommandCenter.test_public_command_center_static_ui_contracts`
7. `test_public_and_wizard.TestPublicCommandCenter.test_public_pipeline_idle_state_honest_with_last_event_timestamp_and_replay_badge`
8. `test_public_and_wizard.TestPublicCommandCenter.test_public_pipeline_reconnect_recovers_full_authoritative_state`
9. `test_public_and_wizard.TestPublicStudio.test_book_view_is_transparent_settings_and_current_plan`
10. `test_public_and_wizard.TestPublicStudio.test_only_gate_passed_chapters_are_readable`
11. `test_public_and_wizard.TestPublicStudio.test_p1_2_public_status_text_distinguishes_manual_pause_from_quota_exhausted`
12. `test_public_and_wizard.TestPublicStudio.test_p2_1_public_book_ordering_active_first_newest_updated_first`
13. `test_public_and_wizard.TestPublicStudio.test_p2_1_public_studio_renders_current_plan_hook_contract`
14. `test_public_and_wizard.TestPublicStudio.test_p2_2_http_head_supported_for_static_health_and_public_routes_with_keepalive`
15. `test_public_and_wizard.TestPublicStudio.test_public_api_is_read_only`
16. `test_public_and_wizard.TestPublicStudio.test_public_api_open_admin_api_locked`
17. `test_public_and_wizard.TestPublicStudio.test_public_event_filter_hides_rewrites`
18. `test_public_and_wizard.TestPublicStudio.test_public_payloads_never_leak_private_fields`
19. `test_public_and_wizard.TestPublicStudio.test_static_page_routing`
20. `test_public_and_wizard.TestWizardHttpFlow.test_four_steps_with_reroll_then_ready`
21. `test_public_and_wizard.TestWizardHttpFlow.test_invalid_llm_proposal_is_reported_not_patched`
22. `test_public_and_wizard.TestWizardHttpFlow.test_invalid_proposal_is_fed_back_and_fixed`
23. `test_public_and_wizard.TestWizardHttpFlow.test_overlong_title_is_rejected_then_corrected`
24. `test_public_and_wizard.TestWizardHttpFlow.test_wizard_requires_admin`
25. `test_public_and_wizard.TestWizardWithoutProvider.test_wizard_reports_unconfigured_llm`
26. `test_workbuddy_deploy.TestWorkBuddyDeployScripts.test_install_script_syntax_and_all_security_invariants`

</details>

---

## 一、并行波次与本轮任务卡（M1 第一阶段）

- **第 0 波**：`T1`（基线对齐 + 任务卡建档）
- **第 1 波（5 卡并行）**：`T0`（对标调研）· `T2`（全书骨架格式）· `T5a`（题材开关/字数容差/冷却工具）· `T6`（记忆修复/身份/时间锚点）· `T7a`（质量规则与裁判）
- **第 2 波（下轮细化）**：`T3` · `T4` · `T5b` · `T7b`
- **第 3 波（下轮细化）**：`T8`（选拔赛）→ `T9`（“夜市算卦”第二版前 30 章试点）

> **本轮禁改文件（任何第 1 波卡均不得修改）**：`src/xiaoshuo_bot/compiler.py`、`src/xiaoshuo_bot/planner.py`、`src/xiaoshuo_bot/server/wizard.py`、`src/xiaoshuo_bot/server/matrix_scheduler.py`、`projects/baiwu_dukou/`、韩国服务器任何文件。

### 任务卡 T1：对齐线上代码基线与确认重叠
- 所属里程碑：M1
- 目标：暂停旧书并建立基于 `origin/ui-command-center`（`fd9d58e`）的干净开发集成 worktree，完成基线测试与近期提交重叠核查。
- 范围：`G:\xiaoshuo-bot-wt\integration`、`G:\xiaoshuo-bot-wt\baseline_failures.txt`、`docs/plans/pipeline_v2_task_cards.md`。
- 不在范围：不修改本机 `g:\xiaoshuo-bot` 主工作树、不删服务器文件、不修改业务代码。
- 依赖：无
- 实施步骤：
  1. 暂停韩国服务器 `book_20261120_064128_7c6c9e`（`PAUSED_MANUAL`）并三轮复查静止在 1,116 章。
  2. 验证 `origin/main` 是 `origin/ui-command-center` 祖先，将 `feat/pipeline-v2` worktree 切至 `fd9d58e`（与线上 56/56 包文件完全一致）。
  3. 清空密钥环境变量跑通基线全量测试，记录 26 项 Windows 已知基线 ID 到 `G:\xiaoshuo-bot-wt\baseline_failures.txt`。
  4. 检视 `c33aa60` / `e0f0d65` / `798ad34` / `ed947c4` 与本轮各卡的重叠点。
- 测试方法：`python -m unittest discover -s tests -p "test_*.py" -v`；`git merge-base --is-ancestor`。
- 验收标准：
  - [x] `feat/pipeline-v2` HEAD == `fd9d58e6ea2ec59997b418929f0a539c8f810d85`
  - [x] 全量测试 298 项中 272 通过、26 项纯 Windows 环境失败已归档
  - [x] 本地主仓未提交文件完好无损

### 任务卡 T0：番茄同类热门书对标调研
- 所属里程碑：M1
- 目标：调研 3 本（+2 备选）番茄热门玄学/算卦/直播/打脸都市脑洞书，提取可量化的节奏与开篇标尺供骨架与裁判使用。
- 范围：`docs/benchmarks/yeshi_suangua_benchmark_report.md`、`docs/benchmarks/yeshi_suangua_benchmark.yaml`；原文仅存仓库外 `G:\xiaoshuo-bot-data\benchmarks\yeshi_suangua\`。
- 不在范围：不修改任何源码；不破解验证码/登录墙；不把受版权保护的全文提交进 git。
- 依赖：T1
- 实施步骤：
  1. 检索并核实 3 本（+2 备选）近 12 个月连载或完结的番茄高人气同类书，记录书名、作者、标签、在读/评分/字数、URL 与截图。
  2. 浏览器只读获取每本免费前 3 章正文至 `G:\xiaoshuo-bot-data\benchmarks\yeshi_suangua\`（遇验证码/登录/字体混淆乱码立即停报）。
  3. 用脚本统计字数、段数、平均段长、对话占比、登场人物数。
  4. 产出中文分析报告（每本卖点、前 300 字结构、前 3 章节拍、爽点位置/间隔、金手指规则代价、人物配置、钩子、漫剧名场面，引文单处 ≤50 字）与结构化标尺 YAML。
- 测试方法：统计脚本输出 + `git status` 检查正文未入库 + 文件结构校验。
- 验收标准：
  - [ ] 3 本对标书出处、页面数据、截图路径真实完整
  - [ ] `yeshi_suangua_benchmark_report.md` 与 `.yaml` 数据和正文统计一致
  - [ ] git 仓内零对标原文全文

### 任务卡 T2：全书骨架（`BookBlueprint`）格式与硬校验
- 所属里程碑：M1
- 目标：新增全书骨架数据结构与聚合校验器，缺任何关键要素直接列出全部错误路径抛 `BlueprintValidationError`。
- 范围：新增 `src/xiaoshuo_bot/blueprint.py`、`tests/test_blueprint.py`、`tests/fixtures/blueprint_valid.yaml`。
- 不在范围：不改 `loader.py`、`models.py`、`planner.py`、`wizard.py`。
- 依赖：T1
- 实施步骤：
  1. 定义 `BookBlueprint` 等 dataclass，实现 `load_blueprint`、`validate_blueprint(bp, *, existing_cast_names)` 与 `collect_existing_cast_names(projects_root, exclude_project_id)`。
  2. 强校验 `premise`、`target_total_chars`、`chapter_chars_range`、`protagonist`、`golden_finger`、`scale_rules`（`bounded_realistic` 必含 `hard_max > 0`）、`volumes`（3–12 卷、章号 1 起连续无缺重、总字数偏差 ≤15%、第 2 卷起 `enters >= 1`、`key_scenes >= 2`）、`cast`（通过 `CharacterNamer.validate_name` 且与 `existing_cast_names` 零同名/零同音）、`foreshadow_plan`（`plant_volume <= payoff_volume` 且引用合法）、`ending`、`style`、`adaptation`；聚合全部错误一次抛出，不补默认值。
  3. 编写“夜市算卦”全新人名有效样例 `tests/fixtures/blueprint_valid.yaml`（禁用旧书角色名）与完整正反单元测试。
- 测试方法：`python -m unittest tests/test_blueprint.py -v` + 全量回归比对基线集合。
- 验收标准：
  - [ ] 有效样例 100% 通过校验
  - [ ] 每类规则反例及多错并报均能精确报出字段路径
  - [ ] 跨书同名与同音（含旧书四人名）均被拦截

### 任务卡 T5a：题材文风开关、字数容差与招牌动作冷却工具
- 所属里程碑：M1
- 目标：按题材精准控制句长波动检查开关、放宽字数下限至容差区间防止注水，并交付招牌动作冷却识别模块。
- 范围：`src/xiaoshuo_bot/genres.py`、`src/xiaoshuo_bot/checker.py`、新增 `src/xiaoshuo_bot/anchor_cooldown.py`、`tests/test_tomato_checker.py`、新增 T5a 测试文件，以及因 `LEN_001W` 必须同步的既有测试断言。
- 不在范围：不改 `compiler.py`、`models.py`、`projects/baiwu_dukou/`。
- 依赖：T1
- 实施步骤：
  1. `genres.py`：`GenreRecipe` 新增无默认值字段 `rhythm_burstiness_check: bool`（仅 `literary_suspense`/`suspense_rules`/`ancient_mystery` 为 `True`）与 `allow_pov_inner_monologue: bool`（仅 `literary_suspense` 为 `False`），在全部 11 个配方显式赋值并更新 `to_dict()`。
  2. `checker.py` 按决定 2 实现独立开关判定 helper（`scan`/`__init__` 显式参数 > `project_meta.genre_id` 查 `get_genre` 未知 id 抛 `UnknownGenreError` > `project_meta is None` 或 `genre_id == ""` 保持开启兼容旧项目），控制 `RHYTHM_BURSTINESS` 警告生成（`diagnostics["burstiness_sigma"]` 仍照算）。
  3. `checker.py` 调整 `LEN_001`：`non_ws_chars < floor(min_chars * 0.85)` 报 `CRITICAL` `LEN_001`；介于 `[floor(min_chars * 0.85), min_chars)` 改报 `WARNING` `LEN_001W`。
  4. 新增 `anchor_cooldown.py` 实现 `extract_anchor_fragments`、`anchors_used_in`、`cooling_anchors`。
  5. 更新 `test_tomato_checker.py::test_rhythm_warning_burstiness_and_bystander_shock_diagnostics` 为 `literary_suspense` 开启 + `urban_brainhole` 关闭两段校验，并补充边界与冷却单测。
- 测试方法：卡内新增与改动的测试 + 全量回归比对基线集合。
- 验收标准：
  - [ ] 11 个配方开关准确，显式参数与 `UnknownGenreError` 行为正确
  - [ ] `baiwu_dukou` 全章与零参数 `AITicChecker().scan()` 保持通过
  - [ ] `LEN_001`/`LEN_001W` 边界与 `anchor_cooldown` 在旧书复读样本上 100% 精准识别

### 任务卡 T6：修复第 1 章永久保留 Bug、角色当前身份与时间锚点
- 所属里程碑：M1
- 目标：根除“第 1,109 章还说昨儿砸摊”的记忆与角色冻龄 Bug，为上下文建立显式虚拟天数时间锚点与身份演化机制。
- 范围：`src/xiaoshuo_bot/memory.py`、`src/xiaoshuo_bot/models.py`、`src/xiaoshuo_bot/state.py`、`src/xiaoshuo_bot/engine.py`（仅 `record_chapter` 调用点）、新增 T6 测试文件，以及因 `record_chapter` 必填参数必须同步的既有测试调用。
- 不在范围：不改 `compiler.py`、`checker.py`、`planner.py`。
- 依赖：T1
- 实施步骤：
  1. `memory.py`：`EntityMemoryRecord.record_appearance` 取消强制保留 `appearances[0]`（只留最近 `max_kept` 条，`first_chapter` 数字保留）；`recall_entity` 仅输出“首现Ch.N”数字和最近 3 条事实，不输出第 1 章旧事实。
  2. `memory.py`：`ChapterMemoryEntry` 增必填 `virtual_day: int`；`record_chapter` 增必填关键字参数 `virtual_day: int`；`compile_context` 增过渡态参数 `current_virtual_day: Optional[int] = None`（非 None 时最前输出 `### [时间锚点] 当前第{D}天` 且 L1/L2/L3 带天数与距今天数；下轮 T5b 改必填）；新增显式迁移函数 `migrate_legacy_memory(data, chapter_day_map)`，普通 `from_dict` 缺 `virtual_day` 直接报错。
  3. `models.py` & `state.py`：`CharacterWorldState` 增 `current_role: Optional[str] = None`，`WorldStateManager.__init__` 从 `char.role` 初始化；`from_dict` 缺字段置 `None`，由 `apply_snapshot` 显式从 `project.characters[cid].role` 迁移并打 `logging.warning`；`CharacterMutation` 增可选 `role`，`apply_character_mutation` 出现时更新 `current_role` 并写入返回字典。
  4. `engine.py` 仅在全部 3 处 `record_chapter(...)` 调用点传入 `virtual_day`（`advance_after_chapter` 后传推进后的 `self.state.virtual_day`，重建历史路径按各章 `calendar_days_elapsed` 递推计算）。
- 测试方法：1,200 章回忆淘汰测试、时间锚点格式测试、旧快照/旧记忆显式迁移测试、`role` 演化测试 + 全量回归比对基线集合。
- 验收标准：
  - [ ] 模拟 1,200 章后 `recall_entity` 不含第 1 章旧事实文本
  - [ ] 普通 `from_dict` 缺 `virtual_day` 抛具名异常，`migrate_legacy_memory` 与旧快照 `current_role` 迁移日志清晰可测
  - [ ] 全量测试无新增失败/错误

### 任务卡 T7a：确定性质量红线规则与跨模型两两裁判模块
- 所属里程碑：M1
- 目标：实现 L1 跨章质检规则引擎与 L2/L3 独立盲比裁判（本轮仅新增模块并拿服务器真书试跑取证，暂不接引擎）。
- 范围：新增 `src/xiaoshuo_bot/quality_rules.py`、`src/xiaoshuo_bot/judge.py`、`tests/test_quality_rules.py`、`tests/test_judge.py`；只读试跑数据存仓库外 `G:\xiaoshuo-bot-data\fixtures_old_books\`。
- 不在范围：不改 `engine.py`、`critic_polisher.py`、`matrix_scheduler.py`、`cli.py`。
- 依赖：T1
- 实施步骤：
  1. `quality_rules.py` 实现 `run_l1_checks(...)` 输出 `RuleHit` 列表：`QR_ANCHOR_REPEAT`（10 章窗内片段 ≥10 字出现 >2 章）、`QR_VERBATIM_REPEAT`（30 章窗内 ≥15 字整段原样出现 ≥2 次）、`QR_SCALE`（越 `hard_max` 或正文中“亿”级在线/关注/粉丝异常数）、`QR_SELLING_POINT_DRIFT`（20 章窗内含卖点词章节占比 <40%）；参数缺失立即报错，无隐式兜底。
  2. `judge.py` 实现 `PairwiseJudge(provider, *, judge_model, writer_model)`：借助 `workbuddy_policy` 槽位与前缀映射判定同源/同族，相同直接抛 `SameModelJudgeError`；`compare` 随机分配 A/B 并交换顺序跑两次，两次不一致则判 `tie`，并输出理由与可能弃书段落；`arc_review` 打包连续章节审出 `同质化/原地踏步/爽感疲劳/工具人/跑偏` 问题；坏 JSON 严格抛 `JudgeResponseError`。
  3. 拿服务器两本旧书（《让你夜市算卦》第 1–60 章 + 最后 60 章、《三点十七分》全 72 章，拷至仓库外）只读试跑 `run_l1_checks` 验证精准度。
- 测试方法：`test_quality_rules.py`、`test_judge.py`（全假 provider 离线）+ 旧书试跑输出原文 + 全量回归比对基线集合。
- 验收标准：
  - [ ] 同族/同模型送审 100% 抛 `SameModelJudgeError`，交换顺序不一致判 `tie`
  - [ ] 旧书后 60 章命中复读/数值/跑偏红牌，《三点十七分》0 误报 `QR_SCALE` 与 `QR_VERBATIM_REPEAT`
  - [ ] 离线单测与全量回归无新增失败

---

## 二、下轮任务卡（第 2–3 波 · 目标与依赖，下轮规划细化）

- **T3（下轮细化）**：升级 `wizard.py`、`namer.py`、新增 `story_architect.py`，落盘全书骨架生成与把关审批流程，清零默认笔名/默认重写次数/固定种子 `42`，接入 `T0` 对标标尺。依赖：`T0, T2`。
- **T4（下轮细化）**：重写 `planner.py` 卷纲/章纲展开与换卷更新（在卷卡边界内展开、答三问、强制事件型钩子、换卷更新角色/道具/伏笔与 `current_role`）。依赖：`T2`。
- **T5b（下轮细化）**：升级 `compiler.py`（接入文风卡删写死铜钱范文、注入 `anchor_cooldown`、传入必填 `current_virtual_day` 并将 `memory.compile_context` 改为必填）。依赖：`T5a, T6`。
- **T7b（下轮细化）**：将 `quality_rules`、`PairwiseJudge`、连错 2 章刹车、每 10 章试跑把关上限接入 `engine.py` 与 `matrix_scheduler.py` / `cli.py`。依赖：`T7a`。
- **T8（下轮细化）**：白名单模型同题写稿盲比选拔赛，定主笔与裁判分工。依赖：`T5b, T7b`。
- **T9（下轮细化）**：用新产线重开“夜市算卦”第二版至前 30 章，并在第 3、10、30 章由 `agy` 把关验收后向老公汇报。依赖：`T3, T4, T5b, T7b, T8`。
