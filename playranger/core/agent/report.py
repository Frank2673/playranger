"""真实反馈数据可视化报告 —— 单文件离线 HTML，零依赖、零外链。

给谁用：
- 独游开发者：跑完一轮自动试玩，直接拿到一份能贴给合伙人 / 发行 / 玩家的报告
- 测试同学：按严重度、异常类型、修复阶段快速排期
- 参赛评审：一眼看到「不是截图堆砌，而是有溯源、有证据、有修复路径」

组成：
1. KPI 面板：缺陷总数、自动确认率、阻断级数量、平均置信度、证据链通过率、复现率
2. 三张分布图：严重度、结论、异常类型（纯 CSS 条形图，不依赖图表库）
3. 执行时间线：每一步的成功 / 修正 / 失败，真实反映试玩轨迹
4. Bug 卡片：触发链、状态差异、证据来源、截图、修复深化方案
5. 聚类摘要：把同类缺陷收敛成修复任务，方便批量派单

设计约束：
- 不引入任何 CDN / JS 依赖，双击即可看，断网也能看
- 所有用户数据一律 HTML 转义，避免缺陷标题里的 `<` 破坏页面
"""

from __future__ import annotations

import html as html_lib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from playranger.core.agent.intel import analyze_bug
from playranger.core.agent.advisor import cluster_bugs
from playranger.core.agent.triage import SEVERITY_ORDER, severity_score

_SEV_LABEL = {
    "info": "提示",
    "minor": "轻微",
    "major": "严重",
    "critical": "紧急",
    "blocker": "阻断",
}

_CONC_LABEL = {
    "auto_confirmed": "自动确认",
    "auto_warning": "自动预警",
    "human_required": "需人工确认",
}

_CONC_CLASS = {
    "auto_confirmed": "ok",
    "auto_warning": "warn",
    "human_required": "unknown",
}


class _ReportContext:
    """统一管理报告路径换算与转义，避免每个渲染函数重复判断。"""

    def __init__(self, report_dir: str | Path | None = None, metrics: dict | None = None):
        self.report_dir = Path(report_dir).resolve() if report_dir else None
        self.metrics = metrics or {}

    def rel_dir(self, bug_dir: str | None) -> str:
        """把缺陷包目录换算成相对报告目录的路径（正斜杠）。"""
        if not bug_dir or not self.report_dir:
            return ""
        try:
            rel = os.path.relpath(Path(bug_dir).resolve(), self.report_dir)
        except (OSError, ValueError):
            return ""
        return rel.replace(os.sep, "/")

    def shot_src(self, bug_dir: str | None, file_rel: str) -> str:
        base = self.rel_dir(bug_dir)
        file_rel = (file_rel or "").replace(os.sep, "/")
        return f"{base}/{file_rel}" if base else file_rel


# ─── 指标 ───────────────────────────────────────────────────────


def build_metrics(bugs: list[dict], history: list[dict] | None = None) -> dict:
    """汇总报告所需的全部统计指标。"""
    bugs = list(bugs or [])
    history = list(history or [])

    by_severity = {s: 0 for s in SEVERITY_ORDER}
    by_conclusion = {c: 0 for c in _CONC_LABEL}
    by_anomaly: dict[str, int] = {}

    confidences: list[float] = []
    rates: list[float] = []
    evidence_ok = 0
    evidence_failed = 0
    auto = 0

    for bug in bugs:
        sev = str(bug.get("severity", "info"))
        if sev in by_severity:
            by_severity[sev] += 1
        else:
            by_severity["info"] += 1

        conc = str(bug.get("conclusion", "human_required"))
        if conc not in by_conclusion:
            conc = "human_required"
        by_conclusion[conc] += 1
        if conc == "auto_confirmed":
            auto += 1

        anomaly = str(bug.get("anomaly_type", "unknown"))
        by_anomaly[anomaly] = by_anomaly.get(anomaly, 0) + 1

        triage = bug.get("triage") or {}
        if triage.get("confidence") is not None:
            try:
                confidences.append(float(triage["confidence"]))
            except (TypeError, ValueError):
                pass

        if bug.get("reproduction_rate") is not None:
            try:
                rates.append(float(bug["reproduction_rate"]))
            except (TypeError, ValueError):
                pass

        chain = bug.get("evidence_chain") or {}
        if "ok" in chain:
            if chain["ok"]:
                evidence_ok += 1
            else:
                evidence_failed += 1

    highest = "info"
    for sev in by_severity:
        if by_severity[sev] > 0 and _sev_index(sev) > _sev_index(highest):
            highest = sev

    # 步轨迹统计
    steps_total = len(history)
    steps_failed = 0
    steps_corrected = 0
    for step in history:
        status = _step_status(step)
        if status == "fail":
            steps_failed += 1
        elif status == "warn":
            steps_corrected += 1

    # 人工复核反馈统计（有 feedback.json 的缺陷才计入）
    review_total = 0
    review_agree = 0
    false_positive = 0
    for bug in bugs:
        label = str((bug.get("feedback") or {}).get("label", ""))
        if label not in ("true_positive", "false_positive"):
            continue
        review_total += 1
        conc = str(bug.get("conclusion", "human_required"))
        if label == "false_positive":
            false_positive += 1
        if label == "true_positive" and conc != "human_required":
            review_agree += 1
        elif label == "false_positive" and conc == "human_required":
            review_agree += 1

    total = len(bugs)
    return {
        "total_bugs": total,
        "by_severity": by_severity,
        "by_conclusion": by_conclusion,
        "by_anomaly": by_anomaly,
        "highest_severity": highest,
        "severity_score": severity_score(highest),
        "auto_confirmed": auto,
        "auto_rate": round(auto / total, 3) if total else 0.0,
        "avg_confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
        "avg_reproduction": round(sum(rates) / len(rates), 3) if rates else 0.0,
        "evidence_ok": evidence_ok,
        "evidence_failed": evidence_failed,
        "evidence_rate": (
            round(evidence_ok / (evidence_ok + evidence_failed), 3)
            if (evidence_ok + evidence_failed)
            else 0.0
        ),
        "steps_total": steps_total,
        "steps_failed": steps_failed,
        "steps_corrected": steps_corrected,
        "step_success_rate": (
            round((steps_total - steps_failed) / steps_total, 3) if steps_total else 0.0
        ),
        "review_total": review_total,
        "review_agree": review_agree,
        "review_rate": round(review_agree / review_total, 3) if review_total else 0.0,
        "false_positive": false_positive,
    }


