"""P1-4 / P1-5 / P2-1 / P2-2 / P2-3 / P3-2 / P3-3 / P3-4 深度构建测试。

不启动真实浏览器：引擎稳定性、性能判定、批量调度均用注入/假对象验证；
浏览器级端到端仍由既有 Edge 测试覆盖。
"""

from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from playranger.core.engines import perf
from playranger.core.engines.play_engine import PlayEngine
from playranger.core.agent.triage import triage
from playranger.core.agent.triage_config import (
    TriageConfig,
    load_triage_config,
    parse_config,
)
from playranger.core.agent.memory import (
    AgentMemory,
    decay_factor,
    semantic_scores,
    tokenize,
)
from playranger.core.agent.batch import BatchItem, load_manifest, run_batch
from playranger.core.engines.adapters import (
    AdapterUnavailable,
    AltTesterAdapter,
    EngineAdapter,
    WebAdapter,
    get_adapter,
)


# ─── P1-5 性能采样 ──────────────────────────────────────────────


class TestPerfSampling:
    def test_healthy_frames_pass(self):
        raw = {"frames": [16.7] * 60, "longTasks": [], "heap": [80.0], "started_at": 0, "now": 5000}
        sample = perf.summarize(raw)
        assert sample.insufficient_data is False
        assert sample.passed is True
        assert 55 <= sample.avg_fps <= 62

    def test_low_fps_violation(self):
        raw = {"frames": [40.0] * 20, "longTasks": [], "heap": [], "started_at": 0, "now": 4000}
        sample = perf.summarize(raw)
        assert sample.avg_fps == pytest.approx(25.0, abs=0.5)
        assert any("FPS" in v for v in sample.violations)

    def test_long_task_violation(self):
        raw = {
            "frames": [16.7] * 60,
            "longTasks": [{"start": 100, "duration": 350}],
            "heap": [],
            "started_at": 0,
            "now": 5000,
        }
        sample = perf.summarize(raw)
        assert sample.max_long_task_ms == 350
        assert any("长任务" in v for v in sample.violations)

    def test_insufficient_frames_never_fabricates(self):
        sample = perf.summarize({"frames": [16.7, 16.7]})
        assert sample.insufficient_data is True
        assert sample.avg_fps == 0.0
        assert sample.passed is False

    def test_build_bug_fields_accepts_dict(self):
        sample = perf.summarize({"frames": [50.0] * 30})
        fields = perf.build_bug_fields(sample.to_dict())
        assert fields["anomaly_type"] == "performance"
        assert fields["evidence"] == {"perf_sample": "runtime_perf"}
        assert "FPS" in fields["description"]


# ─── P1-4 引擎稳定性 ────────────────────────────────────────────


class TestEngineStability:
    def test_runtime_stats_defaults(self):
        engine = PlayEngine(
            "http://example.test",
            ci_mode=True,
            retry=2,
            viewport=(800, 600),
            perf=True,
        )
        stats = engine.runtime_stats()
        assert stats["ci_mode"] is True
        assert stats["retry"] == 2
        assert stats["viewport"] == [800, 600]
        assert stats["perf_enabled"] is True
        assert stats["seed_mode"] == "none"

    def test_start_retries_once_then_succeeds(self):
        engine = PlayEngine("http://example.test", headless=True, retry=1)
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")

        engine._start_once = flaky  # type: ignore[method-assign]
        engine.start()
        assert calls["n"] == 2
        assert engine.retry_count == 1
        assert engine._started is True
        assert engine.retry_reasons and "boom" in engine.retry_reasons[0]

    def test_start_raises_after_retries_exhausted(self):
        engine = PlayEngine("http://example.test", headless=True, retry=1)

        def always_fail():
            raise RuntimeError("nope")

        engine._start_once = always_fail  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="已尝试 2 次"):
            engine.start()

    def test_perf_sample_without_enable_is_insufficient(self):
        engine = PlayEngine("http://example.test", perf=False)
        sample = engine.perf_sample()
        assert sample["insufficient_data"] is True


# ─── P2-3 分级配置化 ────────────────────────────────────────────


