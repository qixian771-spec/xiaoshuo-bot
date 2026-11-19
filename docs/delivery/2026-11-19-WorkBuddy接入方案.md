# 2026-11-19 WorkBuddy 经 CPA 接入调研与设计方案

> 调研性质：**全程只读、未改任何业务代码、未部署、未改动服务器任何文件、未读取任何密钥值**。
> 调研时间：2026-11-19
> 适用仓库：`xiaoshuo-bot`（基线 `batch1`）

---

## 一、核心调研结论

### 1.1 CLIProxyAPI（CPA）能不能接 WorkBuddy 登录？

**结论：能，但不是 CPA 主程序内置原生登录，而是通过 CPA v8 原生动态库插件机制（`plugins`）接入官方插件商店收录的 WorkBuddy Provider 插件。**

实测核实证据如下：

1. **CPA 主程序本身无内置 `-workbuddy-login`**：
   - 服务器 `kr` 归档的旧 CPA 二进制（`/root/cpa-retired-20261119-105650/cli-proxy-api/cli-proxy-api --help`）实测仅支持 `-antigravity-login`、`-claude-login`、`-codex-login`、`-codex-device-login`、`-kimi-login`、`-xai-login`、`-vertex-import`。
   - 上游仓库 `router-for-me/CLIProxyAPI`（最新 Release `v8.0.78`，发布于 `2026-11-19T00:28:17Z`）代码检索确认无内置 WorkBuddy CLI 登录标志；其代码库中出现 `workbuddy` 仅在 README 生态说明以及插件宿主单元测试（`internal/api/handlers/management/model_definitions_test.go:78-115`、`plugin_quota_test.go`）中作为插件 Provider 示例。
2. **CPA v8 原生插件系统与官方插件商店已收录 2 个 WorkBuddy 插件**：
   - 上游 `router-for-me/CLIProxyAPI` 的 `config.example.yaml:1380-1392` 与 `internal/pluginstore/registry.go:17` 证实：CPA 内置官方插件商店注册表 `https://raw.githubusercontent.com/router-for-me/CLIProxyAPI-Plugins-Store/main/registry.json`（实测收录 **186** 个插件），通过 Go `c-shared` 动态库（Linux 为 `.so`）加载第三方 Provider。
   - 在官方插件商店 `registry.json` 中直接查到以下 **2 个 WorkBuddy（腾讯 CodeBuddy）Provider 插件**：

| 插件 ID | 仓库 | 最新版本 / 更新时间 | 定位与核心能力 | 适配 CPA 版本 |
|---|---|---|---|---|
| `cpa-workbuddy`（**首选**） | `ygq-future/cpa-workbuddy` | `v0.1.2`（`2026-11-18`，MIT） | 明确支持 **WorkBuddy 国内站（`wb/` 前缀）与国际站（`wb-ai/` 前缀）双站点**浏览器登录、动态模型发现、OpenAI Chat / Responses / Claude / Gemini 四协议、`QuotaProvider` 积分查询、每日自动签到/活跃点亮、内嵌面板 `/v0/resource/plugins/cpa-workbuddy/panel`；Release 已提供 `linux_amd64` / `linux_arm64` 预编译 ZIP + `checksums.txt` | 明确要求官方 CPA `v8.0.69+`（原生插件 + JSON schema 10）+ 官方 CPAMC |
| `workbuddy`（备选） | `zidanefaqih/codebuddy-intl-cpa` | `v0.10.0`（38 stars，MIT） | 主面向国际版账号（兼支持国内站），含插件内 429/`code 6004` 账号故障转移、单请求积分日志、每日国际站 30 积分奖励；插件 ID 为 `workbuddy`，凭据文件 `workbuddy-<uid>.json` | 源码 `models.go` 导入 `CLIProxyAPI/v7/sdk/pluginapi`，与 CPA v8.0.78 ABI 兼容性**未查到**官方实测结论，需实测验证 |

