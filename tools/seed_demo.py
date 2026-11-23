r"""演示数据种子 —— 为录屏/演示预置一个「有历史、有缺陷、有事件流」的控制台。

用法（在项目根目录）：
    $env:PLAYWRIGHT_CHROMIUM_PATH="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    python tools/seed_demo.py

会做三件事：
1. 重置 output/platform.db 中的任务/事件（保留/新建 admin 账号 demo / demo1234）；
2. 在 output/tasks/ 下生成 8 个历史任务的完整产物（steps.jsonl / progress.json /
   result.json / bugs/<ID>/defect.json + fix_plan.json + provenance.json）；
3. 缺陷包字段结构与真实 BugRecorder/Advisor 产出一致（取自 output/accept_v2 真实样本），
   不伪造检测结论，只回放平台真实会落盘的数据形态。

可重复执行：每次先清空任务与产物再重建。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 默认使用独立的演示库/产物目录，与真实 output/platform.db 隔离；
# 录屏启动脚本会用同一组环境变量启动服务，确保控制台读到这套数据。
os.environ.setdefault("PLAYRANGER_DB_PATH", str(ROOT / "output" / "demo" / "platform.db"))
os.environ.setdefault("PLAYRANGER_TASKS_DIR", str(ROOT / "output" / "demo" / "tasks"))

from playranger.platform import db  # noqa: E402

TASKS_DIR = Path(os.environ["PLAYRANGER_TASKS_DIR"])
DEMO_URL = (ROOT / "examples" / "demo_game" / "index.html").resolve().as_uri()
G2048_URL = (ROOT / "tests" / "fixtures" / "thirdparty_2048" / "index.html").resolve().as_uri()

DEMO_USER = "demo"
DEMO_PASS = "demo1234"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def compact_step(step: int, action: dict, scene: str, score, hp, ok=True, reason="") -> dict:
    return {
        "step": step,
        "action": action,
        "execution_ok": ok,
        "execution_error": None if ok else reason,
        "correction_level": "none",
        "verification": {"success": ok, "reason": reason},
        "state": {"scene": scene, "hp": hp, "score": score,
                  "level": None, "game_over": False, "victory": False},
        "timestamp": f"2026-11-23T0{8 + step % 8}:{(step * 7) % 60:02d}:{(step * 13) % 60:02d}",
    }


# ── 缺陷模板（字段结构与真实产出一致） ──────────────────────────────

def defect(bug_id: str, title: str, atype: str, severity: str, conclusion: str,
           desc: str, origin_step: int, origin_target: str, channels: list[str],
           tags: list[str]) -> dict:
    return {
        "bug_id": bug_id,
        "title": title,
        "anomaly_type": atype,
        "severity": severity,
        "conclusion": conclusion,
        "description": desc,
        "evidence": {atype: channels[0]},
        "game_name": "PlayRanger Demo",
        "game_version": "v1.2",
        "seed": 20261123,
        "discovered_at": "2026-11-23T01:12:00Z",
        "reproduction_rate": 1.0 if conclusion == "auto_confirmed" else 0.66,
        "reproduction_attempts": 3 if conclusion == "auto_confirmed" else 3,
        "action_count": origin_step,
        "tags": tags,
        "source": {"channel": channels[0], "origin_step": origin_step,
                   "detected_by": "verifier"},
        "triage": {
            "severity": severity,
            "conclusion": conclusion,
            "confidence": 0.9 if conclusion == "auto_confirmed" else 0.62,
            "impact": desc,
            "action_hint": "见修复建议，可在沙箱中校验 patch",
            "reasons": [f"异常类型 {atype} 基线定级为 {severity}",
                        f"证据通道：{'、'.join(channels)}",
                        "已生成修复草案与回归验证步骤"],
        },
        "provenance_summary": {
            "origin_step": origin_step,
            "origin_action": "click",
            "origin_target": origin_target,
            "channels": channels,
            "state_diff_count": 2,
            "replay_steps": origin_step,
            "screenshot_count": 2,
            "has_assertion": True,
        },
        "fix_plan_summary": {"effort": "低", "regression_risk": "低",
                             "confidence": 0.9, "root_cause_count": 4},
    }


FIX_PLAN = {
    "confidence": 0.9,
    "effort": "低",
    "regression_risk": "低",
    "root_causes": [
        "断言期望与实际状态不符，失败点可直接用于回归",
        "初始化/重置路径遗漏了该状态字段的写点",
        "同一状态存在 UI 层与逻辑层两个副本，只更新了其中一个",
        "建议把重置逻辑收敛到单一入口，避免再次漏字段",
    ],
    "fix_steps": [
        {"title": "补全状态写点", "detail": "在初始化/重置函数中补回遗漏字段赋值。",
         "file_hint": "js/game_manager.js", "code_hint": "// 在 setup() 内补回字段重置"},
        {"title": "补一条不变量断言",
         "detail": "让同类问题在下一轮自动被捕获。",
         "file_hint": "测试/SDK 断言侧",
         "code_hint": '{"type": "state_equals", "field": "score", "value": 0}'},
    ],
    "verification_steps": [
        "从原始触发步重放，确认异常消失",
        "用原 seed 复跑同一目标",
        "连续触发 3 次确认状态都回到基线",
    ],
    "affected_modules": ["状态初始化", "UI 渲染"],
    "evidence_note": "由 ReAct 循环在真实浏览器中检出，证据链可审计",
}

PROVENANCE = {
    "schema_version": 1,
    "runner": "playranger-react-loop",
    "determinism": {"seed_mode": "injected", "seed": 20261123,
                    "settle_strategy": "network_idle+dom_stable+animend"},
    "evidence_integrity": {"hash_alg": "sha256", "entries": 6, "tampered": False},
}


# ── 8 个历史任务的定义 ──────────────────────────────────────────────

NOW = datetime(2026, 11, 23, 9, 30, tzinfo=timezone.utc)

# (id 后缀, 游戏, 目标, 状态, 步数, 耗时ms, 几小时前, 缺陷列表, 错误)
TASKS = [
    {
        "suffix": "a1f0c9", "url": DEMO_URL, "name": "DemoGame 全量回归",
        "goal": "全流程通关 + 重开回归，找状态/UI/交互缺陷",
        "status": "completed", "steps": 42, "duration_ms": 28400, "hours_ago": 2,
        "bugs": [
            ("BUG-001", "重新开始后 HP 未重置", "hp_not_reset", "critical",
             "auto_confirmed", "点「重新开始」后回到第 1 关，但 HP 仍为 40",
             3, "#restart", ["assertion", "sdk_state"], ["auto_detected"]),
            ("BUG-002", "胜利页最终得分少 200", "ui_display_error", "major",
             "auto_confirmed", "胜利页显示 100，真实状态为 300",
             9, "#win-score-text", ["dom", "sdk_state"], ["auto_detected"]),
            ("BUG-003", "通关 alert 阻塞自动化", "blocking_dialog", "major",
             "auto_warning", "alert() 阻塞主线程，不处理 dialog 后续全部超时",
             11, "#level2-win", ["dialog"], ["auto_detected", "l2_streak_3"]),
        ],
    },
    {
        "suffix": "b2e71d", "url": G2048_URL, "name": "2048 状态桥接（C组）",
        "goal": "预置棋盘 + 状态探针，验证分数/胜利/结束判定",
        "status": "completed", "steps": 35, "duration_ms": 22100, "hours_ago": 6,
        "bugs": [
            ("BUG-201", "合并计分少加（2+2 仅 +2）", "score_mismatch", "major",
             "auto_confirmed", "一次合并后分数比预期少 2",
             7, "body/ArrowLeft", ["state_probe", "dom"], ["auto_detected"]),
            ("BUG-205", "合成 2048 不触发胜利", "victory_not_triggered", "critical",
             "auto_confirmed", "1024+1024 合并后 won=false，无 You win",
             14, "body/ArrowLeft", ["state_probe", "dom"], ["auto_detected"]),
        ],
    },
    {
        "suffix": "c83aa0", "url": DEMO_URL, "name": "DemoGame 新手引导",
        "goal": "验证前 3 步引导无阻断、提示文案正确",
        "status": "completed", "steps": 18, "duration_ms": 12600, "hours_ago": 9,
        "bugs": [
            ("BUG-014", "引导第二步按钮在小视口被遮挡", "ui_layout_overlap", "minor",
             "human_required", "1000px 宽视口下「下一步」按钮与提示气泡重叠",
             6, "#guide-next", ["screenshot_vlm"], ["needs_review"]),
        ],
    },
    {
        "suffix": "d4d21b", "url": G2048_URL, "name": "合成跑酷冒烟",
        "goal": "20 步随机探索冒烟，确认无崩溃",
        "status": "completed", "steps": 20, "duration_ms": 9800, "hours_ago": 20,
        "bugs": [],
    },
    {
        "suffix": "e99154", "url": G2048_URL, "name": "2048 盲测冒烟（无探针）",
        "goal": "纯 DOM + 截图探索，不注入状态探针",
        "status": "failed", "steps": 12, "duration_ms": 31000, "hours_ago": 26,
        "bugs": [], "error": "TimedOut 30000ms: 等待选择器 .tile-new 超时（页面疑似卡死）",
    },
    {
        "suffix": "f07be2", "url": DEMO_URL, "name": "DemoGame 性能基线",
        "goal": "--perf 采集 FPS / 长任务 / 堆内存基线",
        "status": "completed", "steps": 30, "duration_ms": 19200, "hours_ago": 30,
        "bugs": [
            ("BUG-PERF-2", "Level2 入场长任务 420ms", "performance_longtask", "minor",
             "auto_warning", "p95 帧耗时 168ms，连续 3 个 longtask（>200ms）",
             22, "#enter-level2", ["performance_observer", "cdp_metrics"],
             ["auto_detected", "perf"]),
        ],
    },
    {
        "suffix": "a15c77", "url": G2048_URL, "name": "2048 胜利流程回归",
        "goal": "构造 2048 胜利局面并验证 You win 与继续游戏",
        "status": "interrupted", "steps": 8, "duration_ms": 6100, "hours_ago": 34,
        "bugs": [], "error": "服务进程重启，任务被中断",
    },
    {
        "suffix": "b6d8e0", "url": DEMO_URL, "name": "DemoGame 每日冒烟",
        "goal": "每日定时冒烟：开始→过 Level1→断言分数",
        "status": "completed", "steps": 15, "duration_ms": 8400, "hours_ago": 46,
        "bugs": [
            ("BUG-009", "Level1 通关分数偶发 +99", "score_mismatch", "major",
             "auto_warning", "3 次复现中 1 次分数为 199（期望 200），复现率 66%",
             5, "#level1-next", ["assertion", "sdk_state"], ["flaky", "needs_replay"]),
        ],
    },
]


def seed() -> None:
    db.reset_initialized_cache()
    db_path = db.init_db()

    # 1) 账号：确保 demo / demo1234 存在且为 admin
    from playranger.platform import auth
    user = db.get_user_by_username(DEMO_USER)
    if user is None:
        user, _token = auth.register(DEMO_USER, DEMO_PASS)
    uid = int(user["id"])

    # 2) 清空历史任务/事件/产物
    db.reset_platform_data()
    if TASKS_DIR.exists():
        shutil.rmtree(TASKS_DIR)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    total_bugs = 0
    for i, spec in enumerate(TASKS):
        task_id = f"demo-{spec['suffix']}"
        created = NOW - timedelta(hours=spec["hours_ago"])
        started = created + timedelta(seconds=2)
        finished = started + timedelta(milliseconds=spec["duration_ms"])
        status = spec["status"]
        out_dir = TASKS_DIR / task_id
        out_dir.mkdir(parents=True, exist_ok=True)

        # ── 产物文件 ──
        bug_records = []
        for bspec in spec["bugs"]:
            bid = bspec[0]
            bug_dir = out_dir / "bugs" / bid
            write_json(bug_dir / "defect.json", defect(*bspec))
            write_json(bug_dir / "fix_plan.json", FIX_PLAN)
            write_json(bug_dir / "provenance.json", PROVENANCE)
            bug_records.append({"bug_id": bid, "title": bspec[1],
                                "anomaly_type": bspec[2], "severity": bspec[3],
                                "conclusion": bspec[4], "bug_dir": str(bug_dir)})
        total_bugs += len(bug_records)

        # steps.jsonl
        scene_chain = ["menu", "level1", "level2", "win", "level1"]
        lines = []
        for s in range(1, spec["steps"] + 1):
            scene = scene_chain[min(s // 8, len(scene_chain) - 1)]
            action = (
                {"type": "click", "target": "#start-btn"} if s == 1 else
                {"type": "set_param", "name": "hp", "value": 40} if s == 2 else
                {"type": "click", "target": "#restart"} if s == 3 else
                {"type": "assert", "expect": {"hp": 100}} if s == 4 else
                {"type": "click", "target": "#level1-next"} if s % 5 == 0 else
                {"type": "screenshot"} if s % 6 == 0 else
                {"type": "click", "target": ".tile"}
            )
            ok = not (status == "failed" and s == spec["steps"])
            reason = "期望元素在 30s 内未出现" if not ok else ""
            lines.append(json.dumps(compact_step(
                s, action, scene, score=min(s * 10, 300),
                hp=40 if s in (3, 4) else 100, ok=ok, reason=reason),
                ensure_ascii=False))
        (out_dir / "steps.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        progress = {
            "task_id": task_id, "status": status,
            "goal": spec["goal"], "game_url": spec["url"],
            "current_step": spec["steps"], "max_steps": 50,
            "bugs_found": len(bug_records),
            "last_action": {"type": "click", "target": "#restart"},
            "started_at": started.timestamp(),
            "finished_at": finished.timestamp() if status != "queued" else None,
        }
        if status == "failed":
            progress["error"] = spec.get("error", "")
        write_json(out_dir / "progress.json", progress)

        result = {
            "task_id": task_id, "status": "error" if status == "failed" else (
                "cancelled" if status == "interrupted" else "completed"),
            "goal": spec["goal"], "game_url": spec["url"],
            "total_steps": spec["steps"],
            "duration_ms": spec["duration_ms"],
            "brain_source": "deepseek-chat",
            "bugs_found": bug_records,
            "runtime": {"pages": 1, "actions": spec["steps"], "screenshots": spec["steps"] // 6},
            "finished_at": finished.timestamp(),
        }
        if status == "failed":
            result["error"] = spec.get("error", "")
        write_json(out_dir / "result.json", result)

        # ── DB 行 ──
        db.create_task({
            "id": task_id,
            "user_id": uid,
            "project_id": "default",
            "game_url": spec["url"],
            "goal": f"{spec['name']} — {spec['goal']}",
            "max_steps": 50,
            "seed": 20261123,
            "source_root": "",
            "status": status,
            "workers": 1,
            "output_dir": str(out_dir),
            "created_at": iso(created),
            "started_at": iso(started),
            "finished_at": iso(finished) if status != "queued" else None,
            "current_step": spec["steps"],
            "total_steps": spec["steps"],
            "bugs_found": len(bug_records),
            "progress_json": json.dumps(progress, ensure_ascii=False),
            "result_json": json.dumps(result, ensure_ascii=False),
            "error": spec.get("error") if status in ("failed", "interrupted") else None,
        })

    # 3) 系统事件流
    sys_events = [
        (None, "info", "PlayRanger 控制台启动，调度器就绪"),
        (None, "info", "检测到本机可用内存 12.4 GB，建议并发 worker = 4"),
        ("demo-a1f0c9", "info", "任务完成：42 步，发现 3 个缺陷（1 critical / 2 major）"),
        (None, "warn", "任务 demo-e99154 超时，已标记 failed"),
        ("demo-f07be2", "info", "性能采样完成：p95 帧 168ms，3 个 longtask"),
        (None, "info", "调度器空闲，当前 0 个运行中任务"),
    ]
    # 事件按时间顺序（旧→新）
    for tid, level, msg in sys_events:
        # 直接写库（db.add_event 用当前时间，为了让事件有梯度，接受都为当前时刻，
        # 前端事件流只展示消息与相对时间，同分钟内不影响演示）
        db.add_event(tid, level, msg)

    print("=" * 60)
    print("演示数据已就绪")
    print("=" * 60)
    print(f"  数据库 : {db_path}")
    print(f"  产物目录: {TASKS_DIR}")
    print(f"  任务数 : {len(TASKS)}（completed 6 / failed 1 / interrupted 1）")
    print(f"  缺陷总数: {total_bugs}")
    print(f"  登录账号: {DEMO_USER} / {DEMO_PASS}")
    print()
    print("启动控制台：")
    print('  $env:PLAYWRIGHT_CHROMIUM_PATH="C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"')
    print("  python -m playranger.cli server --port 8000")
    print("  浏览器打开 http://127.0.0.1:8000/app/")


if __name__ == "__main__":
    seed()
