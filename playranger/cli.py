"""PlayRanger 命令行入口。

子命令：
- playranger test <game_url>   启动一次自动测试
- playranger replay <bug_dir>  复现指定 bug
- playranger locate <bug_dir>  静态定位缺陷对应的源码位置
- playranger bugs [dir]        列出已发现的 bug
- playranger server            启动 API 服务
- playranger doctor            环境体检（多端可用性）
- playranger native            原生平台冒烟观察（Android/Windows/Linux/iOS）
- playranger --version         查看版本
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from dotenv import load_dotenv

from playranger import __version__


def _load_env() -> None:
    """加载 .env 文件（如果存在）。"""
    # 从当前目录往上找 .env
    cwd = os.getcwd()
    for _ in range(5):
        env_path = os.path.join(cwd, ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)
            break
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent


def _print_banner() -> None:
    """打印启动横幅。"""
    print(f"╔══════════════════════════════════════╗")
    print(f"║  PlayRanger  v{__version__}  ·  试玩队  ║")
    print(f"║  AI 原生游戏自动化测试 Agent        ║")
    print(f"╚══════════════════════════════════════╝")
    print()


# ─── test 子命令 ────────────────────────────────────────────────

def cmd_test(args: argparse.Namespace) -> int:
    """启动一次自动测试。"""
    from playranger.core.engines.play_engine import PlayEngine
    from playranger.core.brain.brain_factory import create_brain
    from playranger.core.engines.action_validator import ActionValidator
    from playranger.core.agent.react_loop import ReActLoop
    from playranger.core.agent.recorder import BugRecorder

    game_url = args.url
    goal = args.goal
    max_steps = args.max_steps
    headless = not args.show_browser
    output_dir = args.output

    os.makedirs(output_dir, exist_ok=True)
    bugs_dir = os.path.join(output_dir, "bugs")
    screenshots_dir = os.path.join(output_dir, "screenshots")

    print(f"🎮 目标: {goal}")
    print(f"🌐 游戏: {game_url}")
    print(f"📈 最大步数: {max_steps}")
    print(f"🖥️  浏览器: {'headless' if headless else 'visible'}")
    print(f"📁 输出目录: {output_dir}")
    print()

    # 创建组件
    print("正在初始化组件...")
    ci_mode = bool(getattr(args, "ci", False))
    engine = PlayEngine(
        url=game_url,
        headless=headless or ci_mode,
        ci_mode=ci_mode,
        retry=int(getattr(args, "retry", 1)),
        perf=bool(getattr(args, "perf", False)),
    )
    planner = create_brain()
    validator = ActionValidator()
    recorder = BugRecorder(output_dir=bugs_dir)

    print(f"  ✅ 引擎就绪 (source={planner.source})")
    print(f"  ✅ Planner 就绪")
    print(f"  ✅ Validator 就绪")
    print(f"  ✅ Recorder 就绪")
    print()

    print("启动引擎并开始测试...")
    print("─" * 50)

    try:
        engine.start()
    except Exception:
        engine.close()
        raise

    loop = ReActLoop(
        play_engine=engine,
        planner=planner,
        validator=validator,
        recorder=recorder,
        capture_screenshots=args.screenshots,
        screenshot_dir=screenshots_dir,
        max_l1_retries=args.l1_retries,
        max_l2_retries=args.l2_retries,
        max_l3_retries=args.l3_retries,
    )

    start_time = time.time()
    result = loop.run(goal=goal, max_steps=max_steps)
    elapsed = time.time() - start_time

    print("─" * 50)
    print()
    print("📊 测试结果")
    print(f"  状态:     {result.status}")
    print(f"  总步数:   {result.total_steps}")
    print(f"  耗时:     {elapsed:.1f}s")
    print(f"  L1重试:   {result.l1_retries}")
    print(f"  L2重试:   {result.l2_retries}")
    print(f"  L3重试:   {result.l3_retries}")
    print(f"  结论:     {result.conclusion}")
    print(f"  发现BUG:  {len(result.bugs_found)} 个")

    if result.bugs_found:
        print()
        print("🐛 发现的缺陷:")
        for i, bug in enumerate(result.bugs_found, 1):
            severity = bug.get("severity", "?")
            title = bug.get("title", "?")
            conclusion = bug.get("conclusion", "?")
            print(f"  {i}. [{severity}] {title}")
            print(f"     类型: {bug.get('anomaly_type', '?')}")
            print(f"     结论: {conclusion}")
            if bug.get("bug_dir"):
                print(f"     位置: {bug['bug_dir']}")

    print()
    print(f"📁 缺陷包: {bugs_dir}")
    if args.screenshots:
        print(f"📸 截图: {screenshots_dir}")

    stats = engine.runtime_stats()
    print(
        f"🧪 运行环境: ci={stats['ci_mode']} retry={stats['retry_count']}/{stats['retry']} "
        f"viewport={stats['viewport'][0]}x{stats['viewport'][1]}"
    )
    if stats["retry_reasons"]:
        print(f"   重试原因: {' | '.join(stats['retry_reasons'][:3])}")

    try:
        engine.close()
    except Exception:
        pass

    return 0 if result.status == "completed" else 1


# ─── replay 子命令 ──────────────────────────────────────────────

def cmd_replay(args: argparse.Namespace) -> int:
    """复现指定 bug。"""
    from playranger.core.agent.replayer import Replayer, update_defect_reproduction

    bug_dir = args.bug_dir

    # 查找缺陷包文件
    defect_json = os.path.join(bug_dir, "defect.json")
    trace_file = os.path.join(bug_dir, "action_trace.jsonl")
    assertion_file = os.path.join(bug_dir, "assertion.json")
    seed_file = os.path.join(bug_dir, "seed.json")

    if not os.path.exists(defect_json):
        print(f"❌ 缺陷包不存在: {bug_dir}")
        return 1

    # 读取缺陷信息
    with open(defect_json, "r", encoding="utf-8") as f:
        defect = json.load(f)

    bug_id = defect.get("bug_id", os.path.basename(bug_dir))
    title = defect.get("title", "未知缺陷")
    seed = None
    if os.path.exists(seed_file):
        with open(seed_file, "r", encoding="utf-8") as f:
            seed_data = json.load(f)
            seed = seed_data.get("seed")

    print(f"🔄 复现缺陷: {bug_id}")
    print(f"   标题: {title}")
    print(f"   次数: {args.runs}")
    if seed is not None:
        print(f"   种子: {seed}")
    print()

    # 从缺陷信息里找 game_url
    game_url = defect.get("game_url") or os.environ.get("PLAYRANGER_GAME_URL", "")
    if not game_url:
        # 尝试从 initial_state 推断？不行，必须有 URL
        print("⚠️  缺陷包中未找到 game_url，请通过 PLAYRANGER_GAME_URL 环境变量指定")
        print("   或使用 --url 参数手动指定")
        if not args.url:
            return 1
        game_url = args.url

    if args.url:
        game_url = args.url

    headless = not args.show_browser

    # 构造 engine factory
    def make_engine():
        from playranger.core.engines.play_engine import PlayEngine
        return PlayEngine(url=game_url, headless=headless)

    replayer = Replayer(engine_factory=make_engine)

    print("开始重放...")
    print("─" * 50)

    report = replayer.replay_from_file(
        trace_file=trace_file,
        assertion_file=assertion_file if os.path.exists(assertion_file) else None,
        num_runs=args.runs,
        bug_id=bug_id,
        initial_state=defect.get("initial_state") or None,
        seed=seed,
    )

    # 三态复现结果写回缺陷包
    reproduction = update_defect_reproduction(bug_dir, report)

    print("─" * 50)
    print()
    print("📋 复现报告")
    print(f"  Bug ID:      {report.bug_id}")
    print(f"  总次数:      {report.total_runs}")
    print(f"  成功次数:    {report.success_runs}")
    print(f"  复现率:      {report.reproduction_rate*100:.0f}%")
    state_label = {
        "reproduced": "✅ 完全复现",
        "partial": "⚠️ 部分复现",
        "not_reproduced": "❌ 未复现",
    }.get(report.reproduction_state, report.reproduction_state)
    print(f"  复现状态:    {state_label}")
    print(f"  种子模式:    {report.seed_mode or 'none'}")
    print(f"  平均耗时:    {report.avg_duration_ms:.0f} ms")
    if reproduction:
        print(f"  已写回:      {os.path.join(bug_dir, 'defect.json')} · reproduction")

    if args.verbose:
        print()
        print("详细结果:")
        for r in report.results:
            status = "✅" if r.success else "❌"
            print(f"  Run {r.run_id}: {status}  steps={r.steps_executed}", end="")
            if r.error:
                print(f"  error={r.error[:50]}", end="")
            print()

    return 0 if report.reproduction_state == "reproduced" else (2 if report.reproduction_state == "partial" else 1)


# ─── locate 子命令 ──────────────────────────────────────────────

def cmd_locate(args: argparse.Namespace) -> int:
    """静态定位缺陷对应的源码位置（文件:行:列）。"""
    from playranger.core.agent.code_locator import locate_bug
    from playranger.core.agent.recorder import load_defect

    bug_dir = args.bug_dir
    defect_json = os.path.join(bug_dir, "defect.json")
    if not os.path.exists(defect_json):
        print(f"❌ 缺陷包不存在: {bug_dir}")
        return 1

    bug = load_defect(bug_dir).to_dict()
    with open(defect_json, "r", encoding="utf-8") as f:
        defect = json.load(f)
    # 缺陷包里的额外键（如 source / source_root）合并进来
    for key, value in defect.items():
        bug.setdefault(key, value)

    result = locate_bug(bug, source_root=args.source)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_code_location(bug, result)

    if args.write:
        target = os.path.join(bug_dir, "code_location.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"💾 已写入: {target}", file=sys.stderr if args.json else sys.stdout)

    return 0 if result.has_hits else 1


def _print_code_location(bug: dict, result: Any) -> None:
    """打印可读的定位结果。"""
    bug_id = bug.get("bug_id", "?")
    anomaly = bug.get("anomaly_type", "?")
    print(f"🔎 代码定位: {bug_id}（{anomaly}）")
    print(f"   字段:     {'、'.join(result.fields) if result.fields else '（无）'}")

    if result.strategies:
        print(f"   策略:     {'、'.join(result.strategies)}")

    print("   命中:")
    printed = False
    for field, hits in result.hits.items():
        if not hits:
            continue
        printed = True
        print(f"     [{field}]")
        for hit in hits[:8]:
            print(
                f"       {hit.path}:{hit.line}:{hit.column} "
                f"[{hit.kind}] {hit.function}"
            )
    if not printed:
        print("     （未命中）")

    if result.patch:
        print("   patch 草案:")
        for line in result.patch.rstrip().splitlines():
            print(f"     {line}")

    print(f"   置信度:   {result.confidence:.0%}")
    if result.note:
        print(f"   备注:     {result.note}")


# ─── bugs 子命令 ────────────────────────────────────────────────

def cmd_bugs(args: argparse.Namespace) -> int:
    """列出已发现的 bug。"""
    bugs_dir = args.dir

    if not os.path.exists(bugs_dir):
        print(f"📁 目录不存在: {bugs_dir}")
        return 0

    # 扫描所有子目录，找含 defect.json 的
    bugs = []
    for name in sorted(os.listdir(bugs_dir)):
        subdir = os.path.join(bugs_dir, name)
        defect_file = os.path.join(subdir, "defect.json")
        if os.path.isdir(subdir) and os.path.exists(defect_file):
            with open(defect_file, "r", encoding="utf-8") as f:
                defect = json.load(f)
            bugs.append({
                "id": name,
                "title": defect.get("title", "?"),
                "severity": defect.get("severity", "?"),
                "type": defect.get("anomaly_type", "?"),
                "conclusion": defect.get("conclusion", "?"),
                "dir": subdir,
            })

    print(f"📋 已发现缺陷列表 ({len(bugs)} 个)")
    print(f"   目录: {bugs_dir}")
    print()

    if not bugs:
        print("  （暂无缺陷记录）")
        return 0

    # 按严重程度排序
    severity_order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
    bugs.sort(key=lambda b: severity_order.get(b["severity"], 99))

    for i, bug in enumerate(bugs, 1):
        print(f"  {i}. [{bug['severity']:8s}] {bug['title']}")
        print(f"      ID: {bug['id']}  |  类型: {bug['type']}  |  结论: {bug['conclusion']}")
        if args.verbose:
            print(f"      位置: {bug['dir']}")

    print()
    print(f"共 {len(bugs)} 个缺陷")
    return 0


# ─── feedback 子命令 ────────────────────────────────────────────

def cmd_feedback(args: argparse.Namespace) -> int:
    """记录人工复核结论，并回写项目级分级规则（误报闭环）。"""
    from playranger.core.agent.feedback import (
        register_label,
        resolve_bug_dir,
        save_feedback,
    )

    bug_dir = resolve_bug_dir(args.bug, args.dir)
    if not bug_dir:
        print(f"❌ 未找到缺陷包: {args.bug}（查找目录: {args.dir}）")
        return 1

    with open(bug_dir / "defect.json", "r", encoding="utf-8") as f:
        defect = json.load(f)
    anomaly = str(defect.get("anomaly_type", "") or "unknown")

    save_feedback(
        bug_dir,
        label=args.label,
        severity=args.severity,
        note=args.note,
        reviewer=args.reviewer,
    )
    rule = register_label(
        bug_dir.parent,
        anomaly_type=anomaly,
        label=args.label,
        severity=args.severity,
    )

    label_text = "真实缺陷" if args.label == "true_positive" else "误报"
    print(f"✅ 已记录复核结论: {bug_dir.name} -> {label_text}")
    print(f"   异常类型    : {anomaly}")
    if rule.get("fp_count"):
        print(f"   连续误报    : {rule['fp_count']} 次（同类新缺陷将降级为 auto_warning）")
    if rule.get("severity"):
        print(f"   严重度覆盖  : {rule['severity']}")
    print(f"   规则文件    : {bug_dir.parent / 'triage_overrides.json'}")
    return 0


# ─── server 子命令 ──────────────────────────────────────────────

def cmd_server(args: argparse.Namespace) -> int:
    """启动 API 服务。"""
    import uvicorn
    from playranger.api.main import app

    host = args.host
    port = args.port

    print(f"🚀 启动 PlayRanger API 服务")
    print(f"   地址: http://{host}:{port}")
    print(f"   文档: http://{host}:{port}/docs")
    print()
    print("按 Ctrl+C 停止")
    print()

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


# ─── batch 子命令 ───────────────────────────────────────────────

def cmd_batch(args: argparse.Namespace) -> int:
    """并行执行批量试玩清单（P3-3）。"""
    from playranger.core.agent.batch import load_manifest, run_batch

    items = load_manifest(args.manifest)
    print(f"📋 批量任务: {len(items)} 个，并发 {args.workers}")
    result = run_batch(items, workers=args.workers, base_output=args.output)

    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["failed_tasks"] == 0 else 1

    print("─" * 50)
    for item in result["items"]:
        mark = "✅" if item.get("status") == "completed" else "❌"
        print(
            f"{mark} #{item.get('index', 0) + 1} {item.get('url')} "
            f"status={item.get('status')} bugs={len(item.get('bugs_found', []))} "
            f"{item.get('duration_ms', 0):.0f}ms"
        )
    print("─" * 50)
    print(f"总耗时: {result['duration_ms']:.0f}ms")
    print(f"缺陷: 共 {result['total_bugs']} 条，去重后 {result['unique_bugs']} 条")
    if result["duplicates"]:
        print("重复缺陷:")
        for dup in result["duplicates"]:
            print(f"  - [{dup['anomaly_type']}] {dup['title']} ×{dup['count']}")
    return 0 if result["failed_tasks"] == 0 else 1


# ─── doctor 子命令 ──────────────────────────────────────────────

def cmd_doctor(args: argparse.Namespace) -> int:
    """环境体检：本机能测哪些端、缺什么、怎么补。"""
    from playranger.platform.doctor import format_text, run_doctor

    report = run_doctor(network=bool(getattr(args, "network", False)))

    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["summary"]["available_platforms"] else 1

    print(format_text(report))
    return 0 if report["summary"]["available_platforms"] else 1


# ─── native 子命令 ──────────────────────────────────────────────

def cmd_native(args: argparse.Namespace) -> int:
    """原生平台冒烟观察（Android / Windows / Linux / iOS）。"""
    from playranger.platform.native_runner import cmd_native as _run

    return _run(args)


# ─── 主入口 ─────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="playranger",
        description="PlayRanger — AI 原生游戏自动化测试 Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  playranger test file:///path/to/game.html
  playranger test http://localhost:8080 --goal "找到所有bug" --max-steps 100
  playranger replay output/bugs/BUG-001 --runs 5
  playranger locate output/bugs/BUG-001 --source src/
  playranger bugs output/bugs
  playranger server --port 8000
        """,
    )

    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"PlayRanger v{__version__}",
    )

    parser.add_argument(
        "--no-env",
        action="store_true",
        help="不自动加载 .env 文件",
    )

    subparsers = parser.add_subparsers(dest="command", help="可用子命令")

    # ── test ──
    test_parser = subparsers.add_parser("test", help="启动一次自动测试")
    test_parser.add_argument("url", help="游戏页面 URL（http/https/file://）")
    test_parser.add_argument("--goal", "-g", default="找到所有可复现的缺陷", help="测试目标")
    test_parser.add_argument("--max-steps", "-n", type=int, default=50, help="最大步数 (默认 50)")
    test_parser.add_argument("--show-browser", action="store_true", help="显示浏览器窗口（默认 headless）")
    test_parser.add_argument("--output", "-o", default="output/test_run", help="输出目录 (默认 output/test_run)")
    test_parser.add_argument("--no-screenshots", dest="screenshots", action="store_false",
                             default=True, help="不保存截图")
    test_parser.add_argument("--l1-retries", type=int, default=2, help="L1 动作层最大重试次数")
    test_parser.add_argument("--l2-retries", type=int, default=3, help="L2 路径层最大重试次数")
    test_parser.add_argument("--l3-retries", type=int, default=2, help="L3 策略层最大重试次数")
    test_parser.add_argument("--ci", action="store_true",
                             help="CI 模式：固定无头/禁用 GPU/固定视口，失败自动重试")
    test_parser.add_argument("--retry", type=int, default=1,
                             help="浏览器启动失败重试次数（默认 1）")
    test_parser.add_argument("--perf", action="store_true",
                             help="开启运行时性能采样（FPS/长任务/内存），超标自动记缺陷")
    test_parser.set_defaults(func=cmd_test)

    # ── replay ──
    replay_parser = subparsers.add_parser("replay", help="复现指定 bug")
    replay_parser.add_argument("bug_dir", help="缺陷包目录路径")
    replay_parser.add_argument("--runs", "-n", type=int, default=3, help="重放次数 (默认 3)")
    replay_parser.add_argument("--url", help="游戏 URL（覆盖缺陷包中的设置）")
    replay_parser.add_argument("--show-browser", action="store_true", help="显示浏览器窗口")
    replay_parser.add_argument("--verbose", "-v", action="store_true", help="显示详细结果")
    replay_parser.set_defaults(func=cmd_replay)

    # ── locate ──
    locate_parser = subparsers.add_parser("locate", help="静态定位缺陷对应的源码位置")
    locate_parser.add_argument("bug_dir", help="缺陷包目录路径")
    locate_parser.add_argument(
        "--source",
        default=None,
        help="源码根目录（默认取缺陷包 source_root 或 PLAYRANGER_SOURCE_ROOT）",
    )
    locate_parser.add_argument("--write", action="store_true", help="写入缺陷包 code_location.json")
    locate_parser.add_argument("--json", action="store_true", help="以 JSON 输出定位结果")
    locate_parser.set_defaults(func=cmd_locate)

    # ── bugs ──
    bugs_parser = subparsers.add_parser("bugs", help="列出已发现的 bug")
    bugs_parser.add_argument("dir", nargs="?", default="output/bugs", help="缺陷目录 (默认 output/bugs)")
    bugs_parser.add_argument("--verbose", "-v", action="store_true", help="显示完整路径")
    bugs_parser.set_defaults(func=cmd_bugs)

    # ── feedback ──
    feedback_parser = subparsers.add_parser("feedback", help="记录人工复核结论（误报闭环）")
    feedback_parser.add_argument("--bug", required=True, help="缺陷包目录或 BUG 目录名")
    feedback_parser.add_argument(
        "--label",
        required=True,
        choices=["true_positive", "false_positive"],
        help="true_positive=确认为真实缺陷，false_positive=误报",
    )
    feedback_parser.add_argument(
        "--severity",
        choices=["info", "minor", "major", "critical", "blocker"],
        help="同时校正该异常类型的严重度",
    )
    feedback_parser.add_argument("--note", default="", help="复核备注")
    feedback_parser.add_argument("--reviewer", default="", help="复核人")
    feedback_parser.add_argument("--dir", default="output/bugs", help="缺陷目录 (默认 output/bugs)")
    feedback_parser.set_defaults(func=cmd_feedback)

    # ── server ──
    server_parser = subparsers.add_parser("server", help="启动 API 服务")
    server_parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    server_parser.add_argument("--port", "-p", type=int, default=8000, help="监听端口 (默认 8000)")
    server_parser.set_defaults(func=cmd_server)

    # ── batch ──
    batch_parser = subparsers.add_parser("batch", help="并行执行批量试玩清单")
    batch_parser.add_argument("manifest", help="批量清单 JSON 路径")
    batch_parser.add_argument("--workers", "-w", type=int, default=2,
                              help="并发数（默认 2，上限 8）")
    batch_parser.add_argument("--output", "-o", default="output/batch",
                              help="输出根目录 (默认 output/batch)")
    batch_parser.add_argument("--json", action="store_true", help="以 JSON 输出汇总")
    batch_parser.set_defaults(func=cmd_batch)

    # ── doctor ──
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="环境体检：本机能测 Web/Android/Windows/Linux/iOS 哪些端",
    )
    doctor_parser.add_argument(
        "--network", action="store_true",
        help="真正请求 LLM/VLM 健康检查（默认只读环境变量）",
    )
    doctor_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    doctor_parser.set_defaults(func=cmd_doctor)

    # ── native ──
    native_parser = subparsers.add_parser(
        "native",
        help="原生平台冒烟观察（Android/Windows/Linux/iOS）",
    )
    native_parser.add_argument(
        "--platform", required=True,
        choices=["android", "windows", "linux", "ios"],
        help="目标平台",
    )
    native_parser.add_argument("--steps", type=int, default=5, help="观察步数（默认 5）")
    native_parser.add_argument("--interval-ms", type=int, default=500, help="步间隔毫秒")
    native_parser.add_argument("--output", "-o", default="output/native", help="输出根目录")
    native_parser.add_argument(
        "--auto-tap", action="store_true",
        help="启用点击（默认只观察不操作；只点客户区中心）",
    )
    native_parser.add_argument("--tap", default=None, help="指定点击坐标 x,y（隐含 --auto-tap 行为）")
    native_parser.add_argument("--json", action="store_true", help="以 JSON 输出")

    # Android
    native_parser.add_argument("--serial", default=None, help="[android] 设备序列号")
    native_parser.add_argument("--package", default=None, help="[android] 应用包名")
    native_parser.add_argument("--activity", default=None, help="[android] 启动 Activity")

    # Desktop
    native_parser.add_argument("--window-title", default=None, help="[windows/linux] 窗口标题")
    native_parser.add_argument("--exe", default=None, help="[windows/linux] 游戏可执行文件路径")
    native_parser.add_argument("--display", default=None, help="[linux] DISPLAY，如 :0")

    # iOS
    native_parser.add_argument("--wda-endpoint", default=None, help="[ios] WebDriverAgent 地址")
    native_parser.add_argument("--bundle-id", default=None, help="[ios] 应用 Bundle ID")

    native_parser.set_defaults(func=cmd_native)

    return parser


def main(argv: list[str] | None = None) -> None:
    """主入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # 加载 .env
    if not getattr(args, "no_env", False):
        _load_env()

    # JSON 输出模式不打印横幅，保证 stdout 是纯 JSON
    if not getattr(args, "json", False):
        _print_banner()

    try:
        exit_code = args.func(args)
    except KeyboardInterrupt:
        print("\n\n⏹️  已取消")
        exit_code = 130
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        if os.environ.get("PLAYRANGER_DEBUG"):
            traceback.print_exc()
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
