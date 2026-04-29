"""Memory trace + conflict + protection APIs (MemoryLake-inspired, /memory/v1/*)

设计参考 docs.memorylake.ai。当前提供的端点:
- GET  /projects/{pid}/memories/{mid}/trace
- GET  /projects/{pid}/conflicts                    (only_unresolved 默认 true)
- GET  /projects/{pid}/conflicts/{cid}
- POST /projects/{pid}/conflicts/{cid}/resolve     (项目 admin)
- GET  /projects/{pid}/memories/{mid}/protection
- PUT  /projects/{pid}/memories/{mid}/protection   (项目 admin)

设计要点:
- trace 返回当前 memory + 完整变更链, 每次变更附触发对话 (source_messages),
  回答 "为什么这条 memory 现在长这样"
- 冲突解决采用 4 策略人工决策:
    keep_memory     保留指定一条, 其余 forget
    trust_memory    冲突视为误报, memory 全留
    trust_document  丢冲突的 memory, 以文档为准
    dismiss         不处理 (误报标记)
- 治理动作 (resolve / set protection) 同时写 audit_log, 留追责痕迹

⚠️ Safety Memory 当前是 advisory only:protection_level 字段已存,但写路径
   (file 上传 / decision 写入) 还没强制检查。enforce 的实现见 task #8。

pseudo-project 鉴权:
- "org"            → 任意登录用户可读, ORG_ADMIN_EMAILS 可写
- "personal:<uid>" → 仅 uid 本人可读写
- 其他 (真实 project) → 走 require_project_member / require_project_admin
"""
import json
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from auth import (
    require_project_member,
    require_project_admin,
    extract_user_from_admin,
)
from config import ORG_ADMIN_EMAILS
from db import use_db_async

router = APIRouter(prefix="/memory/v1")


# ---------- pseudo-project aware auth ----------
# project_id 有 3 种来源:
#   - 真实 project (project_members 有行) → 走 require_project_*
#   - "org"            → 任意登录用户可读, ORG_ADMIN_EMAILS 可写
#   - "personal:<uid>" → 仅 uid 本人可读写
# 写动作(resolve / set protection)总是更严格

async def auth_project_read(request: Request, project_id: str) -> dict:
    """读权限: 项目成员 / org 任意登录 / personal 仅本人。"""
    user = await extract_user_from_admin(request)
    if project_id == "org":
        return user
    if project_id.startswith("personal:"):
        owner = project_id.split(":", 1)[1]
        if user["id"] != owner:
            raise HTTPException(403, "personal scope 仅文件所有者可访问")
        return user
    return await require_project_member(request, project_id)


async def auth_project_write(request: Request, project_id: str) -> dict:
    """写权限: 项目 admin / ORG_ADMIN_EMAILS / personal 仅本人。"""
    user = await extract_user_from_admin(request)
    if project_id == "org":
        if user.get("email") not in ORG_ADMIN_EMAILS:
            raise HTTPException(403, "org scope 写动作需要 ORG_ADMIN_EMAILS 权限")
        return user
    if project_id.startswith("personal:"):
        owner = project_id.split(":", 1)[1]
        if user["id"] != owner:
            raise HTTPException(403, "personal scope 写动作仅文件所有者可执行")
        return user
    return await require_project_admin(request, project_id)


# ---------- helpers ----------

