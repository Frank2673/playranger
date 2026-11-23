"""动作规划器 —— LLM 读状态 → 输出下一步动作（结构化 pydantic）。

Day 2 实现：
- plan / replan / reflect 三个核心方法
- 强制 pydantic 输出，保证动作格式可控
- 支持注入历史反思，实现跨轮策略调整
- 内置系统 prompt 模板，约束 Agent 行为
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from playranger.core.brain.llm_client import LLMClient, LLMError


# ── 输出 schema（pydantic 模型，自动转 JSON Schema）─────────────────


class GameAction(BaseModel):
    """单个动作的结构化表示。"""

    action_type: str = Field(description="动作类型，必须在 available_actions 白名单内")
    target: str | None = Field(default=None, description="目标选择器，DOM id 或坐标")
    params: dict[str, Any] = Field(default_factory=dict, description="动作参数")
    reason: str = Field(description="为什么选这个动作，一句话")


class PlanOutput(BaseModel):
    """plan() 的输出结构。"""

    thought: str = Field(description="简短思考，一两句话")
    sub_goal: str | None = Field(default=None, description="当前子目标（如果有）")
    action: GameAction
    expected_after: str = Field(description="执行后期望看到的状态变化")
    confidence: float = Field(ge=0.0, le=1.0, description="对这个动作的信心 0-1")


class ReplanOutput(BaseModel):
    """replan() 的输出结构。"""

    thought: str = Field(description="失败原因分析")
    new_sub_goal: str | None = Field(default=None, description="调整后的子目标")
    new_approach: str = Field(description="新的策略方向")
    action: GameAction
    expected_after: str


class ReflectionOutput(BaseModel):
    """reflect() 的输出结构。"""

    failure_root_cause: str = Field(description="根因分析")
    strategy_adjustment: str = Field(description="下一轮的策略调整方向")
    should_retry: bool = Field(description="是否值得再试一轮")
    next_attempt_focus: str = Field(description="下一轮重点关注什么")


# ── 系统 prompt ─────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个游戏测试 Agent，名叫 PlayRanger。
你的任务是像真人玩家一样探索游戏、发现异常、留下证据。

行为准则：
1. 只根据当前可见状态做决策，不假设你看不到的信息。
2. 每一步选一个动作，执行后观察结果，再决定下一步。
3. 如果连续几步没有进展，考虑换路或改变策略。
4. 你是在测试游戏，不是在"通关比赛"——探索异常和验证规则也是目标。
5. 严格从 available_actions 里选动作，不要发明新动作。
6. 输出必须是合法 JSON，完全符合给定 schema。"""


