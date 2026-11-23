"""批量并行调度 —— 把多个试玩任务分片并发，汇总时按缺陷指纹去重。

边界：
- 每个任务独立输出目录（缺陷包互不覆盖）
- 每个 worker 独立 PlayEngine / 浏览器上下文（不共享 Playwright 实例）
- 只做进程内线程池；不做分布式队列（那属于 SaaS 阶段）
- 崩溃隔离：单个任务异常只标记该任务失败，不影响其他任务

清单格式（JSON）：
    {
      "defaults": {"max_steps": 30, "goal": "找到所有可复现的缺陷"},
      "items": [
        {"url": "http://localhost:8080", "output": "output/batch/game-a"},
        {"url": "http://localhost:8081", "seed": 42}
      ]
    }
也接受顶层直接是数组。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

DEFAULT_WORKERS = 2
MAX_WORKERS = 8


@dataclass
class BatchItem:
    url: str
    goal: str = "找到所有可复现的缺陷"
    max_steps: int = 30
    seed: int | None = None
    output: str = ""
    name: str = ""

    def resolved_output(self, base: str, index: int) -> str:
        if self.output:
            return self.output
        slug = self.name or _slug(self.url) or f"task-{index + 1}"
        return os.path.join(base, slug)


@dataclass
class BatchResult:
    items: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    workers: int = DEFAULT_WORKERS
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        total_bugs = sum(len(i.get("bugs_found", [])) for i in self.items)
        failed = [i for i in self.items if i.get("status") == "failed"]
        return {
            "workers": self.workers,
            "duration_ms": round(self.duration_ms, 1),
            "total_tasks": len(self.items),
            "failed_tasks": len(failed),
            "total_bugs": total_bugs,
            "unique_bugs": total_bugs - sum(d.get("duplicates", 0) for d in self.duplicates),
            "duplicates": self.duplicates,
            "items": self.items,
        }


def load_manifest(path: str | Path) -> list[BatchItem]:
    """读取批量清单并做最小校验。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    defaults: dict[str, Any] = {}
    if isinstance(data, dict):
        defaults = data.get("defaults") or {}
        raw_items = data.get("items") or []
    elif isinstance(data, list):
        raw_items = data
    else:
        raise ValueError("清单必须是 JSON 对象（含 items）或数组")

    items: list[BatchItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or not raw.get("url"):
            raise ValueError(f"清单项缺少 url: {raw!r}")
        merged = {**defaults, **raw}
        items.append(
            BatchItem(
                url=str(merged["url"]),
                goal=str(merged.get("goal", "找到所有可复现的缺陷")),
                max_steps=int(merged.get("max_steps", 30)),
                seed=merged.get("seed"),
                output=str(merged.get("output", "")),
                name=str(merged.get("name", "")),
            )
        )
    return items


def run_batch(
    items: list[BatchItem],
    *,
    workers: int = DEFAULT_WORKERS,
    base_output: str = "output/batch",
    runner: Callable[[BatchItem, str], dict] | None = None,
) -> dict:
    """并行执行批量任务。

    Args:
        items: 任务清单
        workers: 并发数（1~8）
        base_output: 未指定 output 时的根目录
        runner: 可注入的执行器（测试用）；默认启动真实浏览器

    Returns:
        BatchResult.to_dict()
    """
    if not items:
        return BatchResult(workers=workers).to_dict()

    workers = max(1, min(MAX_WORKERS, int(workers)))
    execute = runner or _run_item
    started = time.perf_counter()

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for index, item in enumerate(items):
            output = item.resolved_output(base_output, index)
            os.makedirs(output, exist_ok=True)
            futures[pool.submit(_safe_run, execute, item, output)] = (index, item, output)

        for future in as_completed(futures):
            index, item, output = futures[future]
            payload = future.result()
            payload.update({
                "index": index,
                "url": item.url,
                "goal": item.goal,
                "output": output,
            })
            results.append(payload)

    results.sort(key=lambda r: r.get("index", 0))
    duration = (time.perf_counter() - started) * 1000.0
    duplicates = _find_duplicates(results)

    return BatchResult(
        items=results,
        duplicates=duplicates,
        workers=workers,
        duration_ms=duration,
    ).to_dict()


def _safe_run(execute: Callable[[BatchItem, str], dict], item: BatchItem, output: str) -> dict:
    """单任务异常隔离。"""
    started = time.perf_counter()
    try:
        payload = execute(item, output) or {}
        payload.setdefault("status", "completed")
    except Exception as e:  # noqa: BLE001 —— 子任务失败不影响其他任务
        payload = {"status": "failed", "error": str(e), "bugs_found": []}
    payload["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    payload.setdefault("bugs_found", [])
    return payload


def _run_item(item: BatchItem, output: str) -> dict:
    """默认执行器：真实浏览器 + ReActLoop。"""
    from playranger.core.agent.react_loop import ReActLoop
    from playranger.core.agent.recorder import BugRecorder
    from playranger.core.brain.brain_factory import create_brain
    from playranger.core.engines.action_validator import ActionValidator
    from playranger.core.engines.play_engine import PlayEngine

    engine = PlayEngine(
        url=item.url,
        headless=True,
        ci_mode=True,
        seed=item.seed,
        perf=True,
    )
    try:
        engine.start()
        loop = ReActLoop(
            play_engine=engine,
            planner=create_brain(),
            validator=ActionValidator(),
            recorder=BugRecorder(output_dir=os.path.join(output, "bugs")),
        )
        result = loop.run(goal=item.goal, max_steps=item.max_steps)
        payload = result.to_dict()
        payload["status"] = "completed" if result.status == "completed" else result.status
        return payload
    finally:
        engine.close()


def _find_duplicates(results: list[dict]) -> list[dict]:
    """按「异常类型 + 标题」找跨任务重复缺陷，并标记重复次数。"""
    seen: dict[tuple[str, str], list[str]] = {}
    for result in results:
        for bug in result.get("bugs_found", []):
            key = (str(bug.get("anomaly_type", "")), str(bug.get("title", "")))
            seen.setdefault(key, []).append(str(bug.get("bug_id", "")))

    duplicates: list[dict] = []
    for (anomaly, title), ids in seen.items():
        if len(ids) > 1:
            duplicates.append({
                "anomaly_type": anomaly,
                "title": title,
                "count": len(ids),
                "duplicates": len(ids) - 1,
                "bug_ids": ids,
            })
    return duplicates


def _slug(url: str) -> str:
    parsed = urlparse(url)
    raw = (parsed.netloc + parsed.path).strip("/") or "task"
    return "".join(ch if ch.isalnum() else "-" for ch in raw)[:40].strip("-")


__all__ = [
    "DEFAULT_WORKERS",
    "MAX_WORKERS",
    "BatchItem",
    "BatchResult",
    "load_manifest",
    "run_batch",
]
