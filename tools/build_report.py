"""一键生成「自动试玩报告」——扫描缺陷包目录，输出单文件离线 HTML。

用法：
    # 默认扫描 output/bugs，输出 output/report.html
    python -m tools.build_report

    # 指定目录与输出路径
    python -m tools.build_report --bugs output/bugs --out output/report.html

    # 附带步骤轨迹（jsonl，每行一步）
    python -m tools.build_report --bugs output/bugs --history output/trace.jsonl

    # 只统计某次测试
    python -m tools.build_report --test-id t-20261113-001

报告内容包括：KPI 面板、严重度 / 结论 / 异常类型分布、执行时间线、
同类缺陷聚类、以及每个 Bug 的触发链、状态差异、证据来源、截图、
修复深化方案（根因 / 步骤 / 代码提示 / 验证清单）。

设计约束：零依赖、零外链，双击即可在断网环境查看。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playranger.core.agent.advisor import fix_plan_from_file
from playranger.core.agent.evidence import EvidenceChain
from playranger.core.agent.feedback import load_feedback
from playranger.core.agent.intel import (
    FIX_PLAN_FILE,
    PROVENANCE_FILE,
    analyze_bug,
)
from playranger.core.agent.provenance import provenance_from_file
from playranger.core.agent.recorder import load_defect
from playranger.core.agent.report import build_metrics, write_report
from playranger.core.agent.trend import (
    DEFAULT_VERSIONS_DIR,
    build_snapshot,
    compare,
    load_history as load_version_history,
    load_snapshot,
    save_snapshot,
)
from playranger.core.agent.triage import SEVERITY_ORDER


# ─── 缺陷包扫描 ─────────────────────────────────────────────────


def find_bug_dirs(root: str | Path) -> list[Path]:
    """找出目录下所有含 defect.json 的缺陷包（含根目录本身）。"""
    root = Path(root)
    if not root.exists():
        return []

    if (root / "defect.json").exists():
        return [root]

    found = [p for p in sorted(root.iterdir()) if p.is_dir() and (p / "defect.json").exists()]
    # 兼容 output/bugs/<run>/BUG-001 这种多一层嵌套
    if not found:
        for sub in sorted(root.iterdir()):
            if sub.is_dir():
                found.extend(
                    p for p in sorted(sub.iterdir())
                    if p.is_dir() and (p / "defect.json").exists()
                )
    return found


def load_bug_entry(bug_dir: str | Path) -> dict:
    """把一个缺陷包加载成报告所需的字典。"""
    bug_dir = Path(bug_dir)
    record = load_defect(bug_dir)
    entry = record.to_dict()
    entry["bug_dir"] = str(bug_dir.resolve())

    # defect.json 里还有 DefectRecord 不承载的字段（分级明细 / 来源 / 摘要）
    meta = _read_json(bug_dir / "defect.json")
    for key in ("triage", "test_id", "source", "vision_confidence",
                "provenance_summary", "fix_plan_summary"):
        if key in meta:
            entry[key] = meta[key]

    # 证据链状态（报告里的「证据链通过率」靠它）
    try:
        verify = EvidenceChain.verify(str(bug_dir))
        entry["evidence_chain"] = {
            "ok": verify.ok,
            "checked": verify.checked,
            "message": verify.message,
        }
    except (OSError, ValueError):
        entry["evidence_chain"] = {}

    # 优先复用落盘结果，缺文件时即时计算
    prov = _read_json(bug_dir / PROVENANCE_FILE)
    plan = _read_json(bug_dir / FIX_PLAN_FILE)
    if not prov or not plan:
        intel = analyze_bug(entry, bug_dir=bug_dir)
        prov = prov or intel["provenance"]
        plan = plan or intel["fix_plan"]
    entry["provenance"] = prov
    entry["fix_plan"] = plan

    # 人工复核结论（报告里的「人工复核一致率」靠它）
    feedback = load_feedback(bug_dir)
    if feedback.get("label"):
        entry["feedback"] = feedback

    return entry


# ─── 轨迹 ───────────────────────────────────────────────────────


def load_history(path: str | Path | None) -> list[dict]:
    """读取步骤轨迹 jsonl；文件不存在时返回空列表。"""
    if not path:
        return []
    target = Path(path)
    if not target.exists():
        print(f"  [!] 轨迹文件不存在，已跳过: {target}")
        return []

    steps: list[dict] = []
    with open(target, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                steps.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return steps


# ─── CLI ────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_report",
        description="扫描缺陷包目录，生成单文件离线 HTML 试玩报告",
    )
    parser.add_argument("--bugs", default="output/bugs", help="缺陷包根目录（默认 output/bugs）")
    parser.add_argument("--out", default="output/report.html", help="输出 HTML 路径")
    parser.add_argument("--history", default="", help="步骤轨迹 jsonl（可选）")
    parser.add_argument("--title", default="PlayRanger 自动试玩报告", help="报告标题")
    parser.add_argument("--game", default="", help="游戏名称（显示在报告头部）")
    parser.add_argument(
        "--test-id",
        default="",
        help="只统计某次测试的缺陷（按缺陷包内 test_id.json 或目录名过滤）",
    )
    parser.add_argument(
        "--tickets",
        default="",
        help="额外导出 Markdown 缺陷单目录（可直接贴进 Jira / GitHub Issues）",
    )
    parser.add_argument(
        "--version",
        default="",
        help="给本次跑测打版本号并落盘快照（如 v1.2），用于跨版本趋势对比",
    )
    parser.add_argument(
        "--baseline",
        default="",
        help="对比的基线版本号（需已存在于 --versions-dir）",
    )
    parser.add_argument(
        "--versions-dir",
        default=DEFAULT_VERSIONS_DIR,
        help=f"版本快照目录（默认 {DEFAULT_VERSIONS_DIR}）",
    )
    return parser


def export_tickets(bug_dirs: list[Path], out_dir: str | Path) -> list[str]:
    """把每个缺陷包导出成一份 Markdown 缺陷单（溯源 + 修复方案）。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for bug_dir in bug_dirs:
        prov = provenance_from_file(bug_dir)
        plan = fix_plan_from_file(str(bug_dir))
        record = load_defect(bug_dir)

        header = [
            f"> 游戏：{record.game_name or '未登记'}　版本：{record.game_version or '未登记'}　"
            f"seed：{record.seed}",
            f"> 缺陷包：`{Path(bug_dir).name}`　生成时间：{record.discovered_at or '—'}",
            "",
        ]
        text = "\n".join(header) + "\n" + prov.to_markdown() + "\n---\n\n" + plan.to_markdown()
        path = target / f"{prov.bug_id or Path(bug_dir).name}.md"
        path.write_text(text, encoding="utf-8")
        written.append(str(path.resolve()))
    return written


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    bug_dirs = find_bug_dirs(args.bugs)
    if args.test_id:
        bug_dirs = [d for d in bug_dirs if _belongs_to_test(d, args.test_id)]

    if not bug_dirs:
        print(f"[!] 未在 {args.bugs} 下找到任何缺陷包（需要 BUG-*/defect.json）")
        return 1

    bugs = [load_bug_entry(d) for d in bug_dirs]
    history = load_history(args.history)
    game_name = args.game or _guess_game_name(bugs)
    metrics = build_metrics(bugs, history)

    # 跨版本趋势：本次快照落盘 + 与基线做差分
    trend = None
    if args.version:
        snapshot = build_snapshot(
            bugs, version=args.version, game_name=game_name, metrics=metrics
        )
        saved = save_snapshot(snapshot, args.versions_dir)
        print(f"  版本快照已落盘: {saved}")

        if args.baseline:
            baseline = load_snapshot(args.versions_dir, args.baseline)
            if baseline:
                trend = compare(snapshot, baseline)
                trend["history"] = [
                    {
                        "version": s.get("version", ""),
                        "total_bugs": (s.get("metrics") or {}).get("total_bugs", 0),
                    }
                    for s in load_version_history(args.versions_dir)
                ]
            else:
                print(f"  [!] 未找到基线版本 {args.baseline}，本次不输出趋势对比")

    out_path = write_report(
        bugs,
        args.out,
        history=history,
        title=args.title,
        game_name=game_name,
        trend=trend,
    )

    _print_summary(metrics, out_path)
    if trend:
        counts = trend["counts"]
        print(
            f"  跨版本对比    : 基线 {trend['baseline_version']} → {trend['version']}，"
            f"新增 {counts['added']} / 修复 {counts['fixed']} / 遗留 {counts['persisted']}"
        )

    if args.tickets:
        tickets = export_tickets(bug_dirs, args.tickets)
        print(f"  缺陷单已导出    : {len(tickets)} 份 -> {Path(args.tickets).resolve()}")
    return 0


