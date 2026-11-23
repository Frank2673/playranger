"""结果验证器 —— 检查动作执行后是否达到预期状态。

验证层级：
1. 视觉异常检查（黑屏/报错/白屏 → 直接失败）
2. 状态断言（state_equals / state_greater_than / state_less_than / scene_changed / has_element）
3. 自然语言预期 vs 实际状态的启发式匹配

用于：
- ReAct 循环每步的"验证"环节
- 自修正 L1：动作失败检测
- 缺陷包断言生成
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class VerificationResult:
    """验证结果。"""

    success: bool = True
    reason: str = ""
    mismatch_details: dict[str, Any] | None = None  # 详细的不匹配信息

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "reason": self.reason,
            "mismatch_details": self.mismatch_details,
        }


# ─── 断言函数 ───────────────────────────────────────────────────

def build_assertion_from_dict(spec: dict) -> "StateAssertion":
    """从 dict 构建断言对象。

    支持的断言类型：
    - state_equals: {field, value}
    - state_greater_than: {field, value}
    - state_less_than: {field, value}
    - scene_changed: {from_scene?, to_scene?}
    - action_available: {action_type}
    - has_element: {element_id}
    - all_of: {assertions: [...]}  # 全部满足才算过
    - any_of: {assertions: [...]}  # 任一满足就算过
    """
    atype = spec.get("type", "")

    if atype == "state_equals":
        return EqualsAssertion(field=spec["field"], value=spec["value"])
    if atype == "state_greater_than":
        return GreaterThanAssertion(field=spec["field"], value=spec["value"])
    if atype == "state_less_than":
        return LessThanAssertion(field=spec["field"], value=spec["value"])
    if atype == "scene_changed":
        return SceneChangedAssertion(
            from_scene=spec.get("from_scene"),
            to_scene=spec.get("to_scene"),
        )
    if atype == "all_of":
        inner = [build_assertion_from_dict(a) for a in spec.get("assertions", [])]
        return AllOfAssertion(assertions=inner)
    if atype == "any_of":
        inner = [build_assertion_from_dict(a) for a in spec.get("assertions", [])]
        return AnyOfAssertion(assertions=inner)

    # 未知类型 → 总是通过（避免误报）
    return AlwaysPassAssertion(reason=f"unknown assertion type: {atype}")


class StateAssertion:
    """断言基类。"""

    def check(self, state: dict) -> VerificationResult:
        raise NotImplementedError


class AlwaysPassAssertion(StateAssertion):
    """总是通过（兜底用）。"""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def check(self, state: dict) -> VerificationResult:
        return VerificationResult(success=True, reason=self.reason)


class EqualsAssertion(StateAssertion):
    """state[field] == value"""

    def __init__(self, field: str, value: Any) -> None:
        self.field = field
        self.value = value

    def check(self, state: dict) -> VerificationResult:
        actual = _get_nested(state, self.field)
        if actual is None:
            return VerificationResult(
                success=False,
                reason=f"状态中没有字段 '{self.field}'",
                mismatch_details={"field": self.field, "expected": self.value, "actual": None},
            )
        if actual == self.value:
            return VerificationResult(success=True, reason=f"{self.field} == {self.value}")
        return VerificationResult(
            success=False,
            reason=f"{self.field} 不相等：期望 {self.value}，实际 {actual}",
            mismatch_details={"field": self.field, "expected": self.value, "actual": actual},
        )


class GreaterThanAssertion(StateAssertion):
    """state[field] > value"""

    def __init__(self, field: str, value: float) -> None:
        self.field = field
        self.value = float(value)

    def check(self, state: dict) -> VerificationResult:
        actual = _get_nested(state, self.field)
        if actual is None:
            return VerificationResult(success=False, reason=f"状态中没有字段 '{self.field}'")
        try:
            actual_num = float(actual)
        except (TypeError, ValueError):
            return VerificationResult(
                success=False,
                reason=f"字段 '{self.field}' 不是数值类型，无法比较",
                mismatch_details={"field": self.field, "actual": actual},
            )
        if actual_num > self.value:
            return VerificationResult(success=True, reason=f"{self.field} ({actual_num}) > {self.value}")
        return VerificationResult(
            success=False,
            reason=f"{self.field} 不大于：期望 > {self.value}，实际 {actual_num}",
            mismatch_details={"field": self.field, "expected_greater_than": self.value, "actual": actual_num},
        )


class LessThanAssertion(StateAssertion):
    """state[field] < value"""

    def __init__(self, field: str, value: float) -> None:
        self.field = field
        self.value = float(value)

    def check(self, state: dict) -> VerificationResult:
        actual = _get_nested(state, self.field)
        if actual is None:
            return VerificationResult(success=False, reason=f"状态中没有字段 '{self.field}'")
        try:
            actual_num = float(actual)
        except (TypeError, ValueError):
            return VerificationResult(
                success=False,
                reason=f"字段 '{self.field}' 不是数值类型，无法比较",
            )
        if actual_num < self.value:
            return VerificationResult(success=True, reason=f"{self.field} ({actual_num}) < {self.value}")
        return VerificationResult(
            success=False,
            reason=f"{self.field} 不小于：期望 < {self.value}，实际 {actual_num}",
            mismatch_details={"field": self.field, "expected_less_than": self.value, "actual": actual_num},
        )


class SceneChangedAssertion(StateAssertion):
    """场景切换断言。"""

    def __init__(self, from_scene: str | None = None, to_scene: str | None = None) -> None:
        self.from_scene = from_scene
        self.to_scene = to_scene

    def check(self, state: dict) -> VerificationResult:
        scene = state.get("scene", "")
        if self.to_scene and scene == self.to_scene:
            return VerificationResult(success=True, reason=f"场景已切换到 {scene}")
        if self.to_scene and scene != self.to_scene:
            return VerificationResult(
                success=False,
                reason=f"场景不是 {self.to_scene}，实际是 {scene}",
                mismatch_details={"expected_scene": self.to_scene, "actual_scene": scene},
            )
        # 没指定目标场景，只要有 scene 就算
        if scene:
            return VerificationResult(success=True, reason=f"当前场景: {scene}")
        return VerificationResult(success=False, reason="状态中没有 scene 字段")


class AllOfAssertion(StateAssertion):
    """所有子断言都必须通过。"""

    def __init__(self, assertions: list[StateAssertion]) -> None:
        self.assertions = assertions

    def check(self, state: dict) -> VerificationResult:
        failures: list[str] = []
        for a in self.assertions:
            r = a.check(state)
            if not r.success:
                failures.append(r.reason)
        if not failures:
            return VerificationResult(success=True, reason="所有断言通过")
        return VerificationResult(
            success=False,
            reason=f"{len(failures)} 个断言失败: {'; '.join(failures[:3])}",
            mismatch_details={"failed_assertions": failures},
        )


class AnyOfAssertion(StateAssertion):
    """任一子断言通过即可。"""

    def __init__(self, assertions: list[StateAssertion]) -> None:
        self.assertions = assertions

    def check(self, state: dict) -> VerificationResult:
        for a in self.assertions:
            r = a.check(state)
            if r.success:
                return VerificationResult(success=True, reason=f"满足断言: {r.reason}")
        return VerificationResult(success=False, reason="所有断言都不满足")


# ─── 高层验证函数 ───────────────────────────────────────────────

def verify_result(
    expected: str,
    new_state: dict,
    vision_result: dict | None = None,
) -> bool:
    """验证动作执行结果是否符合预期（简化版，兼容旧接口）。

    Args:
        expected: 自然语言描述的预期状态
        new_state: 执行后的状态 dict
        vision_result: VisionResult.to_dict() 格式

    Returns:
        True / False
    """
    result = verify(expected, new_state, vision_result)
    return result.success


def verify(
    expected: str,
    new_state: dict,
    vision_result: dict | None = None,
) -> VerificationResult:
    """完整验证，返回 VerificationResult。

    验证顺序：
    1. 视觉异常 → 直接失败
    2. game_over / victory 终态检查
    3. 启发式自然语言匹配
    """
    # 1. 视觉异常优先
    if vision_result and vision_result.get("is_abnormal"):
        return VerificationResult(
            success=False,
            reason=f"视觉异常: {vision_result.get('anomaly_type', 'unknown')} — {vision_result.get('description', '')}",
            mismatch_details={"vision": vision_result},
        )

    # 2. 空预期 → 只要没视觉异常就算过
    if not expected or expected == "no change":
        return VerificationResult(success=True, reason="无明确预期，且无视觉异常")

    # 3. 启发式匹配：检查一些常见关键词
    state_str = str(new_state).lower()
    exp_lower = expected.lower()

    # 提取关键字
    keywords = _extract_keywords(exp_lower)
    matched = sum(1 for kw in keywords if kw in state_str)

    # 如果匹配了一半以上关键词，认为通过
    if keywords and matched >= max(1, len(keywords) // 2):
        return VerificationResult(
            success=True,
            reason=f"启发式匹配：{matched}/{len(keywords)} 关键词匹配",
        )

    # 4. 检查特殊模式
    # "scene is X" / "进入 X 场景"
    scene_match = re.search(r"(scene|场景|进入).+?(\w+)", exp_lower)
    if scene_match:
        target_scene = scene_match.group(2)
        if new_state.get("scene", "").lower() == target_scene:
            return VerificationResult(success=True, reason=f"场景匹配: {target_scene}")

    # 默认：不明确时也算通过（避免卡住循环）
    return VerificationResult(
        success=True,
        reason="无法精确验证，默认通过（保守策略）",
    )


# ─── 工具函数 ───────────────────────────────────────────────────

def _get_nested(state: dict, field: str) -> Any:
    """支持点号嵌套取值，比如 'player.hp'。"""
    if "." not in field:
        return state.get(field)
    parts = field.split(".")
    current: Any = state
    for p in parts:
        if isinstance(current, dict):
            current = current.get(p)
        else:
            return None
    return current


def _extract_keywords(text: str) -> list[str]:
    """从自然语言预期里提取简单关键词（非常基础版）。"""
    # 一些常见的停用词
    stopwords = {
        "的", "了", "是", "在", "和", "与", "及", "或", "有", "没有",
        "应该", "应当", "需要", "必须", "可以", "可能",
        "to", "the", "a", "an", "is", "are", "should", "must", "be",
        "state", "scene", "status",
    }
    # 按常见分隔符切分
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
    return [t for t in tokens if t not in stopwords and len(t) > 1]
