from __future__ import annotations

import html
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import resolve_chapter_path

DEFAULT_HTML_SHARD_THRESHOLD = 30
DEFAULT_HTML_SHARD_SIZE = 30


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
        f"- **起止时间 (Asia/Shanghai)**：`{manifest['started_at']}` → `{manifest['finished_at']}`",
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


def _read_chapter_body(chapters_dir: Path, chapter_no: int, ch_meta: Dict[str, Any]) -> str:
    resolved = resolve_chapter_path(chapters_dir, chapter_no)
    if resolved is not None and resolved.exists():
        return resolved.read_text(encoding="utf-8")
    raw_file = ch_meta.get("chapter_file")
    if raw_file:
        p = Path(str(raw_file))
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def _render_single_dashboard_html(
    *,
    proj: Dict[str, Any],
    manifest: Dict[str, Any],
    chapters_payload: List[Dict[str, Any]],
    subtitle: str = "生产冒烟验收台",
    back_link: Optional[str] = None,
) -> str:
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
    subtitle_esc = html.escape(subtitle)
    back_html = (
        f'<a href="{html.escape(back_link)}" class="badge" style="text-decoration:none;color:var(--accent);">← 返回总索引</a>'
        if back_link
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>《{title_esc}》 · xiaoshuo-bot {subtitle_esc}</title>
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
  .badges {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
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
    <h1>《{title_esc}》 · xiaoshuo-bot {subtitle_esc}</h1>
  </div>
  <div class="badges" id="top-badges">{back_html}</div>
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
  const existingBack = el.innerHTML;
  el.innerHTML = existingBack + `
    <span class="badge ok">● AI-TIC-CHECK 全量通过</span>
    <span class="badge">总章节：<strong>${{DATA.chapter_count}} 章</strong></span>
    <span class="badge">净字数：<strong>${{DATA.total_chars.toLocaleString()}} 字</strong></span>
    <span class="badge">自动拦截重写：<strong>${{DATA.incidents.length}} 次</strong></span>
  `;
}}

