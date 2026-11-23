"""运行时性能采样 —— 把「卡不卡」变成可进缺陷包的数字。

采集方式（页面内、零依赖、不引入 SDK）：
- requestAnimationFrame 帧间隔 → FPS 均值 / 最低值 / P95 帧耗时
- PerformanceObserver('longtask') → 长任务次数与最长耗时
- performance.memory（Chromium 专有）→ JS 堆峰值

判定方式（规则、可解释）：
    avg_fps < min_avg_fps  或  p95_frame_ms > max_p95_frame_ms
   或 max_long_task_ms > max_long_task_ms   或 heap_peak_mb > max_heap_mb

设计边界：
- 只做「采样 + 阈值判定」，不替游戏做优化建议（建议在 advisor.performance 规则里）
- 无头环境 rAF 可能被节流，样本不足时明确返回 insufficient_data，不伪造数字
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PERF_SCRIPT = r"""
(() => {
  if (window.__PLAYRANGER_PERF__) return;
  const P = { frames: [], longTasks: [], heap: [], started_at: performance.now() };
  window.__PLAYRANGER_PERF__ = P;

  let last = 0;
  function tick(ts) {
    if (last > 0) {
      const delta = ts - last;
      if (delta > 0) {
        P.frames.push(delta);
        if (P.frames.length > 3600) P.frames.shift();
      }
    }
    last = ts;
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        P.longTasks.push({ start: e.startTime, duration: e.duration });
        if (P.longTasks.length > 200) P.longTasks.shift();
      }
    }).observe({ entryTypes: ['longtask'] });
  } catch (e) { /* 浏览器不支持 longtask */ }

  try {
    setInterval(() => {
      const mem = performance.memory;
      if (mem && typeof mem.usedJSHeapSize === 'number') {
        P.heap.push(mem.usedJSHeapSize / 1048576);
        if (P.heap.length > 600) P.heap.shift();
      }
    }, 1000);
  } catch (e) { /* 非 Chromium 无 performance.memory */ }
})();
"""

# 默认阈值：面向 Web 小游戏的可玩底线，不是「3A 标准」
DEFAULT_THRESHOLDS: dict[str, float] = {
    "min_avg_fps": 30.0,
    "max_p95_frame_ms": 50.0,
    "max_long_task_ms": 200.0,
    "max_heap_mb": 512.0,
    "min_frames": 10,          # 少于此帧数不下结论
}

# 无头模式 rAF 被节流时的兜底阈值（CDP 窗口增量）
CDP_THRESHOLDS: dict[str, float] = {
    "max_busy_ratio": 0.85,    # 主线程忙碌占比
    "max_heap_mb": 512.0,
    "min_wall_s": 0.2,         # 采样窗口过短不下结论
}


@dataclass
class PerfSample:
    """一次性能采样结果。"""

    avg_fps: float = 0.0
    min_fps: float = 0.0
    p95_frame_ms: float = 0.0
    frame_count: int = 0
    long_task_count: int = 0
    max_long_task_ms: float = 0.0
    heap_peak_mb: float = 0.0
    duration_ms: float = 0.0
    insufficient_data: bool = False
    violations: list[str] = field(default_factory=list)
    # 无头模式 CDP 兜底：明确标注 FPS 不可用，不伪造帧率
    mode: str = "raf"
    busy_ratio: float = 0.0
    script_ratio: float = 0.0
    layout_ratio: float = 0.0
    heap_used_mb: float = 0.0
    fps_unavailable: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def passed(self) -> bool:
        return not self.insufficient_data and not self.violations


def summarize(raw: dict[str, Any], thresholds: dict[str, float] | None = None) -> PerfSample:
    """把页面采集到的原始数据压成可判定的指标。"""
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    frames = [float(x) for x in (raw.get("frames") or []) if isinstance(x, (int, float)) and x > 0]
    long_tasks = raw.get("longTasks") or []
    heap = [float(x) for x in (raw.get("heap") or []) if isinstance(x, (int, float))]
    started = float(raw.get("started_at") or 0.0)
    now_ms = float(raw.get("now") or 0.0)

    sample = PerfSample(
        frame_count=len(frames),
        long_task_count=len(long_tasks),
        max_long_task_ms=max((float(t.get("duration", 0)) for t in long_tasks), default=0.0),
        heap_peak_mb=max(heap, default=0.0),
        duration_ms=max(0.0, now_ms - started) if now_ms else 0.0,
    )

    if len(frames) < th["min_frames"]:
        sample.insufficient_data = True
        sample.violations.append(
            f"帧样本不足（{len(frames)} < {int(th['min_frames'])}），可能处于无头节流或页面未渲染"
        )
        return sample

    frames_sorted = sorted(frames)
    avg_frame = sum(frames) / len(frames)
    p95_index = min(len(frames_sorted) - 1, int(len(frames_sorted) * 0.95))
    p95 = frames_sorted[p95_index]
    worst = max(frames)

    sample.avg_fps = round(1000.0 / avg_frame, 1) if avg_frame > 0 else 0.0
    sample.min_fps = round(1000.0 / worst, 1) if worst > 0 else 0.0
    sample.p95_frame_ms = round(p95, 1)

    if sample.avg_fps < th["min_avg_fps"]:
        sample.violations.append(
            f"平均 FPS {sample.avg_fps:.1f} 低于阈值 {th['min_avg_fps']:.0f}"
        )
    if sample.p95_frame_ms > th["max_p95_frame_ms"]:
        sample.violations.append(
            f"P95 帧耗时 {sample.p95_frame_ms:.1f}ms 超过阈值 {th['max_p95_frame_ms']:.0f}ms"
        )
    if sample.max_long_task_ms > th["max_long_task_ms"]:
        sample.violations.append(
            f"最长长任务 {sample.max_long_task_ms:.0f}ms 超过阈值 {th['max_long_task_ms']:.0f}ms"
        )
    if sample.heap_peak_mb > th["max_heap_mb"]:
        sample.violations.append(
            f"JS 堆峰值 {sample.heap_peak_mb:.0f}MB 超过阈值 {th['max_heap_mb']:.0f}MB"
        )

    return sample


def install_script() -> str:
    """返回可直接 add_init_script 的采集脚本。"""
    return PERF_SCRIPT


def _metric(metrics: dict[str, Any], name: str) -> float | None:
    value = metrics.get(name)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def summarize_cdp(
    before: dict[str, Any],
    after: dict[str, Any],
    wall_s: float,
    thresholds: dict[str, float] | None = None,
) -> PerfSample:
    """把 CDP Performance.getMetrics 的窗口增量压成可判定指标。

    无头模式下 rAF 可能被浏览器节流，帧样本不足；此时用
    TaskDuration / ScriptDuration / LayoutDuration / JSHeapUsedSize 的增量
    判断主线程是否过载。明确标记 fps_unavailable=True，绝不编造 FPS。
    """
    th = {**CDP_THRESHOLDS, **(thresholds or {})}
    sample = PerfSample(mode="cdp_fallback", fps_unavailable=True)
    if not isinstance(before, dict) or not isinstance(after, dict) or not before or not after:
        sample.insufficient_data = True
        sample.violations.append("缺少 CDP 指标基线，无法计算窗口增量")
        return sample
    if wall_s < th["min_wall_s"]:
        sample.insufficient_data = True
        sample.violations.append(
            f"CDP 采样窗口过短（{wall_s:.2f}s < {th['min_wall_s']:.1f}s），不下结论"
        )
        return sample

    task_before = _metric(before, "TaskDuration")
    task_after = _metric(after, "TaskDuration")
    heap_after = _metric(after, "JSHeapUsedSize")
    if task_before is None or task_after is None:
        sample.insufficient_data = True
        sample.violations.append("CDP 未返回 TaskDuration，无法判定主线程负载")
        return sample

    def _delta(name: str) -> float:
        left = _metric(before, name)
        right = _metric(after, name)
        if left is None or right is None:
            return 0.0
        return max(0.0, right - left)

    task_delta = max(0.0, task_after - task_before)
    busy = min(1.0, task_delta / wall_s)
    script = min(1.0, _delta("ScriptDuration") / wall_s)
    layout = min(1.0, _delta("LayoutDuration") / wall_s)
    heap_mb = max(0.0, (heap_after or 0.0) / 1048576.0)

    sample.busy_ratio = round(busy, 3)
    sample.script_ratio = round(script, 3)
    sample.layout_ratio = round(layout, 3)
    sample.heap_used_mb = round(heap_mb, 1)
    sample.heap_peak_mb = round(heap_mb, 1)
    sample.duration_ms = round(wall_s * 1000, 1)

    if busy > th["max_busy_ratio"]:
        sample.violations.append(
            f"主线程忙碌占比 {busy * 100:.0f}% 超过阈值 {th['max_busy_ratio'] * 100:.0f}%"
            "（CDP 兜底判定，FPS 不可用）"
        )
    if heap_mb > th["max_heap_mb"]:
        sample.violations.append(
            f"JS 堆 {heap_mb:.0f}MB 超过阈值 {th['max_heap_mb']:.0f}MB"
        )
    return sample


def build_bug_fields(sample: PerfSample | dict[str, Any]) -> dict[str, Any]:
    """生成 performance 缺陷所需的字段（供 recorder.capture 使用）。"""
    data = sample.to_dict() if isinstance(sample, PerfSample) else dict(sample)
    violations = list(data.get("violations") or [])
    detail = "；".join(violations) or "性能指标未达阈值"
    if data.get("mode") == "cdp_fallback" or data.get("fps_unavailable"):
        description = (
            f"CDP 指标窗口采样（无头模式 FPS 不可用）："
            f"主线程忙碌占比 {float(data.get('busy_ratio', 0)) * 100:.0f}%，"
            f"脚本 {float(data.get('script_ratio', 0)) * 100:.0f}%，"
            f"布局 {float(data.get('layout_ratio', 0)) * 100:.0f}%，"
            f"JS 堆 {float(data.get('heap_used_mb', 0)):.0f}MB。判定：{detail}"
        )
    else:
        description = (
            f"平均 FPS {data.get('avg_fps', 0)}，最低 {data.get('min_fps', 0)}，"
            f"P95 帧耗时 {data.get('p95_frame_ms', 0)}ms，"
            f"长任务 {data.get('long_task_count', 0)} 次"
            f"（最长 {float(data.get('max_long_task_ms', 0)):.0f}ms），"
            f"堆峰值 {data.get('heap_peak_mb', 0):.0f}MB。判定：{detail}"
        )
    return {
        "title": f"运行时性能不达标：{violations[0] if violations else '指标异常'}",
        "anomaly_type": "performance",
        "description": description,
        "evidence": {"perf_sample": "runtime_perf"},
        "tags": ["performance", "auto_sampled"],
    }


__all__ = [
    "CDP_THRESHOLDS",
    "DEFAULT_THRESHOLDS",
    "PERF_SCRIPT",
    "PerfSample",
    "build_bug_fields",
    "install_script",
    "summarize",
    "summarize_cdp",
]