class Planner:
    """游戏测试 Agent 的决策大脑。

    三个核心能力：
    - plan: 根据当前状态 + 目标，输出下一步动作
    - replan: 动作失败/路径不通，重新规划
    - reflect: 整轮失败后，反思原因并调整策略

    Day 2 实现：真实 LLM 调用 + pydantic 强制输出。
    """

    def __init__(
        self,
        llm: LLMClient,
        vlm: Any = None,
        system_prompt: str | None = None,
    ) -> None:
        self.llm = llm
        self.vlm = vlm
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.reflection_log: list[str] = []
        # 统计信息，方便调试
        self.call_count = 0
        self.retry_count = 0

    # ── 公共 API ────────────────────────────────────────────

    def plan(
        self,
        state: dict[str, Any],
        goal: str,
        available_actions: list[str] | None = None,
        history: list[dict[str, Any]] | None = None,
        step: int = 0,
        max_steps: int = 100,
        memory_hint: str = "",
    ) -> PlanOutput:
        """输出下一步动作。

        Args:
            state: 当前游戏状态（结构化）
            goal: 总体测试目标
            available_actions: 当前可执行动作白名单
            history: 历史动作-状态序列（最近 N 条）
            step: 当前步数
            max_steps: 总步数上限
            memory_hint: 长期记忆提示（来自 AgentMemory，可为空）

        Returns:
            PlanOutput — 包含动作、思考、预期结果
        """
        user_msg = self._build_plan_message(
            state=state,
            goal=goal,
            available_actions=available_actions or [],
            history=history or [],
            step=step,
            max_steps=max_steps,
            memory_hint=memory_hint,
        )

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_msg},
        ]

        self.call_count += 1
        try:
            result = self.llm.chat_pydantic(messages, PlanOutput)
            return result  # type: ignore[return-value]
        except LLMError as e:
            # LLM 彻底失败时，返回一个安全的 fallback（空动作 + 标记）
            self.retry_count += 1
            return PlanOutput(
                thought=f"LLM 调用失败: {e}",
                action=GameAction(
                    action_type="noop",
                    reason="llm_error_fallback",
                ),
                expected_after="no change",
                confidence=0.0,
            )

    def replan(
        self,
        state: dict[str, Any],
        goal: str,
        failure_reason: str,
        available_actions: list[str] | None = None,
        failed_attempts: int = 1,
    ) -> ReplanOutput:
        """失败后重新规划，返回新的 plan（可能包含子目标调整）。

        这是 L2 路径层修正的核心。
        """
        user_msg = f"""你刚才尝试了一个动作但失败了。

失败原因: {failure_reason}
失败次数（当前动作/路径）: {failed_attempts}

当前状态:
```json
{json.dumps(state, ensure_ascii=False, indent=2)}
```

总体目标: {goal}

可用动作: {json.dumps(available_actions or [], ensure_ascii=False)}

请分析失败原因，换一个思路或调整子目标，给出下一步动作。
不要重复刚才失败的做法。"""

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_msg},
        ]

        self.call_count += 1
        try:
            result = self.llm.chat_pydantic(messages, ReplanOutput)
            return result  # type: ignore[return-value]
        except LLMError as e:
            self.retry_count += 1
            return ReplanOutput(
                thought=f"LLM 调用失败: {e}",
                new_approach="fallback_noop",
                action=GameAction(
                    action_type="noop",
                    reason="llm_error_fallback",
                ),
                expected_after="no change",
            )

    def reflect(
        self,
        goal: str,
        action_seq: list[dict[str, Any]],
        failure_reason: str,
        evidence: str | None = None,
    ) -> ReflectionOutput:
        """整轮失败后的策略反思，返回反思结果。

        这是 L3 策略层修正的核心。反思结果会注入下一轮 plan 的上下文。
        """
        recent_actions = action_seq[-20:] if len(action_seq) > 20 else action_seq
        actions_text = json.dumps(recent_actions, ensure_ascii=False, indent=2)

        evidence_text = f"\n额外证据:\n{evidence}" if evidence else ""

        user_msg = f"""这一轮测试失败了，请做策略反思。

总体目标: {goal}
失败原因: {failure_reason}
{evidence_text}

最近 {len(recent_actions)} 步的动作轨迹:
```json
{actions_text}
```

请分析：
1. 根本原因是什么？
2. 下一轮应该怎么调整策略？
3. 还值不值得再试一轮？
4. 下一轮的重点应该放在哪？"""

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_msg},
        ]

        self.call_count += 1
        try:
            result = self.llm.chat_pydantic(messages, ReflectionOutput)
            # 记录到反思日志
            reflection_text = (
                f"根因: {result.failure_root_cause}\n"
                f"调整: {result.strategy_adjustment}\n"
                f"重点: {result.next_attempt_focus}"
            )
            self.reflection_log.append(reflection_text)
            return result  # type: ignore[return-value]
        except LLMError as e:
            self.retry_count += 1
            return ReflectionOutput(
                failure_root_cause=f"unknown (llm error: {e})",
                strategy_adjustment="retry_same_approach",
                should_retry=False,
                next_attempt_focus="unknown",
            )

    def set_context(self, previous_reflections: list[str]) -> None:
        """注入历史反思，用于下一轮重试。"""
        self.reflection_log = list(previous_reflections)

    # ── 内部方法 ────────────────────────────────────────────

    @staticmethod
    def _build_plan_message(
        state: dict[str, Any],
        goal: str,
        available_actions: list[str],
        history: list[dict[str, Any]],
        step: int,
        max_steps: int,
        memory_hint: str = "",
    ) -> str:
        """构造 plan 的 user 消息。"""
        # 控制历史长度，避免 token 爆炸
        max_history = 10
        recent_history = history[-max_history:] if len(history) > max_history else history

        history_text = ""
        if recent_history:
            history_items = []
            for i, h in enumerate(recent_history):
                action_desc = h.get("action", {}).get("action_type", "?")
                result = h.get("result", "?")
                history_items.append(f"  step {h.get('step', i)}: {action_desc} → {result}")
            history_text = "\n".join(history_items)
            history_text = f"\n\n历史动作（最近 {len(recent_history)} 步）:\n{history_text}"

        memory_text = f"\n\n{memory_hint}" if memory_hint else ""

        state_text = json.dumps(state, ensure_ascii=False, indent=2)
        actions_text = json.dumps(available_actions, ensure_ascii=False)

        return f"""当前步数: {step} / {max_steps}
总体目标: {goal}
{memory_text}

当前游戏状态:
```json
{state_text}
```

可用动作白名单（必须从这里选，不能发明新动作）:
```json
{actions_text}
```
{history_text}

请选下一步动作。记住：
1. 只能从可用动作里选
2. 每步一个动作
3. 如果不确定，选最安全的探索动作
"""
