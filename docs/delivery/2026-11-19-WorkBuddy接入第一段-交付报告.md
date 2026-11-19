# 2026-11-19 WorkBuddy 接入·第一段（代码与部署脚本，不上服务器）总交付报告

> **工作区**：`/Users/niuniu/Desktop/小说机器人-wb1`（独立 worktree，未触动主目录 `/Users/niuniu/Desktop/小说机器人` 及 `t5-v3`、`p1-1-hide-matrix-companion`、`batch1`）
> **分支与基线**：`wb1`，切自 `batch1`（`c04341d`）
> **交付日期**：2026-11-19
> **远程与线上状态**：**未 `git push`、未合并 `main`、未部署、未执行任何部署脚本、未修改服务器 `kr` 任何文件**（仅对 `kr` 做过 1 次只读工具链检查：`uname -m` / `which gcc git curl tar sha256sum openssl systemctl` / `go version`）
> **真模型调用**：**未验证（待部署与 WorkBuddy 登录后补做）**——当前无可用写作模型，全部测试均为离线单测、本地回环模拟 CPA 测试与本地模拟数据浏览器截图实测。

---

## 一、3 张卡完成总览

| 卡号 | 主题 | 状态 | 本地 Commit | 全量 `pytest` 汇总行原文 | 页面截图 |
|---|---|---|---|---|---|
| 卡 1 | 路由与轮换（白名单强约束 + 额度用尽判定 + 固定轮换链 + DB/SSE 切换记录） | ✅ 完成 | `78171e3` | `253 passed, 103 subtests passed in 63.78s (0:01:03)` | 无页面改动 |
| 卡 2 | 模型配置与启动自检（零猜测 ID + CPA `/v1/models` 拒绝启动自检 + 每章三处模型记录与后台显示） | ✅ 完成 | `8904f17` | `258 passed, 108 subtests passed in 65.15s (0:01:05)` | ✅ 3 张（a 切换提示 / b 全部用尽熔断 / c 章节模型显示） |
| 卡 3 | 服务器部署脚本（CPA `v8.0.69` 校验 + `cpa-workbuddy` `2a1a608` 源码编译 + `auto_checkin_enabled: false` + 老公中文登录指南 + 防篡改测试；**只写不跑**） | ✅ 完成 | `bc813d6` | `262 passed, 108 subtests passed in 65.48s (0:01:05)` | 无页面改动 |

### 老公拍板约束与硬规则核验

| 约束 / 硬规则 | 结论 | 核验证据 |
|---|---|---|
| 积分签到只在 Mac 手动领，服务器 `auto_checkin_enabled: false`，代码和脚本零自动签到 | ✅ | `deploy/cpa.workbuddy.config.example.yaml:53`（`auto_checkin_enabled: false`）；`deploy/install_cpa_workbuddy.sh:231-238`（渲染后强校验）；`tests/test_workbuddy_deploy.py` `test_zero_auto_checkin_enabled_anywhere_in_code_or_deploy` 全仓扫描 `src/` 与 `deploy/` 通过 |
| 模型白名单仅 4 个（`Hy4 flash`、`Hy4`、`DeepSeek-V4.1-Flash`、`Xiaomi MiMo-V2.6-Pro`）；禁自动路由（`auto`/快速/均衡/极致）与 `DeepSeek-V4-Pro` | ✅ | `src/xiaoshuo_bot/llm/workbuddy_policy.py:129-227`；单测 `test_workbuddy_routing.py`、`test_workbuddy_config_and_tracking.py` 全通过 |
| 写稿首选 `Hy4 flash`；立项（`planner`）首选 `DeepSeek-V4.1-Flash`；轮换链 `Hy4 flash → DeepSeek-V4.1-Flash → Xiaomi MiMo-V2.6-Pro`（`planner` 从 `V4.1-Flash` 起）；`Hy4` 不进自动轮换 | ✅ | `src/xiaoshuo_bot/llm/workbuddy_policy.py:34-43,454-475`、`src/xiaoshuo_bot/llm/router.py:507-557`；单测验证顺序与 `Hy4` 拒绝进入轮换链 |
| 每次切换模型：复用 P1-2 告警组件明显提示「从 X 切到 Y，原因」并写库；全部用尽红条熔断停机 | ✅ | `src/xiaoshuo_bot/server/stockpile_db.py:382,494,544`、`matrix_scheduler.py:256-294`、`app.py:591-598`、`app.js:781,878`；本地浏览器截图 (a) 与 (b) 实测可见 |
| 非额度原因（网络错误、超时、5xx、流截断、内容门禁失败、超预算等）绝不换模型 | ✅ | `workbuddy_policy.py:268-337`、`router.py:854-975`；单测 `test_non_quota_errors_and_budget_breaker_never_rotate_model` 覆盖验证 |
| 每章记录实际模型：章节 meta 文件、SQLite、后台页面三处一致 | ✅ | `engine.py:1015-1095`、`stockpile_db.py:418-425,1120-1215`、`matrix_scheduler.py:820-850`、`app.js:2124,2160`；端到端单测 + 截图 (c) 实测一致 |
| 不推送、不合并 `main`、不部署、不运行部署脚本、不 `ssh` 改服务器、不删现有功能/测试、零真模型声称 | ✅ | 全程仅本地 `wb1` 分支提交；`tests/` 既有断言零改删放宽；`install_cpa_workbuddy.sh` 仅用 `bash -n` 静态语法检查未执行 |

