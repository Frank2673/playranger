"""多平台适配协议 —— 让引擎从「Playwright 专用」变成「可替换的执行后端」。

只定义协议与能力位，不假装已支持某引擎：
- WebAdapter：现有 PlayEngine 的正式封装，能力完整
- AltTesterAdapter：Unity 协议桥接的诚实占位（未接入服务时明确不可用）
- 原生手游：暂无适配器，get_adapter 会明确报错而不是静默降级

验收口径：适配器必须能回答「可不可用 / 为什么不可用」，不允许返回假状态。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class AdapterUnavailable(RuntimeError):
    """适配器不可用（未接入、依赖缺失或能力未实现）。"""


@runtime_checkable
class EngineAdapter(Protocol):
    """引擎适配器协议。

    所有方法都应是「薄封装」：真实状态由引擎提供，适配器不臆造。
    """

    name: str

    def capabilities(self) -> dict:
        """返回能力位：playable/evidential/deterministic/tunable/available。"""
        ...

    def get_state(self) -> dict:
        """返回当前游戏状态（至少包含 scene/hp/score/level/available_actions）。"""
        ...

    def get_actions(self) -> list:
        """返回当前可执行动作列表。"""
        ...

    def execute(self, action_type: str, target: str | None = None,
                params: dict[str, Any] | None = None) -> dict:
        """执行动作并返回 {ok, error?, state?}。"""
        ...

    def reset(self) -> None:
        """重置到初始状态。"""
        ...


__all__ = ["AdapterUnavailable", "EngineAdapter"]
