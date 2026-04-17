"""知识管理后端 API（/admin/api/*）"""
import os
import logging
from fastapi import APIRouter, Request, UploadFile, HTTPException, File

import config
from config import ORG_ADMIN_EMAILS
from db import get_db, use_db
from auth import (
    extract_user_from_admin,
    require_project_member,
    require_project_admin,
    require_org_admin,
)
from routing import letta, get_or_create_org_resources, get_or_create_personal_folder, get_or_create_personal_human_block, get_or_create_agent
from webui_sync import grant_model_access, revoke_model_access, revoke_all_model_access, reconcile_all
from knowledge_mirror import mirror_file, unmirror_file

router = APIRouter(prefix="/admin/api")


def _file_name(file_obj) -> str:
    source = getattr(file_obj, "source", None)
    if source and getattr(source, "filename", None):
        return source.filename
    return getattr(file_obj, "original_file_name", None) or getattr(file_obj, "file_name", None) or ""


def _file_size(file_obj) -> int:
    source = getattr(file_obj, "source", None)
    if source and getattr(source, "file_size", None) is not None:
        return source.file_size
    return getattr(file_obj, "file_size", 0) or 0


def _file_items(files_page):
    return list(getattr(files_page, "items", files_page))


# ===== 当前用户 =====


@router.get("/me")
async def get_me(request: Request):
    user = extract_user_from_admin(request)
    with use_db() as db:
        projects = db.execute(
            "SELECT p.project_id, p.name, pm.role FROM projects p "
            "JOIN project_members pm ON p.project_id = pm.project_id "
            "WHERE pm.user_id = ?",
            (user["id"],),
        ).fetchall()
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
        folder = letta.folders.create(name=f"proj-{project_id}", embedding_config={"embedding_model": "nomic-embed-text", "embedding_endpoint_type": "openai", "embedding_endpoint": "http://ollama:11434/v1", "embedding_dim": 768})
        with use_db() as db:
            db.execute(
                "INSERT INTO projects (project_id, name, desc, created_by, project_block_id, project_folder_id) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, name, desc, user["id"], block.id, folder.id),
            )
            db.execute(
                "INSERT INTO project_members (user_id, project_id, role, added_by) VALUES (?, ?, 'admin', ?)",
                (user["id"], project_id, user["id"]),
            )
    except Exception:
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

    try:
        from webui_sync import _ensure_model_registered, _get_webui_db
        model_id = f"letta-{project_id}"
        webui_db = _get_webui_db()
        try:
            _ensure_model_registered(webui_db, model_id, f"Nexus · {name}")
            webui_db.commit()
        finally:
            webui_db.close()
        grant_model_access(user["id"], model_id)
    except Exception as e:
        logging.warning(f"sync grant failed for create_project {project_id}: {e}")

    return {"status": "ok", "project_id": project_id}


