"""误报反馈闭环 —— 人工复核结果回写到分级规则，同类缺陷自动降级。

为什么需要：
- 规则引擎的分级阈值是基于演示游戏调校的，真实项目必然有偏差
- 没有反馈通道，同一个误报会在每一轮重复消耗人工复核成本

机制：
1. 每条缺陷的人工结论写入缺陷包 feedback.json（可多次追加）
2. 同一 anomaly_type 连续 2 次被标为误报，后续新缺陷的
   auto_confirmed 自动降为 auto_warning，不再冒充「已确认」
3. 可显式指定该异常类型的严重度 / 结论，覆盖内置规则表
4. 覆盖结果落在 defects_root/triage_overrides.json，供 BugRecorder 读取

设计约束：
- 全部为本地 JSON，不引入数据库；单机离线可用
- 反馈只影响「新产生的缺陷」，不篡改历史缺陷包，保证证据可追溯
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

FEEDBACK_FILE = "feedback.json"
OVERRIDES_FILE = "triage_overrides.json"

LABEL_TRUE_POSITIVE = "true_positive"
LABEL_FALSE_POSITIVE = "false_positive"
VALID_LABELS = (LABEL_TRUE_POSITIVE, LABEL_FALSE_POSITIVE)

# 连续误报达到该次数后，同类缺陷的自动确认降级为自动预警
FALSE_POSITIVE_THRESHOLD = 2


# ─── 单条缺陷反馈 ───────────────────────────────────────────────


def feedback_path(bug_dir: str | Path) -> Path:
    return Path(bug_dir) / FEEDBACK_FILE


def save_feedback(
    bug_dir: str | Path,
    *,
    label: str,
    severity: str | None = None,
    note: str = "",
    reviewer: str = "",
) -> dict:
    """追加一条人工复核结论，返回完整反馈记录。"""
    if label not in VALID_LABELS:
        raise ValueError(f"label 必须是 {VALID_LABELS} 之一，收到: {label}")

    root = Path(bug_dir)
    root.mkdir(parents=True, exist_ok=True)
    data = load_feedback(root) or {"records": []}
    records = data.get("records")
    if not isinstance(records, list):
        records = []

    records.append(
        {
            "label": label,
            "severity": severity or "",
            "note": note or "",
            "reviewer": reviewer or "",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    data["records"] = records
    # 最新一条置顶，方便读取方直接取用
    data["label"] = label
    data["severity"] = severity or ""
    data["note"] = note or ""
    _write_json(feedback_path(root), data)
    return data


def load_feedback(bug_dir: str | Path) -> dict:
    return _read_json(feedback_path(bug_dir)) or {}


def latest_label(bug_dir: str | Path) -> str:
    return str(load_feedback(bug_dir).get("label", ""))


# ─── 项目级覆盖规则 ─────────────────────────────────────────────


def overrides_path(root: str | Path) -> Path:
    return Path(root) / OVERRIDES_FILE


def load_overrides(root: str | Path) -> dict:
    data = _read_json(overrides_path(root))
    if not data:
        return {"rules": {}}
    rules = data.get("rules")
    if not isinstance(rules, dict):
        data["rules"] = {}
    return data


def overrides_for(overrides: dict | None, anomaly_type: str) -> dict:
    """取出某个异常类型的覆盖规则；没有则返回空字典。"""
    if not overrides:
        return {}
    rule = (overrides.get("rules") or {}).get(anomaly_type)
    return dict(rule) if isinstance(rule, dict) else {}


def register_label(
    root: str | Path,
    *,
    anomaly_type: str,
    label: str,
    severity: str | None = None,
) -> dict:
    """把一次复核结论计入项目级规则，返回更新后的该类型规则。

    false_positive 连续计数，true_positive 会清零连续误报计数。
    """
    if label not in VALID_LABELS:
        raise ValueError(f"label 必须是 {VALID_LABELS} 之一，收到: {label}")

    data = load_overrides(root)
    rules = data.setdefault("rules", {})
    rule = rules.setdefault(str(anomaly_type), {})

    if label == LABEL_FALSE_POSITIVE:
        rule["fp_count"] = int(rule.get("fp_count", 0) or 0) + 1
        rule["tp_count"] = 0
    else:
        rule["tp_count"] = int(rule.get("tp_count", 0) or 0) + 1
        rule["fp_count"] = 0

    rule["last_label"] = label
    rule["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if severity:
        rule["severity"] = severity

    if int(rule.get("fp_count", 0) or 0) >= FALSE_POSITIVE_THRESHOLD:
        # 连续两次误报：不再允许「自动确认」
        rule["conclusion"] = "auto_warning"

    Path(root).mkdir(parents=True, exist_ok=True)
    _write_json(overrides_path(root), data)
    return dict(rule)


# ─── 缺陷包定位 ─────────────────────────────────────────────────


def resolve_bug_dir(value: str | Path, bugs_root: str | Path = "output/bugs") -> Path | None:
    """支持传缺陷包路径、BUG 目录名，或散落在 run 子目录下的缺陷包。"""
    target = Path(value)
    if target.is_dir() and (target / "defect.json").exists():
        return target.resolve()

    root = Path(bugs_root)
    candidates = [root / str(value)]
    if root.exists():
        candidates.extend(p for p in root.glob(f"*/{value}") if p.is_dir())
    for path in candidates:
        if path.is_dir() and (path / "defect.json").exists():
            return path.resolve()
    return None


# ─── 内部工具 ───────────────────────────────────────────────────


def _write_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


__all__ = [
    "FALSE_POSITIVE_THRESHOLD",
    "FEEDBACK_FILE",
    "LABEL_FALSE_POSITIVE",
    "LABEL_TRUE_POSITIVE",
    "OVERRIDES_FILE",
    "VALID_LABELS",
    "feedback_path",
    "latest_label",
    "load_feedback",
    "load_overrides",
    "overrides_for",
    "overrides_path",
    "register_label",
    "resolve_bug_dir",
    "save_feedback",
]