class TestTriageConfig:
    def test_parse_ignores_invalid_values(self):
        cfg = parse_config({
            "base_severity": {"text_overflow": "major", "bad": "nope"},
            "thresholds": {"auto_confirmed_rate": 1.5, "unknown": 0.3},
            "ignore": ["visual_glitch", 123],
            "evidence_weights": {"sdk_state": 0.4, "unknown": 1},
        })
        assert cfg.base_severity == {"text_overflow": "major"}
        assert cfg.thresholds == {}
        assert cfg.ignore == ["visual_glitch"]
        assert cfg.evidence_weights == {"sdk_state": 0.4}

    def test_severity_override(self):
        result = triage("text_overflow", config={"base_severity": {"text_overflow": "major"}})
        assert result.severity == "major"

    def test_ignore_type(self):
        result = triage("visual_glitch", config={"ignore": ["visual_glitch"]})
        assert result.severity == "info"
        assert result.conclusion == "human_required"
        assert "忽略" in result.reasons[0]

    def test_threshold_override_changes_conclusion(self):
        result = triage(
            "hp_not_reset",
            evidence={"hp": "sdk_state"},
            reproduction_rate=0.5,
            config={"thresholds": {"auto_confirmed_rate": 0.5, "auto_warning_rate": 0.2}},
        )
        assert result.conclusion == "auto_confirmed"

    def test_load_from_file(self, tmp_path):
        path = tmp_path / "playranger.triage.json"
        path.write_text(json.dumps({"base_severity": {"crash": "major"}}), encoding="utf-8")
        cfg = load_triage_config(path)
        assert cfg.severity_for("crash") == "major"
        assert cfg.source.endswith("playranger.triage.json")

    def test_recorder_applies_config(self, tmp_path):
        from playranger.core.agent.recorder import BugRecorder

        recorder = BugRecorder(
            output_dir=str(tmp_path / "bugs"),
            triage_config={"base_severity": {"text_overflow": "major"}},
        )
        pack = recorder.capture(
            title="文字溢出",
            anomaly_type="text_overflow",
            severity="info",
            initial_state={"scene": "menu"},
            final_state={"scene": "menu"},
            action_trace=[],
        )
        defect = json.loads((tmp_path / "bugs" / "BUG-001" / "defect.json").read_text(encoding="utf-8"))
        assert defect["severity"] == "major"
        assert defect["triage"]["severity"] == "major"
        assert os.path.isdir(pack)


# ─── P2-2 API Key + 项目隔离 ────────────────────────────────────


class TestApiSecurity:
    def test_api_key_required_when_configured(self, monkeypatch):
        from fastapi.testclient import TestClient
        from playranger.api import main as api_main

        monkeypatch.setenv("PLAYRANGER_API_KEY", "secret-key")
        client = TestClient(api_main.app)
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/bugs").status_code == 401
        ok = client.get("/api/v1/bugs", headers={"X-API-Key": "secret-key"})
        assert ok.status_code == 200

    def test_project_filter(self, monkeypatch):
        from fastapi.testclient import TestClient
        from playranger.api import main as api_main

        monkeypatch.delenv("PLAYRANGER_API_KEY", raising=False)

        def bug(bug_id: str, project: str) -> dict:
            return {
                "bug_id": bug_id,
                "title": f"title-{bug_id}",
                "anomaly_type": "crash",
                "severity": "major",
                "conclusion": "human_required",
                "discovered_at": time.time(),
                "project_id": project,
            }

        with api_main._LOCK:
            saved_bugs = dict(api_main._BUGS)
            saved_order = list(api_main._BUG_ORDER)
            api_main._BUGS = {"A-1": bug("A-1", "alpha"), "B-1": bug("B-1", "beta")}
            api_main._BUG_ORDER = ["A-1", "B-1"]
        try:
            client = TestClient(api_main.app)
            alpha = client.get("/api/v1/bugs?project_id=alpha").json()
            beta = client.get("/api/v1/bugs?project_id=beta").json()
            default = client.get("/api/v1/bugs").json()
            assert alpha["total"] == 1 and alpha["bugs"][0]["bug_id"] == "A-1"
            assert beta["total"] == 1 and beta["bugs"][0]["bug_id"] == "B-1"
            assert default["total"] == 0
        finally:
            with api_main._LOCK:
                api_main._BUGS = saved_bugs
                api_main._BUG_ORDER = saved_order

    def test_project_normalization_blocks_path_traversal(self):
        from playranger.api.main import _normalize_project

        assert _normalize_project("../../etc") == "etc"
        assert _normalize_project("") == "default"
        assert _normalize_project("my-proj_1") == "my-proj_1"