def _print_summary(m: dict, out_path: str) -> None:
    print()
    print("=" * 56)
    print("PlayRanger 自动试玩报告")
    print("=" * 56)
    print(f"  缺陷总数      : {m['total_bugs']}")
    print(f"  自动确认      : {m['auto_confirmed']}（{m['auto_rate']:.0%}）")
    print("  严重度分布    : " + "  ".join(
        f"{sev}={m['by_severity'].get(sev, 0)}" for sev in SEVERITY_ORDER
    ))
    print(f"  平均置信度    : {m['avg_confidence']:.0%}")
    print(f"  证据链通过    : {m['evidence_ok']}（{m['evidence_rate']:.0%}）")
    if m.get("review_total"):
        print(
            f"  人工复核一致  : {m['review_agree']}/{m['review_total']}"
            f"（{m['review_rate']:.0%}），误报标记 {m['false_positive']}"
        )
    if m["steps_total"]:
        print(
            f"  步骤轨迹      : {m['steps_total']} 步，失败 {m['steps_failed']}，"
            f"修正 {m['steps_corrected']}，成功率 {m['step_success_rate']:.0%}"
        )
    print(f"  报告已生成    : {out_path}")
    print("=" * 56)


def _guess_game_name(bugs: list[dict]) -> str:
    for bug in bugs:
        name = bug.get("game_name") or ""
        if name:
            return str(name)
    return ""


def _belongs_to_test(bug_dir: Path, test_id: str) -> bool:
    """test_id 记录位置不完全统一，这里做宽松匹配。"""
    if test_id in bug_dir.name:
        return True
    meta = _read_json(bug_dir / "defect.json")
    if str(meta.get("test_id", "")) == test_id:
        return True
    return (bug_dir / f"test_id.{test_id}.json").exists()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


if __name__ == "__main__":
    sys.exit(main())
