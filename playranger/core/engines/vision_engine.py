"""视觉引擎 —— 用 VLM 分析游戏截图异常。

封装 VLMClient，提供：
- inspect: 单图异常检测（黑屏 / 错误弹窗 / 文字溢出 / UI 错乱 / 白屏）
- compare: 前后帧对比（画面静止 / 进度倒退 / 状态异常）
- describe: 通用场景描述（给 Planner 做盲测模式的视觉输入）

所有返回值都是结构化 dict，方便后续流水线处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from playranger.core.brain.vlm_client import VLMClient


# ─── 数据结构 ───────────────────────────────────────────────────

ANOMALY_TYPES = (
    "black_screen",    # 全黑 / 接近全黑
    "white_screen",    # 全白 / 接近全白
    "error_popup",     # 错误弹窗 / 警告框
    "text_overflow",   # 文字溢出 / 重叠
    "ui_broken",       # UI 错位 / 变形
    "frozen_screen",   # 画面冻结（对比用）
    "crash_page",      # 浏览器错误页 / 404
    "other",           # 其他异常
)

SEVERITY_LEVELS = ("info", "minor", "major", "critical", "blocker")


@dataclass
class VisionResult:
    """视觉检测结果（结构化）。"""

    is_abnormal: bool = False
    anomaly_type: str = ""           # 见 ANOMALY_TYPES
    severity: str = "info"           # 见 SEVERITY_LEVELS
    description: str = ""            # 自然语言描述
    confidence: float = 0.0          # 0~1
    raw_response: dict = field(default_factory=dict)  # VLM 原始输出（调试用）

    def to_dict(self) -> dict:
        return {
            "is_abnormal": self.is_abnormal,
            "anomaly_type": self.anomaly_type,
            "severity": self.severity,
            "description": self.description,
            "confidence": self.confidence,
        }


@dataclass
class CompareResult:
    """前后帧对比结果。"""

    is_same: bool = True            # 画面是否完全一样（可能冻结）
    change_summary: str = ""         # 变化描述
    is_regression: bool = False      # 是否是进度倒退（如回到了更早的界面）
    confidence: float = 0.0
    raw_response: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "is_same": self.is_same,
            "change_summary": self.change_summary,
            "is_regression": self.is_regression,
            "confidence": self.confidence,
        }


# ─── 引擎实现 ───────────────────────────────────────────────────

_VISION_INSPECT_PROMPT = """你是一个游戏 QA 视觉质检员。请分析这张游戏截图，判断是否存在视觉异常。

异常类型：
- black_screen: 画面全黑或几乎全黑
- white_screen: 画面全白或几乎全白（加载中除外）
- error_popup: 有错误提示、警告弹窗、红色报错文字
- text_overflow: 文字溢出框外、重叠、显示不全
- ui_broken: 按钮/面板错位、变形、重叠
- crash_page: 浏览器错误页、404、连接失败
- other: 其他明显视觉问题

请严格按以下 JSON 格式输出（不要包裹 markdown）：
{
  "is_abnormal": true/false,
  "anomaly_type": "black_screen/white_screen/error_popup/text_overflow/ui_broken/crash_page/other/（空字符串表示正常）",
  "severity": "info/minor/major/critical/blocker",
  "description": "一两句话描述你看到了什么，以及为什么判定异常或正常",
  "confidence": 0.0~1.0
}
"""

_VISION_COMPARE_PROMPT = """你是一个游戏 QA 视觉质检员。请对比两张游戏截图（第一张是之前的画面，第二张是当前画面），判断画面变化情况。

请严格按以下 JSON 格式输出（不要包裹 markdown）：
{
  "is_same": true/false,
  "change_summary": "用一两句话描述两张图之间的主要变化，如果完全一样就写 'no visible change'，",
  "is_regression": true/false,
  "confidence": 0.0~1.0
}

说明：
- is_same: 两图视觉上是否完全一致（可能意味着游戏冻结/卡死）
- is_regression: 进度是否倒退（例如第二张图回到了更早的界面/更少的分数/更低的关卡）
"""

_VISION_DESCRIBE_PROMPT = """你是一个游戏场景观察者。请用简洁的语言描述这张游戏截图里你看到了什么：
1. 当前是什么场景（主菜单/关卡/结算/对话/战斗/其他）
2. 有哪些 UI 元素（按钮/文字/血条/分数/对话框 等）
3. 玩家当前能做什么