@router.get("/projects")
async def list_my_projects(request: Request):
    user = extract_user_from_admin(request)
    with use_db() as db:
        rows = db.execute(
            "SELECT p.project_id, p.name, p.desc, p.folder_quota_mb, p.project_folder_id, "
            "p.created_by, pm.role, "
            "(SELECT COUNT(*) FROM project_members pm2 WHERE pm2.project_id = p.project_id) as member_count "
            "FROM projects p "
            "JOIN project_members pm ON p.project_id = pm.project_id "
            "WHERE pm.user_id = ?",
            (user["id"],),
        ).fetchall()
    result = []
    for r in rows:
        try:
            files = _file_items(letta.folders.files.list(folder_id=r["project_folder_id"]))
            used_mb = sum(_file_size(f) for f in files) // (1024 * 1024)
        except Exception:
            used_mb = 0
        result.append({
            "id": r["project_id"],
            "name": r["name"],
            "desc": r["desc"],
            "quota_mb": r["folder_quota_mb"],
            "used_mb": used_mb,
            "members": r["member_count"],
            "role": r["role"],
        })
    return result


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    user = extract_user_from_admin(request)
    is_org = user.get("email") in ORG_ADMIN_EMAILS
    with use_db() as db:
        is_proj_admin = db.execute(
            "SELECT 1 FROM project_members WHERE user_id = ? AND project_id = ? AND role = 'admin'",
            (user["id"], project_id),
        ).fetchone()
        if not is_org and not is_proj_admin:
            raise HTTPException(403, "需要项目 admin 或组织 admin 权限")

        proj = db.execute(
            "SELECT project_block_id, project_folder_id FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if not proj:
            raise HTTPException(404, "项目不存在")

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

    try:
        revoke_all_model_access(f"letta-{project_id}")
    except Exception as e:
        logging.warning(f"sync revoke_all failed for delete_project {project_id}: {e}")

    return {"status": "ok"}


# ===== 项目成员 =====


@router.get("/project/{project_id}/members")
async def list_project_members(project_id: str, request: Request):
    require_project_member(request, project_id)
    with use_db() as db:
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

    with use_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO project_members (user_id, project_id, role, added_by) VALUES (?, ?, ?, ?)",
            (new_user_id, project_id, role, user["id"]),
        )

    # 如果该用户已有 Agent，挂载项目知识
    try:
        with use_db() as db:
            agent_row = db.execute(
                "SELECT agent_id FROM user_agent_map WHERE user_id = ? AND project_id = ?",
                (new_user_id, project_id),
            ).fetchone()
            proj = db.execute(
                "SELECT project_block_id, project_folder_id FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if agent_row and proj:
            letta.agents.blocks.attach(agent_id=agent_row["agent_id"], block_id=proj["project_block_id"])
            letta.agents.folders.attach(agent_id=agent_row["agent_id"], folder_id=proj["project_folder_id"])
    except Exception as e:
        logging.warning(f"attach agent resources failed for {new_user_id}: {e}")

    try:
        grant_model_access(new_user_id, f"letta-{project_id}")
    except Exception as e:
        logging.warning(f"sync grant failed for add_member {new_user_id} to {project_id}: {e}")

    # 为新成员创建该项目所有文件的镜像
    try:
        from knowledge_mirror import mirror_file_for_user, _list_folder_files, _get_file_name
        with use_db() as db:
            proj_row = db.execute(
                "SELECT project_folder_id, name FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        if proj_row:
            for f in _list_folder_files(letta, proj_row["project_folder_id"]):
                mirror_file_for_user(f.id, proj_row["project_folder_id"], _get_file_name(f),
                                     "project", project_id, new_user_id, proj_row["name"])
    except Exception as e:
        logging.warning(f"mirror creation failed for add_member {new_user_id} to {project_id}: {e}")

    return {"status": "ok"}


@router.put("/project/{project_id}/members/{member_id}/role")
async def set_member_role(project_id: str, member_id: str, request: Request):
    require_project_admin(request, project_id)
    body = await request.json()
    with use_db() as db:
        db.execute(
            "UPDATE project_members SET role = ? WHERE user_id = ? AND project_id = ?",
            (body["role"], member_id, project_id),
        )
    return {"status": "ok"}


@router.delete("/project/{project_id}/members/{member_id}")
async def remove_member(project_id: str, member_id: str, request: Request):
    require_project_admin(request, project_id)
    with use_db() as db:
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

    try:
        revoke_model_access(member_id, f"letta-{project_id}")
    except Exception as e:
        logging.warning(f"sync revoke failed for remove_member {member_id} from {project_id}: {e}")

    # 删除该成员的项目文件镜像
    try:
        from knowledge_mirror import _get_admin_token, _api as mirror_api
        with use_db() as mirror_db:
            mirrors = mirror_db.execute(
                "SELECT knowledge_id FROM knowledge_mirrors WHERE scope = 'project' AND scope_id = ? AND for_user_id = ?",
                (project_id, member_id),
            ).fetchall()
            admin_token = _get_admin_token()
            for m in mirrors:
                mirror_api("DELETE", f"/api/v1/knowledge/{m['knowledge_id']}/delete", token=admin_token)
            mirror_db.execute(
                "DELETE FROM knowledge_mirrors WHERE scope = 'project' AND scope_id = ? AND for_user_id = ?",
                (project_id, member_id),
            )
    except Exception as e:
        logging.warning(f"mirror cleanup failed for remove_member {member_id} from {project_id}: {e}")

    return {"status": "ok"}


# ===== 项目设置 =====


@router.get("/project/{project_id}/quota")
async def get_project_quota(project_id: str, request: Request):
    require_project_member(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT folder_quota_mb, project_folder_id FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
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
    with use_db() as db:
        db.execute(
            "UPDATE projects SET folder_quota_mb = ? WHERE project_id = ?",
            (body["folder_quota_mb"], project_id),
        )
    return {"status": "ok", "folder_quota_mb": body["folder_quota_mb"]}


# ===== 项目知识 =====


@router.get("/project/{project_id}/knowledge")
async def get_project_knowledge(project_id: str, request: Request):
    require_project_member(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT project_block_id FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    block = letta.blocks.retrieve(block_id=row["project_block_id"])
    return {"content": block.value, "limit": block.limit}


@router.put("/project/{project_id}/knowledge")
async def update_project_knowledge(project_id: str, request: Request):
    require_project_admin(request, project_id)
    body = await request.json()
    with use_db() as db:
        row = db.execute(
            "SELECT project_block_id FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    letta.blocks.update(block_id=row["project_block_id"], value=body["content"])
    return {"status": "ok"}


# ===== 项目文件 =====


def _check_folder_size(folder_id: str, new_file, project_id: str = None):
    with use_db() as db:
        if project_id:
            row = db.execute(
                "SELECT folder_quota_mb FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            quota = (row["folder_quota_mb"] if row else config.DEFAULT_FOLDER_QUOTA_MB) * 1024 * 1024
        else:
            quota = config.DEFAULT_FOLDER_QUOTA_MB * 1024 * 1024

    files = _file_items(letta.folders.files.list(folder_id=folder_id))
    total_size = sum(_file_size(f) for f in files)
    new_file.file.seek(0, 2)
    new_size = new_file.file.tell()
    new_file.file.seek(0)
    if total_size + new_size > quota:
        used_mb = total_size // (1024 * 1024)
        quota_mb = quota // (1024 * 1024)
        raise HTTPException(413, f"文件夹已用 {used_mb}MB，超过限额 {quota_mb}MB，请先删除旧文件")


@router.get("/project/{project_id}/files")
async def list_project_files(project_id: str, request: Request):
    require_project_member(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT project_folder_id FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    files = _file_items(letta.folders.files.list(folder_id=row["project_folder_id"]))
    return [{"id": f.id, "name": _file_name(f), "size": _file_size(f), "created_at": str(f.created_at)} for f in files]


@router.post("/project/{project_id}/files")
async def upload_project_file(project_id: str, request: Request, file: UploadFile = File(...)):
    require_project_member(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT project_folder_id, name FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    _check_folder_size(row["project_folder_id"], file, project_id)
    proj_name = row["name"] if row else ""
    uploaded = letta.folders.files.upload(folder_id=row["project_folder_id"], file=(file.filename, file.file, file.content_type))
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
    with use_db() as db:
        row = db.execute(
            "SELECT project_folder_id FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
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
    with use_db() as db:
        rows = db.execute(
            "SELECT p.project_id, p.name, p.created_by, p.folder_quota_mb, p.project_folder_id, "
            "uc.name as creator_name, "
            "(SELECT COUNT(*) FROM project_members pm WHERE pm.project_id = p.project_id) as member_count "
            "FROM projects p "
            "LEFT JOIN user_cache uc ON p.created_by = uc.user_id"
        ).fetchall()
    result = []
    for r in rows:
        try:
            files = _file_items(letta.folders.files.list(folder_id=r["project_folder_id"]))
            used_mb = sum(_file_size(f) for f in files) // (1024 * 1024)
            file_count = len(files)
        except Exception:
            used_mb, file_count = 0, 0
        result.append({
            "id": r["project_id"],
            "name": r["name"],
            "creator": r["creator_name"] or r["created_by"][:8],
            "members": r["member_count"],
            "files": file_count,
            "quota_mb": r["folder_quota_mb"],
            "used_mb": used_mb,
        })
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
    return [{"id": f.id, "name": _file_name(f), "size": _file_size(f), "created_at": str(f.created_at)} for f in files]


@router.post("/org/files")
async def upload_org_file(request: Request, file: UploadFile = File(...)):
    require_org_admin(request)
    resources = get_or_create_org_resources()
    _check_folder_size(resources["folder_id"], file)
    uploaded = letta.folders.files.upload(folder_id=resources["folder_id"], file=(file.filename, file.file, file.content_type))
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
    return [{"id": f.id, "name": _file_name(f), "size": _file_size(f), "created_at": str(f.created_at)} for f in files]


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


# ===== 个人记忆（human block）—— 跨项目共享一份 =====


@router.get("/personal/memory")
async def get_personal_memory(request: Request):
    """返回用户的 human block（跨所有项目共享一份）"""
    user = extract_user_from_admin(request)
    try:
        block_id = get_or_create_personal_human_block(user["id"])
        block = letta.blocks.retrieve(block_id=block_id)
    except Exception as e:
        logging.warning(f"get_personal_memory failed: {e}")
        raise HTTPException(500, "读取记忆失败")
    return {
        "block_id": block.id,
        "content": block.value or "",
        "limit": block.limit,
    }


@router.put("/personal/memory")
async def update_personal_memory(request: Request):
    """更新用户的 human block；跨项目自动同步（因为是同一个 block_id）"""
    user = extract_user_from_admin(request)
    body = await request.json()
    content = body.get("content", "")
    block_id = get_or_create_personal_human_block(user["id"])
    letta.blocks.update(block_id=block_id, value=content)
    return {"status": "ok"}


# ===== 对话记忆（message history）=====


def _audit(user_id: str, action: str, scope: str = "", details: str = ""):
    try:
        with use_db() as db:
            db.execute(
                "INSERT INTO audit_log (user_id, action, scope, details) VALUES (?, ?, ?, ?)",
                (user_id, action, scope, details),
            )
    except Exception as e:
        logging.warning(f"audit log failed: {e}")


def _message_text(msg) -> str:
    """从 Letta message 里提取可读文本"""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            t = getattr(p, "text", None)
            if not t and isinstance(p, dict):
                t = p.get("text")
            if t:
                parts.append(t)
        if parts:
            return "".join(parts)
    reasoning = getattr(msg, "reasoning", "")
    if reasoning:
        return reasoning
    return ""


def _message_role(msg) -> str:
    """规约成 user / assistant / reasoning / tool / system"""
    mt = getattr(msg, "message_type", "") or ""
    if mt == "reasoning_message":
        return "reasoning"
    if mt == "assistant_message":
        return "assistant"
    if mt == "user_message":
        return "user"
    if mt in ("tool_call_message", "tool_return_message"):
        return "tool"
    if mt == "system_message":
        return "system"
    return getattr(msg, "role", "") or mt or "unknown"


@router.get("/personal/conversations")
async def list_conversations_overview(request: Request):
    """列当前用户每个项目的 agent + 消息数概览"""
    user = extract_user_from_admin(request)
    with use_db() as db:
        rows = db.execute(
            "SELECT uam.project_id, uam.agent_id, p.name "
            "FROM user_agent_map uam "
            "LEFT JOIN projects p ON p.project_id = uam.project_id "
            "WHERE uam.user_id = ?",
            (user["id"],),
        ).fetchall()
    overview = []
    for r in rows:
        count = 0
        last_at = None
        try:
            msgs = letta.agents.messages.list(agent_id=r["agent_id"], limit=200)
            items = list(getattr(msgs, "items", msgs))
            count = len(items)
            if items:
                last_at = str(items[-1].date) if getattr(items[-1], "date", None) else None
        except Exception as e:
            logging.warning(f"list conversations overview failed for {r['agent_id']}: {e}")
        overview.append({
            "project_id": r["project_id"],
            "project_name": r["name"] or r["project_id"],
            "message_count": count,
            "last_message_at": last_at,
        })
    return overview


@router.get("/personal/conversations/{project_id}")
async def list_conversations(project_id: str, request: Request, limit: int = 100):
    """列指定项目的消息，按时间倒序"""
    user = extract_user_from_admin(request)
    with use_db() as db:
        row = db.execute(
            "SELECT agent_id FROM user_agent_map WHERE user_id = ? AND project_id = ?",
            (user["id"], project_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "无此项目的对话")
    msgs = letta.agents.messages.list(agent_id=row["agent_id"], limit=limit)
    items = list(getattr(msgs, "items", msgs))
    result = []
    for m in items:
        result.append({
            "id": getattr(m, "id", ""),
            "role": _message_role(m),
            "text": _message_text(m),
            "date": str(getattr(m, "date", "")) if getattr(m, "date", None) else "",
            "message_type": getattr(m, "message_type", ""),
        })
    # 倒序展示（最新在前）
    result.reverse()
    return result


def _rebuild_agent(user_id: str, project_id: str, old_agent_id: str):
    """删旧 agent + 重建新 agent。删之前 detach 掉所有共享 block（human/project/org），
    防止 Letta 级联删除殃及到其他 agent 仍在用的 block。"""
    # 先把共享 block 和 folder 都 detach，防止级联删
    try:
        blocks = list(letta.agents.blocks.list(agent_id=old_agent_id))
        for b in blocks:
            if b.label in ("human", "org_knowledge") or (b.label or "").startswith("project_knowledge_"):
                try:
                    letta.agents.blocks.detach(agent_id=old_agent_id, block_id=b.id)
                except Exception as e:
                    logging.warning(f"detach block {b.id} from {old_agent_id}: {e}")
    except Exception as e:
        logging.warning(f"list blocks on {old_agent_id}: {e}")
    try:
        folders = list(letta.agents.folders.list(agent_id=old_agent_id))
        for f in folders:
            try:
                letta.agents.folders.detach(agent_id=old_agent_id, folder_id=f.id)
            except Exception as e:
                logging.warning(f"detach folder {f.id} from {old_agent_id}: {e}")
    except Exception as e:
        logging.warning(f"list folders on {old_agent_id}: {e}")

    with use_db() as db:
        db.execute(
            "DELETE FROM user_agent_map WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        )
    try:
        letta.agents.delete(agent_id=old_agent_id)
    except Exception as e:
        logging.warning(f"delete agent {old_agent_id} failed (continuing): {e}")
    # 触发懒重建
    return get_or_create_agent(user_id, project_id)


@router.delete("/personal/conversations/{project_id}")
async def clear_project_conversations(project_id: str, request: Request):
    """清空指定项目的对话历史。实现：删 agent + 重建（共享 human block 保留）。"""
    user = extract_user_from_admin(request)
    with use_db() as db:
        row = db.execute(
            "SELECT agent_id FROM user_agent_map WHERE user_id = ? AND project_id = ?",
            (user["id"], project_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "无此项目的对话")
    new_agent_id = _rebuild_agent(user["id"], project_id, row["agent_id"])
    _audit(user["id"], "clear_conversations", scope=project_id,
           details=f"old={row['agent_id']} new={new_agent_id}")
    return {"status": "ok", "project_id": project_id, "new_agent_id": new_agent_id}


@router.delete("/personal/conversations")
async def clear_all_conversations(request: Request):
    """清空当前用户所有项目的对话历史"""
    user = extract_user_from_admin(request)
    with use_db() as db:
        rows = db.execute(
            "SELECT project_id, agent_id FROM user_agent_map WHERE user_id = ?",
            (user["id"],),
        ).fetchall()
    cleared = []
    failed = []
    for r in rows:
        try:
            _rebuild_agent(user["id"], r["project_id"], r["agent_id"])
            cleared.append(r["project_id"])
        except Exception as e:
            logging.warning(f"clear all: rebuild {r['project_id']} failed: {e}")
            failed.append(r["project_id"])
    _audit(user["id"], "clear_all_conversations", scope=",".join(cleared),
           details=f"failed={failed}" if failed else "")
    return {"status": "ok", "cleared": cleared, "failed": failed}


# ===== 知识建议 =====


@router.get("/project/{project_id}/suggestions")
async def list_suggestions(project_id: str, request: Request):
    require_project_member(request, project_id)
    with use_db() as db:
        rows = db.execute(
            "SELECT s.id, s.content, s.user_id, s.status, s.created_at, uc.name "
            "FROM knowledge_suggestions s "
            "LEFT JOIN user_cache uc ON s.user_id = uc.user_id "
            "WHERE s.project_id = ? ORDER BY s.created_at DESC",
            (project_id,),
        ).fetchall()
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
    user = require_project_admin(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT content FROM knowledge_suggestions WHERE id = ? AND project_id = ? AND status = 'pending'",
            (suggestion_id, project_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "建议不存在或已处理")

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
    return {"status": "ok"}


@router.post("/project/{project_id}/suggestions/{suggestion_id}/reject")
async def reject_suggestion(project_id: str, suggestion_id: int, request: Request):
    user = require_project_admin(request, project_id)
    with use_db() as db:
        db.execute(
            "UPDATE knowledge_suggestions SET status = 'rejected', reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (user["id"], suggestion_id),
        )
    return {"status": "ok"}


# ===== 知识建议提交（供 Letta Agent 工具调用） =====


@router.post("/project/{project_id}/suggestions")
async def submit_suggestion(project_id: str, request: Request):
    body = await request.json()
    user_id = body.get("user_id", "")
    content = body.get("content", "").strip()
    if not content:
        raise HTTPException(400, "内容不能为空")
    with use_db() as db:
        member = db.execute(
            "SELECT 1 FROM project_members WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        ).fetchone()
        if not member:
            raise HTTPException(403, "非项目成员无法提交建议")
        db.execute(
            "INSERT INTO knowledge_suggestions (project_id, user_id, content) VALUES (?, ?, ?)",
            (project_id, user_id, content),
        )
    return {"status": "ok"}


# ===== 用户搜索（添加成员用） =====


@router.get("/users/search")
async def search_users(request: Request, q: str = ""):
    extract_user_from_admin(request)
    from auth import _admin_api_get
    data = _admin_api_get("/api/v1/users/")
    if not data:
        return []
    users = data.get("users", data) if isinstance(data, dict) else data
    q = q.lower().strip()
    return [
        {"id": u["id"], "name": u.get("name", ""), "email": u.get("email", "")}
        for u in users
        if not q or q in u.get("name", "").lower() or q in u.get("email", "").lower()
    ]
