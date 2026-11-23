"""平台基础件测试：账号、持久化、内存并发公式、CDP 兜底、修复沙箱。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from playranger.core.engines import perf
from playranger.platform import auth, db, fixes, system
from playranger.platform.orchestrator import reset_orchestrator


@pytest.fixture()
def platform_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYRANGER_DB_PATH", str(tmp_path / "platform.db"))
    monkeypatch.setenv("PLAYRANGER_WORKSPACE_ROOTS", str(tmp_path))
    monkeypatch.setenv("PLAYRANGER_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    db.reset_initialized_cache()
    reset_orchestrator()
    yield tmp_path
    reset_orchestrator()
    db.reset_initialized_cache()


class TestAuth:
    def test_first_user_is_admin_second_is_member(self, platform_env):
        admin, token1 = auth.register("owner", "password123")
        member, token2 = auth.register("dev1", "password123")
        assert admin["role"] == "admin"
        assert member["role"] == "member"
        assert token1 != token2

        assert auth.authenticate(token1)["role"] == "admin"
        assert auth.authenticate(token2)["role"] == "member"
        assert auth.authenticate("bad-token") is None

    def test_password_is_hashed_and_verified(self, platform_env):
        user, _ = auth.register("owner", "password123")
        raw = db.get_user(int(user["id"]))
        assert raw["password_hash"] != "password123"
        assert auth.verify_password("password123", raw["salt"], raw["password_hash"])
        assert not auth.verify_password("wrong-password", raw["salt"], raw["password_hash"])

    def test_duplicate_and_weak_credentials_rejected(self, platform_env):
        auth.register("owner", "password123")
        with pytest.raises(auth.AuthError) as dup:
            auth.register("owner", "password456")
        assert dup.value.status == 409
        with pytest.raises(auth.AuthError):
            auth.register("ab", "password123")
        with pytest.raises(auth.AuthError):
            auth.register("user2", "short")
        with pytest.raises(auth.AuthError) as wrong:
            auth.login("owner", "not-the-password")
        assert wrong.value.status == 401

    def test_logout_invalidates_token(self, platform_env):
        _, token = auth.register("owner", "password123")
        assert auth.authenticate(token) is not None
        auth.logout(token)
        assert auth.authenticate(token) is None


class TestSystem:
    def test_recommendation_matches_formula(self):
        assert system.compute_recommendation(32, 16, per_worker_mb_value=1536, reserve_gb_value=2.0, hard_max=8) == 8
        assert system.compute_recommendation(8, 16, per_worker_mb_value=1536, reserve_gb_value=2.0, hard_max=8) == 4
        assert system.compute_recommendation(3, 16, per_worker_mb_value=1536, reserve_gb_value=2.0, hard_max=8) == 1
        assert system.compute_recommendation(1, 2, per_worker_mb_value=1536, reserve_gb_value=2.0, hard_max=8) == 1

    def test_cpu_is_a_cap(self):
        assert system.compute_recommendation(64, 3, per_worker_mb_value=1024, reserve_gb_value=1.0, hard_max=8) == 3

    def test_snapshot_has_recomputable_fields(self):
        snap = system.snapshot()
        assert snap["recommended"] >= 1
        assert snap["recommended"] <= snap["hard_max"]
        assert snap["per_worker_mb"] >= 512
        assert "memory_source" in snap


class TestCdpFallback:
    def _metrics(self, task, script, layout, heap):
        return {
            "TaskDuration": task,
            "ScriptDuration": script,
            "LayoutDuration": layout,
            "JSHeapUsedSize": heap,
        }

    def test_busy_ratio_and_no_fps(self):
        sample = perf.summarize_cdp(
            self._metrics(1.0, 0.5, 0.1, 100 * 1024 * 1024),
            self._metrics(1.8, 1.4, 0.2, 120 * 1024 * 1024),
            1.0,
        )
        assert sample.mode == "cdp_fallback"
        assert sample.fps_unavailable is True
        assert sample.insufficient_data is False
        assert sample.busy_ratio == 0.8
        assert sample.avg_fps == 0.0
        assert sample.violations == []

    def test_busy_overload_and_heap_violation(self):
        sample = perf.summarize_cdp(
            self._metrics(1.0, 0.5, 0.1, 100 * 1024 * 1024),
            self._metrics(1.95, 1.9, 0.1, 900 * 1024 * 1024),
            1.0,
        )
        assert any("主线程忙碌" in v for v in sample.violations)
        assert any("JS 堆" in v for v in sample.violations)
        assert sample.passed is False

    def test_missing_metrics_or_short_window_is_insufficient(self):
        assert perf.summarize_cdp({}, {}, 0.0).insufficient_data is True
        assert perf.summarize_cdp({"TaskDuration": 1}, {"TaskDuration": 2}, 0.05).insufficient_data is True
        fields = perf.build_bug_fields(
            perf.summarize_cdp(
                self._metrics(1.0, 0.5, 0.1, 1),
                self._metrics(2.0, 1.9, 0.1, 1),
                1.0,
            ).to_dict()
        )
        assert "CDP" in fields["description"]
        assert "FPS 不可用" in fields["description"]


PATCH = """diff --git a/game.js b/game.js
--- a/game.js
+++ b/game.js
@@ -1,2 +1,2 @@
 let score = 0;
