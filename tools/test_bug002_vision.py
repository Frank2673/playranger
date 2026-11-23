"""BUG-002 视觉检测验证：胜利页分数显示错误。

BUG-002: 胜利页面显示的分数是 score - 200，而不是实际 score。
例如最终得分 300，胜利页会显示 100。

验证：用 VLM 视觉引擎分析胜利页截图，提取显示的分数，
然后和 SDK 内部的真实 score 对比，看能否检测出不一致。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from playranger.core.engines.play_engine import PlayEngine
from playranger.core.brain.vlm_client import VLMClient

GAME_URL = "file:///E:/AI造游戏大赛/playranger/examples/demo_game/index.html"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "bug002_vision_test")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def main():
    print("=" * 60)
    print("BUG-002 视觉检测验证 — 胜利页分数显示错误")
    print("=" * 60)

    # 1. 启动引擎，玩到胜利页
    print("\n[1/4] 启动游戏并玩到胜利页...")
    engine = PlayEngine(url=GAME_URL, headless=True)
    engine.start()

    # 点开始
    engine.execute(action_type="click", target="#start-btn")
    s1 = engine.read_state()
    print(f"  Level 1: score={s1.score}")

    # 点下一关
    engine.execute(action_type="click", target="#level1-next")
    s2 = engine.read_state()
    print(f"  Level 2: score={s2.score}")

    # 点胜利按钮（触发胜利页面 + BUG-003 alert）
    # 先取消 alert 避免卡住
    engine._page.evaluate("window.alert = () => {}")
    engine.execute(action_type="click", target="#level2-win")
    s_win = engine.read_state()
    real_score = s_win.score
    print(f"  胜利页(SDK): score={real_score}, victory={s_win.victory}")

    # 2. 截图
    print("\n[2/4] 截取胜利页...")
    screenshot_path = os.path.join(OUTPUT_DIR, "win_screen.png")
    engine.screenshot(screenshot_path)
    print(f"  截图: {screenshot_path}")

    # 3. 用 VLM 分析截图，提取显示的分数
    print("\n[3/4] VLM 视觉分析...")
    vlm = VLMClient(
        base_url=os.environ.get("VLM_API_BASE", os.environ.get("LLM_API_BASE", "https://api.deepseek.com/v1")),
        api_key=os.environ.get("VLM_API_KEY", os.environ.get("LLM_API_KEY", "")),
        model=os.environ.get("VLM_MODEL", os.environ.get("LLM_MODEL", "deepseek-flash")),
        timeout=30,
    )

    prompt = """
    这是一张游戏胜利页面的截图。请回答：
    1. 页面中显示的最终分数是多少？（只返回数字）
    2. 简要描述你看到的分数显示位置和格式。
    
    以 JSON 格式返回：
    {
      "displayed_score": <数字，没找到则为 null>,
      "description": "<简要描述>"
    }
    """

    print("  正在分析截图...")
    vlm_result = vlm.inspect(image_path=screenshot_path, prompt=prompt)
    print(f"  VLM 原始输出:\n{vlm_result[:300]}..." if len(vlm_result) > 300 else f"  VLM 输出:\n{vlm_result}")

    # 解析 JSON
    import json
    import re

    displayed_score = None
    description = ""

    # 尝试解析 JSON
    json_match = re.search(r'\{[\s\S]*\}', vlm_result)
    if json_match:
        try:
            data = json.loads(json_match.group())
            displayed_score = data.get("displayed_score")
            description = data.get("description", "")
        except json.JSONDecodeError:
            pass

    # 如果 JSON 解析失败，用正则提取数字
    if displayed_score is None:
        # 找 "分数: X" 或 "得分: X" 之类的模式
        num_match = re.search(r'(?:分数|得分|最终).*?(\d+)', vlm_result)
        if num_match:
            displayed_score = int(num_match.group(1))

    print()
    print(f"  VLM 提取的显示分数: {displayed_score if displayed_score is not None else '未识别'}")
    if description:
        print(f"  VLM 描述: {description}")

    # 4. 对比判断
    print("\n[4/4] BUG-002 检测结果")
    print("-" * 60)
    print(f"  SDK 真实分数:  {real_score}")
    print(f"  视觉显示分数:  {displayed_score if displayed_score is not None else '?'}")
    print(f"  预期差值:      0 (应该相等)")

    if displayed_score is not None:
        diff = real_score - displayed_score
        if diff != 0:
            print(f"  实际差值:      {diff} (BUG-002 预期差值是 200)")
            if diff == 200:
                print()
                print("  ✅ BUG-002 被视觉检测确认！")
                print("     胜利页分数比实际少了 200，与 BUG-002 完全吻合。")
                bug_detected = True
            else:
                print()
                print(f"  ⚠️  分数不一致（差值 {diff}），但不完全匹配 BUG-002 的特征")
                bug_detected = True  # 也是 bug，只是不一定是 BUG-002
        else:
            print()
            print("  ❌ 未检测到 BUG-002")
            print("     显示分数与真实分数一致")
            bug_detected = False
    else:
        print()
        print("  ⚠️  VLM 未能从截图中提取分数")
        bug_detected = None

    engine.close()

    print()
    print("=" * 60)
    print("测试完成")
    print("=" * 60)

    return bug_detected


if __name__ == "__main__":
    main()
