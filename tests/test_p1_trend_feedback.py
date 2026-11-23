"""P1 能力测试：跨版本趋势对比 + 误报反馈闭环。"""

from __future__ import annotations

import json

from playranger.core.agent.feedback import (
    load_feedback,
    load_overrides,
    overrides_for,
    register_label,
    resolve_bug_dir,
    save_feedback,
)
from playranger.core.agent.report import build_metrics, render_html
from playranger.core.agent.trend import (
    build_snapshot,
    compare,
    load_history,
    load_snapshot,
    save_snapshot,
)
from playranger.core.agent.triage import (
    CONCLUSION_AUTO_CONFIRMED,
    CONCLUSION_AUTO_WARNING,
    CONCLUSION_HUMAN_REQUIRED,
    triage,
)


def _bug(bug_id: str, title: str, anomaly: str, severity: str = "major",
         conclusion: str = "auto_confirmed") -> dict:
    return {
        "bug_id": bug_id,
        "title": title,
        "anomaly_type": anomaly,
        "severity": severity,
        "conclusion": conclusion,
        "triage": {"confidence": 0.8},
        "evidence_chain": {"ok": True},
        "reproduction_rate": 1.0,
    }


# ─── 跨版本趋势 ─────────────────────────────────────────────────


class TestTrendSnapshot:
    def test_save_and_load_roundtrip(self, tmp_path):
        snapshot = build_snapshot(
            [_bug("BUG-001", "结算分数错误", "score_mismatch")],
            version="v1.0",
            game_name="演示游戏",
            generated_at="2026-11-15 10:00:00",
        )
        path = save_snapshot(snapshot, tmp_path)

        assert path.endswith("summary.json")
        loaded = load_snapshot(tmp_path, "v1.0")
        assert loaded is not None
        assert loaded["version"] == "v1.0"
        assert loaded["metrics"]["total_bugs"] == 1
        assert loaded["bugs"][0]["anomaly_type"] == "score_mismatch"

    def test_missing_snapshot_returns_none(self, tmp_path):
        assert load_snapshot(tmp_path, "v9.9") is None

    def test_history_sorted_by_time(self, tmp_path):
        for version, stamp in (("v2", "2026-11-16 10:00:00"), ("v1", "2026-11-15 10:00:00")):
            save_snapshot(
                build_snapshot([_bug("BUG-001", "x", "score_mismatch")],
                               version=version, generated_at=stamp),
                tmp_path,
            )
        versions = [s["version"] for s in load_history(tmp_path)]
        assert versions == ["v1", "v2"]


class TestTrendCompare:
    def _diff(self):
        baseline = build_snapshot(
            [
                _bug("BUG-001", "结算分数错误", "score_mismatch", "major"),
                _bug("BUG-002", "血量未重置", "hp_not_reset", "minor"),
            ],
            version="v1",
            generated_at="2026-11-15 10:00:00",
        )
        current = build_snapshot(
            [
                _bug("BUG-001", "结算分数错误", "score_mismatch", "critical"),
                _bug("BUG-002", "文字溢出按钮", "text_overflow", "minor"),
            ],
            version="v2",
            generated_at="2026-11-16 10:00:00",
        )
        return compare(current, baseline)

    def test_counts_added_fixed_persisted(self):
        counts = self._diff()["counts"]
        assert counts == {"added": 1, "fixed": 1, "persisted": 1, "changed": 1}

    def test_added_and_fixed_identified(self):
        diff = self._diff()
        assert [b["anomaly_type"] for b in diff["added"]] == ["text_overflow"]
        assert [b["anomaly_type"] for b in diff["fixed"]] == ["hp_not_reset"]

    def test_severity_change_detected(self):
        changed = self._diff()["changed"]
        assert len(changed) == 1
        assert changed[0]["from_severity"] == "major"
        assert changed[0]["to_severity"] == "critical"

    def test_report_renders_trend_section(self, tmp_path):
        html = render_html([], trend=self._diff(), report_dir=tmp_path)
        assert "跨版本趋势对比" in html
        assert "新增缺陷" in html
        assert "已修复缺陷" in html
        assert "文字溢出按钮" in html


# ─── 误报反馈闭环 ───────────────────────────────────────────────


