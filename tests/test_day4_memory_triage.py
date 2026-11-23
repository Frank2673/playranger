"""Day 4 单元测试：Agent 记忆系统（memory）与缺陷分级（triage）。

纯逻辑测试，不启动浏览器、不调用 LLM、不依赖网络。
"""

from __future__ import annotations

import json

import pytest


# ═══════════════════════════════════════════════════════════════
# 一、缺陷分级 triage
# ═══════════════════════════════════════════════════════════════


class TestTriageBaseline:
    """规则表基线定级。"""

    def test_blocker_types(self):
        from playranger.core.agent.triage import triage

        for t in ("crash_page", "crash"):
            assert triage(t).severity == "blocker"

    def test_critical_types(self):
        from playranger.core.agent.triage import triage

        for t in ("black_screen", "white_screen", "frozen_screen", "error_popup", "interaction_blocked"):
            assert triage(t).severity == "critical", t

    def test_major_types(self):
        from playranger.core.agent.triage import triage

        for t in ("hp_not_reset", "state_inconsistency", "score_mismatch", "ui_display_error", "logic_error"):
            assert triage(t).severity == "major", t

    def test_minor_types(self):
        from playranger.core.agent.triage import triage

        for t in ("text_overflow", "ui_broken", "layout_break", "visual_glitch", "performance", "strategy_stuck"):
            assert triage(t).severity == "minor", t

    def test_unknown_is_info(self):
        from playranger.core.agent.triage import triage

        assert triage("unknown").severity == "info"

    def test_unregistered_type_falls_back_to_minor(self):
        from playranger.core.agent.triage import triage

        r = triage("some_new_type")
        assert r.severity == "minor"
        assert "未登记" in r.impact

    def test_impact_and_hint_non_empty(self):
        from playranger.core.agent.triage import triage

        r = triage("hp_not_reset")
        assert r.impact
        assert r.action_hint

    def test_reasons_contains_baseline(self):
        from playranger.core.agent.triage import triage

        r = triage("text_overflow")
        assert any("基线定级" in x for x in r.reasons)


class TestTriageEvidence:
    """证据加权对置信度的影响。"""

    def test_two_sources_boost_confidence(self):
        from playranger.core.agent.triage import triage

        r = triage(
            "hp_not_reset",
            evidence={"a": "sdk_state", "b": "action_trace"},
        )
        # 0.4 基础 + 0.2 多源 + 0.15 sdk_state + 0.1 action_trace
        assert r.confidence == pytest.approx(0.85)

    def test_single_source_no_multi_boost(self):
        from playranger.core.agent.triage import triage

        r = triage("hp_not_reset", evidence={"a": "sdk_state"})
        assert r.confidence == pytest.approx(0.55)

    def test_vision_confidence_weighted(self):
        from playranger.core.agent.triage import triage

        r = triage("ui_display_error", evidence={"a": "vision_vlm"}, vision_confidence=1.0)
        assert r.confidence == pytest.approx(0.65)

    def test_vision_zero_confidence_no_boost(self):
        from playranger.core.agent.triage import triage

        r = triage("ui_display_error", evidence={"a": "vision_vlm"}, vision_confidence=0.0)
        assert r.confidence == pytest.approx(0.4)

    def test_duplicate_source_kinds_counted_once(self):
        from playranger.core.agent.triage import triage

        r = triage("hp_not_reset", evidence={"a": "sdk_state", "b": "sdk_state"})
        # 只有一类证据源 → 不触发多源加权
        assert r.confidence == pytest.approx(0.55)


