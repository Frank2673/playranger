"""P1-3 回放确定性测试。

覆盖：稳定等待脚本、种子注入、步骤耗时、三态复现、缺陷包回写、开始时间记录。
全部为单元级测试，不依赖浏览器。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from playranger.core.agent.recorder import BugRecorder
from playranger.core.agent.replayer import (
    ReplayResult,
    ReproductionReport,
    update_defect_reproduction,
)
from playranger.core.engines.play_engine import PlayEngine, StepRecord
from playranger.core.engines.settle import (
    SETTLE_SCRIPT,
    SettleResult,
    build_seed_script,
    settle,
)


# ─── 种子注入 ───────────────────────────────────────────────


class TestSeedScript:
    def test_build_seed_script(self) -> None:
        assert build_seed_script(42) == "window.__PLAYRANGER_SEED__ = 42;"

    def test_reject_bool(self) -> None:
        with pytest.raises(ValueError):
            build_seed_script(True)

    def test_reject_non_int(self) -> None:
        with pytest.raises(ValueError):
            build_seed_script("42")


# ─── 稳定等待 ───────────────────────────────────────────────


class TestSettle:
    def test_script_conditions(self) -> None:
        assert "requestAnimationFrame" in SETTLE_SCRIPT
        assert "MutationObserver" in SETTLE_SCRIPT
        assert "getAnimations" in SETTLE_SCRIPT

    def test_result_to_dict(self) -> None:
        result = SettleResult("stable", 2, 0, 15)
        assert result.to_dict() == {
            "status": "stable",
            "frames": 2,
            "animations": 0,
            "elapsed_ms": 15,
        }

    def test_settle_stable(self) -> None:
        class FakePage:
            def wait_for_load_state(self, *args, **kwargs):
                return None

            def evaluate(self, script, arg):
                assert script is SETTLE_SCRIPT
                assert arg == [1200, 2]
                return {"status": "stable", "frames": 2, "animations": 0, "elapsed_ms": 20}

            def wait_for_timeout(self, ms):
                raise AssertionError("稳定时不应调用兜底等待")

        result = settle(FakePage(), 1200)
        assert result.status == "stable"
        assert result.frames == 2
        assert result.elapsed_ms == 20

    def test_settle_error_falls_back(self) -> None:
        class BadPage:
            waited: int = 0

            def wait_for_load_state(self, *args, **kwargs):
                raise RuntimeError("networkidle unsupported")

            def evaluate(self, script, arg):
                raise RuntimeError("evaluate failed")

            def wait_for_timeout(self, ms):
                self.waited = ms

        page = BadPage()
        result = settle(page, 500)
        assert result.status == "error"
        assert page.waited == 80


# ─── 引擎记录 ───────────────────────────────────────────────


class TestPlayEngineDeterminism:
    def test_defaults(self) -> None:
        engine = PlayEngine(url="about:blank", sdk_mode=False)
        assert engine.seed is None
        assert engine.seed_mode == "none"
        assert engine.settle is True

    def test_seed_mode_global(self) -> None:
        engine = PlayEngine(url="about:blank", sdk_mode=False, seed=7)
        assert engine.seed_mode == "global"

    def test_trace_to_list_contains_duration(self) -> None:
        engine = PlayEngine(url="about:blank", sdk_mode=False)
        engine._trace.append(
            StepRecord(
                logic_step=1,
                action_type="wait",
                action_target=None,
                action_params={"ms": 10},
                state_before={},
                state_after={},
                ok=True,
                duration_ms=12.5,
                settle={"status": "stable", "frames": 2},
            )
        )
        data = engine.trace_to_list()
        assert data[0]["duration_ms"] == 12.5
        assert data[0]["settle"]["status"] == "stable"


# ─── 三态复现 ───────────────────────────────────────────────


def _make_report(rate: float, *, total: int = 3, seed_mode: str = "sdk") -> ReproductionReport:
    success = int(round(rate * total))
    results = [
        ReplayResult(
            run_id=i + 1,
            success=i < success,
            final_state={},
            steps_executed=4,
            duration_ms=100.0 + i * 10,
            seed_mode=seed_mode,
        )
        for i in range(total)
    ]
    return ReproductionReport(
        bug_id="BUG-001",
        total_runs=total,
        success_runs=success,
        reproduction_rate=success / total if total else 0.0,
        results=results,
        seed_mode=seed_mode,
    )


class TestReproductionState:
    def test_reproduced(self) -> None:
        assert _make_report(1.0).reproduction_state == "reproduced"

    def test_partial(self) -> None:
        # 3 次成功 1 次 → 33%，低于 80% 阈值
        assert _make_report(1 / 3).reproduction_state == "partial"

    def test_not_reproduced(self) -> None:
        assert _make_report(0.0).reproduction_state == "not_reproduced"

    def test_empty(self) -> None:
        report = ReproductionReport(bug_id="X", total_runs=0, success_runs=0, reproduction_rate=0.0)
        assert report.reproduction_state == "not_reproduced"

    def test_to_dict(self) -> None:
        data = _make_report(1.0).to_dict()
        assert data["state"] == "reproduced"
        assert data["seed_mode"] == "sdk"
        assert data["avg_duration_ms"] == 110.0
        assert data["reproduction_rate"] == 1.0


class TestUpdateDefectReproduction:
    def test_write_back(self, tmp_path: Path) -> None:
        bug_dir = tmp_path / "BUG-001"
        bug_dir.mkdir()
        defect = {"bug_id": "BUG-001", "reproduction_rate": 0.0, "reproduction_attempts": 0}
        (bug_dir / "defect.json").write_text(
            json.dumps(defect, ensure_ascii=False), encoding="utf-8"
        )

        report = _make_report(2 / 3)
        reproduction = update_defect_reproduction(bug_dir, report)

        assert reproduction["state"] == "partial"
        assert reproduction["runs"] == 3
        assert reproduction["success_runs"] == 2
        assert reproduction["seed_mode"] == "sdk"
        assert reproduction["avg_duration_ms"] == 110.0
        assert reproduction["updated_at"]

        saved = json.loads((bug_dir / "defect.json").read_text(encoding="utf-8"))
        assert saved["reproduction_rate"] == round(2 / 3, 4)
        assert saved["reproduction_attempts"] == 3
        assert saved["reproduction"]["state"] == "partial"

    def test_missing_defect(self, tmp_path: Path) -> None:
        assert update_defect_reproduction(tmp_path / "nope", _make_report(1.0)) == {}


# ─── 缺陷包开始时间 ─────────────────────────────────────────


class TestStartedAt:
    def test_capture_records_started_at(self, tmp_path: Path) -> None:
        recorder = BugRecorder(output_dir=str(tmp_path / "bugs"))
        bug_dir = recorder.capture(
            title="重新开始后 HP 未重置",
            anomaly_type="hp_not_reset",
            severity="major",
            initial_state={"hp": 100},
            final_state={"hp": 40},
            action_trace=[{"action_type": "click", "action_target": "#restart"}],
            started_at="2026-11-16T10:00:00Z",
        )

        defect = json.loads(Path(bug_dir, "defect.json").read_text(encoding="utf-8"))
        assert defect["started_at"] == "2026-11-16T10:00:00Z"
        assert defect["seed"] == 0
        assert defect["action_count"] == 1
