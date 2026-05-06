"""组织树管理 API (Issue #14 Day 4).

供 admin-dashboard.html 的"组织管理"tab 用. 全部走 require_org_admin.

端点:
  POST   /admin/api/orgs                     创建 org
  GET    /admin/api/orgs                     列表 (树形, 含成员数 / 子节点数)
  PATCH  /admin/api/orgs/{org_id}            更新 (name / parent_id / letta_block_id)
  DELETE /admin/api/orgs/{org_id}            删除 (硬限制: 无成员 / 无子节点 / 无 project_orgs 引用)
  GET    /admin/api/orgs/{org_id}/members    列成员
  POST   /admin/api/orgs/{org_id}/members    加成员
  DELETE /admin/api/orgs/{org_id}/members/{user_id}  移除成员

  GET    /admin/api/projects/{pid}/orgs                      project 挂的 orgs
  POST   /admin/api/projects/{pid}/orgs                      project 挂新 org (带 access_level)
  DELETE /admin/api/projects/{pid}/orgs/{org_id}             解绑

设计: 所有写操作都调 invalidate_cache() 清掉 org_tree.permission cache, 防陈旧.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field

from auth import require_org_admin
from db import use_db_async
from org_tree import (
    ROOT_ORG_CODE,
    invalidate_cache,
    set_org_letta_block,
)


router = APIRouter(prefix="/admin/api")


# ----------- payload models -----------

_ALLOWED_ORG_TYPES = {"bureau", "department", "division"}
_ALLOWED_ACCESS = {"owner", "shared_write", "shared_read"}
_VALID_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")  # lowercase + digit + hyphen


class CreateOrgIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    code: str = Field(..., min_length=2, max_length=64)
    parent_id: Optional[str] = None
    org_type: str = "department"


class PatchOrgIn(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[str] = None  # None 不变, 显式 "" 设为 NULL (移到顶层)
    letta_block_id: Optional[str] = None  # 同 parent_id 语义


class AddMemberIn(BaseModel):
    user_id: str
    role: str = "member"  # admin / member


class AttachProjectOrgIn(BaseModel):
    org_id: str
    access_level: str = "shared_read"


# ----------- helpers -----------

async def _exists_async(table: str, key: str, value: str) -> bool:
    async with use_db_async() as db:
        async with db.execute(
            f"SELECT 1 FROM {table} WHERE {key} = ? LIMIT 1", (value,)
        ) as cur:
            return await cur.fetchone() is not None


async def _check_no_cycle(org_id: str, new_parent_id: str) -> bool:
    """parent 链向上走, 不应回到 org_id 自身."""
    cur_id = new_parent_id
    seen = set()
    while cur_id:
        if cur_id == org_id:
            return False
        if cur_id in seen:
            return False
        seen.add(cur_id)
        async with use_db_async() as db:
            async with db.execute(
                "SELECT parent_id FROM organizations WHERE id = ?", (cur_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return False
        cur_id = row["parent_id"]
    return True


# ----------- /admin/api/orgs -----------

@router.post("/orgs")
async def create_org(payload: CreateOrgIn, user=Depends(require_org_admin)):
    if payload.org_type not in _ALLOWED_ORG_TYPES:
        raise HTTPException(400, f"org_type must be one of {sorted(_ALLOWED_ORG_TYPES)}")
    if not _VALID_CODE.match(payload.code):
        raise HTTPException(400, "code must be lowercase letters/digits/hyphens, 2-64 chars")
    if payload.code == ROOT_ORG_CODE:
        raise HTTPException(400, f"code '{ROOT_ORG_CODE}' is reserved for root org")
    if payload.parent_id and not await _exists_async("organizations", "id", payload.parent_id):
        raise HTTPException(400, f"parent_id '{payload.parent_id}' not found")

    org_id = "org-" + uuid.uuid4().hex[:12]
    try:
        async with use_db_async() as db:
            await db.execute(
                "INSERT INTO organizations (id, parent_id, name, code, org_type) VALUES (?, ?, ?, ?, ?)",
                (org_id, payload.parent_id, payload.name.strip(), payload.code, payload.org_type),
            )
    except Exception as e:
        if "UNIQUE" in str(e) or "unique" in str(e):
            raise HTTPException(409, f"code '{payload.code}' already exists")
        raise
    invalidate_cache()
    return {"id": org_id, "code": payload.code, "name": payload.name}


@router.get("/orgs")
async def list_orgs(user=Depends(require_org_admin)):
    """返扁平列表 + 每个节点 member_count / child_count / project_count, 前端自己组树."""
    async with use_db_async() as db:
        async with db.execute(
            """
            SELECT o.id, o.parent_id, o.name, o.code, o.org_type, o.letta_block_id,
                   (SELECT COUNT(*) FROM org_members om WHERE om.org_id = o.id) AS member_count,
                   (SELECT COUNT(*) FROM organizations c WHERE c.parent_id = o.id) AS child_count,
                   (SELECT COUNT(*) FROM project_orgs po WHERE po.org_id = o.id) AS project_count
              FROM organizations o
             ORDER BY o.parent_id NULLS FIRST, o.name
            """
        ) as cur:
            rows = await cur.fetchall()
    return {"orgs": [dict(r) for r in rows]}


@router.patch("/orgs/{org_id}")
async def patch_org(org_id: str, payload: PatchOrgIn, user=Depends(require_org_admin)):
    if not await _exists_async("organizations", "id", org_id):
        raise HTTPException(404, "org not found")
    sets = []
    args = []
    if payload.name is not None:
        sets.append("name = ?"); args.append(payload.name.strip())
    if payload.parent_id is not None:
        new_parent = payload.parent_id or None
        if new_parent:
            if new_parent == org_id:
                raise HTTPException(400, "cannot self-parent")
            if not await _exists_async("organizations", "id", new_parent):
                raise HTTPException(400, "parent_id not found")
            if not await _check_no_cycle(org_id, new_parent):
                raise HTTPException(400, "parent change would create cycle")
        sets.append("parent_id = ?"); args.append(new_parent)
    if payload.letta_block_id is not None:
        sets.append("letta_block_id = ?"); args.append(payload.letta_block_id or None)
    if not sets:
        return {"updated": False}
    args.append(org_id)
    async with use_db_async() as db:
        await db.execute(f"UPDATE organizations SET {', '.join(sets)} WHERE id = ?", args)
    invalidate_cache()
    return {"updated": True}


@router.delete("/orgs/{org_id}")
async def delete_org(org_id: str, user=Depends(require_org_admin)):
    if not await _exists_async("organizations", "id", org_id):
        raise HTTPException(404, "org not found")
    # root org 不允许删
    async with use_db_async() as db:
        async with db.execute("SELECT code FROM organizations WHERE id = ?", (org_id,)) as cur:
            row = await cur.fetchone()
        if row and row["code"] == ROOT_ORG_CODE:
            raise HTTPException(400, "cannot delete root org")
        # 硬限制: 无子节点 / 无成员 / 无 project_orgs
        async with db.execute("SELECT COUNT(*) AS c FROM organizations WHERE parent_id = ?", (org_id,)) as cur:
            if (await cur.fetchone())["c"] > 0:
                raise HTTPException(400, "has child orgs; remove or re-parent them first")
        async with db.execute("SELECT COUNT(*) AS c FROM org_members WHERE org_id = ?", (org_id,)) as cur:
            if (await cur.fetchone())["c"] > 0:
                raise HTTPException(400, "has members; remove them first")
        async with db.execute("SELECT COUNT(*) AS c FROM project_orgs WHERE org_id = ?", (org_id,)) as cur:
            if (await cur.fetchone())["c"] > 0:
                raise HTTPException(400, "has project bindings; detach projects first")
        await db.execute("DELETE FROM organizations WHERE id = ?", (org_id,))
    invalidate_cache()
    return {"deleted": org_id}


# ----------- /admin/api/orgs/{org_id}/members -----------

@router.get("/orgs/{org_id}/members")
async def list_org_members(org_id: str, user=Depends(require_org_admin)):
    async with use_db_async() as db:
        async with db.execute(
            """
            SELECT om.user_id, om.role, uc.name, uc.email
              FROM org_members om
              LEFT JOIN user_cache uc ON uc.user_id = om.user_id
             WHERE om.org_id = ?
             ORDER BY uc.name, om.user_id
            """,
            (org_id,),
        ) as cur:
            rows = await cur.fetchall()
    return {"members": [dict(r) for r in rows]}


@router.post("/orgs/{org_id}/members")
async def add_org_member(org_id: str, payload: AddMemberIn, user=Depends(require_org_admin)):
    if payload.role not in {"admin", "member"}:
        raise HTTPException(400, "role must be admin or member")
    if not await _exists_async("organizations", "id", org_id):
        raise HTTPException(404, "org not found")
    if not await _exists_async("user_cache", "user_id", payload.user_id):
        raise HTTPException(400, "user not found in user_cache")
    async with use_db_async() as db:
        await db.execute(
            "INSERT OR REPLACE INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (org_id, payload.user_id, payload.role),
        )
    invalidate_cache(payload.user_id)
    return {"ok": True}


@router.delete("/orgs/{org_id}/members/{user_id}")
async def remove_org_member(org_id: str, user_id: str, user=Depends(require_org_admin)):
    async with use_db_async() as db:
        cur = await db.execute(
            "DELETE FROM org_members WHERE org_id = ? AND user_id = ?", (org_id, user_id)
        )
        deleted = cur.rowcount
    invalidate_cache(user_id)
    if deleted == 0:
        raise HTTPException(404, "not a member")
    return {"removed": True}


@router.get("/orgs/{org_id}/projects")
async def list_org_projects(org_id: str, user=Depends(require_org_admin)):
    """反向: 列 org 关联的 project. 仅本节点直接挂的, 不递归子节点
    (递归留 V2 — 当前 admin UI 一次只看一个 org)."""
    async with use_db_async() as db:
        async with db.execute(
            """
            SELECT po.project_id, po.access_level, p.name
              FROM project_orgs po
              JOIN projects p ON p.project_id = po.project_id
             WHERE po.org_id = ?
             ORDER BY p.name
            """,
            (org_id,),
        ) as cur:
            rows = await cur.fetchall()
    return {"projects": [dict(r) for r in rows]}


# ----------- /admin/api/projects/{pid}/orgs -----------

@router.get("/projects/{project_id}/orgs")
async def list_project_orgs(project_id: str, user=Depends(require_org_admin)):
    async with use_db_async() as db:
        async with db.execute(
            """
            SELECT po.org_id, po.access_level, o.name, o.code, o.org_type
              FROM project_orgs po
              JOIN organizations o ON o.id = po.org_id
             WHERE po.project_id = ?
             ORDER BY o.name
            """,
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()
    return {"orgs": [dict(r) for r in rows]}


@router.post("/projects/{project_id}/orgs")
async def attach_project_org(project_id: str, payload: AttachProjectOrgIn, user=Depends(require_org_admin)):
    if payload.access_level not in _ALLOWED_ACCESS:
        raise HTTPException(400, f"access_level must be one of {sorted(_ALLOWED_ACCESS)}")
    if not await _exists_async("projects", "project_id", project_id):
        raise HTTPException(404, "project not found")
    if not await _exists_async("organizations", "id", payload.org_id):
        raise HTTPException(400, "org not found")
    async with use_db_async() as db:
        await db.execute(
            "INSERT OR REPLACE INTO project_orgs (project_id, org_id, access_level) VALUES (?, ?, ?)",
            (project_id, payload.org_id, payload.access_level),
        )
    invalidate_cache()
    return {"ok": True}


@router.delete("/projects/{project_id}/orgs/{org_id}")
async def detach_project_org(project_id: str, org_id: str, user=Depends(require_org_admin)):
    async with use_db_async() as db:
        cur = await db.execute(
            "DELETE FROM project_orgs WHERE project_id = ? AND org_id = ?",
            (project_id, org_id),
        )
        deleted = cur.rowcount
    invalidate_cache()
    if deleted == 0:
        raise HTTPException(404, "binding not found")
    return {"removed": True}
