"""缺陷分级自动判定 —— 根据异常类型、证据强度、复现率自动定级。

设计目标：
- 不让 LLM 拍脑袋决定严重程度，用可解释的规则引擎
- 每条结论都给出理由（reasons），便于人工复核
- 严重度 5 档：info < minor < major < critical < blocker
- 结论 3 档：auto_confirmed（自动确认）/ auto_warning（自动预警）/ human_required（需人工）

规则优先级：
1. 异常类型基线（查表）
2. 证据加权（证据源数量 / 视觉置信度）
3. 复现率定结论档位
4. 连续失联升级（策略卡死类）
5. 人工反馈覆盖（项目级 triage_overrides.json，见 feedback.py）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playranger.core.agent.triage_config import TriageConfig, parse_config

# 严重度从低到高
SEVERITY_ORDER = ("info", "minor", "major", "critical", "blocker")

CONCLUSION_AUTO_CONFIRMED = "auto_confirmed"
CONCLUSION_AUTO_WARNING = "auto_warning"
CONCLUSION_HUMAN_REQUIRED = "human_required"


@dataclass
class TriageResult:
    """分级结果。"""

    severity: str = "info"
    conclusion: str = CONCLUSION_HUMAN_REQUIRED
    confidence: float = 0.0
    impact: str = ""
    action_hint: str = ""
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "conclusion": self.conclusion,
            "confidence": round(self.confidence, 2),
            "impact": self.impact,
            "action_hint": self.action_hint,
            "reasons": self.reasons,
        }


# ─── 规则表 ─────────────────────────────────────────────────────
# anomaly_type -> (基线严重度, 影响描述, 处理建议)

_RULES: dict[str, tuple[str, str, str]] = {
    # 崩溃类 —— 功能完全不可用
    "crash_page": (
        "blocker",
        "页面崩溃 / 错误页，游戏无法继续",
        "检查前端异常与资源加载，阻断性缺陷需立即修复",
    ),
    "crash": (
        "blocker",
        "运行崩溃，功能完全不可用",
        "抓取控制台堆栈，定位崩溃点",
    ),
    # 黑屏 / 白屏 / 冻结 —— 玩家看不到内容
    "black_screen": (
        "critical",
        "画面全黑，玩家无法获取任何信息",
        "检查渲染管线与场景切换逻辑",
    ),
    "white_screen": (
        "critical",
        "画面全白，内容未渲染",
        "检查首屏加载与 DOM 挂载",
    ),
    "frozen_screen": (
        "critical",
        "画面冻结无响应，疑似死锁",
        "检查主循环与阻塞调用",
    ),
    "error_popup": (
        "critical",
        "出现错误弹窗，中断游戏流程",
        "检查异常捕获与弹窗来源",
    ),
    "interaction_blocked": (
        "critical",
        "交互被阻塞（如 alert 阻塞），玩家无法继续操作",
        "排查原生弹窗 / 事件劫持，改为非阻塞提示",
    ),
    # 状态与数据一致性
    "hp_not_reset": (
        "major",
        "重新开始后生命值未重置，破坏关卡公平性",
        "检查关卡初始化逻辑是否遗漏状态重置",
    ),
    "state_inconsistency": (
        "major",
        "内部状态与预期不一致，可能导致后续逻辑错乱",
        "对照状态机定义核对状态迁移",
    ),
    "score_mismatch": (
        "major",
        "结算显示分数与实际得分不一致，误导玩家",
        "核对结算展示字段与计分逻辑",
    ),
    "ui_display_error": (
        "major",
        "UI 显示的数据与实际状态不符",
        "核对 UI 绑定字段与数据源",
    ),
    "logic_error": (
        "major",
        "游戏逻辑计算结果错误",
        "定位计算分支，补充单元测试",
    ),
    # 视觉 / 布局瑕疵
    "text_overflow": (
        "minor",
        "文字溢出或重叠，影响可读性",
        "检查文本容器宽度与换行",
    ),
    "ui_broken": (
        "minor",
        "UI 元素错位或变形",
        "检查布局约束与响应式规则",
    ),
    "layout_break": (
        "minor",
        "布局错乱，影响观感",
        "检查栅格与断点样式",
    ),
    "visual_glitch": (
        "minor",
        "视觉异常（闪烁 / 残影 / 错色）",
        "排查渲染时序与图层",
    ),
    "performance": (
        "minor",
        "卡顿或加载缓慢，影响体验",
        "采样帧耗时，定位热点",
    ),
    "strategy_stuck": (
        "minor",
        "Agent 连续多轮无法推进，疑似引导缺失或逻辑卡死",
        "人工验证是否存在卡死点或缺失提示",
    ),
    "unknown": (
        "info",
        "未归类的异常",
        "人工复核",
    ),
}


def _rule_for(anomaly_type: str) -> tuple[str, str, str]:
    """取规则，未知类型走兜底。"""
    return _RULES.get(
        anomaly_type,
        ("minor", f"未登记的异常类型: {anomaly_type}", "人工复核"),
    )


def _sev_index(sev: str) -> int:
    try:
        return SEVERITY_ORDER.index(sev)
    except ValueError:
        return 0


def _bump(sev: str, levels: int = 1) -> str:
    """严重度上调若干档（封顶 blocker）。"""
    idx = min(len(SEVERITY_ORDER) - 1, _sev_index(sev) + levels)
    return SEVERITY_ORDER[idx]


def triage(
    anomaly_type: str,
    *,
    evidence: dict[str, str] | None = None,
    reproduction_rate: float = 0.0,
    reproduction_attempts: int = 0,
    vision_confidence: float = 0.0,
    consecutive_failures: int = 0,
    override: dict | None = None,
    config: TriageConfig | dict | None = None,
) -> TriageResult:
    """对单个缺陷做自动分级。

    Args:
        anomaly_type: 异常类型（见 vision_engine.ANOMALY_TYPES 与状态类异常）
        evidence: 证据来源映射 {现象: 来源}，来源见 EVIDENCE_SOURCES
        reproduction_rate: 复现率 0~1（0 表示尚未复现）
        reproduction_attempts: 复现尝试次数
        vision_confidence: 视觉检测置信度 0~1
        consecutive_failures: 连续失败次数（策略卡死类用）
        override: 项目级人工反馈规则
            {severity?, conclusion?, fp_count?}，来自 feedback.overrides_for()
        config: 项目级分级配置（TriageConfig 或同构字典）；不传则用内置规则

    Returns:
        TriageResult
    """
    cfg = _as_config(config)

    # 项目配置：显式忽略的类型直接降为 info / 需人工
    if cfg.ignored(anomaly_type):
        return TriageResult(
            severity="info",
            conclusion=CONCLUSION_HUMAN_REQUIRED,
            confidence=0.0,
            impact="按项目配置忽略该异常类型",
            action_hint="如需恢复，请从配置 ignore 列表移除",
            reasons=[f"异常类型 {anomaly_type} 在项目配置中被忽略"],
        )

    base_severity, impact, hint = _rule_for(anomaly_type)
    configured_severity = cfg.severity_for(anomaly_type)
    if configured_severity:
        base_severity = configured_severity
    reasons: list[str] = [f"异常类型 {anomaly_type} 基线定级为 {base_severity}"]

    severity = base_severity
    confidence = 0.4  # 规则命中的基础置信度

    # ── 证据加权 ──────────────────────────────────────────────
    evidence = evidence or {}
    sources = set(evidence.values())
    if len(sources) >= 2:
        confidence += cfg.weight("multi_source")
        reasons.append(f"存在 {len(sources)} 类独立证据源（{', '.join(sorted(sources))}）")
    if "sdk_state" in sources:
        confidence += cfg.weight("sdk_state")
        reasons.append("有 SDK 状态证据，可精确比对内部状态")
    if "vision_vlm" in sources and vision_confidence > 0:
        confidence += vision_confidence * cfg.weight("vision")
        reasons.append(f"视觉检测置信度 {vision_confidence:.2f}")
    if "action_trace" in sources:
        confidence += cfg.weight("action_trace")
        reasons.append("有完整动作轨迹可回放")

    # ── 连续失败升级 ──────────────────────────────────────────
    if consecutive_failures >= 3:
        severity = _bump(severity)
        confidence += 0.1
        reasons.append(f"连续 {consecutive_failures} 次失败，严重度上调一档")

    # ── 复现率决定结论档位（门槛可由项目配置覆盖）────────────
    confirm_rate = cfg.threshold("auto_confirmed_rate")
    warning_rate = cfg.threshold("auto_warning_rate")
    if reproduction_rate >= confirm_rate and len(sources) >= 1:
        conclusion = CONCLUSION_AUTO_CONFIRMED
        confidence += 0.15
        reasons.append(f"复现率 {reproduction_rate:.0%}，可稳定复现")
    elif reproduction_rate >= warning_rate:
        conclusion = CONCLUSION_AUTO_WARNING
        reasons.append(f"复现率 {reproduction_rate:.0%}，偶发可复现")
    elif reproduction_attempts >= 3:
        conclusion = CONCLUSION_AUTO_WARNING
        reasons.append(f"尝试 {reproduction_attempts} 次复现，复现率偏低（{reproduction_rate:.0%}）")
    else:
        conclusion = CONCLUSION_HUMAN_REQUIRED
        reasons.append("尚未做复现验证，需人工判断")

    # ── 人工反馈：连续误报降级 ────────────────────────────────
    override = override or {}
    fp_count = 0
    try:
        fp_count = int(override.get("fp_count", 0) or 0)
    except (TypeError, ValueError):
        fp_count = 0
    if fp_count >= 2 and conclusion == CONCLUSION_AUTO_CONFIRMED:
        conclusion = CONCLUSION_AUTO_WARNING
        reasons.append(f"同类异常已被人工标记误报 {fp_count} 次，自动确认降级为自动预警")

    # ── 人工反馈：显式覆盖优先级最高 ──────────────────────────
    override_sev = override.get("severity")
    if override_sev in SEVERITY_ORDER and override_sev != severity:
        reasons.append(f"人工反馈将该类型严重度调整为 {override_sev}")
        severity = override_sev
    override_conc = override.get("conclusion")
    if override_conc in (CONCLUSION_AUTO_CONFIRMED, CONCLUSION_AUTO_WARNING, CONCLUSION_HUMAN_REQUIRED):
        if override_conc != conclusion:
            reasons.append(f"人工反馈将该类型结论调整为 {override_conc}")
            conclusion = override_conc

    confidence = max(0.0, min(1.0, confidence))

    return TriageResult(
        severity=severity,
        conclusion=conclusion,
        confidence=confidence,
        impact=impact,
        action_hint=hint,
        reasons=reasons,
    )


def _as_config(config: TriageConfig | dict | None) -> TriageConfig:
    """把 dict / None 归一成 TriageConfig。"""
    if isinstance(config, TriageConfig):
        return config
    if isinstance(config, dict):
        return parse_config(config)
    return TriageConfig()


def severity_score(severity: str) -> int:
    """严重度转分值（0~100），用于排序与聚合。"""
    mapping = {"info": 10, "minor": 30, "major": 70, "critical": 90, "blocker": 100}
    return mapping.get(severity, 10)


def summarize_bugs(bugs: list[dict]) -> dict:
    """聚合一批缺陷的分级统计。"""
    by_severity: dict[str, int] = {s: 0 for s in SEVERITY_ORDER}
    by_conclusion: dict[str, int] = {
        CONCLUSION_AUTO_CONFIRMED: 0,
        CONCLUSION_AUTO_WARNING: 0,
        CONCLUSION_HUMAN_REQUIRED: 0,
    }
    for b in bugs:
        sev = b.get("severity", "info")
        if sev in by_severity:
            by_severity[sev] += 1
        conc = b.get("conclusion", CONCLUSION_HUMAN_REQUIRED)
        if conc in by_conclusion:
            by_conclusion[conc] += 1

    return {
        "total": len(bugs),
        "by_severity": by_severity,
        "by_conclusion": by_conclusion,
        "highest_severity": _highest(
            [b.get("severity", "info") for b in bugs if b.get("severity", "info") in SEVERITY_ORDER]
        ),
    }


def _highest(severities: list[str]) -> str:
    if not severities:
        return "info"
    return max(severities, key=_sev_index)