# ─── 渲染入口 ───────────────────────────────────────────────────


def render_html(
    bugs: list[dict],
    *,
    history: list[dict] | None = None,
    title: str = "PlayRanger 自动试玩报告",
    report_dir: str | Path | None = None,
    generated_at: str | None = None,
    game_name: str = "",
    trend: dict | None = None,
) -> str:
    """渲染完整报告 HTML。"""
    metrics = build_metrics(bugs, history)
    ctx = _ReportContext(report_dir, metrics)
    stamp = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="zh-CN">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f"<title>{_e(title)}</title>")
    parts.append(f"<style>{_CSS}</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append('<main class="wrap">')

    parts.append(_render_header(title, game_name, stamp, metrics))
    parts.append(_render_kpis(metrics))
    parts.append(_render_trend(trend))
    parts.append(_render_charts(metrics))
    parts.append(_render_timeline(history or []))
    parts.append(_render_clusters(bugs))
    parts.append(_render_bugs(bugs, ctx))
    parts.append(_render_footer(stamp))

    parts.append("</main>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


def write_report(
    bugs: list[dict],
    path: str | Path,
    *,
    history: list[dict] | None = None,
    title: str = "PlayRanger 自动试玩报告",
    game_name: str = "",
    trend: dict | None = None,
) -> str:
    """渲染并写入文件，返回绝对路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_html(
        bugs,
        history=history,
        title=title,
        report_dir=target.parent,
        game_name=game_name,
        trend=trend,
    )
    with open(target, "w", encoding="utf-8") as f:
        f.write(html_text)
    return str(target.resolve())


# ─── 各区块 ─────────────────────────────────────────────────────


def _render_header(title: str, game_name: str, stamp: str, metrics: dict) -> str:
    sub = f"{game_name}　·　" if game_name else ""
    return (
        '<header class="hero">'
        f'<div class="hero-title">{_e(title)}</div>'
        f'<div class="hero-sub">{_e(sub)}生成时间 {_e(stamp)}　·　'
        f'最高严重度 <b class="sev-text-{metrics["highest_severity"]}">'
        f'{_sev(metrics["highest_severity"])}</b>'
        f'　·　执行 {metrics["steps_total"]} 步</div>'
        "</header>"
    )


def _render_kpis(m: dict) -> str:
    review_value = (
        f"{m['review_agree']}/{m['review_total']}（{m['review_rate']:.0%}）"
        if m.get("review_total")
        else "暂无复核"
    )
    cards = [
        ("缺陷总数", str(m["total_bugs"]), "bug"),
        ("自动确认", f"{m['auto_confirmed']}（{m['auto_rate']:.0%}）", "auto"),
        ("阻断 / 紧急", f"{m['by_severity']['blocker']} / {m['by_severity']['critical']}", "danger"),
        ("平均置信度", f"{m['avg_confidence']:.0%}", "conf"),
        ("证据链通过", f"{m['evidence_ok']}（{m['evidence_rate']:.0%}）", "chain"),
        ("平均复现率", f"{m['avg_reproduction']:.0%}", "repro"),
        ("人工复核一致", review_value, "review"),
    ]
    cells = "".join(
        f'<div class="kpi kpi-{kind}"><div class="kpi-val">{_e(val)}</div>'
        f'<div class="kpi-label">{_e(label)}</div></div>'
        for label, val, kind in cards
    )
    return f'<section class="kpis">{cells}</section>'


def _render_trend(trend: dict | None) -> str:
    """跨版本趋势区块：本次 vs 基线，回答「比上个版本好转了吗」。"""
    if not trend:
        return ""

    counts = trend.get("counts") or {}
    metric_rows: list[str] = []
    metric_labels = {
        "total_bugs": ("缺陷总数", lambda v: f"{v}"),
        "severity_score": ("最高严重度分", lambda v: f"{v}"),
        "auto_rate": ("自动确认率", lambda v: f"{float(v):.0%}"),
        "avg_confidence": ("平均置信度", lambda v: f"{float(v):.0%}"),
        "evidence_rate": ("证据链通过率", lambda v: f"{float(v):.0%}"),
    }
    for name, (label, fmt) in metric_labels.items():
        cell = trend.get("metrics", {}).get(name) or {}
        try:
            delta = float(cell.get("delta", 0) or 0)
        except (TypeError, ValueError):
            delta = 0.0
        # 缺陷数 / 严重度分下降是好事，其余指标上升是好事
        good = delta < 0 if name in ("total_bugs", "severity_score") else delta > 0
        cls = "trend-good" if delta and good else ("trend-bad" if delta else "trend-flat")
        arrow = "→" if not delta else ("▲" if delta > 0 else "▼")
        try:
            cur_text = fmt(cell.get("current", 0))
            base_text = fmt(cell.get("baseline", 0))
        except (TypeError, ValueError):
            cur_text, base_text = "—", "—"
        metric_rows.append(
            "<tr>"
            f"<td>{_e(label)}</td>"
            f'<td class="num">{_e(base_text)}</td>'
            f'<td class="num">{_e(cur_text)}</td>'
            f'<td class="num {cls}">{arrow} {_e(f"{abs(delta):.2f}".rstrip("0").rstrip("."))}</td>'
            "</tr>"
        )

    def _bug_list(title: str, items: list[dict], empty_text: str) -> str:
        if not items:
            return (
                f'<div class="block"><div class="block-title">{_e(title)}</div>'
                f'<div class="empty-inline">{_e(empty_text)}</div></div>'
            )
        rows = "".join(
            "<tr>"
            f'<td><span class="tag tag-sev-{_safe_cls(str(b.get("severity", "info")))}">'
            f'{_sev(str(b.get("severity", "info")))}</span></td>'
            f'<td>{_e(str(b.get("title", "")))}</td>'
            f'<td><code>{_e(str(b.get("anomaly_type", "")))}</code></td>'
            "</tr>"
            for b in items
        )
        return (
            f'<div class="block"><div class="block-title">{_e(title)}</div>'
            '<table class="tbl"><thead><tr><th>严重度</th><th>缺陷</th><th>异常类型</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>'
        )

    changed = "".join(
        f'<li>{_e(str(c.get("title", "")))}：'
        f'{_sev(str(c.get("from_severity", "")))} → {_sev(str(c.get("to_severity", "")))}</li>'
        for c in trend.get("changed", [])
    )

    # 缺陷收敛曲线：横向条形，展示各版本缺陷总数
    spark = ""
    history = trend.get("history") or []
    if len(history) >= 2:
        max_total = max(int(s.get("total_bugs", 0) or 0) for s in history) or 1
        bars: list[str] = []
        for s in history:
            total = int(s.get("total_bugs", 0) or 0)
            width = total / max_total * 100
            is_current = str(s.get("version")) == str(trend.get("version"))
            bars.append(
                f'<div class="bar-row"><span class="bar-label">{_e(str(s.get("version", "")))}'
                f'{"（本次）" if is_current else ""}</span>'
                f'<div class="bar-track"><div class="bar-fill {"fill-blocker" if is_current else "fill-anomaly"}" '
                f'style="width:{width:.1f}%"></div></div>'
                f'<span class="bar-val">{total}</span></div>'
            )
        spark = (
            '<div class="block"><div class="block-title">缺陷收敛曲线'
            '<span class="panel-hint">各版本缺陷总数，越往右越近</span></div>'
            f'<div class="bars">{"".join(bars)}</div></div>'
        )

    return (
        '<section class="panel panel-trend">'
        '<div class="panel-title">跨版本趋势对比 '
        f'<span class="panel-hint">本次 {_e(str(trend.get("version", "")))} '
        f'对比基线 {_e(str(trend.get("baseline_version", "")))}</span></div>'
        '<div class="trend-kpis">'
        f'<div class="trend-kpi"><b>{int(counts.get("added", 0))}</b><span>新增</span></div>'
        f'<div class="trend-kpi trend-kpi-fixed"><b>{int(counts.get("fixed", 0))}</b><span>已修复</span></div>'
        f'<div class="trend-kpi"><b>{int(counts.get("persisted", 0))}</b><span>遗留</span></div>'
        f'<div class="trend-kpi trend-kpi-changed"><b>{int(counts.get("changed", 0))}</b><span>等级变化</span></div>'
        "</div>"
        '<table class="tbl"><thead><tr><th>指标</th><th>基线</th><th>本次</th><th>变化</th>'
        f'</tr></thead><tbody>{"".join(metric_rows)}</tbody></table>'
        f'{_bug_list("新增缺陷", trend.get("added", []), "没有新增缺陷")}'
        f'{_bug_list("已修复缺陷", trend.get("fixed", []), "没有缺陷被修复")}'
        + (
            f'<div class="block"><div class="block-title">严重度变化</div>'
            f'<ul class="chain">{changed}</ul></div>'
            if changed
            else ""
        )
        + spark
        + "</section>"
    )


def _render_charts(m: dict) -> str:
    return (
        '<section class="grid2">'
        f'{_bar_panel("严重度分布", m["by_severity"], _sev, max_value=max(m["by_severity"].values() or [1]))}'
        f'{_bar_panel("异常类型分布", m["by_anomaly"], lambda k: k, max_value=max(m["by_anomaly"].values() or [1]))}'
        "</section>"
        f"{_conclusion_bar(m)}"
    )


def _bar_panel(title: str, data: dict, label_fn, *, max_value: int = 1) -> str:
    rows: list[str] = []
    ordered = sorted(data.items(), key=lambda kv: (-kv[1], kv[0]))
    for key, value in ordered:
        if value <= 0 and title == "严重度分布":
            continue
        width = (value / max_value * 100) if max_value else 0
        cls = key if key in SEVERITY_ORDER else "anomaly"
        rows.append(
            f'<div class="bar-row"><span class="bar-label">{_e(label_fn(str(key)))}</span>'
            f'<div class="bar-track"><div class="bar-fill fill-{_safe_cls(cls)}" '
            f'style="width:{width:.1f}%"></div></div>'
            f'<span class="bar-val">{value}</span></div>'
        )
    if not rows:
        rows.append('<div class="empty">暂无数据</div>')
    return (
        f'<div class="panel"><div class="panel-title">{_e(title)}</div>'
        f'<div class="bars">{"".join(rows)}</div></div>'
    )


def _conclusion_bar(m: dict) -> str:
    total = m["total_bugs"] or 1
    segments: list[str] = []
    for key, label in _CONC_LABEL.items():
        count = m["by_conclusion"].get(key, 0)
        pct = count / total * 100
        if count == 0:
            continue
        segments.append(
            f'<div class="seg seg-{_CONC_CLASS.get(key, "unknown")}" '
            f'style="width:{pct:.1f}%" title="{_e(label)} {count} 个">'
            f'<span>{count}</span></div>'
        )
    if not segments:
        segments.append('<div class="seg seg-unknown" style="width:100%"><span>0</span></div>')

    legend = "".join(
        f'<span class="legend-item"><i class="dot dot-{_CONC_CLASS.get(k, "unknown")}"></i>'
        f"{_e(v)} {m['by_conclusion'].get(k, 0)}</span>"
        for k, v in _CONC_LABEL.items()
    )
    return (
        '<div class="panel"><div class="panel-title">判定结论分布 '
        '<span class="panel-hint">自动确认越多，人工复核成本越低</span></div>'
        f'<div class="segbar">{"".join(segments)}</div>'
        f'<div class="legend">{legend}</div></div>'
    )


def _render_timeline(history: list[dict]) -> str:
    if not history:
        return (
            '<div class="panel"><div class="panel-title">执行时间线 '
            '<span class="panel-hint">本次报告未附带步骤轨迹</span></div>'
            '<div class="empty">暂无步骤数据</div></div>'
        )

    # 超过 160 步时等距采样，保证页面不卡
    steps = history
    sampled = False
    if len(steps) > 160:
        stride = max(1, len(steps) // 160)
        steps = steps[::stride]
        sampled = True

    cells: list[str] = []
    for i, step in enumerate(steps):
        status = _step_status(step)
        number = step.get("step", i + 1)
        action = _step_action_text(step)
        reason = _step_reason(step)
        tip = f"第 {number} 步 {action}"
        if reason:
            tip += f"：{reason}"
        cells.append(
            f'<span class="tl-cell tl-{status}" title="{_e(tip)}"></span>'
        )

    hint = "每格一步，绿=成功，黄=触发修正，红=失败"
    if sampled:
        hint += "（轨迹较长，已等距采样）"
    return (
        '<div class="panel"><div class="panel-title">执行时间线 '
        f'<span class="panel-hint">{hint}</span></div>'
        f'<div class="timeline">{"".join(cells)}</div></div>'
    )


def _render_clusters(bugs: list[dict]) -> str:
    clusters = cluster_bugs(bugs)
    if not clusters:
        return ""
    rows: list[str] = []
    for c in clusters:
        ids = "、".join(x for x in c["bug_ids"] if x)
        rows.append(
            '<tr>'
            f'<td><code>{_e(c["anomaly_type"])}</code></td>'
            f'<td><span class="tag tag-{_safe_cls(c["stage"])}">{_e(c["stage"])}</span></td>'
            f'<td class="num">{c["count"]}</td>'
            f'<td>{_sev(c["highest_severity"])}</td>'
            f'<td class="ids">{_e(ids)}</td>'
            "</tr>"
        )
    return (
        '<div class="panel"><div class="panel-title">同类缺陷聚类 '
        '<span class="panel-hint">按异常类型 + 修复阶段收敛，便于批量派单</span></div>'
        '<table class="tbl"><thead><tr>'
        "<th>异常类型</th><th>修复阶段</th><th>数量</th><th>最高严重度</th><th>缺陷编号</th>"
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def _render_bugs(bugs: list[dict], ctx: _ReportContext) -> str:
    if not bugs:
        return (
            '<div class="panel"><div class="panel-title">缺陷详情</div>'
            '<div class="empty">本轮没有发现缺陷</div></div>'
        )

    ordered = sorted(
        bugs,
        key=lambda b: (-_sev_index(str(b.get("severity", "info"))), str(b.get("bug_id", ""))),
    )
    cards = "".join(_render_bug_card(bug, ctx) for bug in ordered)
    return (
        '<div class="panel panel-plain"><div class="panel-title">缺陷详情 '
        f'<span class="panel-hint">共 {len(bugs)} 条，按严重度排序</span></div>'
        f"{cards}</div>"
    )


def _render_bug_card(bug: dict, ctx: _ReportContext) -> str:
    intel = _intel_of(bug)
    prov = intel["provenance"]
    plan = intel["fix_plan"]

    severity = str(bug.get("severity", "info"))
    conclusion = str(bug.get("conclusion", "human_required"))
    confidence = float(plan.get("confidence", 0.0) or 0.0)
    bug_dir = bug.get("bug_dir")

    # 头部
    head = (
        f'<div class="bug-head"><div class="bug-title">'
        f'<span class="bug-id">{_e(str(bug.get("bug_id", "")))}</span>'
        f'{_e(str(bug.get("title", "")))}</div>'
        f'<div class="badges">'
        f'<span class="tag tag-sev-{severity}">{_sev(severity)}</span>'
        f'<span class="tag tag-{_CONC_CLASS.get(conclusion, "unknown")}">'
        f'{_e(_CONC_LABEL.get(conclusion, conclusion))}</span>'
        f'<span class="tag tag-plain">{_e(str(bug.get("anomaly_type", "")))}</span>'
        f"</div></div>"
    )

    # 元信息
    origin_action = prov.get("origin_action") or {}
    action_text = (
        f'{origin_action.get("action_type", "")} {origin_action.get("target") or ""}'.strip()
        or "未定位"
    )
    channels = "、".join(prov.get("channels", [])) or "未登记"
    repro = bug.get("reproduction_rate")
    repro_text = f"{float(repro):.0%}" if isinstance(repro, (int, float)) else "未复跑"
    meta = (
        '<div class="meta-grid">'
        f'<div><span>触发步骤</span><b>{prov.get("origin_step") if prov.get("origin_step") is not None else "—"}</b></div>'
        f'<div><span>触发动作</span><b>{_e(action_text)}</b></div>'
        f'<div><span>检测通道</span><b>{_e(channels)}</b></div>'
        f'<div><span>复现率</span><b>{_e(repro_text)}</b></div>'
        f'<div><span>方案置信度</span><b>{confidence:.0%}</b></div>'
        f'<div><span>截图证据</span><b>{len(prov.get("screenshots", []))} 张</b></div>'
        "</div>"
    )

    # 触发链
    chain_items = "".join(
        f"<li>{_e(str(x))}</li>" for x in prov.get("trigger_chain", [])[:8]
    )
    chain = (
        f'<div class="block"><div class="block-title">触发链</div>'
        f'<ol class="chain">{chain_items or "<li>暂无</li>"}</ol></div>'
    )

    # 状态差异
    diff_rows: list[str] = []
    for d in prov.get("state_diff", []):
        diff_rows.append(
            "<tr>"
            f'<td><code>{_e(str(d.get("field", "")))}</code></td>'
            f'<td>{_e(_short(d.get("before")))}</td>'
            f'<td class="hl">{_e(_short(d.get("after")))}</td>'
            f'<td>{_e(_short(d.get("expected")))}</td>'
            "</tr>"
        )
    diff_html = (
        '<details class="block" open><summary>状态差异（'
        f'{len(diff_rows)} 项）</summary>'
        '<table class="tbl"><thead><tr>'
        "<th>字段</th><th>触发前</th><th>触发后</th><th>期望</th>"
        f'</tr></thead><tbody>{"".join(diff_rows)}</tbody></table></details>'
        if diff_rows
        else ""
    )

    # 证据来源
    chips = "".join(
        f'<span class="chip">{_e(str(f.get("channel", "")))} · {_e(str(f.get("signal", "")))}</span>'
        for f in prov.get("findings", [])
    )
    sources = (
        f'<div class="block"><div class="block-title">证据来源</div>'
        f'<div class="chips">{chips or "<span class=\"empty-inline\">未登记</span>"}</div></div>'
    )

    # 截图
    shots = ""
    shots_list = prov.get("screenshots", [])
    if shots_list:
        items: list[str] = []
        for shot in shots_list:
            src = ctx.shot_src(bug_dir, shot.get("file", ""))
            step = shot.get("step")
            step_text = f"第 {step} 步" if step is not None else "步骤未登记"
            items.append(
                f'<figure class="shot"><img src="{_e(src)}" alt="{_e(str(shot.get("kind", "")))}" loading="lazy">'
                f'<figcaption>{_e(str(shot.get("kind", "")))} · {_e(step_text)}</figcaption>'
                "</figure>"
            )
        shots = (
            f'<div class="block"><div class="block-title">截图证据</div>'
            f'<div class="shots">{"".join(items)}</div></div>'
        )

    # 修复方案
    causes = "".join(f"<li>{_e(str(c))}</li>" for c in plan.get("root_causes", []))
    steps_html: list[str] = []
    for i, step in enumerate(plan.get("fix_steps", []), 1):
        hint = f'<span class="file-hint">{_e(str(step.get("file_hint", "")))}</span>' if step.get("file_hint") else ""
        code = ""
        if step.get("code_hint"):
            code = f'<pre class="code">{_e(str(step["code_hint"]))}</pre>'
        steps_html.append(
            f'<li><b>{_e(str(step.get("title", "")))}</b>{hint}'
            f'<div class="step-detail">{_e(str(step.get("detail", "")))}</div>{code}</li>'
        )
    verify = "".join(
        f'<li>{_e(str(v))}</li>' for v in plan.get("verification_steps", [])
    )
    modules = "、".join(plan.get("affected_modules", []))

    fix = (
        '<div class="fix">'
        '<div class="fix-head">'
        "<span>修复深化方案</span>"
        f'<span class="fix-meta">工作量 {_e(str(plan.get("effort", "—")))}　·　'
        f'回归风险 {_e(str(plan.get("regression_risk", "—")))}　·　'
        f'影响 {_e(modules or "—")}</span>'
        "</div>"
        f'<div class="block-title">可能根因</div><ol class="causes">{causes}</ol>'
        f'<div class="block-title">修复步骤</div><ol class="fix-steps">{"".join(steps_html)}</ol>'
        f'<div class="block-title">验证清单</div><ul class="verify">{verify}</ul>'
        "</div>"
    )

    return (
        f'<article class="bug sev-{severity}">'
        f"{head}{meta}{chain}{diff_html}{sources}{shots}{fix}"
        "</article>"
    )


def _render_footer(stamp: str) -> str:
    return (
        '<footer class="foot">'
        f"由 PlayRanger 自动生成 · {_e(stamp)} · "
        "单文件离线报告，无外部依赖 · 证据哈希链可在缺陷包 evidence_chain.json 中复核"
        "</footer>"
    )


# ─── 内部工具 ───────────────────────────────────────────────────


def _intel_of(bug: dict) -> dict:
    """优先复用已算好的分析结果，避免报告阶段重复计算。"""
    prov = bug.get("provenance")
    plan = bug.get("fix_plan")
    if isinstance(prov, dict) and isinstance(plan, dict) and prov and plan:
        return {"provenance": prov, "fix_plan": plan}
    return analyze_bug(bug, bug_dir=bug.get("bug_dir"))


def _step_status(step: dict) -> str:
    if "execution_ok" in step:
        if not step.get("execution_ok"):
            return "fail"
        verification = step.get("verification") or {}
        if verification.get("success") is False:
            return "fail"
        level = verification.get("correction_level") or step.get("correction_level")
        if level and level != "none":
            return "warn"
        return "ok"
    return "ok" if step.get("success", True) else "fail"


def _step_action_text(step: dict) -> str:
    action = step.get("action") or {}
    if isinstance(action, dict) and action:
        text = str(action.get("action_type", ""))
        target = action.get("target")
        if target:
            text += f" {target}"
        return text or "未知动作"
    text = str(step.get("action_type", ""))
    target = step.get("target")
    if target:
        text += f" {target}"
    return text or "未知动作"


def _step_reason(step: dict) -> str:
    verification = step.get("verification") or {}
    return str(
        verification.get("reason")
        or step.get("execution_error")
        or step.get("error")
        or ""
    )


def _sev(value: str) -> str:
    return f"{_SEV_LABEL.get(value, value)}"


def _sev_index(value: str) -> int:
    try:
        return SEVERITY_ORDER.index(value)
    except ValueError:
        return 0


def _safe_cls(value: str) -> str:
    """把任意 key 压成安全 CSS 类名片段。"""
    text = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(value))
    return text.strip("-") or "unknown"


def _short(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text if len(text) <= 48 else text[:45] + "..."


def _e(text: str) -> str:
    return html_lib.escape(str(text), quote=True)


# ─── 样式 ───────────────────────────────────────────────────────
# 注意：保持纯 CSS，不引用任何外部字体 / 图片 / 脚本。

_CSS = """
:root{
  --bg:#0f1216; --panel:#171b21; --panel2:#1d222a; --line:#2a303a;
  --text:#e6e9ef; --muted:#8b94a3; --accent:#5aa9ff;
  --ok:#37c98b; --warn:#e0a83c; --fail:#e05b5b;
  --sev-info:#7b8796; --sev-minor:#5aa9ff; --sev-major:#e0a83c;
  --sev-critical:#f0784a; --sev-blocker:#e04444;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  font-size:14px;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 56px}
.hero{margin-bottom:20px}
.hero-title{font-size:26px;font-weight:700;letter-spacing:.5px}
.hero-sub{color:var(--muted);margin-top:6px;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;position:relative;overflow:hidden}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
.kpi-danger::before{background:var(--fail)}
.kpi-auto::before{background:var(--ok)}
.kpi-chain::before{background:#8a6cf0}
.kpi-repro::before{background:var(--warn)}
.kpi-conf::before{background:#3ec7c7}
.kpi-review::before{background:#b06cf0}
.kpi-val{font-size:22px;font-weight:700}
.kpi-label{color:var(--muted);font-size:12px;margin-top:2px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.panel-plain{background:transparent;border:0;padding:0;margin-top:22px}
.panel-title{font-size:15px;font-weight:600;margin-bottom:12px}
.panel-hint{color:var(--muted);font-size:12px;font-weight:400;margin-left:8px}
.bars{display:flex;flex-direction:column;gap:8px}
.bar-row{display:grid;grid-template-columns:150px 1fr 36px;align-items:center;gap:10px}
.bar-label{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{background:#222833;border-radius:6px;height:14px;overflow:hidden}
.bar-fill{height:100%;border-radius:6px;background:var(--accent);min-width:2px}
.fill-info{background:var(--sev-info)}.fill-minor{background:var(--sev-minor)}
.fill-major{background:var(--sev-major)}.fill-critical{background:var(--sev-critical)}
.fill-blocker{background:var(--sev-blocker)}.fill-anomaly{background:#5a7fd6}
.panel-trend{border-color:#3d3560;background:linear-gradient(180deg,rgba(120,86,220,.07),transparent 60%),var(--panel)}
.trend-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:14px}
.trend-kpi{background:#1c2028;border:1px solid var(--line);border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:2px}
.trend-kpi b{font-size:20px;line-height:1.2}
.trend-kpi span{color:var(--muted);font-size:12px}
.trend-kpi-fixed b{color:var(--ok)}
.trend-kpi-changed b{color:var(--warn)}
.trend-good{color:var(--ok)}
.trend-bad{color:var(--fail)}
.trend-flat{color:var(--muted)}
.block{margin-top:14px}
.bar-val{text-align:right;font-variant-numeric:tabular-nums;color:var(--text);font-size:12px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.empty-inline{color:var(--muted)}
.segbar{display:flex;height:22px;border-radius:6px;overflow:hidden;background:#222833}
.seg{display:flex;align-items:center;justify-content:center;font-size:11px;color:#0c0f13;font-weight:700;min-width:22px}
.seg-ok{background:var(--ok)}.seg-warn{background:var(--warn)}.seg-unknown{background:#4b5666;color:#dfe4ec}
.legend{margin-top:10px;display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px}
.legend-item{display:inline-flex;align-items:center;gap:6px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.dot-ok{background:var(--ok)}.dot-warn{background:var(--warn)}.dot-unknown{background:#4b5666}
.timeline{display:flex;flex-wrap:wrap;gap:3px}
.tl-cell{width:10px;height:22px;border-radius:2px;display:inline-block;background:#2a303a}
.tl-ok{background:#2f7f5f}.tl-warn{background:#a9822f}.tl-fail{background:var(--fail)}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;color:var(--muted);font-weight:500;font-size:12px;padding:8px 10px;border-bottom:1px solid var(--line)}
.tbl td{padding:8px 10px;border-bottom:1px solid #20252d;vertical-align:top}
.tbl tr:last-child td{border-bottom:0}
.tbl .num{text-align:right;font-variant-numeric:tabular-nums}
.tbl .ids{color:var(--muted);font-size:12px}
.hl{color:var(--warn);font-weight:600}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.tag-sev-blocker{background:rgba(224,68,68,.16);border-color:#7a2c2c;color:#ff9a9a}
.tag-sev-critical{background:rgba(240,120,74,.14);border-color:#7a4429;color:#ffb08a}
.tag-sev-major{background:rgba(224,168,60,.14);border-color:#6e5520;color:#f0cd85}
.tag-sev-minor{background:rgba(90,169,255,.14);border-color:#2c4a6e;color:#a8cdff}
.tag-sev-info{background:rgba(123,135,150,.14);border-color:#3a424e;color:#b9c2cf}
.tag-ok{background:rgba(55,201,139,.14);border-color:#27604a;color:#8ce8bf}
.tag-warn{background:rgba(224,168,60,.14);border-color:#6e5520;color:#f0cd85}
.tag-unknown{background:#232933;border-color:#333c49;color:#9aa4b4}
.tag-plain{background:#20262f}
.tag-立即修{background:rgba(224,68,68,.16);border-color:#7a2c2c;color:#ff9a9a}
.tag-本迭代修{background:rgba(224,168,60,.14);border-color:#6e5520;color:#f0cd85}
.tag-排期修{background:#232933;border-color:#333c49;color:#9aa4b4}
.sev-text-blocker{color:#ff8f8f}.sev-text-critical{color:#ffb08a}
.sev-text-major{color:#f0cd85}.sev-text-minor{color:#a8cdff}.sev-text-info{color:#b9c2cf}
.bug{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--sev-info);
  border-radius:10px;padding:16px 18px;margin-bottom:14px}
.bug.sev-blocker{border-left-color:var(--sev-blocker)}
.bug.sev-critical{border-left-color:var(--sev-critical)}
.bug.sev-major{border-left-color:var(--sev-major)}
.bug.sev-minor{border-left-color:var(--sev-minor)}
.bug-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
.bug-title{font-size:16px;font-weight:650}
.bug-id{color:var(--accent);font-family:ui-monospace,Consolas,monospace;margin-right:8px;font-size:13px}
.badges{display:flex;gap:6px;flex-wrap:wrap}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin:14px 0}
.meta-grid>div{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.meta-grid span{display:block;color:var(--muted);font-size:11px}
.meta-grid b{font-weight:600;font-size:13px}
.block{margin-top:12px}
.block-title{font-size:13px;font-weight:600;color:#c3cad6;margin:12px 0 6px}
details.block summary{font-size:13px;font-weight:600;color:#c3cad6;cursor:pointer;margin:12px 0 6px}
.chain{margin:0;padding-left:20px;color:#cfd6e2;font-size:13px}
.chain li{margin:3px 0}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{background:#20262f;border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:11px;color:#aeb8c7}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
.shot{margin:0;background:var(--panel2);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.shot img{display:block;width:100%;height:auto;background:#0a0d11}
.shot figcaption{color:var(--muted);font-size:11px;padding:6px 8px}
.fix{margin-top:16px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.fix-head{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;
  font-weight:600;color:#d7dee9;font-size:13px}
.fix-meta{color:var(--muted);font-weight:400;font-size:12px}
.causes,.fix-steps,.verify{margin:0;padding-left:20px;font-size:13px;color:#cfd6e2}
.causes li,.fix-steps li,.verify li{margin:5px 0}
.step-detail{color:var(--muted);font-size:12px;margin-top:2px}
.file-hint{color:var(--accent);font-family:ui-monospace,Consolas,monospace;font-size:11px;margin-left:8px}
.code{background:#0b0e12;border:1px solid #232a34;border-radius:6px;padding:8px 10px;
  font-family:ui-monospace,Consolas,monospace;font-size:12px;overflow-x:auto;color:#b9d4f5;margin:6px 0 2px}
.foot{margin-top:28px;color:var(--muted);font-size:12px;text-align:center;line-height:1.8}
code{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#9ec7ff}
@media print{
  body{background:#fff;color:#111}
  .panel,.bug,.kpi,.fix,.meta-grid>div{background:#fff;border-color:#ccc}
  .hero-sub,.kpi-label,.bar-label,.panel-hint,.foot,.step-detail,.muted{color:#555}
}
"""


__all__ = ["build_metrics", "render_html", "write_report"]
