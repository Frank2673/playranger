"""任务编排器 —— 内存队列 + 目标并发 + 动态扩缩 + 协作式取消。

并发策略：
- 一个 supervisor 线程每 0.5s 检查一次，按需从 SQLite 领取 queued 任务；
- 目标并发由内存建议值初始化，用户可在控制台 1..8 内随时调整；
- 缩容不打断运行中的任务，只影响后续领取；
- 停止任务只置取消标记，执行器在下一步退出（不跨线程强杀 Playwright）。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from playranger.platform import auth, db, system
from playranger.platform.runner import TaskHooks, run_task, task_output_dir


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "用户停止") -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class Orchestrator:
    def __init__(self, runner: Callable[[dict, TaskHooks], dict] | None = None) -> None:
        self._runner = runner or run_task
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._tokens: dict[str, CancellationToken] = {}
        self._target: int | None = None
        self._paused = False
        self._started = False
        self._stopping = False

    # ─── 生命周期 ────────────────────────────────────────────

    def set_runner(self, runner: Callable[[dict, TaskHooks], dict]) -> None:
        """替换执行器（测试替身 / 自定义引擎适配）。"""
        with self._lock:
            self._runner = runner

    def ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            db.init_db()
            db.mark_running_interrupted()
            if self._target is None:
                snap = system.snapshot()
                self._target = int(snap["recommended"])
            self._thread = threading.Thread(
                target=self._supervise, name="playranger-scheduler", daemon=True
            )
            self._started = True
            self._thread.start()
            db.add_event(
                None, "info", f"调度器启动，初始并发 {self._target}（按本机可用内存推荐）"
            )

    def shutdown(self) -> None:
        with self._lock:
            self._stopping = True
            tokens = list(self._tokens.values())
        for token in tokens:
            token.cancel("调度器关闭")
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _supervise(self) -> None:
        while not self._stopping:
            try:
                self._fill()
            except Exception as exc:  # 调度线程不能死
                try:
                    db.add_event(None, "error", f"调度器异常：{exc}")
                except Exception:
                    pass
            self._wake.wait(0.5)
            self._wake.clear()

    def _fill(self) -> None:
        while True:
            with self._lock:
                if self._stopping or self._paused:
                    return
                target = int(self._target or 1)
                if len(self._tokens) >= target:
                    return
            task = db.claim_next_queued()
            if task is None:
                return
            token = CancellationToken()
            with self._lock:
                self._tokens[task["id"]] = token
            threading.Thread(
                target=self._execute,
                args=(task, token),
                name=f"playranger-task-{task['id'][:8]}",
                daemon=True,
            ).start()

    # ─── 执行 ────────────────────────────────────────────────

    def _execute(self, task: dict, token: CancellationToken) -> None:
        task_id = task["id"]
        db.add_event(task_id, "info", f"Worker 已领取任务（目标 {self._target} 并发）")

        def on_step(step: dict) -> None:
            progress = {
                "current_step": step.get("step", 0),
                "last_action": step.get("action"),
                "last_correction": step.get("correction_level"),
                "last_verification": step.get("verification"),
                "bugs_found": (db.get_task(task_id) or {}).get("bugs_found", 0),
                "updated_at": time.time(),
            }
            db.update_task(
                task_id,
                current_step=int(step.get("step", 0)),
                progress_json=json.dumps(progress, ensure_ascii=False),
            )
            correction = step.get("correction_level")
            if correction and correction != "none":
                reason = (step.get("verification") or {}).get("reason", "")
                db.add_event(
                    task_id,
                    "warn",
                    f"第 {step.get('step')} 步触发 {correction} 修正：{reason}",
                )

        def on_log(level: str, message: str) -> None:
            db.add_event(task_id, level, message)

        hooks = TaskHooks(
            on_step=on_step,
            on_log=on_log,
            should_stop=lambda: token.cancelled,
        )

        try:
            result = self._runner(task, hooks)
            bugs = result.get("bugs_found") or []
            if token.cancelled or result.get("status") == "cancelled":
                status = "cancelled"
            elif result.get("status") == "failed":
                status = "failed"
            else:
                status = "completed"
            finished = db.now_iso()
            progress = {
                "current_step": int(result.get("total_steps") or 0),
                "bugs_found": len(bugs),
                "last_verification": None,
                "updated_at": time.time(),
                "status": status,
            }
            db.update_task(
                task_id,
                status=status,
                finished_at=finished,
                current_step=int(result.get("total_steps") or 0),
                total_steps=int(result.get("total_steps") or 0),
                bugs_found=len(bugs),
                progress_json=json.dumps(progress, ensure_ascii=False),
                result_json=json.dumps(result, ensure_ascii=False),
                error=result.get("error"),
            )
            level = "info" if status == "completed" else "warn"
            db.add_event(
                task_id,
                level,
                f"任务{('完成' if status == 'completed' else '结束')}："
                f"状态 {status}，步数 {result.get('total_steps', 0)}，缺陷 {len(bugs)} 个",
            )
        except Exception as exc:
            status = "cancelled" if token.cancelled else "failed"
            db.update_task(
                task_id,
                status=status,
                finished_at=db.now_iso(),
                error=str(exc),
            )
            db.add_event(task_id, "error", f"任务{('已取消' if status == 'cancelled' else '失败')}：{exc}")
        finally:
            with self._lock:
                self._tokens.pop(task_id, None)
            self._wake.set()

    # ─── 控制指令 ────────────────────────────────────────────

    def submit(self, task_id: str) -> dict | None:
        self.ensure_started()
        db.enqueue_task(task_id)
        db.add_event(task_id, "info", "任务已入队")
        self._wake.set()
        return db.get_task(task_id)

    def stop_task(self, task_id: str) -> dict | None:
        task = db.get_task(task_id)
        if task is None:
            return None
        with self._lock:
            token = self._tokens.get(task_id)
        if token is not None:
            token.cancel("用户在控制台停止")
            db.add_event(task_id, "warn", "已发送停止请求，等待当前动作结束")
        elif task["status"] == "queued":
            db.update_task(
                task_id,
                status="cancelled",
                finished_at=db.now_iso(),
                error="用户在排队阶段取消",
            )
            db.add_event(task_id, "warn", "排队任务已取消")
        elif task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
            db.add_event(task_id, "info", f"任务已处于终态：{task['status']}")
        return db.get_task(task_id)

    def retry_task(self, task: dict) -> dict:
        """复制任务配置，生成新任务并重新入队。"""
        new_id = f"{task['id'][:6]}-r{int(time.time()) % 100000:05d}"
        db.create_task({
            "id": new_id,
            "user_id": task["user_id"],
            "project_id": task.get("project_id") or "default",
            "game_url": task["game_url"],
            "goal": task["goal"],
            "max_steps": task["max_steps"],
            "seed": task.get("seed"),
            "source_root": task.get("source_root"),
            "status": "queued",
            "output_dir": str(task_output_dir(new_id)),
        })
        self.submit(new_id)
        db.add_event(new_id, "info", f"由任务 {task['id']} 复制重跑")
        return db.get_task(new_id) or {}

    def scale(self, workers: int) -> dict:
        value = max(1, min(system.hard_max_workers(), int(workers)))
        with self._lock:
            self._target = value
        db.add_event(None, "info", f"目标并发调整为 {value}")
        self._wake.set()
        return self.state()

    def pause(self) -> dict:
        with self._lock:
            self._paused = True
        db.add_event(None, "warn", "调度已暂停：不再领取新任务，运行中任务继续")
        return self.state()

    def resume(self) -> dict:
        with self._lock:
            self._paused = False
        db.add_event(None, "info", "调度已恢复")
        self._wake.set()
        return self.state()

    def stop_all(self) -> dict:
        with self._lock:
            self._paused = True
            tokens = list(self._tokens.items())
        for _task_id, token in tokens:
            token.cancel("主控台全部停止")
        for queued in db.queued_task_ids():
            db.update_task(
                queued,
                status="cancelled",
                finished_at=db.now_iso(),
                error="主控台全部停止",
            )
            db.add_event(queued, "warn", "排队任务随「全部停止」取消")
        db.add_event(None, "warn", f"主控台执行全部停止：取消 {len(tokens)} 个运行任务")
        return self.state()

    def state(self) -> dict:
        with self._lock:
            active = len(self._tokens)
            target = int(self._target or 1)
            paused = self._paused
            active_ids = sorted(self._tokens)
        counts = db.task_counts()
        return {
            "target_workers": target,
            "active": active,
            "paused": paused,
            "hard_max": system.hard_max_workers(),
            "active_task_ids": active_ids,
            "counts": counts,
        }


# ─── 进程级单例 ────────────────────────────────────────────────

_ORCH_LOCK = threading.Lock()
_ORCH: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _ORCH
    with _ORCH_LOCK:
        if _ORCH is None:
            _ORCH = Orchestrator()
        return _ORCH


def reset_orchestrator() -> None:
    """测试/重启用：关闭调度线程并丢弃单例。"""
    global _ORCH
    with _ORCH_LOCK:
        if _ORCH is not None:
            _ORCH.shutdown()
        _ORCH = None


def public_user_dict(user: dict) -> dict:
    return auth.public_user(user)
