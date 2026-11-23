"""Bug 溯源 —— 回答「这个缺陷到底是从哪一步、哪个动作、哪类证据来的」。

解决的问题：
- 缺陷包里只有一堆轨迹文件，人要在几十步里翻找触发点
- 说不清是视觉检测发现的、SDK 状态比对发现的，还是断言失败的
- 说不清「期望什么 / 实际什么」，修复时只能靠猜

做法（纯规则、零依赖、离线可跑）：
1. 从动作轨迹里倒序找第一个失败步 → 触发点（origin_step）
2. 对比该步前后状态，产出字段级差异（state_diff）
3. 从 evidence 映射与 execution_error 归纳检测通道（findings）
4. 截取「初始 → 触发点」的动作序列作为回放窗口（replay_actions）
5. 若缺陷包带 evidence_chain.json，则附上截图与哈希（screenshots）

输出既可直接序列化进 API，也可 to_markdown() 贴进缺陷单。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 不参与状态差异比对的字段（噪声大、无诊断价值）
_IGNORED_DIFF_FIELDS = {
    "available_actions",
    "history",
    "events",
    "timestamp",
    "time",
    "fps",
    "frame",
}

_CHANNEL_LABELS = {
    "sdk_state": "SDK 状态比对",
    "vision_vlm": "视觉检测",
    "action_trace": "动作轨迹回放",
    "game_log": "游戏日志",
    "human_annotated": "人工标注",
    "exception": "运行时异常",
    "assertion": "断言校验",
    "strategy": "策略卡死聚合",
    "unknown": "未知来源",
}


# ─── 数据结构 ───────────────────────────────────────────────────


@dataclass
class EvidenceFinding:
    """一条来源证据。"""

    channel: str                 # sdk_state / vision_vlm / action_trace / exception / assertion / strategy
    signal: str                  # 具体信号（异常类型、错误文本、字段名）
    detail: str = ""
    step: int | None = None
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BugProvenance:
    """一个缺陷的完整溯源结果。"""

    bug_id: str = ""
    title: str = ""
    anomaly_type: str = ""
    severity: str = "info"
    conclusion: str = "human_required"

    origin_step: int | None = None
    origin_action: dict = field(default_factory=dict)
    trigger_chain: list[str] = field(default_factory=list)
    findings: list[EvidenceFinding] = field(default_factory=list)

    state_diff: list[dict] = field(default_factory=list)
    assertion_check: dict = field(default_factory=dict)

    replay_actions: list[dict] = field(default_factory=list)
    replay_truncated: bool = False

    screenshots: list[dict] = field(default_factory=list)
    evidence_sources: dict = field(default_factory=dict)
    reproduction: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # ─── 序列化 ──────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "bug_id": self.bug_id,
            "title": self.title,
            "anomaly_type": self.anomaly_type,
            "severity": self.severity,
            "conclusion": self.conclusion,
            "origin_step": self.origin_step,
            "origin_action": self.origin_action,
            "trigger_chain": self.trigger_chain,
            "findings": [f.to_dict() for f in self.findings],
            "channels": self.channels,
            "state_diff": self.state_diff,
            "assertion_check": self.assertion_check,
            "replay_actions": self.replay_actions,
            "replay_truncated": self.replay_truncated,
            "screenshots": self.screenshots,
            "evidence_sources": self.evidence_sources,
            "reproduction": self.reproduction,
            "notes": self.notes,
        }

    @property
    def channels(self) -> list[str]:
        """去重后的检测通道（稳定排序）。"""
        return sorted({f.channel for f in self.findings})

    def channel_labels(self) -> list[str]:
        """检测通道的中文名，便于直接展示。"""
        return [_CHANNEL_LABELS.get(c, c) for c in self.channels]

    # ─── 人读输出 ────────────────────────────────────────────

    def to_markdown(self) -> str:
        """转成可直接贴进缺陷单的 Markdown。"""
        lines: list[str] = []
        lines.append(f"## {self.bug_id} {self.title}".strip())
        lines.append("")
        lines.append(
            f"- 异常类型：`{self.anomaly_type}`　严重度：**{self.severity}**　结论：`{self.conclusion}`"
        )
        lines.append(f"- 触发步骤：第 {self.origin_step} 步" if self.origin_step else "- 触发步骤：未定位")
        if self.origin_action:
            lines.append(f"- 触发动作：`{self._format_action(self.origin_action)}`")
        if self.channels:
            lines.append(f"- 检测通道：{' / '.join(self.channel_labels())}")
        lines.append("")

        if self.trigger_chain:
            lines.append("### 触发链")
            for i, item in enumerate(self.trigger_chain, 1):
                lines.append(f"{i}. {item}")
            lines.append("")

        if self.findings:
            lines.append("### 证据来源")
            for f in self.findings:
                step = f"（第 {f.step} 步）" if f.step is not None else ""
                conf = f"，置信度 {f.confidence:.2f}" if f.confidence > 0 else ""
                lines.append(
                    f"- **{_CHANNEL_LABELS.get(f.channel, f.channel)}**{step}："
                    f"{f.signal}{conf}　{f.detail}".rstrip()
                )
            lines.append("")

        if self.state_diff:
            lines.append("### 状态差异")
            lines.append("")
            lines.append("| 字段 | 触发前 | 触发后 | 期望 |")
            lines.append("| --- | --- | --- | --- |")
            for d in self.state_diff:
                lines.append(
                    f"| `{d.get('field')}` | {_short(d.get('before'))} | "
                    f"{_short(d.get('after'))} | {_short(d.get('expected'))} |"
                )
            lines.append("")

        if self.assertion_check:
            ac = self.assertion_check
            passed = "通过" if ac.get("passed") else "未通过"
            lines.append(
                f"### 断言校验\n\n- {ac.get('type', 'state_equals')} "
                f"`{ac.get('field')}` 期望 {_short(ac.get('expected'))}，"
                f"实际 {_short(ac.get('actual'))} → {passed}"
            )
            lines.append("")

        if self.replay_actions:
            lines.append(f"### 复现动作序列（{len(self.replay_actions)} 步）")
            if self.replay_truncated:
                lines.append("")
                lines.append("> 轨迹较长，仅保留触发点之前的最后若干步。")
            lines.append("")
            for a in self.replay_actions:
                lines.append(f"{a.get('step')}. `{self._format_action(a)}`")
            lines.append("")

        if self.screenshots:
            lines.append("### 截图证据")
            lines.append("")
            for s in self.screenshots:
                sha = str(s.get("sha256", ""))[:12]
                step = s.get("step")
                step_text = f"第 {step} 步" if step is not None else "步骤未登记"
                lines.append(
                    f"- `{s.get('file')}`（{s.get('kind')}，{step_text}，sha256:{sha}…）"
                )
            lines.append("")

        if self.reproduction:
            r = self.reproduction
            lines.append(
                f"### 复现情况\n\n- 复现率 {float(r.get('rate', 0)):.0%}"
                f"（{r.get('reproduced', 0)}/{r.get('attempts', 0)} 次）"
            )
            lines.append("")

        if self.notes:
            lines.append("### 备注")
            lines.extend(f"- {n}" for n in self.notes)

        return "\n".join(lines).rstrip() + "\n"

    # ─── 内部 ────────────────────────────────────────────────

    @staticmethod
    def _format_action(action: dict) -> str:
        action_type = action.get("action_type") or action.get("type") or "?"
        target = action.get("target") or action.get("selector") or ""
        params = action.get("params") or {}
        text = action_type
        if target:
            text += f" {target}"
        if params:
            text += f" {json.dumps(params, ensure_ascii=False)}"
        return text


# ─── 构建入口 ───────────────────────────────────────────────────


def build_provenance(
    bug: dict,
    *,
    bug_dir: str | Path | None = None,
    max_actions: int = 30,
) -> BugProvenance:
    """从一条缺陷记录构建溯源信息。

    Args:
        bug: 缺陷字典（recorder / API 的记录格式，键缺失时自动降级）
        bug_dir: 缺陷包目录，提供时读取 evidence_chain.json 补充截图哈希
        max_actions: 回放窗口最多保留多少步

    Returns:
        BugProvenance
    """
    trace: list[dict] = list(bug.get("action_trace") or [])
    initial_state: dict = bug.get("initial_state") or {}
    final_state: dict = bug.get("final_state") or {}
    assertion: dict = bug.get("assertion") or {}
    evidence: dict = bug.get("evidence") or {}

    origin_index, origin_step = _find_origin(trace)
    origin_action = _action_of(trace[origin_index]) if origin_index is not None else {}

    prov = BugProvenance(
        bug_id=str(bug.get("bug_id", "")),
        title=str(bug.get("title", "")),
        anomaly_type=str(bug.get("anomaly_type", "")),
        severity=str(bug.get("severity", "info")),
        conclusion=str(bug.get("conclusion", "human_required")),
        origin_step=origin_step,
        origin_action=origin_action,
        evidence_sources=dict(evidence),
        reproduction={
            "rate": float(bug.get("reproduction_rate", 0.0) or 0.0),
            "reproduced": int(bug.get("reproduced_count", 0) or 0),
            "attempts": int(bug.get("reproduction_attempts", 0) or 0),
        },
    )

    # 1. 状态差异
    before, after = _state_pair(trace, origin_index, initial_state, final_state)
    prov.state_diff = _diff_states(before, after, assertion)

    # 2. 断言校验
    if assertion:
        prov.assertion_check = _check_assertion(assertion, after or final_state)

    # 3. 检测通道
    prov.findings = _collect_findings(
        bug=bug,
        trace=trace,
        origin_index=origin_index,
        origin_step=origin_step,
        assertion=assertion,
        evidence=evidence,
    )

    # 4. 触发链
    prov.trigger_chain = _build_trigger_chain(
        trace=trace,
        origin_index=origin_index,
        state_diff=prov.state_diff,
        assertion_check=prov.assertion_check,
        evidence=evidence,
    )

    # 5. 回放窗口
    prov.replay_actions, prov.replay_truncated = _build_replay(
        trace, origin_index, max_actions
    )

    # 6. 截图哈希（可选）
    if bug_dir:
        prov.screenshots = _load_screenshots(bug_dir)

    # 7. 备注
    if not trace:
        prov.notes.append("缺陷包中没有动作轨迹，无法定位触发点")
    if prov.replay_truncated:
        prov.notes.append(f"动作轨迹共 {len(trace)} 步，回放窗口已截断")
    if not prov.findings:
        prov.notes.append("未登记证据来源，建议补充截图或 SDK 状态比对")

    return prov


def provenance_from_file(bug_dir: str | Path) -> BugProvenance:
    """直接从缺陷包目录构建溯源（读取初始/结束状态与轨迹）。"""
    from playranger.core.agent.recorder import load_defect

    record = load_defect(bug_dir)
    return build_provenance(record.to_dict(), bug_dir=bug_dir)


# ─── 内部实现 ───────────────────────────────────────────────────


def _find_origin(trace: list[dict]) -> tuple[int | None, int | None]:
    """倒序找第一个失败步；全部成功时取最后一步。"""
    if not trace:
        return None, None
    for i in range(len(trace) - 1, -1, -1):
        if not _step_ok(trace[i]):
            return i, _step_number(trace[i], i)
    return len(trace) - 1, _step_number(trace[-1], len(trace) - 1)


def _step_number(step: dict, index: int) -> int:
    value = step.get("step")
    try:
        return int(value)
    except (TypeError, ValueError):
        return index + 1


def _step_ok(step: dict) -> bool:
    """兼容 StepInfo 与引擎级 StepRecord 两种轨迹格式。"""
    if "execution_ok" in step:
        if not step.get("execution_ok"):
            return False
        verification = step.get("verification") or {}
        if verification and verification.get("success") is False:
            return False
        return True
    return bool(step.get("success", True))


def _step_reason(step: dict) -> str:
    verification = step.get("verification") or {}
    return str(
        verification.get("reason")
        or step.get("execution_error")
        or step.get("error")
        or ""
    )


def _action_of(step: dict) -> dict:
    action = step.get("action")
    if isinstance(action, dict) and action:
        return {
            "action_type": action.get("action_type", ""),
            "target": action.get("target"),
            "params": action.get("params") or {},
        }
    return {
        "action_type": step.get("action_type", ""),
        "target": step.get("target"),
        "params": step.get("params") or {},
    }


def _state_pair(
    trace: list[dict],
    origin_index: int | None,
    initial_state: dict,
    final_state: dict,
) -> tuple[dict, dict]:
    """取触发点前后的状态。"""
    if origin_index is None:
        return initial_state, final_state

    step = trace[origin_index]
    before = step.get("state") or initial_state

    verification = step.get("verification") or {}
    after = verification.get("final_state")
    if not isinstance(after, dict) or not after:
        # 取下一步的 state，或最终状态
        if origin_index + 1 < len(trace):
            after = trace[origin_index + 1].get("state")
        if not isinstance(after, dict) or not after:
            after = step.get("state_after") or final_state

    return before or {}, after or {}


def _diff_states(before: dict, after: dict, assertion: dict) -> list[dict]:
    """字段级状态差异。"""
    expected_field = assertion.get("field")
    expected_value = assertion.get("value")

    diff: list[dict] = []
    for key in sorted(set(before) | set(after)):
        if key in _IGNORED_DIFF_FIELDS:
            continue
        b = before.get(key)
        a = after.get(key)
        if b == a:
            continue
        diff.append(
            {
                "field": key,
                "before": b,
                "after": a,
                "expected": expected_value if key == expected_field else None,
            }
        )

    # 断言字段即使没变也要标出来（说明「该变的没变」）
    if expected_field and not any(d["field"] == expected_field for d in diff):
        diff.append(
            {
                "field": expected_field,
                "before": before.get(expected_field),
                "after": after.get(expected_field),
                "expected": expected_value,
                "note": "字段在触发前后均未变化，与期望不符",
            }
        )

    return diff[:20]


def _check_assertion(assertion: dict, actual_state: dict) -> dict:
    """校验断言，返回结构化的期望 / 实际对照。"""
    a_type = str(assertion.get("type", "state_equals"))
    field = assertion.get("field")
    expected = assertion.get("value")
    actual = actual_state.get(field) if field else None

    passed = False
    if a_type == "state_equals":
        passed = actual == expected
    elif a_type == "state_less_than":
        try:
            passed = float(actual) < float(expected)
        except (TypeError, ValueError):
            passed = False
    elif a_type == "state_greater_than":
        try:
            passed = float(actual) > float(expected)
        except (TypeError, ValueError):
            passed = False
    else:
        # 未支持的断言类型：不臆断通过
        passed = False

    return {
        "type": a_type,
        "field": field,
        "expected": expected,
        "actual": actual,
        "passed": passed,
    }


def _collect_findings(
    *,
    bug: dict,
    trace: list[dict],
    origin_index: int | None,
    origin_step: int | None,
    assertion: dict,
    evidence: dict,
) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []

    # 1. 显式登记的 evidence 映射
    for signal, channel in (evidence or {}).items():
        channel = str(channel or "unknown")
        conf = 0.0
        if channel == "vision_vlm":
            conf = float(bug.get("vision_confidence", 0.0) or 0.0)
        findings.append(
            EvidenceFinding(
                channel=channel if channel in _CHANNEL_LABELS else "unknown",
                signal=str(signal),
                detail=f"由{_CHANNEL_LABELS.get(channel, channel)}登记",
                step=origin_step,
                confidence=conf,
            )
        )

    # 2. 运行时异常
    if origin_index is not None:
        step = trace[origin_index]
        error = step.get("execution_error") or step.get("error")
        if error:
            findings.append(
                EvidenceFinding(
                    channel="exception",
                    signal=str(error)[:200],
                    detail="动作执行阶段抛出异常",
                    step=origin_step,
                )
            )

    # 3. 断言失败
    if assertion:
        expected = assertion.get("value")
        findings.append(
            EvidenceFinding(
                channel="assertion",
                signal=f"{assertion.get('type', 'state_equals')} {assertion.get('field')}={expected}",
                detail="失败断言可在复现时自动校验",
                step=origin_step,
            )
        )

    # 4. 策略卡死（连续 L2 失败）
    anomaly = str(bug.get("anomaly_type", ""))
    if anomaly == "strategy_stuck" or "consecutive_l2_failures" in evidence:
        findings.append(
            EvidenceFinding(
                channel="strategy",
                signal=anomaly or "consecutive_l2_failures",
                detail="连续多次路径修正失败，归入策略层异常",
                step=origin_step,
            )
        )

    # 去重（同 channel + signal 只留一条）
    seen: set[tuple[str, str]] = set()
    unique: list[EvidenceFinding] = []
    for f in findings:
        key = (f.channel, f.signal)
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    return unique


def _build_trigger_chain(
    *,
    trace: list[dict],
    origin_index: int | None,
    state_diff: list[dict],
    assertion_check: dict,
    evidence: dict,
) -> list[str]:
    chain: list[str] = []

    if origin_index is not None and origin_index < len(trace):
        step = trace[origin_index]
        number = _step_number(step, origin_index)
        action = _action_of(step)
        reason = _step_reason(step)

        if step.get("validation") and not (step.get("validation") or {}).get("ok", True):
            validation = step.get("validation") or {}
            chain.append(
                f"第 {number} 步动作 `{BugProvenance._format_action(action)}` "
                f"未通过 {validation.get('failed_layer', '语义')} 校验：{validation.get('reason', '')}"
            )
        elif reason:
            chain.append(f"第 {number} 步执行 `{BugProvenance._format_action(action)}` 失败：{reason}")
        else:
            chain.append(f"第 {number} 步执行 `{BugProvenance._format_action(action)}` 后出现异常")

        # 前一步作为上下文
        if origin_index > 0:
            prev = trace[origin_index - 1]
            prev_action = _action_of(prev)
            chain.append(
                f"前一步（第 {_step_number(prev, origin_index - 1)} 步）"
                f"`{BugProvenance._format_action(prev_action)}`"
            )

    if assertion_check:
        status = "通过" if assertion_check.get("passed") else "未通过"
        chain.append(
            f"断言 `{assertion_check.get('field')}` 期望 "
            f"{_short(assertion_check.get('expected'))}，实际 "
            f"{_short(assertion_check.get('actual'))} → {status}"
        )

    for diff in state_diff[:3]:
        expected = diff.get("expected")
        suffix = f"，期望 {_short(expected)}" if expected is not None else ""
        if diff.get("before") == diff.get("after"):
            chain.append(
                f"状态 `{diff.get('field')}` 在触发前后均为 "
                f"{_short(diff.get('before'))}{suffix}（该变的没变）"
            )
        else:
            chain.append(
                f"状态 `{diff.get('field')}` 由 {_short(diff.get('before'))} "
                f"变为 {_short(diff.get('after'))}{suffix}"
            )

    for signal, channel in (evidence or {}).items():
        chain.append(f"{_CHANNEL_LABELS.get(str(channel), channel)} 命中信号 `{signal}`")

    return chain


def _build_replay(
    trace: list[dict], origin_index: int | None, max_actions: int
) -> tuple[list[dict], bool]:
    if not trace:
        return [], False

    end = (origin_index + 1) if origin_index is not None else len(trace)
    window = trace[:end]
    truncated = False

    if len(window) > max_actions:
        window = window[-max_actions:]
        truncated = True

    actions: list[dict] = []
    for i, step in enumerate(window):
        number = _step_number(step, i)
        action = _action_of(step)
        actions.append(
            {
                "step": number,
                "action_type": action.get("action_type", ""),
                "target": action.get("target"),
                "params": action.get("params") or {},
            }
        )
    return actions, truncated


def _load_screenshots(bug_dir: str | Path) -> list[dict]:
    path = Path(bug_dir) / "evidence_chain.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    shots: list[dict] = []
    for entry in payload.get("entries", []) or []:
        shots.append(
            {
                "file": entry.get("file", ""),
                "kind": entry.get("kind", ""),
                "step": entry.get("step"),
                "sha256": entry.get("sha256", ""),
                "size": entry.get("size", 0),
                "captured_at": entry.get("captured_at", ""),
            }
        )
    return shots


def _short(value: Any) -> str:
    """把任意值压成适合表格的单行短文本。"""
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    if len(text) > 60:
        text = text[:57] + "..."
    return text.replace("|", "\\|").replace("\n", " ")


__all__ = [
    "BugProvenance",
    "EvidenceFinding",
    "build_provenance",
    "provenance_from_file",
]