# ─── P2-1 CI 报告 ───────────────────────────────────────────────


class TestCiReport:
    def _write_bug(self, root, bug_id, severity, anomaly="crash"):
        bug_dir = root / bug_id
        bug_dir.mkdir(parents=True)
        (bug_dir / "defect.json").write_text(json.dumps({
            "bug_id": bug_id,
            "title": f"{anomaly} bug",
            "anomaly_type": anomaly,
            "severity": severity,
            "conclusion": "auto_confirmed",
            "description": "demo",
        }), encoding="utf-8")
        (bug_dir / "code_location.json").write_text(json.dumps({
            "hits": {"score": [{"path": "js/game.js", "line": 42, "column": 3}]}
        }), encoding="utf-8")

    def test_junit_and_sarif_and_gate(self, tmp_path):
        from tools import ci_report

        bugs_dir = tmp_path / "bugs"
        bugs_dir.mkdir()
        self._write_bug(bugs_dir, "BUG-001", "blocker", "crash")
        self._write_bug(bugs_dir, "BUG-002", "minor", "text_overflow")

        bugs = ci_report.load_bugs(bugs_dir)
        assert len(bugs) == 2

        root = ET.fromstring(ci_report.build_junit(bugs, {"blocker"}))
        assert root.attrib["tests"] == "2"
        assert root.attrib["failures"] == "1"

        sarif = ci_report.build_sarif(bugs)
        assert sarif["version"] == "2.1.0"
        levels = {r["level"] for r in sarif["runs"][0]["results"]}
        assert "error" in levels and "note" in levels

        passed, hits = ci_report.evaluate_gate(bugs, {"blocker"}, 0)
        assert passed is False and hits == 1

    def test_cli_exit_codes(self, tmp_path, capsys):
        from tools import ci_report

        bugs_dir = tmp_path / "bugs"
        bugs_dir.mkdir()
        self._write_bug(bugs_dir, "BUG-001", "critical")
        out = tmp_path / "ci"

        code = ci_report.main([
            "--bugs", str(bugs_dir), "--out-dir", str(out), "--fail-on", "critical", "--allow", "0",
        ])
        assert code == 1
        assert (out / "junit.xml").exists()
        assert (out / "playranger.sarif").exists()
        assert (out / "summary.md").exists()

        code = ci_report.main([
            "--bugs", str(bugs_dir), "--out-dir", str(out), "--fail-on", "critical", "--allow", "5",
        ])
        assert code == 0


# ─── P3-2 语义记忆 ──────────────────────────────────────────────


