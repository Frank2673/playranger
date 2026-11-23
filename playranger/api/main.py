"""PlayRanger API 服务 —— FastAPI 后端 v0.1。

提供以下接口：
- GET  /health                          健康检查
- POST /api/v1/tests/start              启动一次自动测试
- GET  /api/v1/tests/{test_id}          查询测试状态和结果
- GET  /api/v1/tests/{test_id}/steps    获取步骤轨迹
- GET  /api/v1/bugs                     列出已发现的 bug 列表
- GET  /api/v1/bugs/{bug_id}            查看单个 bug 详情
- POST /api/v1/bugs/{bug_id}/feedback   记录人工复核结论（误报闭环）
- POST /api/v1/replay/{bug_id}          复现指定 bug
- GET  /api/v1/capabilities             获取系统能力信息
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from playranger import __version__


# ─── Pydantic Models ────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    version: str


class StartTestRequest(BaseModel):
    """启动测试的请求体。"""
    game_url: str = Field(..., description="游戏页面 URL")
    goal: str = Field("找到所有可复现的缺陷", description="测试目标")
    max_steps: int = Field(50, ge=1, le=500, description="最大步数")
    mode: str = Field("auto", description="测试模式：auto / explore / verify")
    seed: int | None = Field(None, description="随机种子（可选）")
    project_id: str = Field("default", description="项目 ID（用于缺陷隔离与过滤）")


class TestInfoResponse(BaseModel):
    """测试状态响应。"""
    test_id: str
    status: str  # pending / running / completed / failed
    goal: str
    game_url: str
    started_at: float
    updated_at: float
    current_step: int = 0
    total_steps: int = 0
    bugs_found: int = 0
    conclusion: str = "human_required"
    success: bool = False
    error: str | None = None


class StepListResponse(BaseModel):
    """步骤列表响应。"""
    test_id: str
    total: int
    steps: list[dict]


class BugSummary(BaseModel):
    """Bug 摘要。"""
    bug_id: str
    title: str
    anomaly_type: str
    severity: str
    conclusion: str
    bug_dir: str | None = None
    discovered_at: float
    test_id: str | None = None
    triage: dict | None = None
    evidence_chain: dict | None = None
    origin_step: int | None = None
    detection_channels: list[str] = Field(default_factory=list)
    plan_confidence: float = 0.0


class BugDetailResponse(BaseModel):
    """Bug 详情。"""
    bug_id: str
    title: str
    anomaly_type: str
    severity: str
    description: str
    conclusion: str
    seed: int
    initial_state: dict
    final_state: dict
    assertion: dict
    action_trace_count: int
    action_trace: list[dict] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    bug_dir: str | None = None
    discovered_at: float
    test_id: str | None = None
    triage: dict | None = None
    evidence_chain: dict | None = None
    provenance: dict | None = None
    fix_plan: dict | None = None
    feedback: dict | None = None


class BugListResponse(BaseModel):
    """Bug 列表。"""
    total: int
    bugs: list[BugSummary]


class ReplayRequest(BaseModel):
    """复现请求。"""
    runs: int = Field(3, ge=1, le=20, description="复现次数")


class ReplayResponse(BaseModel):
    """复现结果。"""
    bug_id: str
    runs: int
    reproduced_count: int
    reproduction_rate: float
    is_reproducible: bool


class FeedbackRequest(BaseModel):
    """人工复核结论。"""
    label: str = Field(..., description="true_positive=真实缺陷 / false_positive=误报")
    severity: str | None = Field(None, description="同时校正该异常类型的严重度（可选）")
    note: str = Field("", description="复核备注")
    reviewer: str = Field("", description="复核人")


class CapabilitiesResponse(BaseModel):
    """系统能力信息。"""
    version: str
    codename: str = ""
    llm_available: bool
    vlm_available: bool
    browser_available: bool
    supported_bug_types: list[str]
    supported_actions: list[str] = []
    supported_assertions: list[str] = []
    supported_severities: list[str] = []
    supported_conclusions: list[str] = []
    self_correction_layers: list[str] = []
    max_concurrent_tests: int = 1
    visual_detection: bool = False
    frame_comparison: bool = False


# ─── 内存中的测试和 Bug 存储（v0.1 简化版，进程级） ────────

@dataclass
class TestSession:
    test_id: str
    goal: str
    game_url: str
    status: str = "pending"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    current_step: int = 0
    total_steps: int = 0
    bugs_found: int = 0
    conclusion: str = "human_required"
    success: bool = False
    error: str | None = None
    history: list[dict] = field(default_factory=list)
    bug_ids: list[str] = field(default_factory=list)
    project_id: str = "default"


_TESTS: dict[str, TestSession] = {}
_BUGS: dict[str, dict] = {}
_BUG_ORDER: list[str] = []
_LOCK = threading.Lock()
_OUTPUT_DIR = os.environ.get("PLAYRANGER_OUTPUT_DIR", "output/bugs")


# ─── App ────────────────────────────────────────────────────────

app = FastAPI(
    title="PlayRanger API",
    description="PlayRanger「试玩队」自动游戏测试 Agent —— API 服务",
    version=__version__,
)

# 控制台（账号 / 任务 / 调度 / 修复沙箱），与 /api/v1 并存
from playranger.api.console import router as console_router  # noqa: E402

app.include_router(console_router)


_DEFAULT_PROJECT = "default"


def _configured_api_key() -> str:
    """服务端要求的口令；为空表示未开启鉴权（开发模式）。"""
    return os.environ.get("PLAYRANGER_API_KEY", "").strip()


def _normalize_project(project_id: str | None) -> str:
    """项目 ID 只保留字母数字与 -_，避免路径穿越。"""
    raw = (project_id or _DEFAULT_PROJECT).strip()
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in "-_")[:40]
    return safe or _DEFAULT_PROJECT


def _bug_output_dir(project_id: str) -> str:
    """非默认项目使用独立子目录，默认项目保持原路径（兼容既有数据）。"""
    project = _normalize_project(project_id)
    if project == _DEFAULT_PROJECT:
        return _OUTPUT_DIR
    return os.path.join(_OUTPUT_DIR, "projects", project)


@app.middleware("http")
async def _api_key_guard(request: Request, call_next):
    """P2-2：设置 PLAYRANGER_API_KEY 后，/api/v1/* 必须携带 X-API-Key。"""
    expected = _configured_api_key()
    if expected and request.url.path.startswith("/api/v1"):
        provided = request.headers.get("x-api-key") or request.query_params.get("api_key") or ""
        if provided != expected:
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing X-API-Key"},
            )
    return await call_next(request)


# ─── 健康检查 ──────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """健康检查接口。"""
    return HealthResponse(status="ok", version=__version__)


@app.get("/", include_in_schema=False)
async def index():
    """根路径直接进入控制台。"""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/app/")



@app.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
async def capabilities() -> CapabilitiesResponse:
    """获取系统能力信息。"""
    from playranger.core.capabilities import detect_capabilities

    caps = detect_capabilities()

    return CapabilitiesResponse(
        version=caps.version,
        codename=caps.codename,
        llm_available=caps.llm_available,
        vlm_available=caps.vlm_available,
        browser_available=caps.browser_available,
        supported_bug_types=caps.supported_bug_types,
        supported_actions=caps.supported_actions,
        supported_assertions=caps.supported_assertions,
        supported_severities=caps.supported_severities,
        supported_conclusions=caps.supported_conclusions,
        self_correction_layers=caps.self_correction_layers,
        max_concurrent_tests=caps.max_concurrent_tests,
        visual_detection=caps.visual_detection,
        frame_comparison=caps.frame_comparison,
    )


# ─── 测试接口 ──────────────────────────────────────────────────

@app.post("/api/v1/tests/start", response_model=TestInfoResponse)
async def start_test(req: StartTestRequest) -> TestInfoResponse:
    """启动一次自动测试。"""
    test_id = str(uuid.uuid4())[:8]

    with _LOCK:
        session = TestSession(
            test_id=test_id,
            goal=req.goal,
            game_url=req.game_url,
            project_id=_normalize_project(req.project_id),
        )
        _TESTS[test_id] = session

    # 后台线程执行测试
    t = threading.Thread(
        target=_run_test_thread,
        args=(test_id, req),
        daemon=True,
    )
    t.start()

    return _session_to_response(session)


@app.get("/api/v1/tests/{test_id}", response_model=TestInfoResponse)
async def get_test(test_id: str) -> TestInfoResponse:
    """查询测试状态和结果。"""
    with _LOCK:
        session = _TESTS.get(test_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"test {test_id} not found")
    return _session_to_response(session)


@app.get("/api/v1/tests/{test_id}/steps", response_model=StepListResponse)
async def get_test_steps(test_id: str, limit: int = 100, offset: int = 0) -> StepListResponse:
    """获取测试的步骤轨迹。"""
    with _LOCK:
        session = _TESTS.get(test_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"test {test_id} not found")

    steps = session.history[offset:offset + limit]
    return StepListResponse(
        test_id=test_id,
        total=len(session.history),
        steps=steps,
    )


# ─── Bug 接口 ──────────────────────────────────────────────────

@app.get("/api/v1/bugs", response_model=BugListResponse)
async def list_bugs(limit: int = 50, offset: int = 0, project_id: str = _DEFAULT_PROJECT) -> BugListResponse:
    """列出已发现的 bug（按 project_id 隔离，默认只返回 default 项目）。"""
    project = _normalize_project(project_id)
    with _LOCK:
        ids = [
            bid for bid in _BUG_ORDER
            if _normalize_project((_BUGS.get(bid) or {}).get("project_id")) == project
        ]
        total = len(ids)
        bug_ids = ids[offset:offset + limit]
        bugs = [_BUGS[bid] for bid in bug_ids if bid in _BUGS]

    summaries = [
        BugSummary(
            bug_id=b["bug_id"],
            title=b["title"],
            anomaly_type=b["anomaly_type"],
            severity=b["severity"],
            conclusion=b["conclusion"],
            bug_dir=b.get("bug_dir"),
            discovered_at=b.get("discovered_at", 0),
            test_id=b.get("test_id"),
            triage=b.get("triage"),
            evidence_chain=b.get("evidence_chain"),
            origin_step=b.get("origin_step"),
            detection_channels=list(b.get("detection_channels", []) or []),
            plan_confidence=float(b.get("plan_confidence", 0.0) or 0.0),
        )
        for b in bugs
    ]

    return BugListResponse(
        total=total,
        bugs=summaries,
    )


@app.get("/api/v1/bugs/{bug_id}", response_model=BugDetailResponse)
async def get_bug(bug_id: str, include_trace: bool = False) -> BugDetailResponse:
    """查看单个 bug 详情。"""
    with _LOCK:
        bug = _BUGS.get(bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"bug {bug_id} not found")

    trace = bug.get("action_trace", [])
    trace_sample = trace if include_trace else trace[:10]

    return BugDetailResponse(
        bug_id=bug["bug_id"],
        title=bug["title"],
        anomaly_type=bug["anomaly_type"],
        severity=bug["severity"],
        description=bug.get("description", ""),
        conclusion=bug["conclusion"],
        seed=bug.get("seed", 0),
        initial_state=bug.get("initial_state", {}),
        final_state=bug.get("final_state", {}),
        assertion=bug.get("assertion", {}),
        action_trace_count=len(trace),
        action_trace=trace_sample,
        evidence=bug.get("evidence", {}),
        tags=bug.get("tags", []),
        bug_dir=bug.get("bug_dir"),
        discovered_at=bug.get("discovered_at", 0),
        test_id=bug.get("test_id"),
        triage=bug.get("triage"),
        evidence_chain=bug.get("evidence_chain"),
        provenance=bug.get("provenance"),
        fix_plan=bug.get("fix_plan"),
        feedback=bug.get("feedback"),
    )


@app.get("/api/v1/bugs/{bug_id}/provenance")
async def get_bug_provenance(bug_id: str) -> dict:
    """查看单个 bug 的溯源信息（触发步骤 / 动作 / 状态差异 / 证据来源）。"""
    with _LOCK:
        bug = _BUGS.get(bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"bug {bug_id} not found")
    return {"bug_id": bug_id, "provenance": bug.get("provenance") or {}}


@app.get("/api/v1/bugs/{bug_id}/fix-plan")
async def get_bug_fix_plan(bug_id: str) -> dict:
    """查看单个 bug 的修复深化方案（根因 / 修复步骤 / 验证清单）。"""
    with _LOCK:
        bug = _BUGS.get(bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"bug {bug_id} not found")
    return {"bug_id": bug_id, "fix_plan": bug.get("fix_plan") or {}}


@app.post("/api/v1/bugs/{bug_id}/feedback")
async def submit_bug_feedback(bug_id: str, req: FeedbackRequest) -> dict:
    """记录人工复核结论，并把连续误报写回项目级分级规则。"""
    from playranger.core.agent.feedback import (
        VALID_LABELS,
        register_label,
        save_feedback,
    )

    if req.label not in VALID_LABELS:
        raise HTTPException(status_code=400, detail=f"label 必须是 {VALID_LABELS} 之一")

    with _LOCK:
        bug = _BUGS.get(bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"bug {bug_id} not found")

    bug_dir = bug.get("bug_dir")
    if not bug_dir or not os.path.isdir(bug_dir):
        raise HTTPException(status_code=400, detail="缺陷包目录不存在，无法写入反馈")

    try:
        record = save_feedback(
            bug_dir,
            label=req.label,
            severity=req.severity,
            note=req.note,
            reviewer=req.reviewer,
        )
        rule = register_label(
            os.path.dirname(bug_dir),
            anomaly_type=str(bug.get("anomaly_type", "unknown")),
            label=req.label,
            severity=req.severity,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    with _LOCK:
        bug["feedback"] = record

    return {"bug_id": bug_id, "bug_dir": bug_dir, "feedback": record, "rule": rule}


@app.get("/api/v1/report", response_class=HTMLResponse)
async def get_report(test_id: str | None = None) -> HTMLResponse:
    """生成单文件离线 HTML 报告（可按 test_id 过滤）。"""
    from playranger.core.agent.report import render_html

    with _LOCK:
        bugs = list(_BUGS.values())
        sessions = list(_TESTS.values())

    history: list[dict] = []
    if test_id:
        session = next((s for s in sessions if s.test_id == test_id), None)
        if session is None:
            raise HTTPException(status_code=404, detail=f"test {test_id} not found")
        bugs = [b for b in bugs if b.get("test_id") == test_id]
        history = session.history
    else:
        # 未指定测试时，合并最近一次完成的测试轨迹
        done = [s for s in sessions if s.history]
        if done:
            latest = max(done, key=lambda s: s.updated_at)
            history = latest.history

    html_text = render_html(
        bugs,
        history=history,
        title="PlayRanger 自动试玩报告" + (f" · {test_id}" if test_id else ""),
        game_name=next((s.game_url for s in sessions if s.test_id == test_id), ""),
    )
    return HTMLResponse(content=html_text)


# ─── 复现接口 ──────────────────────────────────────────────────

@app.post("/api/v1/replay/{bug_id}", response_model=ReplayResponse)
async def replay_bug(bug_id: str, req: ReplayRequest) -> ReplayResponse:
    """复现指定 bug。"""
    with _LOCK:
        bug = _BUGS.get(bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=f"bug {bug_id} not found")

    bug_dir = bug.get("bug_dir", "")
    game_url = bug.get("game_url", "") or os.environ.get("PLAYRANGER_GAME_URL", "")

    # 如果有缺陷包目录且有轨迹文件，用真实 Replayer 复现
    trace_file = os.path.join(bug_dir, "action_trace.jsonl") if bug_dir else ""
    assertion_file = os.path.join(bug_dir, "assertion.json") if bug_dir else ""
    seed_file = os.path.join(bug_dir, "seed.json") if bug_dir else ""

    if bug_dir and os.path.exists(trace_file) and game_url:
        try:
            from playranger.core.agent.replayer import Replayer

            # 读取种子
            seed = None
            if os.path.exists(seed_file):
                with open(seed_file, "r", encoding="utf-8") as f:
                    seed_data = json.load(f)
                    seed = seed_data.get("seed")

            assertion = None
            if os.path.exists(assertion_file):
                with open(assertion_file, "r", encoding="utf-8") as f:
                    assertion = json.load(f)

            def make_engine():
                from playranger.core.engines.play_engine import PlayEngine
                return PlayEngine(url=game_url, headless=True)

            replayer = Replayer(engine_factory=make_engine)
            report = replayer.replay_from_file(
                trace_file=trace_file,
                assertion_file=assertion_file if assertion else None,
                num_runs=req.runs,
                bug_id=bug_id,
            )

            return ReplayResponse(
                bug_id=bug_id,
                runs=report.total_runs,
                reproduced_count=report.success_runs,
                reproduction_rate=report.reproduction_rate,
                is_reproducible=report.is_reproducible,
            )
        except Exception as e:
            # 真实复现失败，返回降级结果
            raise HTTPException(status_code=500, detail=f"replay failed: {str(e)}")
    else:
        # 没有足够信息，返回 mock 结果
        trace = bug.get("action_trace", [])
        has_trace = len(trace) > 0
        rate = 1.0 if has_trace else 0.0
        reproduced = req.runs if has_trace else 0

        return ReplayResponse(
            bug_id=bug_id,
            runs=req.runs,
            reproduced_count=reproduced,
            reproduction_rate=rate,
            is_reproducible=has_trace,
        )


# ─── 辅助函数 ──────────────────────────────────────────────────

def _session_to_response(s: TestSession) -> TestInfoResponse:
    return TestInfoResponse(
        test_id=s.test_id,
        status=s.status,
        goal=s.goal,
        game_url=s.game_url,
        started_at=s.started_at,
        updated_at=s.updated_at,
        current_step=s.current_step,
        total_steps=s.total_steps,
        bugs_found=s.bugs_found,
        conclusion=s.conclusion,
        success=s.success,
        error=s.error,
    )


def _check_browser_available() -> bool:
    """检查浏览器是否可用。"""
    # 检查 PLAYWRIGHT_CHROMIUM_PATH 环境变量或默认路径
    custom_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")
    if custom_path and os.path.exists(custom_path):
        return True
    # 检查默认安装位置
    default_paths = [
        os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1248\chrome-win64\chrome.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\ms-playwright\chromium-1248\chrome-win64\chrome.exe"),
    ]
    for p in default_paths:
        if os.path.exists(p):
            return True
    return False


def _enrich_bug_with_artifacts(entry: dict) -> dict:
    """从缺陷包回读分级结论与证据链校验结果，附加到 bug 记录。

    - triage: defect.json 里由规则引擎产出的分级明细
    - evidence_chain: 截图证据链的完整性校验结果
    """
    entry.setdefault("triage", None)
    entry.setdefault("evidence_chain", None)

    bug_dir = entry.get("bug_dir")
    if not bug_dir or not os.path.isdir(bug_dir):
        return entry

    # 1. 回读 defect.json：以分级后的严重度/结论为准，并带上 triage 明细
    defect_path = os.path.join(bug_dir, "defect.json")
    if os.path.exists(defect_path):
        try:
            with open(defect_path, "r", encoding="utf-8") as f:
                defect = json.load(f)
            if defect.get("severity"):
                entry["severity"] = defect["severity"]
            if defect.get("conclusion"):
                entry["conclusion"] = defect["conclusion"]
            if defect.get("triage"):
                entry["triage"] = defect["triage"]
            if defect.get("source"):
                entry.setdefault("source", defect["source"])
            if defect.get("provenance_summary"):
                entry.setdefault("origin_step", defect["provenance_summary"].get("origin_step"))
            entry.setdefault("plan_confidence", (defect.get("fix_plan_summary") or {}).get("confidence", 0.0))
        except (OSError, json.JSONDecodeError):
            pass

    # 2. 校验截图证据链（缺链/被篡改都会体现在 ok=False）
    try:
        from playranger.core.agent.evidence import EvidenceChain

        entry["evidence_chain"] = EvidenceChain.verify(bug_dir).to_dict()
    except Exception:
        pass

    # 3. 回读人工复核结论（误报闭环）
    try:
        from playranger.core.agent.feedback import load_feedback

        feedback = load_feedback(bug_dir)
        if feedback.get("label"):
            entry["feedback"] = feedback
    except Exception:
        pass

    # 4. 回读完整轨迹与状态（列表页默认不带轨迹，详情页需要）
    payload = dict(entry)
    try:
        from playranger.core.agent.recorder import load_defect

        record = load_defect(bug_dir)
        payload = record.to_dict()
        payload.update({
            "bug_id": entry.get("bug_id"),
            "bug_dir": bug_dir,
            # 严重度/结论以回读的 defect.json 为准，避免修复方案写成默认 info
            "severity": entry.get("severity") or payload.get("severity", "info"),
            "conclusion": entry.get("conclusion") or payload.get("conclusion", "human_required"),
        })
        entry.setdefault("description", payload.get("description", ""))
        entry.setdefault("seed", payload.get("seed", 0))
        entry.setdefault("evidence", payload.get("evidence", {}) or {})
        entry.setdefault("tags", payload.get("tags", []) or [])
        entry["action_trace"] = payload.get("action_trace", [])
        entry["initial_state"] = payload.get("initial_state", {})
        entry["final_state"] = payload.get("final_state", {})
        entry["assertion"] = payload.get("assertion", {})
        entry.setdefault("reproduction_rate", payload.get("reproduction_rate", 0.0))
        entry.setdefault("reproduction_attempts", payload.get("reproduction_attempts", 0))
        entry.setdefault("game_name", payload.get("game_name", ""))
        entry.setdefault("game_version", payload.get("game_version", ""))
    except Exception:
        pass

    # 4. 溯源 + 修复方案（优先读缺陷包内已生成的产物，缺失时即时分析）
    prov = _read_json_file(os.path.join(bug_dir, "provenance.json"))
    plan = _read_json_file(os.path.join(bug_dir, "fix_plan.json"))
    if not prov or not plan:
        try:
            from playranger.core.agent.intel import analyze_bug

            intel = analyze_bug(payload, bug_dir=bug_dir)
            prov = prov or intel["provenance"]
            plan = plan or intel["fix_plan"]
        except Exception:
            prov = prov or {}
            plan = plan or {}

    entry["provenance"] = prov
    entry["fix_plan"] = plan
    entry["origin_step"] = prov.get("origin_step")
    entry["detection_channels"] = list(prov.get("channels", []) or [])
    entry["plan_confidence"] = float(plan.get("confidence", 0.0) or 0.0)

    return entry


def _read_json_file(path: str) -> dict:
    """读 JSON 文件，失败返回空字典（分析产物缺失不应中断请求）。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _run_test_thread(test_id: str, req: StartTestRequest) -> None:
    """后台线程：执行测试。

    v0.1 实现：
    - 优先用真实的 PlayEngine + ReActLoop
    - 如果浏览器不可用，用 mock 模式跑一个演示流程
    """
    with _LOCK:
        session = _TESTS.get(test_id)
        if session is None:
            return
        session.status = "running"
        session.updated_at = time.time()

    try:
        result = _execute_test(session, req)
        with _LOCK:
            session.status = "completed"
            session.total_steps = result.get("total_steps", 0)
            session.current_step = session.total_steps
            session.bugs_found = len(result.get("bugs_found", []))
            session.conclusion = result.get("conclusion", "human_required")
            session.success = result.get("success", False)
            session.history = result.get("history", [])
            session.bug_ids = []
            session.updated_at = time.time()

            # 注册 bug（回读分级明细与证据链校验结果）
            for bug in result.get("bugs_found", []):
                bid = bug.get("bug_id") or bug.get("title", "unknown")
                if bid not in _BUGS:
                    _BUGS[bid] = _enrich_bug_with_artifacts({
                        **bug,
                        "bug_id": bid,
                        "discovered_at": time.time(),
                        "test_id": test_id,
                        "project_id": session.project_id,
                    })
                    _BUG_ORDER.append(bid)
                session.bug_ids.append(bid)

    except Exception as e:
        with _LOCK:
            session.status = "failed"
            session.error = str(e)
            session.updated_at = time.time()


def _execute_test(session: TestSession, req: StartTestRequest) -> dict:
    """实际执行测试的核心逻辑。

    会尝试用真实引擎，如果不可用则降级到 mock。
    """
    # 尝试创建真实组件
    engine = None
    planner = None
    vision = None
    recorder = None

    try:
        from playranger.core.brain.brain_factory import create_brain
        planner = create_brain()
    except Exception:
        planner = None

    try:
        from playranger.core.agent.recorder import BugRecorder
        bugs_dir = _bug_output_dir(session.project_id)
        os.makedirs(bugs_dir, exist_ok=True)
        recorder = BugRecorder(output_dir=bugs_dir)
    except Exception:
        recorder = None

    # 跨轮次记忆：加载失败就退化为无记忆模式，不影响测试
    memory = None
    try:
        from playranger.core.agent.memory import AgentMemory
        memory = AgentMemory()
    except Exception:
        memory = None

    # 尝试启动真实浏览器（仅当 game_url 是 http/https/file 时）
    browser_usable = False
    if req.game_url.startswith(("http://", "https://", "file://")):
        try:
            from playranger.core.engines.play_engine import PlayEngine
            engine = PlayEngine(
                url=req.game_url,
                headless=True,
            )
            browser_usable = True
        except Exception:
            engine = None

    try:
        if browser_usable and engine is not None and planner is not None:
            # 真实模式：用 ReActLoop 跑
            from playranger.core.agent.react_loop import ReActLoop
            from playranger.core.engines.action_validator import ActionValidator

            validator = ActionValidator()
            loop = ReActLoop(
                play_engine=engine,
                planner=planner,
                vision_engine=vision,
                validator=validator,
                recorder=recorder,
                memory=memory,
                max_l1_retries=2,
                max_l2_retries=3,
                max_l3_retries=2,
            )

            if req.seed is not None:
                try:
                    engine.sdk_set_seed(req.seed)
                except Exception:
                    pass

            result = loop.run(goal=req.goal, max_steps=req.max_steps)
            return result.to_dict()
        else:
            # Mock 模式：跑一个演示流程
            return _mock_test_run(req, planner, recorder, memory)
    finally:
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass


def _mock_test_run(req: StartTestRequest, planner=None, recorder=None, memory=None) -> dict:
    """Mock 模式的演示流程（用于 API 可用性验证）。"""
    from playranger.core.agent.react_loop import ReActLoop

    loop = ReActLoop(
        planner=planner,
        recorder=recorder,
        memory=memory,
        max_l1_retries=2,
        max_l2_retries=3,
        max_l3_retries=1,
    )

    result = loop.run(
        goal=req.goal,
        max_steps=min(req.max_steps, 10),
        initial_state_override={
            "scene": "menu",
            "hp": 100,
            "score": 0,
            "level": 0,
            "game_over": False,
            "victory": False,
            "available_actions": ["click", "wait", "keypress"],
        },
    )
    d = result.to_dict()
    d["note"] = "mock mode - no browser available"
    return d


# ─── 静态文件（前端控制台） ────────────────────────────────────

def _mount_static_files() -> None:
    """挂载前端静态文件。"""
    from fastapi.staticfiles import StaticFiles

    web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    if os.path.exists(web_dir):
        # 挂载到 /app 路径
        app.mount("/app", StaticFiles(directory=web_dir, html=True), name="web")


_mount_static_files()


# ─── 启动入口 ──────────────────────────────────────────────────

def main() -> None:
    """启动 API 服务的便捷入口。"""
    import uvicorn

    host = os.environ.get("PLAYRANGER_API_HOST", "0.0.0.0")
    port = int(os.environ.get("PLAYRANGER_API_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
