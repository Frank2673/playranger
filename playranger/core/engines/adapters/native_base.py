"""原生平台适配器公共层。

设计原则（与其他适配器一致）：
1. **诚实**：能力不可用就返回 available=False + 原因，绝不伪造状态
2. **零硬依赖**：所有第三方库延迟导入，缺依赖只影响对应平台
3. **可审计**：每次动作都保留命令、耗时、原始输出摘要，便于回溯

支持矩阵（详见 docs/COMPATIBILITY_多端支持矩阵.md）：
- web        HTML5 / Canvas / WebGL（Playwright，全平台宿主）
- android    ADB + uiautomator（Windows/macOS/Linux 宿主 + 真机/模拟器）
- windows    Win32 API（仅 Windows 宿主）
- linux      X11 + xdotool（仅 Linux 宿主，Wayland 受限）
- ios        WebDriverAgent（仅 macOS 宿主）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from playranger.core.engines.adapters.base import AdapterUnavailable


# ─────────────────────────────────────────────────────────────
#  命令执行
# ─────────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    """一次外部命令的结构化结果（不吞异常、不改写退出码）。"""

    cmd: list[str]
    returncode: int
    stdout: str = ""
    stdout_bytes: bytes = b""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def brief(self, limit: int = 200) -> str:
        text = (self.stdout or "").strip() or (self.stderr or "").strip()
        text = text.replace("\n", " ")
        return text[:limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cmd": self.cmd,
            "returncode": self.returncode,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "brief": self.brief(),
        }


def run_command(
    cmd: Sequence[str],
    *,
    timeout: float = 20.0,
    binary: bool = False,
    cwd: str | None = None,
) -> CommandResult:
    """执行命令并结构化返回。找不到可执行文件时返回 returncode=127。"""
    started = time.time()
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
        out = proc.stdout
        err = proc.stderr
        if binary:
            return CommandResult(
                cmd=list(cmd),
                returncode=proc.returncode,
                stdout_bytes=out,
                stderr=err.decode("utf-8", errors="replace"),
                duration_ms=int((time.time() - started) * 1000),
            )
        return CommandResult(
            cmd=list(cmd),
            returncode=proc.returncode,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            duration_ms=int((time.time() - started) * 1000),
        )
    except FileNotFoundError:
        return CommandResult(
            cmd=list(cmd),
            returncode=127,
            stderr=f"可执行文件不存在: {cmd[0]}",
            duration_ms=int((time.time() - started) * 1000),
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            cmd=list(cmd),
            returncode=124,
            stderr=f"命令超时（{timeout}s）",
            duration_ms=int((time.time() - started) * 1000),
            timed_out=True,
        )
    except Exception as e:  # noqa: BLE001 — 适配器边界必须兜住
        return CommandResult(
            cmd=list(cmd),
            returncode=1,
            stderr=f"{type(e).__name__}: {e}",
            duration_ms=int((time.time() - started) * 1000),
        )


def which(name: str) -> str | None:
    return shutil.which(name)


# ─────────────────────────────────────────────────────────────
#  帧指纹（跨平台视觉回归的最小公共能力）
# ─────────────────────────────────────────────────────────────

def frame_signature(png_bytes: bytes) -> str:
    """PNG 字节的稳定指纹。

    注意：这是**内容哈希**，不是感知哈希。它只回答「画面是否逐字节相同」，
    不能回答「画面是否视觉相似」。需要后者时走 VLM 或 perceptual hash。
    """
    return hashlib.sha256(png_bytes).hexdigest()[:32]


def dhash(png_bytes: bytes, size: int = 8) -> str | None:
    """差值感知哈希（dHash）。需要 Pillow，缺失时返回 None（不伪造）。"""
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(BytesIO(png_bytes)).convert("L").resize(
            (size + 1, size), Image.Resampling.LANCZOS
        )
        pixels = list(img.getdata())
        bits = []
        for row in range(size):
            for col in range(size):
                left = pixels[row * (size + 1) + col]
                right = pixels[row * (size + 1) + col + 1]
                bits.append("1" if left > right else "0")
        return f"{int(''.join(bits), 2):016x}"
    except Exception:
        return None


def hamming_distance(a: str, b: str) -> int | None:
    """两个十六进制指纹的汉明距离。长度不一致时返回 None。"""
    if not a or not b or len(a) != len(b):
        return None
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────
#  原生适配器基类
# ─────────────────────────────────────────────────────────────

NATIVE_ACTION_TYPES = (
    "tap",           # 点按 (x, y)
    "swipe",         # 滑动 (x1, y1, x2, y2, duration_ms)
    "click",         # 桌面点击（同 tap，语义区分）
    "keypress",      # 按键 (key)
    "text",          # 输入文本
    "wait",          # 等待
    "back",          # 返回键（Android/iOS）
    "home",          # 回主屏
    "restart",       # 重启被测应用
)


@dataclass
class NativeState:
    """原生平台的一次状态快照。"""

    platform: str
    scene: str = "unknown"
    screenshot_png: bytes | None = None
    frame_sig: str = ""
    ui_tree_summary: dict[str, Any] = field(default_factory=dict)
    available_actions: list[str] = field(default_factory=lambda: list(NATIVE_ACTION_TYPES))
    extra: dict[str, Any] = field(default_factory=dict)

    def raw(self) -> dict[str, Any]:
        """转成与 WebAdapter.get_state() 同构的 dict（便于引擎复用）。"""
        return {
            "platform": self.platform,
            "scene": self.scene,
            "frame_sig": self.frame_sig,
            "available_actions": self.available_actions,
            "ui_tree_summary": self.ui_tree_summary,
            **self.extra,
        }


class NativeAdapterBase:
    """原生平台适配器基类：统一能力上报与状态结构，不实现具体平台命令。"""

    name: str = "native"
    platform_id: str = "unknown"

    # 子类必须覆盖：可用性探测
    def probe(self) -> dict[str, Any]:
        raise NotImplementedError

    def capabilities(self) -> dict:
        info = self.probe()
        info.setdefault("adapter", self.name)
        info.setdefault("platform", self.platform_id)
        info.setdefault("actions", list(NATIVE_ACTION_TYPES))
        info.setdefault("deterministic", False)
        info.setdefault("evidential", True)
        return info

    def _ensure_available(self) -> None:
        info = self.probe()
        if not info.get("available"):
            raise AdapterUnavailable(info.get("reason") or f"{self.name} 不可用")

    # 子类必须覆盖
    def get_state(self) -> dict:
        raise NotImplementedError

    def get_actions(self) -> list:
        return self.get_state().get("available_actions", [])

    def execute(
        self,
        action_type: str,
        target: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    # 供引擎使用的截图落盘
    def screenshot(self, path: str) -> str:
        state = self.get_state()
        png = state.get("_screenshot_png")
        if not png:
            raise AdapterUnavailable(f"{self.name} 当前无法获取截图")
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(png)
        return path


def save_png(png_bytes: bytes, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(png_bytes)
    return path


def summarize_ui_nodes(nodes: list[dict[str, Any]], limit: int = 40) -> dict[str, Any]:
    """把 UI 节点列表压缩成可进 prompt 的摘要（避免上万节点直接进上下文）。"""
    clickable = [n for n in nodes if n.get("clickable")]
    texts = [n.get("text", "") for n in nodes if n.get("text")]
    return {
        "node_count": len(nodes),
        "clickable_count": len(clickable),
        "sample_clickable": clickable[:limit],
        "sample_texts": texts[:limit],
    }


__all__ = [
    "CommandResult",
    "NATIVE_ACTION_TYPES",
    "NativeAdapterBase",
    "NativeState",
    "dhash",
    "frame_signature",
    "hamming_distance",
    "run_command",
    "save_png",
    "summarize_ui_nodes",
    "which",
]
