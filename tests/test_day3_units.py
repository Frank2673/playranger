"""Day 3 单元测试：PlayEngine / GameState / StepRecord 的纯逻辑测试。

不启动真实浏览器，只测数据结构和内部逻辑。
"""

from __future__ import annotations

import json

import pytest


class TestGameState:
    """GameState 数据类测试。"""

    def test_from_dict_full(self):
        from playranger.core.engines.play_engine import GameState

        data = {
            "scene": "level1",
            "hp": 80,
            "score": 150,
            "level": 1,
            "game_over": False,
            "victory": False,
            "available_actions": ["click", "wait"],
            "interactables": [{"id": "btn", "type": "button"}],
            "extra": "field",
        }
        state = GameState.from_dict(data)

        assert state.scene == "level1"
        assert state.hp == 80
        assert state.score == 150
        assert state.level == 1
        assert state.game_over is False
        assert state.victory is False
        assert state.available_actions == ["click", "wait"]
        assert len(state.interactables) == 1
        assert state.raw["extra"] == "field"

    def test_from_dict_defaults(self):
        from playranger.core.engines.play_engine import GameState

        state = GameState.from_dict({})

        assert state.scene == "unknown"
        assert state.hp == 0
        assert state.score == 0
        assert state.level == 0
        assert state.game_over is False
        assert state.victory is False
        assert state.available_actions == []
        assert state.interactables == []

    def test_from_dict_type_coercion(self):
        """字符串数字应该被转成 int。"""
        from playranger.core.engines.play_engine import GameState

        state = GameState.from_dict({
            "hp": "100",
            "score": "50",
            "level": "3",
            "game_over": "true",
            "victory": 1,
        })
        assert state.hp == 100
        assert state.score == 50
        assert state.level == 3
        # bool("true") 是 True，但 bool(0) 是 False
        # 这里确认 from_dict 的行为
        assert isinstance(state.game_over, bool)


class TestStepRecord:
    """StepRecord 数据类测试。"""

    def test_default_fields(self):
        from playranger.core.engines.play_engine import StepRecord
        import time

        before = time.time()
        record = StepRecord(
            logic_step=1,
            action_type="click",
            action_target="#btn",
            action_params={"x": 1},
            state_before={"hp": 100},
            state_after={"hp": 90},
            ok=True,
        )
        after = time.time()

        assert record.logic_step == 1
        assert record.action_type == "click"
        assert record.error is None
        assert record.screenshot_before is None
        assert before <= record.timestamp <= after

    def test_failed_record(self):
        from playranger.core.engines.play_engine import StepRecord

        record = StepRecord(
            logic_step=5,
            action_type="click",
            action_target="#missing",
            action_params={},
            state_before={},
            state_after={},
            ok=False,
            error="element not found",
        )

        assert record.ok is False
        assert record.error == "element not found"
        assert record.logic_step == 5


class TestActionValidatorIntegration:
    """ActionValidator 和 GameState 的集成测试。"""

    def test_validate_against_state_available_actions(self):
        """用 state.available_actions 做白名单。"""
        from playranger.core.engines.action_validator import ActionValidator
        from playranger.core.engines.play_engine import GameState
        from playranger.core.brain.planner import GameAction

        state = GameState.from_dict({
            "scene": "menu",
            "hp": 100,
            "available_actions": ["click", "wait"],
        })

        validator = ActionValidator()
        validator.set_available_actions(state.available_actions)

        # 合法动作
        action = GameAction(action_type="click", target="#start-btn", reason="t")
        assert validator.validate(action, state.raw).ok is True

        # 非法动作
        action2 = GameAction(action_type="swipe", reason="t")
        result = validator.validate(action2, state.raw)
        assert result.ok is False
        assert result.failed_layer == "whitelist"

    def test_precondition_uses_game_state(self):
        """前置条件可以用游戏状态判断。"""
        from playranger.core.engines.action_validator import (
            ActionValidator,
            require_state_flag,
        )
        from playranger.core.engines.play_engine import GameState
        from playranger.core.brain.planner import GameAction

        state_alive = GameState.from_dict({
            "scene": "level1",
            "hp": 50,
            "game_over": False,
            "available_actions": ["jump"],
        })
        state_dead = GameState.from_dict({
            "scene": "level1",
            "hp": 0,
            "game_over": True,
            "available_actions": ["jump"],
        })

        validator = ActionValidator()
        validator.set_available_actions(["jump"])
        # game_over 为 False 才能跳
        validator.add_precondition("jump", require_state_flag("game_over", False))

        action = GameAction(action_type="jump", reason="t")

        # 活着的时候可以跳
        assert validator.validate(action, state_alive.raw).ok is True
        # 死了不能跳
        result = validator.validate(action, state_dead.raw)
        assert result.ok is False
        assert "game_over" in result.reason


