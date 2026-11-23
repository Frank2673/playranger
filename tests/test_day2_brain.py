"""Day 2 集成测试：LLM 客户端 + Planner + ActionValidator + brain_factory。

全部测试不依赖外部服务，使用 mock / 本地验证。
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field


# ── LLMClient 单元测试 ──────────────────────────────────────


class TestLLMClient:
    """LLM 客户端测试。"""

    def test_build_payload_default(self):
        from playranger.core.brain.llm_client import LLMClient

        llm = LLMClient(base_url="http://test/v1", model="test-model")
        payload = llm._build_payload([{"role": "user", "content": "hi"}])

        assert payload["model"] == "test-model"
        assert payload["messages"] == [{"role": "user", "content": "hi"}]
        assert "temperature" in payload
        assert "response_format" not in payload

    def test_build_payload_json_mode(self):
        from playranger.core.brain.llm_client import LLMClient

        llm = LLMClient(base_url="http://test/v1")
        payload = llm._build_payload([], response_format="json")

        assert payload["response_format"] == {"type": "json_object"}

    def test_extract_text_ok(self):
        from playranger.core.brain.llm_client import LLMClient

        data = {"choices": [{"message": {"content": "hello"}}]}
        assert LLMClient._extract_text(data) == "hello"

    def test_extract_text_missing(self):
        from playranger.core.brain.llm_client import LLMClient, LLMError

        with pytest.raises(LLMError):
            LLMClient._extract_text({"choices": []})


# ── Planner pydantic 模型测试 ───────────────────────────────


class TestPlannerModels:
    """Planner 的 pydantic 输出模型测试。"""

    def test_game_action_required_fields(self):
        from playranger.core.brain.planner import GameAction

        # 必须有 action_type 和 reason
        a = GameAction(action_type="click", target="#btn", reason="test")
        assert a.action_type == "click"
        assert a.target == "#btn"
        assert a.reason == "test"
        assert a.params == {}

    def test_game_action_defaults(self):
        from playranger.core.brain.planner import GameAction

        a = GameAction(action_type="wait", reason="waiting")
        assert a.target is None
        assert a.params == {}

    def test_plan_output_confidence_range(self):
        from playranger.core.brain.planner import PlanOutput, GameAction
        from pydantic import ValidationError

        # confidence 必须在 0-1 之间
        with pytest.raises(ValidationError):
            PlanOutput(
                thought="test",
                action=GameAction(action_type="click", reason="t"),
                expected_after="x",
                confidence=1.5,
            )

    def test_reflection_output_boolean(self):
        from playranger.core.brain.planner import ReflectionOutput

        r = ReflectionOutput(
            failure_root_cause="x",
            strategy_adjustment="y",
            should_retry=True,
            next_attempt_focus="z",
        )
        assert r.should_retry is True


# ── Planner 方法测试（用 MockLLMClient）────────────────────


class TestPlanner:
    """Planner 功能测试，使用 MockLLMClient。"""

    def test_plan_returns_plan_output(self):
        from playranger.core.brain.brain_factory import MockLLMClient
        from playranger.core.brain.planner import Planner, PlanOutput

        mock_llm = MockLLMClient()
        planner = Planner(llm=mock_llm)

        result = planner.plan(
            state={"hp": 100, "level": 1},
            goal="通关游戏",
            available_actions=["click", "keypress", "wait"],
            step=0,
            max_steps=50,
        )

        assert isinstance(result, PlanOutput)
        assert result.action.action_type == "wait"
        assert result.confidence == 0.0

    def test_plan_message_includes_goal_and_state(self):
        from playranger.core.brain.planner import Planner

        msg = Planner._build_plan_message(
            state={"hp": 80},
            goal="测试目标",
            available_actions=["click"],
            history=[],
            step=5,
            max_steps=100,
        )

        assert "测试目标" in msg
        assert "hp" in msg
        assert "5 / 100" in msg
        assert "click" in msg

    def test_plan_message_history_truncation(self):
        from playranger.core.brain.planner import Planner

        history = [
            {"step": i, "action": {"action_type": "click"}, "result": "ok"}
            for i in range(20)
        ]
        msg = Planner._build_plan_message(
            state={},
            goal="g",
            available_actions=[],
            history=history,
            step=20,
            max_steps=100,
        )

        # 只显示最近 10 步
        assert "step 10" in msg
        assert "step 0" not in msg

    def test_replan_returns_replan_output(self):
        from playranger.core.brain.brain_factory import MockLLMClient
        from playranger.core.brain.planner import Planner, ReplanOutput

        mock_llm = MockLLMClient()
        planner = Planner(llm=mock_llm)

        result = planner.replan(
            state={"hp": 0},
            goal="通关",
            failure_reason="按钮点不到",
            available_actions=["wait"],
        )

        assert isinstance(result, ReplanOutput)
        assert result.action.action_type == "wait"

    def test_reflect_returns_reflection_output(self):
        from playranger.core.brain.brain_factory import MockLLMClient
        from playranger.core.brain.planner import Planner, ReflectionOutput

        mock_llm = MockLLMClient()
        planner = Planner(llm=mock_llm)

        action_seq = [
            {"action": "click", "step": i}
            for i in range(15)
        ]
        result = planner.reflect(
            goal="通关",
            action_seq=action_seq,
            failure_reason="卡关",
        )

        assert isinstance(result, ReflectionOutput)
        assert result.should_retry is False
        assert len(planner.reflection_log) == 1

    def test_set_context(self):
        from playranger.core.brain.brain_factory import MockLLMClient
        from playranger.core.brain.planner import Planner

        mock_llm = MockLLMClient()
        planner = Planner(llm=mock_llm)

        planner.set_context(["上次失败是因为卡关", "这次换个方向"])
        assert len(planner.reflection_log) == 2
        assert "卡关" in planner.reflection_log[0]


# ── ActionValidator 测试 ────────────────────────────────────


class TestActionValidator:
    """动作语义校验器测试（P0 级）。"""

    def test_whitelist_pass(self):
        from playranger.core.engines.action_validator import ActionValidator
        from playranger.core.brain.planner import GameAction

        v = ActionValidator()
        v.set_available_actions(["click", "wait"])

        action = GameAction(action_type="click", reason="t")
        result = v.validate(action, {})
        assert result.ok is True

    def test_whitelist_fail(self):
        from playranger.core.engines.action_validator import ActionValidator
        from playranger.core.brain.planner import GameAction

        v = ActionValidator()
        v.set_available_actions(["click", "wait"])

        action = GameAction(action_type="jump", reason="t")
        result = v.validate(action, {})
        assert result.ok is False
        assert result.failed_layer == "whitelist"
        assert "jump" in result.reason
        assert "click" in result.suggestion

    def test_empty_whitelist_means_no_restriction(self):
        from playranger.core.engines.action_validator import ActionValidator
        from playranger.core.brain.planner import GameAction

        v = ActionValidator()
        # 不设置白名单 → 不限制
        action = GameAction(action_type="anything", reason="t")
        result = v.validate(action, {})
        assert result.ok is True

    def test_empty_action_type_fails(self):
        from playranger.core.engines.action_validator import ActionValidator
        from playranger.core.brain.planner import GameAction

        v = ActionValidator()
        v.set_available_actions(["click"])

        action = GameAction(action_type="", reason="t")
        result = v.validate(action, {})
        assert result.ok is False
        assert result.failed_layer == "whitelist"

    def test_precondition_pass(self):
        from playranger.core.engines.action_validator import (
            ActionValidator,
            require_state_flag,
        )
        from playranger.core.brain.planner import GameAction

        v = ActionValidator()
        v.set_available_actions(["jump"])
        v.add_precondition("jump", require_state_flag("alive", True))

        action = GameAction(action_type="jump", reason="t")
        result = v.validate(action, {"alive": True})
        assert result.ok is True

    def test_precondition_fail(self):
        from playranger.core.engines.action_validator import (
            ActionValidator,
            require_state_flag,
        )
        from playranger.core.brain.planner import GameAction

        v = ActionValidator()
        v.set_available_actions(["jump"])
        v.add_precondition("jump", require_state_flag("alive", True))

        action = GameAction(action_type="jump", reason="t")
        result = v.validate(action, {"alive": False})
        assert result.ok is False
        assert result.failed_layer == "precondition"
        assert "alive" in result.reason

    def test_global_precondition(self):
        from playranger.core.engines.action_validator import (
            ActionValidator,
            require_state_flag,
        )
        from playranger.core.brain.planner import GameAction

        v = ActionValidator()
        v.set_available_actions(["click", "wait"])
        v.add_global_precondition(require_state_flag("game_loaded", True))

        action = GameAction(action_type="click", reason="t")
        # 游戏没加载，任何动作都不行
        result = v.validate(action, {"game_loaded": False})
        assert result.ok is False
        assert result.failed_layer == "precondition"
        assert "全局" in result.reason

    def test_param_range_pass(self):
        from playranger.core.engines.action_validator import (
            ActionValidator,
            require_param_range,
        )
        from playranger.core.brain.planner import GameAction

        v = ActionValidator()
        v.set_available_actions(["set_volume"])
        v.add_param_validator("set_volume", require_param_range("value", 0, 100))

        action = GameAction(
            action_type="set_volume",
            params={"value": 50},
            reason="t",
        )
        result = v.validate(action, {})
        assert result.ok is True

    def test_param_range_fail_high(self):
        from playranger.core.engines.action_validator import (
            ActionValidator,
            require_param_range,
        )
        from playranger.core.brain.planner import GameAction

        v = ActionValidator()
        v.set_available_actions(["set_volume"])
        v.add_param_validator("set_volume", require_param_range("value", 0, 100))

        action = GameAction(
            action_type="set_volume",
            params={"value": 150},
            reason="t",
        )
        result = v.validate(action, {})
        assert result.ok is False
        assert result.failed_layer == "params"

    def test_param_in_allowlist(self):
        from playranger.core.engines.action_validator import (
            ActionValidator,
            require_param_in,
        )
        from playranger.core.brain.planner import GameAction

        v = ActionValidator()
        v.set_available_actions(["set_difficulty"])
        v.add_param_validator(
            "set_difficulty",
            require_param_in("level", ["easy", "normal", "hard"]),
        )

        # 合法
        action = GameAction(
            action_type="set_difficulty",
            params={"level": "hard"},
            reason="t",
        )
        assert v.validate(action, {}).ok is True

        # 不合法
        action2 = GameAction(
            action_type="set_difficulty",
            params={"level": "impossible"},
            reason="t",
        )
        result = v.validate(action2, {})
        assert result.ok is False
        assert result.failed_layer == "params"


# ── brain_factory 测试 ──────────────────────────────────────


class TestBrainFactory:
    """大脑工厂测试。"""

    def test_create_mock_when_no_config(self, monkeypatch):
        """没配置任何后端时，应该返回 mock。"""
        from playranger.core.brain.brain_factory import create_brain

        monkeypatch.delenv("DGX_SPARKS_HOST", raising=False)
        monkeypatch.delenv("MODEL_SCOPE_API_KEY", raising=False)

        planner = create_brain({})
        assert planner.source == "mock"  # type: ignore[attr-defined]
        assert hasattr(planner, "brain_info")
        assert planner.brain_info.source == "mock"  # type: ignore[attr-defined]

    def test_create_mock_auto_downgrade_false_no_config(self, monkeypatch):
        """没配置 + 不自动降级 → 还是 mock。"""
        from playranger.core.brain.brain_factory import create_brain

        monkeypatch.delenv("DGX_SPARKS_HOST", raising=False)
        monkeypatch.delenv("MODEL_SCOPE_API_KEY", raising=False)

        planner = create_brain({}, auto_downgrade=False)
        assert planner.source == "mock"  # type: ignore[attr-defined]

    def test_list_available_backends_always_has_mock(self, monkeypatch):
        from playranger.core.brain.brain_factory import list_available_backends

        monkeypatch.delenv("DGX_SPARKS_HOST", raising=False)
        monkeypatch.delenv("MODEL_SCOPE_API_KEY", raising=False)

        backends = list_available_backends({})
        # 至少有 mock
        sources = [b.source for b in backends]
        assert "mock" in sources
        # mock 永远健康
        mock_info = next(b for b in backends if b.source == "mock")
        assert mock_info.health_ok is True

    def test_mock_llm_plan_output(self):
        from playranger.core.brain.brain_factory import MockLLMClient
        from playranger.core.brain.planner import PlanOutput

        mock = MockLLMClient()
        result = mock.chat_pydantic([], PlanOutput)
        assert isinstance(result, PlanOutput)
        assert result.action.action_type == "wait"

    def test_mock_llm_reflection_output(self):
        from playranger.core.brain.brain_factory import MockLLMClient
        from playranger.core.brain.planner import ReflectionOutput

        mock = MockLLMClient()
        result = mock.chat_pydantic([], ReflectionOutput)
        assert isinstance(result, ReflectionOutput)
        assert result.should_retry is False
