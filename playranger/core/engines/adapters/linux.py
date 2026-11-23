"""Linux 适配器 —— 基于 X11 + xdotool 的真实实现。

能力边界（诚实声明）：
- ✅ 能读：整屏 / 指定窗口截图（ImageMagick `import`、`scrot`、`grim`(Wayland) 或 Pillow）
- ✅ 能做：鼠标移动/点击/拖拽、按键、文本输入
- ⚠️ **仅 X11 可靠**；原生 Wayland 会拒绝 xdotool 注入（probe 会明确警告）
- ⚠️ 无 UI 控件树（X11 不提供跨工具包的可访问性树）→ 一律「截图 + 视觉」模式
- ❌ 不做：XTest 之外的注入、内核级操作

宿主要求：Linux 桌面 + `xdotool`；截图工具三选一（ImageMagick / scrot / grim）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

from playranger.core.engines.adapters.base import AdapterUnavailable
from playranger.core.engines.adapters.native_base import (
    NATIVE_ACTION_TYPES,
    NativeAdapterBase,
    NativeState,
    frame_signature,
    run_command,
    which,
)

_SCREENSHOT_TOOLS = ("import", "scrot", "grim")


class LinuxAdapter(NativeAdapterBase):
    """X11 窗口级游戏适配器。"""

    name = "linux"
    platform_id = "linux"

    def __init__(
        self,
        title: str | None = None,
        *,
        exe_path: str | None = None,
        display: str | None = None,
        backend: str | None = None,
    ) -> None:
        self.title = title or os.environ.get("PLAYRANGER_WINDOW_TITLE") or None
        self.exe_path = exe_path or os.environ.get("PLAYRANGER_GAME_EXE") or None
        self.display = display or os.environ.get("DISPLAY") or None
        self.backend = backend or os.environ.get("PLAYRANGER_LINUX_BACKEND") or None
        self._window_id: str | None = None
        self._last_png: bytes | None = None
        self._last_sig: str = ""

    # ── 探测 ──────────────────────────────────────────────────

    def probe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"available": False, "adapter": self.name}
        if sys.platform != "linux":
            info["reason"] = f"Linux 适配器仅支持 Linux 宿主，当前为 {sys.platform}"
            return info

        session = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
        wayland = session == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))
        info["session_type"] = session or "unknown"

        if not self.display and not wayland:
            info["reason"] = "未检测到 DISPLAY / WAYLAND_DISPLAY，请在图形会话中运行"
            return info

        xdotool = which("xdotool")
        info["xdotool"] = xdotool
        if not xdotool:
            info["reason"] = "缺少 xdotool：sudo apt install xdotool"
            return info

        if wayland and not self.display:
            info["reason"] = (
                "检测到原生 Wayland 会话且无 XWayland DISPLAY。"
                "xdotool 无法注入 Wayland 原生窗口；请改用 XWayland 运行游戏，"
                "或改用带 Web 导出的构建。"
            )
            return info

        shot_tool = next((t for t in _SCREENSHOT_TOOLS if which(t)), None)
        pil_ok = _pillow_ok()
        info["screenshot_backend"] = shot_tool or ("Pillow" if pil_ok else None)
        if not shot_tool and not pil_ok:
            info["reason"] = (
                "缺少截图工具：安装 ImageMagick(import) / scrot / grim 之一，或 pip install pillow"
            )
            return info

        if self.title:
            wid = self._search_window(self.title)
            if not wid:
                info["reason"] = f"未找到标题匹配 {self.title!r} 的窗口"
                return info
            self._window_id = wid
            info["window_id"] = wid

        info.update(
            {
                "available": True,
                "window_title": self.title,
                "ui_tree": False,
                "note": "X11 无跨工具包控件树，使用截图 + 视觉判定",
            }
        )
        if wayland:
            info["warning"] = "Wayland 会话：仅能操作 XWayland 窗口，原生 Wayland 窗口会失败"
        return info

    def _search_window(self, title: str) -> str | None:
        res = run_command(["xdotool", "search", "--name", title], timeout=8)
        if not res.ok or not res.stdout.strip():
            return None
        ids = res.stdout.split()
        return ids[-1] if ids else None

    # ── 截图 ──────────────────────────────────────────────────

    def _capture(self) -> bytes | None:
        tool = next((t for t in _SCREENSHOT_TOOLS if which(t)), None)
        if tool == "import":
            target = self._window_id or "root"
            res = run_command(
                ["import", "-window", target, "png:-"], timeout=20, binary=True
            )
            if res.ok and res.stdout_bytes.startswith(b"\x89PNG"):
                return res.stdout_bytes
        elif tool == "scrot":
            res = run_command(["scrot", "-o", "-"], timeout=20, binary=True)
            if res.ok and res.stdout_bytes.startswith(b"\x89PNG"):
                return res.stdout_bytes
        elif tool == "grim":
            res = run_command(["grim", "-"], timeout=20, binary=True)
            if res.ok and res.stdout_bytes.startswith(b"\x89PNG"):
                return res.stdout_bytes

        # 兜底：Pillow（X11）
        if _pillow_ok():
            try:
                import io

                from PIL import ImageGrab

                img = ImageGrab.grab()
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return buf.getvalue()
            except Exception:
                return None
        return None

    # ── 状态 ──────────────────────────────────────────────────

    def get_state(self) -> dict:
        self._ensure_available()
        png = self._capture()
        sig = frame_signature(png) if png else ""
        changed = sig != self._last_sig if self._last_sig else True
        self._last_png = png
        self._last_sig = sig

        state = NativeState(
            platform=self.platform_id,
            scene="native_render",
            screenshot_png=png,
            frame_sig=sig,
            ui_tree_summary={
                "ui_tree_available": False,
                "node_count": 0,
                "note": "X11 无控件树，需依赖截图 + VLM",
            },
            available_actions=list(NATIVE_ACTION_TYPES),
            extra={"frame_changed": changed, "window_id": self._window_id},
        )
        raw = state.raw()
        raw["_screenshot_png"] = png
        raw["_nodes"] = []
        return raw

    # ── 动作 ──────────────────────────────────────────────────

    def execute(
        self,
        action_type: str,
        target: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict:
        self._ensure_available()
        params = params or {}
        started = time.time()
        action = (action_type or "").lower()
        res = None

        try:
            if action in ("tap", "click"):
                if "x" not in params or "y" not in params:
                    raise ValueError("X11 无控件树，click/tap 必须提供 x/y 屏幕坐标")
                res = run_command(
                    ["xdotool", "mousemove", "--sync",
                     str(int(params["x"])), str(int(params["y"])), "click", "1"],
                    timeout=10,
                )

            elif action == "swipe":
                x1, y1 = int(params["x1"]), int(params["y1"])
                x2, y2 = int(params["x2"]), int(params["y2"])
                dur = int(params.get("duration_ms", 300))
                steps = max(6, dur // 20)
                run_command(["xdotool", "mousemove", "--sync", str(x1), str(y1)], timeout=10)
                run_command(["xdotool", "mousedown", "1"], timeout=10)
                for i in range(1, steps + 1):
                    run_command(
                        ["xdotool", "mousemove", "--sync",
                         str(int(x1 + (x2 - x1) * i / steps)),
                         str(int(y1 + (y2 - y1) * i / steps))],
                        timeout=5,
                    )
                    time.sleep(dur / 1000.0 / steps)
                res = run_command(["xdotool", "mouseup", "1"], timeout=10)

            elif action in ("keypress", "key"):
                key = str(params.get("key") or target or "")
                if not key:
                    raise ValueError("keypress 需要 key")
                res = run_command(["xdotool", "key", "--clearmodifiers", key], timeout=10)

            elif action == "text":
                text = str(params.get("text") or target or "")
                if not text:
                    raise ValueError("text 需要非空文本")
                res = run_command(["xdotool", "type", "--delay", "20", text], timeout=30)

            elif action == "wait":
                ms = int(params.get("ms", params.get("duration_ms", 500)))
                time.sleep(max(0, ms) / 1000.0)

            elif action == "back":
                res = run_command(["xdotool", "key", "Escape"], timeout=10)

            elif action == "home":
                raise ValueError("Linux 适配器无 home 动作；请用 restart")

            elif action == "restart":
                self.reset()

            else:
                raise ValueError(
                    f"Linux 适配器不支持动作 {action!r}；可用: {', '.join(NATIVE_ACTION_TYPES)}"
                )

        except (ValueError, KeyError, AdapterUnavailable) as e:
            return {
                "ok": False,
                "error": str(e),
                "state": None,
                "duration_ms": int((time.time() - started) * 1000),
                "command": None,
            }

        ok = True if res is None else res.ok
        return {
            "ok": ok,
            "error": None if ok else (res.brief() if res else "未知错误"),
            "state": None,
            "duration_ms": int((time.time() - started) * 1000),
            "command": res.to_dict() if res else None,
        }

    # ── 重置 ──────────────────────────────────────────────────

    def reset(self) -> None:
        if not self.exe_path:
            raise AdapterUnavailable("重启游戏需要 exe_path（或设置 PLAYRANGER_GAME_EXE）")
        name = os.path.basename(self.exe_path)
        run_command(["pkill", "-f", name], timeout=10)
        time.sleep(0.6)
        try:
            subprocess.Popen(
                [self.exe_path],
                cwd=os.path.dirname(self.exe_path) or None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:  # noqa: BLE001
            raise AdapterUnavailable(f"启动 {self.exe_path} 失败: {e}") from e
        time.sleep(1.5)
        if self.title:
            self._window_id = self._search_window(self.title)

    def screenshot(self, path: str) -> str:
        png = self._last_png or self._capture()
        if not png:
            raise AdapterUnavailable("X11 截图失败")
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(png)
        return path


def _pillow_ok() -> bool:
    try:
        from PIL import ImageGrab  # noqa: F401

        return True
    except Exception:
        return False


__all__ = ["LinuxAdapter"]
