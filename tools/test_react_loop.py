"""ReActLoop 快速验证脚本。"""
import sys
import os
# 把项目根目录加到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playranger.core.agent.react_loop import ReActLoop, react_loop, StepInfo, LoopResult
print('✅ ReActLoop import OK')

# 测试 1: 全 mock 模式（无 engine 无 planner）
result = react_loop('测试目标', max_steps=5)
print(f'✅ 全 mock: status={result["status"]}, steps={result["total_steps"]}')

# 测试 2: 只有 mock planner
from playranger.core.brain.brain_factory import create_brain
planner = create_brain()  # 没有 API key，会降级到 mock
print(f'✅ Planner created: source={planner.source}')

loop = ReActLoop(planner=planner)
loop_result = loop.run('探索游戏', max_steps=3, initial_state_override={
    'scene': 'menu',
    'hp': 100,
    'score': 0,
    'level': 1,
    'game_over': False,
    'victory': False,
    'available_actions': ['click', 'wait', 'keypress'],
})
print(f'✅ 有 Planner 的 mock 运行: status={loop_result.status}, steps={loop_result.total_steps}')
print(f'   l1={loop_result.l1_retries}, l2={loop_result.l2_retries}, l3={loop_result.l3_retries}')
print(f'   conclusion={loop_result.conclusion}')
print(f'   history 长度: {len(loop_result.history)}')

# 测试 3: 带 ActionValidator
from playranger.core.engines.action_validator import ActionValidator
validator = ActionValidator()
validator.set_available_actions(['click', 'wait', 'keypress'])
loop2 = ReActLoop(planner=planner, validator=validator)
loop_result2 = loop2.run('找到 bug', max_steps=5, initial_state_override={
    'scene': 'menu',
    'hp': 100,
    'score': 0,
    'level': 1,
    'game_over': False,
    'victory': False,
    'available_actions': ['click', 'wait', 'keypress'],
})
print(f'✅ 带 Validator: status={loop_result2.status}, steps={loop_result2.total_steps}')

# 测试 4: 带 BugRecorder（L2 连续失败触发 bug 记录）
import tempfile
from playranger.core.agent.recorder import BugRecorder

with tempfile.TemporaryDirectory() as tmpdir:
    recorder = BugRecorder(output_dir=os.path.join(tmpdir, "bugs"))
    # 用很小的 max_l2_retries 快速触发 L3
    loop3 = ReActLoop(
        planner=planner,
        recorder=recorder,
        max_l2_retries=2,
        max_l3_retries=1,
    )
    # mock planner 总是返回 wait，verifier 默认通过，所以 L2 不会触发
    # 我们验证一下正常流程不报错
    loop_result3 = loop3.run('测试 bug recorder', max_steps=3, initial_state_override={
        'scene': 'menu', 'hp': 100, 'score': 0, 'level': 1,
        'game_over': False, 'victory': False,
        'available_actions': ['wait'],
    })
    print(f'✅ 带 Recorder: status={loop_result3.status}, bugs={len(loop_result3.bugs_found)}')

# 测试 5: L1 重试触发（给一个非法动作的 validator）
class BadPlanner:
    """总是返回非法动作的 planner，用来测试 L1 重试。"""
    def plan(self, state, goal, available_actions=None, history=None, step=0, max_steps=100):
        from playranger.core.brain.planner import PlanOutput, GameAction
        return PlanOutput(
            thought="bad action",
            action=GameAction(
                action_type="invalid_action_xyz",
                target=None,
                params={},
                reason="testing l1 retry",
            ),
            expected_after="should fail validation",
            confidence=0.5,
        )
    def replan(self, state, goal, failure_reason, available_actions=None, failed_attempts=1):
        from playranger.core.brain.planner import ReplanOutput, GameAction
        # 还是返回非法动作，用来测试 L1 用尽
        return ReplanOutput(
            thought="still bad",
            new_approach="still invalid",
            action=GameAction(
                action_type="also_invalid",
                reason="still testing",
            ),
            expected_after="fail again",
        )
    def reflect(self, goal, action_seq, failure_reason, evidence=None):
        from playranger.core.brain.planner import ReflectionOutput
        return ReflectionOutput(
            failure_root_cause="test",
            strategy_adjustment="test",
            should_retry=False,
            next_attempt_focus="test",
        )

validator2 = ActionValidator()
validator2.set_available_actions(['click', 'wait'])
loop4 = ReActLoop(
    planner=BadPlanner(),  # type: ignore
    validator=validator2,
    max_l1_retries=2,
    max_l2_retries=2,
    max_l3_retries=1,
)
loop_result4 = loop4.run('测试 L1 重试', max_steps=5, initial_state_override={
    'scene': 'menu', 'hp': 100, 'score': 0, 'level': 1,
    'game_over': False, 'victory': False,
    'available_actions': ['click', 'wait'],
})
print(f'✅ L1 重试测试: status={loop_result4.status}, l1_retries={loop_result4.l1_retries}')
print(f'   steps={loop_result4.total_steps}, conclusion={loop_result4.conclusion}')

print()
print('🎉 所有 ReActLoop 基本测试通过！')
