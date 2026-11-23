"""Windows 适配器 —— 基于 Win32 API 的真实实现（无需第三方依赖即可动作）。

能力边界（诚实声明）：
- ✅ 能读：客户区截图（Pillow ImageGrab）、窗口枚举与标题匹配
- ✅ 能做：鼠标点击/拖拽、按键、文本输入（剪贴板粘贴）、重启进程
- ⚠️ UI 元素树：标准 Win32/WPF/UWP 控件在装了 `uiautomation` 时可读；
  Unity / Unreal / 自绘 DirectX 游戏通常为空 → 自动降级为「截图 + 视觉」
- ⚠️ 截图要求窗口在前台（后台/最小化时 ImageGrab 可能截到别的窗口）
- ❌ 不做：注入 DLL、绕过反作弊、内核级操作

宿主要求：Windows 10/11。仅在 Windows 上可用，其他系统 probe() 返回不可用。
"""

from __future__ import annotations

import ctypes
import io
import os
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
    summarize_ui_nodes,
)

# ── Win32 常量 ────────────────────────────────────────────────
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_KEYEVENTF_KEYUP = 0x0002
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

_VK = {
    "enter": 0x0D,
    "return": 0x0D,
    "escape": 0x1B,
    "esc": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


class WindowsAdapter(NativeAdapterBase):
    """Win32 窗口级游戏适配器。"""

    name = "windows"
    platform_id = "windows"

    def __init__(
        self,
        title: str | None = None,
        *,
        exe_path: str | None = None,
        title_regex: str | None = None,
        activate_on_action: bool = True,
    ) -> None:
        self.title = title or os.environ.get("PLAYRANGER_WINDOW_TITLE") or None
        self.title_regex = title_regex or os.environ.get("PLAYRANGER_WINDOW_REGEX") or None
        self.exe_path = exe_path or os.environ.get("PLAYRANGER_GAME_EXE") or None
        self.activate_on_action = activate_on_action
        self._hwnd: int | None = None
        self._last_png: bytes | None = None
        self._last_sig: str = ""

    # ── 可用性探测 ────────────────────────────────────────────

    def probe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"available": False, "adapter": self.name}
        if sys.platform != "win32":
            info["reason"] = f"Windows 适配器仅支持 Windows 宿主，当前为 {sys.platform}"
            return info

        try:
            import PIL  # noqa: F401
        except ImportError:
            info["reason"] = "缺少 Pillow（截图依赖）：pip install pillow"
            return info

        info["uia_available"] = _uia_available()
        info["uia_note"] = (
            "uiautomation 已安装，可读标准控件树"
            if info["uia_available"]
            else "未安装 uiautomation，仅截图模式（对 Unity/Unreal 无影响）"
        )

        hwnd = self._find_window()
        if hwnd is None:
            candidates = [t for _, t in _enum_windows()][:15]
            info["reason"] = (
                "未找到目标窗口。请传 title= 或设置 PLAYRANGER_WINDOW_TITLE。"
                f" 当前可见窗口: {candidates}"
            )
            return info

        self._hwnd = hwnd
        rect = _client_rect_screen(hwnd)
        info.update(
            {
                "available": True,
                "hwnd": hwnd,
                "window_title": _window_title(hwnd),
                "client_rect": rect,
                "exe_path": self.exe_path,
            }
        )
        return info

    def _find_window(self) -> int | None:
        """按标题找窗口；同名窗口有多个时取**客户区面积最大**的那个。

        原因：很多应用会创建同名的隐藏小窗口（托盘/浮层），
        如果取第一个会截到 32×32 的空白位图。
        """
        candidates: list[tuple[int, str]] = []
        if self.title_regex:
            import re

            pat = re.compile(self.title_regex, re.I)
            candidates = [(h, t) for h, t in _enum_windows() if pat.search(t)]
        elif self.title:
            exact = [(h, t) for h, t in _enum_windows() if t == self.title]
            if exact:
                candidates = exact
            else:
                candidates = [
                    (h, t) for h, t in _enum_windows()
                    if self.title.lower() in t.lower()
                ]
        if not candidates:
            return None

        def _area(hwnd: int) -> int:
            rect = _client_rect_screen(hwnd)
            if not rect:
                return 0
            return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])

        return max(candidates, key=lambda pair: _area(pair[0]))[0]

    # ── 状态读取 ──────────────────────────────────────────────

    def _capture(self) -> bytes | None:
        from PIL import ImageGrab

        if not self._hwnd:
            return None
        rect = _client_rect_screen(self._hwnd)
        if not rect or rect[2] <= rect[0] or rect[3] <= rect[1]:
            return None
        try:
            img = ImageGrab.grab(bbox=rect, all_screens=True)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return None

    def _dump_ui_nodes(self) -> tuple[list[dict[str, Any]], str]:
        if not _uia_available() or not self._hwnd:
            return [], "uiautomation 未安装或窗口句柄缺失"
        try:
            import uiautomation as auto

            root = auto.ControlFromHandle(self._hwnd)
            nodes: list[dict[str, Any]] = []
            for ctrl in root.GetChildren():
                try:
                    r = ctrl.BoundingRectangle
                    nodes.append(
                        {
                            "class": ctrl.ControlTypeName,
                            "text": (ctrl.Name or "").strip()[:80],
                            "desc": (ctrl.HelpText or "").strip()[:80],
                            "clickable": ctrl.IsEnabled,
                            "enabled": ctrl.IsEnabled,
                            "bounds": [r.left, r.top, r.right, r.bottom],
                            "center": [(r.left + r.right) // 2, (r.top + r.bottom) // 2],
                        }
                    )
                except Exception:
                    continue
            return nodes, ""
        except Exception as e:  # noqa: BLE001
            return [], f"UIA 读取失败: {type(e).__name__}: {e}"

    def get_state(self) -> dict:
        self._ensure_available()

        png = self._capture()
        nodes, tree_error = self._dump_ui_nodes()
        sig = frame_signature(png) if png else ""
        changed = sig != self._last_sig if self._last_sig else True
        self._last_png = png
        self._last_sig = sig

        summary = summarize_ui_nodes(nodes)
        summary["ui_tree_available"] = bool(nodes)
        if tree_error:
            summary["ui_tree_error"] = tree_error

        state = NativeState(
            platform=self.platform_id,
            scene="ui_tree" if nodes else "native_render",
            screenshot_png=png,
            frame_sig=sig,
            ui_tree_summary=summary,
            available_actions=list(NATIVE_ACTION_TYPES),
            extra={
                "frame_changed": changed,
                "window_title": _window_title(self._hwnd) if self._hwnd else None,
                "client_rect": _client_rect_screen(self._hwnd) if self._hwnd else None,
            },
        )
        raw = state.raw()
        raw["_screenshot_png"] = png
        raw["_nodes"] = nodes
        return raw

    # ── 动作执行 ──────────────────────────────────────────────

    def _focus(self) -> None:
        if self.activate_on_action and self._hwnd:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SetForegroundWindow(self._hwnd)
            time.sleep(0.05)

    def _click_at(self, x: int, y: int) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.02)
        user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.02)
        user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _key(self, vk: int) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
        user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)

    def _type_text(self, text: str) -> None:
        """通过剪贴板 + Ctrl+V 输入，兼容中文。"""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.OpenClipboard(None)
        try:
            user32.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            kernel32.GlobalAlloc.restype = ctypes.c_void_p
            handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
            kernel32.GlobalLock.restype = ctypes.c_void_p
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, buf, size)
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(_CF_UNICODETEXT, ctypes.c_void_p(handle))
        finally:
            user32.CloseClipboard()

        vk_ctrl, vk_v = 0x11, 0x56
        user32.keybd_event(vk_ctrl, 0, 0, 0)
        user32.keybd_event(vk_v, 0, 0, 0)
        user32.keybd_event(vk_v, 0, _KEYEVENTF_KEYUP, 0)
        user32.keybd_event(vk_ctrl, 0, _KEYEVENTF_KEYUP, 0)

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

        try:
            if action in ("tap", "click"):
                x, y = self._resolve_point(params, target)
                self._focus()
                self._click_at(x, y)

            elif action == "swipe":
                x1 = int(params["x1"]); y1 = int(params["y1"])
                x2 = int(params["x2"]); y2 = int(params["y2"])
                dur = int(params.get("duration_ms", 300))
                self._focus()
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.SetCursorPos(x1, y1)
                user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                steps = max(4, dur // 20)
                for i in range(1, steps + 1):
                    user32.SetCursorPos(
                        int(x1 + (x2 - x1) * i / steps),
                        int(y1 + (y2 - y1) * i / steps),
                    )
                    time.sleep(dur / 1000.0 / steps)
                user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

            elif action in ("keypress", "key"):
                key = str(params.get("key") or target or "").lower()
                if key in _VK:
                    vk = _VK[key]
                elif len(key) == 1:
                    vk = ord(key.upper())
                else:
                    raise ValueError(
                        f"不支持的按键 {key!r}；可用: {', '.join(sorted(_VK))} 或单个字符"
                    )
                self._focus()
                self._key(vk)

            elif action == "text":
                text = str(params.get("text") or target or "")
                if not text:
                    raise ValueError("text 需要非空文本")
                self._focus()
                self._type_text(text)

            elif action == "wait":
                ms = int(params.get("ms", params.get("duration_ms", 500)))
                time.sleep(max(0, ms) / 1000.0)

            elif action == "restart":
                self.reset()

            elif action == "back":
                self._focus()
                self._key(_VK["escape"])

            elif action == "home":
                raise ValueError("Windows 适配器无 home 动作；请用 restart 或 keypress")

            else:
                raise ValueError(
                    f"Windows 适配器不支持动作 {action!r}；可用: {', '.join(NATIVE_ACTION_TYPES)}"
                )

        except (ValueError, KeyError, AdapterUnavailable) as e:
            return {
                "ok": False,
                "error": str(e),
                "state": None,
                "duration_ms": int((time.time() - started) * 1000),
                "command": None,
            }

        return {
            "ok": True,
            "error": None,
            "state": None,
            "duration_ms": int((time.time() - started) * 1000),
            "command": None,
        }

    def _resolve_point(self, params: dict[str, Any], target: str | None) -> tuple[int, int]:
        if "x" in params and "y" in params:
            return int(params["x"]), int(params["y"])
        if target:
            nodes, _ = self._dump_ui_nodes()
            for n in nodes:
                if target in n.get("text", "") or target in n.get("desc", ""):
                    cx, cy = n["center"]
                    return int(cx), int(cy)
            raise ValueError(
                f"找不到目标控件 {target!r}（UIA 可能不可用，请改用 x/y 坐标）"
            )
        rect = _client_rect_screen(self._hwnd) if self._hwnd else None
        if not rect:
            raise ValueError("click 需要 x/y 坐标或可定位的 target")
        # 默认点客户区中心
        return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2

    # ── 重置 ──────────────────────────────────────────────────

    def reset(self) -> None:
        if not self.exe_path:
            raise AdapterUnavailable(
                "重启游戏需要 exe_path（或设置 PLAYRANGER_GAME_EXE）"
            )
        exe_name = os.path.basename(self.exe_path)
        run_command(["taskkill", "/F", "/IM", exe_name], timeout=15)
        time.sleep(0.8)
        started = time.time()
        try:
            import subprocess

            subprocess.Popen([self.exe_path], cwd=os.path.dirname(self.exe_path) or None)
        except Exception as e:  # noqa: BLE001
            raise AdapterUnavailable(f"启动 {self.exe_path} 失败: {e}") from e
        # 等窗口出现，最多 20s
        while time.time() - started < 20:
            hwnd = self._find_window()
            if hwnd:
                self._hwnd = hwnd
                return
            time.sleep(0.5)
        raise AdapterUnavailable("进程已启动但 20s 内未找到窗口")

    def screenshot(self, path: str) -> str:
        png = self._last_png or self._capture()
        if not png:
            raise AdapterUnavailable("窗口截图失败（窗口可能被遮挡或最小化）")
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(png)
        return path


# ── Win32 辅助函数 ────────────────────────────────────────────

def _uia_available() -> bool:
    try:
        import uiautomation  # noqa: F401

        return True
    except ImportError:
        return False


def _enum_windows() -> list[tuple[int, str]]:
    if sys.platform != "win32":
        return []
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    result: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):  # noqa: ANN001
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value.strip():
                    result.append((int(hwnd), buf.value))
        return True

    user32.EnumWindows(_cb, 0)
    return result


def _window_title(hwnd: int | None) -> str:
    if not hwnd or sys.platform != "win32":
        return ""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _client_rect_screen(hwnd: int | None) -> tuple[int, int, int, int] | None:
    """客户区在屏幕坐标系中的矩形 (left, top, right, bottom)。"""
    if not hwnd or sys.platform != "win32":
        return None
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y, pt.x + rect.right, pt.y + rect.bottom)


__all__ = ["WindowsAdapter"]