---

## 二、逐卡交付明细

### 卡 1：路由与轮换

- **Commit 号**：`78171e3`（`feat(WB1-card1): implement WorkBuddy whitelist policy, quota rotation, and DB/SSE switch logging`）
- **改动文件清单（9 个）**：
  - `M deploy/deploy_remote.sh`（将新单测加入 `cloud_tests` 数组）
  - `M src/xiaoshuo_bot/llm/__init__.py`
  - `M src/xiaoshuo_bot/llm/base.py`
  - `M src/xiaoshuo_bot/llm/openai_compat.py`
  - `M src/xiaoshuo_bot/llm/router.py`
  - `A src/xiaoshuo_bot/llm/workbuddy_policy.py`
  - `M src/xiaoshuo_bot/server/matrix_scheduler.py`
  - `M src/xiaoshuo_bot/server/stockpile_db.py`
  - `A tests/test_workbuddy_routing.py`
- **全量 `pytest` 汇总行原文**：
  `253 passed, 103 subtests passed in 63.78s (0:01:03)`
- **页面截图**：无页面改动
- **验收标准逐条核验**：

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 额度用尽判定依据来自 `ygq-future/cpa-workbuddy @ 2a1a608` 与 CPA `v8.0.69` 真实源码，无猜测 | ✅ | 见第三节逐条 `文件:行号`；实现位于 `src/xiaoshuo_bot/llm/workbuddy_policy.py:268-337`、`base.py:67-84`、`openai_compat.py:167-205,286-297,441-452` |
| 2 | 白名单默认拒绝 + 硬拦截 `DeepSeek-V4-Pro` 与自动路由（`auto`/快速/均衡/极致）；`Hy4` 严禁进入自动轮换链，仅可手动指定 | ✅ | `workbuddy_policy.py:129-227`、`router.py:100-156`、`matrix_scheduler.py:1113-1143`；单测 `test_forbidden_models_and_whitelist_default_deny`、`test_hy4_manual_only_cannot_enter_rotation_chain`、`test_manual_hy4_never_rotates_on_quota_exhaustion` 全通过 |
| 3 | 严格单向轮换顺序：`writer` 按 `Hy4 flash → DeepSeek-V4.1-Flash → Xiaomi MiMo-V2.6-Pro`；`planner` 按 `DeepSeek-V4.1-Flash → Xiaomi MiMo-V2.6-Pro`；全部用尽抛 `QuotaExhaustedError` 停机，不切其他模型 | ✅ | `router.py:608-980`（独立按 `(role, model)` 隔离 key 冷却，防止同 CPA client key 误锁下一模型）；单测 `test_writer_and_planner_fixed_rotation_order_and_all_exhausted_halt` 用本地模拟 CPA HTTP 服务端到端验证通过 |
| 4 | 网络错误、超时（408/504）、5xx（500/502/503）、鉴权/风控 401/403（`11140`/`model_permission_denied`）、400 边界码 `140185`、流截断、预算熔断均不触发换模型 | ✅ | `workbuddy_policy.py:268-337`、`router.py:920-950`；单测 `test_quota_exhaustion_classifier_matches_cpa_workbuddy_2a1a608_evidence` 与 `test_non_quota_errors_and_budget_breaker_never_rotate_model` 全通过 |
| 5 | 每次切换写 SQLite `model_switch_events` 并广播 P1-2 `SSE_CPA_QUOTA_ALERT`（`kind="model_switched"`）；全部用尽转 `PAUSED_QUOTA_EXHAUSTED` 并广播 `kind="quota_exhausted"` | ✅ | `stockpile_db.py:382-398,494-590`、`matrix_scheduler.py:256-294,754-800`；单测 `test_scheduler_records_switch_in_db_and_emits_p1_2_sse_alert_and_halts_when_exhausted` 全通过 |
| 6 | 真实 WorkBuddy 上游额度耗尽实测 | 未验证 | 无可用写作模型（列入第三节「部署后必须实测」清单） |

---

### 卡 2：模型配置与启动自检 + 每章模型记录与页面显示