3. **社区其他关联实现（仅作对照参考）**：
   - 其他 CPA 插件：`SadPull/cpa-plugin`（`2026-10-04`）、`Lxapk/workbuddy-cpa-plugin`（`2026-10-04`，注：标明 CPA v7 插件）、`lovingfish/workbuddy-cliproxy`（`2026-07-08`，注明基于 CPA v7.2.x，模型快照较旧仅到 Hy3/DS-V4）。
   - 独立反代网关（不走 CPA 插件 ABI）：`misakano7545/workbuddy2api-panel`（1,329 stars，`2026-11-19` 活跃维护，提供 `127.0.0.1:7863` OpenAI 兼容接口 + Web 管理面板 + `models.hidden`/`models.allowed` 模型控制）。
   - **路线建议**：沿用老板要求的「服务器部署 CPA 接 WorkBuddy」同一套做法，**首选 CPA `v8.0.78+` + 官方插件商店 `cpa-workbuddy`**；若后续实测插件在 `kr` 出现登录或 ABI 问题，再评估备选独立网关 `workbuddy2api-panel`（仍可由 `xiaoshuo-bot` 直接连其 `127.0.0.1:7863/v1` 或挂到 CPA 后面）。

4. **上游合规与稳定性风险（来自已查社区仓库实测记录）**：
   - `misakano7545/workbuddy2api-panel` README 明确提示：直接上游 `linguo2625469/workbuddy2api-panel` 已于 `2026-10-21` 停更，原因之一为上游收紧反代，通道存在随时变更或风控风险，严禁多小号滥用。
   - `lovingfish/workbuddy-cliproxy` README 记录：CodeBuddy 上游对非流式请求可能返回 `code 11101` 拒绝（插件内部转流式再聚合），而 `xiaoshuo-bot` 的 `OpenAICompatibleProvider` 默认就是 `stream=True`（SSE 流式），天然契合。

---

## 二、服务器 CPA + WorkBuddy 配置方案

### 2.1 CPA 侧安装与 `config.yaml` 关键配置（部署时在 `kr` 实施）

1. **升级/安装 CPA `v8.0.78+`**：
   - 保持现有安全红线不变：仅监听 `127.0.0.1:8317`、`remote-management.allow-remote: false`、普通用户 `xiaoshuo` 运行、禁止暴露 8317 到公网。
   - 禁止 CPA 侧静默跨模型降级（保留铁律配置）：
     ```yaml
     host: "127.0.0.1"
     port: 8317
     quota-exceeded:
       switch-project: true
       switch-preview-model: false
       antigravity-credits: false
     remote-management:
       allow-remote: false
       secret-key: "__REPLACE_WITH_CPA_MANAGEMENT_KEY__"
     api-keys:
       - "__REPLACE_WITH_CPA_CLIENT_API_KEY__"
     ```
2. **启用插件并安装 `cpa-workbuddy`**：
   - 方式 A（官方插件商店 / 自定义源）：在 `config.yaml` 启用插件，重启 CPA 后通过本机 SSH 隧道打开 CPAMC → 插件商店安装 **CPA WorkBuddy**，再重启 CPA：
     ```yaml
     plugins:
       enabled: true
       dir: "plugins"
       # 官方商店内置源已收录 cpa-workbuddy；如需直连插件仓库最新 Release 可选追加：
       # store-sources:
       #   - "https://raw.githubusercontent.com/ygq-future/cpa-workbuddy/main/registry.json"
       configs:
         cpa-workbuddy:
           enabled: true
           cn_model_prefix: true       # 国内站模型以 wb/<id> 暴露
           intl_model_prefix: true     # 国际站模型以 wb-ai/<id> 暴露
           auto_checkin_enabled: true
           auto_checkin_time: "09:00"
     ```
   - 方式 B（手动安装）：从 `ygq-future/cpa-workbuddy` Release 下载 `cpa-workbuddy_0.1.2_linux_amd64.zip` 与 `checksums.txt`，校验 SHA256 后解压至 `/opt/cli-proxy-api/plugins/linux/amd64/cpa-workbuddy.so`，重启 `cpa.service` 并确认日志加载 `cpa-workbuddy`。
   - 注：旧归档 `/root/cpa-retired-20261119-105650/cli-proxy-api/config.example.yaml:89-109` 已包含 `plugins:` 配置节，但其 `SOURCE_COMMIT`（`4e25b1d474`）在 GitHub 公开上游查不到对应 commit（返回 422，疑似本地自编译或旧分支），**建议重装 CPA 时直接使用官方 Release `v8.0.78+`**（确保支持插件 JSON schema 10）。

