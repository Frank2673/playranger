"""Agent 记忆系统 —— 跨测试轮次的长期经验积累。

三层记忆：
- 工作记忆：当前一轮的 history（已存在于 ReActLoop）
- 情景记忆：本模块，记录「在什么状态下发现了什么缺陷 / 在哪里卡住过」
- 语义记忆：由情景记忆聚合出的通用提示（build_context 生成给 Planner）

关键设计：不做向量库，用状态签名 + 加权相似度做召回，离线可跑、零依赖。

文件：output/memory/agent_memory.json
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

MEMORY_VERSION = 1
DEFAULT_MEMORY_PATH = "output/memory/agent_memory.json"

# P3-2：容量与时间衰减（近似语义层，不是神经向量）
DEFAULT_CAPACITY = 500
DEFAULT_HALF_LIFE_DAYS = 30.0

KIND_BUG = "bug"
KIND_FAILURE = "failure"   # 卡住/无效路径，不等于缺陷
KIND_SUCCESS = "success"   # 有效推进路径，可复用

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-zA-Z0-9_]+")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class MemoryEntry:
    """一条情景记忆。"""

    entry_id: str
    kind: str
    anomaly_type: str
    title: str
    severity: str
    signature: dict = field(default_factory=dict)   # scene/level/hp/score 等状态特征
    action_sequence: list = field(default_factory=list)
    hint: str = ""
    count: int = 1
    first_seen: str = ""
    last_seen: str = ""
    bug_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        return cls(
            entry_id=d.get("entry_id", ""),
            kind=d.get("kind", KIND_BUG),
            anomaly_type=d.get("anomaly_type", ""),
            title=d.get("title", ""),
            severity=d.get("severity", "info"),
            signature=d.get("signature", {}),
            action_sequence=list(d.get("action_sequence", [])),
            hint=d.get("hint", ""),
            count=d.get("count", 1),
            first_seen=d.get("first_seen", ""),
            last_seen=d.get("last_seen", ""),
            bug_ids=list(d.get("bug_ids", [])),
        )


def extract_signature(state: dict) -> dict:
    """从游戏状态中提取用于相似度比较的签名。"""
    return {
        "scene": str(state.get("scene", "")),
        "level": state.get("level"),
        "hp": state.get("hp"),
        "score": state.get("score"),
    }


def similarity(a: dict, b: dict) -> float:
    """签名相似度 0~1，加权计算。"""
    score = 0.0
    if a.get("scene") and a.get("scene") == b.get("scene"):
        score += 0.45
    if a.get("level") is not None and a.get("level") == b.get("level"):
        score += 0.25

    hp_a, hp_b = a.get("hp"), b.get("hp")
    if hp_a is not None and hp_b is not None:
        try:
            diff = abs(float(hp_a) - float(hp_b))
            if diff == 0:
                score += 0.15
            elif diff <= 20:
                score += 0.08
        except (TypeError, ValueError):
            pass

    sc_a, sc_b = a.get("score"), b.get("score")
    if sc_a is not None and sc_b is not None:
        try:
            # 同一数量级就算相似
            if max(abs(float(sc_a)), abs(float(sc_b))) > 0:
                if abs(float(sc_a) - float(sc_b)) <= max(50.0, abs(float(sc_a)) * 0.2):
                    score += 0.15
        except (TypeError, ValueError):
            pass

    return min(1.0, score)


# ─── 近似语义层（TF-IDF + 字符 n-gram，零依赖）──────────────────


def tokenize(text: str) -> list[str]:
    """英文按词、中文按 1+2-gram 切分，避免引入分词依赖。"""
    text = (text or "").lower()
    tokens = _WORD.findall(text)
    cjk = "".join(_CJK.findall(text))
    tokens.extend(cjk)
    tokens.extend(cjk[i:i + 2] for i in range(max(0, len(cjk) - 1)))
    return [t for t in tokens if t]


def _term_freq(tokens: list[str]) -> dict[str, float]:
    freq: dict[str, float] = {}
    for token in tokens:
        freq[token] = freq.get(token, 0.0) + 1.0
    total = float(len(tokens)) or 1.0
    return {k: v / total for k, v in freq.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    dot = sum(a[k] * b[k] for k in keys)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def entry_text(entry: "MemoryEntry") -> str:
    """把一条记忆压成可比较的文本（标题 + 类型 + 提示 + 场景）。"""
    parts = [entry.title, entry.anomaly_type, entry.hint, str(entry.signature.get("scene", ""))]
    return " ".join(p for p in parts if p)


def state_text(state: dict) -> str:
    """当前状态里可用于语义比较的部分（只取描述性字段）。"""
    parts = [str(state.get("scene", "")), str(state.get("mode", ""))]
    return " ".join(p for p in parts if p)


def semantic_scores(query: str, docs: list[str]) -> list[float]:
    """对一组文档计算 TF-IDF 余弦相似度。"""
    query_tokens = tokenize(query)
    if not query_tokens or not docs:
        return [0.0] * len(docs)

    doc_tokens = [tokenize(d) for d in docs]
    doc_count = len(doc_tokens)
    df: dict[str, int] = {}
    for tokens in doc_tokens:
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1
    idf = {token: math.log((1 + doc_count) / (1 + n)) + 1.0 for token, n in df.items()}

    def vector(tokens: list[str]) -> dict[str, float]:
        tf = _term_freq(tokens)
        return {k: v * idf.get(k, math.log(1 + doc_count) + 1.0) for k, v in tf.items()}

    query_vec = vector(query_tokens)
    return [_cosine(query_vec, vector(tokens)) for tokens in doc_tokens]


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def decay_factor(last_seen: str, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """按最后命中时间做半衰期衰减，返回 0~1。"""
    seen = _parse_time(last_seen)
    if seen is None or half_life_days <= 0:
        return 1.0
    days = max(0.0, (datetime.now(timezone.utc) - seen).total_seconds() / 86400.0)
    return 0.5 ** (days / half_life_days)


class AgentMemory:
    """跨轮次记忆库。

    用法：
        mem = AgentMemory()
        mem.remember_bug(bug_dict, state=state, action_sequence=history, hint="...")
        mem.save()

        # 下一轮测试开始时
        ctx = mem.build_context(current_state)
        # ctx 是一段可直接放进 Planner prompt 的中文提示；为空表示无相关记忆
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        auto_load: bool = True,
        capacity: int = DEFAULT_CAPACITY,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    ) -> None:
        self.path = Path(path or os.environ.get("PLAYRANGER_MEMORY_PATH", DEFAULT_MEMORY_PATH))
        self.entries: list[MemoryEntry] = []
        self.capacity = max(1, int(capacity))
        self.half_life_days = max(0.0, float(half_life_days))
        if auto_load:
            self.load()

    # ─── 写入 ────────────────────────────────────────────────

    def remember_bug(
        self,
        bug: dict,
        *,
        state: dict | None = None,
        action_sequence: list | None = None,
        hint: str = "",
    ) -> MemoryEntry:
        """记住一个缺陷。相同签名 + 同类型会合并计数。"""
        anomaly_type = bug.get("anomaly_type", "unknown")
        signature = extract_signature(state or bug.get("initial_state", {}) or {})
        entry = self._find_mergeable(KIND_BUG, anomaly_type, signature)

        bug_id = bug.get("bug_id", "")
        if entry is None:
            entry = MemoryEntry(
                entry_id=f"{KIND_BUG}-{anomaly_type}-{len(self.entries)}",
                kind=KIND_BUG,
                anomaly_type=anomaly_type,
                title=bug.get("title", ""),
                severity=bug.get("severity", "info"),
                signature=signature,
                action_sequence=self._slim_sequence(action_sequence or []),
                hint=hint or bug.get("description", ""),
                first_seen=_now(),
                last_seen=_now(),
                bug_ids=[bug_id] if bug_id else [],
            )
            self.entries.append(entry)
        else:
            entry.count += 1
            entry.last_seen = _now()
            if bug_id and bug_id not in entry.bug_ids:
                entry.bug_ids.append(bug_id)

        return entry

    def remember_failure(
        self,
        state: dict,
        failure_reason: str,
        action_sequence: list | None = None,
        hint: str = "",
    ) -> MemoryEntry:
        """记住一次卡住 / 无效路径，下次遇到相似状态优先避开。"""
        signature = extract_signature(state)
        entry = self._find_mergeable(KIND_FAILURE, "strategy_stuck", signature)

        if entry is None:
            entry = MemoryEntry(
                entry_id=f"{KIND_FAILURE}-{len(self.entries)}",
                kind=KIND_FAILURE,
                anomaly_type="strategy_stuck",
                title=(failure_reason or "无效路径")[:80],
                severity="minor",
                signature=signature,
                action_sequence=self._slim_sequence(action_sequence or []),
                hint=hint or failure_reason,
                first_seen=_now(),
                last_seen=_now(),
            )
            self.entries.append(entry)
        else:
            entry.count += 1
            entry.last_seen = _now()

        return entry

    def remember_success(
        self,
        state: dict,
        action_sequence: list | None = None,
        hint: str = "",
    ) -> None:
        """记住一次有效推进，作为可复用路径。"""
        signature = extract_signature(state)
        entry = self._find_mergeable(KIND_SUCCESS, "progress", signature)
        if entry is None:
            self.entries.append(MemoryEntry(
                entry_id=f"{KIND_SUCCESS}-{len(self.entries)}",
                kind=KIND_SUCCESS,
                anomaly_type="progress",
                title="有效推进路径",
                severity="info",
                signature=signature,
                action_sequence=self._slim_sequence(action_sequence or []),
                hint=hint,
                first_seen=_now(),
                last_seen=_now(),
            ))
        else:
            entry.count += 1
            entry.last_seen = _now()

    # ─── 召回 ────────────────────────────────────────────────

    def recall(
        self,
        state: dict,
        *,
        threshold: float = 0.35,
        limit: int = 5,
        kinds: tuple[str, ...] | None = None,
        semantic: bool = False,
    ) -> list[tuple[MemoryEntry, float]]:
        """按当前状态召回相似记忆，按相似度倒序。

        semantic=True 时启用 TF-IDF 近似语义召回（跨游戏/不同场景名也能命中），
        签名相似度始终作为兜底；默认关闭以保持既有行为的确定性。
        """
        signature = extract_signature(state)
        pool = [e for e in self.entries if not kinds or e.kind in kinds]
        if not pool:
            return []

        sem_scores = (
            semantic_scores(state_text(state), [entry_text(e) for e in pool])
            if semantic else [0.0] * len(pool)
        )

        scored: list[tuple[MemoryEntry, float, float]] = []
        for entry, sem in zip(pool, sem_scores):
            sig = similarity(signature, entry.signature)
            score = sig
            if semantic:
                score = min(1.0, 0.65 * sig + 0.35 * sem)
                # 语义高度相似时允许越过签名不匹配（跨游戏迁移）
                if sem >= 0.55:
                    score = max(score, 0.8 * sem)
            if score >= threshold:
                weight = entry.count * (0.5 + 0.5 * decay_factor(entry.last_seen, self.half_life_days))
                scored.append((entry, score, weight))

        scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return [(entry, score) for entry, score, _ in scored[:limit]]

    def build_context(self, state: dict, *, limit: int = 3) -> str:
        """生成给 Planner 的记忆提示。无相关记忆时返回空字符串。"""
        hits = self.recall(state, limit=limit, semantic=True)
        if not hits:
            return ""

        lines: list[str] = []
        for entry, sim in hits:
            if entry.kind == KIND_BUG:
                lines.append(
                    f"- 在类似状态（{self._describe(entry.signature)}）发现过缺陷 "
                    f"[{entry.severity}] {entry.title}（命中 {entry.count} 次）。"
                    f"建议：{entry.hint or '重点验证该路径'}"
                )
            elif entry.kind == KIND_FAILURE:
                lines.append(
                    f"- 在类似状态（{self._describe(entry.signature)}）曾卡住："
                    f"{entry.title}（{entry.count} 次）。请更换策略，避免重复无效动作。"
                )
            else:
                lines.append(
                    f"- 类似状态（{self._describe(entry.signature)}）下有有效推进路径，"
                    f"可优先复用（见动作序列）。"
                )

        if not lines:
            return ""
        return "历史测试经验（来自以往的缺陷与卡点）：\n" + "\n".join(lines)

    # ─── 持久化 ──────────────────────────────────────────────

    def save(self) -> str:
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": MEMORY_VERSION,
            "updated_at": _now(),
            "capacity": self.capacity,
            "half_life_days": self.half_life_days,
            "entries": [e.to_dict() for e in self.entries],
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return str(self.path)

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        entries = payload.get("entries", [])
        self.entries = [MemoryEntry.from_dict(d) for d in entries]

    def clear(self) -> None:
        self.entries = []

    def stats(self) -> dict:
        by_kind: dict[str, int] = {}
        for e in self.entries:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        vocab = set()
        for e in self.entries:
            vocab.update(tokenize(entry_text(e)))
        return {
            "path": str(self.path),
            "total_entries": len(self.entries),
            "by_kind": by_kind,
            "total_hits": sum(e.count for e in self.entries),
            "capacity": self.capacity,
            "half_life_days": self.half_life_days,
            "semantic_vocab": len(vocab),
            "semantic_mode": "tfidf_ngram_approximation",
        }

    # ─── 内部 ────────────────────────────────────────────────

    def _prune(self) -> None:
        """超出容量时按「命中次数 × 时间衰减」淘汰，避免记忆文件无限膨胀。"""
        if len(self.entries) <= self.capacity:
            return
        self.entries.sort(
            key=lambda e: e.count * (0.5 + 0.5 * decay_factor(e.last_seen, self.half_life_days)),
            reverse=True,
        )
        self.entries = self.entries[: self.capacity]

    def _find_mergeable(self, kind: str, anomaly_type: str, signature: dict) -> MemoryEntry | None:
        for e in self.entries:
            if e.kind != kind or e.anomaly_type != anomaly_type:
                continue
            if similarity(signature, e.signature) >= 0.8:
                return e
        return None

    @staticmethod
    def _slim_sequence(seq: list, keep: int = 10) -> list:
        """只保留最近的关键动作，避免记忆文件膨胀。"""
        slim = []
        for item in seq[-keep:]:
            if isinstance(item, dict):
                slim.append({
                    "action": item.get("action_type") or item.get("action") or "",
                    "params": item.get("action_params") or item.get("params") or {},
                })
            else:
                slim.append(item)
        return slim

    @staticmethod
    def _describe(sig: dict) -> str:
        parts = []
        if sig.get("scene"):
            parts.append(f"场景={sig['scene']}")
        if sig.get("level") is not None:
            parts.append(f"关卡={sig['level']}")
        if sig.get("hp") is not None:
            parts.append(f"HP={sig['hp']}")
        if sig.get("score") is not None:
            parts.append(f"分数={sig['score']}")
        return "、".join(parts) or "未知状态"
