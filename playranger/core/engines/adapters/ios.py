"""iOS 适配器 —— 基于 WebDriverAgent (WDA) 的真实实现。

能力边界（诚实声明）：
- ✅ 能读：截图、可访问性元素树（XCUITest）
- ✅ 能做：点按、拖拽、输入文本、按键（home/volume）、启停应用
- ⚠️ **必须在 macOS 上运行 WebDriverAgent**（Xcode 签名 + 真机信任）
  本仓库只负责连接 WDA，不打包 Xcode 工程
- ⚠️ 未签名的 WDA 每 7 天需重签（Apple 免费开发者证书限制）
- ❌ 不做：越狱设备私有 API、绕过 App Store 保护

宿主要求：macOS + Xcode + WebDriverAgent 运行在 http://127.0.0.1:8100。
若 WDA 跑在另一台 Mac 上，可用 endpoint 参数指向其局域网地址。
"""

from __future__ import annotations

import os
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any

from playranger.core.engines.adapters.base import AdapterUnavailable
from playranger.core.engines.adapters.native_base import (
    NATIVE_ACTION_TYPES,
    NativeAdapterBase,
    NativeState,
    frame_signature,
    summarize_ui_nodes,
)

DEFAULT_WDA_ENDPOINT = "http://127.0.0.1:8100"