class TestTriageConclusion:
    """复现率与连续失败对结论档位、严重度的影响。"""

    def test_stable_reproduction_auto_confirmed(self):
        from playranger.core.agent.triage import triage

        r = triage("hp_not_reset", evidence={"a": "sdk_state"}, reproduction_rate=0.9)
        assert r.conclusion == "auto_confirmed"
        assert r.confidence > 0.55

    def test_high_rate_without_evidence_degrades_to_warning(self):
        from playranger.core.agent.triage import triage

        r = triage("hp_not_reset", reproduction_rate=0.9)
        assert r.conclusion == "auto_warning"

    def test_partial_reproduction_auto_warning(self):
        from playranger.core.agent.triage import triage

        assert triage("hp_not_reset", reproduction_rate=0.5).conclusion == "auto_warning"

    def test_many_attempts_low_rate_auto_warning(self):
        from playranger.core.agent.triage import triage

        r = triage("hp_not_reset", reproduction_rate=0.1, reproduction_attempts=3)
        assert r.conclusion == "auto_warning"

    def test_no_attempts_human_required(self):
        from playranger.core.agent.triage import triage

        assert triage("hp_not_reset").conclusion == "human_required"

    def test_consecutive_failures_bump_severity(self):
        from playranger.core.agent.triage import triage

        r = triage("strategy_stuck", consecutive_failures=5)
        assert r.severity == "major"      # minor 上调一档
        assert any("连续" in x for x in r.reasons)

    def test_bump_capped_at_blocker(self):
        from playranger.core.agent.triage import triage

        assert triage("crash_page", consecutive_failures=10).severity == "blocker"

    def test_two_failures_no_bump(self):
        from playranger.core.agent.triage import triage

        assert triage("strategy_stuck", consecutive_failures=2).severity == "minor"

    def test_confidence_clamped_to_one(self):
        from playranger.core.agent.triage import triage

        r = triage(
            "strategy_stuck",
            evidence={"a": "sdk_state", "b": "action_trace", "c": "vision_vlm"},
            vision_confidence=1.0,
            reproduction_rate=1.0,
            consecutive_failures=5,
        )
        assert r.confidence <= 1.0

    def test_to_dict_schema(self):
        from playranger.core.agent.triage import triage

        d = triage("hp_not_reset", evidence={"a": "sdk_state"}).to_dict()
        assert set(d) == {"severity", "conclusion", "confidence", "impact", "action_hint", "reasons"}
        assert isinstance(d["reasons"], list)


class TestTriageAggregation:
    """严重度分值与缺陷聚合统计。"""

    def test_severity_score_monotonic(self):
        from playranger.core.agent.triage import SEVERITY_ORDER, severity_score

        scores = [severity_score(s) for s in SEVERITY_ORDER]
        assert scores == sorted(scores)
        assert severity_score("blocker") == 100

    def test_severity_score_unknown_fallback(self):
        from playranger.core.agent.triage import severity_score

        assert severity_score("not_a_level") == 10

    def test_summarize_bugs(self):
        from playranger.core.agent.triage import summarize_bugs

        bugs = [
            {"severity": "major", "conclusion": "auto_confirmed"},
            {"severity": "major", "conclusion": "auto_warning"},
            {"severity": "minor", "conclusion": "human_required"},
        ]
        s = summarize_bugs(bugs)
        assert s["total"] == 3
        assert s["by_severity"]["major"] == 2
        assert s["by_severity"]["minor"] == 1
        assert s["by_conclusion"]["auto_confirmed"] == 1
        assert s["highest_severity"] == "major"

    def test_summarize_bugs_empty(self):
        from playranger.core.agent.triage import summarize_bugs

        s = summarize_bugs([])
        assert s["total"] == 0
        assert s["highest_severity"] == "info"

    def test_summarize_bugs_ignores_unknown_values(self):
        from playranger.core.agent.triage import summarize_bugs

        s = summarize_bugs([{"severity": "nope", "conclusion": "nope"}])
        assert s["total"] == 1
        assert s["by_severity"]["info"] == 0  # 非法值不计数
        assert s["highest_severity"] == "info"


# ═══════════════════════════════════════════════════════════════
# 二、状态签名与相似度
# ═══════════════════════════════════════════════════════════════


