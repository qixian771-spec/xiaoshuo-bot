# xiaoshuo-bot · 番茄爆款全自动矩阵引擎与云边协同套件

`xiaoshuo-bot` 是一套面向中文网文（番茄小说 / 七猫 / 知乎盐选 / 英文 Webnovel）的**资产驱动型长篇小说生产、质量门禁、云端矩阵调度、Telegram 远控与本地 Chrome 发布回采伴侣**全链路工程系统。

---

## 核心架构升级（M1 + M2 + M3）

### 1. M1：爆款核心生产与自进化引擎
- **领域契约与分卷加载 (`models.py`, `loader.py`)**：
  - 全仓统一零依赖北京时间常量 `SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")`，杜绝跨系统（Windows / 韩国 `UTC+9`）时区偏差。
  - 统一 `:04d` 章节与快照路径规范（`chapter_0001.txt` + `.meta.json`），兼容存量 `:02d` 项目只读恢复，并阻止 `:02d`/`:04d` 同章双写歧义。
  - 分卷大纲 `outline/vol_*.yaml` 加载，校验番茄 `<=15` 字书名、连续压抑 `<=2` 章与跨卷/终卷伏笔清账。
- **CPA 流式网关与五角色动态路由 (`llm/`)**：
  - `OpenAICompatibleProvider` 默认启用 `stream=True` SSE 长文流式拼接（容忍 `: keepalive` 心跳，`300s` 超时，强校验 `finish_reason == "stop"`）。
  - `RoleModelRouter` 支持 `planner` / `writer` / `critic` / `polisher` / `image` 五角色独立端点与同档多 Key 轮询抗 429 冷却（**零跨档降级**，全池耗尽立即抛 `QuotaExhaustedError`）。
  - `UsageBudgetLedger` 每日预算与章节熔断；`CPAManagementClient` 对接本机 `CLIProxyAPI` `/v0/management/*` 凭据热加载与 OAuth 管理。
- **十大爆款类目、反 AI 起名与架空合规 (`genres.py`, `namer.py`, `sanitizers.py`)**：
  - 内置男女频 10 大番茄爆款配方 + `literary_suspense` 兼容类目、黄金三章节拍与题材硬常识防穿帮卡。
  - `namer.py` 封杀 AI 高频名黑名单，并使用 `pypinyin` 防止全书角色同音混淆与生僻字。
  - `sanitizers.py` 拦截现代真地名/公检法番号、插符号变体绕审与违规推文别名。
- **百万字五维防吃书状态机与四级分层记忆 (`state.py`, `memory.py`)**：
  - 维护器物物理状态、货币/物资/战力数值账本与 `VolumeCapLock`、角色生死不可逆与过期称谓禁用图、期待感倒计时账本（强校验证据词）与虚拟日历伤势锁；支持含锁章 SHA256 校验的 `save_snapshot` / `load_snapshot`。
  - `MemoryPyramid`（L0 上章尾 ~260 字、L1 近 5 章、L2 每 10 章、L3 每 100 章 + 实体精准召回）控制注入上下文体积。
- **参数化 `AI-TIC-CHECK` 2.0 与手术刀精修 (`checker.py`, `compiler.py`, `critic_polisher.py`)**：
  - 支持 `third_limited` / `first_person` 人称与受控 `【系统】` 面板（`TTS_001` 限制 `<=65` 字），新增首屏 200 字冲突检测（`HOOK_001`）、章末泄气拦截（`HOOK_002`）、反圣母（`OOC_SAINT`）、状态交叉校验与节奏 Burstiness 诊断。
  - `TomatoCriticPolisher` 对草稿四维打分，仅对低分段落做局部手术重写，并强制二次复检 `AITicChecker` 与生成隔离的 `author_note`。
- **三源自动立项、防洗稿查重与留存自进化 (`plagiarism_guard.py`, `deconstructor.py`, `planner.py`, `evolution.py`)**：
  - 支持热榜风向 + 配方库 + 五维拆书换皮三源立项，强制连续汉字重合 `<=7` 字及 200 字滑窗 4-gram Jaccard `<5%` + 本机跨书查重。
  - `lv0_1`（15～20 万字紧凑完本）vs `lv2_plus`（2 万字责编 6 要素签约表 + 首秀测书 + 60～80 章换地图续纲）。
  - 确定性赛马决策器（晋级加更 `PROMOTE_DAILY_UPDATE` / 冷启动换名换封 `ROTATE_PACKAGING` / 止损切书 `SUGGEST_CUT`，未结算抛 `MetricsNotSettledError` 绝不按 0 误判），并锁死 `<= max_published_chapter` 已发章。
