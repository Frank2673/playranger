"""快速冒烟测试：验证所有核心模块基本功能正常。

不依赖 pytest，直接用 assert 验证。
"""
import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_game_state():
    from playranger.core.engines.play_engine import GameState
    gs = GameState.from_dict({"hp": 100, "score": 0, "scene": "menu"})
    assert gs.hp == 100
    assert gs.scene == "menu"
    assert gs.raw["hp"] == 100
    print("✅ GameState")

def test_step_record():
    from playranger.core.engines.play_engine import StepRecord
    sr = StepRecord(
        logic_step=1,
        action_type="click",
        action_target="#btn",
        action_params={},
        state_before={"hp": 100},
        state_after={"hp": 50},
        ok=True,
    )
    assert sr.action_type == "click"
    assert sr.ok is True
    # 验证 as_dict 或 dataclass 序列化
    from dataclasses import asdict
    d = asdict(sr)
    assert d["action_type"] == "click"
    assert d["ok"] is True
    print("✅ StepRecord")

def test_action_validator():
    from playranger.core.engines.action_validator import ActionValidator, ValidationResult
    from playranger.core.brain.planner import GameAction
    v = ActionValidator()
    v.set_available_actions(["click", "wait"])
    result = v.validate(GameAction(action_type="click", target="#a", reason="test"), {})
    assert result.ok is True
    result2 = v.validate(GameAction(action_type="invalid", reason="test"), {})
    assert result2.ok is False
    print("✅ ActionValidator")

def test_planner_models():
    from playranger.core.brain.planner import PlanOutput, GameAction, ReflectionOutput
    action = GameAction(action_type="click", target="#btn", reason="test")
    plan = PlanOutput(thought="test", action=action, expected_after="change", confidence=0.8)
    assert plan.confidence == 0.8
    assert plan.action.action_type == "click"
    ref = ReflectionOutput(
        failure_root_cause="test",
        strategy_adjustment="test",
        should_retry=True,
        next_attempt_focus="test",
    )
    assert ref.should_retry is True
    print("✅ PlannerModels")

def test_brain_factory_mock():
    from playranger.core.brain.brain_factory import create_brain
    # 无配置时返回 mock
    planner = create_brain(config={}, auto_downgrade=True)
    assert planner.source == "mock"
    result = planner.plan(state={"hp": 100}, goal="test")
    assert isinstance(result.thought, str)
    assert result.action.action_type  # 非空
    print("✅ BrainFactory (mock)")

def test_vision_engine_mock():
    from playranger.core.engines.vision_engine import VisionEngine, VisionResult
    ve = VisionEngine(vlm=None)  # mock 模式
    result = ve.inspect("fake_path.png")
    assert isinstance(result, VisionResult)
    assert result.is_abnormal is not None
    print("✅ VisionEngine (mock)")

def test_verifier():
    from playranger.core.agent.verifier import verify, build_assertion_from_dict
    # 用字符串预期（自然语言启发式）
    result = verify(
        expected="hp should be 100",
        new_state={"hp": 100, "score": 0},
    )
    # mock 模式下 verify 可能返回不同结果，只要不报错就行
    assert result is not None
    assertion = build_assertion_from_dict({"type": "state_equals", "field": "score", "value": 100})
    assert assertion is not None
    print("✅ Verifier")

def test_bug_recorder():
    from playranger.core.agent.recorder import BugRecorder
    with tempfile.TemporaryDirectory() as tmpdir:
        recorder = BugRecorder(output_dir=os.path.join(tmpdir, "bugs"))
        bug_dir = recorder.capture(
            title="test bug",
            anomaly_type="state_inconsistency",
            severity="major",
            description="test",
            conclusion="auto_warning",
            seed=42,
            initial_state={"hp": 100},
            final_state={"hp": 50},
            action_trace=[{"step": 1, "action": "click"}],
            assertion={"type": "state_equals", "field": "hp", "value": 100},
        )
        assert os.path.exists(bug_dir)
        assert os.path.exists(os.path.join(bug_dir, "defect.json"))
        # 读回来验证
        record = recorder.load(bug_dir)
        assert record.title == "test bug"
        assert record.severity == "major"
    print("✅ BugRecorder")

def test_replayer():
    from playranger.core.agent.replayer import ReproductionReport
    # 纯 mock 测试
    report = ReproductionReport(
        bug_id="BUG-001",
        total_runs=5,
        success_runs=5,
        reproduction_rate=1.0,
    )
    assert report.is_reproducible is True
    assert report.reproduction_rate == 1.0
    print("✅ Replayer (models)")

def test_react_loop():
    from playranger.core.agent.react_loop import ReActLoop, LoopResult
    from playranger.core.brain.brain_factory import create_brain
    planner = create_brain()  # mock
    loop = ReActLoop(planner=planner)
    result = loop.run("测试", max_steps=3, initial_state_override={
        "scene": "menu", "hp": 100,
        "available_actions": ["click", "wait"],
    })
    assert isinstance(result, LoopResult)
    assert result.total_steps == 3
    assert len(result.history) == 3
    print("✅ ReActLoop")

def test_vlm_client_import():
    from playranger.core.brain.vlm_client import VLMClient
    assert VLMClient is not None
    print("✅ VLMClient import")

if __name__ == "__main__":
    tests = [
        test_game_state,
        test_step_record,
        test_action_validator,
        test_planner_models,
        test_brain_factory_mock,
        test_vision_engine_mock,
        test_verifier,
        test_bug_recorder,
        test_replayer,
        test_react_loop,
        test_vlm_client_import,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n=== 结果: {passed} passed, {failed} failed ===")
    sys.exit(0 if failed == 0 else 1)