async def _record_memory_event_async(
    db,
    *,
    memory_id: str,
    project_id: str,
    event_type: str,         # ADD / UPDATE / DELETE
    new_memory: str,
    event_id: str = "",
    source_messages: Optional[list] = None,
    actor_user_id: str = "",
) -> int:
    """写一条 memory 变更记录(在传入的 async db 连接里, 共用调用方 transaction)。

    sync 版在 memory_helpers.record_memory_event,带幂等检查;
    async 版本在 transaction 里走原 INSERT,依赖 uq_mh_memory_event 部分索引兜底。
    """
    if event_type not in ("ADD", "UPDATE", "DELETE"):
        raise ValueError(f"invalid event_type: {event_type}")
    msgs_json = json.dumps(source_messages or [], ensure_ascii=False)
    cur = await db.execute(
        """INSERT INTO memory_history
           (memory_id, project_id, event_type, new_memory, event_id, source_messages, actor_user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (memory_id, project_id, event_type, new_memory, event_id, msgs_json, actor_user_id),
    )
    return cur.lastrowid


async def _write_audit(db, *, user_id: str, action: str, scope: str, details: dict) -> None:
    """治理动作写 audit_log,共用调用方 transaction。"""
    await db.execute(
        "INSERT INTO audit_log (user_id, action, scope, details) VALUES (?, ?, ?, ?)",
        (user_id, action, scope, json.dumps(details, ensure_ascii=False)),
    )


# ---------- response models ----------

class TraceEntry(BaseModel):
    history_id: int
    event_type: str
    new_memory: str
    expired: bool
    changed_at: str
    event_id: str
    source_messages: list = Field(default_factory=list)
    actor_user_id: str = ""


class TraceResponse(BaseModel):
    memory_id: str
    current_memory: Optional[str]
    trace: List[TraceEntry]


class ConflictSummary(BaseModel):
    conflict_id: int
    project_id: str
    memory_ids: list
    detected_at: str
    detection_reason: str
    resolved_at: Optional[str]
    strategy: str = ""


class ResolveBody(BaseModel):
    strategy: str = Field(..., description="keep_memory|trust_memory|trust_document|dismiss")
    keep_memory_id: Optional[str] = None


# ---------- trace ----------

@router.get("/projects/{project_id}/memories/{memory_id}/trace", response_model=TraceResponse)
async def get_memory_trace(project_id: str, memory_id: str, request: Request):
    await auth_project_read(request, project_id)

    async with use_db_async() as db:
        async with db.execute(
            """SELECT history_id, event_type, new_memory, expired, event_id,
                      source_messages, actor_user_id, changed_at
               FROM memory_history
               WHERE project_id = ? AND memory_id = ?
               ORDER BY changed_at ASC, history_id ASC""",
            (project_id, memory_id),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="memory_id not found in this project")

    entries = [
        TraceEntry(
            history_id=r["history_id"],
            event_type=r["event_type"],
            new_memory=r["new_memory"],
            expired=bool(r["expired"]),
            changed_at=str(r["changed_at"]),
            event_id=r["event_id"] or "",
            source_messages=json.loads(r["source_messages"] or "[]"),
            actor_user_id=r["actor_user_id"] or "",
        )
        for r in rows
    ]

    # 当前值 = 最新一条非 DELETE 事件的 new_memory;若最后一条是 DELETE,current_memory=None
    current = None
    for e in reversed(entries):
        if e.event_type == "DELETE":
            break
        if not e.expired:
            current = e.new_memory
            break

    return TraceResponse(memory_id=memory_id, current_memory=current, trace=entries)


# ---------- conflicts ----------

@router.get("/projects/{project_id}/conflicts")
async def list_conflicts(project_id: str, request: Request, only_unresolved: bool = True):
    await auth_project_read(request, project_id)

    sql = """SELECT conflict_id, project_id, memory_ids, detected_at, detection_reason,
                    resolved_at, strategy
             FROM memory_conflicts
             WHERE project_id = ?"""
    params = [project_id]
    if only_unresolved:
        sql += " AND resolved_at IS NULL"
    sql += " ORDER BY detected_at DESC"

    async with use_db_async() as db:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()

    return {
        "data": [
            {
                "conflict_id": r["conflict_id"],
                "project_id": r["project_id"],
                "memory_ids": json.loads(r["memory_ids"] or "[]"),
                "detected_at": str(r["detected_at"]),
                "detection_reason": r["detection_reason"] or "",
                "resolved_at": str(r["resolved_at"]) if r["resolved_at"] else None,
                "strategy": r["strategy"] or "",
            }
            for r in rows
        ]
    }


@router.get("/projects/{project_id}/conflicts/{conflict_id}")
async def get_conflict(project_id: str, conflict_id: int, request: Request):
    await auth_project_read(request, project_id)

    async with use_db_async() as db:
        async with db.execute(
            """SELECT * FROM memory_conflicts
               WHERE project_id = ? AND conflict_id = ?""",
            (project_id, conflict_id),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="conflict not found")

    return {
        "conflict_id": row["conflict_id"],
        "project_id": row["project_id"],
        "memory_ids": json.loads(row["memory_ids"] or "[]"),
        "detected_at": str(row["detected_at"]),
        "detection_reason": row["detection_reason"] or "",
        "resolved_at": str(row["resolved_at"]) if row["resolved_at"] else None,
        "resolved_by": row["resolved_by"] or "",
        "strategy": row["strategy"] or "",
        "kept_memory_id": row["kept_memory_id"] or "",
        "forgotten_ids": json.loads(row["forgotten_ids"] or "[]"),
    }


VALID_STRATEGIES = {"keep_memory", "trust_memory", "trust_document", "dismiss"}

VALID_PROTECTION_LEVELS = {"read_only", "append_only", "mutable"}


# ---------- protection (Safety Memory) ----------

class ProtectionResponse(BaseModel):
    memory_id: str
    project_id: str
    protection_level: str
    set_by: str = ""
    set_at: Optional[str] = None
    reason: str = ""


class SetProtectionBody(BaseModel):
    protection_level: str = Field(..., description="read_only|append_only|mutable")
    reason: Optional[str] = ""


@router.get(
    "/projects/{project_id}/memories/{memory_id}/protection",
    response_model=ProtectionResponse,
)
async def get_protection(project_id: str, memory_id: str, request: Request):
    await auth_project_read(request, project_id)

    async with use_db_async() as db:
        async with db.execute(
            """SELECT memory_id, project_id, protection_level, set_by, set_at, reason
               FROM memory_protection
               WHERE project_id = ? AND memory_id = ?""",
            (project_id, memory_id),
        ) as cur:
            row = await cur.fetchone()

    # 未显式设置 → 默认 mutable,这样调用方拿到的 schema 一致
    if not row:
        return ProtectionResponse(
            memory_id=memory_id,
            project_id=project_id,
            protection_level="mutable",
        )
    return ProtectionResponse(
        memory_id=row["memory_id"],
        project_id=row["project_id"],
        protection_level=row["protection_level"],
        set_by=row["set_by"] or "",
        set_at=str(row["set_at"]) if row["set_at"] else None,
        reason=row["reason"] or "",
    )


@router.put(
    "/projects/{project_id}/memories/{memory_id}/protection",
    response_model=ProtectionResponse,
)
async def set_protection(
    project_id: str,
    memory_id: str,
    body: SetProtectionBody,
    request: Request,
):
    # 改 protection_level 是治理动作,仅项目 admin
    user = await auth_project_write(request, project_id)

    if body.protection_level not in VALID_PROTECTION_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"protection_level must be one of {sorted(VALID_PROTECTION_LEVELS)}",
        )

    actor = user.get("id", "") if isinstance(user, dict) else ""

    async with use_db_async() as db:
        await db.execute(
            """INSERT INTO memory_protection
               (memory_id, project_id, protection_level, set_by, set_at, reason)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
               ON CONFLICT(memory_id) DO UPDATE SET
                 protection_level = excluded.protection_level,
                 set_by           = excluded.set_by,
                 set_at           = CURRENT_TIMESTAMP,
                 reason           = excluded.reason""",
            (memory_id, project_id, body.protection_level, actor, body.reason or ""),
        )
        await _write_audit(
            db,
            user_id=actor,
            action="memory.protection.set",
            scope=project_id,
            details={
                "memory_id": memory_id,
                "protection_level": body.protection_level,
                "reason": body.reason or "",
            },
        )

    return await get_protection(project_id, memory_id, request)


@router.post("/projects/{project_id}/conflicts/{conflict_id}/resolve")
async def resolve_conflict(
    project_id: str,
    conflict_id: int,
    body: ResolveBody,
    request: Request,
):
    # 治理责任要明确,resolve 仅项目 admin 可操作
    user = await auth_project_write(request, project_id)

    if body.strategy not in VALID_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=f"strategy must be one of {sorted(VALID_STRATEGIES)}",
        )
    if body.strategy == "keep_memory" and not body.keep_memory_id:
        raise HTTPException(
            status_code=400,
            detail="keep_memory_id is required when strategy=keep_memory",
        )

    async with use_db_async() as db:
        async with db.execute(
            "SELECT memory_ids, resolved_at FROM memory_conflicts "
            "WHERE project_id = ? AND conflict_id = ?",
            (project_id, conflict_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="conflict not found")
        if row["resolved_at"]:
            raise HTTPException(status_code=409, detail="conflict already resolved")

        memory_ids = json.loads(row["memory_ids"] or "[]")
        forgotten: list[str] = []
        if body.strategy == "keep_memory":
            forgotten = [m for m in memory_ids if m != body.keep_memory_id]
        elif body.strategy == "trust_document":
            forgotten = list(memory_ids)
        # trust_memory / dismiss → forgotten 空

        actor = user.get("id", "") if isinstance(user, dict) else ""
        await db.execute(
            """UPDATE memory_conflicts
               SET resolved_at = CURRENT_TIMESTAMP,
                   resolved_by = ?,
                   strategy = ?,
                   kept_memory_id = ?,
                   forgotten_ids = ?
               WHERE conflict_id = ?""",
            (
                actor,
                body.strategy,
                body.keep_memory_id or "",
                json.dumps(forgotten, ensure_ascii=False),
                conflict_id,
            ),
        )

        # 给被 forget 的 memory 写一条 DELETE history (可由 chat 流程后续真正同步到 Letta)
        for mid in forgotten:
            await _record_memory_event_async(
                db,
                memory_id=mid,
                project_id=project_id,
                event_type="DELETE",
                new_memory="",
                event_id=f"conflict_resolve:{conflict_id}",
                actor_user_id=actor,
            )

        await _write_audit(
            db,
            user_id=actor,
            action="memory.conflict.resolve",
            scope=project_id,
            details={
                "conflict_id": conflict_id,
                "strategy": body.strategy,
                "kept_memory_id": body.keep_memory_id or "",
                "forgotten_memory_ids": forgotten,
            },
        )

    return {
        "conflict_id": conflict_id,
        "resolved": True,
        "strategy": body.strategy,
        "forgotten_memory_ids": forgotten,
    }
