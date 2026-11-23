"""Android 适配器 —— 基于 ADB + uiautomator 的真实实现。

能力边界（诚实声明）：
- ✅ 能读：截图、可访问性节点树（uiautomator dump）
- ✅ 能做：tap / swipe / keyevent / 文本输入 / 启停应用
- ⚠️ 节点树只对**标准 View/Compose/Flutter 带语义**的界面有效；
  Unity / Unreal / Cocos 原生渲染的游戏通常返回空树 → 自动降级为「截图 + 视觉」模式
- ❌ 不做：绕过反调试/防作弊、root 注入、内存读写

宿主要求：Windows / macOS / Linux 任一，装 platform-tools(adb)，设备开启 USB 调试。
在宿主机上执行 `adb devices` 能看到设备即可。
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from playranger.core.engines.adapters.base import AdapterUnavailable
from playranger.core.engines.adapters.native_base import (
    NATIVE_ACTION_TYPES,
    NativeAdapterBase,
    NativeState,
    frame_signature,
    run_command,
    summarize_ui_nodes,
    which,
)

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# Android KeyEvent 白名单（避免任意字符串直接拼进 shell）
_KEYCODES = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "enter": "KEYCODE_ENTER",
    "escape": "KEYCODE_ESCAPE",
    "tab": "KEYCODE_TAB",
    "space": "KEYCODE_SPACE",
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "power": "KEYCODE_POWER",
    "menu": "KEYCODE_MENU",
}


class AndroidAdapter(NativeAdapterBase):
    """ADB 驱动的 Android 游戏适配器。"""

    name = "android"
    platform_id = "android"

    def __init__(
        self,
        serial: str | None = None,
        package: str | None = None,
        activity: str | None = None,
        *,
        adb_path: str | None = None,
        ui_dump_timeout: float = 12.0,
    ) -> None:
        self.serial = serial or os.environ.get("PLAYRANGER_ANDROID_SERIAL") or None
        self.package = package or os.environ.get("PLAYRANGER_ANDROID_PACKAGE") or None
        self.activity = activity or os.environ.get("PLAYRANGER_ANDROID_ACTIVITY") or None
        self.adb = adb_path or which("adb") or "adb"
        self.ui_dump_timeout = ui_dump_timeout
        self._last_png: bytes | None = None
        self._last_frame_sig: str = ""

    # ── 命令封装 ──────────────────────────────────────────────

    def _adb(self, *args: str, timeout: float = 20.0, binary: bool = False):
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        return run_command(cmd, timeout=timeout, binary=binary)

    # ── 可用性探测 ────────────────────────────────────────────

    def probe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"available": False, "adapter": self.name}
        if which(self.adb) is None and not os.path.exists(self.adb):
            info["reason"] = (
                "未找到 adb。请安装 Android platform-tools 并把 adb 加入 PATH，"
                "或用 PLAYRANGER_ADB_PATH 指定绝对路径。"
            )
            return info

        res = self._adb("devices", "-l", timeout=8)
        if not res.ok:
            info["reason"] = f"adb devices 执行失败: {res.brief()}"
            return info

        devices: list[dict[str, str]] = []
        for line in res.stdout.splitlines()[1:]:
            line = line.strip()
            if not line or "\t" not in line:
                continue
            serial, rest = line.split("\t", 1)
            parts = rest.split()
            state = parts[0] if parts else "unknown"
            model = ""
            for p in parts[1:]:
                if p.startswith("model:"):
                    model = p.split(":", 1)[1]
            devices.append({"serial": serial, "state": state, "model": model})

        online = [d for d in devices if d["state"] == "device"]
        info["devices"] = devices
        info["adb_path"] = self.adb

        if not online:
            reason = "未检测到已授权设备。"
            unauthorized = [d for d in devices if d["state"] == "unauthorized"]
            if unauthorized:
                reason += " 有设备未授权：请在手机屏幕点『允许 USB 调试』。"
            elif not devices:
                reason += " 请用 USB 连接设备或 `adb connect <ip:port>` 连接无线调试。"
            info["reason"] = reason
            return info

        if self.serial and self.serial not in {d["serial"] for d in online}:
            info["reason"] = f"指定设备 {self.serial} 不在线"
            return info
        if not self.serial:
            self.serial = online[0]["serial"]

        # 屏幕尺寸 / 版本
        size_res = self._adb("shell", "wm", "size", timeout=8)
        version_res = self._adb("shell", "getprop", "ro.build.version.release", timeout=8)
        m = re.search(r"(\d+)x(\d+)", size_res.stdout or "")
        info.update(
            {
                "available": True,
                "serial": self.serial,
                "model": next(
                    (d["model"] for d in online if d["serial"] == self.serial), ""
                ),
                "screen": {"width": int(m.group(1)), "height": int(m.group(2))} if m else None,
                "android_version": (version_res.stdout or "").strip() or None,
                "package": self.package,
                "ui_tree": "uiautomator",
            }
        )
        return info

    # ── 状态读取 ──────────────────────────────────────────────

    def _screenshot_bytes(self) -> bytes | None:
        res = self._adb("exec-out", "screencap", "-p", timeout=20, binary=True)
        if res.returncode != 0:
            return None
        data = res.stdout_bytes
        # 某些 adb 版本会把 CRLF 混进 PNG 流，只接受标准 PNG 头
        if data and data.startswith(b"\x89PNG"):
            return data
        return None

    def _dump_ui_nodes(self) -> tuple[list[dict[str, Any]], str]:
        """返回 (节点列表, 失败原因)。失败不抛异常——原生渲染游戏本来就是空树。"""
        remote = "/sdcard/playranger_ui.xml"
        dump = self._adb(
            "shell", "uiautomator", "dump", remote, timeout=self.ui_dump_timeout
        )
        if not dump.ok:
            return [], f"uiautomator dump 失败: {dump.brief()}"

        cat = self._adb("exec-out", "cat", remote, timeout=self.ui_dump_timeout)
        if not cat.ok or "<hierarchy" not in (cat.stdout or ""):
            return [], "uiautomator 输出为空（可能是原生渲染界面）"

        try:
            root = ET.fromstring(cat.stdout)
        except ET.ParseError as e:
            return [], f"UI XML 解析失败: {e}"

        nodes: list[dict[str, Any]] = []
        for node in root.iter("node"):
            bounds = node.get("bounds", "")
            m = _BOUNDS_RE.match(bounds)
            if not m:
                continue
            x1, y1, x2, y2 = (int(v) for v in m.groups())
            nodes.append(
                {
                    "class": node.get("class", ""),
                    "text": (node.get("text") or "").strip(),
                    "desc": (node.get("content-desc") or "").strip(),
                    "resource_id": node.get("resource-id", ""),
                    "clickable": node.get("clickable") == "true",
                    "enabled": node.get("enabled") == "true",
                    "bounds": [x1, y1, x2, y2],
                    "center": [(x1 + x2) // 2, (y1 + y2) // 2],
                }
            )
        return nodes, ""

    def get_state(self) -> dict:
        self._ensure_available()

        png = self._screenshot_bytes()
        nodes, tree_error = self._dump_ui_nodes()
        sig = frame_signature(png) if png else ""

        # 画面完全没变 + 也没有节点 → 明确标记，避免上层误判为「卡死」
        changed = sig != self._last_frame_sig if self._last_frame_sig else True
        self._last_png = png
        self._last_frame_sig = sig

        scene = "native_render" if not nodes else "ui_tree"
        summary = summarize_ui_nodes(nodes)
        summary["ui_tree_available"] = bool(nodes)
        if tree_error:
            summary["ui_tree_error"] = tree_error

        state = NativeState(
            platform=self.platform_id,
            scene=scene,
            screenshot_png=png,
            frame_sig=sig,
            ui_tree_summary=summary,
            available_actions=list(NATIVE_ACTION_TYPES),
            extra={
                "frame_changed": changed,
                "package": self.package,
                "node_count": len(nodes),
            },
        )
        raw = state.raw()
        raw["_screenshot_png"] = png
        raw["_nodes"] = nodes
        return raw

    # ── 动作执行 ──────────────────────────────────────────────

    @staticmethod
    def _require_xy(params: dict[str, Any], keys: tuple[str, ...]) -> list[int]:
        vals: list[int] = []
        for k in keys:
            if k not in params:
                raise ValueError(f"缺少参数 {k}")
            vals.append(int(params[k]))
        return vals

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
                # 支持三种定位：x/y 坐标、target 为元素文本、节点下标
                if "x" in params and "y" in params:
                    x, y = int(params["x"]), int(params["y"])
                elif target:
                    x, y = self._locate_by_text(target)
                else:
                    raise ValueError("tap 需要 x/y 或 target 文本")
                res = self._adb("shell", "input", "tap", str(x), str(y))

            elif action == "swipe":
                x1, y1, x2, y2 = self._require_xy(
                    params, ("x1", "y1", "x2", "y2")
                )
                dur = int(params.get("duration_ms", 300))
                res = self._adb(
                    "shell", "input", "swipe",
                    str(x1), str(y1), str(x2), str(y2), str(dur),
                )

            elif action in ("keypress", "key"):
                key = str(params.get("key") or target or "").lower()
                code = _KEYCODES.get(key)
                if not code:
                    raise ValueError(
                        f"不支持的按键 {key!r}；可用: {', '.join(sorted(_KEYCODES))}"
                    )
                res = self._adb("shell", "input", "keyevent", code)

            elif action == "back":
                res = self._adb("shell", "input", "keyevent", "KEYCODE_BACK")

            elif action == "home":
                res = self._adb("shell", "input", "keyevent", "KEYCODE_HOME")

            elif action == "text":
                text = str(params.get("text") or target or "")
                if not text:
                    raise ValueError("text 需要非空文本")
                # input text 不支持空格与部分符号，用 %s 代替空格
                safe = text.replace(" ", "%s")
                res = self._adb("shell", "input", "text", safe)

            elif action == "wait":
                ms = int(params.get("ms", params.get("duration_ms", 500)))
                time.sleep(max(0, ms) / 1000.0)
                return {"ok": True, "error": None, "state": None,
                        "duration_ms": int((time.time() - started) * 1000),
                        "command": None}

            elif action == "restart":
                self.reset()
                return {"ok": True, "error": None, "state": self.get_state(),
                        "duration_ms": int((time.time() - started) * 1000),
                        "command": "am force-stop + start"}

            else:
                raise ValueError(
                    f"Android 适配器不支持动作 {action!r}；可用: {', '.join(NATIVE_ACTION_TYPES)}"
                )

        except (ValueError, AdapterUnavailable) as e:
            return {
                "ok": False,
                "error": str(e),
                "state": None,
                "duration_ms": int((time.time() - started) * 1000),
                "command": None,
            }

        ok = bool(res and res.ok)
        return {
            "ok": ok,
            "error": None if ok else (res.brief() if res else "未知错误"),
            "state": None,
            "duration_ms": int((time.time() - started) * 1000),
            "command": res.to_dict() if res else None,
        }

    def _locate_by_text(self, text: str) -> tuple[int, int]:
        nodes, _ = self._dump_ui_nodes()
        for n in nodes:
            if text in n.get("text", "") or text in n.get("desc", ""):
                cx, cy = n["center"]
                return int(cx), int(cy)
        raise ValueError(f"界面上找不到可点击元素: {text!r}")

    # ── 重置 ──────────────────────────────────────────────────

    def reset(self) -> None:
        if not self.package:
            raise AdapterUnavailable(
                "重启应用需要指定包名：PlayRanger(... package='com.example.game') "
                "或设置 PLAYRANGER_ANDROID_PACKAGE"
            )
        self._adb("shell", "am", "force-stop", self.package, timeout=15)
        time.sleep(0.6)
        if self.activity:
            component = f"{self.package}/{self.activity}"
            self._adb("shell", "am", "start", "-n", component, timeout=20)
        else:
            self._adb(
                "shell", "monkey", "-p", self.package,
                "-c", "android.intent.category.LAUNCHER", "1",
                timeout=20,
            )

    # ── 截图落盘 ──────────────────────────────────────────────

    def screenshot(self, path: str) -> str:
        png = self._last_png or self._screenshot_bytes()
        if not png:
            raise AdapterUnavailable("adb screencap 未返回 PNG")
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(png)
        return path


def _read_binary(cmd: list[str]) -> bytes:
    """已废弃：保留仅为兼容旧调用，新代码请用 run_command(binary=True)。"""
    import subprocess

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=25, check=False)
        return proc.stdout if proc.returncode == 0 else b""
    except Exception:
        return b""


__all__ = ["AndroidAdapter"]
