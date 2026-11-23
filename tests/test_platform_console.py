"""控制台 API 与编排器测试 —— 用替身执行器验证并发/启停/权限/事件。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from playranger.api.main import app
from playranger.platform import db
from playranger.platform.orchestrator import get_orchestrator, reset_orchestrator

PASSWORD = "password123"


class FakeRunner:
    """不启动浏览器，按任务目标模拟步进与取消。"""

    def __init__(self, steps: int = 3, delay: float = 0.05) -> None:
        self.steps = steps
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, task: dict, hooks) -> dict:
        task_id = task["id"]
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(task_id)
        slow = "slow" in (task.get("goal") or "")
        total = 40 if slow else self.steps
        done = 0
        out_dir = Path(task["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        steps_file = out_dir / "steps.jsonl"
        steps_file.write_text("", encoding="utf-8")
        try:
            for step in range(1, total + 1):
                if hooks.should_stop():
                    return {
                        "task_id": task_id,
                        "status": "cancelled",
                        "total_steps": done,
                        "bugs_found": [],
                    }
                step_payload = {
                    "step": step,
                    "action": {"type": "click", "target": "button"},
                    "execution_ok": True,
                    "execution_error": None,
                    "correction_level": "none",
                    "verification": {"success": True, "reason": "ok"},
                    "timestamp": time.time(),
                }
                with open(steps_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(step_payload, ensure_ascii=False) + "\n")
                hooks.on_step(step_payload)
                done = step
                time.sleep(self.delay)
            if "with-bug" in (task.get("goal") or ""):
                bug_dir = Path(task["output_dir"]) / "bugs" / "BUG-001"
                bug_dir.mkdir(parents=True, exist_ok=True)
                (bug_dir / "defect.json").write_text(json.dumps({
                    "bug_id": "BUG-001",
                    "title": "重开后 HP 未重置",
                    "anomaly_type": "hp_not_reset",
                    "severity": "major",
                    "conclusion": "auto_confirmed",
                }), encoding="utf-8")
                (bug_dir / "fix_plan.json").write_text(json.dumps({
                    "patch": "--- a/game.js\n+++ b/game.js\n@@ -1 +1 @@\n-let hp = 0;\n+let hp = 100;\n",
                    "confidence": 0.6,
                }), encoding="utf-8")
                return {
                    "task_id": task_id,
                    "status": "completed",
                    "total_steps": done,
                    "bugs_found": [{"bug_id": "BUG-001", "bug_dir": str(bug_dir)}],
                }
            return {
                "task_id": task_id,
                "status": "completed",
                "total_steps": done,
                "bugs_found": [],
            }
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYRANGER_DB_PATH", str(tmp_path / "platform.db"))
    monkeypatch.setenv("PLAYRANGER_WORKSPACE_ROOTS", str(tmp_path))
    monkeypatch.setenv("PLAYRANGER_TASKS_DIR", str(tmp_path / "tasks"))
    db.reset_initialized_cache()
    reset_orchestrator()
    runner = FakeRunner()
    get_orchestrator().set_runner(runner)
    with TestClient(app) as client:
        yield client, runner, tmp_path
    reset_orchestrator()
    db.reset_initialized_cache()


def _register(client: TestClient, username: str = "owner") -> dict:
    resp = client.post(
        "/api/console/auth/register",
        json={"username": username, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(client: TestClient, token: str, tmp_path: Path, goal: str = "找 bug") -> dict:
    game = tmp_path / "game.html"
    if not game.exists():
        game.write_text("<html><body>demo</body></html>", encoding="utf-8")
    resp = client.post(
        "/api/console/tasks",
        headers=_headers(token),
        json={"game_url": str(game), "goal": goal, "max_steps": 10},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["task"]


def _wait(client: TestClient, token: str, task_id: str, statuses: set[str], timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        resp = client.get(f"/api/console/tasks/{task_id}", headers=_headers(token))
        assert resp.status_code == 200, resp.text
        last = resp.json()["task"]
        if last["status"] in statuses:
            return last
        time.sleep(0.1)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内进入 {statuses}，最后状态 {last}")


class TestAuthApi:
    def test_first_admin_login_me_and_logout(self, env):
        client, _, _ = env
        first = _register(client, "owner")
        second = _register(client, "dev1")
        assert first["user"]["role"] == "admin"
        assert second["user"]["role"] == "member"

        me = client.get("/api/console/auth/me", headers=_headers(first["token"]))
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "owner"

        assert client.get("/api/console/tasks").status_code == 401
        bad = client.post(
            "/api/console/auth/login",
            json={"username": "owner", "password": "wrong-password"},
        )
        assert bad.status_code == 401

        login = client.post(
            "/api/console/auth/login",
            json={"username": "owner", "password": PASSWORD},
        )
        assert login.status_code == 200
        token = login.json()["token"]
        assert client.post("/api/console/auth/logout", headers=_headers(token)).status_code == 200
        assert client.get("/api/console/auth/me", headers=_headers(token)).status_code == 401

    def test_admin_only_endpoints(self, env):
        client, _, _ = env
        admin = _register(client, "owner")
        member = _register(client, "dev1")
        assert client.get(
            "/api/console/admin/users", headers=_headers(admin["token"])
        ).status_code == 200
        assert client.get(
            "/api/console/admin/users", headers=_headers(member["token"])
        ).status_code == 403


class TestTaskFlow:
    def test_run_task_steps_bugs_events(self, env):
        client, runner, tmp_path = env
        user = _register(client)
        task = _create(client, user["token"], tmp_path, goal="with-bug 找异常")
        done = _wait(client, user["token"], task["id"], {"completed"})
        assert done["total_steps"] == 3
        assert done["bugs_found"] == 1
        assert done["result"]["total_steps"] == 3

        steps = client.get(
            f"/api/console/tasks/{task['id']}/steps", headers=_headers(user["token"])
        ).json()
        assert steps["total"] == 3
        assert steps["steps"][0]["action"]["type"] == "click"

        bugs = client.get(
            f"/api/console/tasks/{task['id']}/bugs", headers=_headers(user["token"])
        ).json()
        assert bugs["total"] == 1
        assert bugs["bugs"][0]["severity"] == "major"
        assert "patch" in bugs["bugs"][0]["fix_plan"]

        events = client.get("/api/console/events?since=0", headers=_headers(user["token"]))
        assert events.status_code == 200
        payload = events.json()
        assert payload["last_id"] > 0
        assert any("完成" in item["message"] for item in payload["events"])

    def test_parallel_limit_follows_scale(self, env):
        client, runner, tmp_path = env
        user = _register(client)
        scale = client.post(
            "/api/console/scale", headers=_headers(user["token"]), json={"workers": 2}
        )
        assert scale.status_code == 200
        assert scale.json()["scheduler"]["target_workers"] == 2

        tasks = [_create(client, user["token"], tmp_path, goal=f"task-{i}") for i in range(4)]
        for task in tasks:
            _wait(client, user["token"], task["id"], {"completed"})
        assert runner.max_active == 2

    def test_pause_resume_and_stop(self, env):
        client, runner, tmp_path = env
        user = _register(client)
        client.post("/api/console/scale", headers=_headers(user["token"]), json={"workers": 1})
        client.post("/api/console/pause", headers=_headers(user["token"]))

        paused_task = _create(client, user["token"], tmp_path, goal="paused-task")
        time.sleep(0.6)
        still = client.get(
            f"/api/console/tasks/{paused_task['id']}", headers=_headers(user["token"])
        ).json()["task"]
        assert still["status"] == "queued"

        client.post("/api/console/resume", headers=_headers(user["token"]))
        _wait(client, user["token"], paused_task["id"], {"completed"})

        slow = _create(client, user["token"], tmp_path, goal="slow-task")
        _wait(client, user["token"], slow["id"], {"running"})
        stop = client.post(
            f"/api/console/tasks/{slow['id']}/stop", headers=_headers(user["token"])
        )
        assert stop.status_code == 200
        cancelled = _wait(client, user["token"], slow["id"], {"cancelled"})
        assert cancelled["status"] == "cancelled"

    def test_retry_creates_new_task(self, env):
        client, runner, tmp_path = env
        user = _register(client)
        task = _create(client, user["token"], tmp_path)
        _wait(client, user["token"], task["id"], {"completed"})
        retry = client.post(
            f"/api/console/tasks/{task['id']}/retry", headers=_headers(user["token"])
        )
        assert retry.status_code == 200
        new_task = retry.json()["task"]
        assert new_task["id"] != task["id"]
        _wait(client, user["token"], new_task["id"], {"completed"})

    def test_member_cannot_read_others_task(self, env):
        client, runner, tmp_path = env
        admin = _register(client, "owner")
        member = _register(client, "dev1")
        task = _create(client, admin["token"], tmp_path)
        _wait(client, admin["token"], task["id"], {"completed"})

        denied = client.get(
            f"/api/console/tasks/{task['id']}", headers=_headers(member["token"])
        )
        assert denied.status_code == 403
        allowed = client.get(
            f"/api/console/tasks/{task['id']}", headers=_headers(admin["token"])
        )
        assert allowed.status_code == 200


class TestSystemAndFixApi:
    def test_system_snapshot(self, env):
        client, _, _ = env
        user = _register(client)
        resp = client.get("/api/console/system", headers=_headers(user["token"]))
        assert resp.status_code == 200
        data = resp.json()
        assert data["recommended"] >= 1
        assert data["scheduler"]["target_workers"] >= 1
        assert "db_path" in data

    def test_fix_validate_endpoint(self, env):
        client, _, tmp_path = env
        user = _register(client)
        src = tmp_path / "src"
        src.mkdir()
        (src / "game.js").write_text("let hp = 100;\n", encoding="utf-8")
        patch = (
            "diff --git a/game.js b/game.js\n"
            "--- a/game.js\n+++ b/game.js\n"
            "@@ -1 +1 @@\n-let hp = 100;\n+let hp = 120;\n"
        )
        resp = client.post(
            "/api/console/fixes/validate",
            headers=_headers(user["token"]),
            json={"root": str(src), "patch": patch},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