用 JSON 格式输出：
{
  "scene_type": "menu/level/dialog/battle/result/other",
  "ui_elements": ["按钮名1", "按钮名2", "..."],
  "player_options": ["可以点击开始", "可以按空格", "..."],
  "description": "1-2 句话整体描述"
}
"""


class VisionEngine:
    """视觉校验引擎 —— 封装 VLMClient，提供游戏截图的结构化分析。

    三种模式：
    1. inspect(screenshot) → 检测单张图是否有视觉异常
    2. compare(prev, curr) → 对比两帧，看是否冻结 / 倒退
    3. describe(screenshot) → 通用场景描述（给盲测模式的 Planner 用）
    """

    def __init__(self, vlm: VLMClient | None = None) -> None:
        self.vlm = vlm
        self._use_mock = vlm is None

    # ─── 公共 API ────────────────────────────────────────────

    def inspect(self, screenshot_path: str) -> VisionResult:
        """检查单张截图是否异常。

        Args:
            screenshot_path: 截图文件路径

        Returns:
            VisionResult —— 结构化检测结果
        """
        if self._use_mock or not self.vlm:
            return self._mock_inspect(screenshot_path)

        try:
            raw = self.vlm.inspect_json(screenshot_path, _VISION_INSPECT_PROMPT)
            return self._parse_inspect_result(raw)
        except Exception as e:
            # VLM 调用失败时，返回"未知"而不是报错，避免打断主流程
            return VisionResult(
                is_abnormal=False,
                anomaly_type="",
                severity="info",
                description=f"VLM 调用失败，跳过视觉检测: {e}",
                confidence=0.0,
                raw_response={"error": str(e)},
            )

    def compare(self, prev_path: str, curr_path: str) -> CompareResult:
        """前后帧对比。

        Returns:
            CompareResult —— is_same / change_summary / is_regression
        """
        if self._use_mock or not self.vlm:
            return self._mock_compare(prev_path, curr_path)

        try:
            raw = self.vlm.compare_json(prev_path, curr_path, _VISION_COMPARE_PROMPT)
            return self._parse_compare_result(raw)
        except Exception as e:
            return CompareResult(
                is_same=False,
                change_summary=f"VLM 调用失败: {e}",
                is_regression=False,
                confidence=0.0,
                raw_response={"error": str(e)},
            )

    def describe(self, screenshot_path: str) -> dict:
        """描述截图内容（盲测模式给 Planner 用）。

        Returns:
            {scene_type, ui_elements, player_options, description}
        """
        if self._use_mock or not self.vlm:
            return {
                "scene_type": "unknown",
                "ui_elements": [],
                "player_options": [],
                "description": "mock vision engine",
            }

        try:
            raw = self.vlm.inspect_json(screenshot_path, _VISION_DESCRIBE_PROMPT)
            return {
                "scene_type": raw.get("scene_type", "unknown"),
                "ui_elements": raw.get("ui_elements", []),
                "player_options": raw.get("player_options", []),
                "description": raw.get("description", ""),
            }
        except Exception as e:
            return {
                "scene_type": "error",
                "ui_elements": [],
                "player_options": [],
                "description": f"VLM 调用失败: {e}",
            }

    # ─── 解析 VLM 输出 ────────────────────────────────────────

    @staticmethod
    def _parse_inspect_result(raw: dict) -> VisionResult:
        """把 VLM 的 JSON 输出解析成 VisionResult。"""
        is_abnormal = bool(raw.get("is_abnormal", False))
        anomaly_type = str(raw.get("anomaly_type", "")).lower()
        if anomaly_type not in ANOMALY_TYPES:
            anomaly_type = "other" if is_abnormal else ""
        severity = str(raw.get("severity", "info")).lower()
        if severity not in SEVERITY_LEVELS:
            severity = "info"
        try:
            confidence = float(raw.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0

        return VisionResult(
            is_abnormal=is_abnormal,
            anomaly_type=anomaly_type if is_abnormal else "",
            severity=severity,
            description=str(raw.get("description", "")),
            confidence=confidence,
            raw_response=raw,
        )

    @staticmethod
    def _parse_compare_result(raw: dict) -> CompareResult:
        """把 VLM 的 JSON 输出解析成 CompareResult。"""
        is_same = bool(raw.get("is_same", True))
        is_regression = bool(raw.get("is_regression", False))
        try:
            confidence = float(raw.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0

        return CompareResult(
            is_same=is_same,
            change_summary=str(raw.get("change_summary", "")),
            is_regression=is_regression,
            confidence=confidence,
            raw_response=raw,
        )

    # ─── Mock 实现（无 VLM 时兜底） ─────────────────────────

    @staticmethod
    def _mock_inspect(screenshot_path: str) -> VisionResult:
        """Mock 版 —— 直接返回正常（不做检测）。"""
        return VisionResult(
            is_abnormal=False,
            anomaly_type="",
            severity="info",
            description="mock vision: 未检测到异常（无 VLM 后端）",
            confidence=0.0,
            raw_response={"mock": True},
        )

    @staticmethod
    def _mock_compare(prev_path: str, curr_path: str) -> CompareResult:
        """Mock 版 —— 假设两帧不同（不做检测）。"""
        return CompareResult(
            is_same=False,
            change_summary="mock vision: 无法对比（无 VLM 后端）",
            is_regression=False,
            confidence=0.0,
            raw_response={"mock": True},
        )
