"""Bug 记录器 —— 发现异常后生成可执行缺陷包。

每个 bug 一个独立文件夹，结构：
    BUG-001/
    ├── defect.json            # 元数据（ID / 标题 / 类型 / 严重度 / 时间...）
    ├── initial_state.json     # 初始状态
    ├── action_trace.jsonl     # 动作轨迹（每步一行 JSON）
    ├── assertion.json         # 失败断言（复现检测用）
    ├── screenshots/           # 关键截图（初始 / 失败 / 关键帧）
    │   ├── initial.png
    │   └── failure.png
    ├── seed.json              # 随机种子
    └── game_version.json      # 游戏版本信息

遵循 P0 规范：
- 自修正不抹缺陷：恢复成功也保留原始失败记录
- 结论三档：auto_confirmed / auto_warning / human_required
- 证据状态纪律：每个字段都要标记证据来源
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playranger.core.agent.evidence import (
    EvidenceChain,
    KIND_FAILURE,
    KIND_INITIAL,
)
from playranger.core.agent.feedback import load_overrides, overrides_for
from playranger.core.agent.intel import save_intel
from playranger.core.agent.triage import triage as run_triage


# ─── 数据结构 ───────────────────────────────────────────────────

CONCLUSION_LEVELS = ("auto_confirmed", "auto_warning", "human_required")
EVIDENCE_SOURCES = ("sdk_state", "vision_vlm", "action_trace", "game_log", "human_annotated")


@dataclass
class DefectRecord:
    """一个缺陷的完整记录。"""

    # 基本信息
    bug_id: str = ""
    title: str = ""
    anomaly_type: str = ""        # 见 vision_engine.ANOMALY_TYPES + sdk_state / logic_error
    severity: str = "info"        # info / minor / major / critical / blocker
    conclusion: str = "human_required"  # auto_confirmed / auto_warning / human_required
    description: str = ""

    # 证据
    evidence: dict[str, str] = field(default_factory=dict)
    # 例: {"hp_not_reset": "sdk_state", "black_screen": "vision_vlm"}

    # 环境
    game_name: str = ""
    game_version: str = ""
    seed: int = 0
    initial_state: dict = field(default_factory=dict)
    final_state: dict = field(default_factory=dict)

    # 轨迹
    action_trace: list[dict] = field(default_factory=list)  # 每步一条记录

    # 断言（复现用）
    assertion: dict = field(default_factory=dict)
    # 例: {"type": "state_equals", "field": "hp", "value": 30}

    # 元数据
    discovered_at: str = ""
    reproduction_rate: float = 0.0
    reproduction_attempts: int = 0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ─── 记录器实现 ─────────────────────────────────────────────────

class BugRecorder:
    """Bug 快照记录器 —— 每个 bug 生成一个独立文件夹。

    用法：
        recorder = BugRecorder(output_dir="bugs")
        bug_dir = recorder.capture(
            state={...},
            seed=42,
            action_trace=[...],
            anomaly_type="hp_not_reset",
            severity="major",
            title="重新开始后 HP 未重置",
            screenshot_path="fail.png",
        )
    """

    def __init__(self, output_dir: str = "bugs", triage_config: dict | None = None) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        self._bugs: list[DefectRecord] = []
        # P2-3：项目级分级配置（未传时按当前目录自动查找）
        if triage_config is None:
            from playranger.core.agent.triage_config import load_triage_config

            config_obj = load_triage_config()
            triage_config = config_obj.to_dict()
        self.triage_config = triage_config

    # ─── 公共 API ────────────────────────────────────────────

    def capture(
        self,
        *,
        title: str,
        anomaly_type: str,
        severity: str,
        description: str = "",
        conclusion: str = "human_required",
        seed: int = 0,
        initial_state: dict,
        final_state: dict,
        action_trace: list[dict],
        assertion: dict | None = None,
        screenshot_initial: str | None = None,
        screenshot_failure: str | None = None,
        game_name: str = "",
        game_version: str = "",
        evidence: dict[str, str] | None = None,
        tags: list[str] | None = None,
        consecutive_failures: int = 0,
        auto_triage: bool = True,
        source: dict | None = None,
        started_at: str = "",
    ) -> str:
        """记录一个 bug，返回缺陷包文件夹路径。

        所有关键字参数都必填（除了带默认值的）。

        auto_triage=True 时，若调用方未给出实质判定（severity=info 且
        conclusion=human_required），会用规则引擎自动定级；显式传入的
        判定始终优先。分级结果额外写入 defect.json 的 triage 字段。

        source 用于标记缺陷来源（检测通道 / 触发步骤 / 连续失败次数等），
        会原样写入 defect.json，并用于生成 provenance.json 与 fix_plan.json。
        """
        self._counter += 1
        bug_id = f"BUG-{self._counter:03d}"

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ── 自动分级 ──────────────────────────────────────────
        triage_dict: dict | None = None
        if auto_triage:
            # 项目级人工反馈规则：连续误报的类型会被降级 / 覆盖
            override = overrides_for(load_overrides(self.output_dir), anomaly_type)
            triage_result = run_triage(
                anomaly_type,
                evidence=evidence,
                consecutive_failures=consecutive_failures,
                override=override,
                config=self.triage_config,
            )
            triage_dict = triage_result.to_dict()

            # 仅当调用方未给出实质判定时才采用自动判定
            if severity == "info" and triage_result.severity != "info":
                severity = triage_result.severity
            if conclusion == "human_required" and triage_result.conclusion != "human_required":
                conclusion = triage_result.conclusion

        record = DefectRecord(
            bug_id=bug_id,
            title=title,
            anomaly_type=anomaly_type,
            severity=severity,
            conclusion=conclusion,
            description=description,
            evidence=evidence or {},
            game_name=game_name,
            game_version=game_version,
            seed=seed,
            initial_state=initial_state,
            final_state=final_state,
            action_trace=action_trace,
            assertion=assertion or {},
            discovered_at=now,
            tags=tags or [],
        )

        # 创建文件夹
        bug_dir = self.output_dir / bug_id
        bug_dir.mkdir(parents=True, exist_ok=True)

        # 1. defect.json —— 元数据（不含轨迹详情，避免重复）
        defect_meta = {
            "bug_id": bug_id,
            "title": title,
            "anomaly_type": anomaly_type,
            "severity": severity,
            "conclusion": conclusion,
            "description": description,
            "evidence": record.evidence,
            "game_name": game_name,
            "game_version": game_version,
            "seed": seed,
            "discovered_at": now,
            "started_at": started_at,
            "reproduction_rate": record.reproduction_rate,
            "reproduction_attempts": record.reproduction_attempts,
            "action_count": len(action_trace),
            "tags": record.tags,
            "source": source or {},
        }
        if triage_dict is not None:
            defect_meta["triage"] = triage_dict
        _write_json(bug_dir / "defect.json", defect_meta)

        # 2. initial_state.json
        _write_json(bug_dir / "initial_state.json", initial_state)

        # 3. final_state.json
        _write_json(bug_dir / "final_state.json", final_state)

        # 4. action_trace.jsonl
        trace_path = bug_dir / "action_trace.jsonl"
        with open(trace_path, "w", encoding="utf-8") as f:
            for step in action_trace:
                f.write(json.dumps(step, ensure_ascii=False) + "\n")

        # 5. assertion.json
        if assertion:
            _write_json(bug_dir / "assertion.json", assertion)

        # 6. seed.json
        _write_json(bug_dir / "seed.json", {"seed": seed})

        # 7. game_version.json
        _write_json(bug_dir / "game_version.json", {
            "name": game_name,
            "version": game_version,
        })

        # 8. screenshots/ + 证据链
        shots_dir = bug_dir / "screenshots"
        shots_dir.mkdir(exist_ok=True)
        chain = EvidenceChain(bug_dir)
        if screenshot_initial:
            if _safe_copy(screenshot_initial, shots_dir / "initial.png"):
                chain.add("screenshots/initial.png", kind=KIND_INITIAL)
        if screenshot_failure:
            if _safe_copy(screenshot_failure, shots_dir / "failure.png"):
                chain.add(
                    "screenshots/failure.png",
                    kind=KIND_FAILURE,
                    step=len(action_trace),
                )
        chain.save()

        # 9. provenance.json + fix_plan.json —— 溯源与修复方案
        #    分析失败不能影响缺陷包本身，因此异常在内部消化。
        bug_payload = {**defect_meta, **record.to_dict()}
        intel = save_intel(bug_payload, bug_dir)
        if intel.get("saved"):
            from playranger.core.agent.intel import provenance_summary

            defect_meta["provenance_summary"] = provenance_summary(intel["provenance"])
            defect_meta["fix_plan_summary"] = {
                "effort": intel["fix_plan"].get("effort", ""),
                "regression_risk": intel["fix_plan"].get("regression_risk", ""),
                "confidence": intel["fix_plan"].get("confidence", 0.0),
                "root_cause_count": len(intel["fix_plan"].get("root_causes", [])),
            }
            _write_json(bug_dir / "defect.json", defect_meta)

        self._bugs.append(record)
        return str(bug_dir.resolve())

    def summary(self) -> list[dict]:
        """返回所有已记录 bug 的摘要列表（给控制台用）。"""
        return [
            {
                "bug_id": b.bug_id,
                "title": b.title,
                "anomaly_type": b.anomaly_type,
                "severity": b.severity,
                "conclusion": b.conclusion,
                "reproduction_rate": b.reproduction_rate,
                "action_count": len(b.action_trace),
            }
            for b in self._bugs
        ]

    def load(self, bug_dir: str) -> DefectRecord:
        """从缺陷包目录加载一个 DefectRecord。"""
        return load_defect(bug_dir)

    @property
    def count(self) -> int:
        return len(self._bugs)


# ─── 工具函数 ───────────────────────────────────────────────────

def load_defect(bug_dir: str | Path) -> DefectRecord:
    """从缺陷包目录加载 DefectRecord（模块级函数，API / CLI 可直接调用）。"""
    path = Path(bug_dir)

    defect = _read_json(path / "defect.json", {})
    initial = _read_json(path / "initial_state.json", {})
    final = _read_json(path / "final_state.json", {})
    seed_data = _read_json(path / "seed.json", {"seed": 0})
    assertion = _read_json(path / "assertion.json", {})
    version = _read_json(path / "game_version.json", {})

    # 读 jsonl 轨迹
    trace: list[dict] = []
    trace_path = path / "action_trace.jsonl"
    if trace_path.exists():
        with open(trace_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    trace.append(json.loads(line))

    return DefectRecord(
        bug_id=defect.get("bug_id", path.name),
        title=defect.get("title", ""),
        anomaly_type=defect.get("anomaly_type", ""),
        severity=defect.get("severity", "info"),
        conclusion=defect.get("conclusion", "human_required"),
        description=defect.get("description", ""),
        evidence=defect.get("evidence", {}),
        game_name=version.get("name", ""),
        game_version=version.get("version", ""),
        seed=seed_data.get("seed", 0),
        initial_state=initial,
        final_state=final,
        action_trace=trace,
        assertion=assertion,
        discovered_at=defect.get("discovered_at", ""),
        reproduction_rate=defect.get("reproduction_rate", 0.0),
        reproduction_attempts=defect.get("reproduction_attempts", 0),
        tags=defect.get("tags", []),
    )

def _write_json(path: Path, data: Any) -> None:
    """写 JSON 文件（UTF-8 + 缩进 2）。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_json(path: Path, default: Any) -> Any:
    """读 JSON 文件，不存在返回 default。"""
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_copy(src: str, dst: Path) -> bool:
    """安全复制文件，源不存在就跳过。返回是否复制成功。"""
    src_p = Path(src)
    if src_p.exists():
        shutil.copy2(src_p, dst)
        return True
    return False