### 2.2 登录流程与管理面板核验

1. 本机建 SSH 隧道转发 `8317` 端口（不暴露公网）：`ssh -N -L 8317:127.0.0.1:8317 kr`。
2. 浏览器访问 CPAMC 管理面板 → OAuth 登录 → 选择 **WorkBuddy国内** 或 **WorkBuddy国际** → 打开登录链接在浏览器完成扫码/身份授权（会话有效期 5 分钟）。
3. 授权成功后，在 `http://127.0.0.1:8317/v0/resource/plugins/cpa-workbuddy/panel` 确认账号状态与积分，并调用：
   ```bash
   curl -s http://127.0.0.1:8317/v1/models -H "Authorization: Bearer $CPA_API_KEY"
   ```
   **记录账号真实返回的全部模型 ID**，用于下文白名单映射。
4. **`xiaoshuo-bot` 现有后台 OAuth 面板缺口说明**：
   - `src/xiaoshuo_bot/llm/cpa_manager.py:14-24` 的 `_OAUTH_ENDPOINT_ALIASES` 和 `src/xiaoshuo_bot/server/static/index.html:304-310` 的 `#cpa-oauth-provider` 仅列了 `claude`/`codex`/`gemini`/`antigravity`/`qwen`/`iflow`，尚未接入插件 OAuth 登录入口。
   - **未查到**：`cpa-workbuddy` 插件向 CPA Management API 暴露的具体 OAuth 发起端点路径（是否走 `/v0/management/plugins/cpa-workbuddy/...` 或通用插件 OAuth 接口），需在部署 `cpa-workbuddy` 后实测 CPAMC 网络请求或直接通过 CPAMC 完成登录。

---

## 三、模型白名单、黑名单与上游精确 ID 核查

### 3.1 老板规定的模型约束（强约束，不可放宽）

| 类别 | 模型清单 | 约束说明 |
|---|---|---|
| **允许白名单（仅限 4 个）** | 1. `Hy4 flash`<br>2. `Hy4`<br>3. `DeepSeek-V4.1-Flash`<br>4. `Xiaomi MiMo-V2.6-Pro` | 仅允许调用这 4 个模型；采用 **默认拒绝（default-deny）白名单校验**，任何未列入白名单的 ID 在配置加载、API 绑定保存、运行时路由三个环节均直接报错拒绝 |
| **写作自动轮换顺序（3 档）** | `Hy4 flash` → `DeepSeek-V4.1-Flash` → `Xiaomi MiMo-V2.6-Pro` | 仅在当前模型额度耗尽时按此顺序切换；全部 3 档用尽后才停机告警；`Hy4` 在白名单内，可用于显式指定角色（如高难立项/复审），不默认插入低耗轮换链（除非后续显式配置） |
| **明令禁止（硬黑名单 + 非白名单双重拦截）** | 1. 自动路由模式：`快速` / `均衡` / `极致`（及 `auto`、`auto-*`）<br>2. `DeepSeek-V4-Pro`（`deepseek-v4-pro` 及所有前缀变体）<br>3. 其余一切未入白名单的模型 | 严禁使用自动路由档位与 `DeepSeek-V4-Pro`；即使上游 `/v1/models` 返回了这些 ID，前端模型下拉与后端校验都必须过滤/拒绝 |

### 3.2 上游精确 Model ID 核实表（查不到的均标注「未查到」，绝不猜测）

