"""适配器注册表 —— 统一的「可不可用 / 为什么不可用」查询入口。

支持的后端（能力矩阵见 docs/COMPATIBILITY_多端支持矩阵.md）：

| kind              | 宿主平台           | 真值读取            | 动作执行 |
|-------------------|--------------------|---------------------|----------|
| web               | Windows/macOS/Linux | DOM / WebGL 探测    | 鼠标键盘 |
| android           | Windows/macOS/Linux | uiautomator 节点树  | adb input |
| windows           | 仅 Windows          | UIA（可选）+ 截图  | Win32 API |
| linux             | 仅 Linux(X11)       | 截图                | xdotool  |
| ios               | 仅 macOS            | XCUITest 节点树     | WDA HTTP |
| unity-altester    | 任意（需 AltServer） | AltTester 协议占位  | 未实现   |
"""

from __future__ import annotations

from typing import Any

from playranger.core.engines.adapters.base import AdapterUnavailable, EngineAdapter
from playranger.core.engines.adapters.unity_altester import AltTesterAdapter
from playranger.core.engines.adapters.web import WebAdapter

SUPPORTED_KINDS = ("web", "android", "windows", "linux", "ios", "unity-altester")


def get_adapter(kind: str, **kwargs: Any) -> EngineAdapter:
    """按类型创建适配器。

    - web: 需要 engine=<PlayEngine>
    - android: 可选 serial=/package=/activity=
    - windows: 可选 title=/exe_path=/title_regex=
    - linux: 可选 title=/exe_path=/display=
    - ios: 可选 endpoint=/bundle_id=
    - unity-altester: 返回诚实占位（不可用）

    原生适配器构造时不连接设备；真正是否可跑由 capabilities()['available'] 决定。
    """
    key = (kind or "").strip().lower()

    if key in ("web", "playwright", "browser", "webgl", "html5"):
        engine = kwargs.get("engine")
        if engine is None:
            raise AdapterUnavailable("Web 适配器需要传入 engine=<PlayEngine>")
        return WebAdapter(engine)

    if key in ("android", "adb"):
        from playranger.core.engines.adapters.android import AndroidAdapter

        return AndroidAdapter(
            serial=kwargs.get("serial"),
            package=kwargs.get("package"),
            activity=kwargs.get("activity"),
            adb_path=kwargs.get("adb_path"),
        )

    if key in ("windows", "win32", "win"):
        from playranger.core.engines.adapters.windows import WindowsAdapter

        return WindowsAdapter(
            title=kwargs.get("title"),
            exe_path=kwargs.get("exe_path"),
            title_regex=kwargs.get("title_regex"),
        )

    if key in ("linux", "x11"):
        from playranger.core.engines.adapters.linux import LinuxAdapter

        return LinuxAdapter(
            title=kwargs.get("title"),
            exe_path=kwargs.get("exe_path"),
            display=kwargs.get("display"),
        )

    if key in ("ios", "iphone", "ipad", "wda"):
        from playranger.core.engines.adapters.ios import IOSAdapter

        return IOSAdapter(
            endpoint=kwargs.get("endpoint"),
            bundle_id=kwargs.get("bundle_id"),
        )

    if key in ("unity", "altester", "unity-altester"):
        return AltTesterAdapter(
            endpoint=kwargs.get("endpoint", "ws://127.0.0.1:13000"),
            connected=kwargs.get("connected", False),
        )

    raise AdapterUnavailable(
        f"未知适配器类型: {kind}；支持: {', '.join(SUPPORTED_KINDS)}"
    )


def probe_all() -> dict[str, dict]:
    """探测所有适配器的可用性，返回能力矩阵。

    只做只读探测：不启动游戏、不修改设备状态。
    单项探测抛异常时记录为 available=False + error，不影响其他项。
    """
    matrix: dict[str, dict] = {}

    # web：只需检查 playwright 是否安装，不启动浏览器
    try:
        import playwright  # noqa: F401

        matrix["web"] = {
            "available": True,
            "adapter": "web",
            "platform": "web",
            "reason": None,
            "note": "Playwright 已安装；WebGL/Canvas 通过 detect_web_render_mode 判定",
        }
    except ImportError:
        matrix["web"] = {
            "available": False,
            "adapter": "web",
            "platform": "web",
            "reason": "未安装 playwright：pip install playwright && playwright install chromium",
        }

    probes = [
        ("android", lambda: get_adapter("android").probe()),
        ("windows", lambda: get_adapter("windows").probe()),
        ("linux", lambda: get_adapter("linux").probe()),
        ("ios", lambda: get_adapter("ios").probe()),
    ]
    for name, fn in probes:
        try:
            matrix[name] = fn()
        except Exception as e:  # noqa: BLE001
            matrix[name] = {
                "available": False,
                "adapter": name,
                "platform": name,
                "reason": f"{type(e).__name__}: {e}",
            }

    try:
        alt = AltTesterAdapter()
        matrix["unity-altester"] = alt.capabilities()
    except Exception as e:  # noqa: BLE001
        matrix["unity-altester"] = {
            "available": False,
            "adapter": "unity-altester",
            "platform": "unity",
            "reason": f"{type(e).__name__}: {e}",
        }

    return matrix


def supported_platforms() -> list[str]:
    """返回本次运行环境中**真正可用**的平台列表。"""
    return [k for k, v in probe_all().items() if v.get("available")]


__all__ = [
    "AltTesterAdapter",
    "AdapterUnavailable",
    "EngineAdapter",
    "SUPPORTED_KINDS",
    "WebAdapter",
    "get_adapter",
    "probe_all",
    "supported_platforms",
]
