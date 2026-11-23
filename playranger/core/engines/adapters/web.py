"""Web 适配器 —— 把现有 PlayEngine 包成统一协议。"""

from __future__ import annotations

from typing import Any

from playranger.core.engines.adapters.base import AdapterUnavailable


class WebAdapter:
    """基于 Playwright + PlayEngine 的 Web 游戏适配器。"""

    name = "web"

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def capabilities(self) -> dict:
        caps: dict[str, Any] = {
            "available": True,
            "adapter": self.name,
        }
        try:
            caps.update(self.engine.get_capabilities())
        except Exception as e:  # 页面未启动时只报 available=False，不抛异常
            caps.update({"available": False, "reason": str(e)})
        return caps

    def get_state(self) -> dict:
        if self.engine is None:
            raise AdapterUnavailable("WebAdapter 未绑定 PlayEngine")
        return self.engine.read_state().raw

    def get_actions(self) -> list:
        state = self.get_state()
        return list(state.get("available_actions", []))

    def execute(
        self,
        action_type: str,
        target: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict:
        record = self.engine.execute(action_type, target=target, params=params or {})
        return {
            "ok": bool(record.ok),
            "error": record.error,
            "state": record.state_after,
            "duration_ms": record.duration_ms,
        }

    def reset(self) -> None:
        self.engine.sdk_reset()


__all__ = ["WebAdapter"]
