"""大脑工厂 —— 创建 Planner，支持多级自动降级。

降级链路（从高到低）：
1. 通用 OpenAI 兼容接口（LLM_API_BASE / LLM_API_KEY 环境变量，最灵活）
2. DGX Spark 本地推理（最低延迟、最高隐私、首选）
3. 魔搭 API 兜底（远程、有 API key 就能用）
4. MockBrain（纯本地、无 LLM 依赖、用于开发和测试）

自动降级逻辑：
- 按优先级逐个尝试健康检查
- 第一个可用的就用
- 把实际使用的来源记在 planner.source 上方便调试
- 如果全部失败，返回 mock（保证永远有可用的大脑）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from playranger.core.brain.llm_client import LLMClient
from playranger.core.brain.planner import Planner, GameAction, PlanOutput, ReplanOutput, ReflectionOutput
from playranger.core.brain.vlm_client import VLMClient


@dataclass
class BrainInfo:
    """大脑信息，方便调试和展示。"""

    source: str  # openai-compatible / dgx-spark / modelscope / mock
    base_url: str
    model: str
    health_ok: bool
    health_message: str = ""


class MockLLMClient:
    """Mock LLM 客户端，纯本地、不发网络请求。

    用于：
    - 开发期没有 API key 时
    - 单元测试
    - 网络完全不可用时的最后兜底

    行为：总是返回第一个可用动作的 noop/wait 动作。
    """

    def __init__(self, model: str = "mock") -> None:
        self.model = model
        self.base_url = "mock://local"
        self.api_key = ""

    def chat(self, messages: list[dict], **kwargs: Any) -> str:
        return "mock response"

    def chat_json(self, messages: list[dict], **kwargs: Any) -> dict:
        return {"status": "mock", "action": "noop"}

    def chat_pydantic(
        self,
        messages: list[dict],
        model_cls: Any,
        **kwargs: Any,
    ) -> Any:
        if model_cls is PlanOutput:
            return PlanOutput(
                thought="Mock LLM: 没有可用的真实推理服务，返回默认动作。",
                action=GameAction(
                    action_type="wait",
                    reason="mock_fallback",
                ),
                expected_after="等待状态变化",
                confidence=0.0,
            )
        if model_cls is ReplanOutput:
            return ReplanOutput(
                thought="Mock LLM: 重规划失败兜底。",
                new_approach="mock_fallback",
                action=GameAction(action_type="wait", reason="mock_fallback"),
                expected_after="no change",
            )
        if model_cls is ReflectionOutput:
            return ReflectionOutput(
                failure_root_cause="unknown (mock)",
                strategy_adjustment="retry_same_approach",
                should_retry=False,
                next_attempt_focus="unknown",
            )
        # 默认
        return model_cls()

    def health_check(self) -> tuple[bool, str]:
        return True, "mock always ok"

    def close(self) -> None:
        pass


def create_brain(
    config: dict[str, Any] | None = None,
    auto_downgrade: bool = True,
) -> Planner:
    """根据配置创建 Planner，支持自动降级。

    优先级：通用 OpenAI 兼容 → DGX Spark → 魔搭 API → Mock

    Args:
        config: 配置 dict，可覆盖环境变量
        auto_downgrade: 是否自动降级（测试时可以关）

    Returns:
        Planner 对象，附加 .brain_info 属性记录实际使用的后端
    """
    config = config or {}

    # 从配置或环境变量读取
    llm_base = config.get("llm_api_base") or os.environ.get("LLM_API_BASE", "")
    llm_key = config.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    llm_model = config.get("llm_model") or os.environ.get("LLM_MODEL", "qwen-max-latest")

    vlm_base = config.get("vlm_api_base") or os.environ.get("VLM_API_BASE", "") or llm_base
    vlm_key = config.get("vlm_api_key") or os.environ.get("VLM_API_KEY", "") or llm_key
    vlm_model = config.get("vlm_model") or os.environ.get("VLM_MODEL", "qwen-vl-max-latest")

    dgx_host = config.get("dgx_sparks_host") or os.environ.get("DGX_SPARKS_HOST", "")
    modelscope_key = config.get("modelscope_api_key") or os.environ.get("MODEL_SCOPE_API_KEY", "")
    timeout = int(config.get("timeout") or os.environ.get("LLM_TIMEOUT", "10"))

    # 候选后端列表（优先级从高到低）
    candidates: list[tuple[str, str, str, str]] = []  # (source, base_url, api_key, llm_model)

    # 1. 通用 OpenAI 兼容接口（最灵活，用户自定义）
    if llm_base and llm_key:
        candidates.append((
            "openai-compatible",
            llm_base.rstrip("/"),
            llm_key,
            llm_model,
        ))

    # 2. DGX Spark 本地推理
    if dgx_host:
        candidates.append((
            "dgx-spark",
            f"http://{dgx_host}:30000/v1",
            "sk-local",
            llm_model,
        ))

    # 3. 魔搭 API
    if modelscope_key:
        candidates.append((
            "modelscope",
            "https://api-inference.modelscope.cn/v1",
            modelscope_key,
            llm_model,
        ))

    if not candidates:
        # 啥都没配，直接用 mock
        return _create_mock_brain(llm_model, vlm_model)

    if not auto_downgrade:
        # 不自动降级，就用第一个
        source, base_url, key, model = candidates[0]
        llm = LLMClient(
            base_url=base_url,
            api_key=key,
            model=model,
            timeout=timeout,
        )
        vlm = VLMClient(base_url=vlm_base or base_url, api_key=vlm_key or key, model=vlm_model)
        planner = Planner(llm=llm, vlm=vlm)
        planner.brain_info = BrainInfo(  # type: ignore[attr-defined]
            source=source,
            base_url=base_url,
            model=model,
            health_ok=True,
            health_message="skip_health_check",
        )
        planner.source = source  # type: ignore[attr-defined]
        return planner

    # 自动降级：逐个试健康检查
    last_error = ""
    for source, base_url, key, model in candidates:
        try:
            llm = LLMClient(
                base_url=base_url,
                api_key=key,
                model=model,
                timeout=timeout,
                max_retries=0,  # 降级检测时不重试，快
            )
            ok, msg = llm.health_check()
            if ok:
                llm.max_retries = 2  # 正式用时恢复重试
                vlm = VLMClient(
                    base_url=vlm_base or base_url,
                    api_key=vlm_key or key,
                    model=vlm_model,
                )
                planner = Planner(llm=llm, vlm=vlm)
                planner.brain_info = BrainInfo(  # type: ignore[attr-defined]
                    source=source,
                    base_url=base_url,
                    model=model,
                    health_ok=True,
                    health_message=msg,
                )
                planner.source = source  # type: ignore[attr-defined]
                return planner
            else:
                last_error = f"{source}: {msg}"
                llm.close()
        except Exception as e:
            last_error = f"{source}: {e}"
            continue

    # 全部失败 → 降级到 mock
    return _create_mock_brain(llm_model, vlm_model, last_error)


def _create_mock_brain(
    llm_model: str,
    vlm_model: str,
    reason: str = "",
) -> Planner:
    """创建 mock 大脑（纯本地，无网络依赖）。"""
    mock_llm = MockLLMClient(model=llm_model)
    planner = Planner(llm=mock_llm, vlm=None)  # type: ignore[arg-type]
    planner.brain_info = BrainInfo(  # type: ignore[attr-defined]
        source="mock",
        base_url="mock://local",
        model=llm_model,
        health_ok=True,
        health_message=reason or "mock fallback",
    )
    planner.source = "mock"  # type: ignore[attr-defined]
    return planner


def list_available_backends(config: dict[str, Any] | None = None) -> list[BrainInfo]:
    """列出所有可用后端及其健康状态。

    用于调试和控制台展示。
    """
    config = config or {}
    llm_base = config.get("llm_api_base") or os.environ.get("LLM_API_BASE", "")
    llm_key = config.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    llm_model = config.get("llm_model") or os.environ.get("LLM_MODEL", "qwen-max-latest")
    dgx_host = config.get("dgx_sparks_host") or os.environ.get("DGX_SPARKS_HOST", "")
    modelscope_key = config.get("modelscope_api_key") or os.environ.get("MODEL_SCOPE_API_KEY", "")
    timeout = int(config.get("timeout") or os.environ.get("LLM_TIMEOUT", "5"))

    results: list[BrainInfo] = []

    # 通用 OpenAI 兼容
    if llm_base and llm_key:
        try:
            llm = LLMClient(
                base_url=llm_base.rstrip("/"),
                api_key=llm_key,
                model=llm_model,
                timeout=timeout,
                max_retries=0,
            )
            ok, msg = llm.health_check()
            llm.close()
            results.append(BrainInfo(
                source="openai-compatible",
                base_url=llm_base.rstrip("/"),
                model=llm_model,
                health_ok=ok,
                health_message=msg,
            ))
        except Exception as e:
            results.append(BrainInfo(
                source="openai-compatible",
                base_url=llm_base.rstrip("/"),
                model=llm_model,
                health_ok=False,
                health_message=str(e),
            ))

    # DGX Spark
    if dgx_host:
        base_url = f"http://{dgx_host}:30000/v1"
        try:
            llm = LLMClient(
                base_url=base_url,
                api_key="sk-local",
                model=llm_model,
                timeout=timeout,
                max_retries=0,
            )
            ok, msg = llm.health_check()
            llm.close()
            results.append(BrainInfo(
                source="dgx-spark",
                base_url=base_url,
                model=llm_model,
                health_ok=ok,
                health_message=msg,
            ))
        except Exception as e:
            results.append(BrainInfo(
                source="dgx-spark",
                base_url=base_url,
                model=llm_model,
                health_ok=False,
                health_message=str(e),
            ))

    # 魔搭 API
    if modelscope_key:
        base_url = "https://api-inference.modelscope.cn/v1"
        try:
            llm = LLMClient(
                base_url=base_url,
                api_key=modelscope_key,
                model=llm_model,
                timeout=timeout,
                max_retries=0,
            )
            ok, msg = llm.health_check()
            llm.close()
            results.append(BrainInfo(
                source="modelscope",
                base_url=base_url,
                model=llm_model,
                health_ok=ok,
                health_message=msg,
            ))
        except Exception as e:
            results.append(BrainInfo(
                source="modelscope",
                base_url=base_url,
                model=llm_model,
                health_ok=False,
                health_message=str(e),
            ))

    # Mock 永远可用
    results.append(BrainInfo(
        source="mock",
        base_url="mock://local",
        model=llm_model,
        health_ok=True,
        health_message="mock always available",
    ))

    return results
