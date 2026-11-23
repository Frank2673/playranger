"""验证新功能：缺陷分级 / 截图证据链 / Agent 记忆系统 / 溯源与修复方案 / 可视化报告 / API 与 SDK 字段透出。"""

import os
import shutil
import tempfile

print("=" * 60)
print("1. 缺陷分级自动判定")
print("=" * 60)

from playranger.core.agent.triage import triage, summarize_bugs, severity_score

cases = [
    ("crash_page", {}, 0.0, 0, "应为 blocker"),
    ("hp_not_reset", {"hp_not_reset": "sdk_state"}, 0.9, 0, "应为 major + auto_confirmed"),
    ("score_mismatch", {"score_mismatch": "vision_vlm", "sdk": "sdk_state"}, 0.0, 0, "应为 major + human_required"),
    ("text_overflow", {"text_overflow": "vision_vlm"}, 0.5, 0, "应为 minor"),
    ("strategy_stuck", {}, 0.0, 5, "连续失败 5 次应上调"),
]
for atype, ev, rate, fails, note in cases:
    r = triage(atype, evidence=ev, reproduction_rate=rate, consecutive_failures=fails, vision_confidence=0.8)
    print(f"  {atype:20s} -> severity={r.severity:8s} conclusion={r.conclusion:16s} conf={r.confidence:.2f}  ({note})")
    print(f"      理由: {r.reasons[0]}")

stats = summarize_bugs([
    {"severity": "blocker", "conclusion": "auto_confirmed"},
    {"severity": "major", "conclusion": "human_required"},
])
print(f"  聚合: {stats}")

assert triage("crash_page").severity == "blocker"
assert triage("hp_not_reset", evidence={"a": "sdk_state"}, reproduction_rate=0.9).conclusion == "auto_confirmed"
assert severity_score("blocker") > severity_score("minor")
print("  ✅ 分级判定通过")

print()
print("=" * 60)
print("2. 截图证据链")
print("=" * 60)

from playranger.core.agent.evidence import EvidenceChain

tmp = tempfile.mkdtemp(prefix="pr_evidence_")
try:
    bug_dir = os.path.join(tmp, "BUG-001")
    shots = os.path.join(bug_dir, "screenshots")
    os.makedirs(shots)

    # 造两张假截图
    with open(os.path.join(shots, "initial.png"), "wb") as f:
        f.write(b"fake-png-initial" * 100)
    with open(os.path.join(shots, "failure.png"), "wb") as f:
        f.write(b"fake-png-failure" * 100)

    chain = EvidenceChain(bug_dir)
    chain.add("screenshots/initial.png", kind="initial")
    chain.add("screenshots/failure.png", kind="failure", step=12, action="click")
    path = chain.save()
    print(f"  证据链已写入: {os.path.basename(path)}")

    ok = EvidenceChain.verify(bug_dir)
    print(f"  校验：ok={ok.ok}, checked={ok.checked}, msg={ok.message}")
    assert ok.ok

    # 篡改截图 → 应断链
    with open(os.path.join(shots, "failure.png"), "wb") as f:
        f.write(b"tampered" * 100)
    bad = EvidenceChain.verify(bug_dir)
    print(f"  篡改后：ok={bad.ok}, msg={bad.message}")
    assert not bad.ok
    print("  ✅ 证据链构建与篡改检测通过")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 60)
print("3. Agent 记忆系统")
print("=" * 60)

from playranger.core.agent.memory import AgentMemory

tmp = tempfile.mkdtemp(prefix="pr_memory_")
try:
    mem_path = os.path.join(tmp, "agent_memory.json")
    mem = AgentMemory(mem_path, auto_load=False)

    mem.remember_bug(
        {"bug_id": "BUG-001", "title": "重新开始后 HP 未重置",
         "anomaly_type": "hp_not_reset", "severity": "major", "description": "HP 保持旧值"},
        state={"scene": "level2", "level": 2, "hp": 40, "score": 120},
    )
    mem.remember_bug(
        {"bug_id": "BUG-002", "title": "结算分数显示错误",
         "anomaly_type": "score_mismatch", "severity": "major", "description": "显示 100，实际 300"},
        state={"scene": "victory", "level": 2, "hp": 0, "score": 300},
    )
    mem.remember_failure(
        {"scene": "level2", "level": 2, "hp": 40, "score": 120},
        "连续 3 次点击同一按钮无效",
    )
    mem.save()
    print(f"  写入 {len(mem.entries)} 条记忆 -> {os.path.basename(mem_path)}")

    # 重新加载（模拟下一次测试）
    mem2 = AgentMemory(mem_path)
    print(f"  重新加载: {len(mem2.entries)} 条")

    ctx = mem2.build_context({"scene": "level2", "level": 2, "hp": 40, "score": 120})
    print("  召回上下文:")
    for line in ctx.splitlines():
        print(f"    {line}")
    assert "HP 未重置" in ctx
    assert len(mem2.recall({"scene": "level2", "level": 2})) >= 1
    print(f"  统计: {mem2.stats()}")
    print("  ✅ 记忆写入、持久化、召回通过")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 60)