| 业务名称 | 是否查到 WorkBuddy 精确 API Model ID | 已核实的线索 / 原始出处 | 接入前必做动作 |
|---|---|---|---|
| `Hy4 flash` | **部分查到（仅国际站示例）** | `ygq-future/cpa-workbuddy/README.md` 示例中出现国际站调用 ID `wb-ai/hy4flash-free`；但国内站（`wb/`）及非 free 档位的确切 ID **未查到** | 登录后调用 `GET /v1/models` 实测确认精确 ID 并写入白名单映射 |
| `Hy4` | **未查到最终正式 ID** | 仅在 `zidanefaqih/codebuddy-intl-cpa/workbuddy/models.go:57` 查到 `hy4-preview`，在 `misakano7545/workbuddy2api-panel/internal/pool/modelrules.go` 注释查到 `cn:hy4std`；`cpa-workbuddy` 动态接口当前正式 ID **未查到** | 同上，以实测 `GET /v1/models` 返回为准 |
| `DeepSeek-V4.1-Flash` | **已查到裸 ID** | `zidanefaqih/codebuddy-intl-cpa/workbuddy/models.go:39,61` 查到 `deepseek-v4.1-flash`（配合 `cpa-workbuddy` 前缀预计为 `wb/deepseek-v4.1-flash` 或 `wb-ai/deepseek-v4.1-flash`） | 登录后经 `GET /v1/models` 核实站点前缀与大小写 |
| `Xiaomi MiMo-V2.6-Pro` | **未查到** | 已检索 `ygq-future/cpa-workbuddy`、`zidanefaqih/codebuddy-intl-cpa`、`SadPull/cpa-plugin`、`misakano7545/workbuddy2api-panel` 全部源码及 GitHub 代码搜索，均未查到 WorkBuddy 上游硬编码 ID（仅在 OpenRouter/HuggingFace 查到公开名 `xiaomi/mimo-v2.6-pro` / `XiaomiMiMo/MiMo-V2.6-Pro-RL`，不可直接假定等同于 WorkBuddy 内部 ID） | 登录后经 `GET /v1/models` 实测确认，再写入配置 |
| `DeepSeek-V4-Pro`（禁止） | **已查到裸 ID** | `zidanefaqih/codebuddy-intl-cpa/workbuddy/models.go:31,72`、`SadPull/cpa-plugin/workbuddy/models.go:35`、`lovingfish/workbuddy-cliproxy/README.md` 均确认裸 ID 为 `deepseek-v4-pro` | 纳入黑名单正则 + 白名单 default-deny 双拦截 |
| 自动路由 `快速`/`均衡`/`极致`（禁止） | **仅查到 `auto`，三档具体子 ID 未查到** | `SadPull/cpa-plugin/workbuddy/models.go:23` 与 `zidanefaqih/codebuddy-intl-cpa/workbuddy/models.go:56` 查到 `auto`；`快速`/`均衡`/`极致` 的具体 API 子 ID 在上述 4 个仓库与 Web 检索中均**未查到** | 依赖 **白名单 default-deny** 彻底拦截一切非白名单路由 ID，再配合 `GET /v1/models` 实测补全黑名单过滤展示 |

> **关键设计原则**：因为 `cpa-workbuddy` 和 `SadPull/cpa-plugin` 均通过上游 `GET /console/enterprises/personal/models` 动态发现模型列表，静态猜测 ID 极易 400/404。因此 `xiaoshuo-bot` 必须将「4 个白名单模型的逻辑名 → 实测 `/v1/models` 精确 ID 映射表」作为显式配置项（带启动校验，禁止静默回退到未配置模型）。

---

## 四、自动轮换、网页告警与每章模型记录设计方案

### 4.1 职责划分（不让 CPA 偷偷降级）

- **CPA + `cpa-workbuddy` 插件层**：只负责 WorkBuddy 浏览器 OAuth 登录、Token 刷新、同模型多账号轮换与协议转发；**严禁在 CPA 层配置跨模型静默回退**（保持 `switch-preview-model: false`、`antigravity-credits: false`）。
- **`xiaoshuo-bot` 路由与调度层**：集中掌控白名单校验、有序轮换（`Hy4 flash` → `DeepSeek-V4.1-Flash` → `Xiaomi MiMo-V2.6-Pro`）、网页切换提示、每章模型记录与全耗尽熔断停机。

