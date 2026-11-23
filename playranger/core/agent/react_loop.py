"""ReAct 主循环 —— 感知 → 规划 → 校验 → 执行 → 验证 → 修正 → 下一轮。

6 步循环：
1. 感知 (observe) — 读取游戏状态 + 视觉检查（可选）
2. 规划 (plan) — LLM 根据状态+目标输出下一步动作
3. 校验 (validate) — 动作语义校验（白名单/前置条件/参数范围）
4. 执行 (execute) — PlayEngine 执行动作，记录轨迹
5. 验证 (verify) — 检查结果是否符合预期 + 视觉异常检测
6. 修正 (correct) — 三层自修正：L1 动作层 / L2 路径层 / L3 策略层

三层自修正：
- L1 动作层：动作校验失败 → 让 Planner 重新生成（同一状态）
- L2 路径层：执行成功但验证失败 → replan 换路径/子目标
- L3 策略层：连续 N 次 L2 失败 → reflect 深度反思 + 记录缺陷 + 决定是否升级

P0 纪律：
- 自修正不抹缺陷：恢复成功也保留原始失败记录
- 结论三档：auto_confirmed / auto_warning / human_required
- 证据状态纪律：每个字段都要标记证据来源
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from playranger.core.brain.planner import Planner, PlanOutput, ReplanOutput, GameAction
from playranger.core.engines.play_engine import PlayEngine, GameState
from playranger.core.engines.vision_engine import VisionEngine
from playranger.core.engines.action_validator import ActionValidator, ValidationResult
from playranger.core.agent.recorder import BugRecorder
from playranger.core.agent.memory import AgentMemory
from playranger.core.agent.verifier import (
    verify as verifier_verify,
    build_assertion_from_dict,
    VerificationResult,
)


# ─── 数据结构 ───────────────────────────────────────────────────

CORRECTION_LEVELS = ("none", "L1_action", "L2_path", "L3_strategy")
CONCLUSION_LEVELS = ("auto_confirmed", "auto_warning", "human_required")


@dataclass
class StepInfo:
    """单步的完整信息（用于历史和调试）。"""
    step: int
    state: dict
    plan: dict  # PlanOutput.to_dict
    action: dict  # GameAction.dict
    validation: dict  # ValidationResult
    execution_ok: bool
    execution_error: str | None
    verification: dict  # VerificationResult.to_dict
    correction_level: str  # none / L1_action / L2_path / L3_strategy
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "state": self.state,
            "plan": self.plan,
            "action": self.action,
            "validation": self.validation,
            "execution_ok": self.execution_ok,
            "execution_error": self.execution_error,
            "verification": self.verification,
            "correction_level": self.correction_level,
            "timestamp": self.timestamp,
        }


@dataclass
class LoopResult:
    """ReAct 循环的最终结果。"""
    goal: str
    success: bool          # 是否达成目标
    total_steps: int      # 总步数
    l1_retries: int       # L1 重试次数
    l2_retries: int       # L2 重试次数
    l3_retries: int       # L3 重试次数
    status: str           # completed / max_steps / l3_exhausted / error
    final_state: dict
    bugs_found: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    conclusion: str = "human_required"  # auto_confirmed / auto_warning / human_required

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "success": self.success,
            "total_steps": self.total_steps,
            "l1_retries": self.l1_retries,
            "l2_retries": self.l2_retries,
            "l3_retries": self.l3_retries,
            "status": self.status,
            "final_state": self.final_state,
            "bugs_found": self.bugs_found,
            "history_count": len(self.history),
            "conclusion": self.conclusion,
        }


# ─── 主循环实现 ────────────────────────────────────────────────

class ReActLoop:
    """ReAct 6 步循环 + 三层自修正。

    用法：
        loop = ReActLoop(
            play_engine=engine,
            planner=planner,
            vision_engine=vision,
            validator=validator,
            recorder=recorder,
        )
        result = loop.run("找到至少 1 个 bug", max_steps=50)
    """

    def __init__(
        self,
        play_engine: PlayEngine | None = None,
        planner: Planner | None = None,
        vision_engine: VisionEngine | None = None,
        validator: ActionValidator | None = None,
        recorder: BugRecorder | None = None,
        max_l1_retries: int = 2,       # L1：同一动作失败的重试次数
        max_l2_retries: int = 3,       # L2：同一路径连续失败的次数
        max_l3_retries: int = 2,       # L3：策略反思的轮数
        capture_screenshots: bool = False,  # 是否每步截图（给视觉检测用）
        screenshot_dir: str = "screenshots",
        bug_on_l2_fail: bool = True,   # L2 连续失败是否记 bug
        memory: AgentMemory | None = None,  # 跨轮次记忆（可选）
        on_step: Callable[[StepInfo], None] | None = None,  # 每步回调（控制台进度）
        should_stop: Callable[[], bool] | None = None,      # 协作式取消检查
    ) -> None:
        self.play_engine = play_engine
        self.planner = planner
        self.vision_engine = vision_engine
        self.validator = validator
        self.recorder = recorder
        self.memory = memory
        self._on_step = on_step
        self._should_stop = should_stop

        self.max_l1_retries = max_l1_retries
        self.max_l2_retries = max_l2_retries
        self.max_l3_retries = max_l3_retries
        self.capture_screenshots = capture_screenshots
        self.screenshot_dir = screenshot_dir
        self.bug_on_l2_fail = bug_on_l2_fail

        # 运行时状态
        self._history: list[StepInfo] = []
        self._bugs_found: list[dict] = []
        self._l2_streak = 0  # 连续 L2 失败计数
        self._current_round_l2 = 0  # 当前轮的 L2 计数

    # ─── 公共 API ────────────────────────────────────────────

    def run(
        self,
        goal: str,
        max_steps: int = 50,
        initial_state_override: dict | None = None,
    ) -> LoopResult:
        """运行 ReAct 循环直到达成目标或耗尽步数。

        Args:
            goal: 测试目标（自然语言）
            max_steps: 最大步数
            initial_state_override: 可选，覆盖初始状态（纯 mock 测试用）

        Returns:
            LoopResult —— 完整的运行结果
        """
        if self.play_engine is None and self.planner is None:
            # 全 mock 模式，直接返回
            return LoopResult(
                goal=goal,
                success=False,
                total_steps=0,
                l1_retries=0,
                l2_retries=0,
                l3_retries=0,
                status="not_implemented",
                final_state=initial_state_override or {},
                conclusion="human_required",
            )

        # 确保截图目录存在
        if self.capture_screenshots:
            os.makedirs(self.screenshot_dir, exist_ok=True)

        # 记录本轮测试开始时间（用于缺陷包确定性复盘）
        run_started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._run_started_at = run_started_at

        # 初始化
        total_l1 = 0
        total_l2 = 0
        total_l3 = 0
        status = "running"
        current_state_dict = initial_state_override or self._observe_state()
        initial_state = dict(current_state_dict)  # 保存初始状态快照

        for step in range(1, max_steps + 1):
            # 协作式取消：控制台停止/关闭时，在下一步开始前退出
            if self._stop_requested():
                status = "cancelled"
                break
            # Step 1: 感知（已经在循环末尾做了，第一步用初始状态）
            # Step 2-6: 规划 → 校验 → 执行 → 验证 → 修正
            step_result, correction = self._do_one_step(
                step=step,
                goal=goal,
                state=current_state_dict,
            )

            self._history.append(step_result)
            self._notify_step(step_result)

            if self._stop_requested():
                status = "cancelled"
                break

            if correction == "L1_action":
                total_l1 += 1
            elif correction == "L2_path":
                total_l2 += 1
                self._l2_streak += 1
                self._current_round_l2 += 1
            elif correction == "L3_strategy":
                total_l3 += 1
                self._l2_streak = 0
                self._current_round_l2 = 0
            else:
                # 正常推进，清零 streak
                self._l2_streak = 0

            # 更新当前状态
            if step_result.execution_ok:
                current_state_dict = step_result.verification.get("final_state") or step_result.state

            # 检查是否达成目标
            if self._check_goal_reached(goal, current_state_dict):
                status = "completed"
                break

            # 检查 L3 是否耗尽
            if total_l3 >= self.max_l3_retries:
                status = "l3_exhausted"
                break

            # L3 触发：连续 L2 失败达到阈值 → 反思 + 记录 bug
            if self._l2_streak >= self.max_l2_retries:
                self._handle_l3_strategy(
                    goal=goal,
                    state=current_state_dict,
                    initial_state=initial_state,
                    step=step,
                )
                total_l3 += 1
                self._l2_streak = 0
                self._current_round_l2 = 0

                if total_l3 >= self.max_l3_retries:
                    status = "l3_exhausted"
                    break
        else:
            status = "max_steps"

        # P1-5 性能采样：跑完后按阈值判定，超标自动生成 performance 缺陷
        self._maybe_record_perf(
            goal=goal,
            initial_state=initial_state,
            final_state=current_state_dict,
            step=len(self._history),
        )

        # 生成最终结论
        conclusion = self._compute_conclusion(status, total_l3)

        return LoopResult(
            goal=goal,
            success=(status == "completed"),
            total_steps=len(self._history),
            l1_retries=total_l1,
            l2_retries=total_l2,
            l3_retries=total_l3,
            status=status,
            final_state=current_state_dict,
            bugs_found=list(self._bugs_found),
            history=[s.to_dict() for s in self._history],
            conclusion=conclusion,
        )

    # ─── 单步六步循环 ────────────────────────────────────────

    def _stop_requested(self) -> bool:
        """外部取消检查；回调异常不应中断测试。"""
        if self._should_stop is None:
            return False
        try:
            return bool(self._should_stop())
        except Exception:
            return False

    def _notify_step(self, step_result: StepInfo) -> None:
        """把单步结果推给控制台；回调异常不影响测试主流程。"""
        if self._on_step is None:
            return
        try:
            self._on_step(step_result)
        except Exception:
            pass

    def _do_one_step(
        self,
        step: int,
        goal: str,
        state: dict,
    ) -> tuple[StepInfo, str]:
        """执行一步：规划 → L1 校验重试 → 执行 → 验证 → L2 replan。

        Returns:
            (StepInfo, correction_level)
            correction_level: none / L1_action / L2_path / L3_strategy
        """
        correction_level = "none"

        # ===== Step 2: 规划 =====
        plan = self._plan_step(step, goal, state)

        # ===== Step 3: 校验（带 L1 重试） =====
        action = plan.action
        validation = self._validate_action(action, state)
        l1_count = 0

        while not validation.ok and l1_count < self.max_l1_retries:
            l1_count += 1
            correction_level = "L1_action"
            # 把失败原因告诉 Planner，让它重生成
            plan = self._replan_l1(step, goal, state, plan, validation)
            action = plan.action
            validation = self._validate_action(action, state)

        if not validation.ok:
            # L1 用尽，动作还是不合法 → 执行失败，记为 L1 失败
            return StepInfo(
                step=step,
                state=state,
                plan=self._plan_to_dict(plan),
                action=action.model_dump(),
                validation={"ok": False, "reason": validation.reason, "failed_layer": validation.failed_layer},
                execution_ok=False,
                execution_error=f"action validation failed after L1 retries: {validation.reason}",
                verification={"success": False, "reason": "action not executed", "mismatch_details": None},
                correction_level="L1_action",
            ), "L1_action"

        # ===== Step 4: 执行 =====
        exec_ok, exec_error, new_state, screenshot_before, screenshot_after = self._execute_action(
            action, step,
        )

        # ===== Step 5: 验证 =====
        vision_result = None
        if self.vision_engine and screenshot_after:
            vr = self.vision_engine.inspect(screenshot_after)
            vision_result = vr.to_dict()

        verification = verifier_verify(
            expected=plan.expected_after,
            new_state=new_state,
            vision_result=vision_result,
        )

        # 如果执行失败，验证肯定也失败
        if not exec_ok:
            verification = VerificationResult(
                success=False,
                reason=f"执行失败: {exec_error}",
                mismatch_details={"execution_error": exec_error},
            )

        step_info = StepInfo(
            step=step,
            state=state,
            plan=self._plan_to_dict(plan),
            action=action.model_dump(),
            validation={"ok": validation.ok, "reason": validation.reason, "failed_layer": validation.failed_layer},
            execution_ok=exec_ok,
            execution_error=exec_error,
            verification=verification.to_dict(),
            correction_level=correction_level,
        )
        # 把新状态附在 verification 里，方便上层取用
        step_info.verification["final_state"] = new_state

        # ===== Step 6: L2 路径层修正 =====
        if not verification.success:
            # 触发 L2 replan（只做一次，具体重试在上层循环控制）
            correction_level = "L2_path" if correction_level == "none" else correction_level
            step_info.correction_level = correction_level

        return step_info, correction_level

    # ─── 各步骤具体实现 ──────────────────────────────────────

    def _observe_state(self) -> dict:
        """Step 1: 感知 —— 读取游戏状态。"""
        if self.play_engine:
            gs = self.play_engine.read_state()
            return gs.raw
        return {}

    def _plan_step(self, step: int, goal: str, state: dict) -> PlanOutput:
        """Step 2: 规划 —— 让 Planner 输出下一步动作。"""
        if self.planner is None:
            # 无 Planner，返回 wait
            return PlanOutput(
                thought="no planner available",
                action=GameAction(action_type="wait", reason="no_planner"),
                expected_after="no change",
                confidence=0.0,
            )

        available_actions = state.get("available_actions", [])
        history_simple = self._build_history_for_planner()

        memory_hint = self.memory.build_context(state) if self.memory else ""

        return self.planner.plan(
            state=state,
            goal=goal,
            available_actions=available_actions,
            history=history_simple,
            step=step,
            max_steps=100,
            memory_hint=memory_hint,
        )

    def _validate_action(self, action: GameAction, state: dict) -> ValidationResult:
        """Step 3: 动作语义校验。"""
        if self.validator is None:
            # 无校验器，默认通过（但至少检查 action_type 非空）
            if not action.action_type:
                return ValidationResult.fail("whitelist", "empty action_type", "provide a valid action type")
            return ValidationResult.pass_()

        # 用 state 里的 available_actions 动态更新白名单
        available = state.get("available_actions", [])
        if available:
            self.validator.set_available_actions(available)
        return self.validator.validate(action, state)

    def _replan_l1(
        self,
        step: int,
        goal: str,
        state: dict,
        failed_plan: PlanOutput,
        validation: ValidationResult,
    ) -> PlanOutput:
        """L1 修正：动作校验失败，让 Planner 重新规划。"""
        if self.planner is None:
            return PlanOutput(
                thought="no planner, returning wait",
                action=GameAction(action_type="wait", reason="l1_fallback"),
                expected_after="no change",
                confidence=0.0,
            )

        failure_reason = f"动作校验失败（{validation.failed_layer}）: {validation.reason}. 建议: {validation.suggestion}"
        available_actions = state.get("available_actions", [])

        replan_result = self.planner.replan(
            state=state,
            goal=goal,
            failure_reason=failure_reason,
            available_actions=available_actions,
            failed_attempts=1,
        )

        # 把 ReplanOutput 转成 PlanOutput（统一接口）
        return PlanOutput(
            thought=f"[L1 修正] {replan_result.thought}",
            sub_goal=replan_result.new_sub_goal,
            action=replan_result.action,
            expected_after=replan_result.expected_after,
            confidence=0.3,
        )

    def _execute_action(
        self,
        action: GameAction,
        step: int,
    ) -> tuple[bool, str | None, dict, str | None, str | None]:
        """Step 4: 执行动作。

        Returns:
            (ok, error, new_state, screenshot_before_path, screenshot_after_path)
        """
        new_state = {}
        shot_before = None
        shot_after = None

        if self.play_engine is None:
            # 无引擎，模拟成功
            return True, None, {}, None, None

        try:
            # 截图（前）
            if self.capture_screenshots:
                shot_before = os.path.join(self.screenshot_dir, f"step_{step:03d}_before.png")
                self.play_engine.screenshot(shot_before)

            # 执行
            record = self.play_engine.execute(
                action_type=action.action_type,
                target=action.target,
                params=action.params or {},
                capture_screenshots=False,  # 我们自己管理截图
            )

            new_state = record.state_after

            # 截图（后）
            if self.capture_screenshots:
                shot_after = os.path.join(self.screenshot_dir, f"step_{step:03d}_after.png")
                self.play_engine.screenshot(shot_after)

            return record.ok, record.error, new_state, shot_before, shot_after

        except Exception as e:
            error_msg = str(e)
            # 尝试读取当前状态（即使动作失败了也可能有状态）
            try:
                gs = self.play_engine.read_state()
                new_state = gs.raw
            except Exception:
                pass
            return False, error_msg, new_state, shot_before, shot_after

    def _handle_l3_strategy(
        self,
        goal: str,
        state: dict,
        initial_state: dict,
        step: int,
    ) -> None:
        """L3 策略层修正：连续 L2 失败 → 反思 + 记录 bug。"""
        # 1. 记录 bug
        if self.recorder and self.bug_on_l2_fail:
            self._record_bug(
                goal=goal,
                initial_state=initial_state,
                final_state=state,
                step=step,
                anomaly_type="strategy_stuck",
                severity="major",
                title=f"连续 {self.max_l2_retries} 次路径失败，疑似游戏逻辑或引导问题",
                conclusion="auto_warning",
                evidence={"consecutive_l2_failures": "action_trace"},
            )

        # 2. 调用 Planner.reflect 做深度反思（如果有）
        if self.planner:
            action_seq = [s.to_dict() for s in self._history[-10:]]
            failure_reason = f"连续 {self.max_l2_retries} 次 L2 路径修正失败，当前状态无法推进"
            self.planner.reflect(
                goal=goal,
                action_seq=action_seq,
                failure_reason=failure_reason,
            )

    def _record_bug(
        self,
        goal: str,
        initial_state: dict,
        final_state: dict,
        step: int,
        anomaly_type: str,
        severity: str,
        title: str,
        conclusion: str,
        evidence: dict,
        description: str = "",
        source: dict | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """记录一个缺陷到 BugRecorder。"""
        if self.recorder is None:
            # 没有 recorder，只记在内存里
            entry = {
                "title": title,
                "anomaly_type": anomaly_type,
                "severity": severity,
                "conclusion": conclusion,
                "step": step,
            }
            self._bugs_found.append(entry)
            self._remember_bug(entry, final_state or initial_state)
            return

        action_trace = [s.to_dict() for s in self._history]

        # 找最后一次失败的截图
        failure_screenshot = None
        if self.capture_screenshots and self._history:
            last = self._history[-1]
            shot_path = os.path.join(self.screenshot_dir, f"step_{last.step:03d}_after.png")
            if os.path.exists(shot_path):
                failure_screenshot = shot_path

        initial_screenshot = None
        if self.capture_screenshots:
            shot_path = os.path.join(self.screenshot_dir, "step_001_before.png")
            if os.path.exists(shot_path):
                initial_screenshot = shot_path

        # 构造断言（用于复现检测）
        assertion = self._build_assertion_from_state_diff(initial_state, final_state)

        # 获取 seed
        seed = 0
        if self.play_engine:
            try:
                seed_val = self.play_engine.sdk_get_seed()
                seed = int(seed_val or 0)
            except Exception:
                pass

        bug_dir = self.recorder.capture(
            title=title,
            anomaly_type=anomaly_type,
            severity=severity,
            description=description
            or f"测试目标: {goal}\n在第 {step} 步发现异常，连续 {self._l2_streak} 次路径修正失败",
            conclusion=conclusion,
            seed=seed,
            initial_state=initial_state,
            final_state=final_state,
            action_trace=action_trace,
            assertion=assertion,
            screenshot_initial=initial_screenshot,
            screenshot_failure=failure_screenshot,
            evidence=evidence,
            tags=tags or ["auto_detected", f"l2_streak_{self._l2_streak}"],
            consecutive_failures=self._l2_streak,
            source=source
            or {
                "channel": "strategy_stuck",
                "origin_step": step,
                "l2_streak": self._l2_streak,
                "goal": goal,
                "detected_by": "react_loop.l3_strategy",
            },
            started_at=getattr(self, "_run_started_at", ""),
        )

        entry = {
            "bug_id": self.recorder._bugs[-1].bug_id if self.recorder._bugs else "",
            "title": title,
            "anomaly_type": anomaly_type,
            "severity": severity,
            "conclusion": conclusion,
            "bug_dir": bug_dir,
            "step": step,
        }
        self._bugs_found.append(entry)
        self._remember_bug(entry, final_state or initial_state)

    def _maybe_record_perf(
        self,
        *,
        goal: str,
        initial_state: dict,
        final_state: dict,
        step: int,
    ) -> None:
        """跑完后采样性能指标，超标则记一条 performance 缺陷。

        采样不足（无头节流 / 页面未渲染）时静默跳过，不产生假缺陷。
        """
        engine = self.play_engine
        if engine is None or not getattr(engine, "perf", False):
            return
        try:
            sample = engine.perf_sample()
        except Exception:
            return
        if sample.get("insufficient_data") or not sample.get("violations"):
            return

        from playranger.core.engines.perf import build_bug_fields

        fields = build_bug_fields(sample)
        self._record_bug(
            goal=goal,
            initial_state=initial_state,
            final_state=final_state,
            step=step,
            anomaly_type="performance",
            severity="minor",
            title=fields["title"],
            conclusion="auto_warning",
            evidence=fields["evidence"],
            description=fields["description"],
            source={
                "channel": "perf_sample",
                "origin_step": step,
                "goal": goal,
                "detected_by": "play_engine.perf",
                "sample": sample,
            },
            tags=fields["tags"],
        )

    def _remember_bug(self, entry: dict, state: dict) -> None:
        """把一个缺陷写入长期记忆（失败不中断测试）。"""
        if self.memory is None:
            return
        try:
            self.memory.remember_bug(
                entry,
                state=state,
                action_sequence=[s.to_dict() for s in self._history],
            )
            self.memory.save()
        except Exception:
            pass

    # ─── 辅助函数 ────────────────────────────────────────────

    def _build_history_for_planner(self) -> list[dict]:
        """把 StepInfo 历史转成 Planner 能看懂的精简格式。"""
        result = []
        for s in self._history[-10:]:
            result.append({
                "step": s.step,
                "action": s.action,
                "result": "success" if s.execution_ok and s.verification.get("success") else "failed",
                "reason": s.verification.get("reason", ""),
            })
        return result

    @staticmethod
    def _plan_to_dict(plan: PlanOutput) -> dict:
        """PlanOutput 转 dict。"""
        return {
            "thought": plan.thought,
            "sub_goal": plan.sub_goal,
            "action": plan.action.model_dump(),
            "expected_after": plan.expected_after,
            "confidence": plan.confidence,
        }

    @staticmethod
    def _check_goal_reached(goal: str, state: dict) -> bool:
        """简单的目标达成检查（启发式）。

        真正的目标达成验证应该由上层用断言系统做。
        这里只处理一些明显的终态。
        """
        # 如果状态里有 victory=True，通常算达成
        if state.get("victory"):
            return True
        # game_over 也算一种"结束"（但不一定是达成目标）
        return False

    @staticmethod
    def _compute_conclusion(status: str, l3_count: int) -> str:
        """根据运行状态计算最终结论等级。"""
        if status == "completed":
            return "human_required"  # 正常完成，不需要自动结论
        if status == "cancelled":
            return "human_required"  # 人工停止，不下结论
        if status == "l3_exhausted" and l3_count >= 2:
            return "auto_confirmed"  # 反复尝试都失败，高度疑似 bug
        if l3_count >= 1:
            return "auto_warning"  # 有异常但不确定
        return "human_required"

    @staticmethod
    def _build_assertion_from_state_diff(initial: dict, final: dict) -> dict:
        """从状态差异构造一个断言（用于复现检测）。

        找变化最显著的字段做断言。
        """
        # 简单策略：如果 hp 变了，断言 hp 值
        init_hp = initial.get("hp")
        final_hp = final.get("hp")
        if init_hp is not None and final_hp is not None and init_hp != final_hp:
            return {"type": "state_equals", "field": "hp", "value": final_hp}

        # 如果 scene 变了，断言 scene 值
        init_scene = initial.get("scene")
        final_scene = final.get("scene")
        if init_scene != final_scene and final_scene:
            return {"type": "state_equals", "field": "scene", "value": final_scene}

        # 默认用 score
        final_score = final.get("score", 0)
        return {"type": "state_equals", "field": "score", "value": final_score}


# ─── 便捷函数 ───────────────────────────────────────────────────

def react_loop(
    goal: str,
    max_steps: int = 50,
    max_retries: int = 2,
    play_engine: PlayEngine | None = None,
    planner: Planner | None = None,
    vision_engine: VisionEngine | None = None,
    recorder: BugRecorder | None = None,
) -> dict:
    """便捷函数 —— 创建 ReActLoop 并运行。

    兼容旧接口（返回 dict）。
    """
    loop = ReActLoop(
        play_engine=play_engine,
        planner=planner,
        vision_engine=vision_engine,
        recorder=recorder,
        max_l1_retries=max_retries,
    )
    result = loop.run(goal=goal, max_steps=max_steps)
    return result.to_dict()