- **600×800 双阶封面、多平台导出与便携备份 (`cover.py`, `exporter.py`, `transcreator.py`, `backup.py`, `fonts/`)**：
  - 内置 SIL OFL 1.1 免费商用开源中文字体与 `OFL.txt`；支持 `ai_image`（Gemini 原生生图底图）与 `procedural` 离线排版，将书名与笔名精准压印于 `600×800` 安全框内。
  - 导出番茄（双框无 `第X章` 标题 + 隔离 `author_note` + 星耀卡 + 达人推文包）、七猫、知乎盐选及英文 Webnovel 转译包；`backup export/verify/import` 剥离密钥并附 SHA256 清单校验。

### 2. M2：云端矩阵中枢、Web 控制台、Telegram 远控与本地发布伴侣
- **云端调度与 HTTP/SSE 服务 (`server/`)**：
  - `auth.py`：非回环监听强制校验 `>=24` 字符 `XIAOSHUO_ADMIN_TOKEN`（否则抛 `InsecureBindError`），连续 5 次失败 IP 封禁 30 分钟；伴侣路由强制 HMAC-SHA256 + `±60s` 时间戳 + Nonce 防重放。
  - `stockpile_db.py`：SQLite WAL 幂等囤稿数据库、单书序列租约锁、`lv0_1` 8500 字 / `lv2_plus` 17000 字北京时间日配额安全锁、T-1 指标与进化联动。
  - `matrix_scheduler.py`：10 章安全垫水位巡检、`<6` 章置顶抢产与单书故障隔离舱（`ISOLATED_GATE_FAILURE`）。
  - `app.py` + `server/static/`：零外部构建依赖响应式暗色中文 Web 控制台、`/api/cpa/*` 透传代理与 `/api/events/stream` SSE 实时直播。
- **原生 HTTPS 长轮询 Telegram Bot (`telegram_bot.py`)**：
  - 零入站端口直连 `api.telegram.org`（`timeout=30, read_timeout=45`），`owner_user_id` 首行鉴权、1 秒内 `answerCallbackQuery`、纯 ASCII `<=64B` 短码、切书二级防误触 CAS 确认、HTML 长日报自动分页与验证码截图解冻恢复。
- **本地多马甲 Chrome 发布伴侣与 WOFF2 解码器 (`companion/`)**：
  - 纯出站长轮询 + SQLite WAL 本地 `outbox.db`；每个马甲独立根级 `user_data_dir` + `.companion.lock` 互斥锁。
  - **三层零触碰哨兵**：检测登录失效或任何 `.secsdk-captcha` 弹窗 **0 毫秒急停挂起**并截图推云端/TG（**严禁任何验证码点击、求解或指纹篡改**）。
  - **发稿三铁闸**：逐字核对 `author_name + book_id + book_title`、写前近 20 章远端对账幂等跳过、双框剥离 `第X章` 前缀填入 `.ProseMirror` 并核对字数误差 `<=5%` 存草稿定时发布。
  - **12:30 CST 回采与 WOFF2 解码**：北京时间 `12:30` 后回采指标（未结算抛 `MetricsNotSettledError`），`Woff2FontDecoder` 解析 `.woff2` 轮廓哈希还原 PUA 字符并强扫残余私用区码位。

### 3. M3：韩国云服务器零兜底部署套件 (`deploy/`)
- 提供 `cpa.config.example.yaml`（锁死 `127.0.0.1:8317`、`disable-image-generation: false`、`switch-preview-model: false`）、`cpa.service`、`xiaoshuo-bot.service`、`Caddyfile.example`、`env.example`、严格 `set -euo pipefail` 的 `deploy_remote.sh` 与 Windows SSH 隧道脚本 `ssh_cpa_tunnel.ps1`。

---

## 目录结构

