"""PlayRanger SDK 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Health:
    """健康状态。"""
    status: str
    version: str


@dataclass
class Capabilities:
    """系统能力。"""
    version: str
    llm_available: bool
    vlm_available: bool
    browser_available: bool
    supported_bug_types: list[str] = field(default_factory=list)
    max_concurrent_tests: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> "Capabilities":
        return cls(
            version=d.get("version", ""),
            llm_available=d.get("llm_available", False),
            vlm_available=d.get("vlm_available", False),
            browser_available=d.get("browser_available", False),
            supported_bug_types=list(d.get("supported_bug_types", [])),
            max_concurrent_tests=d.get("max_concurrent_tests", 1),
        )


@dataclass
class TestInfo:
    """测试信息。"""
    test_id: str
    status: str  # pending / running / completed / failed
    goal: str
    game_url: str
    started_at: float
    updated_at: float
    current_step: int = 0
    total_steps: int = 0
    bugs_found: int = 0
    conclusion: str = "human_required"
    success: bool = False
    error: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "TestInfo":
        return cls(
            test_id=d["test_id"],
            status=d["status"],
            goal=d["goal"],
            game_url=d["game_url"],
            started_at=d.get("started_at", 0),
            updated_at=d.get("updated_at", 0),
            current_step=d.get("current_step", 0),
            total_steps=d.get("total_steps", 0),
            bugs_found=d.get("bugs_found", 0),
            conclusion=d.get("conclusion", "human_required"),
            success=d.get("success", False),
            error=d.get("error"),
        )

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def is_done(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")


@dataclass
class StepRecord:
    """步骤记录。"""
    step_index: int
    action_type: str
    action_params: dict = field(default_factory=dict)
    state_before: dict = field(default_factory=dict)
    state_after: dict = field(default_factory=dict)
    success: bool = True
    error: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "StepRecord":
        return cls(
            step_index=d.get("step_index", 0),
            action_type=d.get("action_type", ""),
            action_params=d.get("action_params", {}),
            state_before=d.get("state_before", {}),
            state_after=d.get("state_after", {}),
            success=d.get("success", True),
            error=d.get("error"),
        )


@dataclass
class StepList:
    """步骤列表。"""
    test_id: str
    total: int
    steps: list[StepRecord]

    @classmethod
    def from_dict(cls, d: dict) -> "StepList":
        return cls(
            test_id=d["test_id"],
            total=d.get("total", 0),
            steps=[StepRecord.from_dict(s) for s in d.get("steps", [])],
        )


@dataclass
class BugSummary:
    """Bug 摘要。"""
    bug_id: str
    title: str
    anomaly_type: str
    severity: str
    conclusion: str
    bug_dir: str | None = None
    discovered_at: float = 0
    test_id: str | None = None
    triage: dict = field(default_factory=dict)
    evidence_chain: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    fix_plan: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "BugSummary":
        return cls(
            bug_id=d["bug_id"],
            title=d["title"],
            anomaly_type=d["anomaly_type"],
            severity=d["severity"],
            conclusion=d["conclusion"],
            bug_dir=d.get("bug_dir"),
            discovered_at=d.get("discovered_at", 0),
            test_id=d.get("test_id"),
            triage=d.get("triage") or {},
            evidence_chain=d.get("evidence_chain") or {},
            provenance=d.get("provenance") or {},
            fix_plan=d.get("fix_plan") or {},
        )


@dataclass
class BugDetail:
    """Bug 详情。"""
    bug_id: str
    title: str
    anomaly_type: str
    severity: str
    description: str = ""
    conclusion: str = "human_required"
    seed: int = 0
    initial_state: dict = field(default_factory=dict)
    final_state: dict = field(default_factory=dict)
    assertion: dict = field(default_factory=dict)
    action_trace_count: int = 0
    action_trace: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    bug_dir: str | None = None
    discovered_at: float = 0
    test_id: str | None = None
    triage: dict = field(default_factory=dict)
    evidence_chain: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    fix_plan: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "BugDetail":
        return cls(
            bug_id=d["bug_id"],
            title=d["title"],
            anomaly_type=d["anomaly_type"],
            severity=d["severity"],
            description=d.get("description", ""),
            conclusion=d.get("conclusion", "human_required"),
            seed=d.get("seed", 0),
            initial_state=d.get("initial_state", {}),
            final_state=d.get("final_state", {}),
            assertion=d.get("assertion", {}),
            action_trace_count=d.get("action_trace_count", 0),
            action_trace=list(d.get("action_trace", [])),
            evidence=d.get("evidence", {}),
            tags=list(d.get("tags", [])),
            bug_dir=d.get("bug_dir"),
            discovered_at=d.get("discovered_at", 0),
            test_id=d.get("test_id"),
            triage=d.get("triage") or {},
            evidence_chain=d.get("evidence_chain") or {},
            provenance=d.get("provenance") or {},
            fix_plan=d.get("fix_plan") or {},
        )

    @property
    def is_auto_confirmed(self) -> bool:
        """是否已由自动判定确认为缺陷。"""
        return self.conclusion == "auto_confirmed"

    @property
    def evidence_verified(self) -> bool:
        """截图证据链是否校验通过。"""
        return bool(self.evidence_chain.get("ok", False))


@dataclass
class BugList:
    """Bug 列表。"""
    total: int
    bugs: list[BugSummary]

    @classmethod
    def from_dict(cls, d: dict) -> "BugList":
        return cls(
            total=d.get("total", 0),
            bugs=[BugSummary.from_dict(b) for b in d.get("bugs", [])],
        )


@dataclass
class ReplayResult:
    """复现结果。"""
    bug_id: str
    runs: int
    reproduced_count: int
    reproduction_rate: float
    is_reproducible: bool

    @classmethod
    def from_dict(cls, d: dict) -> "ReplayResult":
        return cls(
            bug_id=d["bug_id"],
            runs=d["runs"],
            reproduced_count=d["reproduced_count"],
            reproduction_rate=d["reproduction_rate"],
            is_reproducible=d["is_reproducible"],
        )

    @property
    def rate_pct(self) -> str:
        return f"{self.reproduction_rate * 100:.1f}%"