class TestSignatureAndSimilarity:
    def test_extract_signature(self):
        from playranger.core.agent.memory import extract_signature

        sig = extract_signature({"scene": "level2", "level": 2, "hp": 40, "score": 120, "extra": "x"})
        assert sig == {"scene": "level2", "level": 2, "hp": 40, "score": 120}

    def test_extract_signature_empty(self):
        from playranger.core.agent.memory import extract_signature

        assert extract_signature({}) == {"scene": "", "level": None, "hp": None, "score": None}

    def test_identical_signature_is_one(self):
        from playranger.core.agent.memory import similarity

        a = {"scene": "level1", "level": 1, "hp": 80, "score": 100}
        assert similarity(a, dict(a)) == pytest.approx(1.0)

    def test_scene_only(self):
        from playranger.core.agent.memory import similarity

        assert similarity({"scene": "a"}, {"scene": "a"}) == pytest.approx(0.45)

    def test_level_only(self):
        from playranger.core.agent.memory import similarity

        assert similarity({"level": 1}, {"level": 1}) == pytest.approx(0.25)

    def test_hp_equal_and_near(self):
        from playranger.core.agent.memory import similarity

        assert similarity({"hp": 50}, {"hp": 50}) == pytest.approx(0.15)
        assert similarity({"hp": 50}, {"hp": 65}) == pytest.approx(0.08)
        assert similarity({"hp": 50}, {"hp": 90}) == pytest.approx(0.0)

    def test_score_within_relative_threshold(self):
        from playranger.core.agent.memory import similarity

        # 差 40 ≤ max(50, 200*0.2)=50
        assert similarity({"score": 200}, {"score": 240}) == pytest.approx(0.15)
        assert similarity({"score": 200}, {"score": 400}) == pytest.approx(0.0)

    def test_none_values_ignored(self):
        from playranger.core.agent.memory import similarity

        assert similarity({}, {}) == pytest.approx(0.0)

    def test_non_numeric_tolerated(self):
        from playranger.core.agent.memory import similarity

        assert similarity({"hp": "abc"}, {"hp": 10}) == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════
# 三、记忆写入与合并
# ═══════════════════════════════════════════════════════════════


STATE_L2 = {"scene": "level2", "level": 2, "hp": 40, "score": 120}


