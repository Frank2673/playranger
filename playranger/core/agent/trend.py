"""跨版本趋势对比 —— 把每轮试玩结果落盘成快照，并与基线版本做差分。

为什么需要：
- 单次报告只能回答「这次有几个缺陷」，回答不了「比上个版本好转了吗」
- 修完一轮再跑一轮，最需要看的是：新增了什么、修掉了什么、还遗留什么

设计要点：
- 快照就是一份 summary.json，复用 report.build_metrics 的口径，避免两套统计
- 缺陷匹配不依赖 bug_id（每轮重新编号），用「异常类型 + 标题」做指纹
- 全部为纯本地 JSON 读写，不引入任何存储依赖
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

SNAPSHOT_FILE = "summary.json"
DEFAULT_VERSIONS_DIR = "output/versions"

_SEV_ORDER = ("info", "minor", "major", "critical", "blocker")


# ─── 指纹 ───────────────────────────────────────────────────────


def bug_key(bug: dict) -> str:
    """缺陷指纹：异常类型 + 归一化标题。

    bug_id 每轮都会重排（BUG-001 可能是另一个问题），不能作为跨版本主键。
    """
    anomaly = str(bug.get("anomaly_type") or "unknown").strip().lower()
    title = " ".join(str(bug.get("title") or "").split()).lower()
    if title:
        return f"{anomaly}::{title}"
    return f"{anomaly}::id::{bug.get('bug_id', '')}"


def _index_bugs(bugs: list[dict]) -> dict[str, dict]:
    """按指纹建索引；同一指纹重复出现时保留最严重的一条。"""
    index: dict[str, dict] = {}
    for bug in bugs:
        key = bug_key(bug)
        prev = index.get(key)
        if prev is None or _sev_index(bug.get("severity")) > _sev_index(prev.get("severity")):
            index[key] = bug
    return index


# ─── 快照 ───────────────────────────────────────────────────────


def build_snapshot(
    bugs: list[dict],
    *,
    version: str,
    game_name: str = "",
    metrics: dict | None = None,
    generated_at: str | None = None,
) -> dict:
    """把一轮缺陷结果压成可落盘的版本快照。"""
    if metrics is None:
        # 延迟导入，避免 report ↔ trend 的循环依赖
        from playranger.core.agent.report import build_metrics

        metrics = build_metrics(bugs)

    ordered = sorted(
        bugs,
        key=lambda b: (-_sev_index(b.get("severity")), str(b.get("bug_id", ""))),
    )
    return {
        "version": str(version),
        "generated_at": generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "game_name": game_name or "",
        "metrics": metrics,
        "bugs": [
            {
                "key": bug_key(b),
                "bug_id": b.get("bug_id", ""),
                "title": b.get("title", ""),
                "anomaly_type": b.get("anomaly_type", ""),
                "severity": b.get("severity", "info"),
                "conclusion": b.get("conclusion", "human_required"),
            }
            for b in ordered
        ],
    }


def _safe_segment(name: str) -> str:
    """把版本号压成安全目录名，保留点 / 横线 / 下划线。"""
    text = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "-", str(name)).strip("-")
    return text or "unversioned"


def snapshot_path(root: str | Path, version: str) -> Path:
    return Path(root) / _safe_segment(version) / SNAPSHOT_FILE


def save_snapshot(snapshot: dict, root: str | Path = DEFAULT_VERSIONS_DIR) -> str:
    """写入 versions/<version>/summary.json，返回绝对路径。"""
    target = snapshot_path(root, snapshot["version"])
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return str(target.resolve())


def load_snapshot(root: str | Path, version: str) -> dict | None:
    path = snapshot_path(root, version)
    return _read_json(path) or None


def load_history(root: str | Path = DEFAULT_VERSIONS_DIR) -> list[dict]:
    """读取全部历史快照，按生成时间升序。"""
    base = Path(root)
    if not base.exists():
        return []
    snapshots: list[dict] = []
    for path in sorted(base.glob(f"*/{SNAPSHOT_FILE}")):
        data = _read_json(path)
        if data and data.get("version"):
            snapshots.append(data)
    snapshots.sort(key=lambda s: str(s.get("generated_at", "")))
    return snapshots


# ─── 差分 ───────────────────────────────────────────────────────


def compare(current: dict, baseline: dict) -> dict:
    """当前快照对比基线快照，产出新增 / 修复 / 遗留 / 等级变化。"""
    cur_index = _index_bugs(current.get("bugs", []))
    base_index = _index_bugs(baseline.get("bugs", []))

    added = [cur_index[k] for k in cur_index if k not in base_index]
    fixed = [base_index[k] for k in base_index if k not in cur_index]
    persisted = [cur_index[k] for k in cur_index if k in base_index]

    changed: list[dict] = []
    for key in cur_index:
        if key not in base_index:
            continue
        cur_sev = cur_index[key].get("severity", "info")
        base_sev = base_index[key].get("severity", "info")
        if cur_sev != base_sev:
            changed.append(
                {
                    "title": cur_index[key].get("title", ""),
                    "anomaly_type": cur_index[key].get("anomaly_type", ""),
                    "from_severity": base_sev,
                    "to_severity": cur_sev,
                }
            )

    cur_metrics = current.get("metrics") or {}
    base_metrics = baseline.get("metrics") or {}
    metric_delta = {
        name: {
            "current": cur_metrics.get(name, 0),
            "baseline": base_metrics.get(name, 0),
            "delta": _round(cur_metrics.get(name, 0)) - _round(base_metrics.get(name, 0)),
        }
        for name in (
            "total_bugs",
            "severity_score",
            "auto_rate",
            "avg_confidence",
            "evidence_rate",
        )
    }

    return {
        "version": current.get("version", ""),
        "baseline_version": baseline.get("version", ""),
        "generated_at": current.get("generated_at", ""),
        "baseline_generated_at": baseline.get("generated_at", ""),
        "counts": {
            "added": len(added),
            "fixed": len(fixed),
            "persisted": len(persisted),
            "changed": len(changed),
        },
        "added": [_brief(b) for b in added],
        "fixed": [_brief(b) for b in fixed],
        "changed": changed,
        "metrics": metric_delta,
    }


def _brief(bug: dict) -> dict:
    return {
        "bug_id": bug.get("bug_id", ""),
        "title": bug.get("title", ""),
        "anomaly_type": bug.get("anomaly_type", ""),
        "severity": bug.get("severity", "info"),
    }


# ─── 内部工具 ───────────────────────────────────────────────────


def _sev_index(sev: str | None) -> int:
    try:
        return _SEV_ORDER.index(str(sev))
    except ValueError:
        return 0


def _round(value) -> float:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0


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
    "DEFAULT_VERSIONS_DIR",
    "SNAPSHOT_FILE",
    "bug_key",
    "build_snapshot",
    "compare",
    "load_history",
    "load_snapshot",
    "save_snapshot",
    "snapshot_path",
]
