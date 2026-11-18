# `xiaoshuo-bot` 系统架构与契约规范文档

## 1. 总体拓扑与设计原则

`xiaoshuo-bot` 采用**零兜底、确定性门禁、云边物理隔离**的矩阵生产架构：

1. **时区统一锚定北京时间 (`SHANGHAI_TZ`)**：全仓使用 `models.py` 定义的 `timezone(timedelta(hours=8), name="Asia/Shanghai")`，不依赖 OS IANA 时区数据库；所有自然日配额、预算账本及番茄 `12:30` 结算窗口均以北京时间为准。
2. **严禁永久删除与静默兜底**：旧稿/废稿/快照覆盖统一移动至项目下 `_archive/`；关键配置（LLM 端点、CPA 管理密钥、Telegram Token/Owner、非回环 `XIAOSHUO_ADMIN_TOKEN`、伴侣 HMAC 密钥、未结算指标）缺失时立即具名报错。
3. **云边零触碰安全边界**：云端负责立项、生产、囤稿与赛马决策；本地伴侣通过纯出站 HTTPS + HMAC-SHA256 长轮询拉取任务，在独立 Chrome `user_data_dir` 中运行，遇任何登录过期或图形/滑块验证码 **0 毫秒急停挂起截图推 TG**，严禁自动点击或绕过逻辑。

```text
[Web Console (server/static/)] ──(Bearer Token + SSE)──┐
[Telegram Bot (telegram_bot.py)] ──(HTTPS getUpdates)──┼──► [MatrixHTTPServer + MatrixScheduler]
                                                        │         ├── AutoNovelPlanner / Evolution / Cover / Export
                                                        │         ├── NovelGenerationEngine 2.0 (:04d + _archive/)
                                                        │         │     └── RoleModelRouter -> CPA (127.0.0.1:8317)
                                                        │         └── StockpileDB (SQLite WAL + 序列锁 + 日配额锁)
                                                        │
[本地 Chrome 伴侣 (companion/)] ◄──(纯出站 HMAC 长轮询)──┘
   ├── CompanionHttpClient + OutboxStore (WAL 断网续传)
   ├── TomatoBrowserWorker (三铁闸 + 三层被动验证码哨兵)
   └── Woff2FontDecoder (TTFont 字形轮廓哈希 + PUA 残余强扫)
```

---

## 2. M1 章节生成与精修管线

```text
[分卷大纲 outline/vol_*.yaml + 角色/器物/伏笔/文风资产]
        │
        ▼
[WorldStateManager (五维账本/生死图/期待感/日历伤势)] + [MemoryPyramid (L0~L3)]
        │
        ▼
[PromptCompiler 2.0] ──(动态人称/受控系统面板/题材常识/架空词表)──► [RoleModelRouter (writer, SSE stream)]
                                                                            │
                                                                            ▼
                                                                     [AITicChecker 2.0]
                                                                    ╱                ╲
                                                          CRITICAL 违规            0 CRITICAL 通过
                                                                  ╱                    ╲
                                                                 ▼                      ▼
                                                    ProductionIncident          [TomatoCriticPolisher]
                                                    靶向 Rewrite 循环           四维打分 + 局部手术精修
                                                                                        │
                                                                                 二次复检 AITicChecker
                                                                                        │
                                                                                        ▼
                                                                      原子落盘 chapter_XXXX.txt + .meta.json
                                                                      推进状态机与快照 chapter_XXXX_after.json
```

### 门禁规则矩阵 (`checker.py`)

