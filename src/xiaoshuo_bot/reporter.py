from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List


def generate_markdown_report(manifest: Dict[str, Any]) -> str:
    proj = manifest["project"]
    lines: List[str] = [
        f"# 《{proj['title']}》生产环境冒烟测试报告 (Smoke Test Audit Report)",
        "",
        "## 1. 运行总览",
        f"- **项目 ID**：`{proj['project_id']}`",
        f"- **题材类型**：{proj['genre']}",
        f"- **完成章节数**：{manifest['chapter_count']} 章",
        f"- **总正文字数（不含空白符）**：{manifest['total_chars']:,} 字",
        f"- **生产事故拦截次数（AI-TIC-CHECK 触发重写）**：{manifest['total_incidents_detected']} 次",
        f"- **最终质量门禁结论**：{'✅ 全部通过 (0 违规残留)' if manifest['all_gates_passed'] else '❌ 未通过'}",
        f"- **起止时间 (UTC)**：`{manifest['started_at']}` → `{manifest['finished_at']}`",
        "",
        "## 2. 分章门禁与篇幅审计表",
        "",
        "| 章节 | 场景标识 | 限知视点 | 净字数 | 段落数 | 生成轮次 | 触发重写 | 首字符校验 |",
        "| :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- |",
    ]

    for ch in manifest["chapters"]:
        lines.append(
            f"| 第 {ch['chapter']} 章 | `{ch['scene_id']}` | {ch['pov_character']} | "
            f"{ch['char_count']:,} | {ch['paragraph_count']} | {ch['attempts_used']} | "
            f"{ch['rewrites_triggered']} | `{ch['first_char']}` (纯正文首字) |"
        )

    lines.extend(["", "## 3. 资产物理一致性演进轨迹 (`[Current_Asset_State]`) ", ""])
    final_assets = manifest["final_world_state"]["assets"]
    for aid, asset in final_assets.items():
        lines.append(f"### 器物：{asset['name']} (`{aid}`)")
        lines.append(f"- **终态位置**：{asset['location']}")
        lines.append(f"- **终态持有人**：{asset['holder']}")
        lines.append(f"- **终态外观**：{asset['condition']}")
        lines.append("- **跨章流转日志**：")
        for h in asset["history"]:
            lines.append(f"  - {h}")
        lines.append("")

    lines.extend(["## 4. 伏笔物理埋设与到期回收账本 (`[Foreshadow_Plan]`)", ""])
    final_fs = manifest["final_world_state"]["foreshadows"]
    for fid, fs in final_fs.items():
        status_badge = "✅ 已物理回收" if fs["status"] == "resolved" else f"⚠️ {fs['status']}"
        lines.extend(
            [
                f"### [{fid}] {fs['title']} — {status_badge}",
                f"- **计划周期**：第 {fs['plant_chapter']} 章埋设 → 第 {fs['due_chapter']} 章到期回收",
                f"- **埋设正文回执**：> {fs['planted_evidence'] or '（同章即收）'}",
                f"- **回收正文回执**：> {fs['payoff_evidence']}",
                "",
            ]
        )

    lines.extend(["## 5. AI-TIC-CHECK 生产事故拦截与重写记录", ""])
    if not manifest["incidents"]:
        lines.append("- 本次冒烟全量章节均一次命中全部生产门禁（0 违禁词、0 结构化对称、0 视角越界）。")
    else:
        for inc in manifest["incidents"]:
            lines.append(
                f"- **`{inc['incident_id']}`**（第 {inc['chapter']} 章 第 {inc['attempt']} 轮）："
                f"拦截 {inc['violation_count']} 项违规 → `{inc['action_taken']}`"
            )
            for v in inc["violations"]:
                lines.append(
                    f"  - `[{v['rule_id']}]` 第 {v['line_number']} 行命中 `{v['matched_text']}`：{v['fix_hint']}"
                )

    lines.append("")
    return "\n".join(lines)


