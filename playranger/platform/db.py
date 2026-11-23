"""SQLite 持久层 —— 账号、会话、任务、事件。

设计约束：
- 单文件、零运维，适合独立开发者本机使用；
- 每次操作独立连接（WAL），避免 FastAPI 多线程共享连接；
- 任务状态机：queued → running → completed | failed | cancelled | interrupted。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = "output/platform.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('admin', 'member')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    game_url TEXT NOT NULL,
    goal TEXT NOT NULL,
    max_steps INTEGER NOT NULL DEFAULT 50,
    seed INTEGER,
    source_root TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    workers INTEGER NOT NULL DEFAULT 1,
    output_dir TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    current_step INTEGER NOT NULL DEFAULT 0,
    total_steps INTEGER NOT NULL DEFAULT 0,
    bugs_found INTEGER NOT NULL DEFAULT 0,
    progress_json TEXT,
    result_json TEXT,
    error TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
"""

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()

TASK_FIELDS = {
    "project_id",
    "game_url",
    "goal",
    "max_steps",
    "seed",
    "source_root",
    "status",
    "workers",
    "output_dir",
    "created_at",
    "started_at",
    "finished_at",
    "current_step",
    "total_steps",
    "bugs_found",
    "progress_json",
    "result_json",
    "error",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    """数据库路径；测试可通过 PLAYRANGER_DB_PATH 指向临时文件。"""
    raw = os.environ.get("PLAYRANGER_DB_PATH", "").strip() or DEFAULT_DB_PATH
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def reset_initialized_cache() -> None:
    """测试切换数据库路径后清掉 schema 缓存。"""
    with _INIT_LOCK:
        _INITIALIZED.clear()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    conn = sqlite3.connect(str(path), timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # 手动事务，避免隐式 BEGIN 冲突
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        key = str(path)
        if key not in _INITIALIZED:
            with _INIT_LOCK:
                if key not in _INITIALIZED:
                    conn.executescript(_SCHEMA)
                    # 轻量迁移：老库补 source_root 列
                    columns = {
                        row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
                    }
                    if "source_root" not in columns:
                        conn.execute("ALTER TABLE tasks ADD COLUMN source_root TEXT")
                    _INITIALIZED.add(key)
        yield conn
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def init_db() -> Path:
    with connect():
        pass
    return db_path()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ─── 用户 ───────────────────────────────────────────────────────


def create_user(username: str, password_hash: str, salt: str) -> dict:
    """创建用户；表为空时第一个用户自动成为管理员。"""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            role = "admin" if count == 0 else "member"
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, salt, role, created_at)"
                " VALUES(?,?,?,?,?)",
                (username, password_hash, salt, role, now_iso()),
            )
            user_id = int(cur.lastrowid)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {
            "id": user_id,
            "username": username,
            "role": role,
            "created_at": now_iso(),
        }


def get_user_by_username(username: str) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())


def get_user(user_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def list_users() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


# ─── 会话 ───────────────────────────────────────────────────────


def create_session(token_hash: str, user_id: int, expires_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions(token_hash, user_id, created_at, expires_at)"
            " VALUES(?,?,?,?)",
            (token_hash, user_id, now_iso(), expires_at),
        )


def get_session_user(token_hash: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT u.id, u.username, u.role, u.created_at, s.expires_at"
            " FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash=?",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        if data["expires_at"] <= now_iso():
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            return None
        return data


def delete_session(token_hash: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))


def purge_expired_sessions() -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),))
        return int(cur.rowcount or 0)


# ─── 任务 ───────────────────────────────────────────────────────


def create_task(task: dict) -> dict:
    payload = {
        "id": task["id"],
        "user_id": int(task["user_id"]),
        "project_id": task.get("project_id") or "default",
        "game_url": task["game_url"],
        "goal": task.get("goal") or "找到所有可复现的缺陷",
        "max_steps": int(task.get("max_steps") or 50),
        "seed": task.get("seed"),
        "source_root": task.get("source_root"),
        "status": task.get("status") or "queued",
        "workers": int(task.get("workers") or 1),
        "output_dir": task.get("output_dir"),
        "created_at": task.get("created_at") or now_iso(),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "current_step": int(task.get("current_step") or 0),
        "total_steps": int(task.get("total_steps") or 0),
        "bugs_found": int(task.get("bugs_found") or 0),
        "progress_json": task.get("progress_json"),
        "result_json": task.get("result_json"),
        "error": task.get("error"),
    }
    with connect() as conn:
        conn.execute(
            "INSERT INTO tasks(id, user_id, project_id, game_url, goal, max_steps, seed,"
            " source_root, status, workers, output_dir, created_at, started_at, finished_at,"
            " current_step, total_steps, bugs_found, progress_json, result_json, error)"
            " VALUES(:id, :user_id, :project_id, :game_url, :goal, :max_steps, :seed,"
            " :source_root, :status, :workers, :output_dir, :created_at, :started_at, :finished_at,"
            " :current_step, :total_steps, :bugs_found, :progress_json, :result_json, :error)",
            payload,
        )
    return get_task(task["id"]) or payload


