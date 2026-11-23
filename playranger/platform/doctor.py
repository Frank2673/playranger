"""环境体检 —— `playranger doctor` 的实现。

回答三个问题：
1. 这台机器能测哪些端？（Web / Android / Windows / Linux / iOS）
2. 每个端缺什么？怎么补？
3. LLM / VLM / 浏览器这些引擎依赖是否真的可用？

原则：只做只读探测，不启动游戏、不修改设备、不偷偷联网。
`--network` 才会真正 ping LLM/VLM。
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from typing import Any

from playranger.core.engines.adapters import SUPPORTED_KINDS, probe_all

_REMEDIATION = {
    "web": [
        "pip install playwright",
        "playwright install chromium",
        'PowerShell 无网时可复用本机浏览器: $env:PLAYWRIGHT_CHROMIUM_PATH="C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"',
    ],
    "android": [
        "安装 Android platform-tools，把 adb 加入 PATH（或用 PLAYRANGER_ADB_PATH 指定）",
        "手机开启「开发者选项 → USB 调试」，连接后执行 adb devices",
        "无线调试: adb connect <手机IP>:5555",
        "指定被测应用: --package com.example.game （重启功能必需）",
    ],
    "windows": [
        "仅在 Windows 宿主可用（本项需 sys.platform == 'win32'）",
        "安装 Pillow 以启用截图: pip install pillow",
        "可选 UIA 控件树: pip install uiautomation（Unity/Unreal 游戏不需要）",
        "运行游戏后传 --window-title 或设置 PLAYRANGER_WINDOW_TITLE",
    ],
    "linux": [
        "安装 xdotool: sudo apt install xdotool",
        "安装截图工具之一: sudo apt install imagemagick / scrot / grim",
        "确认在 X11 会话中运行（echo $DISPLAY）；Wayland 原生窗口不支持注入",
        "传 --window-title 指定窗口",
    ],
    "ios": [
        "需要 macOS + Xcode，并在设备上运行 WebDriverAgent",
        "启动 WDA 后确认 http://127.0.0.1:8100/status 返回 JSON",
        "传 --bundle-id com.company.game",
        "免费证书签名的 WDA 每 7 天需重签",
    ],
    "unity-altester": [
        "Unity 工程安装 AltTester Unity SDK 并导出测试包",
        "启动 AltTester Desktop / AltServer，默认端口 13000",
        "该后端当前为协议占位，未实现真实动作执行",
    ],
}


def _os_info() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "is_wsl": "microsoft" in platform.release().lower(),
        "session_type": (os.environ.get("XDG_SESSION_TYPE") or "").lower() or None,
    }


def _tool_versions() -> dict[str, str | None]:
    tools = ["adb", "xdotool", "import", "scrot", "grim", "node", "npm"]
    return {t: shutil.which(t) for t in tools}


def run_doctor(*, network: bool = False) -> dict[str, Any]:
    """聚合体检结果。network=True 时才真正请求 LLM/VLM 健康检查。"""
    adapters = probe_all()
    os_info = _os_info()
    tools = _tool_versions()

    engines: dict[str, Any] = {"llm": None, "vlm": None, "browser": None}

    # 浏览器：只查模块是否可导入，不启动
    try:
        import playwright  # noqa: F401

        engines["browser"] = {"available": True, "reason": None}
    except ImportError:
        engines["browser"] = {
            "available": False,
            "reason": "未安装 playwright",
        }

    if network:
        try:
            from playranger.core.capabilities.system_capabilities import detect_capabilities

            caps = detect_capabilities()
            engines["llm"] = {"available": caps.llm_available}
            engines["vlm"] = {"available": caps.vlm_available}
        except Exception as e:  # noqa: BLE001
            engines["llm"] = {"available": False, "reason": f"{type(e).__name__}: {e}"}
            engines["vlm"] = {"available": False, "reason": f"{type(e).__name__}: {e}"}
    else:
        llm_key = os.environ.get("LLM_API_KEY", "")
        engines["llm"] = {
            "available": bool(llm_key and llm_key != "sk-placeholder"),
            "reason": None if llm_key else "未配置 LLM_API_KEY（跳过网络检查需 --network 实测）",
            "checked": "env_only",
        }
        engines["vlm"] = {
            "available": bool(os.environ.get("VLM_API_KEY") or llm_key),
            "reason": None if (os.environ.get("VLM_API_KEY") or llm_key) else "未配置 VLM_API_KEY / LLM_API_KEY",
            "checked": "env_only",
        }

    available = [k for k, v in adapters.items() if v.get("available")]
    unavailable = {k: v.get("reason") for k, v in adapters.items() if not v.get("available")}

    remediation = []
    for kind in SUPPORTED_KINDS:
        info = adapters.get(kind, {})
        if info.get("available"):
            continue
        reason = info.get("reason") or "不可用"
        remediation.append(
            {
                "platform": kind,
                "reason": reason,
                "steps": _REMEDIATION.get(kind, []),
            }
        )

    return {
        "os": os_info,
        "tools": tools,
        "adapters": adapters,
        "engines": engines,
        "summary": {
            "available_platforms": available,
            "unavailable_count": len(unavailable),
            "can_test_web": "web" in available,
            "can_test_mobile": bool({"android", "ios"} & set(available)),
            "can_test_desktop": bool({"windows", "linux"} & set(available)),
        },
        "remediation": remediation,
        "honesty_notes": [
            "Android 的节点树对 Unity/Unreal 原生渲染通常为空，此时走截图+视觉；"
            "这不会让测试失败，但会降低断言精度。",
            "iOS 必须 macOS + WebDriverAgent，Windows/Linux 宿主无法直接测 iOS。",
            "Linux 仅 X11 可靠；Wayland 原生窗口无法注入鼠标键盘。",
            "Windows 截图要求窗口在前台，最小化或被完全遮挡时截图不可信。",
            "WebGL/Canvas 游戏的 DOM 为空是正常现象，请用视觉断言而非选择器断言。",
        ],
    }


def format_text(report: dict[str, Any]) -> str:
    """渲染成人类可读的体检报告。"""
    lines: list[str] = []
    os_i = report["os"]
    lines.append("=" * 62)
    lines.append("PlayRanger 环境体检")
    lines.append("=" * 62)
    lines.append(
        f"宿主: {os_i['system']} {os_i['release']} ({os_i['machine']})  "
        f"Python {os_i['python']}"
    )
    if os_i.get("is_wsl"):
        lines.append("⚠️  检测到 WSL：WSL 内无法直接使用 Windows 的 GUI 注入能力")
    if os_i.get("session_type"):
        lines.append(f"会话类型: {os_i['session_type']}")
    lines.append("")

    lines.append("── 平台适配 ───────────────────────────────────────────")
    for kind in SUPPORTED_KINDS:
        info = report["adapters"].get(kind, {})
        ok = info.get("available")
        mark = "✅" if ok else "❌"
        label = {
            "web": "Web (HTML5/Canvas/WebGL)",
            "android": "Android (ADB)",
            "windows": "Windows (Win32)",
            "linux": "Linux (X11)",
            "ios": "iOS (WebDriverAgent)",
            "unity-altester": "Unity (AltTester)",
        }.get(kind, kind)
        lines.append(f"{mark} {label}")
        if ok:
            extra_bits = []
            for key in ("serial", "model", "android_version", "window_title",
                        "screenshot_backend", "wda_version", "client_rect"):
                if info.get(key):
                    extra_bits.append(f"{key}={info[key]}")
            if extra_bits:
                lines.append(f"     {'  '.join(extra_bits)}")
            if info.get("warning"):
                lines.append(f"     ⚠️  {info['warning']}")
            if info.get("note"):
                lines.append(f"     ℹ️  {info['note']}")
        else:
            lines.append(f"     原因: {info.get('reason')}")
    lines.append("")

    lines.append("── 引擎依赖 ───────────────────────────────────────────")
    for name, label in (("browser", "浏览器自动化"), ("llm", "LLM 规划"), ("vlm", "VLM 视觉")):
        info = report["engines"].get(name) or {}
        mark = "✅" if info.get("available") else "❌"
        suffix = ""
        if info.get("checked") == "env_only":
            suffix = "（仅检查环境变量，加 --network 实测）"
        if not info.get("available") and info.get("reason"):
            suffix = f" — {info['reason']}"
        lines.append(f"{mark} {label}{suffix}")
    lines.append("")

    summary = report["summary"]
    lines.append("── 结论 ───────────────────────────────────────────────")
    lines.append(f"可用平台: {', '.join(summary['available_platforms']) or '无'}")
    lines.append(
        f"Web {'✅' if summary['can_test_web'] else '❌'}   "
        f"移动端 {'✅' if summary['can_test_mobile'] else '❌'}   "
        f"桌面端 {'✅' if summary['can_test_desktop'] else '❌'}"
    )
    lines.append("")

    if report["remediation"]:
        lines.append("── 修复建议 ───────────────────────────────────────────")
        for item in report["remediation"]:
            lines.append(f"[{item['platform']}] {item['reason']}")
            for step in item["steps"]:
                lines.append(f"  • {step}")
            lines.append("")

    lines.append("── 诚实边界（必读）────────────────────────────────────")
    for note in report["honesty_notes"]:
        lines.append(f"• {note.strip()}")
    lines.append("")
    lines.append("=" * 62)

    return "\n".join(lines)


__all__ = ["format_text", "run_doctor"]