| 规则组 | 规则编码范围 | 检测目标 | 处置级别 |
| :--- | :--- | :--- | :--- |
| 输出格式门禁 | `FMT_000`–`FMT_007` | 首字符合法性、Markdown 标题、章节号、字符画分割线、代码围栏、创作手记 | `CRITICAL` |
| 总结腔与 AI 套话 | `SUM_001`–`SUM_007`, `SAFE_004` | “命运的齿轮”、“总而言之”、“三分薄凉”、“空气仿佛凝固”等高频套话 | `CRITICAL` |
| 身体反应套路 | `TIC_001`–`TIC_010` | “不禁一愣”、“心中一紧”、“眸光微闪”、“瞳孔微缩”等（已豁免台词与成语） | `CRITICAL` |
| 结构化对称 | `SYM_001`–`SYM_004` | “不是A，而是B”、“与其说是A，不如说是B”及三连排比（叙述段） | `CRITICAL` |
| 限知视角越界 | `POV_001`–`POV_005` | 按 `pov_mode` 动态校验人称，拦截全知预告与非视点人物心理越界 | `CRITICAL` |
| 番茄首屏与断章 | `HOOK_001`–`HOOK_002` | 第 1 章前 3 段禁天气/简历开局；章末禁“一夜无话/沉沉睡去”泄气收尾 | `CRITICAL` |
| 合规架空与红线 | `SAFE_001`–`SAFE_003`, `OOC_SAINT` | 现代真地名/公检法番号、插符号变体绕审、高危红线及无代价原谅死敌 | `CRITICAL` |
| 状态交叉与听书 | `STATE_*`, `TTS_001` | 死人诈尸、禁用过期称谓、余额透支、期待感逾期、`【】` 面板 `<=65` 字禁 `|/` | `CRITICAL` |
| 节奏量化诊断 | `PACE_*` | 超长砖头段/连续旁白/连续裸对话报 `CRITICAL`；`Burstiness` 过低输出 `WARNING` 供精修 | `CRITICAL` / `WARNING` |

---

## 3. M2 云边通信与安全契约 (`contracts.py`)

### 3.1 REST API 与 SSE 路由
- **健康检查与类目**：`GET /api/health`（公开零密钥，含 `admin_auth_required` 布尔状态）、`GET /api/genres`
- **书目与囤稿调度**：`GET /api/books`、`POST /api/books/auto-create`、`GET/POST /api/books/{book_id}/status`、`GET /api/books/{book_id}/chapters`、`GET /api/books/{book_id}/chapters/{chapter}`、`POST /api/books/{book_id}/chapters/{chapter}/lock`、`GET/POST /api/books/{book_id}/export`、`GET/POST /api/books/{book_id}/cover`、`POST/DELETE /api/books/{book_id}/cut`、`POST /api/books/{book_id}/promote`、`POST /api/books/{book_id}/extend`、`POST /api/books/{book_id}/resume`
- **CPA 网关代理**：`GET/POST /api/cpa/auth-files`、`DELETE /api/cpa/auth-files/{name}`、`GET /api/cpa/models`、`GET /api/cpa/usage`、`GET/POST /api/cpa/oauth`、`GET/PUT/POST /api/cpa/bindings`
- **伴侣云边长轮询**：`POST /api/companion/poll`（最长 `25s` 挂起唤醒）、`POST /api/companion/lease`、`POST /api/companion/receipt`（支持章节与 `chapter <= 0` 非章节任务）、`POST /api/companion/metrics`、`POST /api/companion/captcha`、`POST /api/companion/resume`
- **实时直播流**：`GET /api/events/stream`（`text/event-stream`，推送 15 种冻结 SSE 事件）

### 3.2 鉴权与状态机
- **管理端鉴权 (`server/auth.py`)**：非 `127.0.0.1`/`localhost`/`::1` 绑定必须提供 `>=24` 字符 `XIAOSHUO_ADMIN_TOKEN`；使用 `hmac.compare_digest` 校验 `Authorization: Bearer <token>`，同 IP 连续 5 次失败封禁 30 分钟（HTTP 429）。
- **伴侣端鉴权 (`contracts.py`)**：`X-Xiaoshuo-Timestamp`（`±60s`）、`X-Xiaoshuo-Nonce`（防重放）、`X-Xiaoshuo-Client-Id`、`X-Xiaoshuo-Signature`（对 `METHOD\nPATH\nTS\nNONCE\nCLIENT_ID\nSHA256(BODY)` 计算 HMAC-SHA256）。
- **囤稿状态枚举**：`STOCKPILED -> LEASED -> DRAFT_SAVED -> SCHEDULED -> PUBLISHED`，异常态 `PAUSED_AUTH_CAPTCHA` / `FAILED_HARD_MISMATCH`；单书第 `N` 章仅在第 `N-1` 章处于 `SCHEDULED` 或 `PUBLISHED` 时允许认领，且受北京时间单日安全字数上限（`lv0_1`: `8500` 字，`lv2_plus`: `17000` 字）约束。
- **Telegram 控制端口 (`MatrixControlPort`)**：通过 `cli.py serve --with-tg-bot` 同进程后台线程驱动 `TelegramControlBot`，`MatrixHTTPServer` 启动/关闭时自动托管其生命周期。