### 4.2 后端路由改造设计（`src/xiaoshuo_bot/llm/router.py`）

1. **白名单与轮换链配置（`RoleEndpointConfig`）**：
   - 新增字段：
     - `model_whitelist: Tuple[str, ...]`：严格限制仅允许已映射的 4 个白名单模型 ID（逻辑名 `hy4_flash` / `hy4` / `deepseek_v4_1_flash` / `mimo_v2_6_pro` 对应的精确上游 ID）；
     - `rotation_models: Tuple[str, ...]`：有序轮换链，写作默认 `[Hy4 flash, DeepSeek-V4.1-Flash, Xiaomi MiMo-V2.6-Pro]`；
     - 校验函数 `validate_allowed_model(model_id, whitelist)`：
       - 先硬拒绝黑名单模式（如包含 `deepseek-v4-pro` 或裸名等于 `auto` / 含 `快速`/`均衡`/`极致` 路由标识）；
       - 再检查 `model_id in whitelist`（default-deny）；不通过直接抛 `LLMConfigurationError`，绝不静默替换。
   - **API 绑定校验同步拦截**（`src/xiaoshuo_bot/server/matrix_scheduler.py` `update_role_bindings` / `POST /api/cpa/bindings`）：保存角色绑定时同样执行白名单校验，防止在后台误选自动路由或 `DeepSeek-V4-Pro`。

2. **额度耗尽判定与显式切换（`RoleModelRouter.generate()`，对应 `src/xiaoshuo_bot/llm/router.py:537-598`）**：
   - 保持现有「预算上限 `BudgetCircuitBreakerError` 立即停机」不变（超预算绝不换模型，直接熔断）。
   - 对单模型内所有 API key 依次尝试；当某模型全部凭据返回 **额度耗尽/限流**（HTTP `429`，或上游明确的积分/配额耗尽错误如 CodeBuddy `code 6004` / `insufficient credit` / `quota exceeded`）：
     1. 将该 `(role, model_id)` 标记为冷却（独立于 key 冷却，按配置冷却时长或重置时间解除）；
     2. 查询 `rotation_models` 中下一个未冷却的白名单模型 `next_model`；
     3. **若存在 `next_model`**：
        - 更新该角色当前生效模型指针为 `next_model`；
        - 立即调用回调 `on_model_switched(role=role, from_model=cur_model, to_model=next_model, reason=err_reason, chapter=chapter_no)`；
        - 使用 `next_model` 继续完成本次请求（返回 `LLMResponse(model=next_model, ...)`，账本 `ledger.record_usage` 按 `next_model` 精确记账）。
     4. **若 `rotation_models` 全部模型均处于额度耗尽/冷却状态**：
        - 抛出 `QuotaExhaustedError("白名单轮换链全部模型（Hy4 flash / DeepSeek-V4.1-Flash / Xiaomi MiMo-V2.6-Pro）均已耗尽额度，停止自动写作")`；
        - 由 `MatrixScheduler.produce_next_chapter()` 捕获，将书籍状态置为 `PAUSED_QUOTA_EXHAUSTED` 并广播 `SSE_CPA_QUOTA_ALERT`（`kind="quota_exhausted"`）。

### 4.3 网页端明显提示（直接复用卡 1 / P1-2 已实现的告警组件）

卡 1（P1-2）已在 `src/xiaoshuo_bot/server/static/index.html`、`styles.css`、`app.js` 建成可复用的 `#model-quota-alert-region` 告警组件与 `MODEL_QUOTA_ALERT_KINDS` 协议，后续接入 WorkBuddy 轮换时**无需重写前端告警组件**，直接按以下契约发事件即可：

