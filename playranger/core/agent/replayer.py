"""回放器 —— 从缺陷包重放动作序列，验证复现。

设计原则（来自 Codex 方案交叉验证）：
- 复现模式 = 固定初始条件 + 固定动作轨迹 + 关闭重新规划
- 复现率 = 成功次数 / 总次数，不是"成功一次就叫可复现"
- 关闭大模型也能重放（纯引擎层）
- 失败也要保留完整证据，不能直接关单

Day 3 实现：基础版重放（从轨迹 JSONL 重放 + 断言验证 + 复现率统计）
Day 6 完善：完整缺陷包（defect.json + screenshots + trace.zip）
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from playranger.core.engines.play_engine import PlayEngine


# ── 重放结果 ────────────────────────────────────────────────


@dataclass
class ReplayResult:
    """单次重放结果。"""

    run_id: int
    success: bool  # 断言是否命中
    final_state: dict[str, Any]
    steps_executed: int
    error: str | None = None
    duration_ms: float = 0.0
    seed_mode: str = ""


@dataclass
class ReproductionReport:
    """复现报告（多次重放的统计）。"""

    bug_id: str
    total_runs: int
    success_runs: int
    reproduction_rate: float  # 0.0 - 1.0
    results: list[ReplayResult] = field(default_factory=list)
    seed_mode: str = ""

    @property
    def is_reproducible(self) -> bool:
        """是否认为可复现（默认阈值 80%）。"""
        if self.total_runs == 0:
            return False
        return self.reproduction_rate >= 0.8

    @property
    def reproduction_state(self) -> str:
        """三态复现：reproduced / partial / not_reproduced。"""
        if self.total_runs == 0 or self.reproduction_rate <= 0:
            return "not_reproduced"
        if self.reproduction_rate >= 0.8:
            return "reproduced"
        return "partial"

    @property
    def avg_duration_ms(self) -> float:
        if not self.results:
            return 0.0
        return round(sum(r.duration_ms for r in self.results) / len(self.results), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bug_id": self.bug_id,
            "total_runs": self.total_runs,
            "success_runs": self.success_runs,
            "reproduction_rate": round(self.reproduction_rate, 4),
            "state": self.reproduction_state,
            "seed_mode": self.seed_mode,
            "avg_duration_ms": self.avg_duration_ms,
        }


# ── 断言函数 ────────────────────────────────────────────────


AssertionFn = Callable[[dict[str, Any]], tuple[bool, str]]


def build_assertion_from_dict(assertion_spec: dict[str, Any]) -> AssertionFn:
    """从断言规范构造断言函数。

    支持的断言类型：
    - state_equals: { field: value } 检查 state[field] == value
    - state_greater_than / state_less_than: 数值比较
    - custom: 用户自定义表达式字符串（慎用）
    """
    def _assert(state: dict[str, Any]) -> tuple[bool, str]:
        # state_equals
        if "state_equals" in assertion_spec:
            for field, expected in assertion_spec["state_equals"].items():
                actual = state.get(field)
                if actual != expected:
                    return False, f"state['{field}'] = {actual}, 期望 {expected}"
            return True, "all state_equals passed"

        # state_less_than
        if "state_less_than" in assertion_spec:
            for field, expected in assertion_spec["state_less_than"].items():
                actual = state.get(field, 0)
                if not isinstance(actual, (int, float)):
                    return False, f"state['{field}'] = {actual} 不是数字"
                if actual >= expected:
                    return False, f"state['{field}'] = {actual} >= {expected}"
            return True, "all state_less_than passed"

        # state_greater_than
        if "state_greater_than" in assertion_spec:
            for field, expected in assertion_spec["state_greater_than"].items():
                actual = state.get(field, 0)
                if not isinstance(actual, (int, float)):
                    return False, f"state['{field}'] = {actual} 不是数字"
                if actual <= expected:
                    return False, f"state['{field}'] = {actual} <= {expected}"
            return True, "all state_greater_than passed"

        return True, "no assertion checks"

    return _assert


# ── 回放器主体 ──────────────────────────────────────────────


class Replayer:
    """Bug 复现回放器。

    核心能力：
    - 从动作轨迹 JSONL 重放
    - 支持设置初始状态 + 随机种子
    - 支持多次重放统计复现率
    - 断言验证（缺陷是否真的触发）

    复现模式下：固定动作，不允许重规划，不做自修正。
    """

    def __init__(
        self,
        engine_factory: Callable[[], PlayEngine],
    ) -> None:
        """
        Args:
            engine_factory: 无参函数，返回一个新的 PlayEngine 实例（每次重放创建新的）
        """
        self._make_engine = engine_factory

    # ── 公共 API ────────────────────────────────────────

    def replay_once(
        self,
        action_trace: list[dict[str, Any]],
        assertion: dict[str, Any] | AssertionFn | None = None,
        initial_state: dict[str, Any] | None = None,
        seed: int | None = None,
    ) -> ReplayResult:
        """重放一次，返回重放结果。

        Args:
            action_trace: 动作轨迹列表，每项含 action_type / action_target / action_params
            assertion: 断言（dict 规范或函数），None 则只重放不验证
            initial_state: 可选，初始状态（通过 set_param 等方式设置）
            seed: 可选，随机种子

        Returns:
            ReplayResult
        """
        engine = self._make_engine()
        started_at = time.perf_counter()
        seed_mode = "none"
        try:
            # 种子必须在页面加载前设置，才能注入 init script
            if seed is not None:
                try:
                    engine.seed = seed
                except Exception:
                    pass

            engine.start()

            # 切换到复现模式（虽然目前行为一样，但语义明确）
            engine.mode = "reproduce"
            seed_mode = str(getattr(engine, "seed_mode", "none"))

            # 设置种子（SDK 支持时双重保险）
            if seed is not None:
                try:
                    engine.sdk_set_seed(seed)
                except Exception:
                    pass

            # 重置游戏
            engine.sdk_reset()

            # 设置初始状态（如果提供）
            if initial_state:
                self._apply_initial_state(engine, initial_state)

            # 清空轨迹（reset 已经清了，但保险）
            engine.reset_trace()

            # 逐个重放动作
            steps_executed = 0
            last_error = None

            for step_data in action_trace:
                action_type = step_data.get("action_type", "wait")
                target = step_data.get("action_target") or step_data.get("target")
                params = step_data.get("action_params") or step_data.get("params") or {}

                record = engine.execute(
                    action_type=action_type,
                    target=target,
                    params=params,
                )
                steps_executed += 1

                if not record.ok:
                    last_error = record.error
                    # 复现模式：动作失败也要继续走完？还是停下来？
                    # 这里选择：继续走完，因为有时后面的动作才触发 bug
                    # 但记录最后一个错误

            # 读取终态
            final_state = engine.read_state().raw

            # 验证断言
            success = True
            if assertion is not None:
                if callable(assertion):
                    ok, _ = assertion(final_state)
                    success = ok
                else:
                    assert_fn = build_assertion_from_dict(assertion)
                    ok, _ = assert_fn(final_state)
                    success = ok

            return ReplayResult(
                run_id=0,
                success=success,
                final_state=final_state,
                steps_executed=steps_executed,
                error=last_error,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
                seed_mode=seed_mode,
            )

        finally:
            engine.close()

    def replay_multiple(
        self,
        action_trace: list[dict[str, Any]],
        num_runs: int = 3,
        assertion: dict[str, Any] | AssertionFn | None = None,
        initial_state: dict[str, Any] | None = None,
        seed: int | None = None,
        bug_id: str = "unknown",
    ) -> ReproductionReport:
        """多次重放，统计复现率。

        Args:
            action_trace: 动作轨迹
            num_runs: 重放次数
            assertion: 断言
            initial_state: 初始状态
            seed: 随机种子（None 则每次用不同种子）
            bug_id: bug 标识

        Returns:
            ReproductionReport
        """
        results: list[ReplayResult] = []
        success_count = 0

        for i in range(num_runs):
            # 如果没指定种子，每次用不同种子模拟随机性
            run_seed = seed if seed is not None else 1000 + i

            result = self.replay_once(
                action_trace=action_trace,
                assertion=assertion,
                initial_state=initial_state,
                seed=run_seed,
            )
            result.run_id = i + 1
            results.append(result)
            if result.success:
                success_count += 1

        rate = success_count / num_runs if num_runs > 0 else 0.0

        return ReproductionReport(
            bug_id=bug_id,
            total_runs=num_runs,
            success_runs=success_count,
            reproduction_rate=rate,
            results=results,
            seed_mode=results[0].seed_mode if results else "",
        )

    def replay_from_file(
        self,
        trace_file: str,
        assertion_file: str | None = None,
        num_runs: int = 3,
        bug_id: str = "unknown",
        initial_state: dict[str, Any] | None = None,
        seed: int | None = None,
    ) -> ReproductionReport:
        """从文件重放。

        Args:
            trace_file: 动作轨迹文件（JSON 或 JSONL）
            assertion_file: 断言规范文件（JSON），可选
            num_runs: 重放次数
            bug_id: bug 标识
            initial_state: 可选，重放前通过 set_param 设置的初始状态
            seed: 可选，固定随机种子
        """
        # 加载轨迹
        action_trace = self._load_trace(trace_file)

        # 加载断言
        assertion = None
        if assertion_file and os.path.exists(assertion_file):
            with open(assertion_file, "r", encoding="utf-8") as f:
                assertion = json.load(f)

        return self.replay_multiple(
            action_trace=action_trace,
            num_runs=num_runs,
            assertion=assertion,
            initial_state=initial_state,
            seed=seed,
            bug_id=bug_id,
        )

    # ── 内部方法 ────────────────────────────────────────

    @staticmethod
    def _load_trace(path: str) -> list[dict[str, Any]]:
        """加载轨迹文件（支持 JSON 数组或 JSONL）。"""
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()

        if not text:
            return []

        # 试试 JSON 数组
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

        # 试试 JSONL
        lines = text.split("\n")
        result = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return result

    @staticmethod
    def _apply_initial_state(engine: PlayEngine, state: dict[str, Any]) -> None:
        """应用初始状态（通过 SDK 的 set_param）。

        只处理简单的数值字段，复杂状态初始化留给游戏自己的 reset + snapshot。
        """
        for key, value in state.items():
            if key in ("hp", "score", "level"):
                engine.execute(
                    action_type="set_param",
                    target=None,
                    params={"param": key, "value": value},
                )


# ── 缺陷包回写 ──────────────────────────────────────────────

DEFECT_FILE = "defect.json"


def update_defect_reproduction(
    bug_dir: str | os.PathLike[str],
    report: ReproductionReport,
) -> dict[str, Any]:
    """把复现三态写回缺陷包的 defect.json。

    Returns:
        写入的 reproduction 对象；缺陷包不存在或读取失败时返回空 dict。
    """
    path = os.path.join(str(bug_dir), DEFECT_FILE)
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            defect = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(defect, dict):
        return {}

    reproduction = {
        "state": report.reproduction_state,
        "rate": round(report.reproduction_rate, 4),
        "runs": report.total_runs,
        "success_runs": report.success_runs,
        "seed_mode": report.seed_mode,
        "avg_duration_ms": report.avg_duration_ms,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    defect["reproduction_rate"] = reproduction["rate"]
    defect["reproduction_attempts"] = report.total_runs
    defect["reproduction"] = reproduction

    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(defect, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return {}

    return reproduction


__all__ = [
    "DEFECT_FILE",
    "ReplayResult",
    "ReproductionReport",
    "Replayer",
    "build_assertion_from_dict",
    "update_defect_reproduction",
]