-let hp = 100;
+let hp = 120;
"""


class TestFixSandbox:
    def _source(self, tmp_path: Path) -> Path:
        src = tmp_path / "src"
        src.mkdir()
        (src / "game.js").write_text("let score = 0;\nlet hp = 100;\n", encoding="utf-8")
        return src

    def test_validate_does_not_modify_source(self, platform_env):
        src = self._source(platform_env)
        result = fixes.validate_patch(str(src), PATCH)
        assert result["ok"] is True
        assert result["files"] == ["game.js"]
        assert (src / "game.js").read_text(encoding="utf-8").endswith("let hp = 100;\n")

    def test_invalid_patch_is_rejected(self, platform_env):
        src = self._source(platform_env)
        bad = PATCH.replace("-let hp = 100;", "-let hp = 999;")
        result = fixes.validate_patch(str(src), bad)
        assert result["ok"] is False

    def test_verify_applies_in_sandbox_only(self, platform_env):
        src = self._source(platform_env)
        result = fixes.verify_patch(str(src), PATCH)
        assert result["ok"] is True
        assert result["applied"] is True
        sandbox_file = Path(result["sandbox_dir"]) / "game.js"
        assert "let hp = 120;" in sandbox_file.read_text(encoding="utf-8")
        assert "let hp = 100;" in (src / "game.js").read_text(encoding="utf-8")

    def test_path_outside_whitelist_is_rejected(self, platform_env):
        outside = platform_env.parent / "outside_root"
        outside.mkdir(exist_ok=True)
        with pytest.raises(fixes.SandboxError):
            fixes.resolve_root(str(outside))

    def test_test_command_runs_in_sandbox(self, platform_env):
        src = self._source(platform_env)
        command = (
            f'"{sys.executable}" -c "import pathlib; '
            "assert 'hp = 120' in pathlib.Path('game.js').read_text()\""
        )
        result = fixes.verify_patch(str(src), PATCH, test_command=command, timeout_s=30)
        assert result["applied"] is True
        assert result["test"]["returncode"] == 0


class TestTaskStore:
    def test_tasks_and_events_persist(self, platform_env):
        user, _ = auth.register("owner", "password123")
        task = db.create_task({
            "id": "t-001",
            "user_id": user["id"],
            "game_url": "file:///demo/index.html",
            "goal": "找 bug",
            "output_dir": str(platform_env / "tasks" / "t-001"),
        })
        assert task["status"] == "queued"
        db.add_event("t-001", "info", "入队")
        claimed = db.claim_next_queued()
        assert claimed["id"] == "t-001"
        assert claimed["status"] == "running"
        db.update_task("t-001", status="completed", bugs_found=2,
                       result_json=json.dumps({"total_steps": 5}))
        row = db.get_task("t-001")
        assert row["status"] == "completed"
        public = db.task_to_public(row, include_result=True)
        assert public["result"]["total_steps"] == 5
        events = db.list_events(since=0, user_id=user["id"], is_admin=False)
        assert events and events[-1]["message"] == "入队"
