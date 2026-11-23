"""`playranger native` —— 原生平台冒烟执行器。

定位：**观察优先**的可运行验证，不是完整的 LLM 决策回路。
默认只截图 + 计算帧指纹，不点击任何东西（避免误操作桌面）；
需要动作时显式加 `--auto-tap`，且只点客户区中心。

它能回答的问题：
- 适配器在这台机器上能不能连上？（能力探测）
- 画面是否在变化？（连续 N 帧指纹相同 → 疑似冻结/白屏，标注为「疑似」不判定为 bug）
- 截图证据是否可落盘、可复查？

它不能回答的问题：
- 游戏逻辑是否正确（需要断言/LLM 规划，属于 Web ReAct 回路的职责）
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from typing import Any

from playranger.core.engines.adapters import get_adapter


def _save(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def run_native_smoke(
    platform: str,
    *,
    steps: int = 5,
    interval_ms: int = 500,
    output: str = "output/native",
    auto_tap: bool = False,
    tap_xy: tuple[int, int] | None = None,
    **adapter_kwargs: Any,
) -> dict[str, Any]:
    """执行一次原生平台冒烟观察，返回结构化结果。"""
    adapter = get_adapter(platform, **adapter_kwargs)
    caps = adapter.capabilities()
    if not caps.get("available"):
        return {
            "ok": False,
            "platform": platform,
            "error": caps.get("reason") or "适配器不可用",
            "capabilities": caps,
        }

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(output, f"{platform}_{stamp}")
    shots_dir = os.path.join(run_dir, "shots")
    os.makedirs(shots_dir, exist_ok=True)

    trace_path = os.path.join(run_dir, "trace.jsonl")
    frames: list[dict[str, Any]] = []
    unchanged_streak = 0
    frozen_suspects: list[int] = []

    with open(trace_path, "a", encoding="utf-8") as trace:
        for i in range(1, max(1, steps) + 1):
            state = adapter.get_state()
            png = state.get("_screenshot_png")
            sig = state.get("frame_sig") or ""
            shot_path = None
            if png:
                shot_path = _save(os.path.join(shots_dir, f"step_{i:03d}.png"), png)

            if frames and frames[-1]["frame_sig"] == sig:
                unchanged_streak += 1
                if unchanged_streak >= 2:
                    frozen_suspects.append(i)
            else:
                unchanged_streak = 0

            record = {
                "step": i,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "scene": state.get("scene"),
                "frame_sig": sig,
                "frame_changed": state.get("frame_changed"),
                "ui_tree_available": (state.get("ui_tree_summary") or {}).get("ui_tree_available"),
                "node_count": state.get("node_count"),
                "screenshot": os.path.relpath(shot_path, run_dir) if shot_path else None,
            }
            trace.write(json.dumps(record, ensure_ascii=False) + "\n")
            trace.flush()
            frames.append(record)

            action_result = None
            if auto_tap and frames:
                if tap_xy:
                    x, y = tap_xy
                else:
                    rect = state.get("client_rect")
                    if rect:
                        x = (rect[0] + rect[2]) // 2
                        y = (rect[1] + rect[3]) // 2
                    else:
                        # Android/iOS 没有 client_rect，退回设备屏幕中心
                        screen = caps.get("screen") or {}
                        if screen.get("width") and screen.get("height"):
                            x = int(screen["width"]) // 2
                            y = int(screen["height"]) // 2
                        else:
                            x = y = None
                if x is not None and y is not None:
                    action_result = adapter.execute("tap", params={"x": x, "y": y})
                    trace.write(json.dumps(
                        {"step": i, "action": {"type": "tap", "x": x, "y": y},
                         "result": action_result},
                        ensure_ascii=False,
                    ) + "\n")

            if i < steps:
                time.sleep(max(0, interval_ms) / 1000.0)

    # 结论：只报「疑似画面冻结」，不冒充已确认缺陷
    summary = {
        "ok": True,
        "platform": platform,
        "run_dir": os.path.abspath(run_dir),
        "steps": steps,
        "capabilities": caps,
        "frames_captured": len(frames),
        "frames_with_image": sum(1 for f in frames if f["screenshot"]),
        "frozen_suspect_steps": frozen_suspects,
        "verdict": (
            "unavailable" if not caps.get("available")
            else ("suspected_freeze" if frozen_suspects else "frames_changing")
        ),
        "honesty_note": (
            "「疑似冻结」只是连续帧指纹相同，不代表一定是 bug："
            "菜单、暂停、回合制等待都会出现静止画面。需人工或 VLM 复核。"
        ),
    }

    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    with open(os.path.join(run_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(_render_md(summary, frames))

    return summary


def _render_md(summary: dict[str, Any], frames: list[dict[str, Any]]) -> str:
    caps = summary["capabilities"]
    lines = [
        f"# 原生冒烟报告 · {summary['platform']}",
        "",
        f"- 运行目录: `{summary['run_dir']}`",
        f"- 结论: **{summary['verdict']}**",
        f"- 截图帧: {summary['frames_with_image']}/{summary['steps']}",
        f"- 疑似冻结步: {summary['frozen_suspect_steps'] or '无'}",
        "",
        "## 适配器能力",
        "",
        "```json",
        json.dumps(
            {k: v for k, v in caps.items() if k not in ("devices",)},
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
        "## 逐帧",
        "",
        "| 步 | scene | 帧指纹 | 有截图 | 节点数 |",
        "|----|-------|--------|--------|--------|",
    ]
    for fr in frames:
        lines.append(
            f"| {fr['step']} | {fr.get('scene')} | `{(fr.get('frame_sig') or '')[:10]}` "
            f"| {'✅' if fr.get('screenshot') else '❌'} | {fr.get('node_count', '-')} |"
        )
    lines += ["", "## 诚实边界", "", summary["honesty_note"], ""]
    return "\n".join(lines)


def cmd_native(args: argparse.Namespace) -> int:
    platform = args.platform
    kwargs: dict[str, Any] = {}
    if platform == "android":
        kwargs.update(
            serial=args.serial,
            package=args.package,
            activity=args.activity,
        )
    elif platform == "windows":
        kwargs.update(title=args.window_title, exe_path=args.exe)
    elif platform == "linux":
        kwargs.update(title=args.window_title, exe_path=args.exe, display=args.display)
    elif platform == "ios":
        kwargs.update(endpoint=args.wda_endpoint, bundle_id=args.bundle_id)

    tap_xy = None
    if args.tap:
        try:
            x_str, y_str = args.tap.split(",")
            tap_xy = (int(x_str), int(y_str))
        except ValueError:
            print("❌ --tap 格式应为 x,y（例: --tap 640,360）")
            return 2

    result = run_native_smoke(
        platform,
        steps=args.steps,
        interval_ms=args.interval_ms,
        output=args.output,
        auto_tap=bool(args.auto_tap or args.tap),
        tap_xy=tap_xy,
        **kwargs,
    )

    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2

    if not result.get("ok"):
        print(f"❌ {platform} 不可用: {result.get('error')}")
        print("提示：先运行 `playranger doctor` 查看修复建议")
        return 2

    print(f"✅ {platform} 冒烟完成")
    print(f"   目录: {result['run_dir']}")
    print(f"   结论: {result['verdict']}")
    print(f"   截图: {result['frames_with_image']}/{result['steps']}")
    print(f"   疑似冻结步: {result['frozen_suspect_steps'] or '无'}")
    print(f"   报告: {os.path.join(result['run_dir'], 'summary.md')}")
    return 0


__all__ = ["cmd_native", "run_native_smoke"]