| 场景 | 后端触发点 | SSE 事件与 `data` 载荷 | 后台 `/admin/` 视觉呈现（已在卡 1 落地） |
|---|---|---|---|
| **单次模型已切换** | `RoleModelRouter` 触发 `on_model_switched` → `MatrixScheduler._emit` | `event: cpa_quota_alert`<br>`data: {"kind": "model_switched", "from_model": "<旧模型>", "to_model": "<新模型>", "reason": "<具体原因如 HTTP 429 / 额度用尽>", "role": "writer"}` | 顶部 `#model-quota-alert-region` 弹出**琥珀色横幅** `🔄 写作模型已切换`，带 `<旧模型> → <新模型>` 标签、书目与切换原因、跳转「前往 CPA 配置」与「知道了」按钮（`role="status" aria-live="polite"`） |
| **全部白名单额度用尽 / 超预算** | `RoleModelRouter` 抛 `QuotaExhaustedError` / `BudgetCircuitBreakerError` → `MatrixScheduler` 置 `PAUSED_QUOTA_EXHAUSTED` | `event: cpa_quota_alert`<br>`data: {"kind": "quota_exhausted", "error_type": "QuotaExhaustedError", "reason": "<全部耗尽原因>"}` | 顶部 `#model-quota-alert-region` 弹出**红色告警横幅** `🚨 超预算 / 全部额度用尽 · 熔断停机`（`role="alert" aria-live="assertive"`）+ 书目卡片红色高亮 `超预算 / 额度熔断`；且刷新页面后会依据数据库 `PAUSED_QUOTA_EXHAUSTED` 自动持久化重现 |
| **刷新页面后回看历史切换** | （待实现卡补齐）`StockpileDB` 增加 `model_switch_events` 表或 `chapters.model` 字段 | `GET /api/books` / `GET /api/books/{id}/chapters` 返回最近切换记录与每章模型 | 打开阅读页或刷新后台仍可追溯哪一章因何切换了模型 |

### 4.4 每章记录所用模型（文件 + SQLite + 页面三处对齐）

1. **章节元数据文件（已有基础，小幅补全）**：
   - `src/xiaoshuo_bot/engine.py:1010-1017` 写盘的 `output/chapters/chapter_XXXX.meta.json` 已经记录 `"model": llm_resp.model`（正文实际使用的 `writer` 模型）。
   - 接入卡中补充字段：`writer_model`、`critic_model`、`polisher_model`、`model_switches`（本章生成过程中如发生切换，记录 `[{from_model, to_model, reason, timestamp}]`）。
2. **SQLite 持久化与老库迁移（`src/xiaoshuo_bot/server/stockpile_db.py`）**：
   - `chapters` 表新增列 `model TEXT NOT NULL DEFAULT ''`（通过 `_init_schema()` `PRAGMA table_info(chapters)` 自动 `ALTER TABLE` 兼容旧库）；`add_stockpiled_chapter()` 接收 `model` 参数落库。
   - `production_stats` 表新增列 `model TEXT NOT NULL DEFAULT ''`（同样自动 `ALTER TABLE` 迁移），便于后续按模型统计写稿耗时与重写率。
   - `ChapterStatusItem`（`src/xiaoshuo_bot/contracts.py`）增加可选 `model: str = ""`，透传给 `GET /api/books/{id}/chapters` 与 `GET /api/books/{id}/chapters/{ch}`；后台阅读页 `#reader-chapter-meta` 直接展示「模型：`xxx`」。

---

## 五、后续落地拆卡建议与前置实测清单

接入 WorkBuddy 建议拆为 **2 步**（第 1 步需老板本人扫码登录，第 2 步才改代码）：

1. **前置运维实测（`kr` 服务器，老板操作）**：
   - 安装官方 CPA `v8.0.78+` 并配置仅监听 `127.0.0.1:8317`、`switch-preview-model: false`、`antigravity-credits: false`；
   - 启用 `plugins.enabled: true` 并安装 `cpa-workbuddy`（`v0.1.2+`）；
   - 通过 SSH 隧道登录 CPAMC 完成 WorkBuddy 账号授权；
   - 实测调用 `GET http://127.0.0.1:8317/v1/models`，拿到账号真实可用的 4 个白名单模型精确 ID（以及自动路由和 `DeepSeek-V4-Pro` 的精确 ID）发给开发卡锁定。
