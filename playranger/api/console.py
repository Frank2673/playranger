"""控制台 API —— 账号、系统资源、任务、事件、修复沙箱。

独立前缀 /api/console，与既有 /api/v1 兼容并存。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from playranger import __version__
from playranger.platform import auth, db, fixes, system
from playranger.platform.orchestrator import get_orchestrator
from playranger.platform.runner import normalize_game_url, task_output_dir

router = APIRouter(prefix="/api/console", tags=["console"])


# ─── 鉴权 ───────────────────────────────────────────────────────


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def require_user(authorization: str | None = Header(default=None)) -> dict:
    user = auth.authenticate(_bearer(authorization))
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或 Token 已过期")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可访问")
    return user


def _public_task(task: dict, include_result: bool = True) -> dict:
    data = db.task_to_public(task, include_result=include_result)
    owner = db.get_user(int(task["user_id"]))
    data["owner"] = owner["username"] if owner else "unknown"
    # 结果里可能包含大字段，列表页只需要摘要
    if not include_result:
        data.pop("result", None)
    return data


def _load_task(task_id: str, user: dict) -> dict:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    if user.get("role") != "admin" and int(task["user_id"]) != int(user["id"]):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return task


# ─── 请求模型 ───────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateTaskRequest(BaseModel):
    game_url: str = Field(..., description="http(s)/file URL，或本地游戏文件路径")
    goal: str = Field("找到所有可复现的缺陷", max_length=500)
    max_steps: int = Field(50, ge=1, le=500)
    seed: int | None = None
    source_root: str = Field("", description="源码根目录（可选，用于定位与修复建议）")
    project_id: str = Field("default", max_length=40)


class ScaleRequest(BaseModel):
    workers: int = Field(..., ge=1, le=32)


class FixValidateRequest(BaseModel):
    root: str = Field(..., description="源码根目录")
    patch: str = Field(..., description="unified diff 文本")
    test_command: str = ""
    timeout_s: int = Field(120, ge=5, le=600)


# ─── 账号 ───────────────────────────────────────────────────────


@router.post("/auth/register")
def register(req: RegisterRequest) -> dict:
    try:
        user, token = auth.register(req.username, req.password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    db.add_event(None, "info", f"新用户注册：{user['username']}（{user['role']}）")
    return {"token": token, "user": auth.public_user(user)}


@router.post("/auth/login")
def login(req: LoginRequest) -> dict:
    try:
        user, token = auth.login(req.username, req.password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"token": token, "user": user}


@router.post("/auth/logout")
def logout(authorization: str | None = Header(default=None)) -> dict:
    auth.logout(_bearer(authorization))
    return {"ok": True}


@router.get("/auth/me")
def me(user: dict = Depends(require_user)) -> dict:
    return {"user": auth.public_user(user)}


# ─── 系统资源 ───────────────────────────────────────────────────


@router.get("/system")
def system_info(user: dict = Depends(require_user)) -> dict:
    orch = get_orchestrator()
    orch.ensure_started()
    snap = system.snapshot()
    snap["scheduler"] = orch.state()
    snap["version"] = __version__
    snap["db_path"] = str(db.db_path())
    snap["terminal"] = "idle" if orch.state()["active"] == 0 else "busy"
    return snap


# ─── 任务 ───────────────────────────────────────────────────────


@router.get("/tasks")
def list_tasks(
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(require_user),
) -> dict:
    uid = None if user.get("role") == "admin" else int(user["id"])
    rows = db.list_tasks(uid, limit=limit)
    return {"total": len(rows), "tasks": [_public_task(r, include_result=False) for r in rows]}


@router.post("/tasks")
def create_task(req: CreateTaskRequest, user: dict = Depends(require_user)) -> dict:
    try:
        game_url = normalize_game_url(req.game_url)
        source_root = fixes.resolve_root(req.source_root) if req.source_root.strip() else ""
    except (ValueError, fixes.SandboxError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task_id = uuid.uuid4().hex[:12]
    db.create_task({
        "id": task_id,
        "user_id": int(user["id"]),
        "project_id": req.project_id or "default",
        "game_url": game_url,
        "goal": req.goal,
        "max_steps": req.max_steps,
        "seed": req.seed,
        "source_root": str(source_root) if source_root else "",
        "status": "queued",
        "output_dir": str(task_output_dir(task_id)),
        "created_at": db.now_iso(),
    })
    task = get_orchestrator().submit(task_id)
    db.add_event(task_id, "info", f"用户 {user['username']} 创建任务：{game_url}")
    return {"task": _public_task(task or db.get_task(task_id) or {})}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, user: dict = Depends(require_user)) -> dict:
    task = _load_task(task_id, user)
    return {"task": _public_task(task)}


def _read_steps(task: dict, offset: int, limit: int) -> dict:
    out_dir = Path(task.get("output_dir") or task_output_dir(task["id"]))
    path = out_dir / "steps.jsonl"
    if not path.exists():
        return {"task_id": task["id"], "total": 0, "steps": []}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    selected = lines[offset: offset + limit]
    steps: list[dict] = []
    for line in selected:
        try:
            steps.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"task_id": task["id"], "total": total, "steps": steps}


@router.get("/tasks/{task_id}/steps")
def get_steps(
    task_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    user: dict = Depends(require_user),
) -> dict:
    return _read_steps(_load_task(task_id, user), offset, limit)


def _task_bug_summaries(task: dict) -> list[dict]:
    out_dir = Path(task.get("output_dir") or task_output_dir(task["id"]))
    bug_root = out_dir / "bugs"
    if not bug_root.is_dir():
        return []
    summaries: list[dict] = []
    for child in sorted(bug_root.iterdir()):
        if not child.is_dir():
            continue
        item: dict = {"bug_id": child.name, "bug_dir": str(child)}
        defect_path = child / "defect.json"
        if defect_path.exists():
            try:
                defect = json.loads(defect_path.read_text(encoding="utf-8"))
                item.update({
                    "bug_id": defect.get("bug_id") or child.name,
                    "title": defect.get("title", ""),
                    "anomaly_type": defect.get("anomaly_type", ""),
                    "severity": defect.get("severity", ""),
                    "conclusion": defect.get("conclusion", ""),
                    "description": defect.get("description", ""),
                    "tags": defect.get("tags", []),
                })
            except (OSError, json.JSONDecodeError):
                pass
        for name, key in (("fix_plan.json", "fix_plan"), ("provenance.json", "provenance")):
            path = child / name
            if path.exists():
                try:
                    item[key] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    item[key] = {}
        summaries.append(item)
    return summaries


@router.get("/tasks/{task_id}/bugs")
def get_task_bugs(task_id: str, user: dict = Depends(require_user)) -> dict:
    task = _load_task(task_id, user)
    bugs = _task_bug_summaries(task)
    return {"task_id": task_id, "total": len(bugs), "bugs": bugs}


@router.post("/tasks/{task_id}/stop")
def stop_task(task_id: str, user: dict = Depends(require_user)) -> dict:
    _load_task(task_id, user)
    task = get_orchestrator().stop_task(task_id)
    return {"task": _public_task(task or {})}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, user: dict = Depends(require_user)) -> dict:
    task = _load_task(task_id, user)
    new_task = get_orchestrator().retry_task(task)
    return {"task": _public_task(new_task)}


@router.get("/tasks/{task_id}/artifacts/{name}")
def get_artifact(task_id: str, name: str, user: dict = Depends(require_user)):
    task = _load_task(task_id, user)
    out_dir = Path(task.get("output_dir") or task_output_dir(task["id"])).resolve()
    target = (out_dir / name).resolve()
    if out_dir != target and not target.is_relative_to(out_dir):
        raise HTTPException(status_code=400, detail="非法产物路径")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="产物不存在")
    return FileResponse(str(target))


# ─── 主控指令 ───────────────────────────────────────────────────


@router.post("/scale")
def scale(req: ScaleRequest, user: dict = Depends(require_user)) -> dict:
    state = get_orchestrator().scale(req.workers)
    return {"scheduler": state, "system": system.snapshot()}


@router.post("/pause")
def pause(user: dict = Depends(require_user)) -> dict:
    return {"scheduler": get_orchestrator().pause()}


@router.post("/resume")
def resume(user: dict = Depends(require_user)) -> dict:
    return {"scheduler": get_orchestrator().resume()}


@router.post("/stop-all")
def stop_all(user: dict = Depends(require_user)) -> dict:
    return {"scheduler": get_orchestrator().stop_all()}


# ─── 事件流 ─────────────────────────────────────────────────────


@router.get("/events")
def events(
    since: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    user: dict = Depends(require_user),
) -> dict:
    rows = db.list_events(
        since=since,
        limit=limit,
        user_id=int(user["id"]),
        is_admin=user.get("role") == "admin",
    )
    return {
        "events": rows,
        "last_id": rows[-1]["id"] if rows else since,
    }


# ─── 修复沙箱 ───────────────────────────────────────────────────


@router.post("/fixes/validate")
def validate_fix(req: FixValidateRequest, user: dict = Depends(require_user)) -> dict:
    try:
        result = fixes.validate_patch(req.root, req.patch)
    except fixes.SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/fixes/verify")
def verify_fix(req: FixValidateRequest, user: dict = Depends(require_user)) -> dict:
    try:
        result = fixes.verify_patch(
            req.root, req.patch, test_command=req.test_command, timeout_s=req.timeout_s
        )
    except fixes.SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


# ─── 管理员 ─────────────────────────────────────────────────────


@router.get("/admin/users")
def admin_users(user: dict = Depends(require_admin)) -> dict:
    users = db.list_users()
    return {"total": len(users), "users": users}


@router.get("/admin/overview")
def admin_overview(user: dict = Depends(require_admin)) -> dict:
    counts = db.task_counts()
    return {
        "users": len(db.list_users()),
        "tasks": sum(counts.values()),
        "counts": counts,
        "scheduler": get_orchestrator().state(),
    }
