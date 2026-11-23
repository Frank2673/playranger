"""真实试玩任务执行器 —— PlayEngine + ReActLoop + 缺陷归档。

每个任务在独立线程、独立浏览器实例中运行：
- 步骤回调写 steps.jsonl / progress.json，供控制台实时展示；
- 发现缺陷时由 BugRecorder 落盘完整证据包；
- 协作式取消：每步检查 should_stop，不跨线程强杀 Playwright。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

OUTPUT_ROOT = Path("output/tasks")
_MEMORY_LOCK = threading.Lock()


class TaskHooks:
    """执行器回调；控制台调度器与测试替身都按这个协议接入。"""

    def __init__(
        self,
        on_step: Callable[[dict], None] | None = None,
        on_log: Callable[[str, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._on_step = on_step or (lambda _step: None)
        self._on_log = on_log or (lambda _level, _message: None)
        self._should_stop = should_stop or (lambda: False)

    def on_step(self, step: dict) -> None:
        self._on_step(step)

    def on_log(self, level: str, message: str) -> None:
        self._on_log(level, message)

    def should_stop(self) -> bool:
        return bool(self._should_stop())


def task_output_dir(task_id: str) -> Path:
    root = Path(os.environ.get("PLAYRANGER_TASKS_DIR", str(OUTPUT_ROOT)))
    if not root.is_absolute():
        root = Path.cwd() / root
    return root / task_id


def normalize_game_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError("game_url 不能为空")
    if value.startswith(("http://", "https://", "file://")):
        return value
    path = Path(value).expanduser()
    if path.exists():
        return path.resolve().as_uri()
    raise ValueError("game_url 需为 http(s)/file URL，或一个存在的本地游戏文件路径")


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, payload: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _headless() -> bool:
    raw = os.environ.get("PLAYRANGER_HEADLESS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _capture_screenshots() -> bool:
    raw = os.environ.get("PLAYRANGER_CAPTURE_SCREENSHOTS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _compact_step(step: Any) -> dict:
    """把 ReActLoop 的 StepInfo 压成控制台友好的结构。"""
    state = step.state or {}
    verification = step.verification or {}
    return {
        "step": step.step,
        "action": step.action,
        "execution_ok": bool(step.execution_ok),
        "execution_error": step.execution_error,
        "correction_level": step.correction_level,
        "verification": {
            "success": bool(verification.get("success", False)),
            "reason": verification.get("reason", ""),
        },
        "state": {
            key: state.get(key)
            for key in ("scene", "hp", "score", "level", "game_over", "victory")
        },
        "timestamp": step.timestamp,
    }


class _LockedMemory:
    """并发任务共享记忆文件时，读写加进程内锁。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def build_context(self, state: dict) -> str:
        with _MEMORY_LOCK:
            return self._inner.build_context(state)

    def remember_bug(self, *args: Any, **kwargs: Any) -> Any:
        with _MEMORY_LOCK:
            return self._inner.remember_bug(*args, **kwargs)

    def save(self) -> Any:
        with _MEMORY_LOCK:
            return self._inner.save()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _collect_bugs(bug_root: Path, found: list[dict], source_root: str) -> list[dict]:
    """回读缺陷包，补全严重度、修复方案与源码定位。"""
    summaries: dict[str, dict] = {}
    for item in found or []:
        bug_dir = item.get("bug_dir")
        if not bug_dir:
            continue
        summaries[str(Path(bug_dir).resolve())] = dict(item)

    if bug_root.is_dir():
        for child in sorted(bug_root.iterdir()):
            if child.is_dir():
                summaries.setdefault(str(child.resolve()), {"bug_id": child.name})

    result: list[dict] = []
    for resolved, item in summaries.items():
        bug_dir = Path(resolved)
        payload = dict(item)
        defect_path = bug_dir / "defect.json"
        if defect_path.exists():
            try:
                defect = json.loads(defect_path.read_text(encoding="utf-8"))
                payload.update({
                    "bug_id": defect.get("bug_id") or payload.get("bug_id"),
                    "title": defect.get("title") or payload.get("title"),
                    "anomaly_type": defect.get("anomaly_type") or payload.get("anomaly_type"),
                    "severity": defect.get("severity") or payload.get("severity"),
                    "conclusion": defect.get("conclusion") or payload.get("conclusion"),
                    "description": defect.get("description", ""),
                    "tags": defect.get("tags", []),
                    "source": defect.get("source", {}),
                })
            except (OSError, json.JSONDecodeError):
                pass

        # 带源码根重新分析：修复方案里的 patch / 代码定位才有意义
        if source_root and (bug_dir / "defect.json").exists():
            try:
                from playranger.core.agent.recorder import load_defect
                from playranger.core.agent.intel import save_intel

                record = load_defect(bug_dir).to_dict()
                record["source_root"] = source_root
                save_intel(record, bug_dir)
            except Exception:
                pass

        for name, key in (("fix_plan.json", "fix_plan"), ("provenance.json", "provenance")):
            file_path = bug_dir / name
            if file_path.exists():
                try:
                    payload[key] = json.loads(file_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload[key] = {}
        payload["bug_dir"] = str(bug_dir)
        result.append(payload)
    return result


def run_task(task: dict, hooks: TaskHooks | None = None) -> dict:
    """执行一个试玩任务；返回写入 result.json 的 payload。"""
    hooks = hooks or TaskHooks()
    task_id = str(task["id"])
    out_dir = Path(task.get("output_dir") or task_output_dir(task_id))
    out_dir.mkdir(parents=True, exist_ok=True)
    bug_dir = out_dir / "bugs"
    screenshot_dir = out_dir / "screenshots"
    steps_path = out_dir / "steps.jsonl"
    steps_path.write_text("", encoding="utf-8")

    game_url = normalize_game_url(task["game_url"])
    goal = task.get("goal") or "找到所有可复现的缺陷"
    max_steps = int(task.get("max_steps") or 50)
    seed = task.get("seed")
    source_root = (task.get("source_root") or "").strip()
    started = time.perf_counter()
    steps_seen = 0

    progress = {
        "task_id": task_id,
        "status": "running",
        "goal": goal,
        "game_url": game_url,
        "current_step": 0,
        "max_steps": max_steps,
        "bugs_found": 0,
        "last_action": None,
        "started_at": time.time(),
    }
    _write_json_atomic(out_dir / "progress.json", progress)
    hooks.on_log("info", f"任务 {task_id} 启动：{game_url}（目标：{goal}）")

    def _on_step(step: Any) -> None:
        nonlocal steps_seen
        steps_seen += 1
        compact = _compact_step(step)
        _append_jsonl(steps_path, compact)
        progress.update({
            "current_step": compact["step"],
            "last_action": compact.get("action"),
            "last_correction": compact.get("correction_level"),
            "last_verification": compact.get("verification"),
            "bugs_found": progress.get("bugs_found", 0),
            "updated_at": time.time(),
        })
        _write_json_atomic(out_dir / "progress.json", progress)
        hooks.on_step(compact)

    engine = None
    try:
        from playranger.core.brain.brain_factory import create_brain
        from playranger.core.agent.react_loop import ReActLoop
        from playranger.core.agent.recorder import BugRecorder
        from playranger.core.agent.memory import AgentMemory
        from playranger.core.engines.action_validator import ActionValidator
        from playranger.core.engines.play_engine import PlayEngine

        planner = create_brain()
        brain_source = getattr(planner, "source", "mock")
        recorder = BugRecorder(output_dir=str(bug_dir))
        memory = _LockedMemory(AgentMemory())

        engine = PlayEngine(
            url=game_url,
            headless=_headless(),
            ci_mode=True,
            retry=1,
            viewport=(1280, 720),
            perf=True,
            seed=int(seed) if seed is not None else None,
        )
        engine.start()
        if seed is not None:
            try:
                engine.sdk_set_seed(int(seed))
            except Exception:
                pass

        loop = ReActLoop(
            play_engine=engine,
            planner=planner,
            validator=ActionValidator(),
            recorder=recorder,
            memory=memory,
            max_l1_retries=2,
            max_l2_retries=3,
            max_l3_retries=2,
            capture_screenshots=_capture_screenshots(),
            screenshot_dir=str(screenshot_dir),
            bug_on_l2_fail=True,
            on_step=_on_step,
            should_stop=hooks.should_stop,
        )
        loop_result = loop.run(goal=goal, max_steps=max_steps)
        payload = loop_result.to_dict()
        bugs = _collect_bugs(bug_dir, payload.get("bugs_found", []), source_root)
        status = "cancelled" if payload.get("status") == "cancelled" else (
            "failed" if payload.get("status") == "error" else "completed"
        )
        payload.update({
            "task_id": task_id,
            "status": status,
            "game_url": game_url,
            "source_root": source_root,
            "bugs_found": bugs,
            "brain_source": brain_source,
            "runtime": engine.runtime_stats(),
            "finished_at": time.time(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        _write_json_atomic(out_dir / "result.json", payload)
        progress.update({
            "status": status,
            "current_step": payload.get("total_steps", steps_seen),
            "bugs_found": len(bugs),
            "finished_at": time.time(),
        })
        _write_json_atomic(out_dir / "progress.json", progress)
        hooks.on_log(
            "info",
            f"任务 {task_id} 结束：{status}，步数 {payload.get('total_steps', steps_seen)}，"
            f"缺陷 {len(bugs)} 个",
        )
        return payload
    except Exception as exc:
        payload = {
            "task_id": task_id,
            "status": "failed",
            "goal": goal,
            "game_url": game_url,
            "error": str(exc),
            "bugs_found": [],
            "finished_at": time.time(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        try:
            _write_json_atomic(out_dir / "result.json", payload)
            progress.update({"status": "failed", "error": str(exc), "finished_at": time.time()})
            _write_json_atomic(out_dir / "progress.json", progress)
        except OSError:
            pass
        hooks.on_log("error", f"任务 {task_id} 失败：{exc}")
        raise
    finally:
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass


@dataclass
class StepSnapshot:
    """供测试/调试使用的轻量步骤结构。"""

    step: int
    action: dict
    execution_ok: bool
    correction_level: str
