"""冒烟测试：验证 API capabilities 接口和 SDK。"""

from playranger.api.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

print("=== GET /health ===")
r = client.get("/health")
print(f"  status: {r.status_code}")
print(f"  body: {r.json()}")

print()
print("=== GET /api/v1/capabilities ===")
r = client.get("/api/v1/capabilities")
print(f"  status: {r.status_code}")
data = r.json()
print(f"  version: {data['version']}")
print(f"  codename: {data['codename']}")
print(f"  llm_available: {data['llm_available']}")
print(f"  vlm_available: {data['vlm_available']}")
print(f"  browser_available: {data['browser_available']}")
print(f"  bug_types: {len(data['supported_bug_types'])} 种")
print(f"  actions: {len(data['supported_actions'])} 种")
print(f"  assertions: {len(data['supported_assertions'])} 种")
print(f"  severities: {data['supported_severities']}")
print(f"  conclusions: {data['supported_conclusions']}")
print(f"  self_heal: {data['self_correction_layers']}")
print(f"  visual_detection: {data['visual_detection']}")

print()
print("=== SDK 模型测试 ===")
from playranger.sdk import (
    PlayRangerClient, PlayRanger,
    Health, Capabilities, TestInfo, StepList, StepRecord,
    BugList, BugSummary, BugDetail, ReplayResult,
)

h = Health(status="ok", version="0.1.0")
print(f"  Health: {h.status} {h.version}")

caps = Capabilities.from_dict({
    "version": "0.1.0",
    "llm_available": True,
    "vlm_available": True,
    "browser_available": True,
    "supported_bug_types": ["state_inconsistency", "ui_display_error"],
})
print(f"  Capabilities: version={caps.version}, bug_types={len(caps.supported_bug_types)}")

test = TestInfo.from_dict({
    "test_id": "abc123",
    "status": "running",
    "goal": "test",
    "game_url": "http://test",
    "started_at": 0,
    "updated_at": 0,
})
print(f"  TestInfo: {test.test_id}, running={test.is_running}, done={test.is_done}")

replay = ReplayResult.from_dict({
    "bug_id": "bug-001",
    "runs": 5,
    "reproduced_count": 4,
    "reproduction_rate": 0.8,
    "is_reproducible": True,
})
print(f"  ReplayResult: {replay.rate_pct}")

print()
print("✅ 全部测试通过")
