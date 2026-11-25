# Pipeline v2.1（M2）：一卷一副本 + 每卷末六项硬体检锁卷 + 强模型定纲升级方案与验收记录

> **状态**：M2 全部 6 张任务卡（U1–U6）已实施、合并并完成离线全栈验收  
> **分支**：`feat/pipeline-v2`（基于 Pipeline v2.0.0 `027498f` 增量升级）  
> **核心铁律**：每卷末 6 项硬体检不合格立即锁死下一卷入口（大纲展开与引擎正文写入双端 0-token 拦截），必须返修重检拿到 `PASS` 才放行；总纲、分卷细纲与卷末体检默认走强推理模型 `wb/mimo-v2.6-pro`，日常单章写作沿用 `wb/deepseek-v4.1-flash`，严禁自动触碰手动档 `wb/hy4std`。

---

## 一、架构升级总览

1. **千章 30 卷蓝图模型（U1）**
   - `src/xiaoshuo_bot/blueprint.py`：`MAX_VOLUMES` 提升至 `40`；`VolumeSpec` 新增反套路重复与本味配额可选字段（`antagonist_motive`、`new_gameplay`、`mainline_step`、`daily_case_quota`、`fugitive_case_quota`、`forbidden_from_prev_volume`）；`CastMember` 新增独立配角成长线可选字段（`independent_goal`、`flaw_or_setback`、`stage_arcs`），对旧版蓝图 100% 向后兼容。
2. **卷末六项硬体检与锁卷状态机（U2）**
   - `src/xiaoshuo_bot/judge.py`：新增 `PairwiseJudge.volume_health_review(...)` 与 `VolumeHealthJudgeResult`，强制跨模型家族隔离、严格 6 项检查 ID 契约与 1 次格式纠错重试。
   - `src/xiaoshuo_bot/volume_health.py`：实现确定性检查 + 强裁判语义审查双融合的 6 项硬体检（`VH_01_NO_ROUTINE_REPEAT`、`VH_02_FORESHADOW_AND_REWARD`、`VH_03_CHARACTER_AND_ARC`、`VH_04_CORE_FLAVOR_QUOTA`、`VH_05_THREE_ACT_AND_HOOK`、`VH_06_LEDGER_AND_DEBT_MATH`），原子落盘 `audit/volume_health_vXXXX.json` 与 `audit/volume_locks.json`（`LOCKED_NEEDS_REPAIR` / `UNLOCKED_PASSED`），并提供 `assert_previous_volume_unlocked(...)` 严格锁检（锁表损坏抛 `ValueError`，未通过抛 `VolumeHealthLockError`）。
3. **强模型定纲 / 现用模型写正文路由（U3）**
   - `src/xiaoshuo_bot/llm/workbuddy_policy.py`：`WORKBUDDY_PLANNER_ROTATION_SLOTS` 升级为 `(mimo_v2_6_pro, deepseek_v4_1_flash)`，导出 `WORKBUDDY_STRONG_PLANNER_ROLES = ("planner", "architect", "volume_planner", "volume_auditor")` 与 `catalog.strong_planner_model()`；日常正文写作继续走现有模型配置；`hy4` 严格锁定仅限手动点选，禁止进入任何自动轮换链。
4. **大纲展开强锁门禁、防套路黑名单与配角弧线注入（U4）**
   - `src/xiaoshuo_bot/blueprint_planner.py`：`expand_blueprint_chapters(...)` 在进入第 `V > 1` 卷且开启锁检时，在任何 LLM 调用前调用 `assert_previous_volume_unlocked(...)`（0-token 拦截）；展开提示词自动注入上一卷玩法/反派动机/禁用清单防重黑名单，以及当前卷活跃配角的 `independent_goal`、`flaw_or_setback`、`stage_arcs`。
   - `src/xiaoshuo_bot/story_architect.py`：升级全书蓝图模板与提示词以支持最高 40 卷（千章 30 卷推荐）一卷一副本结构及配角独立成长线。
5. **引擎卷末硬刹车 + `volume-check` CLI + 云端测试清单对齐（U5）**
   - `src/xiaoshuo_bot/engine.py`：`NovelGenerationEngine` 新增 `volume_health_judge` / `enforce_volume_health_gate`；进入第 `V > 1` 卷前 0-token 强检锁表；写完卷末章自动触发 6 项体检，不通过立即抛 `VolumeHealthLockError` 锁卷停机。
   - `src/xiaoshuo_bot/cli.py`：新增 `xiaoshuo-bot volume-check` 子命令与 `execute_volume_check(...)`，支持返修后原地重跑 6 项体检并自动解锁下一卷（`PASS` 退出码 `0`，`FAIL` 退出码 `2`）。
   - `deploy/deploy_remote.sh`：同步注册全部 M2 新增测试模块，确保云端发布自动验收覆盖。
6. **1,000 章 / 30 卷实战项目落地与全栈 E2E 演练（U6）**
   - `tests/test_u6_m2_e2e_volume_lifecycle.py`：离线全栈 loopback E2E 测试，覆盖「强弱模型分工 → 卷 1 自动体检 FAIL 锁卷 → 卷 2 大纲与引擎双端 0-token 拦截 → 返修卷 1 → CLI `volume-check` 6/6 PASS 解锁 → 卷 2 强模型展开防重大纲 + 现用模型写正文」。
   - 同步将老公拍板的《夜市算卦：一卦一个通缉犯》1,000 章 / 30 卷总纲物化为通过 `load_blueprint`、`validate_blueprint` 与 `load_project` 严格校验的项目目录 `yeshi_v3_1000`。

---

## 二、常用命令参考

### 返修后手动重跑某卷 6 项体检并解锁下一卷

```bash
xiaoshuo-bot volume-check \
  --project <PROJECT_DIR> \
  --output <OUTPUT_DIR> \
  --volume 1
```

- 6/6 全部通过：写入 `audit/volume_health_v0001.json` 与 `audit/volume_locks.json`（`UNLOCKED_PASSED`），退出码 `0`。
- 任意 1 项不通过：保持 `LOCKED_NEEDS_REPAIR`，输出逐项返修单与章号，退出码 `2`。