class IOSAdapter(NativeAdapterBase):
    """WebDriverAgent 驱动的 iOS 适配器。"""

    name = "ios"
    platform_id = "ios"

    def __init__(
        self,
        endpoint: str | None = None,
        bundle_id: str | None = None,
        *,
        timeout: float = 15.0,
    ) -> None:
        self.endpoint = (endpoint or os.environ.get("PLAYRANGER_WDA_ENDPOINT")
                         or DEFAULT_WDA_ENDPOINT).rstrip("/")
        self.bundle_id = bundle_id or os.environ.get("PLAYRANGER_IOS_BUNDLE_ID") or None
        self.timeout = timeout
        self._session_id: str | None = None
        self._last_png: bytes | None = None
        self._last_sig: str = ""

    # ── HTTP 封装 ─────────────────────────────────────────────

    def _client(self):
        import httpx

        return httpx.Client(base_url=self.endpoint, timeout=self.timeout)

    def _request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        with self._client() as client:
            resp = client.request(method, path, json=json_body)
        if resp.status_code >= 400:
            raise AdapterUnavailable(
                f"WDA {method} {path} 返回 {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            raise AdapterUnavailable(f"WDA 响应不是 JSON: {e}") from e

    # ── 探测 ──────────────────────────────────────────────────

    def probe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"available": False, "adapter": self.name}
        info["host_platform"] = sys.platform
        info["endpoint"] = self.endpoint

        if sys.platform != "darwin":
            info["reason"] = (
                f"iOS 适配器需要 macOS 宿主运行 WebDriverAgent（当前 {sys.platform}）。"
                "若 WDA 已运行在另一台 Mac，可显式传 endpoint= 指向它并忽略此提示。"
            )
            return info

        try:
            data = self._request("GET", "/status")
        except Exception as e:  # noqa: BLE001
            info["reason"] = (
                f"无法连接 WebDriverAgent（{self.endpoint}）：{e}。"
                "请先启动 WDA：xcodebuild test-without-building -scheme WebDriverAgentRunner ..."
            )
            return info

        build = (data.get("value") or {}).get("build") or {}
        info.update(
            {
                "available": True,
                "wda_version": build.get("version"),
                "device": build.get("productBundleIdentifier") or build.get("device"),
                "ui_tree": "XCUITest",
            }
        )

        # bundle_id 存在就提前建 session（能建成功说明设备真的可用）
        if self.bundle_id:
            try:
                self._ensure_session()
                info["session_id"] = self._session_id
            except Exception as e:  # noqa: BLE001
                info["available"] = False
                info["reason"] = f"WDA 在线但建会话失败（bundle_id={self.bundle_id}）：{e}"
        return info

    def _ensure_session(self) -> str:
        if self._session_id:
            return self._session_id
        if not self.bundle_id:
            raise AdapterUnavailable(
                "iOS 需要 bundle_id（如 com.company.game），"
                "传参 bundle_id= 或设置 PLAYRANGER_IOS_BUNDLE_ID"
            )
        data = self._request(
            "POST",
            "/session",
            {"capabilities": {"alwaysMatch": {"bundleId": self.bundle_id}}},
        )
        sid = (data or {}).get("sessionId") or ((data or {}).get("value") or {}).get("sessionId")
        if not sid:
            raise AdapterUnavailable(f"WDA 建会话未返回 sessionId: {str(data)[:200]}")
        self._session_id = sid
        return sid

    # ── 状态 ──────────────────────────────────────────────────

    def _screenshot_bytes(self) -> bytes | None:
        import base64

        try:
            sid = self._ensure_session()
            data = self._request("GET", f"/session/{sid}/wda/screenshot")
            b64 = (data.get("value") or {})
            if isinstance(b64, str):
                return base64.b64decode(b64)
            if isinstance(b64, dict):
                return base64.b64decode(b64.get("data") or "")
        except Exception:
            return None
        return None

    def _dump_nodes(self) -> tuple[list[dict[str, Any]], str]:
        try:
            sid = self._ensure_session()
            data = self._request("GET", f"/session/{sid}/source")
            xml_text = (data.get("value") or "")
            if not isinstance(xml_text, str) or "<" not in xml_text:
                return [], "WDA source 为空"
            root = ET.fromstring(xml_text)
            nodes: list[dict[str, Any]] = []
            for el in root.iter():
                if not el.tag.startswith("XCUIElementType"):
                    continue
                x = _int(el.get("x"))
                y = _int(el.get("y"))
                w = _int(el.get("width"))
                h = _int(el.get("height"))
                if w == 0 and h == 0:
                    continue
                nodes.append(
                    {
                        "class": el.tag,
                        "text": (el.get("label") or el.get("name") or "").strip()[:80],
                        "desc": (el.get("value") or "").strip()[:80],
                        "clickable": el.get("enabled") == "true",
                        "enabled": el.get("enabled") == "true",
                        "bounds": [x, y, x + w, y + h],
                        "center": [x + w // 2, y + h // 2],
                    }
                )
            return nodes, ""
        except Exception as e:  # noqa: BLE001
            return [], f"WDA source 解析失败: {type(e).__name__}: {e}"

    def get_state(self) -> dict:
        self._ensure_available()
        png = self._screenshot_bytes()
        nodes, tree_error = self._dump_nodes()
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
                "bundle_id": self.bundle_id,
                "node_count": len(nodes),
            },
        )
        raw = state.raw()
        raw["_screenshot_png"] = png
        raw["_nodes"] = nodes
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

        try:
            sid = self._ensure_session()

            if action in ("tap", "click"):
                if "x" in params and "y" in params:
                    x, y = int(params["x"]), int(params["y"])
                elif target:
                    x, y = self._locate_by_text(target)
                else:
                    raise ValueError("tap 需要 x/y 或 target 文本")
                self._request(
                    "POST", f"/session/{sid}/wda/tap/0", {"x": x, "y": y}
                )

            elif action == "swipe":
                x1, y1 = int(params["x1"]), int(params["y1"])
                x2, y2 = int(params["x2"]), int(params["y2"])
                dur = float(params.get("duration_ms", 300)) / 1000.0
                self._request(
                    "POST",
                    f"/session/{sid}/wda/dragfromtoforduration",
                    {"fromX": x1, "fromY": y1, "toX": x2, "toY": y2, "duration": dur},
                )

            elif action == "text":
                text = str(params.get("text") or target or "")
                if not text:
                    raise ValueError("text 需要非空文本")
                self._request("POST", f"/session/{sid}/wda/keys", {"value": [text]})

            elif action in ("keypress", "key"):
                key = str(params.get("key") or target or "").lower()
                if key in ("home", "back"):
                    self._request("POST", f"/session/{sid}/wda/pressButton", {"name": "home"})
                elif key.startswith("volume_"):
                    self._request(
                        "POST", f"/session/{sid}/wda/pressButton",
                        {"name": key.replace("_", "")},
                    )
                else:
                    raise ValueError(
                        "iOS 仅支持 home / volume_up / volume_down 按键；"
                        "文本请输入用 text 动作"
                    )

            elif action in ("back", "home"):
                self._request("POST", f"/session/{sid}/wda/pressButton", {"name": "home"})

            elif action == "wait":
                ms = int(params.get("ms", params.get("duration_ms", 500)))
                time.sleep(max(0, ms) / 1000.0)

            elif action == "restart":
                self.reset()
                return {
                    "ok": True, "error": None, "state": None,
                    "duration_ms": int((time.time() - started) * 1000),
                    "command": "terminate + launch",
                }

            else:
                raise ValueError(
                    f"iOS 适配器不支持动作 {action!r}；可用: {', '.join(NATIVE_ACTION_TYPES)}"
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

    def _locate_by_text(self, text: str) -> tuple[int, int]:
        nodes, _ = self._dump_nodes()
        for n in nodes:
            if text in n.get("text", "") or text in n.get("desc", ""):
                cx, cy = n["center"]
                return int(cx), int(cy)
        raise ValueError(f"界面找不到元素: {text!r}")

    # ── 重置 ──────────────────────────────────────────────────

    def reset(self) -> None:
        if not self.bundle_id:
            raise AdapterUnavailable("iOS 重启应用需要 bundle_id")
        sid = self._ensure_session()
        try:
            self._request(
                "POST", f"/session/{sid}/appium/app/terminate",
                {"bundleId": self.bundle_id},
            )
        except Exception:
            pass
        time.sleep(0.5)
        self._request(
            "POST", f"/session/{sid}/appium/app/launch",
            {"bundleId": self.bundle_id},
        )

    def screenshot(self, path: str) -> str:
        png = self._last_png or self._screenshot_bytes()
        if not png:
            raise AdapterUnavailable("WDA 截图失败")
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(png)
        return path

    def close(self) -> None:
        if self._session_id:
            try:
                self._request("DELETE", f"/session/{self._session_id}")
            except Exception:
                pass
            self._session_id = None


def _int(v: str | None, default: int = 0) -> int:
    try:
        return int(float(v)) if v is not None else default
    except (TypeError, ValueError):
        return default


__all__ = ["DEFAULT_WDA_ENDPOINT", "IOSAdapter"]
