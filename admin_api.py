"""知识管理后端 API（/admin/api/*）"""
import os
import logging
from fastapi import APIRouter, Request, UploadFile, HTTPException, File

import config
from config import ORG_ADMIN_EMAILS
from db import get_db
from auth import (
    extract_user_from_admin,
    require_project_member,
    require_project_admin,
    require_org_admin,
)
from routing import letta, get_or_create_org_resources, get_or_create_personal_folder
from webui_sync import grant_model_access, revoke_model_access, revoke_all_model_access, reconcile_all
from knowledge_mirror import mirror_file, unmirror_file

router = APIRouter(prefix="/admin/api")


def _file_name(file_obj) -> str:
    """兼容不同版本 Letta SDK 的文件名字段。"""
    source = getattr(file_obj, "source", None)
    if source and getattr(source, "filename", None):
        return source.filename
    return (
        getattr(file_obj, "original_file_name", None)
        or getattr(file_obj, "file_name", None)
        or ""
    )


def _file_size(file_obj) -> int:
    """兼容不同版本 Letta SDK 的文件大小字段。"""
    source = getattr(file_obj, "source", None)
    if source and getattr(source, "file_size", None) is not None:
        return source.file_size
    return getattr(file_obj, "file_size", 0) or 0


def _file_items(files_page):
    """把 Letta 的 page/list 响应统一转成可安全复用的文件列表。"""
    return list(getattr(files_page, "items", files_page))


# ===== 当前用户 =====


@router.get("/me")
async def get_me(request: Request):
    user = extract_user_from_admin(request)
    db = get_db()
    projects = db.execute(
        "SELECT p.project_id, p.name, pm.role FROM projects p "
        "JOIN project_members pm ON p.project_id = pm.project_id "
        "WHERE pm.user_id = ?",
        (user["id"],),
    ).fetchall()
    db.close()
    return {
        "id": user["id"],
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "is_org_admin": user.get("email") in ORG_ADMIN_EMAILS,
        "projects": [{"id": r["project_id"], "name": r["name"], "role": r["role"]} for r in projects],
    }


# ===== 项目管理 =====


@router.post("/projects")
async def create_project(request: Request):
    user = extract_user_from_admin(request)
    body = await request.json()
    project_id = body["id"]
    name = body["name"]
    desc = body.get("desc", "")

    block = letta.blocks.create(
        label=f"project_knowledge_{project_id}",
        value=f"【{name}】项目知识待填充...",
        limit=2000,
    )
    folder = None
    try:
        folder = letta.folders.create(name=f"proj-{project_id}", embedding_config={"embedding_model": "nomic-embed-text", "embedding_endpoint_type": "ollama", "embedding_endpoint": "http://ollama:11434", "embedding_dim": 768})

        db = get_db()
        db.execute(
            "INSERT INTO projects (project_id, name, desc, created_by, project_block_id, project_folder_id) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, name, desc, user["id"], block.id, folder.id),
        )
        db.execute(
            "INSERT INTO project_members (user_id, project_id, role, added_by) VALUES (?, ?, 'admin', ?)",
            (user["id"], project_id, user["id"]),
        )
        db.commit()
        db.close()
    except Exception:
        # DB 插入失败，清理已创建的 Letta 资源
        try:
            letta.blocks.delete(block_id=block.id)
        except Exception:
            pass
        if folder:
            try:
                letta.folders.delete(folder_id=folder.id)
            except Exception:
                pass
        raise

    # 同步 Open WebUI 模型权限
    try:
        grant_model_access(user["id"], f"letta-{project_id}")
    except Exception as e:
        logging.warning(f"sync grant failed for create_project {project_id}: {e}")

    return {"status": "ok", "project_id": project_id}