- **Commit 号**：`8904f17`（`feat(WB1-card2): add WorkBuddy model catalog config, CPA /v1/models startup self-check, and per-chapter model tracking`）
- **改动文件清单（13 个）**：
  - `M deploy/deploy_remote.sh`（将新单测加入 `cloud_tests` 数组）
  - `M src/xiaoshuo_bot/cli.py`
  - `M src/xiaoshuo_bot/contracts.py`
  - `M src/xiaoshuo_bot/engine.py`
  - `M src/xiaoshuo_bot/llm/__init__.py`
  - `M src/xiaoshuo_bot/llm/router.py`
  - `M src/xiaoshuo_bot/llm/workbuddy_policy.py`
  - `M src/xiaoshuo_bot/server/app.py`
  - `M src/xiaoshuo_bot/server/matrix_scheduler.py`
  - `M src/xiaoshuo_bot/server/static/app.js`
  - `M src/xiaoshuo_bot/server/static/styles.css`
  - `M src/xiaoshuo_bot/server/stockpile_db.py`
  - `A tests/test_workbuddy_config_and_tracking.py`
- **全量 `pytest` 汇总行原文**：
  `258 passed, 108 subtests passed in 65.15s (0:01:05)`
- **页面截图路径（本地起服务 + 模拟数据 + 内置浏览器实测 + 人工视觉核验；预览服务已关闭）**：
  - **(a) 切换模型的提示**（琥珀色横幅 `🔄 写作模型已切换`，清晰显示 `从 Hy4 flash (wb/cfg-hy4-flash) 切到 DeepSeek-V4.1-Flash (wb/cfg-ds-v41-flash)` 与 `原因：HTTP 429 · WorkBuddy 模型限流/额度用尽 (rate_limit_exceeded)`）：
    `/Users/niuniu/.gemini/antigravity/brain/b710f6f7-31c0-4030-904c-36ef4ff24657/browser_evidence/ac3606c3-3912-4dce-bf70-c4b5af0a0a59/final_step_8.png`
  - **(b) 全部用尽的熔断告警**（红色横幅 `🚨 超预算 / 全部额度用尽 · 熔断停机`，显示 `旧港密室` 白名单轮换链全部 3 档耗尽 + `upstream code 14018` 及书目卡片红色高亮）：
    `/Users/niuniu/.gemini/antigravity/brain/b710f6f7-31c0-4030-904c-36ef4ff24657/browser_evidence/ac3606c3-3912-4dce-bf70-c4b5af0a0a59/final_step_24.png`
  - **(c) 章节显示所用模型**（阅读中心左侧章节目录每章展示 `模型：wb/cfg-hy4-flash`、`模型：wb/cfg-ds-v41-flash` 且无折行截断，右侧正文元信息 `#reader-chapter-model` 显示 `模型：wb/cfg-ds-v41-flash`）：
    `/Users/niuniu/.gemini/antigravity/brain/b710f6f7-31c0-4030-904c-36ef4ff24657/browser_evidence/ac3606c3-3912-4dce-bf70-c4b5af0a0a59/final_step_30.png`
- **验收标准逐条核验**：

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 4 个白名单模型上游 ID 全部配置化（`WORKBUDDY_MODEL_HY4_FLASH` / `WORKBUDDY_MODEL_HY4` / `WORKBUDDY_MODEL_DEEPSEEK_V4_1_FLASH` / `WORKBUDDY_MODEL_MIMO_V2_6_PRO`，支持 env / CLI / `router_config`），零硬编码猜测值，缺一项或配禁止/额外项即报错 | ✅ | `workbuddy_policy.py:45-53,347-485`、`router.py:507-624`、`cli.py:626-710,1403-1408`；单测 `test_catalog_requires_explicit_config_for_all_4_slots_and_rejects_extra_or_forbidden`、`test_role_defaults_from_catalog_and_cli_router_builder` 通过 |
| 2 | 服务启动时向 CPA `GET /v1/models` 核对：任一配置模型不存在、CPA 不可达、或配置了白名单外/禁止模型，直接抛 `LLMConfigurationError` 拒绝启动并写明具体是哪个模型 | ✅ | `workbuddy_policy.py:511-596`、`server/app.py:1215-1255`（在打开 DB / 绑定端口之前执行）；单测 `test_startup_self_check_verifies_cpa_v1_models_and_refuses_missing_or_unwhitelisted` 覆盖 5 种场景全部通过 |
| 3 | 每章所用模型三处一致：章节 `chapter_XXXX.meta.json`、SQLite（`chapters.model` & `production_stats.model` 含旧库自动迁移）、后台网页（章节列表 + 阅读器头部） | ✅ | `engine.py:712-719,1015-1095`、`contracts.py:610`、`stockpile_db.py:241,363,418-425,595-618,1120-1215`、`matrix_scheduler.py:820-852`、`app.js:2124,2160`、`styles.css:1414-1435,1574-1590`；端到端单测 `test_chapter_model_consistent_across_meta_sqlite_and_api_and_ui_contract` + `test_legacy_db_migrates_chapters_and_production_stats_model_column` + 截图 (c) 全通过 |
| 4 | 后台刷新页面也能从 `GET /api/model-switches` 回显历史切换提示「从 X 切到 Y，原因」 | ✅ | `contracts.py:52,97`、`server/app.py:591-598`、`app.js:27,781-811,878,2642`；截图 (a) 与 (b) 实测可见 |
| 5 | 连接真实服务器 CPA `/v1/models` 实测启动自检 | 未验证 | 未部署上服务器（列入第三节「部署后必须实测」清单） |