2. **代码实现卡（W1：WorkBuddy 白名单轮换与每章模型记录）**：
   - `RoleModelRouter` 实现白名单硬校验 + 3 模型有序轮换（`Hy4 flash → DeepSeek-V4.1-Flash → Xiaomi MiMo-V2.6-Pro`）+ `on_model_switched` 回调；
   - `MatrixScheduler` 透传 `model_switched` SSE 事件对接卡 1 告警组件；
   - `StockpileDB` + `engine.py` + 后台阅读页补齐每章 `model` 列与展示；
   - 离线单测覆盖：白名单拒绝 `DeepSeek-V4-Pro` 与自动路由、模拟 429 按精确顺序 1→2→3 切换并触发网页提示、3 档全耗尽停机转 `PAUSED_QUOTA_EXHAUSTED`、每章 `meta.json` 与 SQLite `chapters.model` 准确记录实际模型；
   - 最后补做 T5 的真模型试跑验收（`lv0_1` 与 `lv2_plus`）。

---

## 六、本次调研核查来源清单（不含猜测）

1. **GitHub 上游源码与发布信息（只读 `gh api`）**：
   - `router-for-me/CLIProxyAPI`：仓库元数据、最新 Release `v8.0.78`、`README.md`、`README_CN.md`、`config.example.yaml`（`plugins:` 配置节）、`internal/pluginstore/registry.go`、`internal/api/handlers/management/model_definitions_test.go`、`internal/api/handlers/management/plugin_quota_test.go`
   - `router-for-me/CLIProxyAPI-Plugins-Store`：`registry.json`（完整解析全部 186 个官方收录插件）
   - `ygq-future/cpa-workbuddy`：仓库元数据、最新 Release `v0.1.2`（含 6 平台资产列表）、`README.md`、文件树
   - `zidanefaqih/codebuddy-intl-cpa`：仓库元数据、`README.md`、`workbuddy/models.go`
   - `SadPull/cpa-plugin`：`README.md`、`workbuddy/models.go`、`workbuddy/oauth.go`
   - `Lxapk/workbuddy-cpa-plugin`：`README.md`
   - `lovingfish/workbuddy-cliproxy`：`README.md`
   - `misakano7545/workbuddy2api-panel`：`README.md`、`config.example.json`、`internal/pool/modelrules.go`、`internal/pool/modelview.go`、`internal/panel/model_hidden.go`、`internal/panel/models_test.go`
2. **服务器 `kr` 只读核查（未读任何凭据/密钥值，未改任何文件）**：
   - `ls -la /root/cpa-retired-20261119-105650/` 及 `cli-proxy-api/`
   - `/root/cpa-retired-20261119-105650/cli-proxy-api/cli-proxy-api --help`
   - `/root/cpa-retired-20261119-105650/cli-proxy-api/SOURCE_COMMIT` 与 `config.example.yaml` 中 `plugins:` 节
3. **本仓库代码核查（只读）**：
   - `src/xiaoshuo_bot/llm/router.py:221-605`
   - `src/xiaoshuo_bot/llm/cpa_manager.py:1-100`
   - `src/xiaoshuo_bot/engine.py:179,1010-1085`
   - `src/xiaoshuo_bot/server/matrix_scheduler.py:515-775`
   - `src/xiaoshuo_bot/server/stockpile_db.py:370-465`
   - `src/xiaoshuo_bot/server/static/index.html:301-345`、`app.js`
4. **Web 检索（`search_web`）**：
   - `CLIProxyAPI WorkBuddy login provider OAuth supported providers`
   - `"CLIProxyAPI" "WorkBuddy" OR "workbuddy" github`
   - `"WorkBuddy" CLIProxyAPI OR "router-for-me/CLIProxyAPI" WorkBuddy Hy4 DeepSeek-V4.1-Flash MiMo-V2.6-Pro`
   - `"WorkBuddy" "Hy4" OR "DeepSeek-V4.1-Flash" OR "MiMo-V2.6-Pro" 模型 ID`
