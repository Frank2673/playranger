"""端到端测试：真实 LLM + 真实浏览器 + Demo Game 的 ReAct 循环。

目标：验证 PlayRanger 能否自动玩 Demo Game 并发现 BUG-001（HP 不重置）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from playranger.core.engines.play_engine import PlayEngine
from playranger.core.brain.brain_factory import create_brain, list_available_backends
from playranger.core.engines.action_validator import ActionValidator
from playranger.core.agent.react_loop import ReActLoop
from playranger.core.agent.recorder import BugRecorder

GAME_URL = "file:///E:/AI造游戏大赛/playranger/examples/demo_game/index.html"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "e2e_test")
BUGS_DIR = os.path.join(OUTPUT_DIR, "bugs")
SCREENSHOT_DIR = os.path.join(OUTPUT_DIR, "screenshots")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(BUGS_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def main():
    print("=" * 60)
    print("PlayRanger 端到端测试 — Demo Game + 真实 LLM")
    print("=" * 60)

    # 1. 创建 PlayEngine
    print("\n[1/5] 启动 PlayEngine...")
    engine = PlayEngine(
        url=GAME_URL,
        headless=True,
    )
    engine.start()
    print(f"  ✅ 引擎已启动，游戏 URL: {GAME_URL}")

    # 读取初始状态
    state = engine.read_state()
    print(f"  初始状态: scene={state.scene}, hp={state.hp}, score={state.score}")

    # 2. 创建 Planner（真实 LLM）
    print("\n[2/5] 创建 Planner (真实 DeepSeek LLM)...")
    
    # 先打印可用后端（调试用）
    backends = list_available_backends()
    print("  可用后端:")
    for b in backends:
        status = "✅" if b.health_ok else "❌"
        print(f"    {status} [{b.source}] {b.model} ({b.base_url})")
        if not b.health_ok:
            print(f"       原因: {b.health_message}")
    
    planner = create_brain()
    print(f"  ✅ Planner: source={planner.source}")

    # 3. 创建 ActionValidator
    print("\n[3/5] 创建 ActionValidator...")
    validator = ActionValidator()
    print("  ✅ Validator 就绪")

    # 4. 创建 BugRecorder
    print("\n[4/5] 创建 BugRecorder...")
    recorder = BugRecorder(output_dir=BUGS_DIR)
    print(f"  ✅ Recorder 就绪，输出目录: {BUGS_DIR}")

    # 5. 运行 ReAct 循环
    print("\n[5/5] 运行 ReAct 循环（最多 15 步）...")
    print("-" * 60)

    loop = ReActLoop(
        play_engine=engine,
        planner=planner,
        validator=validator,
        recorder=recorder,
        capture_screenshots=True,
        screenshot_dir=SCREENSHOT_DIR,
        max_l1_retries=2,
        max_l2_retries=3,
        max_l3_retries=2,
    )

    result = loop.run(
        goal="玩这个游戏，点击开始按钮进入游戏，然后通过点击下一关按钮推进。尝试找到游戏中的缺陷，特别是状态重置相关的问题。",
        max_steps=15,
    )

    print("-" * 60)
    print("\n📊 结果汇总:")
    print(f"  状态: {result.status}")
    print(f"  总步数: {result.total_steps}")
    print(f"  L1 重试: {result.l1_retries}")
    print(f"  L2 重试: {result.l2_retries}")
    print(f"  L3 重试: {result.l3_retries}")
    print(f"  结论: {result.conclusion}")
    print(f"  发现 BUG 数: {len(result.bugs_found)}")

    if result.bugs_found:
        print("\n🐛 发现的 BUG:")
        for i, bug in enumerate(result.bugs_found, 1):
            print(f"  {i}. [{bug.get('severity', '?')}] {bug.get('title', '?')}")
            print(f"     类型: {bug.get('anomaly_type', '?')}")
            print(f"     结论: {bug.get('conclusion', '?')}")
            if bug.get('bug_dir'):
                print(f"     缺陷包: {bug['bug_dir']}")

    print(f"\n  最终状态: {result.final_state}")
    print(f"\n  截图保存在: {SCREENSHOT_DIR}")
    print(f"  缺陷包保存在: {BUGS_DIR}")

    # 关闭引擎
    engine.close()
    print("\n✅ 引擎已关闭")

    print("\n" + "=" * 60)
    print("测试完成！")
    print("=" * 60)

    return result


if __name__ == "__main__":
    main()