---

### 卡 3：服务器部署脚本（只写不跑）

- **Commit 号**：`bc813d6`（`feat(WB1-card3): add CPA v8.0.69 + cpa-workbuddy source-build deploy kit, Chinese login guide, and tamper-detection tests`）
- **改动文件清单（8 个）**：
  - `A deploy/WORKBUDDY_LOGIN_GUIDE.md`
  - `A deploy/cpa-workbuddy.service`
  - `A deploy/cpa.workbuddy.config.example.yaml`
  - `M deploy/deploy_remote.sh`（将 `tests/test_workbuddy_deploy.py` 加入 `cloud_tests`）
  - `M deploy/env.example`（补充 4 个 `WORKBUDDY_MODEL_*` 占位符）
  - `A deploy/install_cpa_workbuddy.sh`
  - `M tests/test_deploy_configs.py`（在 `ALL_DEPLOY_FILES` 注册 4 个新建 `deploy/` 文件接受安全扫描）
  - `A tests/test_workbuddy_deploy.py`
- **全量 `pytest` 汇总行原文**：
  `262 passed, 108 subtests passed in 65.48s (0:01:05)`
- **页面截图**：无页面改动
- **验收标准逐条核验**：

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | CPA 固定 `v8.0.69`，下载后校验官方 `checksums.txt` 与脚本内置官方 SHA256（`linux_amd64` / `linux_aarch64`，均选用支持插件的构建而非 `_no-plugin`），不符立即中止 | ✅ | `deploy/install_cpa_workbuddy.sh:22-28,136-162`（官方 SHA256 取自 `router-for-me/CLIProxyAPI` `v8.0.69/checksums.txt` 原文）；`tests/test_workbuddy_deploy.py` 校验及改坏版本号/摘要的篡改测试均通过 |
| 2 | 插件 `cpa-workbuddy` 从源码编译：`git clone` → `git checkout 2a1a608` → 核对 `git rev-parse HEAD` 严格等于 `2a1a608fb0ff28c51996393822a7f36df4367d5b`，不符立即中止；`go build -trimpath -buildmode=c-shared` 编译 `.so`；严禁下载作者预编译 zip | ✅ | `deploy/install_cpa_workbuddy.sh:30-33,173-196`；`tests/test_workbuddy_deploy.py` 正向与篡改提交号/注入 zip 下载反向测试均通过 |
| 3 | CPA 只监听 `127.0.0.1`、`allow-remote: false`；管理密钥与客户端密钥在服务器上用 `openssl rand -hex 32` 现场生成，仓库内仅占位符，零真实密钥/公网 IP | ✅ | `deploy/cpa.workbuddy.config.example.yaml:11-39`、`deploy/install_cpa_workbuddy.sh:210-230`、`deploy/cpa-workbuddy.service:1-39`、`deploy/env.example:34-40`；`test_workbuddy_deploy.py` 与 `test_deploy_configs.py` 全通过 |
| 4 | `config` 模板强制 `auto_checkin_enabled: false`，且全仓 `src/` 与 `deploy/` 零自动签到启用或调用 | ✅ | `deploy/cpa.workbuddy.config.example.yaml:53`、`deploy/install_cpa_workbuddy.sh:231-238`；`test_zero_auto_checkin_enabled_anywhere_in_code_or_deploy` 与篡改 `false→true` 失败测试均通过 |
| 5 | 写好给老公看的中文登录步骤说明（SSH 隧道 + 老公亲自点授权链接 + 查 `/v1/models` 填 4 个白名单环境变量） | ✅ | `deploy/WORKBUDDY_LOGIN_GUIDE.md:1-106`；单测检查关键要素齐全且零密钥泄漏 |
| 6 | 自动化测试覆盖版本号、提交号、本机监听、签到关闭、无密钥写进仓库，且任意一条改坏测试即失败；`bash -n` 语法校验通过 | ✅ | `tests/test_workbuddy_deploy.py`（含 `test_tamper_mutations_fail_validation` 7 项篡改反向测试 + `bash -n`）；全量 pytest `262 passed` |
| 7 | 在服务器 `kr` 上实际运行安装脚本 | 未验证 | 按硬规则 1 **只写不跑**；注：只读排查发现 `kr` 当前已具备 `gcc`/`git`/`curl`/`tar`/`sha256sum`/`openssl`/`systemctl`（Debian 13 x86_64），但尚未安装 Go `1.27.1+`（`cpa-workbuddy/go.mod:3` 要求），已写入登录指南前置条件及脚本报错提示 |

