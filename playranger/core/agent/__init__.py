"""Agent 层：Replayer + ReAct 循环 + 缺陷记录 + 自动分级 + 证据链 + 长期记忆。"""

from playranger.core.agent.replayer import (
    ReplayResult,
    Replayer,
    ReproductionReport,
    build_assertion_from_dict,
)
from playranger.core.agent.memory import (
    AgentMemory,
    MemoryEntry,
    extract_signature,
    similarity,
)
from playranger.core.agent.triage import (
    SEVERITY_ORDER,
    TriageResult,
    severity_score,
    summarize_bugs,
    triage,
)
from playranger.core.agent.evidence import (
    EVIDENCE_CHAIN_FILE,
    EvidenceChain,
    EvidenceEntry,
    VerifyResult,
)

__all__ = [
    # 回放
    "ReplayResult",
    "Replayer",
    "ReproductionReport",
    "build_assertion_from_dict",
    # 记忆
    "AgentMemory",
    "MemoryEntry",
    "extract_signature",
    "similarity",
    # 分级
    "SEVERITY_ORDER",
    "TriageResult",
    "triage",
    "severity_score",
    "summarize_bugs",
    # 证据链
    "EVIDENCE_CHAIN_FILE",
    "EvidenceChain",
    "EvidenceEntry",
    "VerifyResult",
]
