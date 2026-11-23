"""动作语义校验器 —— P0 级安全闸门。

设计原则（来自 Codex 方案交叉验证）：
- 格式合法 ≠ 动作合法
- LLM 输出的动作必须过两道关：Pydantic 格式校验（在 planner 里做） + 语义校验（这里做）
- 任何层都不能修改验收规则

语义校验三层：
1. 动作白名单检查 — action_type 必须在当前 available_actions 里
2. 状态前置条件检查 — 当前状态是否允许执行这个动作
3. 参数范围检查 — 参数值是否在合法范围内

失败后返回结构化错误，供 L1 修正使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from playranger.core.brain.planner import GameAction


# ── 校验结果 ────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """动作校验结果。"""

    ok: bool
    reason: str = ""
    failed_layer: str = ""  # whitelist / precondition / params
    # 建议的修正方向（给 L1 修参考）
    suggestion: str = ""

    @classmethod
    def pass_(cls) -> "ValidationResult":
        return cls(ok=True)

    @classmethod
    def fail(
        cls,
        layer: str,
        reason: str,
        suggestion: str = "",
    ) -> "ValidationResult":
        return cls(
            ok=False,
            reason=reason,
            failed_layer=layer,
            suggestion=suggestion,
        )


# ── 前置条件函数类型 ─────────────────────────────────────────


PreconditionFn = Callable[[dict[str, Any]], tuple[bool, str]]
ParamValidatorFn = Callable[[dict[str, Any]], tuple[bool, str]]


# ── 校验器主体 ──────────────────────────────────────────────


class ActionValidator:
    """动作语义校验器。

    三层校验：
    1. whitelist — 动作名在白名单里
    2. precondition — 当前状态允许执行
    3. params — 参数在合法范围内

    用法：
        validator = ActionValidator()
        validator.set_available_actions(["click", "keypress", "wait"])
        validator.add_precondition("click", lambda s: not s.get("paused"))
        result = validator.validate(action, state)
    """

    def __init__(self) -> None:
        self._available_actions: list[str] = []
        # 每个动作的前置条件列表
        self._preconditions: dict[str, list[PreconditionFn]] = {}
        # 每个动作的参数校验函数列表
        self._param_validators: dict[str, list[ParamValidatorFn]] = {}
        # 通用前置条件（所有动作都要过）
        self._global_preconditions: list[PreconditionFn] = []

    # ── 配置方法 ─────────────────────────────────────────

    def set_available_actions(self, actions: list[str]) -> None:
        """设置当前可用动作白名单。"""
        self._available_actions = list(actions)

    def add_precondition(
        self,
        action_type: str,
        fn: PreconditionFn,
    ) -> None:
        """给某个动作加前置条件。"""
        self._preconditions.setdefault(action_type, []).append(fn)

    def add_global_precondition(self, fn: PreconditionFn) -> None:
        """加全局前置条件（所有动作都要过）。"""
        self._global_preconditions.append(fn)

    def add_param_validator(
        self,
        action_type: str,
        fn: ParamValidatorFn,
    ) -> None:
        """给某个动作加参数校验。"""
        self._param_validators.setdefault(action_type, []).append(fn)

    # ── 校验方法 ─────────────────────────────────────────

    def validate(
        self,
        action: GameAction,
        state: dict[str, Any],
    ) -> ValidationResult:
        """校验一个动作是否合法。

        Returns:
            ValidationResult — ok=True 表示通过，ok=False 带原因和建议
        """
        # Layer 1: 白名单
        whitelist_result = self._check_whitelist(action)
        if not whitelist_result.ok:
            return whitelist_result

        # Layer 2: 全局前置条件
        for precond in self._global_preconditions:
            ok, reason = precond(state)
            if not ok:
                return ValidationResult.fail(
                    layer="precondition",
                    reason=f"全局前置条件不满足: {reason}",
                    suggestion="等待状态变化或选择其他动作",
                )

        # Layer 2: 动作专属前置条件
        for precond in self._preconditions.get(action.action_type, []):
            ok, reason = precond(state)
            if not ok:
                return ValidationResult.fail(
                    layer="precondition",
                    reason=f"动作 {action.action_type} 前置条件不满足: {reason}",
                    suggestion="换一个当前状态允许的动作",
                )

        # Layer 3: 参数校验
        for validator in self._param_validators.get(action.action_type, []):
            ok, reason = validator(action.params)
            if not ok:
                return ValidationResult.fail(
                    layer="params",
                    reason=f"参数不合法: {reason}",
                    suggestion="调整参数值",
                )

        return ValidationResult.pass_()

    # ── 内部方法 ─────────────────────────────────────────

    def _check_whitelist(self, action: GameAction) -> ValidationResult:
        if not action.action_type:
            return ValidationResult.fail(
                layer="whitelist",
                reason="action_type 为空",
                suggestion="设置一个有效的 action_type",
            )

        if not self._available_actions:
            # 白名单为空 = 不限制（开发期/盲测模式）
            return ValidationResult.pass_()

        if action.action_type not in self._available_actions:
            return ValidationResult.fail(
                layer="whitelist",
                reason=(
                    f"动作 '{action.action_type}' 不在白名单里。"
                    f"可用: {self._available_actions}"
                ),
                suggestion=(
                    f"从 {self._available_actions} 中选一个"
                ),
            )

        return ValidationResult.pass_()


# ── 常用校验函数工厂 ─────────────────────────────────────────


def require_state_flag(flag: str, expected: bool = True) -> PreconditionFn:
    """工厂函数：要求 state[flag] == expected 才能执行。"""

    def _check(state: dict[str, Any]) -> tuple[bool, str]:
        actual = state.get(flag)
        if actual == expected:
            return True, ""
        return False, f"状态 {flag} = {actual}, 期望 {expected}"

    return _check


def require_param_range(
    param_name: str,
    min_val: float | None = None,
    max_val: float | None = None,
) -> ParamValidatorFn:
    """工厂函数：要求参数在指定范围内。"""

    def _check(params: dict[str, Any]) -> tuple[bool, str]:
        val = params.get(param_name)
        if val is None:
            return True, ""  # 没有这个参数不管，让业务层处理
        if not isinstance(val, (int, float)):
            return False, f"参数 {param_name} 不是数字: {type(val)}"
        if min_val is not None and val < min_val:
            return False, f"参数 {param_name} = {val} < 最小值 {min_val}"
        if max_val is not None and val > max_val:
            return False, f"参数 {param_name} = {val} > 最大值 {max_val}"
        return True, ""

    return _check


def require_param_in(param_name: str, allowed: list[Any]) -> ParamValidatorFn:
    """工厂函数：要求参数值在允许列表里。"""

    def _check(params: dict[str, Any]) -> tuple[bool, str]:
        val = params.get(param_name)
        if val is None:
            return True, ""
        if val not in allowed:
            return False, f"参数 {param_name} = {val} 不在 {allowed} 里"
        return True, ""

    return _check