print("4. Recorder 集成（自动分级 + 证据链）")
print("=" * 60)

from playranger.core.agent.recorder import BugRecorder
import json

tmp = tempfile.mkdtemp(prefix="pr_recorder_")
try:
    shot1 = os.path.join(tmp, "s1.png")
    shot2 = os.path.join(tmp, "s2.png")
    for p in (shot1, shot2):
        with open(p, "wb") as f:
            f.write(b"pngdata" * 50)

    rec = BugRecorder(output_dir=os.path.join(tmp, "bugs"))
    bug_dir = rec.capture(
        title="重新开始后 HP 未重置",
        anomaly_type="hp_not_reset",
        severity="info",              # 未给实质判定 → 应自动升级
        conclusion="human_required",
        initial_state={"hp": 100, "scene": "level2"},
        final_state={"hp": 40, "scene": "level2"},
        action_trace=[{"step": 1, "action_type": "click"}],
        screenshot_initial=shot1,
        screenshot_failure=shot2,
        evidence={"hp_not_reset": "sdk_state"},
    )

    with open(os.path.join(bug_dir, "defect.json"), encoding="utf-8") as f:
        defect = json.load(f)
    print(f"  severity: info -> {defect['severity']}（自动升级）")
    print(f"  triage: {defect.get('triage')}")
    assert defect["severity"] == "major"
    assert "triage" in defect

    files = sorted(os.listdir(bug_dir))
    print(f"  缺陷包: {files}")
    assert "evidence_chain.json" in files

    ok = EvidenceChain.verify(bug_dir)
    print(f"  证据链校验: ok={ok.ok}, {ok.message}")
    assert ok.ok
    print("  ✅ Recorder 集成通过")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 60)
print("5. API + SDK 可观测层（分级 / 证据链字段透出）")
print("=" * 60)

import shutil as _shutil

import playranger.api.main as api
from fastapi.testclient import TestClient
from playranger.sdk.models import BugDetail as SdkBugDetail, BugSummary as SdkBugSummary

tmp = tempfile.mkdtemp(prefix="pr_api_")
try:
    shot1 = os.path.join(tmp, "s1.png")
    shot2 = os.path.join(tmp, "s2.png")
    for p in (shot1, shot2):
        with open(p, "wb") as f:
            f.write(b"pngdata" * 50)

    rec = BugRecorder(output_dir=os.path.join(tmp, "bugs"))
    bug_dir = rec.capture(
        title="重新开始后 HP 未重置",
        anomaly_type="hp_not_reset",
        severity="info",
        conclusion="human_required",
        initial_state={"hp": 100, "scene": "level2"},
        final_state={"hp": 40, "scene": "level2"},
        action_trace=[{"step": 1, "action_type": "click"}],
        screenshot_initial=shot1,
        screenshot_failure=shot2,
        evidence={"hp_not_reset": "sdk_state"},
    )

    entry = api._enrich_bug_with_artifacts({
        "bug_id": "BUG-API-001",
        "title": "重新开始后 HP 未重置",
        "anomaly_type": "hp_not_reset",
        "severity": "info",              # 未分级 → 应从 defect.json 回读 major
        "conclusion": "human_required",
        "bug_dir": bug_dir,
        "discovered_at": 1.0,
        "test_id": "t-api",
    })
    print(f"  回读分级: severity={entry['severity']}, conclusion={entry['conclusion']}")
    print(f"  triage: {entry['triage']}")
    print(f"  evidence_chain: {entry['evidence_chain']}")
    assert entry["severity"] == "major"
    assert entry["triage"]["severity"] == "major"
    assert entry["evidence_chain"]["ok"] is True

    with api._LOCK:
        api._BUGS["BUG-API-001"] = entry
        api._BUG_ORDER.append("BUG-API-001")

    client = TestClient(api.app)
    r = client.get("/api/v1/bugs/BUG-API-001")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["triage"]["severity"] == "major"
    assert detail["evidence_chain"]["ok"] is True
    print(f"  GET /bugs/{{id}}: severity={detail['severity']}, chain_ok={detail['evidence_chain']['ok']}")

    r = client.get("/api/v1/bugs")
    assert r.status_code == 200, r.text
    listed = next(b for b in r.json()["bugs"] if b["bug_id"] == "BUG-API-001")
    assert listed["evidence_chain"]["ok"] is True
    print(f"  GET /bugs: 已含 triage 与 evidence_chain")

    # SDK 同步解析
    sdk_detail = SdkBugDetail.from_dict(detail)
    print(f"  SDK BugDetail: auto_confirmed={sdk_detail.is_auto_confirmed}, "
          f"evidence_verified={sdk_detail.evidence_verified}")
    assert sdk_detail.evidence_verified
    sdk_sum = SdkBugSummary.from_dict(listed)
    assert sdk_sum.triage["severity"] == "major"
    print("  ✅ API 与 SDK 字段透出通过")
