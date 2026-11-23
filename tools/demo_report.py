"""生成一份可直接打开的样例报告（无需浏览器 / 无需 LLM）。

用途：
- 评审 / 合作方想先看「报告长什么样」，不用真的跑一局游戏
- 新手开发者对照样例理解缺陷包结构与修复方案字段

产出：
    output/demo/bugs/BUG-00X/...    # 标准缺陷包（含证据链、溯源、修复方案）
    output/demo/report.html         # 单文件离线报告
    output/demo/tickets/BUG-00X.md  # 可直接贴进 Issue 的缺陷单

注意：数据为演示用途，用于展示报告结构，不代表真实测试结论。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playranger.core.agent.recorder import BugRecorder
from tools.build_report import find_bug_dirs, load_bug_entry
from tools.build_report import main as build_main

# 1x1 透明 PNG，仅用于演示证据链与截图区块（真实测试会写入实际截图）
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
)


def _trace(*steps: tuple[int, str, str, dict, dict, bool, str]) -> list[dict]:
    """把 (step, action_type, target, state, final_state, ok, reason) 拼成 StepInfo 结构。"""
    trace: list[dict] = []
    for step, action_type, target, state, final_state, ok, reason in steps:
        trace.append({
            "step": step,
            "state": state,
            "plan": {"reasoning": "演示数据", "confidence": 0.8},
            "action": {"action_type": action_type, "target": target, "params": {}},
            "validation": {"ok": True},
            "execution_ok": True,
            "execution_error": None,
            "verification": {"success": ok, "reason": reason, "final_state": final_state},
            "correction_level": "none" if ok else "L2_path",
        })
    return trace


def _ensure_png(path: Path) -> None:
    """写入演示截图；文件被占用时复用已有文件（不影响报告生成）。"""
    try:
        path.write_bytes(_PNG)
    except PermissionError:
        if not path.exists():
            raise


def main() -> int:
    root = Path("output/demo")
    bugs_dir = root / "bugs"
    shots_dir = root / "shots"
    bugs_dir.mkdir(parents=True, exist_ok=True)
    shots_dir.mkdir(parents=True, exist_ok=True)

    # 演示截图（真实运行时由 PlayEngine 采集）
    init_shot = shots_dir / "initial.png"
    fail_shot = shots_dir / "failure.png"
    _ensure_png(init_shot)
    _ensure_png(fail_shot)

    recorder = BugRecorder(output_dir=bugs_dir)

    # 1) 阻断级：重开后 HP 未重置（SDK 状态比对）
    recorder.capture(
        title="重新开始后 HP 未重置",
        anomaly_type="hp_not_reset",
        severity="blocker",
        conclusion="auto_confirmed",
        description="点击「重新开始」后场景回到第 1 关，但 HP 仍为上一局的 40",
        initial_state={"scene": "level2", "level": 2, "hp": 40, "score": 300},
        final_state={"scene": "level1", "level": 1, "hp": 40, "score": 0},
        action_trace=_trace(
            (1, "click", "#start", {"scene": "menu", "hp": 100}, {"scene": "level1", "hp": 100}, True, ""),
            (2, "click", "#next", {"scene": "level1", "hp": 100}, {"scene": "level2", "hp": 40}, True, ""),
            (3, "click", "#restart", {"scene": "level2", "hp": 40}, {"scene": "level1", "hp": 40}, False,
             "hp 未回到初始值 100"),
        ),
        assertion={"type": "state_equals", "field": "hp", "value": 100},
        evidence={"hp_not_reset": "sdk_state"},
        screenshot_initial=str(init_shot),
        screenshot_failure=str(fail_shot),
        seed=20261113,
        game_name="DemoGame",
        tags=["auto_detected", "l2_streak_3"],
        consecutive_failures=3,
        source={"channel": "sdk_state", "origin_step": 3, "detected_by": "verifier"},
    )

    # 2) 严重级：结算分数与内部状态不一致（视觉检测）
    recorder.capture(
        title="胜利页分数显示错误",
        anomaly_type="score_mismatch",
        severity="major",
        conclusion="human_required",
        description="结算页显示 100 分，SDK 读到实际分数 300",
        initial_state={"scene": "playing", "score": 300},
        final_state={"scene": "victory", "score": 300},
        action_trace=_trace(
            (1, "click", "#finish", {"scene": "playing", "score": 300}, {"scene": "victory", "score": 300}, False,
             "结算页显示值与内部 score 不一致"),
        ),
        assertion={"type": "state_equals", "field": "score", "value": 300},
        evidence={"score_mismatch": "vision_vlm"},
        screenshot_initial=str(init_shot),
        screenshot_failure=str(fail_shot),
        seed=20261113,
        game_name="DemoGame",
        tags=["auto_detected"],
        source={"channel": "vision_vlm", "origin_step": 1, "detected_by": "vision_engine"},
    )

    # 3) 严重级：连续路径修正失败（策略卡死）
    recorder.capture(
        title="第 2 关存在死路，Agent 连续 3 次无法推进",
        anomaly_type="strategy_stuck",
        severity="major",
        conclusion="human_required",
        description="可用交互只剩一个已禁用的按钮，无法进入下一关",
        initial_state={"scene": "level2", "hp": 40, "score": 120},
        final_state={"scene": "level2", "hp": 40, "score": 120},
        action_trace=_trace(
            (1, "click", "#door", {"scene": "level2", "hp": 40}, {"scene": "level2", "hp": 40}, False,
             "点击 #door 无任何状态变化"),
            (2, "click", "#door", {"scene": "level2", "hp": 40}, {"scene": "level2", "hp": 40}, False,
             "重试仍无变化"),
            (3, "click", "#door", {"scene": "level2", "hp": 40}, {"scene": "level2", "hp": 40}, False,
             "连续 3 次失败"),
        ),
        evidence={"consecutive_l2_failures": "action_trace"},
        screenshot_initial=str(init_shot),
        screenshot_failure=str(fail_shot),
        seed=20261113,
        game_name="DemoGame",
        tags=["auto_detected", "l2_streak_3"],
        consecutive_failures=3,
        source={"channel": "strategy_stuck", "origin_step": 3, "l2_streak": 3,
                "detected_by": "react_loop.l3_strategy"},
    )

    # 4) 轻微级：结算文案溢出（视觉检测）
    recorder.capture(
        title="超长昵称导致结算卡片文字溢出",
        anomaly_type="text_overflow",
        severity="minor",
        conclusion="auto_warning",
        description="昵称超过 12 个字符时，结算卡片文字压到「再来一局」按钮",
        initial_state={"scene": "victory"},
        final_state={"scene": "victory"},
        action_trace=_trace(
            (1, "click", "#finish", {"scene": "playing"}, {"scene": "victory"}, True, ""),
        ),
        evidence={"text_overflow": "vision_vlm"},
        screenshot_initial=str(init_shot),
        screenshot_failure=str(fail_shot),
        seed=7,
        game_name="DemoGame",
        tags=["auto_detected"],
        source={"channel": "vision_vlm", "origin_step": 1, "detected_by": "vision_engine"},
    )

    bugs = [load_bug_entry(d) for d in find_bug_dirs(bugs_dir)]
    code = build_main([
        "--bugs", str(bugs_dir),
        "--out", str(root / "report.html"),
        "--tickets", str(root / "tickets"),
        "--title", "PlayRanger 样例报告（演示数据）",
        "--game", "DemoGame",
    ])
    if code != 0:
        return code

    print(f"  样例缺陷包    : {len(bugs)} 个 -> {bugs_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
