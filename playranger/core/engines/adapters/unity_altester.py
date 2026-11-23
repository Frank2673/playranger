"""Unity / AltTester 适配器 —— 协议占位，未接入时明确报不可用。

为什么保留这个文件：
- 多平台是路线图 P3-4 的明确目标，但当前没有 AltTester 服务与 Unity 测试包
- 与其写一个「看起来能跑」的假适配器，不如把协议、能力位与失败原因写清楚

接入前置条件（未满足即抛 AdapterUnavailable）：
1. Unity 工程安装 AltTester Unity SDK 并导出测试包
2. 启动 AltTester Desktop / AltServer，暴露 WebSocket 端点
3. 本机可访问该端点（默认 127.0.0.1:13000）
"""

from __future__ import annotations

from typing import Any

from playranger.core.engines.adapters.base import AdapterUnavailable

DEFAULT_ENDPOINT = "ws://127.0.0.1:13000"


class AltTesterAdapter:
    """AltTester 协议桥接占位实现。"""

    name = "unity-altester"

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, *, connected: bool = False) -> None:
        self.endpoint = endpoint
        self.connected = connected

    def capabilities(self) -> dict:
        return {
            "available": False,
            "adapter": self.name,
            "endpoint": self.endpoint,
            "reason": (
                "未接入 AltTester 服务：当前仅定义协议，不具备 Unity 真值读取与动作执行能力。"
                "接入需 Unity 侧安装 AltTester SDK、启动 AltServer，并完成连通性验证。"
            ),
        }

    def get_state(self) -> dict:
        raise AdapterUnavailable(self.capabilities()["reason"])

    def get_actions(self) -> list:
        raise AdapterUnavailable(self.capabilities()["reason"])

    def execute(
        self,
        action_type: str,
        target: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict:
        raise AdapterUnavailable(self.capabilities()["reason"])

    def reset(self) -> None:
        raise AdapterUnavailable(self.capabilities()["reason"])


__all__ = ["AltTesterAdapter", "DEFAULT_ENDPOINT"]