---

## 三、额度用尽判定依据（源码 `文件:行号`）与「部署后必须实测」清单

### 3.1 核实到的插件与 CPA 宿主源码依据（只读审计，绝无猜测）

核查基线：
- 插件仓库：`https://github.com/ygq-future/cpa-workbuddy` checkout `2a1a608`（实测 `git rev-parse HEAD` = `2a1a608fb0ff28c51996393822a7f36df4367d5b`）
- CPA 宿主：`https://github.com/router-for-me/CLIProxyAPI` tag `v8.0.69`

| 信号类别 | 精确判定依据（源码 `文件:行号`） | CPA `v8.0.69` 透传到客户端的形态 | 本系统处理动作 |
|---|---|---|---|
| **① 积分包额度用尽（`credit_package_exhausted` / `14018`）** | `cpa-workbuddy/internal/workbuddy/chat_errors.go:95-96`：匹配上游 code `"14018"` 或正文 `"credits exhausted"` / `"额度已用尽"` → 返回 `Code="credit_package_exhausted"`, `Message="WorkBuddy requires an active credit package with available balance (upstream code 14018)"`, `UpstreamCode="14018"`, **`HTTPStatus` 保留上游状态码（`chat_faults_test.go:21` 实证可为 HTTP `400`！）**；`internal/plugin/runtime.go:310-322` 经 RPC 封装为 `rpcError` | CPA `internal/pluginhost/rpc_client.go:455-460` 保留 `HTTPStatus` 与 `Message`；`sdk/api/handlers/handlers_errors.go:108-159` 原样下发该 HTTP 状态码（如 `400`）和含 `upstream code 14018` 的 JSON 错误体 | **判定为额度用尽** → 严格按固定顺序切往下一白名单模型（写 DB + 发 P1-2 提示）；若全部用尽则熔断停机 |
| **② 模型限流 / 额度触顶（HTTP `429` / `rate_limit_exceeded`）** | `cpa-workbuddy/internal/workbuddy/chat_errors.go:107-108`：上游 HTTP `429` → 返回 `Code="rate_limit_exceeded"`, `Message="WorkBuddy rate limit exceeded"`, `HTTPStatus=429` + `RetryAfter`（`chat_faults_test.go:25,29`、`internal/plugin/runtime_retry_test.go:28,77`） | CPA `internal/pluginhost/rpc_client.go:51-106,455-460` 与 `sdk/api/handlers/handlers_errors.go:108-122` 保留 HTTP `429` 与 `Retry-After` 头 | **判定为额度用尽/限流** → 同上按固定顺序轮换；全部用尽则熔断停机 |
| **③ 非额度错误（明确不换模型）** | `cpa-workbuddy/internal/workbuddy/chat_errors.go:91-94,97-106,109-112`、`chat_response.go:39-47,85-90`、`invalidation.go:11-62`、`chat_faults_test.go:20,24,28`：<br>- `10085` `client_version_rejected` (502)<br>- `11140` `account_restricted` (403)<br>- `11159`/`11153`/`11128` 协议/提示词形态错误 (400)<br>- `context_length_exceeded` (400)<br>- `model_permission_denied` (403)<br>- `upstream_timeout` (408/504)、`upstream_unavailable` (5xx)、`upstream_stream_*` (502)<br>- `12153`/`invalid_token`/`invalid_grant` (401)<br>- **边界码 `140185`**（`chat_faults_test.go:28` 明确要求不能误匹配 `14018`） | 按各自 HTTP 状态码透传，网络中断时 `status_code=None` | **严禁换模型**；直接按现有 `LLMRequestError` / `LLMStreamTruncatedError` / 门禁隔离 / `BudgetCircuitBreakerError` 规则处理 |

### 3.2 「未查到」与「部署后必须实测」清单（绝不猜测）

以下 6 项在 `cpa-workbuddy @ 2a1a608` 与 CPA `v8.0.69` 静态源码中**未查到**确切运行态表现，**部署并由老公登录后必须逐条实测验证**：