class TestMemoryRemember:
    def _mem(self, tmp_path):
        from playranger.core.agent.memory import AgentMemory

        return AgentMemory(tmp_path / "mem.json", auto_load=False)

    def test_remember_bug_creates_entry(self, tmp_path):
        mem = self._mem(tmp_path)
        bug = {
            "bug_id": "BUG-001",
            "title": "HP 未重置",
            "anomaly_type": "hp_not_reset",
            "severity": "major",
            "description": "重开关卡后血量没回满",
        }
        entry = mem.remember_bug(bug, state=STATE_L2, action_sequence=[{"action_type": "click"}])

        assert entry.kind == "bug"
        assert entry.count == 1
        assert entry.bug_ids == ["BUG-001"]
        assert entry.signature["scene"] == "level2"
        assert entry.hint == "重开关卡后血量没回满"
        assert len(mem.entries) == 1

    def test_remember_bug_merges_same_signature(self, tmp_path):
        mem = self._mem(tmp_path)
        bug = {"bug_id": "BUG-001", "title": "HP 未重置", "anomaly_type": "hp_not_reset", "severity": "major"}

        mem.remember_bug(bug, state=STATE_L2)
        entry = mem.remember_bug({**bug, "bug_id": "BUG-002"}, state=dict(STATE_L2))

        assert len(mem.entries) == 1
        assert entry.count == 2
        assert entry.bug_ids == ["BUG-001", "BUG-002"]

    def test_remember_bug_dedup_bug_id(self, tmp_path):
        mem = self._mem(tmp_path)
        bug = {"bug_id": "BUG-001", "anomaly_type": "hp_not_reset"}
        mem.remember_bug(bug, state=STATE_L2)
        entry = mem.remember_bug(bug, state=STATE_L2)

        assert entry.count == 2
        assert entry.bug_ids == ["BUG-001"]

    def test_remember_bug_different_anomaly_not_merged(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.remember_bug({"anomaly_type": "hp_not_reset"}, state=STATE_L2)
        mem.remember_bug({"anomaly_type": "score_mismatch"}, state=STATE_L2)

        assert len(mem.entries) == 2

    def test_remember_bug_different_scene_not_merged(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.remember_bug({"anomaly_type": "hp_not_reset"}, state=STATE_L2)
        mem.remember_bug({"anomaly_type": "hp_not_reset"}, state={**STATE_L2, "scene": "level9"})

        assert len(mem.entries) == 2

    def test_remember_failure_merges(self, tmp_path):
        mem = self._mem(tmp_path)
        e1 = mem.remember_failure(STATE_L2, "连续 3 次点击无效")
        e2 = mem.remember_failure(STATE_L2, "连续 3 次点击无效")

        assert e1 is e2 or e1.entry_id == e2.entry_id
        assert len(mem.entries) == 1
        assert e2.count == 2
        assert e2.kind == "failure"

    def test_remember_success(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.remember_success(STATE_L2, action_sequence=[{"action_type": "click"}])
        mem.remember_success(STATE_L2)

        assert len(mem.entries) == 1
        assert mem.entries[0].kind == "success"
        assert mem.entries[0].count == 2
        assert mem.entries[0].severity == "info"

    def test_slim_sequence_keeps_last_ten(self):
        from playranger.core.agent.memory import AgentMemory

        seq = [{"action_type": f"a{i}", "action_params": {"i": i}} for i in range(15)]
        slim = AgentMemory._slim_sequence(seq)

        assert len(slim) == 10
        assert slim[-1] == {"action": "a14", "params": {"i": 14}}
        assert "action_type" not in slim[0]

    def test_slim_sequence_keeps_non_dict(self):
        from playranger.core.agent.memory import AgentMemory

        assert AgentMemory._slim_sequence(["raw", 1]) == ["raw", 1]


# ═══════════════════════════════════════════════════════════════
# 四、记忆召回与上下文生成
# ═══════════════════════════════════════════════════════════════


class TestMemoryRecall:
    def _mem(self, tmp_path):
        from playranger.core.agent.memory import AgentMemory

        return AgentMemory(tmp_path / "mem.json", auto_load=False)

    def test_recall_matches_similar_state(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.remember_bug({"title": "HP 未重置", "anomaly_type": "hp_not_reset", "severity": "major"}, state=STATE_L2)

        hits = mem.recall({**STATE_L2, "hp": 45})
        assert len(hits) == 1
        assert hits[0][0].title == "HP 未重置"
        assert hits[0][1] >= 0.35

    def test_recall_threshold_filters_unrelated(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.remember_bug({"title": "HP 未重置", "anomaly_type": "hp_not_reset"}, state=STATE_L2)

        hits = mem.recall({"scene": "menu", "level": 9, "hp": 100, "score": 9999})
        assert hits == []

    def test_recall_kinds_filter(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.remember_bug({"title": "缺陷", "anomaly_type": "hp_not_reset"}, state=STATE_L2)
        mem.remember_failure(STATE_L2, "卡住了")

        only_failure = mem.recall(STATE_L2, kinds=("failure",))
        assert len(only_failure) == 1
        assert only_failure[0][0].kind == "failure"

    def test_recall_limit(self, tmp_path):
        mem = self._mem(tmp_path)
        for i in range(5):
            mem.remember_bug(
                {"title": f"缺陷{i}", "anomaly_type": f"type{i}"},
                state=STATE_L2,
            )

        assert len(mem.recall(STATE_L2, limit=2)) == 2

    def test_build_context_contains_bug_info(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.remember_bug(
            {"title": "HP 未重置", "anomaly_type": "hp_not_reset", "severity": "major"},
            state=STATE_L2,
            hint="重开关卡前先记录血量",
        )
        ctx = mem.build_context(STATE_L2)

        assert "历史测试经验" in ctx
        assert "HP 未重置" in ctx
        assert "重开关卡前先记录血量" in ctx
        assert "major" in ctx

    def test_build_context_failure_wording(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.remember_failure(STATE_L2, "连续 3 次点击无效")
        ctx = mem.build_context(STATE_L2)

        assert "卡住" in ctx
        assert "更换策略" in ctx

    def test_build_context_success_wording(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.remember_success(STATE_L2, hint="点击开始按钮")
        ctx = mem.build_context(STATE_L2)

        assert "有效推进路径" in ctx
        assert "复用" in ctx

    def test_build_context_empty_when_no_hits(self, tmp_path):
        mem = self._mem(tmp_path)
        assert mem.build_context({"scene": "unknown", "level": 99, "hp": 1, "score": 0}) == ""

    def test_build_context_limit(self, tmp_path):
        mem = self._mem(tmp_path)
        for i in range(5):
            mem.remember_bug({"title": f"缺陷{i}", "anomaly_type": f"t{i}"}, state=STATE_L2)

        ctx = mem.build_context(STATE_L2, limit=2)
        assert len([x for x in ctx.splitlines() if x.startswith("- ")]) == 2


# ═══════════════════════════════════════════════════════════════
# 五、持久化与统计
# ═══════════════════════════════════════════════════════════════


class TestMemoryPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        from playranger.core.agent.memory import AgentMemory

        path = tmp_path / "sub" / "mem.json"
        mem = AgentMemory(path, auto_load=False)
        mem.remember_bug({"bug_id": "B1", "title": "HP 未重置", "anomaly_type": "hp_not_reset"}, state=STATE_L2)
        mem.remember_failure(STATE_L2, "点击无效")
        saved = mem.save()

        assert saved == str(path)
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert len(payload["entries"]) == 2

        reloaded = AgentMemory(path)
        assert len(reloaded.entries) == 2
        assert reloaded.entries[0].title == "HP 未重置"
        assert reloaded.stats()["total_entries"] == 2

    def test_load_missing_file_is_noop(self, tmp_path):
        from playranger.core.agent.memory import AgentMemory

        mem = AgentMemory(tmp_path / "nope.json")
        assert mem.entries == []

    def test_load_corrupt_file_is_noop(self, tmp_path):
        from playranger.core.agent.memory import AgentMemory

        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        mem = AgentMemory(p)
        assert mem.entries == []

    def test_clear(self, tmp_path):
        from playranger.core.agent.memory import AgentMemory

        mem = AgentMemory(tmp_path / "mem.json", auto_load=False)
        mem.remember_failure(STATE_L2, "x")
        mem.clear()
        assert mem.entries == []

    def test_stats(self, tmp_path):
        from playranger.core.agent.memory import AgentMemory

        mem = AgentMemory(tmp_path / "mem.json", auto_load=False)
        mem.remember_bug({"anomaly_type": "hp_not_reset"}, state=STATE_L2)
        mem.remember_bug({"anomaly_type": "hp_not_reset"}, state=STATE_L2)
        mem.remember_failure(STATE_L2, "卡住")

        s = mem.stats()
        assert s["total_entries"] == 2
        assert s["by_kind"] == {"bug": 1, "failure": 1}
        assert s["total_hits"] == 3

    def test_env_path_override(self, tmp_path, monkeypatch):
        from playranger.core.agent.memory import AgentMemory

        custom = tmp_path / "env_mem.json"
        monkeypatch.setenv("PLAYRANGER_MEMORY_PATH", str(custom))
        mem = AgentMemory()
        mem.save()

        assert custom.exists()
        assert mem.path == custom


class TestMemoryEntryModel:
    def test_from_dict_defaults(self):
        from playranger.core.agent.memory import MemoryEntry

        e = MemoryEntry.from_dict({})
        assert e.entry_id == ""
        assert e.kind == "bug"
        assert e.severity == "info"
        assert e.count == 1
        assert e.bug_ids == []

    def test_to_dict_roundtrip(self):
        from playranger.core.agent.memory import MemoryEntry

        e = MemoryEntry(
            entry_id="bug-1",
            kind="bug",
            anomaly_type="hp_not_reset",
            title="HP 未重置",
            severity="major",
            signature=dict(STATE_L2),
            bug_ids=["B1"],
        )
        assert MemoryEntry.from_dict(e.to_dict()).to_dict() == e.to_dict()
