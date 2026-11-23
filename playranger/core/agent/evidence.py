"""Bug 截图证据链 —— 为缺陷包中的截图建立可校验的哈希链。

解决的问题：
- 缺陷包里的截图谁都能改，无法证明「这张图确实是测试时截的」
- 前后帧之间的顺序、对应的动作没有结构化记录

做法：
- 每张截图记录 sha256 + 文件大小 + 采集时间 + 对应步骤/动作
- 每条记录额外计算链式哈希：chain_hash = sha256(prev_chain | file_sha256 | kind | step | action | captured_at)
- 根哈希写入 evidence_chain.json
- verify() 重算全链，任何文件被替换、条目被删除/插入都会断链

文件结构（在缺陷包目录下）：
    evidence_chain.json
    {
      "version": 1,
      "created_at": "...",
      "entries": [ {...}, ... ],
      "root_hash": "..."
    }
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

EVIDENCE_CHAIN_FILE = "evidence_chain.json"
CHAIN_VERSION = 1

# 截图角色
KIND_INITIAL = "initial"    # 初始画面
KIND_BEFORE = "before"      # 动作执行前
KIND_AFTER = "after"        # 动作执行后
KIND_FAILURE = "failure"    # 失败瞬间
KIND_FINAL = "final"        # 结束画面
VALID_KINDS = (KIND_INITIAL, KIND_BEFORE, KIND_AFTER, KIND_FAILURE, KIND_FINAL)


@dataclass
class EvidenceEntry:
    """一条证据记录。"""

    index: int
    kind: str
    file: str                 # 相对缺陷包目录的路径
    sha256: str
    size: int
    captured_at: str
    step: int | None = None
    action: str | None = None
    prev_hash: str = ""
    chain_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerifyResult:
    """证据链校验结果。"""

    ok: bool
    checked: int = 0
    broken_index: int | None = None
    message: str = ""
    missing_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "broken_index": self.broken_index,
            "message": self.message,
            "missing_files": self.missing_files,
        }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _chain_hash(prev_hash: str, file_hash: str, kind: str, step: int | None,
                action: str | None, captured_at: str) -> str:
    raw = f"{prev_hash}|{file_hash}|{kind}|{step}|{action}|{captured_at}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EvidenceChain:
    """缺陷包的截图证据链构建器。

    用法：
        chain = EvidenceChain(bug_dir)
        chain.add("screenshots/initial.png", kind="initial")
        chain.add("screenshots/failure.png", kind="failure", step=12, action="click")
        chain.save()

        ok, msg = EvidenceChain.verify(bug_dir)
    """

    def __init__(self, bug_dir: str | Path) -> None:
        self.bug_dir = Path(bug_dir)
        self.entries: list[EvidenceEntry] = []
        self.skipped: list[str] = []

    # ─── 构建 ────────────────────────────────────────────────

    def add(
        self,
        file_path: str | Path,
        *,
        kind: str,
        step: int | None = None,
        action: str | None = None,
    ) -> EvidenceEntry | None:
        """加入一张截图。文件不存在时跳过并记录，不抛异常。"""
        target = self._resolve(file_path)
        if not target.exists():
            self.skipped.append(str(file_path))
            return None

        if kind not in VALID_KINDS:
            kind = KIND_AFTER

        file_sha = _sha256_file(target)
        prev_hash = self.entries[-1].chain_hash if self.entries else ""
        captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        chain_hash = _chain_hash(prev_hash, file_sha, kind, step, action, captured_at)

        entry = EvidenceEntry(
            index=len(self.entries),
            kind=kind,
            file=self._relative(target),
            sha256=file_sha,
            size=target.stat().st_size,
            captured_at=captured_at,
            step=step,
            action=action,
            prev_hash=prev_hash,
            chain_hash=chain_hash,
        )
        self.entries.append(entry)
        return entry

    def save(self) -> str | None:
        """写入 evidence_chain.json，返回文件路径；无条目时不写。"""
        if not self.entries:
            return None

        payload = {
            "version": CHAIN_VERSION,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entries": [e.to_dict() for e in self.entries],
            "root_hash": self.entries[-1].chain_hash,
        }
        path = self.bug_dir / EVIDENCE_CHAIN_FILE
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return str(path)

    # ─── 校验 ────────────────────────────────────────────────

    @staticmethod
    def verify(bug_dir: str | Path) -> VerifyResult:
        """校验缺陷包的证据链完整性。

        - 文件被替换 → sha256 不匹配 → 断链
        - 条目被删除 / 顺序被改 → 链式哈希不匹配 → 断链
        """
        root = Path(bug_dir)
        chain_file = root / EVIDENCE_CHAIN_FILE
        if not chain_file.exists():
            return VerifyResult(ok=False, message="evidence_chain.json 不存在")

        try:
            with open(chain_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            return VerifyResult(ok=False, message=f"证据链文件读取失败: {e}")

        entries = payload.get("entries", [])
        if not entries:
            return VerifyResult(ok=False, message="证据链为空")

        prev_hash = ""
        missing: list[str] = []

        for i, raw in enumerate(entries):
            file_rel = raw.get("file", "")
            abs_path = root / file_rel

            if not abs_path.exists():
                missing.append(file_rel)
                return VerifyResult(
                    ok=False,
                    checked=i + 1,
                    broken_index=i,
                    message=f"第 {i} 条证据文件缺失: {file_rel}",
                    missing_files=missing,
                )

            actual_sha = _sha256_file(abs_path)
            if actual_sha != raw.get("sha256"):
                return VerifyResult(
                    ok=False,
                    checked=i + 1,
                    broken_index=i,
                    message=f"第 {i} 条证据文件哈希不匹配（可能被篡改）: {file_rel}",
                    missing_files=missing,
                )

            expected_chain = _chain_hash(
                prev_hash,
                raw.get("sha256", ""),
                raw.get("kind", ""),
                raw.get("step"),
                raw.get("action"),
                raw.get("captured_at", ""),
            )
            if expected_chain != raw.get("chain_hash"):
                return VerifyResult(
                    ok=False,
                    checked=i + 1,
                    broken_index=i,
                    message=f"第 {i} 条证据链哈希不匹配（条目可能被改动或顺序被调整）",
                    missing_files=missing,
                )

            prev_hash = raw.get("chain_hash", "")

        if prev_hash != payload.get("root_hash"):
            return VerifyResult(
                ok=False,
                checked=len(entries),
                broken_index=len(entries) - 1,
                message="根哈希与链尾不一致",
            )

        return VerifyResult(
            ok=True,
            checked=len(entries),
            message=f"证据链完整，共 {len(entries)} 条记录",
        )

    # ─── 内部 ────────────────────────────────────────────────

    def _resolve(self, p: str | Path) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (self.bug_dir / path)

    def _relative(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.bug_dir)).replace(os.sep, "/")
        except ValueError:
            return str(p)