1. **4 个白名单模型的真实 CPA 上游 ID（未查到完整确切值）**：`cpa-workbuddy` 采用动态模型发现（`internal/workbuddy/model_catalog.go`），部署登录后必须先调 `GET http://127.0.0.1:8317/v1/models` 拿到 `Hy4 flash`、`Hy4`、`DeepSeek-V4.1-Flash`、`Xiaomi MiMo-V2.6-Pro` 的真实完整 ID 填入 `/etc/xiaoshuo-bot/xiaoshuo.env`，并验证 `xiaoshuo-bot` 启动自检放行；同时核对返回的自动路由档位和 `DeepSeek-V4-Pro` 实际 ID 是否全部被 `find_forbidden_model_reason()` 拦截。
2. **单模型免费/日额度用尽的真实上游状态码与错误码（未查到）**：`cpa-workbuddy @ 2a1a608` 源码仅显式映射了 HTTP `429`（`rate_limit_exceeded`）与 `14018`（`credit_package_exhausted`），未见历史社区提及的 `6004` 或其他单模型限额错误码；部署后须实测当 `Hy4 flash` 免费/日额度用尽时，WorkBuddy 实际返回的是 HTTP `429`、HTTP `400 + 14018` 还是其他新错误码（如另有新错误码再按实测补入 `classify_workbuddy_quota_exhausted()`）。
3. **账号级积分耗尽（`14018`）与单模型限额的区别（未查到）**：若单个 WorkBuddy 账号返回 `14018`（积分包余额归零），实测同账号下切换 `DeepSeek-V4.1-Flash` / `Xiaomi MiMo-V2.6-Pro` 时是能继续消耗对应模型额度还是同样返回 `14018`（当前代码会依次尝试并在全部耗尽后立刻停机红条告警，安全兜底不会死循环）。
4. **CPA `v8.0.69` 宿主对 `credit_package_exhausted`（HTTP `400`）的具体 JSON 错误体格式（部分已查，需实测核对）**：源码确认 `handlers_errors.go:148-159` 会写出含 `upstream code 14018` 的 `message`，但是否在顶层或 `error.code` 字段保留 `"credit_package_exhausted"` 须在实机触发或日志抓包确认（当前分类器已同时兼容 `error.code`、`upstream_code` 与 `message` 中的严格边界 `upstream code 14018`）。
5. **CPA 多账号全冷却时宿主的最终 HTTP 状态码（需实测核对）**：若 CPA 侧所有账号均因 429 冷却，实测 CPA `v8.0.69` 返回给客户端的是 HTTP `429` 还是 `503 auth_unavailable`（若返回 503 且消息指明冷却，可按实测决定是否纳为触发条件；当前为遵守「非额度错误/5xx 不乱切模型」原则，未对 503 盲目切模型）。
6. **服务器 `kr` 源码编译前置环境**：本次只读检查实测 `kr`（Debian 13 x86_64）已有 `gcc`/`git`/`curl`/`tar`/`sha256sum`/`openssl`/`systemctl`，但**尚未安装 Go `1.27.1+`**（`cpa-workbuddy/go.mod:3` 要求）；老公执行 `deploy/install_cpa_workbuddy.sh` 前需先在 `kr` 安装 Go `1.27.1+`，部署完成后再实测 `cpa.service` 成功加载 `cpa-workbuddy.so` 并确认插件面板中 `auto_checkin_enabled` 为关闭态。

---

## 四、所有改动过的已有测试的 `git diff` 与理由

经 `git diff c04341d..bc813d6 -- tests/` 全量核对：
- **全批 3 张卡中，所有 25 个既有测试文件的已有断言零修改、零删除、零放宽、零跳过（零 `skip`/`xfail`）**；
- **唯一触及的既有测试文件仅为 `tests/test_deploy_configs.py`**（卡 3 / commit `bc813d6`），仅在文件顶部常量表与 `ALL_DEPLOY_FILES` 元组中**新增注册**本次在 `deploy/` 下创建的 4 个新文件：

```diff
--- a/tests/test_deploy_configs.py
+++ b/tests/test_deploy_configs.py
@@ -16,23 +16,31 @@ REPO_ROOT = Path(__file__).resolve().parents[1]
 DEPLOY_DIR = REPO_ROOT / "deploy"
 
 CPA_CONFIG = DEPLOY_DIR / "cpa.config.example.yaml"
+CPA_WORKBUDDY_CONFIG = DEPLOY_DIR / "cpa.workbuddy.config.example.yaml"
 CPA_SERVICE = DEPLOY_DIR / "cpa.service"
+CPA_WORKBUDDY_SERVICE = DEPLOY_DIR / "cpa-workbuddy.service"
 BOT_SERVICE = DEPLOY_DIR / "xiaoshuo-bot.service"
 CADDYFILE = DEPLOY_DIR / "Caddyfile.example"
 NGINX_SITE_CONF = DEPLOY_DIR / "nginx-baogongtou.vip.conf"
 ENV_EXAMPLE = DEPLOY_DIR / "env.example"
 DEPLOY_SCRIPT = DEPLOY_DIR / "deploy_remote.sh"
+INSTALL_CPA_WORKBUDDY_SCRIPT = DEPLOY_DIR / "install_cpa_workbuddy.sh"
 TUNNEL_SCRIPT = DEPLOY_DIR / "ssh_cpa_tunnel.ps1"
+WORKBUDDY_LOGIN_GUIDE = DEPLOY_DIR / "WORKBUDDY_LOGIN_GUIDE.md"
 
 ALL_DEPLOY_FILES = (
     CPA_CONFIG,
+    CPA_WORKBUDDY_CONFIG,
     CPA_SERVICE,
+    CPA_WORKBUDDY_SERVICE,
     BOT_SERVICE,
     CADDYFILE,
     NGINX_SITE_CONF,
     ENV_EXAMPLE,
     DEPLOY_SCRIPT,
+    INSTALL_CPA_WORKBUDDY_SCRIPT,
     TUNNEL_SCRIPT,
+    WORKBUDDY_LOGIN_GUIDE,
 )
```