```text
xiaoshuo-bot/
├── bin/xiaoshuo-bot                  # 命令行直接执行入口
├── deploy/                           # 韩国云服务器零兜底部署套件
├── docs/ARCHITECTURE.md              # 架构规范、M2 契约与门禁规则详述
├── projects/baiwu_dukou/             # 存量 5 章回归基准项目：《白雾渡口》
├── pyproject.toml                    # 包配置（含 [companion] 可选依赖与静态资源）
├── src/xiaoshuo_bot/
│   ├── contracts.py                  # M2 冻结 REST/SSE/HMAC/MatrixControlPort 契约
│   ├── cli.py                        # 全量 CLI 入口（M1 命令 + serve + companion）
│   ├── engine.py                     # 主生成引擎 2.0（:04d 原子落盘 / _archive/ 归档）
│   ├── models.py / loader.py         # SHANGHAI_TZ、WorldStateSnapshot、分卷加载器
│   ├── genres.py / namer.py / sanitizers.py
│   ├── state.py / memory.py / checker.py / compiler.py / critic_polisher.py
│   ├── plagiarism_guard.py / deconstructor.py / planner.py / evolution.py
│   ├── cover.py / exporter.py / transcreator.py / reporter.py / backup.py
│   ├── fonts/                        # SIL OFL 1.1 免费商用中文字体与 OFL.txt
│   ├── llm/                          # CPA 流式网关、RoleModelRouter、CPAManagementClient
│   ├── server/                       # auth / stockpile_db / matrix_scheduler / app / static/
│   ├── telegram_bot.py               # 原生 HTTPS 长轮询 Telegram Bot
│   └── companion/                    # client / browser_worker / font_decoder
└── tests/                            # 单元、静态契约、部署与云边 E2E 测试套件
```

---

## 快速开始

### 1. 安装（核心引擎 + 可选本地伴侣）
```bash
pip install -e .
# 如需在本机运行 Chrome 发布与 WOFF2 解码伴侣端：
pip install -e ".[companion]"
playwright install chromium
```

### 2. M1 核心命令
```bash
# 查看注册的 10+1 类目配方
xiaoshuo-bot genres
xiaoshuo-bot genres --id urban_brainhole

# 校验存量项目与故障注入重写冒烟
xiaoshuo-bot validate --project projects/baiwu_dukou
xiaoshuo-bot smoke-fault --project projects/baiwu_dukou

# 拆书、三源自动立项、滚动续纲与完结收官
xiaoshuo-bot auto-create --project-id demo_book --genre urban_brainhole --output-dir projects/demo_book --provider openai
xiaoshuo-bot extend --project projects/demo_book --provider openai

# 生成章节正文、600x800 封面（存量无笔名项目可加 --pen-name）与多平台导出、整站备份
xiaoshuo-bot run --project projects/demo_book --provider openai
xiaoshuo-bot cover --project projects/demo_book --output output/demo_cover.png --base-mode procedural
xiaoshuo-bot export --project projects/demo_book --platform tomato --output-dir output/tomato_bundle
xiaoshuo-bot backup export --output backups/site.zip --projects-dir projects
```

### 3. M2 云端中枢 (`serve`) 与本地发布伴侣 (`companion`)
```bash
# 启动云端矩阵服务 + Web 控制台（监听 0.0.0.0 时必须配置 >=24 位 XIAOSHUO_ADMIN_TOKEN）
export XIAOSHUO_ADMIN_TOKEN="replace-with-strong-token-at-least-24-chars"
export XIAOSHUO_COMPANION_HMAC_SECRET="replace-with-strong-hmac-secret"
export CPA_MANAGEMENT_URL="http://127.0.0.1:8317"
export CPA_MANAGEMENT_KEY="replace-with-cpa-management-key"
export TELEGRAM_BOT_TOKEN="123456:your-bot-token"
export TELEGRAM_OWNER_USER_ID="123456789"

xiaoshuo-bot serve \
  --host 127.0.0.1 \
  --port 8765 \
  --projects-root ./projects \
  --db-path ./data/matrix.db \
  --with-tg-bot

# 在本地日常电脑运行伴侣端（纯出站长轮询，默认拉起正式版 Chrome 非无头窗口）
xiaoshuo-bot companion \
  --server-url http://127.0.0.1:8765 \
  --hmac-secret "$XIAOSHUO_COMPANION_HMAC_SECRET" \
  --account-id acct_01 \
  --author-name "砚山行" \
  --profile-root ./chrome_profiles \
  --target-url "https://fanqienovel.com/main/writer/book-manage"
```

### 4. 运行测试（Windows PowerShell，先清空继承密钥环境变量）
```powershell
$env:XIAOSHUO_API_KEY = ''
if ($?) { $env:OPENAI_API_KEY = '' }
if ($?) { $env:ANTHROPIC_API_KEY = '' }
if ($?) { $env:PYTHONPATH = 'src' }
if ($?) { python -m unittest discover -s tests -p "test_*.py" -v }
```
