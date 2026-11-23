"""PlayRanger 系统能力定义。

集中管理：
- 系统版本信息
- 支持的缺陷类型
- 支持的动作类型
- 支持的断言类型
- 严重程度等级
- 结论分级
- 自修正层级说明
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ─── 版本信息 ───────────────────────────────────────────────────

VERSION = "0.1.0"
CODENAME = "试玩队"


# ─── 缺陷类型 ───────────────────────────────────────────────────

BUG_TYPE_STATE_INCONSISTENCY = "state_inconsistency"      # 状态不一致（如 HP 不重置）
BUG_TYPE_UI_DISPLAY_ERROR = "ui_display_error"            # UI 显示错误（如分数错显示）
BUG_TYPE_INTERACTION_BLOCKED = "interaction_blocked"      # 交互阻塞（如 alert 卡住）
BUG_TYPE_CRASH = "crash"                                  # 崩溃/白屏/黑屏
BUG_TYPE_VISUAL_GLITCH = "visual_glitch"                  # 视觉异常（错位/重叠/闪烁）
BUG_TYPE_LAYOUT_BREAK = "layout_break"                    # 布局错乱
BUG_TYPE_LOGIC_ERROR = "logic_error"                      # 逻辑错误（得分计算错误等）
BUG_TYPE_PERFORMANCE = "performance"                      # 性能问题（卡顿/加载慢）
BUG_TYPE_UNKNOWN = "unknown"                              # 未知类型

SUPPORTED_BUG_TYPES = [
    BUG_TYPE_STATE_INCONSISTENCY,
    BUG_TYPE_UI_DISPLAY_ERROR,
    BUG_TYPE_INTERACTION_BLOCKED,
    BUG_TYPE_CRASH,
    BUG_TYPE_VISUAL_GLITCH,
    BUG_TYPE_LAYOUT_BREAK,
    BUG_TYPE_LOGIC_ERROR,
]

BUG_TYPE_LABELS = {
    BUG_TYPE_STATE_INCONSISTENCY: "状态不一致",
    BUG_TYPE_UI_DISPLAY_ERROR: "UI 显示错误",
    BUG_TYPE_INTERACTION_BLOCKED: "交互阻塞",
    BUG_TYPE_CRASH: "崩溃",
    BUG_TYPE_VISUAL_GLITCH: "视觉异常",
    BUG_TYPE_LAYOUT_BREAK: "布局错乱",
    BUG_TYPE_LOGIC_ERROR: "逻辑错误",
    BUG_TYPE_PERFORMANCE: "性能问题",
    BUG_TYPE_UNKNOWN: "未知",
}


# ─── 严重程度 ──────────────────────────────────────────────────

SEVERITY_CRITICAL = "critical"
SEVERITY_MAJOR = "major"
SEVERITY_MINOR = "minor"
SEVERITY_INFO = "info"

SEVERITY_LEVELS = [SEVERITY_CRITICAL, SEVERITY_MAJOR, SEVERITY_MINOR, SEVERITY_INFO]

SEVERITY_LABELS = {
    SEVERITY_CRITICAL: "致命",
    SEVERITY_MAJOR: "严重",
    SEVERITY_MINOR: "一般",
    SEVERITY_INFO: "提示",
}

SEVERITY_WEIGHTS = {
    SEVERITY_CRITICAL: 100,
    SEVERITY_MAJOR: 70,
    SEVERITY_MINOR: 30,
    SEVERITY_INFO: 10,
}


# ─── 结论分级 ──────────────────────────────────────────────────

CONCLUSION_AUTO_CONFIRMED = "auto_confirmed"    # 自动确认（可复现 + 有断言）
CONCLUSION_AUTO_WARNING = "auto_warning"        # 自动预警（疑似，需人工确认）
CONCLUSION_HUMAN_REQUIRED = "human_required"    # 需人工判断

CONCLUSION_LEVELS = [
    CONCLUSION_AUTO_CONFIRMED,
    CONCLUSION_AUTO_WARNING,
    CONCLUSION_HUMAN_REQUIRED,
]

CONCLUSION_LABELS = {
    CONCLUSION_AUTO_CONFIRMED: "自动确认",
    CONCLUSION_AUTO_WARNING: "自动预警",
    CONCLUSION_HUMAN_REQUIRED: "需人工判断",
}


# ─── 支持的动作类型 ────────────────────────────────────────────

ACTION_CLICK = "click"
ACTION_WAIT = "wait"
ACTION_KEYPRESS = "keypress"
ACTION_SET_PARAM = "set_param"
ACTION_NAVIGATE = "navigate"
ACTION_SWIPE = "swipe"
ACTION_TAP = "tap"

SUPPORTED_ACTIONS = [
    ACTION_CLICK,
    ACTION_WAIT,
    ACTION_KEYPRESS,
    ACTION_SET_PARAM,
]


# ─── 断言类型 ──────────────────────────────────────────────────

ASSERT_STATE_EQUALS = "state_equals"
ASSERT_STATE_NOT_EQUALS = "state_not_equals"
ASSERT_STATE_GREATER_THAN = "state_greater_than"
ASSERT_STATE_LESS_THAN = "state_less_than"
ASSERT_STATE_CONTAINS = "state_contains"
ASSERT_VISUAL_MATCH = "visual_match"
ASSERT_EVENT_FIRED = "event_fired"

SUPPORTED_ASSERTIONS = [
    ASSERT_STATE_EQUALS,
    ASSERT_STATE_NOT_EQUALS,
    ASSERT_STATE_GREATER_THAN,
    ASSERT_STATE_LESS_THAN,
    ASSERT_STATE_CONTAINS,
    ASSERT_VISUAL_MATCH,
    ASSERT_EVENT_FIRED,
]


# ─── 自修正层级 ────────────────────────────────────────────────

LAYER_L1_ACTION = "L1"    # 动作层：ActionValidator 校验失败
LAYER_L2_PATH = "L2"      # 路径层：Verifier 验证失败
LAYER_L3_STRATEGY = "L3"  # 策略层：连续 L2 失败，触发深度反思

LAYER_DESCRIPTIONS = {
    LAYER_L1_ACTION: "动作层修正 — 动作不合法时重生成",
    LAYER_L2_PATH: "路径层修正 — 结果不对时调整路径",
    LAYER_L3_STRATEGY: "策略层修正 — 连续失败时反思策略",
}


# ─── 测试状态 ──────────────────────────────────────────────────

TEST_STATUS_PENDING = "pending"
TEST_STATUS_RUNNING = "running"
TEST_STATUS_COMPLETED = "completed"
TEST_STATUS_FAILED = "failed"
TEST_STATUS_CANCELLED = "cancelled"

TEST_STATUSES = [
    TEST_STATUS_PENDING,
    TEST_STATUS_RUNNING,
    TEST_STATUS_COMPLETED,
    TEST_STATUS_FAILED,
    TEST_STATUS_CANCELLED,
]


# ─── 系统能力聚合 ──────────────────────────────────────────────

@dataclass
class SystemCapabilities:
    """系统能力汇总。"""
    version: str = VERSION
    codename: str = CODENAME

    # 引擎可用性
    llm_available: bool = False
    vlm_available: bool = False
    browser_available: bool = False

    # 支持的能力
    supported_bug_types: list[str] = field(default_factory=lambda: list(SUPPORTED_BUG_TYPES))
    supported_actions: list[str] = field(default_factory=lambda: list(SUPPORTED_ACTIONS))
    supported_assertions: list[str] = field(default_factory=lambda: list(SUPPORTED_ASSERTIONS))
    supported_severities: list[str] = field(default_factory=lambda: list(SEVERITY_LEVELS))
    supported_conclusions: list[str] = field(default_factory=lambda: list(CONCLUSION_LEVELS))

    # 自修正
    self_correction_layers: list[str] = field(default_factory=lambda: [LAYER_L1_ACTION, LAYER_L2_PATH, LAYER_L3_STRATEGY])

    # 并发
    max_concurrent_tests: int = 1

    # 视觉能力
    visual_detection: bool = False   # 是否支持视觉异常检测
    frame_comparison: bool = False   # 是否支持前后帧对比

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "version": self.version,
            "codename": self.codename,
            "llm_available": self.llm_available,
            "vlm_available": self.vlm_available,
            "browser_available": self.browser_available,
            "supported_bug_types": self.supported_bug_types,
            "supported_actions": self.supported_actions,
            "supported_assertions": self.supported_assertions,
            "supported_severities": self.supported_severities,
            "supported_conclusions": self.supported_conclusions,
            "self_correction_layers": self.self_correction_layers,
            "max_concurrent_tests": self.max_concurrent_tests,
            "visual_detection": self.visual_detection,
            "frame_comparison": self.frame_comparison,
        }


def get_default_capabilities() -> SystemCapabilities:
    """获取默认能力配置（全部标记为可用的理论最大值）。"""
    return SystemCapabilities(
        llm_available=True,
        vlm_available=True,
        browser_available=True,
        visual_detection=True,
        frame_comparison=True,
    )


def detect_capabilities() -> SystemCapabilities:
    """实际检测当前环境的系统能力。

    会检查 LLM/VLM/浏览器等是否真的可用。
    """
    caps = SystemCapabilities()

    import os
    from dotenv import load_dotenv
    load_dotenv()

    # LLM 检测
    llm_base = os.environ.get("LLM_API_BASE", "")
    llm_key = os.environ.get("LLM_API_KEY", "")
    llm_model = os.environ.get("LLM_MODEL", "")
    if llm_base and llm_key and llm_model and llm_key != "sk-placeholder":
        try:
            from playranger.core.brain.llm_client import LLMClient
            client = LLMClient(
                base_url=llm_base,
                api_key=llm_key,
                model=llm_model,
                timeout=5,
            )
            ok, _ = client.health_check()
            caps.llm_available = ok
            client.close()
        except Exception:
            caps.llm_available = False
    else:
        caps.llm_available = False

    # VLM 检测
    vlm_base = os.environ.get("VLM_API_BASE", "") or llm_base
    vlm_key = os.environ.get("VLM_API_KEY", "") or llm_key
    vlm_model = os.environ.get("VLM_MODEL", "") or llm_model
    if vlm_base and vlm_key and vlm_model and vlm_key != "sk-placeholder":
        try:
            from playranger.core.brain.vlm_client import VLMClient
            client = VLMClient(
                base_url=vlm_base,
                api_key=vlm_key,
                model=vlm_model,
                timeout=5,
            )
            ok, _ = client.health_check()
            caps.vlm_available = ok
            caps.visual_detection = ok
            caps.frame_comparison = ok
        except Exception:
            caps.vlm_available = False
            caps.visual_detection = False
            caps.frame_comparison = False

    # 浏览器检测
    chrome_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")
    if chrome_path and os.path.exists(chrome_path):
        caps.browser_available = True
    else:
        # 尝试 playwright 自带 chromium
        try:
            import playwright
            caps.browser_available = True
        except ImportError:
            caps.browser_available = False

    return caps