- **理由（属于硬规则 3：因卡 3 在 `deploy/` 新增 4 个部署文件而必须同步的清单注册）**：`tests/test_deploy_configs.py:468-475`（P1-4 安全扫描）强制要求 `deploy/` 下每一个文件都必须列入 `ALL_DEPLOY_FILES`，确保任何新增部署脚本/模板都自动接受真实密钥和公网 IPv4 正则扫描。原有全部断言一字未动。
- 本批新增的 3 个独立测试文件均为纯新建：`tests/test_workbuddy_routing.py`、`tests/test_workbuddy_config_and_tracking.py`、`tests/test_workbuddy_deploy.py`。

---

## 五、跳过或没做完的卡及原因

- **跳过或没做完的卡**：**无**（本批全部 3 张卡均已按顺序完成、全量 pytest 通过并逐卡本地提交至 `wb1` 分支）。
- **未验证项**：真模型调用、实机运行 `deploy/install_cpa_workbuddy.sh`、实机 WorkBuddy OAuth 授权登录及实测 `/v1/models` 动态返回（均须等老公在 `kr` 装好 Go `1.27.1+` 并亲自执行部署与扫码登录后在第二段补做）。

---

## 六、说明书回执（读了哪些文件、用了哪几节、跑了哪些命令）

### 6.1 必读说明书与使用章节回执

| 必读说明书 / 源码 | 实际阅读范围 | 本次用到的章节与落实方式 |
|---|---|---|
| `docs/delivery/2026-11-19-WorkBuddy接入方案.md` | 全文（1-217 行） | - **§2（CPA 插件部署与配置）**：采用 CPA 原生插件路径 `plugins/linux/<arch>/cpa-workbuddy.so` 与 `plugins.configs.cpa-workbuddy` 结构；**特别注意**：§2.1 原文示例中的 `auto_checkin_enabled: true` 已严格按老公拍板覆盖为 `auto_checkin_enabled: false`，且 CPA 版本按卡 3 要求固定为 `v8.0.69`、插件改为源码编译 `2a1a608`<br>- **§3（4 模型白名单与黑名单）**：落地 `WorkBuddyModelCatalog` 与 `find_forbidden_model_reason()` default-deny 白名单 + 禁用自动路由与 `DeepSeek-V4-Pro`<br>- **§4（轮换、告警与每章记录）**：落地 §4.2 `RoleModelRouter` 轮换与 `on_model_switched`、§4.3 复用 P1-2 SSE 协议与 DB 持久化、§4.4 `chapter_XXXX.meta.json` + SQLite `chapters.model`/`production_stats.model` + 阅读页显示 |
| `docs/delivery/2026-11-19-batch1-交付报告.md` | 卡 1 P1-2 节（1-120 行） | 复用 P1-2 已建成的 `#model-quota-alert-region`、`MODEL_QUOTA_ALERT_KINDS`（`model_switched` / `quota_exhausted`）及 `SSE_CPA_QUOTA_ALERT` 协议，并补齐 `/api/model-switches` 持久化回显与「从 X 切到 Y」文案 |
| `src/xiaoshuo_bot/llm/base.py` | 全文（1-181 行） | 扩展 `LLMRequestError` 保留 `error_code`/`upstream_code`/`response_body` |
| `src/xiaoshuo_bot/llm/openai_compat.py` | 全文（1-421 行） | 解析 CPA/WorkBuddy JSON 错误体元数据并随 `LLMRequestError` 透传 |
| `src/xiaoshuo_bot/llm/router.py` | 全文（1-856 行） | 改造 `RoleEndpointConfig` 与 `RoleModelRouter` 实现白名单校验、按 `(role, model)` 隔离冷却、严格单向轮换与 `from_workbuddy_catalog()` |
| `src/xiaoshuo_bot/llm/cpa_manager.py` | 全文（1-236 行） | 核对 CPA `/v0/management/*` 客户端边界（本次保持只读无破坏改动） |
| 其余读过的本仓文件 | `src/xiaoshuo_bot/llm/__init__.py`、`cli.py`、`contracts.py`、`engine.py`、`critic_polisher.py`、`server/app.py`、`server/matrix_scheduler.py`、`server/stockpile_db.py`、`server/static/app.js`、`server/static/styles.css`、`deploy/cpa.config.example.yaml`、`deploy/cpa.service`、`deploy/env.example`、`deploy/deploy_remote.sh`、`tests/test_deploy_configs.py`、`projects/baiwu_dukou/outline.yaml` & `foreshadows.yaml` | 用于无缝集成每章模型落盘/落库/页面渲染、启动自检、`cloud_tests` 清单与部署安全测试 |
| 只读审计的外部插件与 CPA 源码 | `ygq-future/cpa-workbuddy @ 2a1a608fb0ff28c51996393822a7f36df4367d5b`（`internal/workbuddy/chat_errors.go`、`chat_faults_test.go`、`chat.go`、`chat_response.go`、`invalidation.go`、`billing.go`、`config.go`、`service.go`、`scheduler.go`、`internal/plugin/runtime.go`、`cmd/project/main.go`、`go.mod`、`README.md`）<br>`router-for-me/CLIProxyAPI @ v8.0.69`（`releases/tags/v8.0.69` & `checksums.txt`、`internal/pluginhost/adapters_executors.go`、`rpc_client.go`、`rpc_client_error_test.go`、`sdk/api/handlers/handlers_errors.go`、`sdk/api/handlers/openai/openai_handlers.go`） | 核实额度用尽判定码（HTTP 429 + `14018`/`credit_package_exhausted`）、CPA `v8.0.69` 官方 SHA256 摘要、`cpa-workbuddy` Go `1.27.1` 与 `-buildmode=c-shared` 编译方式以及默认 `AutoCheckinEnabled: true` 需显式覆写为 `false` |