class TestSemanticMemory:
    def test_tokenize_chinese_and_english(self):
        tokens = tokenize("HP 未重置 restart")
        assert "hp" in tokens and "restart" in tokens
        assert "重置" in tokens

    def test_semantic_scores_similar_text_higher(self):
        scores = semantic_scores("重新开始后生命值没有重置", [
            "重新开始后生命值未重置",
            "结算页分数显示错误",
        ])
        assert scores[0] > scores[1]

    def test_decay_half_life(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        recent = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert decay_factor(recent) == pytest.approx(1.0, abs=0.01)
        assert decay_factor(old) == pytest.approx(0.5, abs=0.05)

    def test_semantic_recall_cross_scene(self, tmp_path):
        mem = AgentMemory(tmp_path / "mem.json", auto_load=False)
        mem.remember_bug(
            {"bug_id": "B1", "title": "重新开始后生命值未重置", "anomaly_type": "hp_not_reset"},
            state={"scene": "level1", "level": 1, "hp": 0, "score": 0},
        )
        query = {"scene": "battle", "mode": "重新开始后生命值未重置"}
        assert mem.recall(query) == []
        hits = mem.recall(query, semantic=True)
        assert hits and hits[0][0].bug_ids == ["B1"]

    def test_capacity_pruning(self, tmp_path):
        mem = AgentMemory(tmp_path / "mem.json", auto_load=False, capacity=2)
        for i in range(5):
            mem.remember_bug(
                {"bug_id": f"B{i}", "title": f"缺陷{i}", "anomaly_type": f"type{i}"},
                state={"scene": f"scene{i}", "level": i, "hp": i, "score": i},
            )
        mem.save()
        payload = json.loads((tmp_path / "mem.json").read_text(encoding="utf-8"))
        assert len(payload["entries"]) == 2
        assert payload["capacity"] == 2
        assert mem.stats()["semantic_mode"] == "tfidf_ngram_approximation"


# ─── P3-3 并行调度 ──────────────────────────────────────────────


class TestBatchScheduling:
    def test_manifest_defaults(self, tmp_path):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({
            "defaults": {"goal": "默认目标", "max_steps": 12},
            "items": [{"url": "http://a.test"}, {"url": "http://b.test", "max_steps": 5}],
        }), encoding="utf-8")
        items = load_manifest(path)
        assert items[0].goal == "默认目标" and items[0].max_steps == 12
        assert items[1].max_steps == 5

    def test_parallel_runs_and_dedupes(self):
        def runner(item, output):
            time.sleep(0.05)
            return {
                "status": "completed",
                "bugs_found": [{"bug_id": f"{item.name}-1", "anomaly_type": "crash", "title": "同题"}],
            }

        items = [
            BatchItem(url="http://a.test", name="a"),
            BatchItem(url="http://b.test", name="b"),
        ]
        result = run_batch(items, workers=2, base_output="output/test-batch", runner=runner)
        assert result["total_tasks"] == 2
        assert result["failed_tasks"] == 0
        assert result["total_bugs"] == 2
        assert result["unique_bugs"] == 1
        assert result["duplicates"][0]["count"] == 2

    def test_failure_isolation(self):
        def runner(item, output):
            if item.name == "bad":
                raise RuntimeError("worker down")
            return {"status": "completed", "bugs_found": []}

        items = [BatchItem(url="http://a.test", name="good"), BatchItem(url="http://b.test", name="bad")]
        result = run_batch(items, workers=2, base_output="output/test-batch", runner=runner)
        assert result["failed_tasks"] == 1
        assert any(i.get("error") == "worker down" for i in result["items"])

    def test_worker_limit_clamped(self):
        result = run_batch([], workers=99)
        assert result["workers"] == 99 or result["workers"] <= 8


# ─── P3-4 平台适配 ──────────────────────────────────────────────


class _FakeEngine:
    def read_state(self):
        return SimpleNamespace(raw={"scene": "menu", "hp": 100, "available_actions": ["click"]})

    def get_capabilities(self):
        return {"playable": True, "evidential": True}

    def execute(self, action_type, target=None, params=None):
        return SimpleNamespace(ok=True, error=None, state_after={"scene": "play"}, duration_ms=12.5)

    def sdk_reset(self):
        self.reset_called = True


class TestAdapters:
    def test_web_adapter_implements_protocol(self):
        adapter = WebAdapter(_FakeEngine())
        assert isinstance(adapter, EngineAdapter)
        assert adapter.get_state()["scene"] == "menu"
        assert adapter.get_actions() == ["click"]
        result = adapter.execute("click", target="#start")
        assert result["ok"] is True and result["duration_ms"] == 12.5

    def test_get_adapter_web(self):
        adapter = get_adapter("web", engine=_FakeEngine())
        assert adapter.name == "web"

    def test_unity_adapter_is_honest_stub(self):
        adapter = get_adapter("unity")
        caps = adapter.capabilities()
        assert caps["available"] is False
        assert "AltTester" in caps["reason"]
        with pytest.raises(AdapterUnavailable):
            adapter.get_state()

    def test_mobile_native_alias_now_supported(self):
        """v0.3 起 android/ios 已有真实适配器；旧的 mobile-native 泛称不再接受，
        但报错必须列出可用后端，便于迁移。"""
        with pytest.raises(AdapterUnavailable, match="android"):
            get_adapter("mobile-native")

    def test_unknown_adapter(self):
        with pytest.raises(AdapterUnavailable, match="未知适配器"):
            get_adapter("nintendo")
