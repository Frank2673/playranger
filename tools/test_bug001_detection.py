"""BUG-001 专项测试：验证 HP 重置缺陷能否被自动发现。

BUG-001: 点击"再玩一次"后，HP 应该重置为 100，但实际保留上一局的值。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from playranger.core.engines.play_engine import PlayEngine
from playranger.core.brain.brain_factory import create_brain
from playranger.core.engines.action_validator import ActionValidator
from playranger.core.agent.react_loop import ReActLoop
from playranger.core.agent.recorder import BugRecorder

GAME_URL = "file:///E:/AI造游戏大赛/playranger/examples/demo_game/index.html"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "bug001_test")
BUGS_DIR = os.path.join(OUTPUT_DIR, "bugs")
SCREENSHOT_DIR = os.path.join(OUTPUT_DIR, "screenshots")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(BUGS_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def main():
    print("=" * 60)
    print("BUG-001 专项测试 — HP 重置缺陷检测")
    print("=" * 60)

    # 1. 启动引擎
    print("\n[1/4] 启动 PlayEngine...")
    engine = PlayEngine(url=GAME_URL, headless=True)
    engine.start()
    
    state = engine.read_state()
    print(f"  初始: scene={state.scene}, hp={state.hp}, score={state.score}")

    # 2. 创建组件
    print("\n[2/4] 创建 Planner + Validator + Recorder...")
    planner = create_brain()
    validator = ActionValidator()
    recorder = BugRecorder(output_dir=BUGS_DIR)
    print(f"  Planner: {planner.source}")

    # 3. 第一轮：玩到 Level 2，然后点"重新开始"
    print("\n[3/4] 走到 Level 2，点击'重新开始'...")
    print("-" * 60)
    
    # 先点开始
    engine.execute(action_type="click", target="#start-btn")
    s1 = engine.read_state()
    print(f"  点开始后: scene={s1.scene}, hp={s1.hp}, score={s1.score}")
    
    # 点下一关到 Level 2
    engine.execute(action_type="click", target="#level1-next")
    s2 = engine.read_state()
    print(f"  到 Level 2: scene={s2.scene}, hp={s2.hp}, score={s2.score}")
    
    # 先把 HP 调低（模拟战斗中掉血），这样重置后对比更明显
    engine.execute(action_type="set_param", params={"param": "hp", "value": 30})
    s_damaged = engine.read_state()
    print(f"  人为扣血后: hp={s_damaged.hp}")
    
    # 点"重新开始"（Level 2 的重新开始按钮）
    engine.execute(action_type="click", target="#level2-restart")
    s_after_restart = engine.read_state()
    print(f"  点重新开始后: scene={s_after_restart.scene}, hp={s_after_restart.hp}, score={s_after_restart.score}")
    
    # 验证 BUG-001
    hp_before = s_damaged.hp
    hp_after = s_after_restart.hp
    expected_hp = 100  # 重置后应该是 100
    
    print()
    print("=" * 60)
    print("🔍 BUG-001 检测结果")
    print("=" * 60)
    print(f"  重新开始前 HP: {hp_before}")
    print(f"  重新开始后 HP: {hp_after}")
    print(f"  预期 HP:      {expected_hp}")
    
    if hp_after == expected_hp:
        print("\n  ✅ HP 已正确重置，未发现 BUG-001")
        bug_detected = False
    else:
        print(f"\n  ❌ BUG-001 确认存在！HP 未重置（实际 {hp_after}，预期 {expected_hp}）")
        bug_detected = True
        
        # 手动记录一个缺陷包
        print("\n  正在生成缺陷包...")
        recorder2 = BugRecorder(output_dir=BUGS_DIR)
        bug_dir = recorder2.capture(
            title="BUG-001: Level 2 点击'重新开始'后 HP 未重置",
            anomaly_type="state_not_reset",
            severity="major",
            description="在 Level 2 场景点击'重新开始'按钮，生命值（HP）没有重置为初始值 100，而是保留了之前的值。预期：重新开始后 HP 应为 100。",
            conclusion="auto_confirmed",
            seed=engine.sdk_get_seed(),
            initial_state=s_damaged.raw,
            final_state=s_after_restart.raw,
            action_trace=[],
            assertion={
                "type": "state_equals",
                "field": "hp",
                "value": expected_hp,
                "actual": hp_after,
            },
            tags=["BUG-001", "state_reset", "hp"],
        )
        print(f"  缺陷包: {bug_dir}")

    # 关闭
    engine.close()
    
    print()
    print("=" * 60)
    print("测试完成！")
    print("=" * 60)
    
    return bug_detected


if __name__ == "__main__":
    main()