### 6.2 跑过的关键命令清单

- **Worktree 创建与插件只读克隆**：
  - `git -C /Users/niuniu/Desktop/小说机器人 worktree add /Users/niuniu/Desktop/小说机器人-wb1 -b wb1 batch1`
  - `git clone https://github.com/ygq-future/cpa-workbuddy <scratch>/cpa-workbuddy && git checkout 2a1a608 && git rev-parse HEAD`（实测 `2a1a608fb0ff28c51996393822a7f36df4367d5b`）
- **CPA `v8.0.69` 官方发布信息与源码只读检索**：
  - `gh api repos/router-for-me/CLIProxyAPI/releases/tags/v8.0.69`
  - `curl -fsSL https://github.com/router-for-me/CLIProxyAPI/releases/download/v8.0.69/checksums.txt`
  - `gh api repos/router-for-me/CLIProxyAPI/contents/...?ref=v8.0.69`（只读查看 `adapters_executors.go`、`rpc_client.go`、`handlers_errors.go` 等）
  - `ssh kr 'uname -m && go version; which gcc cc git curl tar sha256sum openssl systemctl; cat /etc/os-release'`（仅只读查询工具链与架构，未改任何文件）
- **卡 1 测试与提交**：
  - `env -u XIAOSHUO_API_KEY PYTHONPATH=src /Users/niuniu/miniconda3/bin/python -m pytest -v tests/test_workbuddy_routing.py tests/test_llm_router_cpa.py tests/test_export_only_scheduler.py`
  - `env -u XIAOSHUO_API_KEY PYTHONPATH=src /Users/niuniu/miniconda3/bin/python -m pytest -q tests` → `253 passed, 103 subtests passed in 63.78s (0:01:03)`
  - `git add -A && git commit` → `78171e3`
- **卡 2 测试、本地预览截图与提交**：
  - `env -u XIAOSHUO_API_KEY PYTHONPATH=src /Users/niuniu/miniconda3/bin/python -m pytest -v tests/test_workbuddy_config_and_tracking.py tests/test_web_static_contract.py tests/test_deploy_configs.py`
  - `env -u XIAOSHUO_API_KEY PYTHONPATH=src /Users/niuniu/miniconda3/bin/python -m pytest -q tests` → `258 passed, 108 subtests passed in 65.21s`（CSS 精修后复跑：`258 passed, 108 subtests passed in 65.15s (0:01:05)`）
  - 本地预览服务 `scratch/run_wb1_preview.py` + 内置浏览器子代理 `WB1 Admin Model Alert Verifier` 截取 (a)/(b)/(c) 3 张实测图并关闭预览服务
  - `git add -A && git commit` → `8904f17`
- **卡 3 测试与提交（未执行部署脚本）**：
  - `env -u XIAOSHUO_API_KEY PYTHONPATH=src /Users/niuniu/miniconda3/bin/python -m pytest -v tests/test_workbuddy_deploy.py tests/test_deploy_configs.py`（内含 `bash -n deploy/install_cpa_workbuddy.sh` 静态语法检查）→ `19 passed in 0.16s`
  - `env -u XIAOSHUO_API_KEY PYTHONPATH=src /Users/niuniu/miniconda3/bin/python -m pytest -q tests` → `262 passed, 108 subtests passed in 65.48s (0:01:05)`
  - `git add -A && git commit` → `bc813d6`
- **交付前终审复核**：
  - `git log --reverse --name-status c04341d..HEAD`、`git diff c04341d..HEAD -- tests/`、全 worktree 只读状态检查、公网 IPv4 零泄漏扫描及最终全量 `pytest -q tests` 复跑。
