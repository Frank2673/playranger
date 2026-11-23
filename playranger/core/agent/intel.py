"""缺陷智能分析聚合层 —— 一次调用同时产出「溯源」与「修复方案」。

为什么单独一层：
- provenance 只负责「从哪来」，advisor 只负责「怎么修」
- recorder / API / CLI 都需要「两者一起要」，避免各处重复拼装
- 落盘文件命名集中在这里，避免多处硬编码
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from playranger.core.agent.advisor import FixPlan, advise
from playranger.core.agent.provenance import BugProvenance, build_provenance

PROVENANCE_FILE = "provenance.json"
FIX_PLAN_FILE = "fix_plan.json"


def analyze_bug(
    bug: dict,
    *,
    bug_dir: str | Path | None = None,
    source_root: str | Path | None = None,
) -> dict:
    """分析一条缺陷，返回 {provenance, fix_plan} 两个字典。"""
    prov = build_provenance(bug, bug_dir=bug_dir)
    plan = advise(bug, prov, source_root=str(source_root) if source_root else None)
    return {
        "provenance": prov.to_dict(),
        "fix_plan": plan.to_dict(),
    }


def analyze_objects(
    bug: dict,
    *,
    bug_dir: str | Path | None = None,
    source_root: str | Path | None = None,
) -> tuple[BugProvenance, FixPlan]:
    """与 analyze_bug 相同，但返回对象（供需要 markdown 的调用方使用）。"""
    prov = build_provenance(bug, bug_dir=bug_dir)
    plan = advise(bug, prov, source_root=str(source_root) if source_root else None)
    return prov, plan


def save_intel(bug: dict, bug_dir: str | Path) -> dict:
    """把溯源与修复方案写入缺陷包，返回分析结果。

    写盘失败不抛异常（测试主流程不能因为分析失败而中断），
    但会把错误信息放进返回值，便于上层排查。
    """
    source_root = bug.get("source_root") or os.environ.get("PLAYRANGER_SOURCE_ROOT") or None
    intel = analyze_bug(bug, bug_dir=bug_dir, source_root=source_root)

    try:
        root = Path(bug_dir)
        root.mkdir(parents=True, exist_ok=True)
        _write_json(root / PROVENANCE_FILE, intel["provenance"])
        _write_json(root / FIX_PLAN_FILE, intel["fix_plan"])
        intel["saved"] = True
    except OSError as e:
        intel["saved"] = False
        intel["error"] = f"分析结果写盘失败: {e}"

    return intel


def provenance_summary(provenance: dict) -> dict:
    """从溯源结果提炼摘要，供 defect.json / API 列表页使用。"""
    origin_action = provenance.get("origin_action") or {}
    return {
        "origin_step": provenance.get("origin_step"),
        "origin_action": origin_action.get("action_type", ""),
        "origin_target": origin_action.get("target"),
        "channels": list(provenance.get("channels", [])),
        "state_diff_count": len(provenance.get("state_diff", [])),
        "replay_steps": len(provenance.get("replay_actions", [])),
        "screenshot_count": len(provenance.get("screenshots", [])),
        "has_assertion": bool(provenance.get("assertion_check")),
    }


def _write_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


__all__ = [
    "FIX_PLAN_FILE",
    "PROVENANCE_FILE",
    "analyze_bug",
    "analyze_objects",
    "provenance_summary",
    "save_intel",
]