@router.get("/projects")
async def list_my_projects(request: Request):
    user = extract_user_from_admin(request)
    db = get_db()
    rows = db.execute(
        "SELECT p.project_id, p.name, p.desc, p.folder_quota_mb, p.project_folder_id, p.created_by, pm.role "
        "FROM projects p "
        "JOIN project_members pm ON p.project_id = pm.project_id "
        "WHERE pm.user_id = ?",
        (user["id"],),
    ).fetchall()
    result = []
    for r in rows:
        member_count = db.execute(
            "SELECT COUNT(*) as c FROM project_members WHERE project_id = ?",
            (r["project_id"],),
        ).fetchone()["c"]
        files = _file_items(letta.folders.files.list(folder_id=r["project_folder_id"]))
        used_mb = sum(_file_size(f) for f in files) // (1024 * 1024)
        result.append(
            {
                "id": r["project_id"],
                "name": r["name"],
                "desc": r["desc"],
                "quota_mb": r["folder_quota_mb"],
                "used_mb": used_mb,
                "members": member_count,
                "role": r["role"],
            }
        )
    db.close()
    return result


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    user = extract_user_from_admin(request)
    is_org = user.get("email") in ORG_ADMIN_EMAILS
    db = get_db()
    is_proj_admin = db.execute(
        "SELECT 1 FROM project_members WHERE user_id = ? AND project_id = ? AND role = 'admin'",
        (user["id"], project_id),
    ).fetchone()
    if not is_org and not is_proj_admin:
        db.close()
        raise HTTPException(403, "需要项目 admin 或组织 admin 权限")

    proj = db.execute(
        "SELECT project_block_id, project_folder_id FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if not proj:
        db.close()
        raise HTTPException(404, "项目不存在")

    # detach + 清理映射
    agents = db.execute(
        "SELECT user_id, agent_id FROM user_agent_map WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    for a in agents:
        try:
            letta.agents.blocks.detach(agent_id=a["agent_id"], block_id=proj["project_block_id"])
            letta.agents.folders.detach(agent_id=a["agent_id"], folder_id=proj["project_folder_id"])
        except Exception:
            pass

    db.execute("DELETE FROM user_agent_map WHERE project_id = ?", (project_id,))

    try:
        letta.blocks.delete(block_id=proj["project_block_id"])
    except Exception:
        pass
    try:
        letta.folders.delete(folder_id=proj["project_folder_id"])
    except Exception:
        pass

    db.execute("DELETE FROM project_members WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    db.commit()
    db.close()

    # 同步 Open WebUI：撤销该项目模型的所有权限
    try:
        revoke_all_model_access(f"letta-{project_id}")
    except Exception as e:
        logging.warning(f"sync revoke_all failed for delete_project {project_id}: {e}")

    return {"status": "ok"}


# ===== 项目成员 =====


@router.get("/project/{project_id}/members")
async def list_project_members(project_id: str, request: Request):
    require_project_member(request, project_id)
    db = get_db()
    creator_id = db.execute(
        "SELECT created_by FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()["created_by"]
    rows = db.execute(
        "SELECT pm.user_id, pm.role, uc.name, uc.email "
        "FROM project_members pm "
        "LEFT JOIN user_cache uc ON pm.user_id = uc.user_id "
        "WHERE pm.project_id = ?",
        (project_id,),
    ).fetchall()
    db.close()
    return [
        {
            "id": r["user_id"],
            "name": r["name"] or "",
            "email": r["email"] or "",
            "role": r["role"],
            "creator": r["user_id"] == creator_id,
        }
        for r in rows
    ]


@router.post("/project/{project_id}/members")
async def add_member(project_id: str, request: Request):
    user = require_project_admin(request, project_id)
    body = await request.json()
    new_user_id = body["user_id"]
    role = body.get("role", "member")

    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO project_members (user_id, project_id, role, added_by) VALUES (?, ?, ?, ?)",
        (new_user_id, project_id, role, user["id"]),
    )
    db.commit()
    db.close()

    # 如果该用户已有 Agent，挂载项目知识
    try:
        db2 = get_db()
        agent_row = db2.execute(
            "SELECT agent_id FROM user_agent_map WHERE user_id = ? AND project_id = ?",
            (new_user_id, project_id),
        ).fetchone()
        if agent_row:
            proj = db2.execute(
                "SELECT project_block_id, project_folder_id FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if proj:
                letta.agents.blocks.attach(agent_id=agent_row["agent_id"], block_id=proj["project_block_id"])
                letta.agents.folders.attach(agent_id=agent_row["agent_id"], folder_id=proj["project_folder_id"])
        db2.close()
    except Exception:
        pass

    # 同步 Open WebUI 模型权限
    try:
        grant_model_access(new_user_id, f"letta-{project_id}")
    except Exception as e:
        logging.warning(f"sync grant failed for add_member {new_user_id} to {project_id}: {e}")

    return {"status": "ok"}


@router.put("/project/{project_id}/members/{member_id}/role")
async def set_member_role(project_id: str, member_id: str, request: Request):
    require_project_admin(request, project_id)
    body = await request.json()
    db = get_db()
    db.execute(
        "UPDATE project_members SET role = ? WHERE user_id = ? AND project_id = ?",
        (body["role"], member_id, project_id),
    )
    db.commit()
    db.close()
    return {"status": "ok"}


@router.delete("/project/{project_id}/members/{member_id}")
async def remove_member(project_id: str, member_id: str, request: Request):
    require_project_admin(request, project_id)
    db = get_db()

    agent_row = db.execute(
        "SELECT agent_id FROM user_agent_map WHERE user_id = ? AND project_id = ?",
        (member_id, project_id),
    ).fetchone()
    if agent_row:
        proj = db.execute(
            "SELECT project_block_id, project_folder_id FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if proj:
            try:
                letta.agents.blocks.detach(agent_id=agent_row["agent_id"], block_id=proj["project_block_id"])
                letta.agents.folders.detach(agent_id=agent_row["agent_id"], folder_id=proj["project_folder_id"])
            except Exception:
                pass

    db.execute(
        "DELETE FROM project_members WHERE user_id = ? AND project_id = ?",
        (member_id, project_id),
    )
    db.commit()
    db.close()

    # 同步 Open WebUI 模型权限
    try:
        revoke_model_access(member_id, f"letta-{project_id}")
    except Exception as e:
        logging.warning(f"sync revoke failed for remove_member {member_id} from {project_id}: {e}")

    return {"status": "ok"}


# ===== 项目设置 =====


@router.get("/project/{project_id}/quota")
async def get_project_quota(project_id: str, request: Request):
    require_project_member(request, project_id)
    db = get_db()
    row = db.execute(
        "SELECT folder_quota_mb, project_folder_id FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    db.close()
    files = _file_items(letta.folders.files.list(folder_id=row["project_folder_id"]))
    used_bytes = sum(_file_size(f) for f in files)
    return {
        "quota_mb": row["folder_quota_mb"],
        "used_mb": used_bytes // (1024 * 1024),
        "file_count": len(files),
    }


@router.put("/project/{project_id}/quota")
async def update_project_quota(project_id: str, request: Request):
    require_org_admin(request)
    body = await request.json()
    db = get_db()
    db.execute(
        "UPDATE projects SET folder_quota_mb = ? WHERE project_id = ?",
        (body["folder_quota_mb"], project_id),
    )
    db.commit()
    db.close()
    return {"status": "ok", "folder_quota_mb": body["folder_quota_mb"]}


# ===== 项目知识 =====


@router.get("/project/{project_id}/knowledge")
async def get_project_knowledge(project_id: str, request: Request):
    require_project_member(request, project_id)
    db = get_db()
    row = db.execute(
        "SELECT project_block_id FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    db.close()
    block = letta.blocks.retrieve(block_id=row["project_block_id"])
    return {"content": block.value, "limit": block.limit}


@router.put("/project/{project_id}/knowledge")
async def update_project_knowledge(project_id: str, request: Request):
    require_project_admin(request, project_id)
    body = await request.json()
    db = get_db()
    row = db.execute(
        "SELECT project_block_id FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    db.close()
    letta.blocks.update(block_id=row["project_block_id"], value=body["content"])
    return {"status": "ok"}


# ===== 项目文件 =====


def _check_folder_size(folder_id: str, new_file, project_id: str = None):
    db = get_db()
    if project_id:
        row = db.execute(
            "SELECT folder_quota_mb FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        quota = (row["folder_quota_mb"] if row else config.DEFAULT_FOLDER_QUOTA_MB) * 1024 * 1024
    else:
        quota = config.DEFAULT_FOLDER_QUOTA_MB * 1024 * 1024
    db.close()

    files = _file_items(letta.folders.files.list(folder_id=folder_id))
    total_size = sum(_file_size(f) for f in files)
    new_file.file.seek(0, 2)
    new_size = new_file.file.tell()
    new_file.file.seek(0)
    if total_size + new_size > quota:
        used_mb = total_size // (1024 * 1024)
        quota_mb = quota // (1024 * 1024)
        raise HTTPException(
            413, f"文件夹已用 {used_mb}MB，超过限额 {quota_mb}MB，请先删除旧文件"
        )


@router.get("/project/{project_id}/files")
async def list_project_files(project_id: str, request: Request):
    require_project_member(request, project_id)
    db = get_db()
    row = db.execute(
        "SELECT project_folder_id FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    db.close()
    files = _file_items(letta.folders.files.list(folder_id=row["project_folder_id"]))
    return [
        {
            "id": f.id,
            "name": _file_name(f),
            "size": _file_size(f),
            "created_at": str(f.created_at),
        }
        for f in files
    ]


@router.post("/project/{project_id}/files")
async def upload_project_file(project_id: str, request: Request, file: UploadFile = File(...)):
    require_project_member(request, project_id)
    db = get_db()
    row = db.execute(
        "SELECT project_folder_id, name FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    db.close()
    _check_folder_size(row["project_folder_id"], file, project_id)
    proj_name = row["name"] if row else ""
    uploaded = letta.folders.files.upload(folder_id=row["project_folder_id"], file=(file.filename, file.file, file.content_type))
    # 镜像到 Open WebUI Knowledge
    try:
        fid = uploaded.id if hasattr(uploaded, "id") else None
        if fid:
            mirror_file(fid, row["project_folder_id"], file.filename, "project", project_id, "", proj_name)
    except Exception as e:
        logging.warning(f"mirror failed for {file.filename}: {e}")
    return {"status": "ok", "filename": file.filename}


@router.delete("/project/{project_id}/files/{file_id}")
async def delete_project_file(project_id: str, file_id: str, request: Request):
    require_project_admin(request, project_id)
    db = get_db()
    row = db.execute(
        "SELECT project_folder_id FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    db.close()
    letta.folders.files.delete(folder_id=row["project_folder_id"], file_id=file_id)
    try:
        unmirror_file(file_id)
    except Exception as e:
        logging.warning(f"unmirror failed for {file_id}: {e}")
    return {"status": "ok"}


# ===== 组织管理 =====


@router.get("/org/projects")
async def list_all_projects(request: Request):
    require_org_admin(request)
    db = get_db()
    rows = db.execute(
        "SELECT project_id, name, created_by, folder_quota_mb, project_folder_id FROM projects"
    ).fetchall()
    result = []
    for r in rows:
        member_count = db.execute(
            "SELECT COUNT(*) as c FROM project_members WHERE project_id = ?",
            (r["project_id"],),
        ).fetchone()["c"]
        files = _file_items(letta.folders.files.list(folder_id=r["project_folder_id"]))
        used_mb = sum(_file_size(f) for f in files) // (1024 * 1024)
        creator = db.execute(
            "SELECT name FROM user_cache WHERE user_id = ?", (r["created_by"],)
        ).fetchone()
        result.append(
            {
                "id": r["project_id"],
                "name": r["name"],
                "creator": creator["name"] if creator else r["created_by"],
                "members": member_count,
                "files": len(files),
                "quota_mb": r["folder_quota_mb"],
                "used_mb": used_mb,
            }
        )
    db.close()
    return result


@router.get("/org/settings")
async def get_org_settings(request: Request):
    require_org_admin(request)
    return {"default_folder_quota_mb": config.DEFAULT_FOLDER_QUOTA_MB}


@router.put("/org/settings")
async def update_org_settings(request: Request):
    require_org_admin(request)
    body = await request.json()
    config.DEFAULT_FOLDER_QUOTA_MB = body["default_folder_quota_mb"]
    return {"status": "ok", "default_folder_quota_mb": config.DEFAULT_FOLDER_QUOTA_MB}


@router.post("/reconcile")
async def manual_reconcile(request: Request):
    """手动触发全量对账（POST，会修改 Open WebUI 数据库状态）"""
    require_org_admin(request)
    reconcile_all()
    from knowledge_mirror import reconcile_mirrors
    reconcile_mirrors()
    return {"status": "ok"}


# ===== 组织知识 =====

@router.get("/org/knowledge")
async def get_org_knowledge(request: Request):
    extract_user_from_admin(request)
    resources = get_or_create_org_resources()
    block = letta.blocks.retrieve(block_id=resources["block_id"])
    return {"content": block.value, "limit": block.limit}


@router.put("/org/knowledge")
async def update_org_knowledge(request: Request):
    require_org_admin(request)
    body = await request.json()
    resources = get_or_create_org_resources()
    block = letta.blocks.update(block_id=resources["block_id"], value=body["content"])
    return {"status": "ok", "limit": block.limit}


@router.get("/org/files")
async def list_org_files(request: Request):
    extract_user_from_admin(request)
    resources = get_or_create_org_resources()
    files = _file_items(letta.folders.files.list(folder_id=resources["folder_id"]))
    return [
        {
            "id": f.id,
            "name": _file_name(f),
            "size": _file_size(f),
            "created_at": str(f.created_at),
        }
        for f in files
    ]


@router.post("/org/files")
async def upload_org_file(request: Request, file: UploadFile = File(...)):
    extract_user_from_admin(request)
    resources = get_or_create_org_resources()
    _check_folder_size(resources["folder_id"], file)
    uploaded = letta.folders.files.upload(
        folder_id=resources["folder_id"],
        file=(file.filename, file.file, file.content_type),
    )
    try:
        fid = uploaded.id if hasattr(uploaded, "id") else None
        if fid:
            mirror_file(fid, resources["folder_id"], file.filename, "org")
    except Exception as e:
        logging.warning(f"mirror failed for org file {file.filename}: {e}")
    return {"status": "ok", "filename": file.filename}


@router.delete("/org/files/{file_id}")
async def delete_org_file(file_id: str, request: Request):
    require_org_admin(request)
    resources = get_or_create_org_resources()
    letta.folders.files.delete(folder_id=resources["folder_id"], file_id=file_id)
    try:
        unmirror_file(file_id)
    except Exception as e:
        logging.warning(f"unmirror failed for org file {file_id}: {e}")
    return {"status": "ok"}


# ===== 个人文件 =====


@router.get("/personal/files")
async def list_personal_files(request: Request):
    user = extract_user_from_admin(request)
    folder_id = get_or_create_personal_folder(user["id"])
    files = _file_items(letta.folders.files.list(folder_id=folder_id))
    return [
        {
            "id": f.id,
            "name": _file_name(f),
            "size": _file_size(f),
            "created_at": str(f.created_at),
        }
        for f in files
    ]


@router.post("/personal/files")
async def upload_personal_file(request: Request, file: UploadFile = File(...)):
    user = extract_user_from_admin(request)
    folder_id = get_or_create_personal_folder(user["id"])
    _check_folder_size(folder_id, file)
    uploaded = letta.folders.files.upload(folder_id=folder_id, file=(file.filename, file.file, file.content_type))
    try:
        fid = uploaded.id if hasattr(uploaded, "id") else None
        if fid:
            mirror_file(fid, folder_id, file.filename, "personal", "", user["id"])
    except Exception as e:
        logging.warning(f"mirror failed for personal file {file.filename}: {e}")
    return {"status": "ok", "filename": file.filename}


@router.delete("/personal/files/{file_id}")
async def delete_personal_file(file_id: str, request: Request):
    user = extract_user_from_admin(request)
    folder_id = get_or_create_personal_folder(user["id"])
    letta.folders.files.delete(folder_id=folder_id, file_id=file_id)
    try:
        unmirror_file(file_id)
    except Exception as e:
        logging.warning(f"unmirror failed for personal file {file_id}: {e}")
    return {"status": "ok"}


# ===== 个人记忆（human block） =====


@router.get("/personal/memory")
async def get_personal_memory(request: Request):
    """读取用户的 human block（AI 学到的用户信息）"""
    user = extract_user_from_admin(request)
    db = get_db()
    rows = db.execute(
        "SELECT agent_id, project_id FROM user_agent_map WHERE user_id = ?",
        (user["id"],),
    ).fetchall()
    db.close()
    memories = []
    for row in rows:
        try:
            blocks = list(getattr(letta.agents.blocks.list(agent_id=row["agent_id"]), "items",
                                  letta.agents.blocks.list(agent_id=row["agent_id"])))
            for block in blocks:
                if block.label == "human":
                    memories.append({
                        "project_id": row["project_id"],
                        "agent_id": row["agent_id"],
                        "block_id": block.id,
                        "content": block.value,
                        "limit": block.limit,
                    })
        except Exception:
            pass
    return memories


@router.put("/personal/memory/{block_id}")
async def update_personal_memory(block_id: str, request: Request):
    """用户编辑自己的 human block"""
    user = extract_user_from_admin(request)
    # 校验 block 属于该用户的 agent
    db = get_db()
    agent_ids = [r["agent_id"] for r in db.execute(
        "SELECT agent_id FROM user_agent_map WHERE user_id = ?", (user["id"],)
    ).fetchall()]
    db.close()
    body = await request.json()
    for aid in agent_ids:
        try:
            blocks = list(getattr(letta.agents.blocks.list(agent_id=aid), "items",
                                  letta.agents.blocks.list(agent_id=aid)))
            for block in blocks:
                if block.id == block_id and block.label == "human":
                    letta.blocks.update(block_id=block_id, value=body["content"])
                    return {"status": "ok"}
        except Exception:
            pass
    raise HTTPException(403, "无权编辑此记忆块")


# ===== 知识建议 =====


@router.get("/project/{project_id}/suggestions")
async def list_suggestions(project_id: str, request: Request):
    """列出项目的待审批知识建议"""
    require_project_member(request, project_id)
    db = get_db()
    rows = db.execute(
        "SELECT s.id, s.content, s.user_id, s.status, s.created_at, uc.name "
        "FROM knowledge_suggestions s "
        "LEFT JOIN user_cache uc ON s.user_id = uc.user_id "
        "WHERE s.project_id = ? ORDER BY s.created_at DESC",
        (project_id,),
    ).fetchall()
    db.close()
    return [
        {
            "id": r["id"],
            "content": r["content"],
            "user_name": r["name"] or r["user_id"][:8],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@router.post("/project/{project_id}/suggestions/{suggestion_id}/approve")
async def approve_suggestion(project_id: str, suggestion_id: int, request: Request):
    """采纳建议：追加到项目知识 Block"""
    user = require_project_admin(request, project_id)
    db = get_db()
    row = db.execute(
        "SELECT content FROM knowledge_suggestions WHERE id = ? AND project_id = ? AND status = 'pending'",
        (suggestion_id, project_id),
    ).fetchone()
    if not row:
        db.close()
        raise HTTPException(404, "建议不存在或已处理")

    # 追加到项目知识 Block
    proj = db.execute(
        "SELECT project_block_id FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    block = letta.blocks.retrieve(block_id=proj["project_block_id"])
    new_content = block.value.rstrip() + "\n" + row["content"]
    letta.blocks.update(block_id=proj["project_block_id"], value=new_content)

    db.execute(
        "UPDATE knowledge_suggestions SET status = 'approved', reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (user["id"], suggestion_id),
    )
    db.commit()
    db.close()
    return {"status": "ok"}


@router.post("/project/{project_id}/suggestions/{suggestion_id}/reject")
async def reject_suggestion(project_id: str, suggestion_id: int, request: Request):
    """拒绝建议"""
    user = require_project_admin(request, project_id)
    db = get_db()
    db.execute(
        "UPDATE knowledge_suggestions SET status = 'rejected', reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (user["id"], suggestion_id),
    )
    db.commit()
    db.close()
    return {"status": "ok"}


# ===== 知识建议提交（供 Letta Agent 工具调用） =====


@router.post("/project/{project_id}/suggestions")
async def submit_suggestion(project_id: str, request: Request):
    """Agent 工具调用：提交项目知识建议"""
    body = await request.json()
    user_id = body.get("user_id", "")
    content = body.get("content", "").strip()
    if not content:
        raise HTTPException(400, "内容不能为空")
    db = get_db()
    db.execute(
        "INSERT INTO knowledge_suggestions (project_id, user_id, content) VALUES (?, ?, ?)",
        (project_id, user_id, content),
    )
    db.commit()
    db.close()
    return {"status": "ok"}


# ===== 用户搜索（添加成员用） =====


@router.get("/users/search")
async def search_users(request: Request, q: str = ""):
    """搜索 Open WebUI 用户（按姓名或邮箱匹配），用于添加项目成员"""
    extract_user_from_admin(request)
    from auth import _admin_api_get
    data = _admin_api_get("/api/v1/users/")
    if not data:
        return []
    users = data.get("users", data) if isinstance(data, dict) else data
    q = q.lower().strip()
    results = []
    for u in users:
        name = u.get("name", "")
        email = u.get("email", "")
        if q and q not in name.lower() and q not in email.lower():
            continue
        results.append({"id": u["id"], "name": name, "email": email})
    return results
