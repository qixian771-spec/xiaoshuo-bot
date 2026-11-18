# xiaoshuo-bot · 资产驱动型中文长篇小说生产引擎

`xiaoshuo-bot` 是一套面向严肃长篇/中篇中文叙事生产的工程化生成管线。系统将**角色行为锚点 (`Character_Identity_Anchor`)**、**器物物理状态机 (`Current_Asset_State`)**、**跨章伏笔账本 (`Foreshadow_Plan`)**、**人类作家颗粒度样本 (`Style_Samples`)** 与 **确定性反 AI 套话静态扫描器 (`AI-TIC-CHECK`)** 闭环整合，命中任何叙事禁令即判定为生产事故并自动触发靶向重写。

---

## 核心特性

1. **生产级五段式动态提示词编译 (`compiler.py`)**
   - 自动按当前章纲 (`SceneOutline`) 裁剪注入在场角色的性格底色、语言习惯、微动作锚点与 OOC 禁区。
   - 实时从世界状态机 (`WorldStateManager`) 注入受控器物在**当前章节时刻**的最新物理位置、持有人、磨损细节与跨章流转记录。
   - 自动区分并注入**本章到期必收伏笔**与**本章待埋设物理伏笔**，同时附带上一章结尾切片确保时空无缝衔接。

2. **六维确定性 `AI-TIC-CHECK` 质量门禁 (`checker.py`)**
   - **格式污染门禁 (`FORMAT_VIOLATION`)**：强制校验首字符即为正文首字；拦截标题、小标题、章节号、分割线、Markdown 代码围栏与创作手记。
   - **总结腔/升华词门禁 (`SUMMARY_CLICHE`)**：拦截“命运的齿轮”、“总而言之”、“由此可见”、“某种意义上”、“仿佛在诉说着”、“一切才刚刚开始”等议论升华套话。
   - **身体反应套路门禁 (`BODY_TIC_CLICHE`)**：拦截“不禁一愣”、“心中一紧”、“眸光微闪”、“瞳孔微缩”、“嘴角勾起一抹”、“倒吸一口凉气”、“苦笑一声”及惯性副词“不禁”。
   - **结构化对称门禁 (`STRUCTURAL_SYMMETRY`)**：正则拦截“不是A，而是B”、“并非A，而是B”、“与其说是A，不如说是B”及“是……，是……，是……”三连排比。
   - **第三人称限知视角门禁 (`POV_VIOLATION`)**：对话掩码剥离后，精准拦截叙述段第一人称“我”泄露、全知上帝视角词（“殊不知”、“没有人知道”、“与此同时在另一边”）及非视点角色内心独白越界（如视点为林晚时出现“周叙心想”）。
   - **伏笔回收与篇幅门禁 (`FORESHADOW_UNRECOVERED` / `LENGTH_VIOLATION`)**：强制核验到期伏笔的物理证据词是否真正落入正文，并校验章纲字数上下限。

3. **生产事故自动拦截与靶向重写闭环 (`engine.py`)**
   - 任意章节草稿命中上述任一规则，立即生成结构化事故单 (`audit/incidents.jsonl`)，将精确行号、命中词与整改提示编译为重写指令自动打回模型重生成，直至 `0 违规` 通过。

4. **跨章器物状态与伏笔账本演进 (`state.py`)**
   - 每章定稿后自动应用器物位置/归属/损耗变更，提取伏笔埋设与回收的正文原句作为账本回执，并落盘章节快照 (`snapshots/chapter_XX_after.json`)。

---

## 目录结构

```text
xiaoshuo-bot/
├── bin/xiaoshuo-bot                 # 命令行直接执行入口
├── docs/ARCHITECTURE.md             # 架构规范与门禁规则详述
├── pyproject.toml                   # Python 包定义
├── src/xiaoshuo_bot/
│   ├── models.py                    # 领域数据模型
│   ├── loader.py                    # 项目 YAML 加载与引用完整性校验
│   ├── compiler.py                  # 动态提示词与事故重写提示词编译器
│   ├── checker.py                   # AI-TIC-CHECK 静态扫描引擎
│   ├── state.py                     # 跨章器物物理状态机与伏笔账本
│   ├── engine.py                    # 章节生成、事故拦截、重写与归档主引擎
│   ├── reporter.py                  # Markdown 冒烟审计报告与 HTML 交互阅读台生成
│   ├── cli.py                       # CLI 子命令实现
│   └── llm/                         # OpenAI兼容 / Anthropic / 本地IPC Bridge / 故障注入后端
├── projects/
│   └── baiwu_dukou/                 # 实测五章完整悬疑中篇项目：《白雾渡口》
└── tests/                           # 单元测试与故障注入自动重写集成测试
```

---

## 快速开始

### 1. 运行单元测试与故障注入重写验证
```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
./bin/xiaoshuo-bot validate --project projects/baiwu_dukou
./bin/xiaoshuo-bot smoke-fault --project projects/baiwu_dukou
```

### 2. 使用 OpenAI 兼容接口（DeepSeek / OpenAI / OpenRouter / vLLM）生成整部小说
```bash
export XIAOSHUO_API_KEY="sk-..."
export XIAOSHUO_API_BASE="https://api.deepseek.com/v1"
export XIAOSHUO_MODEL="deepseek-chat"

./bin/xiaoshuo-bot run --project projects/baiwu_dukou --provider openai
```

### 3. 对任意外部文稿执行独立 `AI-TIC-CHECK` 扫描
```bash
./bin/xiaoshuo-bot check-text \
  --file projects/baiwu_dukou/output/chapters/chapter_01.txt \
  --project projects/baiwu_dukou \
  --chapter 1
```