class TestFeedbackRules:
    def test_two_false_positives_downgrade(self, tmp_path):
        first = register_label(tmp_path, anomaly_type="text_overflow", label="false_positive")
        assert first["fp_count"] == 1
        assert "conclusion" not in first

        second = register_label(tmp_path, anomaly_type="text_overflow", label="false_positive")
        assert second["fp_count"] == 2
        assert second["conclusion"] == "auto_warning"

        rule = overrides_for(load_overrides(tmp_path), "text_overflow")
        assert rule["conclusion"] == "auto_warning"

    def test_true_positive_resets_false_positive_counter(self, tmp_path):
        register_label(tmp_path, anomaly_type="text_overflow", label="false_positive")
        rule = register_label(tmp_path, anomaly_type="text_overflow", label="true_positive")
        assert rule["fp_count"] == 0
        assert rule["tp_count"] == 1

    def test_severity_override_persisted(self, tmp_path):
        register_label(
            tmp_path, anomaly_type="text_overflow", label="false_positive", severity="info"
        )
        assert overrides_for(load_overrides(tmp_path), "text_overflow")["severity"] == "info"

    def test_triage_applies_override(self):
        base = triage(
            "score_mismatch",
            evidence={"状态差异": "sdk_state"},
            reproduction_rate=1.0,
        )
        assert base.conclusion == CONCLUSION_AUTO_CONFIRMED

        downgraded = triage(
            "score_mismatch",
            evidence={"状态差异": "sdk_state"},
            reproduction_rate=1.0,
            override={"fp_count": 2},
        )
        assert downgraded.conclusion == CONCLUSION_AUTO_WARNING
        assert any("误报" in reason for reason in downgraded.reasons)

        forced = triage(
            "score_mismatch",
            evidence={"状态差异": "sdk_state"},
            reproduction_rate=1.0,
            override={"severity": "info"},
        )
        assert forced.severity == "info"


class TestFeedbackFiles:
    def test_save_feedback_appends_records(self, tmp_path):
        bug_dir = tmp_path / "BUG-001"
        bug_dir.mkdir()
        (bug_dir / "defect.json").write_text(
            json.dumps({"anomaly_type": "text_overflow"}, ensure_ascii=False),
            encoding="utf-8",
        )

        save_feedback(bug_dir, label="false_positive", note="按钮截断是设计")
        save_feedback(bug_dir, label="true_positive", note="复核后确认真实缺陷")

        data = load_feedback(bug_dir)
        assert data["label"] == "true_positive"
        assert len(data["records"]) == 2
        assert data["records"][0]["note"] == "按钮截断是设计"

    def test_resolve_bug_dir_by_id(self, tmp_path):
        bug_dir = tmp_path / "BUG-002"
        bug_dir.mkdir()
        (bug_dir / "defect.json").write_text("{}", encoding="utf-8")

        assert resolve_bug_dir("BUG-002", tmp_path) == bug_dir.resolve()
        assert resolve_bug_dir(str(bug_dir), tmp_path) == bug_dir.resolve()
        assert resolve_bug_dir("BUG-404", tmp_path) is None


# ─── 复核一致率指标 ─────────────────────────────────────────────


class TestReviewMetric:
    def test_review_agreement_counted(self):
        bugs = [
            _bug("BUG-001", "真实缺陷", "score_mismatch", conclusion=CONCLUSION_AUTO_CONFIRMED),
            _bug("BUG-002", "误报", "text_overflow", conclusion=CONCLUSION_HUMAN_REQUIRED),
            _bug("BUG-003", "未复核", "hp_not_reset"),
        ]
        bugs[0]["feedback"] = {"label": "true_positive"}
        bugs[1]["feedback"] = {"label": "false_positive"}

        metrics = build_metrics(bugs)
        assert metrics["review_total"] == 2
        assert metrics["review_agree"] == 2
        assert metrics["review_rate"] == 1.0
        assert metrics["false_positive"] == 1

    def test_review_disagreement_counted(self):
        bug = _bug("BUG-001", "判错了", "text_overflow", conclusion=CONCLUSION_AUTO_CONFIRMED)
        bug["feedback"] = {"label": "false_positive"}
        metrics = build_metrics([bug])
        assert metrics["review_total"] == 1
        assert metrics["review_agree"] == 0
