"""Day5 测试 —— Bug 溯源 / 修复深化方案 / 反馈数据可视化报告 / Recorder 集成。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from playranger.core.agent.advisor import (
    EFFORT_LEVELS,
    RISK_LEVELS,
    FixPlan,
    advise,
    cluster_bugs,
)
from playranger.core.agent.evidence import EvidenceChain
from playranger.core.agent.intel import (
    FIX_PLAN_FILE,
    PROVENANCE_FILE,
    analyze_bug,
    provenance_summary,
    save_intel,
)
from playranger.core.agent.provenance import (
    BugProvenance,
    EvidenceFinding,
    build_provenance,
    provenance_from_file,
)
from playranger.core.agent.recorder import BugRecorder, load_defect
from playranger.core.agent.report import build_metrics, render_html, write_report

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.build_report import (  # noqa: E402
    export_tickets,
    find_bug_dirs,
    load_bug_entry,
    main as cli_main,
)


# ─── 公共夹具 ───────────────────────────────────────────────────


def _failed_trace() -> list[dict]:
    """两步轨迹：第 2 步验证失败（StepInfo 结构）。"""
    return [
        {
            "step": 1,
            "state": {"hp": 100, "score": 0, "scene": "menu", "available_actions": ["start"]},
            "action": {"action_type": "click", "target": "#start", "params": {}},
            "execution_ok": True,
            "verification": {"success": True, "reason": ""},
            "correction_level": "none",
        },
        {
            "step": 2,
            "state": {"hp": 100, "score": 0, "scene": "level2", "available_actions": ["restart"]},
            "action": {"action_type": "click", "target": "#restart", "params": {}},
            "execution_ok": True,
            "verification": {"success": False, "reason": "HP 未重置"},
            "correction_level": "L2_path",
        },
    ]


def _failed_bug(**overrides) -> dict:
    bug = {
        "bug_id": "BUG-001",
        "title": "重新开始后 HP 未重置",
        "anomaly_type": "hp_not_reset",
        "severity": "major",
        "conclusion": "auto_confirmed",
        "initial_state": {"hp": 100, "score": 0, "scene": "menu"},
        "final_state": {"hp": 40, "score": 0, "scene": "level2"},
        "action_trace": _failed_trace(),
        "assertion": {"type": "state_equals", "field": "hp", "value": 100},
        "evidence": {"hp_not_reset": "sdk_state"},
        "reproduction_rate": 1.0,
        "reproduction_attempts": 5,
        "reproduced_count": 5,
    }
    bug.update(overrides)
    return bug


# ─── 1. 溯源 ────────────────────────────────────────────────────


class TestProvenance:
    def test_origin_is_last_failed_step(self):
        prov = build_provenance(_failed_bug())
        assert prov.origin_step == 2
        assert prov.origin_action["action_type"] == "click"
        assert prov.origin_action["target"] == "#restart"

    def test_all_success_uses_last_step(self):
        trace = [
            {"step": 1, "state": {"hp": 100}, "execution_ok": True,
             "verification": {"success": True}},
            {"step": 2, "state": {"hp": 90}, "execution_ok": True,
             "verification": {"success": True}},
        ]
        prov = build_provenance({"bug_id": "B", "action_trace": trace})
        assert prov.origin_step == 2

    def test_empty_trace_records_note(self):
        prov = build_provenance({"bug_id": "B-EMPTY"})
        assert prov.origin_step is None
        assert any("没有动作轨迹" in n for n in prov.notes)

    def test_state_diff_detects_changed_field(self):
        prov = build_provenance(_failed_bug())
        fields = {d["field"]: d for d in prov.state_diff}
        assert "hp" in fields
        assert fields["hp"]["before"] == 100
        assert fields["hp"]["after"] == 40
        assert fields["hp"]["expected"] == 100

    def test_state_diff_ignores_noise_fields(self):
        prov = build_provenance(_failed_bug())
        assert all(d["field"] != "available_actions" for d in prov.state_diff)

    def test_state_diff_marks_unchanged_assertion_field(self):
        bug = _failed_bug(
            assertion={"type": "state_equals", "field": "score", "value": 0},
        )
        prov = build_provenance(bug)
        score = next(d for d in prov.state_diff if d["field"] == "score")
        assert score.get("note", "") != ""

    def test_assertion_equals_pass_and_fail(self):
        passed = build_provenance(
            _failed_bug(final_state={"hp": 100, "score": 0, "scene": "level2"})
        )
        assert passed.assertion_check["passed"] is True

        failed = build_provenance(_failed_bug(final_state={"hp": 40, "score": 0, "scene": "level2"}))
        assert failed.assertion_check["passed"] is False

    def test_assertion_numeric_comparisons(self):
        less = build_provenance(
            _failed_bug(
                assertion={"type": "state_less_than", "field": "hp", "value": 50},
                final_state={"hp": 40},
            )
        )
        assert less.assertion_check["passed"] is True

        greater = build_provenance(
            _failed_bug(
                assertion={"type": "state_greater_than", "field": "score", "value": 10},
                final_state={"score": 0},
            )
        )
        assert greater.assertion_check["passed"] is False

    def test_unknown_assertion_type_never_passes(self):
        prov = build_provenance(
            _failed_bug(assertion={"type": "scene_changed", "field": "scene", "value": "menu"})
        )
        assert prov.assertion_check["passed"] is False

    def test_findings_from_evidence_and_assertion(self):
        prov = build_provenance(_failed_bug())
        channels = prov.channels
        assert "sdk_state" in channels
        assert "assertion" in channels
        assert channels == sorted(channels)

    def test_findings_include_exception(self):
        trace = _failed_trace()
        trace[1]["execution_ok"] = False
        trace[1]["execution_error"] = "Element not found: #restart"
        prov = build_provenance(_failed_bug(action_trace=trace))
        exception = [f for f in prov.findings if f.channel == "exception"]
        assert exception and "Element not found" in exception[0].signal

    def test_strategy_stuck_channel(self):
        prov = build_provenance(
            {"bug_id": "B-STUCK", "anomaly_type": "strategy_stuck", "action_trace": []}
        )
        assert "strategy" in prov.channels

    def test_findings_deduplicated(self):
        prov = build_provenance(
            _failed_bug(evidence={"hp_not_reset": "sdk_state"}, assertion={})
        )
        keys = [(f.channel, f.signal) for f in prov.findings]
        assert len(keys) == len(set(keys))

    def test_replay_window_truncation(self):
        trace = [
            {"step": i, "state": {"hp": 100}, "action": {"action_type": "click", "target": f"#{i}"},
             "execution_ok": True, "verification": {"success": True}}
            for i in range(1, 6)
        ]
        trace.append(
            {"step": 6, "state": {"hp": 0}, "action": {"action_type": "click", "target": "#dead"},
             "execution_ok": True, "verification": {"success": False, "reason": "卡死"}}
        )
        prov = build_provenance({"bug_id": "B-LONG", "action_trace": trace}, max_actions=3)
        assert prov.replay_truncated is True
        assert len(prov.replay_actions) == 3
        assert prov.replay_actions[-1]["target"] == "#dead"

    def test_screenshots_loaded_from_chain(self, tmp_path):
        shot = tmp_path / "screenshots" / "failure.png"
        shot.parent.mkdir(parents=True)
        shot.write_bytes(b"fake-png")
        chain = EvidenceChain(tmp_path)
        chain.add("screenshots/failure.png", kind="failure", step=2, action="click")
        chain.save()

        prov = build_provenance(_failed_bug(), bug_dir=tmp_path)
        assert len(prov.screenshots) == 1
        assert prov.screenshots[0]["kind"] == "failure"
        assert prov.screenshots[0]["sha256"]

    def test_to_markdown_contains_sections(self):
        md = build_provenance(_failed_bug()).to_markdown()
        for section in ("触发链", "证据来源", "状态差异", "断言校验", "复现动作序列"):
            assert section in md

    def test_provenance_from_file(self, tmp_path):
        (tmp_path / "defect.json").write_text(
            json.dumps({
                "bug_id": "BUG-FILE", "title": "读文件", "anomaly_type": "hp_not_reset",
                "severity": "major", "evidence": {"hp_not_reset": "sdk_state"},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        (tmp_path / "final_state.json").write_text(json.dumps({"hp": 10}), encoding="utf-8")
        (tmp_path / "action_trace.jsonl").write_text(
            json.dumps({"step": 1, "execution_ok": False, "state": {"hp": 100}},
                       ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        prov = provenance_from_file(tmp_path)
        assert prov.bug_id == "BUG-FILE"
        assert prov.origin_step == 1


# ─── 2. 修复深化方案 ────────────────────────────────────────────


class TestAdvisor:
    def test_rule_hit_for_known_anomaly(self):
        plan = advise(_failed_bug())
        assert plan.effort in EFFORT_LEVELS
        assert plan.regression_risk in RISK_LEVELS
        assert "玩家状态" in plan.affected_modules
        assert plan.fix_steps

    def test_unknown_anomaly_falls_back(self):
        plan = advise({"bug_id": "B-X", "anomaly_type": "never_seen"})
        assert plan.anomaly_type == "never_seen"
        assert plan.fix_steps
        assert "待定位" in plan.affected_modules

    def test_specialize_injects_measured_diff(self):
        plan = advise(_failed_bug())
        assert any("实测证据" in c and "hp" in c for c in plan.root_causes)

    def test_specialize_puts_assertion_first(self):
        plan = advise(_failed_bug())
        assert "断言" in plan.root_causes[0]

    def test_verification_prepends_replay(self):
        plan = advise(_failed_bug())
        assert plan.verification_steps[0].startswith("从第 2 步")

    def test_confidence_grows_with_evidence(self):
        bare = advise({"bug_id": "B-BARE", "anomaly_type": "hp_not_reset"})
        rich = advise(_failed_bug())
        assert bare.confidence < rich.confidence
        assert 0.0 <= rich.confidence <= 1.0

    def test_confidence_capped_with_screenshots(self):
        prov = build_provenance(_failed_bug())
        prov.screenshots = [{"file": "screenshots/failure.png", "kind": "failure", "step": 2,
                             "sha256": "deadbeef"}]
        plan = advise(_failed_bug(), prov)
        assert plan.confidence <= 1.0
        assert any("截图" in v for v in plan.verification_steps)

    def test_cluster_bugs_groups_by_stage(self):
        bugs = [
            {"bug_id": "B-1", "anomaly_type": "hp_not_reset", "severity": "blocker", "title": "t1"},
            {"bug_id": "B-2", "anomaly_type": "hp_not_reset", "severity": "major", "title": "t2"},
            {"bug_id": "B-3", "anomaly_type": "hp_not_reset", "severity": "minor", "title": "t3"},
            {"bug_id": "B-4", "anomaly_type": "score_mismatch", "severity": "blocker", "title": "t4"},
        ]
        clusters = cluster_bugs(bugs)
        assert len(clusters) == 4
        assert clusters[0]["highest_severity"] == "blocker"
        stages = {c["stage"] for c in clusters}
        assert stages == {"立即修", "本迭代修", "排期修"}

    def test_to_markdown_has_checklist(self):
        md = advise(_failed_bug()).to_markdown()
        assert "可能根因" in md
        assert "修复步骤" in md
        assert "- [ ]" in md

    def test_to_dict_rounds_confidence(self):
        data = advise(_failed_bug()).to_dict()
        assert isinstance(data["confidence"], float)
        assert data["fix_steps"][0]["title"]


# ─── 3. 反馈数据可视化报告 ──────────────────────────────────────


def _two_bugs() -> list[dict]:
    return [
        {
            "bug_id": "BUG-001", "title": "黑屏", "anomaly_type": "black_screen",
            "severity": "blocker", "conclusion": "auto_confirmed",
            "triage": {"confidence": 0.9}, "reproduction_rate": 1.0,
            "evidence_chain": {"ok": True, "checked": 2},
        },
        {
            "bug_id": "BUG-002", "title": "分数显示错误", "anomaly_type": "score_mismatch",
            "severity": "major", "conclusion": "human_required",
            "triage": {"confidence": 0.5}, "reproduction_rate": 0.0,
            "evidence_chain": {"ok": False, "checked": 0},
        },
    ]


def _history() -> list[dict]:
    return [
        {"step": 1, "success": True},
        {"step": 2, "execution_ok": False},
        {"step": 3, "execution_ok": True,
         "verification": {"success": True, "correction_level": "L1_action"}},
    ]


class TestReport:
    def test_metrics_empty(self):
        m = build_metrics([])
        assert m["total_bugs"] == 0
        assert m["auto_rate"] == 0.0
        assert m["highest_severity"] == "info"
        assert m["step_success_rate"] == 0.0

    def test_metrics_severity_and_conclusion(self):
        m = build_metrics(_two_bugs())
        assert m["by_severity"]["blocker"] == 1
        assert m["by_severity"]["major"] == 1
        assert m["by_conclusion"]["auto_confirmed"] == 1
        assert m["by_conclusion"]["human_required"] == 1
        assert m["by_anomaly"]["black_screen"] == 1
        assert m["highest_severity"] == "blocker"
        assert m["auto_rate"] == 0.5

    def test_metrics_confidence_and_evidence(self):
        m = build_metrics(_two_bugs())
        assert m["avg_confidence"] == pytest.approx(0.7)
        assert m["avg_reproduction"] == pytest.approx(0.5)
        assert m["evidence_ok"] == 1
        assert m["evidence_failed"] == 1
        assert m["evidence_rate"] == pytest.approx(0.5)

    def test_metrics_step_stats(self):
        m = build_metrics(_two_bugs(), _history())
        assert m["steps_total"] == 3
        assert m["steps_failed"] == 1
        assert m["steps_corrected"] == 1
        assert m["step_success_rate"] == pytest.approx(0.667, abs=1e-3)

    def test_metrics_invalid_values_normalized(self):
        m = build_metrics([
            {"severity": "nope", "conclusion": "bad", "anomaly_type": "x"},
        ])
        assert m["by_severity"]["info"] == 1
        assert m["by_conclusion"]["human_required"] == 1

    def test_html_contains_core_sections(self):
        html = render_html(_two_bugs(), history=_history(), game_name="Demo")
        assert "缺陷总数" in html
        assert "执行时间线" in html
        assert "同类缺陷聚类" in html
        assert "修复深化方案" in html
        assert "触发链" in html
        assert "Demo" in html

    def test_html_escapes_user_text(self):
        html = render_html([{
            "bug_id": "BUG-X", "title": "<img src=x onerror=alert(1)>",
            "anomaly_type": "logic_error", "severity": "major",
        }])
        assert "<img src=x" not in html
        assert "&lt;img" in html

    def test_html_empty_bugs(self):
        html = render_html([])
        assert "本轮没有发现缺陷" in html
        assert "暂无数据" in html

    def test_write_report_relative_screenshot(self, tmp_path):
        bug_dir = tmp_path / "bugs" / "BUG-001"
        shot = bug_dir / "screenshots" / "failure.png"
        shot.parent.mkdir(parents=True)
        shot.write_bytes(b"png-bytes")

        chain = EvidenceChain(bug_dir)
        chain.add("screenshots/failure.png", kind="failure", step=2)
        chain.save()

        bug = {
            "bug_id": "BUG-001", "title": "h", "anomaly_type": "hp_not_reset",
            "severity": "major", "bug_dir": str(bug_dir),
        }
        out = write_report([bug], tmp_path / "report.html")
        html = Path(out).read_text(encoding="utf-8")
        assert "bugs/BUG-001/screenshots/failure.png" in html
        assert Path(out).exists()

    def test_metrics_zero_division_safe(self):
        m = build_metrics([{"severity": "info", "conclusion": "auto_confirmed"}])
        assert m["auto_rate"] == 1.0
        assert m["avg_confidence"] == 0.0
        assert m["evidence_rate"] == 0.0


# ─── 4. Intel 聚合 + Recorder 集成 ──────────────────────────────
class TestIntelAndRecorder:
    def test_analyze_bug_returns_both(self):
        data = analyze_bug(_failed_bug())
        assert set(data) == {"provenance", "fix_plan"}
        assert data["provenance"]["origin_step"] == 2
        assert data["fix_plan"]["anomaly_type"] == "hp_not_reset"

    def test_provenance_summary_fields(self):
        prov = build_provenance(_failed_bug()).to_dict()
        summary = provenance_summary(prov)
        assert summary["origin_step"] == 2
        assert summary["origin_action"] == "click"
        assert summary["origin_target"] == "#restart"
        assert summary["state_diff_count"] >= 1
        assert summary["has_assertion"] is True

    def test_save_intel_writes_files(self, tmp_path):
        intel = save_intel(_failed_bug(), tmp_path)
        assert intel["saved"] is True
        assert (tmp_path / PROVENANCE_FILE).exists()
        assert (tmp_path / FIX_PLAN_FILE).exists()

    def test_capture_writes_intel_files(self, tmp_path):
        shot1 = tmp_path / "s1.png"
        shot2 = tmp_path / "s2.png"
        shot1.write_bytes(b"s1")
        shot2.write_bytes(b"s2")

        rec = BugRecorder(output_dir=tmp_path / "bugs")
        bug_dir = rec.capture(
            title="重新开始后 HP 未重置",
            anomaly_type="hp_not_reset",
            severity="major",
            conclusion="auto_confirmed",
            initial_state={"hp": 100, "score": 0, "scene": "menu"},
            final_state={"hp": 40, "score": 0, "scene": "level2"},
            action_trace=_failed_trace(),
            assertion={"type": "state_equals", "field": "hp", "value": 100},
            screenshot_initial=str(shot1),
            screenshot_failure=str(shot2),
            evidence={"hp_not_reset": "sdk_state"},
            seed=42,
            game_name="DemoGame",
            source={"channel": "sdk_state", "origin_step": 2, "detected_by": "verifier"},
        )

        root = Path(bug_dir)
        assert (root / PROVENANCE_FILE).exists()
        assert (root / FIX_PLAN_FILE).exists()
        assert (root / "evidence_chain.json").exists()

        defect = json.loads((root / "defect.json").read_text(encoding="utf-8"))
        assert defect["source"]["channel"] == "sdk_state"
        assert defect["provenance_summary"]["origin_step"] == 2
        assert defect["provenance_summary"]["screenshot_count"] == 2
        assert defect["fix_plan_summary"]["root_cause_count"] >= 1
        assert defect["fix_plan_summary"]["effort"] in EFFORT_LEVELS

        prov = json.loads((root / PROVENANCE_FILE).read_text(encoding="utf-8"))
        assert prov["origin_step"] == 2
        assert "sdk_state" in prov["channels"]

        plan = json.loads((root / FIX_PLAN_FILE).read_text(encoding="utf-8"))
        assert plan["anomaly_type"] == "hp_not_reset"
        assert plan["verification_steps"]

    def test_load_defect_roundtrip(self, tmp_path):
        rec = BugRecorder(output_dir=tmp_path / "bugs")
        bug_dir = rec.capture(
            title="分数错误",
            anomaly_type="score_mismatch",
            severity="major",
            initial_state={"score": 0},
            final_state={"score": 300},
            action_trace=[{"step": 1, "execution_ok": True, "state": {"score": 0}}],
            assertion={"type": "state_equals", "field": "score", "value": 300},
            seed=7,
            tags=["auto_detected"],
        )
        record = load_defect(bug_dir)
        assert record.bug_id == "BUG-001"
        assert record.title == "分数错误"
        assert record.seed == 7
        assert record.tags == ["auto_detected"]
        assert len(record.action_trace) == 1
        assert record.assertion["field"] == "score"

    def test_cli_find_and_load(self, tmp_path):
        shot = tmp_path / "s.png"
        shot.write_bytes(b"png")
        rec = BugRecorder(output_dir=tmp_path / "bugs")
        rec.capture(
            title="只跑一次",
            anomaly_type="hp_not_reset",
            severity="major",
            initial_state={"hp": 100},
            final_state={"hp": 0},
            action_trace=_failed_trace(),
            screenshot_initial=str(shot),
            evidence={"hp_not_reset": "sdk_state"},
        )

        dirs = find_bug_dirs(tmp_path / "bugs")
        assert len(dirs) == 1
        entry = load_bug_entry(dirs[0])
        assert entry["bug_id"] == "BUG-001"
        assert entry["provenance"]["origin_step"] == 2
        assert entry["fix_plan"]["anomaly_type"] == "hp_not_reset"
        assert entry["evidence_chain"]["ok"] is True

    def test_cli_find_missing_dir(self, tmp_path):
        assert find_bug_dirs(tmp_path / "not-exist") == []

    def test_cli_main_end_to_end(self, tmp_path, capsys):
        rec = BugRecorder(output_dir=tmp_path / "bugs")
        rec.capture(
            title="黑屏",
            anomaly_type="black_screen",
            severity="blocker",
            initial_state={"scene": "level1"},
            final_state={"scene": "level1"},
            action_trace=_failed_trace(),
            evidence={"black_screen": "vision_vlm"},
        )

        code = cli_main([
            "--bugs", str(tmp_path / "bugs"),
            "--out", str(tmp_path / "report.html"),
            "--tickets", str(tmp_path / "tickets"),
        ])
        assert code == 0
        assert (tmp_path / "report.html").exists()

        tickets = list((tmp_path / "tickets").glob("*.md"))
        assert len(tickets) == 1
        text = tickets[0].read_text(encoding="utf-8")
        assert "触发步骤" in text
        assert "修复方案" in text
        assert "验证清单" in text

    def test_export_tickets_empty(self, tmp_path):
        assert export_tickets([], tmp_path / "tickets") == []


# ─── 5. API 端点（溯源 / 修复方案 / HTML 报告） ─────────────────


class TestApiIntelEndpoints:
    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient

        import playranger.api.main as api

        client = TestClient(api.app)
        yield client

        with api._LOCK:
            api._BUGS.pop("BUG-API-D5", None)
            if "BUG-API-D5" in api._BUG_ORDER:
                api._BUG_ORDER.remove("BUG-API-D5")

    def _register(self) -> None:
        import playranger.api.main as api

        prov = build_provenance(_failed_bug()).to_dict()
        plan = advise(_failed_bug()).to_dict()
        entry = {
            "bug_id": "BUG-API-D5",
            "title": "重新开始后 HP 未重置",
            "anomaly_type": "hp_not_reset",
            "severity": "major",
            "conclusion": "auto_confirmed",
            "description": "演示",
            "seed": 1,
            "initial_state": {"hp": 100},
            "final_state": {"hp": 40},
            "assertion": {"type": "state_equals", "field": "hp", "value": 100},
            "evidence": {"hp_not_reset": "sdk_state"},
            "tags": ["auto_detected"],
            "bug_dir": None,
            "discovered_at": 1.0,
            "test_id": "t-d5",
            "triage": {"severity": "major", "confidence": 0.9},
            "evidence_chain": {"ok": True},
            "provenance": prov,
            "fix_plan": plan,
            "origin_step": 2,
            "detection_channels": prov["channels"],
            "plan_confidence": plan["confidence"],
        }
        with api._LOCK:
            api._BUGS["BUG-API-D5"] = entry
            api._BUG_ORDER.append("BUG-API-D5")

    def test_bug_detail_exposes_intel(self, client):
        self._register()
        r = client.get("/api/v1/bugs/BUG-API-D5")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["provenance"]["origin_step"] == 2
        assert data["fix_plan"]["anomaly_type"] == "hp_not_reset"

    def test_bug_provenance_endpoint(self, client):
        self._register()
        r = client.get("/api/v1/bugs/BUG-API-D5/provenance")
        assert r.status_code == 200
        assert r.json()["provenance"]["origin_action"]["target"] == "#restart"

    def test_bug_fix_plan_endpoint(self, client):
        self._register()
        r = client.get("/api/v1/bugs/BUG-API-D5/fix-plan")
        assert r.status_code == 200
        assert r.json()["fix_plan"]["verification_steps"]

    def test_unknown_bug_returns_404(self, client):
        assert client.get("/api/v1/bugs/NOPE/provenance").status_code == 404
        assert client.get("/api/v1/bugs/NOPE/fix-plan").status_code == 404

    def test_report_endpoint_returns_html(self, client):
        r = client.get("/api/v1/report")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "缺陷总数" in r.text

    def test_report_unknown_test_returns_404(self, client):
        assert client.get("/api/v1/report", params={"test_id": "nope"}).status_code == 404
