"""修复沙箱 —— patch 校验、副本应用、可选测试命令。

边界：
- 只允许操作 PLAYRANGER_WORKSPACE_ROOTS 白名单内的目录（默认当前目录与项目根）；
- 原始源码目录只做 `git apply --check`，绝不直接写入；
- 真正应用只发生在 output/fix_sandbox/<id>/ 的副本里；
- 测试命令可选，默认不执行；执行时限定工作目录为沙箱副本并有超时。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_COPY_FILES = 20_000
MAX_COPY_BYTES = 800 * 1024 * 1024
MAX_OUTPUT_CHARS = 8_000
DEFAULT_TEST_TIMEOUT_S = 120
IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "output",
    ".playwright-browsers",
    "playranger.egg-info",
    "dist",
    "build",
    ".idea",
    ".vscode",
}


class SandboxError(ValueError):
    """输入/路径不合法（不是 patch 应用失败）。"""


def allowed_roots() -> list[Path]:
    raw = os.environ.get("PLAYRANGER_WORKSPACE_ROOTS", "").strip()
    roots: list[Path] = []
    if raw:
        for item in raw.replace(";", os.pathsep).split(os.pathsep):
            item = item.strip().strip('"')
            if item:
                roots.append(Path(item))
    if not roots:
        roots.append(Path.cwd())
        roots.append(Path(__file__).resolve().parents[2])  # 项目根
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:
            continue
    return resolved


def resolve_root(raw_root: str) -> Path:
    if not raw_root or not str(raw_root).strip():
        raise SandboxError("source_root 不能为空")
    root = Path(str(raw_root).strip().strip('"')).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise SandboxError(f"源码目录不存在：{raw_root}") from exc
    if not root.is_dir():
        raise SandboxError(f"源码路径不是目录：{raw_root}")
    allowed = allowed_roots()
    if not any(root == base or root.is_relative_to(base) for base in allowed):
        raise SandboxError(
            "源码目录不在白名单内；请设置 PLAYRANGER_WORKSPACE_ROOTS（分号分隔）"
            f"；当前允许：{'; '.join(str(p) for p in allowed)}"
        )
    return root


def _truncate(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n…（已截断，共 {len(text)} 字符）"


def _patch_files(patch: str) -> list[str]:
    files: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.append(parts[3][2:] if parts[3].startswith("b/") else parts[3])
        elif line.startswith("+++ b/"):
            name = line[6:].strip()
            if name and name != "/dev/null":
                files.append(name)
    seen: list[str] = []
    for name in files:
        if name not in seen:
            seen.append(name)
    return seen


def _run(cmd: list[str] | str, cwd: Path, timeout_s: int, shell: bool = False) -> dict:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            shell=shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        return {
            "command": cmd if isinstance(cmd, str) else " ".join(cmd),
            "returncode": proc.returncode,
            "stdout": _truncate(proc.stdout),
            "stderr": _truncate(proc.stderr),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd if isinstance(cmd, str) else " ".join(cmd),
            "returncode": -1,
            "stdout": _truncate((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            "stderr": _truncate(f"命令超时（>{timeout_s}s）"),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "timed_out": True,
        }
    except OSError as exc:
        return {
            "command": cmd if isinstance(cmd, str) else " ".join(cmd),
            "returncode": -2,
            "stdout": "",
            "stderr": _truncate(f"命令无法执行：{exc}"),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "timed_out": False,
        }


def _git_available() -> bool:
    try:
        proc = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=10
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _safe_rel(name: str) -> str | None:
    """补丁里的路径必须是 root 内的相对路径。"""
    candidate = name.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
        return None
    parts = [p for p in candidate.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts) or None


def validate_patch(raw_root: str, patch: str) -> dict:
    """校验 patch 可否应用。

    统一在临时 git 仓库里检查：复制 patch 涉及的源文件，再执行
    `git apply --check`。这样既不会写入原目录，也不受「source_root
    恰好位于某个无关 git 仓库子目录」的影响（git 会静默跳过）。
    """
    if not patch or not patch.strip():
        raise SandboxError("patch 不能为空")
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise SandboxError("patch 超过 2MB 限制")
    root = resolve_root(raw_root)
    files = _patch_files(patch)
    rel_files: list[str] = []
    for name in files:
        safe = _safe_rel(name)
        if safe is None:
            raise SandboxError(f"patch 包含非法路径：{name}")
        rel_files.append(safe)
    if not _git_available():
        return {
            "ok": False,
            "root": str(root),
            "files": rel_files,
            "git_available": False,
            "check": None,
            "message": "未找到 git，无法执行 git apply --check",
        }

    with tempfile.TemporaryDirectory(prefix="playranger_check_") as tmp:
        temp_root = Path(tmp)
        _run(["git", "init", "-q"], cwd=temp_root, timeout_s=30)
        for rel in rel_files:
            source = root / rel
            if source.is_file():
                target = temp_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        patch_file = temp_root / ".playranger_change.patch"
        patch_file.write_text(patch, encoding="utf-8")
        result = _run(
            ["git", "apply", "--check", "--whitespace=nowarn", str(patch_file)],
            cwd=temp_root,
            timeout_s=60,
        )
    return {
        "ok": result["returncode"] == 0,
        "root": str(root),
        "files": rel_files,
        "git_available": True,
        "mode": "temp_repo",
        "check": result,
        "message": "patch 可应用" if result["returncode"] == 0 else "patch 无法应用",
    }


def _copy_tree(src: Path, dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=False)
    count = 0
    total = 0
    for base, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        rel = Path(base).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            source = Path(base) / name
            try:
                size = source.stat().st_size
            except OSError:
                continue
            count += 1
            total += size
            if count > MAX_COPY_FILES:
                raise SandboxError(f"源码目录文件数超过 {MAX_COPY_FILES}，请缩小 source_root")
            if total > MAX_COPY_BYTES:
                raise SandboxError(
                    f"源码目录超过 {MAX_COPY_BYTES // 1024 // 1024}MB，请缩小 source_root"
                )
            try:
                shutil.copy2(source, target_dir / name)
            except OSError:
                continue
    return count


def sandbox_root() -> Path:
    raw = os.environ.get("PLAYRANGER_SANDBOX_ROOT", "").strip() or "output/fix_sandbox"
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def verify_patch(
    raw_root: str,
    patch: str,
    test_command: str = "",
    timeout_s: int = DEFAULT_TEST_TIMEOUT_S,
) -> dict:
    """在沙箱副本里应用 patch，可选执行测试命令。"""
    check = validate_patch(raw_root, patch)
    if not check["ok"]:
        return {
            "ok": False,
            "stage": "validate",
            "applied": False,
            "validate": check,
            "test": None,
            "sandbox_dir": "",
            "message": check["message"],
        }

    root = Path(check["root"])
    sandbox_base = sandbox_root()
    sandbox = sandbox_base / f"{uuid.uuid4().hex[:10]}"
    sandbox_base.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    try:
        copied = _copy_tree(root, sandbox)
    except SandboxError:
        shutil.rmtree(sandbox, ignore_errors=True)
        raise

    patch_file = sandbox / ".playranger_change.patch"
    patch_file.write_text(patch, encoding="utf-8")
    # 副本目录默认没有 git 仓库；git apply 在非仓库目录会静默跳过
    _run(["git", "init", "-q"], cwd=sandbox, timeout_s=30)
    apply_result = _run(
        ["git", "apply", "--whitespace=nowarn", str(patch_file)],
        cwd=sandbox,
        timeout_s=120,
    )
    applied = apply_result["returncode"] == 0

    test_result = None
    if applied and (test_command or "").strip():
        timeout = max(5, min(int(timeout_s or DEFAULT_TEST_TIMEOUT_S), 600))
        test_result = _run(test_command.strip(), cwd=sandbox, timeout_s=timeout, shell=True)

    return {
        "ok": applied and (test_result is None or test_result["returncode"] == 0),
        "stage": "test" if test_result is not None else "apply",
        "applied": applied,
        "validate": check,
        "apply": apply_result,
        "test": test_result,
        "sandbox_dir": str(sandbox),
        "copied_files": copied,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "message": (
            "patch 已应用"
            + ("，测试通过" if test_result and test_result["returncode"] == 0 else "")
            if applied
            else "patch 应用失败"
        ),
    }