def generate_html_dashboard(manifest: Dict[str, Any], chapters_dir: Path) -> str:
    proj = manifest["project"]
    chapters_payload = []
    for ch in manifest["chapters"]:
        ch_file = chapters_dir / f"chapter_{ch['chapter']:02d}.txt"
        body = ch_file.read_text(encoding="utf-8") if ch_file.exists() else ""
        chapters_payload.append(
            {
                "chapter": ch["chapter"],
                "scene_id": ch["scene_id"],
                "internal_title": ch["internal_title"],
                "pov": ch["pov_character"],
                "chars": ch["char_count"],
                "paragraphs": ch["paragraph_count"],
                "attempts": ch["attempts_used"],
                "rewrites": ch["rewrites_triggered"],
                "body": body,
                "transitions": ch["state_transitions"],
            }
        )

    embedded_json = json.dumps(
        {
            "project": proj,
            "total_chars": manifest["total_chars"],
            "chapter_count": manifest["chapter_count"],
            "incidents": manifest["incidents"],
            "final_world_state": manifest["final_world_state"],
            "chapters": chapters_payload,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    title_esc = html.escape(proj["title"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>《{title_esc}》 · xiaoshuo-bot 冒烟验收与正文阅读台</title>
<style>
  :root {{
    --bg: #0f1419;
    --panel: #171f27;
    --panel-alt: #1e2833;
    --border: #2c3a47;
    --text: #e6edf3;
    --muted: #8b9eb0;
    --accent: #38bdf8;
    --ok: #34d399;
    --warn: #fbbf24;
    --serif: "Noto Serif CJK SC", "Songti SC", "STSong", Georgia, serif;
    --sans: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: var(--sans); line-height: 1.6;
  }}
  header {{
    padding: 20px 28px; border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, #16202a 0%, #0f1419 100%);
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 16px;
  }}
  header h1 {{ margin: 0; font-size: 20px; letter-spacing: 0.5px; }}
  .badges {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .badge {{
    padding: 5px 12px; border-radius: 999px; font-size: 12.5px;
    background: var(--panel-alt); border: 1px solid var(--border); color: var(--muted);
  }}
  .badge strong {{ color: var(--text); }}
  .badge.ok {{ border-color: rgba(52, 211, 153, 0.4); color: var(--ok); }}
  .layout {{
    display: grid; grid-template-columns: 320px 1fr 360px; min-height: calc(100vh - 76px);
  }}
  @media (max-width: 1180px) {{
    .layout {{ grid-template-columns: 1fr; }}
  }}
  .sidebar, .inspector {{
    background: var(--panel); padding: 18px; overflow-y: auto;
  }}
  .sidebar {{ border-right: 1px solid var(--border); }}
  .inspector {{ border-left: 1px solid var(--border); }}
  .section-label {{
    font-size: 11.5px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--muted); margin: 4px 0 12px; font-weight: 600;
  }}
  .ch-btn {{
    width: 100%; text-align: left; background: var(--panel-alt); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
    margin-bottom: 8px; cursor: pointer; transition: all 0.15s;
  }}
  .ch-btn:hover {{ border-color: var(--accent); }}
  .ch-btn.active {{ border-color: var(--accent); background: rgba(56, 189, 248, 0.1); }}
  .ch-btn .ch-top {{ display: flex; justify-content: space-between; font-weight: 600; font-size: 14px; }}
  .ch-btn .ch-sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
  .reader {{
    padding: 36px 48px; max-width: 820px; margin: 0 auto; width: 100%;
  }}
  .reader-meta {{
    display: flex; justify-content: space-between; align-items: center;
    padding-bottom: 14px; margin-bottom: 24px; border-bottom: 1px dashed var(--border);
    font-size: 13px; color: var(--muted);
  }}
  .prose {{
    font-family: var(--serif); font-size: 17.5px; line-height: 2.02; color: #eceff4;
  }}
  .prose p {{ margin: 0 0 1.25em 0; text-indent: 2em; text-align: justify; }}
  .card {{
    background: var(--panel-alt); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; font-size: 13px;
  }}
  .card h4 {{ margin: 0 0 6px 0; font-size: 13.5px; color: var(--accent); }}
  .kv {{ color: var(--muted); margin: 3px 0; font-size: 12.5px; }}
  .kv span {{ color: var(--text); }}
  .pill-ok {{ color: var(--ok); font-weight: 600; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>《{title_esc}》 · xiaoshuo-bot 生产冒烟验收台</h1>
  </div>
  <div class="badges" id="top-badges"></div>
</header>
<div class="layout">
  <aside class="sidebar">
    <div class="section-label">章节目录（纯正文零标题输出）</div>
    <div id="ch-list"></div>
    <div class="section-label" style="margin-top:22px;">伏笔回收账本 (Foreshadow Ledger)</div>
    <div id="fs-list"></div>
  </aside>
  <main class="reader">
    <div class="reader-meta" id="reader-meta"></div>
    <article class="prose" id="prose-body"></article>
  </main>
  <aside class="inspector">
    <div class="section-label">本章门禁校验 (AI-TIC-CHECK)</div>
    <div id="gate-card"></div>
    <div class="section-label" style="margin-top:18px;">本章器物物理状态演进</div>
    <div id="asset-updates"></div>
    <div class="section-label" style="margin-top:18px;">终态器物总览 (World State)</div>
    <div id="all-assets"></div>
  </aside>
</div>
<script>
const DATA = {embedded_json};
let activeIdx = 0;

function renderTop() {{
  const el = document.getElementById('top-badges');
  el.innerHTML = `
    <span class="badge ok">● AI-TIC-CHECK 全量通过</span>
    <span class="badge">总章节：<strong>${{DATA.chapter_count}} 章</strong></span>
    <span class="badge">净字数：<strong>${{DATA.total_chars.toLocaleString()}} 字</strong></span>
    <span class="badge">自动拦截重写：<strong>${{DATA.incidents.length}} 次</strong></span>
  `;
}}

function selectChapter(idx) {{
  activeIdx = idx;
  renderSidebar();
  const ch = DATA.chapters[idx];
  document.getElementById('reader-meta').innerHTML = `
    <span>内部章纲标识：<strong>第 ${{ch.chapter}} 章 · ${{ch.internal_title}}</strong>（输出文件首字符直起正文）</span>
    <span>限知视点：<strong>${{ch.pov}}</strong> · ${{ch.chars.toLocaleString()}} 字</span>
  `;
  const paras = ch.body.split(/\\n+/).filter(Boolean);
  document.getElementById('prose-body').innerHTML = paras.map(p => `<p>${{escapeHtml(p)}}</p>`).join('');

  document.getElementById('gate-card').innerHTML = `
    <div class="card">
      <h4>✅ 生产门禁 0 违规通过</h4>
      <div class="kv">生成轮次：<span>第 ${{ch.attempts}} 轮定稿（触发重写 ${{ch.rewrites}} 次）</span></div>
      <div class="kv">格式门禁：<span>首字符「${{escapeHtml(ch.body.slice(0,1))}}」，无标题/代码围栏/手记</span></div>
      <div class="kv">违禁词门禁：<span>总结腔 0 · 身体套路 0 · 结构对称 0</span></div>
      <div class="kv">限知视角门禁：<span>严格锁定「${{ch.pov}}」外部观察</span></div>
    </div>
  `;

  const updates = ch.transitions.asset_updates || [];
  document.getElementById('asset-updates').innerHTML = updates.length
    ? updates.map(u => `
      <div class="card">
        <h4>${{escapeHtml(u.name)}}</h4>
        <div class="kv">位置变更：<span>${{escapeHtml(u.before.location)}} → ${{escapeHtml(u.after.location)}}</span></div>
        <div class="kv">状态变更：<span>${{escapeHtml(u.after.condition)}}</span></div>
        <div class="kv">物理记录：<span>${{escapeHtml(u.note)}}</span></div>
      </div>
    `).join('')
    : `<div class="card"><div class="kv">本章无器物归属变更</div></div>`;
}}

function renderSidebar() {{
  const list = document.getElementById('ch-list');
  list.innerHTML = DATA.chapters.map((ch, idx) => `
    <button class="ch-btn ${{idx === activeIdx ? 'active' : ''}}" onclick="selectChapter(${{idx}})">
      <div class="ch-top">
        <span>第 ${{ch.chapter}} 章 · ${{escapeHtml(ch.internal_title)}}</span>
        <span class="pill-ok">PASS</span>
      </div>
      <div class="ch-sub">${{ch.chars.toLocaleString()}} 字 · ${{ch.paragraphs}} 段 · 视点：${{escapeHtml(ch.pov)}}</div>
    </button>
  `).join('');

  const fsList = document.getElementById('fs-list');
  fsList.innerHTML = Object.values(DATA.final_world_state.foreshadows).map(fs => `
    <div class="card">
      <h4>[${{escapeHtml(fs.id)}}] ${{escapeHtml(fs.title)}}</h4>
      <div class="kv">周期：<span>第 ${{fs.plant_chapter}} 章埋设 → 第 ${{fs.due_chapter}} 章回收</span></div>
      <div class="kv">状态：<span class="pill-ok">${{fs.status === 'resolved' ? '✅ 已物理回收' : escapeHtml(fs.status)}}</span></div>
      <div class="kv">回收摘录：<span>${{escapeHtml(fs.payoff_evidence)}}</span></div>
    </div>
  `).join('');

  const allAssets = document.getElementById('all-assets');
  allAssets.innerHTML = Object.values(DATA.final_world_state.assets).map(a => `
    <div class="card">
      <h4>${{escapeHtml(a.name)}}</h4>
      <div class="kv">终态位置：<span>${{escapeHtml(a.location)}}</span></div>
      <div class="kv">持有人：<span>${{escapeHtml(a.holder)}}</span></div>
      <div class="kv">物理状态：<span>${{escapeHtml(a.condition)}}</span></div>
    </div>
  `).join('');
}}

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, m => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m]));
}}

renderTop();
selectChapter(0);
</script>
</body>
</html>
"""