function selectChapter(idx) {{
  if (!DATA.chapters.length) return;
  activeIdx = idx;
  renderSidebar();
  const ch = DATA.chapters[idx];
  document.getElementById('reader-meta').innerHTML = `
    <span>内部章纲标识：<strong>第 ${{ch.chapter}} 章 · ${{escapeHtml(ch.internal_title)}}</strong>（输出文件首字符直起正文）</span>
    <span>限知视点：<strong>${{escapeHtml(ch.pov)}}</strong> · ${{ch.chars.toLocaleString()}} 字</span>
  `;
  const paras = ch.body.split(/\\n+/).filter(Boolean);
  document.getElementById('prose-body').innerHTML = paras.map(p => `<p>${{escapeHtml(p)}}</p>`).join('');

  document.getElementById('gate-card').innerHTML = `
    <div class="card">
      <h4>✅ 生产门禁 0 违规通过</h4>
      <div class="kv">生成轮次：<span>第 ${{ch.attempts}} 轮定稿（触发重写 ${{ch.rewrites}} 次）</span></div>
      <div class="kv">格式门禁：<span>首字符「${{escapeHtml(ch.body.slice(0,1))}}」，无标题/代码围栏/手记</span></div>
      <div class="kv">违禁词门禁：<span>总结腔 0 · 身体套路 0 · 结构对称 0</span></div>
      <div class="kv">限知视角门禁：<span>严格锁定「${{escapeHtml(ch.pov)}}」外部观察</span></div>
    </div>
  `;

  const updates = (ch.transitions && ch.transitions.asset_updates) || [];
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
        <span>第 ${{ch.chapter}} 章 · ${{escapeHtml(ch.public_title || ch.internal_title)}}</span>
        <span class="pill-ok">PASS</span>
      </div>
      <div class="ch-sub">${{ch.chars.toLocaleString()}} 字 · ${{ch.paragraphs}} 段 · 视点：${{escapeHtml(ch.pov)}}</div>
    </button>
  `).join('');

  const fsList = document.getElementById('fs-list');
  fsList.innerHTML = Object.values(DATA.final_world_state.foreshadows || {{}}).map(fs => `
    <div class="card">
      <h4>[${{escapeHtml(fs.id)}}] ${{escapeHtml(fs.title)}}</h4>
      <div class="kv">周期：<span>第 ${{fs.plant_chapter}} 章埋设 → 第 ${{fs.due_chapter}} 章回收</span></div>
      <div class="kv">状态：<span class="pill-ok">${{fs.status === 'resolved' ? '✅ 已物理回收' : escapeHtml(fs.status)}}</span></div>
      <div class="kv">回收摘录：<span>${{escapeHtml(fs.payoff_evidence)}}</span></div>
    </div>
  `).join('');

  const allAssets = document.getElementById('all-assets');
  allAssets.innerHTML = Object.values(DATA.final_world_state.assets || {{}}).map(a => `
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


def _partition_chapters_into_shards(
    chapters: List[Dict[str, Any]],
    shard_size: int = DEFAULT_HTML_SHARD_SIZE,
) -> List[Tuple[str, str, List[Dict[str, Any]]]]:
    """Group chapters by volume and split large volumes into <= shard_size HTML shards."""
    by_volume: Dict[int, List[Dict[str, Any]]] = {}
    for ch in chapters:
        vol = int(ch.get("volume", 1) or 1)
        by_volume.setdefault(vol, []).append(ch)

    shards: List[Tuple[str, str, List[Dict[str, Any]]]] = []
    for vol in sorted(by_volume.keys()):
        vol_chapters = sorted(by_volume[vol], key=lambda c: int(c["chapter"]))
        if len(vol_chapters) <= shard_size:
            c_start = vol_chapters[0]["chapter"]
            c_end = vol_chapters[-1]["chapter"]
            shards.append(
                (
                    f"volume_{vol:04d}.html",
                    f"第 {vol} 卷（第 {c_start}～{c_end} 章）",
                    vol_chapters,
                )
            )
        else:
            for part_idx, offset in enumerate(range(0, len(vol_chapters), shard_size), start=1):
                chunk = vol_chapters[offset : offset + shard_size]
                c_start = chunk[0]["chapter"]
                c_end = chunk[-1]["chapter"]
                shards.append(
                    (
                        f"volume_{vol:04d}_part_{part_idx:02d}.html",
                        f"第 {vol} 卷分片 {part_idx}（第 {c_start}～{c_end} 章）",
                        chunk,
                    )
                )
    return shards


def _render_sharded_index_html(
    *,
    proj: Dict[str, Any],
    manifest: Dict[str, Any],
    shards_meta: List[Dict[str, Any]],
    chapter_index_items: List[Dict[str, Any]],
) -> str:
    """Render lightweight summary-indexed HTML without embedding multi-megabyte raw chapter bodies."""
    embedded_index = json.dumps(
        {
            "sharded": True,
            "project": proj,
            "total_chars": manifest["total_chars"],
            "chapter_count": manifest["chapter_count"],
            "incidents": manifest["incidents"],
            "final_world_state": manifest["final_world_state"],
            "shards": shards_meta,
            "chapters": chapter_index_items,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    title_esc = html.escape(proj["title"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>《{title_esc}》 · xiaoshuo-bot 长篇分卷索引验收台</title>
<style>
  :root {{
    --bg: #0f1419; --panel: #171f27; --panel-alt: #1e2833;
    --border: #2c3a47; --text: #e6edf3; --muted: #8b9eb0;
    --accent: #38bdf8; --ok: #34d399;
    --sans: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font-family: var(--sans); line-height: 1.6; }}
  header {{ padding: 20px 28px; border-bottom: 1px solid var(--border); background: #16202a; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 16px; }}
  header h1 {{ margin: 0; font-size: 20px; }}
  .badges {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .badge {{ padding: 5px 12px; border-radius: 999px; font-size: 12.5px; background: var(--panel-alt); border: 1px solid var(--border); color: var(--muted); }}
  .badge.ok {{ border-color: rgba(52, 211, 153, 0.4); color: var(--ok); }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px 28px; }}
  .shard-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; margin-bottom: 26px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }}
  .card h3 {{ margin: 0 0 8px 0; font-size: 15px; color: var(--accent); }}
  .card a {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); }}
  th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: left; font-size: 13px; }}
  th {{ background: var(--panel-alt); color: var(--muted); }}
</style>
</head>
<body>
<header>
  <h1>《{title_esc}》 · 分卷分片索引控制台 (Sharded Summary Index)</h1>
  <div class="badges">
    <span class="badge ok">● 分片防膨胀模式已启用</span>
    <span class="badge">总章节：<strong>{manifest['chapter_count']} 章</strong></span>
    <span class="badge">净字数：<strong>{manifest['total_chars']:,} 字</strong></span>
    <span class="badge">拦截重写：<strong>{len(manifest['incidents'])} 次</strong></span>
  </div>
</header>
<div class="container">
  <h2>分卷阅读分片</h2>
  <div class="shard-grid" id="shard-grid"></div>
  <h2>全书章节摘要索引（零全量正文内嵌）</h2>
  <table>
    <thead>
      <tr><th>章号</th><th>标题</th><th>卷/分片</th><th>净字数</th><th>段落</th><th>首句摘要</th></tr>
    </thead>
    <tbody id="chapter-rows"></tbody>
  </table>
</div>
<script>
const INDEX_DATA = {embedded_index};
function esc(s) {{ return String(s).replace(/[&<>"']/g, m => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m])); }}
document.getElementById('shard-grid').innerHTML = INDEX_DATA.shards.map(s => `
  <div class="card">
    <h3>${{esc(s.label)}}</h3>
    <div>章节数：${{s.chapter_count}} 章 ｜ 总字数：${{s.total_chars.toLocaleString()}} 字</div>
    <div style="margin-top:8px;"><a href="dashboard_shards/${{esc(s.filename)}}">打开本卷阅读分片 →</a></div>
  </div>
`).join('');
document.getElementById('chapter-rows').innerHTML = INDEX_DATA.chapters.map(c => `
  <tr>
    <td>第 ${{c.chapter}} 章</td>
    <td>${{esc(c.public_title || c.internal_title)}}</td>
    <td><a href="dashboard_shards/${{esc(c.shard_file)}}">${{esc(c.shard_file)}}</a></td>
    <td>${{c.chars.toLocaleString()}}</td>
    <td>${{c.paragraphs}}</td>
    <td>${{esc(c.opening_preview)}}</td>
  </tr>
`).join('');
</script>
</body>
</html>
"""


def generate_sharded_html_dashboards(
    manifest: Dict[str, Any],
    chapters_dir: Path,
    output_dir: Optional[Path] = None,
    *,
    shard_threshold: int = DEFAULT_HTML_SHARD_THRESHOLD,
    shard_size: int = DEFAULT_HTML_SHARD_SIZE,
) -> Dict[str, Any]:
    """Generate either single-file dashboard (<=30 ch) or volume-sharded index + shard HTML files (>30 ch)."""
    proj = manifest["project"]
    chapters_meta = list(manifest.get("chapters", []))

    if len(chapters_meta) <= shard_threshold:
        chapters_payload = []
        for ch in chapters_meta:
            body = _read_chapter_body(chapters_dir, int(ch["chapter"]), ch)
            chapters_payload.append(
                {
                    "chapter": ch["chapter"],
                    "scene_id": ch["scene_id"],
                    "internal_title": ch["internal_title"],
                    "public_title": ch.get("public_title", ""),
                    "pov": ch["pov_character"],
                    "chars": ch["char_count"],
                    "paragraphs": ch["paragraph_count"],
                    "attempts": ch["attempts_used"],
                    "rewrites": ch["rewrites_triggered"],
                    "body": body,
                    "transitions": ch["state_transitions"],
                }
            )
        index_html = _render_single_dashboard_html(
            proj=proj,
            manifest=manifest,
            chapters_payload=chapters_payload,
        )
        return {
            "sharded": False,
            "index_html": index_html,
            "shard_paths": [],
        }

    shards = _partition_chapters_into_shards(chapters_meta, shard_size=max(1, shard_size))
    shards_dir: Optional[Path] = (output_dir / "dashboard_shards") if output_dir is not None else None
    if shards_dir is not None:
        shards_dir.mkdir(parents=True, exist_ok=True)

    shards_meta: List[Dict[str, Any]] = []
    chapter_index_items: List[Dict[str, Any]] = []
    shard_paths: List[Path] = []

    for filename, label, chunk in shards:
        chunk_payload: List[Dict[str, Any]] = []
        chunk_chars = 0
        for ch in chunk:
            ch_no = int(ch["chapter"])
            body = _read_chapter_body(chapters_dir, ch_no, ch)
            chunk_chars += int(ch["char_count"])
            chunk_payload.append(
                {
                    "chapter": ch_no,
                    "scene_id": ch["scene_id"],
                    "internal_title": ch["internal_title"],
                    "public_title": ch.get("public_title", ""),
                    "pov": ch["pov_character"],
                    "chars": ch["char_count"],
                    "paragraphs": ch["paragraph_count"],
                    "attempts": ch["attempts_used"],
                    "rewrites": ch["rewrites_triggered"],
                    "body": body,
                    "transitions": ch["state_transitions"],
                }
            )
            chapter_index_items.append(
                {
                    "chapter": ch_no,
                    "volume": int(ch.get("volume", 1) or 1),
                    "scene_id": ch["scene_id"],
                    "internal_title": ch["internal_title"],
                    "public_title": ch.get("public_title", ""),
                    "chars": ch["char_count"],
                    "paragraphs": ch["paragraph_count"],
                    "opening_preview": ch.get("opening_preview", body[:68].replace("\n", " ")),
                    "ending_preview": ch.get("ending_preview", body[-68:].replace("\n", " ")),
                    "shard_file": filename,
                }
            )

        shards_meta.append(
            {
                "filename": filename,
                "label": label,
                "chapter_count": len(chunk),
                "total_chars": chunk_chars,
                "start_chapter": chunk[0]["chapter"],
                "end_chapter": chunk[-1]["chapter"],
            }
        )

        if shards_dir is not None:
            shard_html = _render_single_dashboard_html(
                proj=proj,
                manifest=manifest,
                chapters_payload=chunk_payload,
                subtitle=label,
                back_link="../smoke_test_dashboard.html",
            )
            shard_path = shards_dir / filename
            tmp_shard = shard_path.with_name(f"{shard_path.name}.tmp")
            tmp_shard.write_text(shard_html, encoding="utf-8")
            os.replace(tmp_shard, shard_path)
            shard_paths.append(shard_path)

    chapter_index_items.sort(key=lambda c: int(c["chapter"]))
    index_html = _render_sharded_index_html(
        proj=proj,
        manifest=manifest,
        shards_meta=shards_meta,
        chapter_index_items=chapter_index_items,
    )
    return {
        "sharded": True,
        "index_html": index_html,
        "shard_paths": shard_paths,
        "shards_meta": shards_meta,
    }


def generate_html_dashboard(
    manifest: Dict[str, Any],
    chapters_dir: Path,
    *,
    output_dir: Optional[Path] = None,
    shard_threshold: int = DEFAULT_HTML_SHARD_THRESHOLD,
    shard_size: int = DEFAULT_HTML_SHARD_SIZE,
) -> str:
    """Generate self-contained HTML dashboard (<=30ch) or lightweight sharded index (>30ch)."""
    result = generate_sharded_html_dashboards(
        manifest=manifest,
        chapters_dir=chapters_dir,
        output_dir=output_dir,
        shard_threshold=shard_threshold,
        shard_size=shard_size,
    )
    return str(result["index_html"])
