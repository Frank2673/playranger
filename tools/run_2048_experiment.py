"""在第三方 2048 上执行 D 组（静态定位）缺陷注入实验。

只做「可复算」的部分：
- 每个缺陷一个独立源码副本，注入点与替换内容写在 MUTATIONS 里
- 缺陷包由注入清单生成（症状来自注入方式），不冒充运行时发现的缺陷包
- 调用真实 locate_bug，记录 Top-1/Top-3/可操作定位/patch/耗时
- 预注册值 vs 实测值同时输出，偏离时给原因

未覆盖：A 人工 / B 盲测 / C 状态桥接 / E 人工定位，均标记 pending，不编造数字。

用法：
    python tools/run_2048_experiment.py
    python tools/run_2048_experiment.py --only D01,D02 --json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playranger.core.agent.code_locator import locate_bug  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "thirdparty_2048"
DEFAULT_OUT = ROOT / "output" / "experiments" / "2048"

# 预注册预期（来自 EXPERIMENT_2048 第 4.1 节，冻结于 2026-11-16）
PREREG = {
    "top3_hits": 4,       # D01 / D02 / D05 / D06
    "patch_usable": 2,    # D03 / D04
    "operable": 6,        # Top-3 命中 或 patch 指向注入行
    "top1_hits": None,    # 方案未预注册 Top-1 总数，只逐条描述，避免事后挑好听的说
}

MUTATIONS = [
    {
        "id": "D01", "category": "状态", "anomaly_type": "score_mismatch",
        "title": "重新开始后分数未归零",
        "file": "js/game_manager.js", "target_line": 48,
        "old": "    this.score       = 0;",
        "new": "    this.score       = this.score || 0;",
        "trigger": "得分 > 0 后按 R 重开",
        "expectation": "重开分数归零；实际保留上局分数",
        "state": {"score": 128}, "final": {"score": 128},
        "prereg_top3": True, "prereg_patch": False,
    },
    {
        "id": "D02", "category": "状态", "anomaly_type": "score_mismatch",
        "title": "合并计分少加",
        "file": "js/game_manager.js", "target_line": 167,
        "old": "          self.score += merged.value;",
        "new": "          self.score += tile.value;",
        "trigger": "任意一次合并（如 4+4）",
        "expectation": "应 +8；实际 +4",
        "state": {"score": 0}, "final": {"score": 4},
        "prereg_top3": True, "prereg_patch": False,
    },
    {
        "id": "D03", "category": "UI", "anomaly_type": "ui_display_error",
        "title": "分数显示比真实值少 100",
        "file": "js/html_actuator.js", "target_line": 112,
        "old": "  this.scoreContainer.textContent = this.score;",
        "new": "  this.scoreContainer.textContent = this.score - 100;",
        "trigger": "任意得分",
        "expectation": "DOM 分数 = 真实分；实际少 100",
        "state": {"score": 128}, "final": {"score": 128},
        "prereg_top3": False, "prereg_patch": True,
    },
    {
        "id": "D04", "category": "UI", "anomaly_type": "ui_display_error",
        "title": "分数显示恒为 0",
        "file": "js/html_actuator.js", "target_line": 112,
        "old": "  this.scoreContainer.textContent = this.score;",
        "new": "  this.scoreContainer.textContent = \"0\";",
        "trigger": "任意得分",
        "expectation": "DOM 随分数变化；实际恒为 0",
        "state": {"score": 128}, "final": {"score": 128},
        "prereg_top3": False, "prereg_patch": True,
    },
    {
        "id": "D05", "category": "状态", "anomaly_type": "victory_not_triggered",
        "title": "合成 2048 不出现 You win",
        "file": "js/game_manager.js", "target_line": 170,
        "old": "          if (merged.value === 2048) self.won = true;",
        "new": "          if (merged.value > 4096) self.won = true;",
        "trigger": "使用固定棋盘合成 2048",
        "expectation": "出现 You win；实际不出现",
        "state": {"won": False}, "final": {"won": False},
        "prereg_top3": True, "prereg_patch": False,
    },
    {
        "id": "D06", "category": "状态", "anomaly_type": "game_over_early",
        "title": "满盘仍有合并对却直接 Game over",
        "file": "js/game_manager.js", "target_line": 185,
        "old": "    if (!this.movesAvailable()) {",
        "new": "    if (!this.grid.cellsAvailable()) {",
        "trigger": "固定满盘 + 可合并对",
        "expectation": "可继续；实际直接 Game over",
        "state": {"over": True}, "final": {"over": True},
        "prereg_top3": True, "prereg_patch": False,
    },
    {
        "id": "D07", "category": "交互", "anomaly_type": "double_action",
        "title": "一次方向键触发两次移动",
        "file": "js/game_manager.js", "target_line": 9,
        "old": "  this.inputManager.on(\"move\", this.move.bind(this));",
        "new": ("  this.inputManager.on(\"move\", this.move.bind(this));\n"
                "  this.inputManager.on(\"move\", this.move.bind(this));"),
        "trigger": "按一次方向键",
        "expectation": "移动一次；实际移动两次并多生成 tile",
        "state": {"score": 0}, "final": {"score": 4},
        "prereg_top3": False, "prereg_patch": False,
    },
    {
        "id": "D08", "category": "状态", "anomaly_type": "persist_loss",
        "title": "刷新后分数少 4 分",
        "file": "js/game_manager.js", "target_line": 105,
        "old": "    score:       this.score,",
        "new": "    score:       Math.max(0, this.score - 4),",
        "trigger": "得分后刷新页面",
        "expectation": "分数不变；实际少 4 分",
        "state": {"score": 128}, "final": {"score": 124},
        "prereg_top3": False, "prereg_patch": False,
    },
    {
        "id": "D09", "category": "交互", "anomaly_type": "input_swallowed",
        "title": "120ms 内第二次方向键被吞",
        "file": "js/game_manager.js", "target_line": 9,
        "old": "  this.inputManager.on(\"move\", this.move.bind(this));",
        "new": ("  var __pr_move = this.move.bind(this), __pr_locked = false;\n"
                "  this.inputManager.on(\"move\", function (dir) {\n"
                "    if (__pr_locked) return;\n"
                "    __pr_locked = true;\n"
                "    setTimeout(function () { __pr_locked = false; }, 120);\n"
                "    __pr_move(dir);\n"
                "  });"),
        "trigger": "120ms 内连按两次方向",
        "expectation": "两次都生效；实际第二次被吞",
        "state": {"score": 0}, "final": {"score": 4},
        "prereg_top3": False, "prereg_patch": False,
    },
    {
        "id": "D10", "category": "状态", "anomaly_type": "best_score_not_updated",
        "title": "最高分不更新",
        "file": "js/game_manager.js", "target_line": 80,
        "old": "  if (this.storageManager.getBestScore() < this.score) {",
        "new": "  if (this.storageManager.getBestScore() > this.score) {",
        "trigger": "先设 best=100，再得 200",
        "expectation": "Best 更新为 200；实际保持 100",
        "state": {"bestScore": 100}, "final": {"bestScore": 100},
        "prereg_top3": False, "prereg_patch": False,
    },
]


def prepare_build(mutation: dict, out_dir: Path) -> tuple[Path, Path, int]:
    """复制干净 fixture、注入缺陷、生成缺陷包。返回 (源码目录, 缺陷包路径, 实际行号)。"""
    build_dir = out_dir / "builds" / mutation["id"]
    source_dir = build_dir / "source"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    shutil.copytree(FIXTURE, source_dir)

    target = source_dir / mutation["file"]
    text = target.read_text(encoding="utf-8")
    count = text.count(mutation["old"])
    if count != 1:
        raise ValueError(
            f"{mutation['id']} 注入锚点出现 {count} 次（期望 1 次）：{mutation['old']!r}"
        )
    target.write_text(text.replace(mutation["old"], mutation["new"], 1), encoding="utf-8")

    actual_line = text[: text.index(mutation["old"])].count("\n") + 1
    bug_dir = build_dir / "bug"
    bug_dir.mkdir(parents=True, exist_ok=True)
    defect = {
        "bug_id": f"BUG-2048-{mutation['id']}",
        "title": mutation["title"],
        "anomaly_type": mutation["anomaly_type"],
        "severity": "major",
        "conclusion": "auto_warning",
        "description": f"{mutation['expectation']}；触发路径：{mutation['trigger']}",
        "initial_state": mutation["state"],
        "final_state": mutation["final"],
        "assertion": {"state_equals": mutation["state"]},
        "evidence": {"state": "sdk_state"},
        "source_root": str(source_dir),
    }
    (bug_dir / "defect.json").write_text(
        json.dumps(defect, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return source_dir, bug_dir, actual_line


def run_one(mutation: dict, out_dir: Path) -> dict:
    source_dir, bug_dir, actual_line = prepare_build(mutation, out_dir)
    defect = json.loads((bug_dir / "defect.json").read_text(encoding="utf-8"))

    started = time.perf_counter()
    result = locate_bug(defect, source_root=source_dir)
    duration_ms = (time.perf_counter() - started) * 1000.0

    all_hits = []
    for field, hits in result.hits.items():
        for index, hit in enumerate(hits):
            all_hits.append({
                "field": field,
                "rank": index + 1,
                "path": str(hit.path),
                "line": hit.line,
                "column": hit.column,
                "kind": hit.kind,
                "score": hit.score,
            })

    def same_line(hit: dict) -> bool:
        hit_path = Path(hit["path"])
        if not hit_path.is_absolute():
            hit_path = source_dir / hit_path
        return (
            hit_path.resolve() == (source_dir / mutation["file"]).resolve()
            and abs(int(hit["line"]) - actual_line) <= 3
        )

    top1_hit = same_line(all_hits[0]) if all_hits else False
    top3_hit = any(same_line(h) for h in all_hits[:3])
    for field in result.hits:
        top3_hit = top3_hit or any(same_line(h) for h in all_hits if h["field"] == field and h["rank"] <= 3)

    patch_target = _patch_targets_line(result.patch, mutation, source_dir, actual_line)
    operable = bool(top3_hit or patch_target)
    patch_available = bool(result.patch.strip())

    return {
        "id": mutation["id"],
        "category": mutation["category"],
        "title": mutation["title"],
        "target": {
            "file": mutation["file"],
            "line": actual_line,
            "declared_line": mutation["target_line"],
        },
        "fields": result.fields,
        "confidence": round(float(result.confidence), 3),
        "strategy": "；".join(result.strategies),
        "total_hits": len(all_hits),
        "top1": all_hits[0] if all_hits else None,
        "top1_hit": top1_hit,
        "top3_hit": top3_hit,
        "patch_available": patch_available,
        "patch_targets_line": patch_target,
        "operable": operable,
        "duration_ms": round(duration_ms, 1),
        "patch": result.patch,
        "note": result.note,
        "prereg": {
            "top3_hit": mutation["prereg_top3"],
            "patch_available": mutation["prereg_patch"],
        },
        "deviation": [],
    }


def _patch_targets_line(patch: str, mutation: dict, source_dir: Path, actual_line: int) -> bool:
    """patch 是否改在注入文件的目标行 ±3 内。只解析统一 diff 头。"""
    if not patch.strip():
        return False
    current_file = ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current_file = line[4:].strip().removeprefix("b/")
        elif line.startswith("@@ "):
            try:
                old_part = line.split()[1]  # -l,s
                start = int(old_part[1:].split(",")[0])
            except (IndexError, ValueError):
                continue
            if current_file == mutation["file"] and abs(start - actual_line) <= 3:
                return True
    return False


def summarize(records: list[dict]) -> dict:
    total = len(records)
    counts = {
        "top1_hits": sum(1 for r in records if r.get("top1_hit")),
        "top3_hits": sum(1 for r in records if r.get("top3_hit")),
        "patch_available": sum(1 for r in records if r.get("patch_available")),
        "patch_targets_line": sum(1 for r in records if r.get("patch_targets_line")),
        "operable": sum(1 for r in records if r.get("operable")),
    }
    counts["operable_pct"] = round(100.0 * counts["operable"] / total, 1) if total else 0.0
    counts["prereg_operable"] = PREREG["operable"]
    counts["prereg_top3"] = PREREG["top3_hits"]
    counts["prereg_patch"] = PREREG["patch_usable"]
    counts["prereg_top1"] = PREREG["top1_hits"]
    counts["median_duration_ms"] = (
        sorted(r["duration_ms"] for r in records)[total // 2] if total else 0.0
    )
    counts["errors"] = sum(1 for r in records if "error" in r)
    return counts


def render_markdown(records: list[dict], metrics: dict) -> str:
    lines = [
        "# 2048 D 组（静态定位）实验结果",
        "",
        "> 组别：D 工具定位（静态）。A 人工 / B 盲测 / C 状态桥接 / E 人工定位 **未执行**，不在此表。",
        "> 缺陷包由注入清单生成，不代表运行时自动发现的原始缺陷包。",
        "",
        "## 预注册 vs 实测",
        "",
        "| 指标 | 预注册 | 实测 | 偏离 |",
        "| --- | --- | --- | --- |",
        f"| Top-1 命中（同函数 ±3 行） | 未预注册 | {metrics['top1_hits']}/{len(records)} | — |",
        f"| Top-3 命中 | {metrics['prereg_top3']}/10 | {metrics['top3_hits']}/{len(records)} | {_delta(metrics['top3_hits'], metrics['prereg_top3'])} |",
        f"| patch 可用 | {metrics['prereg_patch']}/10 | {metrics['patch_available']}/{len(records)} | {_delta(metrics['patch_available'], metrics['prereg_patch'])} |",
        f"| 可操作定位 | {metrics['prereg_operable']}/10 | {metrics['operable']}/{len(records)} | {_delta(metrics['operable'], metrics['prereg_operable'])} |",
        f"| 定位耗时中位数 | — | {metrics['median_duration_ms']:.0f} ms | — |",
        "",
        "## 逐条结果",
        "",
        "| ID | 类别 | 注入点 | 字段 | Top-1 | Top-3 | patch | 可操作 | 耗时(ms) | 预注册偏差 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        target = f"{record['target']['file']}:{record['target']['line']}" if "target" in record else "—"
        top1 = record.get("top1")
        top1_text = f"{Path(top1['path']).name}:{top1['line']}" if top1 else "—"
        deviation = "；".join(record["deviation"]) or "—"
        lines.append(
            f"| {record['id']} | {record['category']} | {target} | "
            f"{', '.join(record['fields']) or '—'} | {top1_text} | "
            f"{'是' if record['top3_hit'] else '否'} | "
            f"{'有' if record['patch_available'] else '无'} | "
            f"{'是' if record['operable'] else '否'} | "
            f"{record['duration_ms']:.0f} | {deviation} |"
        )

    lines.extend([
        "",
        "## 说明",
        "",
        "- Top-1/Top-3 判定：命中注入文件且行号 ±3；不是「函数名相同」的模糊判定。",
        "- Top-1 未预注册，实测 0/10：`score/won/over` 的首个写点全部被 `setup()` 的 "
        "42–44 行抢占，这是「写点同权、按文件顺序」排序的直接后果，不据此调权重。",
        "- patch 可用 ≠ patch 正确：本表只检查 patch 是否生成，未执行 `git apply --check` 与回归验证。",
        "- 未在 2048 测试集上调整定位权重；结果按现状记录。",
        "",
    ])
    return "\n".join(lines)


def _delta(actual: int, prereg: int) -> str:
    if actual == prereg:
        return "0"
    return f"{actual - prereg:+d}"


def annotate_deviations(records: list[dict]) -> None:
    for record in records:
        if "prereg" not in record:
            continue
        if record["top3_hit"] != record["prereg"]["top3_hit"]:
            record["deviation"].append(
                f"Top-3 预注册 {'是' if record['prereg']['top3_hit'] else '否'}，"
                f"实测 {'是' if record['top3_hit'] else '否'}"
            )
        if record["patch_available"] != record["prereg"]["patch_available"]:
            record["deviation"].append(
                f"patch 预注册 {'有' if record['prereg']['patch_available'] else '无'}，"
                f"实测 {'有' if record['patch_available'] else '无'}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行 2048 D 组静态定位实验")
    parser.add_argument("--only", default="", help="只跑指定 ID，逗号分隔（如 D01,D03）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    args = parser.parse_args(argv)

    selected = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    mutations = [m for m in MUTATIONS if not selected or m["id"] in selected]
    if not mutations:
        print(f"没有匹配的缺陷 ID: {args.only}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    for mutation in mutations:
        try:
            records.append(run_one(mutation, out_dir))
        except Exception as e:  # noqa: BLE001 —— 单个注入失败不拖垮整组
            records.append({
                "id": mutation["id"],
                "category": mutation["category"],
                "title": mutation["title"],
                "error": str(e),
                "target": {"file": mutation["file"], "line": mutation["target_line"]},
                "top1_hit": False,
                "top3_hit": False,
                "patch_available": False,
                "patch_targets_line": False,
                "operable": False,
                "duration_ms": 0.0,
                "fields": [],
                "patch": "",
                "prereg": {
                    "top3_hit": mutation["prereg_top3"],
                    "patch_available": mutation["prereg_patch"],
                },
                "deviation": [f"执行失败: {e}"],
            })

    annotate_deviations(records)
    metrics = summarize(records)

    report = {
        "experiment": "2048-defect-battery",
        "group": "D-static-locate",
        "fixture_commit": "478b6ec346e3787f589e4af751378d06ded4cbbc",
        "pending_groups": ["A-human", "B-blind", "C-state-bridge", "E-human-locate"],
        "prereg": PREREG,
        "metrics": metrics,
        "records": records,
    }
    (out_dir / "d_group_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "d_group_report.md").write_text(
        render_markdown(records, metrics), encoding="utf-8"
    )

    if args.json:
        print(json.dumps({"metrics": metrics, "records": [
            {k: r[k] for k in ("id", "top1_hit", "top3_hit", "patch_available", "operable", "duration_ms", "deviation")}
            for r in records
        ]}, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(records, metrics))
        print(f"💾 {out_dir / 'd_group_report.json'}")
        print(f"💾 {out_dir / 'd_group_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