def get_task(task_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row(row)


def list_tasks(user_id: int | None = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    with connect() as conn:
        if user_id is None:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id=?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def update_task(task_id: str, **fields: Any) -> None:
    clean = {k: v for k, v in fields.items() if k in TASK_FIELDS}
    if not clean:
        return
    assignments = ", ".join(f"{k}=?" for k in clean)
    with connect() as conn:
        conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id=?",
            (*clean.values(), task_id),
        )


def claim_next_queued() -> dict | None:
    """原子地把最早排队的任务置为 running 并返回（多 worker 不会重复领）。"""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE status='queued'"
                " ORDER BY created_at ASC, rowid ASC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            task_id = row["id"]
            conn.execute(
                "UPDATE tasks SET status='running', started_at=? WHERE id=?",
                (now_iso(), task_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    task = dict(row)
    task["status"] = "running"
    task["started_at"] = now_iso()
    return task


def queued_task_ids() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status='queued' ORDER BY created_at ASC, rowid ASC"
        ).fetchall()
    return [r["id"] for r in rows]


def enqueue_task(task_id: str) -> None:
    """幂等入队：只把非 queued/running 的任务置回 queued。

    条件更新是原子的，避免「提交时把调度器刚领走的 running 写回 queued」，
    否则同一个任务会被领取两次。
    """
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET status='queued', finished_at=NULL, error=NULL"
            " WHERE id=? AND status NOT IN ('queued','running')",
            (task_id,),
        )


def task_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status"
        ).fetchall()
    return {r["status"]: int(r["c"]) for r in rows}


def mark_running_interrupted(reason: str = "服务进程重启，任务被中断") -> int:
    """启动时把遗留 running 任务标记为 interrupted，避免永久占位。"""
    with connect() as conn:
        ids = [
            r["id"]
            for r in conn.execute("SELECT id FROM tasks WHERE status='running'").fetchall()
        ]
        if not ids:
            return 0
        stamp = now_iso()
        for task_id in ids:
            conn.execute(
                "UPDATE tasks SET status='interrupted', finished_at=?, error=? WHERE id=?",
                (stamp, reason, task_id),
            )
            conn.execute(
                "INSERT INTO events(task_id, level, message, created_at) VALUES(?,?,?,?)",
                (task_id, "warn", reason, stamp),
            )
    return len(ids)


# ─── 事件 ───────────────────────────────────────────────────────


def add_event(task_id: str | None, level: str, message: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO events(task_id, level, message, created_at) VALUES(?,?,?,?)",
            (task_id, level, str(message)[:1000], now_iso()),
        )
        return int(cur.lastrowid)


def list_events(
    since: int = 0,
    limit: int = 200,
    user_id: int | None = None,
    is_admin: bool = False,
) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    sql = (
        "SELECT e.id, e.task_id, e.level, e.message, e.created_at"
        " FROM events e LEFT JOIN tasks t ON t.id = e.task_id"
        " WHERE e.id > ?"
    )
    params: list[Any] = [int(since)]
    if not is_admin:
        sql += " AND (e.task_id IS NULL OR t.user_id = ?)"
        params.append(int(user_id or -1))
    sql += " ORDER BY e.id ASC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def reset_platform_data() -> None:
    """仅供测试/演示重置：清空任务与事件，保留账号。"""
    with connect() as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM tasks")


def task_to_public(row: dict, include_result: bool = False) -> dict:
    """把数据库行转成 API 友好的结构。"""
    progress: dict = {}
    if row.get("progress_json"):
        try:
            progress = json.loads(row["progress_json"])
        except (TypeError, json.JSONDecodeError):
            progress = {}
    result = None
    if include_result and row.get("result_json"):
        try:
            result = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError):
            result = None
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "project_id": row.get("project_id") or "default",
        "game_url": row["game_url"],
        "goal": row["goal"],
        "max_steps": row["max_steps"],
        "seed": row.get("seed"),
        "source_root": row.get("source_root"),
        "status": row["status"],
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "current_step": row.get("current_step") or 0,
        "total_steps": row.get("total_steps") or 0,
        "bugs_found": row.get("bugs_found") or 0,
        "progress": progress,
        "error": row.get("error"),
        "output_dir": row.get("output_dir"),
        "result": result,
    }
