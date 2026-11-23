"""分级配置 —— 让「按演示游戏调出来的阈值」可以被项目覆盖。

加载顺序（后者覆盖前者）：
1. 内置规则（triage._RULES）
2. 项目配置文件

配置文件查找顺序：
1. 环境变量 PLAYRANGER_TRIAGE_CONFIG 指向的文件
2. 当前目录 playranger.triage.json
3. 当前目录 playranger.triage.yaml / .yml（仅当环境已装 PyYAML，否则跳过并记录原因）

只做「覆盖」，不引入新规则引擎：
- base_severity：按 anomaly_type 覆盖基线严重度
- evidence_weights：覆盖证据加权系数
- thresholds：覆盖复现率 → 结论的档位门槛
- ignore：忽略某些异常类型（仍保留记录，但降为 info / 需人工）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME_JSON = "playranger.triage.json"
CONFIG_FILENAME_YAML = ("playranger.triage.yaml", "playranger.triage.yml")

_SEVERITIES = ("info", "minor", "major", "critical", "blocker")
_DEFAULT_WEIGHTS = {
    "multi_source": 0.2,
    "sdk_state": 0.15,
    "vision": 0.25,
    "action_trace": 0.1,
}
_DEFAULT_THRESHOLDS = {
    "auto_confirmed_rate": 0.8,
    "auto_warning_rate": 0.3,
}


@dataclass
class TriageConfig:
    """项目级分级配置。"""

    base_severity: dict[str, str] = field(default_factory=dict)
    evidence_weights: dict[str, float] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)
    ignore: list[str] = field(default_factory=list)
    source: str = ""

    def weight(self, name: str) -> float:
        return float(self.evidence_weights.get(name, _DEFAULT_WEIGHTS[name]))

    def threshold(self, name: str) -> float:
        return float(self.thresholds.get(name, _DEFAULT_THRESHOLDS[name]))

    def ignored(self, anomaly_type: str) -> bool:
        return anomaly_type in self.ignore

    def severity_for(self, anomaly_type: str) -> str | None:
        value = self.base_severity.get(anomaly_type)
        return value if value in _SEVERITIES else None

    def to_dict(self) -> dict:
        return {
            "base_severity": self.base_severity,
            "evidence_weights": self.evidence_weights,
            "thresholds": self.thresholds,
            "ignore": self.ignore,
            "source": self.source,
        }


def load_triage_config(
    path: str | Path | None = None,
    *,
    root: str | Path = ".",
) -> TriageConfig:
    """加载项目配置；没有配置文件时返回空配置（等价于内置规则）。"""
    candidate = _resolve_path(path, root)
    if candidate is None:
        return TriageConfig()

    if candidate.suffix.lower() in (".yaml", ".yml"):
        payload = _load_yaml(candidate)
    else:
        payload = _load_json(candidate)

    return parse_config(payload, source=str(candidate))


def parse_config(payload: dict, *, source: str = "") -> TriageConfig:
    """校验并规范化配置内容（未知键忽略，非法值丢弃而不是崩溃）。"""
    if not isinstance(payload, dict):
        raise ValueError("分级配置必须是 JSON/YAML 对象")

    base: dict[str, str] = {}
    for key, value in (payload.get("base_severity") or {}).items():
        if isinstance(value, str) and value in _SEVERITIES:
            base[str(key)] = value

    weights: dict[str, float] = {}
    for key, value in (payload.get("evidence_weights") or {}).items():
        if key in _DEFAULT_WEIGHTS:
            try:
                weights[str(key)] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue

    thresholds: dict[str, float] = {}
    for key, value in (payload.get("thresholds") or {}).items():
        if key in _DEFAULT_THRESHOLDS:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if 0.0 <= number <= 1.0:
                thresholds[str(key)] = number
    # 保证 confirmed 门槛不低于 warning 门槛
    if "auto_confirmed_rate" in thresholds and "auto_warning_rate" in thresholds:
        if thresholds["auto_confirmed_rate"] < thresholds["auto_warning_rate"]:
            thresholds["auto_confirmed_rate"] = thresholds["auto_warning_rate"]

    ignore = [str(x) for x in (payload.get("ignore") or []) if isinstance(x, str)]

    return TriageConfig(
        base_severity=base,
        evidence_weights=weights,
        thresholds=thresholds,
        ignore=ignore,
        source=source,
    )


def _resolve_path(path: str | Path | None, root: str | Path) -> Path | None:
    if path:
        p = Path(path)
        return p if p.exists() else None

    env_path = os.environ.get("PLAYRANGER_TRIAGE_CONFIG")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    base = Path(root)
    json_path = base / CONFIG_FILENAME_JSON
    if json_path.exists():
        return json_path

    for name in CONFIG_FILENAME_YAML:
        yaml_path = base / name
        if yaml_path.exists():
            return yaml_path
    return None


def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"无法读取分级配置 {path}: {e}") from e


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise ValueError(
            f"发现 YAML 配置 {path}，但当前环境未安装 PyYAML；"
            f"请改用 {CONFIG_FILENAME_JSON}"
        ) from e
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as e:
        raise ValueError(f"无法读取分级配置 {path}: {e}") from e
    return data or {}


__all__ = [
    "CONFIG_FILENAME_JSON",
    "CONFIG_FILENAME_YAML",
    "TriageConfig",
    "load_triage_config",
    "parse_config",
]
