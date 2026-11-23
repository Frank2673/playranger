"""检查大脑后端状态 + 做一次真实规划测试。

用法：
    python tools/check_brain.py

会自动加载项目根目录的 .env 文件，然后：
1. 列出所有配置了的后端及其健康状态
2. 用第一个可用的后端做一次 plan() 调用测试
3. 打印 Planner 的实际输出
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 确保能 import playranger
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 加载 .env（如果有 python-dotenv）
try:
    from dotenv import load_dotenv
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✅ 已加载 .env 文件: {env_path}")
    else:
        print("ℹ️  没找到 .env 文件，使用系统环境变量")
except ImportError:
    print("ℹ️  python-dotenv 未安装，使用系统环境变量")
    print("   安装命令: pip install python-dotenv")

from playranger.core.brain.brain_factory import list_available_backends, create_brain

print()
print("=" * 60)
print("后端健康状态检查")
print("=" * 60)

backends = list_available_backends()
for b in backends:
    status = "✅" if b.health_ok else "❌"
    print(f"  {status} [{b.source:18s}] {b.model:20s}  {b.base_url}")
    if b.health_message:
        print(f"         消息: {b.health_message}")

print()
print("=" * 60)
print("真实规划调用测试")
print("=" * 60)

planner = create_brain()
print(f"使用后端: {planner.brain_info.source}")
print(f"模型: {planner.brain_info.model}")
print()

# 模拟一个游戏状态，测试 plan
fake_state = {
    "scene": "menu",
    "hp": 100,
    "score": 0,
    "level": 0,
    "game_over": False,
    "victory": False,
    "available_actions": ["click", "wait", "keypress"],
    "interactables": [
        {"id": "#start-btn", "type": "button", "text": "开始游戏"},
    ],
}
fake_goal = "开始游戏，进入第一关"
history: list[dict] = []

print(f"目标: {fake_goal}")
print(f"状态: scene={fake_state['scene']}, hp={fake_state['hp']}")
print(f"可用动作: {fake_state['available_actions']}")
print()
print("正在思考...")

result = planner.plan(
    goal=fake_goal,
    state=fake_state,
    history=history,
    available_actions=fake_state["available_actions"],
)

print()
print("📋 规划结果:")
print(f"  思考过程: {result.thought}")
print(f"  动作类型: {result.action.action_type}")
if result.action.target:
    print(f"  动作目标: {result.action.target}")
if result.action.params:
    print(f"  动作参数: {result.action.params}")
print(f"  动作理由: {result.action.reason}")
print(f"  预期结果: {result.expected_after}")
print(f"  置信度: {result.confidence:.0%}")

print()
print("✅ 规划调用成功！")