finally:
    with api._LOCK:
        api._BUGS.pop("BUG-API-001", None)
        if "BUG-API-001" in api._BUG_ORDER:
            api._BUG_ORDER.remove("BUG-API-001")
    _shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 60)
print("6. API 后台路径接入记忆（mock 模式）")
print("=" * 60)

mem = AgentMemory(os.path.join(tempfile.gettempdir(), "pr_api_memory.json"), auto_load=False)
mem.remember_failure({"scene": "menu", "level": 0, "hp": 100, "score": 0}, "菜单点击无效")

from playranger.core.brain.brain_factory import MockLLMClient
from playranger.core.brain.planner import Planner

planner = Planner(llm=MockLLMClient())
res = api._mock_test_run(
    api.StartTestRequest(game_url="demo://game", goal="找出所有缺陷", max_steps=2),
    planner=planner,
    recorder=None,
    memory=mem,
)
print(f"  mock 运行: status={res.get('status')}, steps={res.get('total_steps')}")
print(f"  记忆提示注入: {bool(mem.build_context({'scene': 'menu', 'level': 0, 'hp': 100, 'score': 0}))}")
print("  ✅ API 路径已接受并传递 AgentMemory")

print()
print("=" * 60)
print("7. Bug 溯源 + 修复深化方案 + 可视化报告")
print("=" * 60)

from playranger.core.agent.provenance import build_provenance
from playranger.core.agent.advisor import advise
from playranger.core.agent.report import render_html, build_metrics

demo_bug = {
    "bug_id": "BUG-DEMO",
    "title": "重新开始后 HP 未重置",
    "anomaly_type": "hp_not_reset",
    "severity": "major",
    "conclusion": "auto_confirmed",
    "initial_state": {"hp": 100, "scene": "menu"},
    "final_state": {"hp": 40, "scene": "level1"},
    "assertion": {"type": "state_equals", "field": "hp", "value": 100},
    "evidence": {"hp_not_reset": "sdk_state"},
    "action_trace": [
        {"step": 1, "state": {"hp": 100, "scene": "menu"},
         "action": {"action_type": "click", "target": "#start"},
         "execution_ok": True, "verification": {"success": True}},
        {"step": 2, "state": {"hp": 40, "scene": "level1"},
         "action": {"action_type": "click", "target": "#restart"},
         "execution_ok": True,
         "verification": {"success": False, "reason": "HP 未回到初始值"}},
    ],
}

prov = build_provenance(demo_bug)
plan = advise(demo_bug, prov)
print(f"  触发步骤: {prov.origin_step}  动作: {prov.origin_action.get('target')}")
print(f"  检测通道: {prov.channel_labels()}")
print(f"  状态差异: {len(prov.state_diff)} 项  断言通过: {prov.assertion_check.get('passed')}")
print(f"  修复方案: 工作量={plan.effort} 风险={plan.regression_risk} 置信度={plan.confidence:.0%}")
print(f"  根因首条: {plan.root_causes[0]}")
assert prov.origin_step == 2
assert not prov.assertion_check["passed"]
assert plan.verification_steps[0].startswith("从第 2 步")

demo_entry = {
    **demo_bug,
    "provenance": prov.to_dict(),
    "fix_plan": plan.to_dict(),
    "triage": {"confidence": plan.confidence},
}
html_text = render_html([demo_entry], title="冒烟报告")
for marker in ("缺陷总数", "修复深化方案", "触发链", "状态差异"):
    assert marker in html_text, marker
metrics = build_metrics([demo_entry])
print(f"  报告指标: 缺陷={metrics['total_bugs']} 自动确认率={metrics['auto_rate']:.0%} "
      f"平均置信度={metrics['avg_confidence']:.0%}")
print("  ✅ 溯源、修复方案与报告渲染通过")

print()
print("🎉 全部新功能验证通过")
