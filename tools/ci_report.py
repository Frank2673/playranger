"""把缺陷包转换成 CI 可直接消费的产物：JUnit XML + SARIF + Markdown 摘要。

用法：
    python tools/ci_report.py --bugs output/bugs --out-dir output/ci \
        --fail-on blocker,critical --allow 0

退出码：
    0 —— 门禁通过
    1 —— 门禁未通过（fail-on 严重度数量超过 allow）
    2 —— 输入错误（缺陷目录不存在等）

设计边界：
- 只读缺陷包，不重新跑测试
- SARIF 的物理位置来自缺陷包的 code_location / provenance（有则给，无则只给逻辑位置）
- JUnit 把每条缺陷映射成一个 testcase，便于 GitHub/GitLab/Jenkins 原样展示
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SEVERITY_LEVEL = {
    "blocker": "error",
    "critical": "error",
    "major": "warning",
    "minor": "note",
    "info": "note",
}
SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "critical": 3, "blocker": 4}


def load_bugs(bugs_dir: str | Path) -> list[dict]:
    """读取目录下所有 defect.json，并合并 code_location/provenance 摘要。"""
    root = Path(bugs_dir)
    if not root.exists():
        raise FileNotFoundError(f"缺陷目录不存在: {root}")

    bugs: list[dict] = []
    for defect_path in sorted(root.glob("*/defect.json")):
        try:
            defect = json.loads(defect_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        bug_dir = defect_path.parent

        location = _read_json(bug_dir / "code_location.json")
        provenance = _read_json(bug_dir / "provenance.json")

        bugs.append({
            **defect,
            "bug_dir": str(bug_dir),
            "code_location": location,
            "provenance": provenance,
        })
    return bugs


def build_junit(bugs: list[dict], fail_on: set[str]) -> str:
    suites = ET.Element("testsuites", {
        "name": "PlayRanger",
        "tests": str(len(bugs)),
        "failures": str(sum(1 for b in bugs if _severity(b) in fail_on)),
    })
    suite = ET.SubElement(suites, "testsuite", {
        "name": "playranger.bugs",
        "tests": str(len(bugs)),
        "failures": str(sum(1 for b in bugs if _severity(b) in fail_on)),
        "errors": "0",
        "time": "0",
    })

    for bug in bugs:
        anomaly = str(bug.get("anomaly_type", "unknown"))
        case = ET.SubElement(suite, "testcase", {
            "classname": f"playranger.{anomaly}",
            "name": f"{bug.get('bug_id', 'BUG-???')} {bug.get('title', '')}".strip(),
        })
        if _severity(bug) in fail_on:
            failure = ET.SubElement(case, "failure", {
                "message": f"[{_severity(bug)}] {bug.get('title', '')}",
                "type": anomaly,
            })
            failure.text = _failure_text(bug)

    return ET.tostring(suites, encoding="unicode")


def build_sarif(bugs: list[dict], version: str = "0.0.0") -> dict:
    rules = _build_rules(bugs)
    results = []

    for bug in bugs:
        anomaly = str(bug.get("anomaly_type", "unknown"))
        location = _physical_location(bug)
        result: dict[str, Any] = {
            "ruleId": anomaly,
            "level": SEVERITY_LEVEL.get(_severity(bug), "note"),
            "message": {"text": _failure_text(bug)},
            "partialFingerprints": {
                "playrangerBugId": str(bug.get("bug_id", "")),
            },
        }
        if location:
            result["locations"] = [location]
        results.append(result)

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "PlayRanger",
                    "version": version,
                    "rules": rules,
                }
            },
            "results": results,
        }],
    }


def build_markdown(bugs: list[dict], fail_on: set[str], gate_passed: bool) -> str:
    by_sev: dict[str, int] = {}
    for bug in bugs:
        sev = _severity(bug)
        by_sev[sev] = by_sev.get(sev, 0) + 1

    lines = [
        "# PlayRanger CI 摘要",
        "",
        f"- 缺陷总数：{len(bugs)}",
        f"- 门禁：{'通过' if gate_passed else '未通过'}（fail-on: {', '.join(sorted(fail_on)) or '无'}）",
        "",
        "## 按严重度",
        "",
        "| 严重度 | 数量 |",
        "| --- | --- |",
    ]
    for sev in ("blocker", "critical", "major", "minor", "info"):
        lines.append(f"| {sev} | {by_sev.get(sev, 0)} |")

    lines.extend(["", "## 缺陷清单", "", "| Bug | 类型 | 严重度 | 触发步 | 定位 |", "| --- | --- | --- | --- | --- |"])
    for bug in sorted(bugs, key=lambda b: -SEVERITY_RANK.get(_severity(b), 0)):
        loc = bug.get("code_location") or {}
        hits = loc.get("hits") or {}
        first = next((h[0] for h in hits.values() if h), None)
        loc_text = (
            f"{first.get('path')}:{first.get('line')}" if first else "未定位"
        )
        prov = bug.get("provenance") or {}
        lines.append(
            f"| {bug.get('bug_id', '')} | {bug.get('anomaly_type', '')} | "
            f"{_severity(bug)} | {prov.get('origin_step', '-')} | {loc_text} |"
        )
    return "\n".join(lines) + "\n"


def evaluate_gate(bugs: list[dict], fail_on: set[str], allow: int) -> tuple[bool, int]:
    hits = sum(1 for b in bugs if _severity(b) in fail_on)
    return hits <= allow, hits


def _build_rules(bugs: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for bug in bugs:
        anomaly = str(bug.get("anomaly_type", "unknown"))
        if anomaly in seen:
            continue
        seen[anomaly] = {
            "id": anomaly,
            "name": anomaly,
            "shortDescription": {"text": f"PlayRanger 检测到 {anomaly} 类缺陷"},
        }
    return list(seen.values())


def _physical_location(bug: dict) -> dict | None:
    location = bug.get("code_location") or {}
    hits = location.get("hits") or {}
    first = next((h[0] for h in hits.values() if h), None)
    if not first:
        return None
    return {
        "physicalLocation": {
            "artifactLocation": {"uri": str(first.get("path", ""))},
            "region": {
                "startLine": int(first.get("line", 1) or 1),
                "startColumn": int(first.get("column", 1) or 1),
            },
        }
    }


def _failure_text(bug: dict) -> str:
    prov = bug.get("provenance") or {}
    parts = [
        f"[{_severity(bug)}] {bug.get('title', '')}",
        f"类型: {bug.get('anomaly_type', '')}",
        f"结论: {bug.get('conclusion', '')}",
    ]
    if prov.get("origin_step") is not None:
        parts.append(f"触发步骤: {prov.get('origin_step')}")
    diff = prov.get("state_diff") or []
    if diff:
        parts.append("状态差异: " + "; ".join(
            f"{d.get('field')} {d.get('before')}->{d.get('after')}" for d in diff[:3]
        ))
    if bug.get("description"):
        parts.append(str(bug["description"]))
    return "\n".join(parts)


def _severity(bug: dict) -> str:
    return str(bug.get("severity", "info"))


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 PlayRanger CI 报告（JUnit/SARIF/Markdown）")
    parser.add_argument("--bugs", default="output/bugs", help="缺陷目录")
    parser.add_argument("--out-dir", default="output/ci", help="输出目录")
    parser.add_argument("--fail-on", default="blocker,critical",
                        help="触发门禁的严重度，逗号分隔（默认 blocker,critical）")
    parser.add_argument("--allow", type=int, default=0,
                        help="允许的 fail-on 缺陷数量（默认 0）")
    parser.add_argument("--version", default="0.1.0", help="写入 SARIF 的工具版本")
    args = parser.parse_args(argv)

    fail_on = {s.strip() for s in args.fail_on.split(",") if s.strip()}
    unknown = fail_on - set(SEVERITY_RANK)
    if unknown:
        print(f"未知严重度: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    try:
        bugs = load_bugs(args.bugs)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2

    gate_passed, hits = evaluate_gate(bugs, fail_on, args.allow)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "junit.xml").write_text(build_junit(bugs, fail_on), encoding="utf-8")
    (out_dir / "playranger.sarif").write_text(
        json.dumps(build_sarif(bugs, version=args.version), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "summary.md").write_text(
        build_markdown(bugs, fail_on, gate_passed), encoding="utf-8"
    )

    print(f"缺陷 {len(bugs)} 条；fail-on 命中 {hits} 条（允许 {args.allow}）")
    print(f"输出: {out_dir / 'junit.xml'}")
    print(f"输出: {out_dir / 'playranger.sarif'}")
    print(f"输出: {out_dir / 'summary.md'}")
    print("门禁:", "通过" if gate_passed else "未通过")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
