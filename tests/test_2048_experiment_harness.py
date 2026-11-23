"""2048 D 组实验执行器的回归测试（不启动浏览器）。"""

from __future__ import annotations

import json

from tools import run_2048_experiment as exp


def test_mutation_anchors_are_unique():
    for mutation in exp.MUTATIONS:
        text = (exp.FIXTURE / mutation["file"]).read_text(encoding="utf-8")
        assert text.count(mutation["old"]) == 1, mutation["id"]


def test_d_group_matches_prereg(tmp_path):
    code = exp.main(["--out", str(tmp_path)])
    assert code == 0

    report = json.loads((tmp_path / "d_group_report.json").read_text(encoding="utf-8"))
    metrics = report["metrics"]

    assert metrics["errors"] == 0
    assert metrics["top3_hits"] == 4
    assert metrics["patch_available"] == 2
    assert metrics["operable"] == 6
    assert metrics["patch_targets_line"] == 2

    by_id = {record["id"]: record for record in report["records"]}
    assert by_id["D01"]["top3_hit"] is True and by_id["D01"]["patch_available"] is False
    assert by_id["D03"]["patch_targets_line"] is True
    assert by_id["D04"]["patch_targets_line"] is True
    for failed in ("D07", "D08", "D09", "D10"):
        assert by_id[failed]["top3_hit"] is False
        assert by_id[failed]["operable"] is False