class TestTraceSerialization:
    """轨迹序列化测试。"""

    def test_step_record_is_json_serializable(self):
        from playranger.core.engines.play_engine import StepRecord

        record = StepRecord(
            logic_step=1,
            action_type="click",
            action_target="#btn",
            action_params={"idx": 42},
            state_before={"hp": 100},
            state_after={"hp": 80},
            ok=True,
            error=None,
            screenshot_before=None,
            screenshot_after=None,
        )

        # 把 record 的核心字段打包成 dict，应该能 JSON 序列化
        data = {
            "logic_step": record.logic_step,
            "action_type": record.action_type,
            "action_target": record.action_target,
            "action_params": record.action_params,
            "state_before": record.state_before,
            "state_after": record.state_after,
            "ok": record.ok,
            "error": record.error,
            "timestamp": record.timestamp,
        }

        s = json.dumps(data)
        parsed = json.loads(s)
        assert parsed["logic_step"] == 1
        assert parsed["action_params"]["idx"] == 42
        assert parsed["ok"] is True
        assert parsed["error"] is None
        assert isinstance(parsed["timestamp"], float)

    def test_jsonl_trace_roundtrip(self):
        """轨迹可以存成 JSONL 再读回来。"""
        from playranger.core.engines.play_engine import StepRecord
        import io

        records = [
            StepRecord(
                logic_step=i,
                action_type="click",
                action_target=f"#btn-{i}",
                action_params={"idx": i},
                state_before={"step": i - 1},
                state_after={"step": i},
                ok=True,
            )
            for i in range(1, 4)
        ]

        # 写 JSONL
        buf = io.StringIO()
        for r in records:
            line = json.dumps({
                "logic_step": r.logic_step,
                "action_type": r.action_type,
                "action_target": r.action_target,
                "action_params": r.action_params,
                "state_before": r.state_before,
                "state_after": r.state_after,
                "ok": r.ok,
                "error": r.error,
            })
            buf.write(line + "\n")

        # 读回来
        buf.seek(0)
        loaded = []
        for line in buf:
            loaded.append(json.loads(line))

        assert len(loaded) == 3
        assert loaded[0]["logic_step"] == 1
        assert loaded[2]["action_target"] == "#btn-3"
        assert all(r["ok"] for r in loaded)


class TestReplayerAssertions:
    """Replayer 断言函数的纯单元测试（不需要浏览器）。"""

    def test_build_assertion_state_equals_pass(self):
        from playranger.core.agent.replayer import build_assertion_from_dict

        fn = build_assertion_from_dict({"state_equals": {"hp": 100, "scene": "menu"}})
        ok, reason = fn({"hp": 100, "scene": "menu", "score": 0})
        assert ok is True
        assert "passed" in reason

    def test_build_assertion_state_equals_fail(self):
        from playranger.core.agent.replayer import build_assertion_from_dict

        fn = build_assertion_from_dict({"state_equals": {"hp": 100}})
        ok, reason = fn({"hp": 50})
        assert ok is False
        assert "hp" in reason

    def test_build_assertion_state_less_than(self):
        from playranger.core.agent.replayer import build_assertion_from_dict

        fn = build_assertion_from_dict({"state_less_than": {"hp": 100}})
        assert fn({"hp": 50})[0] is True
        assert fn({"hp": 100})[0] is False  # 等于不算 <
        assert fn({"hp": 150})[0] is False

    def test_build_assertion_state_greater_than(self):
        from playranger.core.agent.replayer import build_assertion_from_dict

        fn = build_assertion_from_dict({"state_greater_than": {"score": 0}})
        assert fn({"score": 100})[0] is True
        assert fn({"score": 0})[0] is False  # 等于不算 >
        assert fn({"score": -10})[0] is False

    def test_build_assertion_nonnumeric_less_than(self):
        """非数字值应该返回失败。"""
        from playranger.core.agent.replayer import build_assertion_from_dict

        fn = build_assertion_from_dict({"state_less_than": {"scene": 100}})
        ok, _ = fn({"scene": "menu"})
        assert ok is False

    def test_build_assertion_empty(self):
        """空断言应该返回 True。"""
        from playranger.core.agent.replayer import build_assertion_from_dict

        fn = build_assertion_from_dict({})
        ok, _ = fn({"hp": 100})
        assert ok is True


class TestReproductionReport:
    """ReproductionReport 的纯单元测试。"""

    def test_is_reproducible_threshold(self):
        from playranger.core.agent.replayer import ReproductionReport

        # 80% 以上为可复现
        r = ReproductionReport(bug_id="BUG-001", total_runs=10, success_runs=8, reproduction_rate=0.8)
        assert r.is_reproducible is True

        r = ReproductionReport(bug_id="BUG-001", total_runs=10, success_runs=7, reproduction_rate=0.7)
        assert r.is_reproducible is False

    def test_zero_runs(self):
        from playranger.core.agent.replayer import ReproductionReport

        r = ReproductionReport(bug_id="BUG-001", total_runs=0, success_runs=0, reproduction_rate=0.0)
        assert r.is_reproducible is False
