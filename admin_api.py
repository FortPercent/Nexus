"""知识管理后端 API（/admin/api/*）"""
import os
import logging
from fastapi import APIRouter, Request, UploadFile, HTTPException, File

import config
from config import ORG_ADMIN_EMAILS
from db import get_db, use_db, use_db_async
from auth import (
    extract_user_from_admin,
    require_project_member,
    require_project_admin,
    require_org_admin,
)
from routing import letta, letta_async, get_or_create_org_resources, get_or_create_personal_folder, get_or_create_personal_human_block, get_or_create_agent
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


def _file_to_dict(f) -> dict:
    """统一文件列表返回结构，带 embedding 进度字段供前端"索引中"徽章用。"""
    total = getattr(f, "total_chunks", None)
    done = getattr(f, "chunks_embedded", None)
    status = getattr(f, "processing_status", None)
    status_str = getattr(status, "value", status) if status is not None else None
    return {
        "id": f.id,
        "name": _file_name(f),
        "size": _file_size(f),
        "created_at": str(f.created_at),
        "processing_status": status_str,
        "total_chunks": total,
        "chunks_embedded": done,
        "progress": (done / total) if (total and done is not None) else None,
    }


# ===== 健康检查 =====


@router.get("/health")
async def health():
    """各依赖服务连通性检查。adapter 自己能响应即 adapter=ok；其他逐一 ping。"""
    import httpx as _httpx
    result = {"adapter": "ok"}
    # Letta
    try:
        r = _httpx.get(f"{config.LETTA_BASE_URL}/v1/health/", timeout=3)
        result["letta"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:
        result["letta"] = f"err: {type(e).__name__}"
    # vLLM（经代理网关访问，需 Content-Type + Auth）
    try:
        r = _httpx.get(
            f"{config.VLLM_ENDPOINT}/models",
            timeout=3,
            headers={
                "Authorization": f"Bearer {config.VLLM_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        result["vllm"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:
        result["vllm"] = f"err: {type(e).__name__}"
    # Ollama
    try:
        r = _httpx.get("http://ollama:11434/api/tags", timeout=3)
        result["ollama"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:
        result["ollama"] = f"err: {type(e).__name__}"
    # Open WebUI
    try:
        r = _httpx.get(f"{config.OPENWEBUI_URL}/health", timeout=3)
        result["webui"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:
        result["webui"] = f"err: {type(e).__name__}"
    # 数据库
    try:
        with use_db() as db:
            db.execute("SELECT 1").fetchone()
        result["sqlite"] = "ok"
    except Exception as e:
        result["sqlite"] = f"err: {type(e).__name__}"
    result["all_ok"] = all(v == "ok" for k, v in result.items() if k != "all_ok")
    return result


# ===== 当前用户 =====


@router.get("/me")
async def get_me(request: Request):
    user = await extract_user_from_admin(request)
    async with use_db_async() as db:
        async with db.execute(
            "SELECT p.project_id, p.name, pm.role FROM projects p "
            "JOIN project_members pm ON p.project_id = pm.project_id "
            "WHERE pm.user_id = ?",
            (user["id"],),
        ) as cur:
            projects = await cur.fetchall()
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
    user = await extract_user_from_admin(request)
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
    user = await extract_user_from_admin(request)
    with use_db() as db:
        rows = db.execute(
            "SELECT p.project_id, p.name, p.desc, p.folder_quota_mb, p.project_folder_id, "
            "p.created_by, pm.role, "
            "(SELECT COUNT(*) FROM project_members pm2 WHERE pm2.project_id = p.project_id) AS member_count, "
            "(SELECT COUNT(*) FROM project_todos t WHERE t.project_id = p.project_id "
            " AND t.priority='high' AND t.status IN ('open','in_progress')) AS todo_high_count, "
            "(SELECT COUNT(*) FROM project_todos t WHERE t.project_id = p.project_id "
            " AND ((t.status='awaiting_user' AND t.created_by = ?) "
            "   OR (t.status='awaiting_admin' AND pm.role='admin'))) AS todo_pending_count "
            "FROM projects p "
            "JOIN project_members pm ON p.project_id = pm.project_id "
            "WHERE pm.user_id = ?",
            (user["id"], user["id"]),
        ).fetchall()
    # 预取每个项目的高优 TODO 标题（top 2）
    with use_db() as db:
        todo_previews = {}
        for r in rows:
            prev = db.execute(
                "SELECT id, title FROM project_todos "
                "WHERE project_id = ? AND priority='high' AND status IN ('open','in_progress') "
                "ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, updated_at DESC LIMIT 2",
                (r["project_id"],),
            ).fetchall()
            todo_previews[r["project_id"]] = [
                {"id": p["id"], "title": p["title"]} for p in prev
            ]
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
            "todo_high_count": r["todo_high_count"] or 0,
            "todo_pending_count": r["todo_pending_count"] or 0,
            "todo_preview": todo_previews.get(r["project_id"], []),
        })
    return result


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    user = await extract_user_from_admin(request)
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
    await require_project_member(request, project_id)
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
    user = await require_project_admin(request, project_id)
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
    await require_project_admin(request, project_id)
    body = await request.json()
    with use_db() as db:
        db.execute(
            "UPDATE project_members SET role = ? WHERE user_id = ? AND project_id = ?",
            (body["role"], member_id, project_id),
        )
    return {"status": "ok"}


@router.delete("/project/{project_id}/members/{member_id}")
async def remove_member(project_id: str, member_id: str, request: Request):
    await require_project_admin(request, project_id)
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
    await require_project_member(request, project_id)
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
    await require_org_admin(request)
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
    await require_project_member(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT project_block_id FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    block = letta.blocks.retrieve(block_id=row["project_block_id"])
    return {"content": block.value, "limit": block.limit}


@router.put("/project/{project_id}/knowledge")
async def update_project_knowledge(project_id: str, request: Request):
    await require_project_admin(request, project_id)
    body = await request.json()
    with use_db() as db:
        row = db.execute(
            "SELECT project_block_id FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    letta.blocks.update(block_id=row["project_block_id"], value=body["content"])
    return {"status": "ok"}


# ===== 项目文件 =====


def _check_folder_size(folder_id: str, new_file, project_id: str = None):
    new_file.file.seek(0, 2)
    new_size = new_file.file.tell()
    new_file.file.seek(0)
    _check_folder_size_bytes(folder_id, new_size, project_id)


def _check_folder_size_bytes(folder_id: str, new_bytes: int, project_id: str = None):
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
    if total_size + new_bytes > quota:
        used_mb = total_size // (1024 * 1024)
        quota_mb = quota // (1024 * 1024)
        raise HTTPException(413, f"文件夹已用 {used_mb}MB，超过限额 {quota_mb}MB，请先删除旧文件")


def _display_name(letta_name: str) -> str:
    """Letta 内部存 `.xlsx.md`/`.csv.md`/`.docx.md`（Letta 只认 md/pdf/txt），
    但 UI 镜像给用户看的应是原名 `.xlsx`/`.csv`/`.docx`。"""
    for suffix in (".xlsx.md", ".xls.md", ".csv.md", ".docx.md"):
        if letta_name.endswith(suffix):
            return letta_name[: -3]  # 去掉末尾的 ".md"
    return letta_name


async def _process_and_upload(file, folder_id: str, scope: str, scope_id: str = "", owner_id: str = "", display_scope: str = "", project_id_for_size: str = None):
    """读取上传文件 → file_processor 预处理 → 逐条上传到 Letta + mirror。
    返回 [display_name, ...]（用户看到的名字，不带 .md 后缀）。"""
    import io as _io
    from file_processor import process_upload

    data = await file.read()
    _check_folder_size_bytes(folder_id, len(data), project_id_for_size)
    processed = process_upload(file.filename, data)

    uploaded_names = []
    for letta_name, content, mime in processed:
        try:
            uploaded = await letta_async.folders.files.upload(folder_id=folder_id, file=(letta_name, _io.BytesIO(content), mime))
        except Exception as e:
            logging.warning(f"upload {letta_name} failed: {e}")
            continue
        disp = _display_name(letta_name)
        try:
            fid = uploaded.id if hasattr(uploaded, "id") else None
            if fid:
                mirror_file(fid, folder_id, disp, scope, scope_id, owner_id, display_scope)
        except Exception as e:
            logging.warning(f"mirror failed for {disp}: {e}")
        uploaded_names.append(disp)
    return uploaded_names


@router.get("/project/{project_id}/files")
async def list_project_files(project_id: str, request: Request):
    await require_project_member(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT project_folder_id FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    files = _file_items(letta.folders.files.list(folder_id=row["project_folder_id"]))
    return [_file_to_dict(f) for f in files]


@router.post("/project/{project_id}/files")
async def upload_project_file(project_id: str, request: Request, file: UploadFile = File(...)):
    await require_project_member(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT project_folder_id, name FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
    proj_name = row["name"] if row else ""
    names = await _process_and_upload(
        file, row["project_folder_id"],
        scope="project", scope_id=project_id, owner_id="",
        display_scope=proj_name, project_id_for_size=project_id,
    )
    return {"status": "ok", "filename": file.filename, "uploaded": names}


@router.delete("/project/{project_id}/files/{file_id}")
async def delete_project_file(project_id: str, file_id: str, request: Request):
    await require_project_admin(request, project_id)
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
    await require_org_admin(request)
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
    await require_org_admin(request)
    return {"default_folder_quota_mb": config.DEFAULT_FOLDER_QUOTA_MB}


@router.put("/org/settings")
async def update_org_settings(request: Request):
    await require_org_admin(request)
    body = await request.json()
    config.DEFAULT_FOLDER_QUOTA_MB = body["default_folder_quota_mb"]
    return {"status": "ok", "default_folder_quota_mb": config.DEFAULT_FOLDER_QUOTA_MB}


@router.post("/reconcile")
async def manual_reconcile(request: Request):
    await require_org_admin(request)
    reconcile_all()
    from knowledge_mirror import reconcile_mirrors
    reconcile_mirrors()
    return {"status": "ok"}


# ===== 组织知识 =====

@router.get("/org/knowledge")
async def get_org_knowledge(request: Request):
    await extract_user_from_admin(request)
    resources = get_or_create_org_resources()
    block = letta.blocks.retrieve(block_id=resources["block_id"])
    return {"content": block.value, "limit": block.limit}


@router.put("/org/knowledge")
async def update_org_knowledge(request: Request):
    await require_org_admin(request)
    body = await request.json()
    resources = get_or_create_org_resources()
    block = letta.blocks.update(block_id=resources["block_id"], value=body["content"])
    return {"status": "ok", "limit": block.limit}


@router.get("/org/files")
async def list_org_files(request: Request):
    await extract_user_from_admin(request)
    resources = get_or_create_org_resources()
    files = _file_items(letta.folders.files.list(folder_id=resources["folder_id"]))
    return [_file_to_dict(f) for f in files]


@router.post("/org/files")
async def upload_org_file(request: Request, file: UploadFile = File(...)):
    await require_org_admin(request)
    resources = get_or_create_org_resources()
    names = await _process_and_upload(
        file, resources["folder_id"], scope="org",
    )
    return {"status": "ok", "filename": file.filename, "uploaded": names}


@router.delete("/org/files/{file_id}")
async def delete_org_file(file_id: str, request: Request):
    await require_org_admin(request)
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
    user = await extract_user_from_admin(request)
    folder_id = get_or_create_personal_folder(user["id"])
    files = _file_items(letta.folders.files.list(folder_id=folder_id))
    return [_file_to_dict(f) for f in files]


@router.post("/personal/files")
async def upload_personal_file(request: Request, file: UploadFile = File(...)):
    user = await extract_user_from_admin(request)
    folder_id = get_or_create_personal_folder(user["id"])
    names = await _process_and_upload(
        file, folder_id, scope="personal", owner_id=user["id"],
    )
    return {"status": "ok", "filename": file.filename, "uploaded": names}


@router.delete("/personal/files/{file_id}")
async def delete_personal_file(file_id: str, request: Request):
    user = await extract_user_from_admin(request)
    folder_id = get_or_create_personal_folder(user["id"])
    letta.folders.files.delete(folder_id=folder_id, file_id=file_id)
    try:
        unmirror_file(file_id)
    except Exception as e:
        logging.warning(f"unmirror failed for personal file {file_id}: {e}")
    return {"status": "ok"}


# ===== 批量文件 embedding 状态 —— 前端索引徽章用 =====


@router.get("/file-statuses")
async def file_statuses(request: Request):
    """返回当前用户能看到的所有 Letta 文件的 embedding 状态。

    前端在 Knowledge 列表页调用这个端点，按 letta_file_id 建 map，
    然后给每个 letta-mirror KB 行打"索引中 N/M"徽章。
    """
    user = await extract_user_from_admin(request)
    out = {}

    def _collect(folder_id):
        try:
            for f in _file_items(letta.folders.files.list(folder_id=folder_id)):
                out[f.id] = {
                    "processing_status": getattr(getattr(f, "processing_status", None), "value", getattr(f, "processing_status", None)),
                    "total_chunks": getattr(f, "total_chunks", None),
                    "chunks_embedded": getattr(f, "chunks_embedded", None),
                }
        except Exception as e:
            logging.warning(f"file_statuses collect {folder_id} failed: {e}")

    # 个人
    try:
        _collect(get_or_create_personal_folder(user["id"]))
    except Exception as e:
        logging.warning(f"file_statuses personal failed: {e}")

    # 组织
    try:
        _collect(get_or_create_org_resources()["folder_id"])
    except Exception as e:
        logging.warning(f"file_statuses org failed: {e}")

    # 项目（用户所在所有项目）
    with use_db() as db:
        rows = db.execute(
            "SELECT p.project_folder_id FROM projects p "
            "JOIN project_members pm ON p.project_id = pm.project_id "
            "WHERE pm.user_id = ?",
            (user["id"],),
        ).fetchall()
    for r in rows:
        _collect(r["project_folder_id"])

    return out


# ===== 个人记忆（human block）—— 跨项目共享一份 =====


@router.get("/personal/memory")
async def get_personal_memory(request: Request):
    """返回用户的 human block（跨所有项目共享一份）"""
    user = await extract_user_from_admin(request)
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
    user = await extract_user_from_admin(request)
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
    user = await extract_user_from_admin(request)
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
    user = await extract_user_from_admin(request)
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


async def _rebuild_agent_async(user_id: str, project_id: str, old_agent_id: str):
    """删旧 agent + 重建新 agent。detach 操作用 asyncio.gather 并行化。

    2026-04-19 改造：sync letta_client → letta_async + 并行 detach。
    之前 sync 调用累计 ~1.5s；并行化后 block/folder detach 同时做，总时间看单次 HTTP 延迟。"""
    import asyncio as _asyncio
    # 先把共享 block 和 folder 都 detach，防止级联删
    async def _detach_shared_blocks():
        try:
            page = await letta_async.agents.blocks.list(agent_id=old_agent_id)
            blocks = list(getattr(page, "items", page))
        except Exception as e:
            logging.warning(f"list blocks on {old_agent_id}: {e}")
            return
        shared = [b for b in blocks
                  if b.label in ("human", "org_knowledge") or (b.label or "").startswith("project_knowledge_")]
        async def _one(b):
            try:
                await letta_async.agents.blocks.detach(agent_id=old_agent_id, block_id=b.id)
            except Exception as e:
                logging.warning(f"detach block {b.id} from {old_agent_id}: {e}")
        await _asyncio.gather(*(_one(b) for b in shared), return_exceptions=True)

    async def _detach_all_folders():
        try:
            page = await letta_async.agents.folders.list(agent_id=old_agent_id)
            folders = list(getattr(page, "items", page))
        except Exception as e:
            logging.warning(f"list folders on {old_agent_id}: {e}")
            return
        async def _one(f):
            try:
                await letta_async.agents.folders.detach(agent_id=old_agent_id, folder_id=f.id)
            except Exception as e:
                logging.warning(f"detach folder {f.id} from {old_agent_id}: {e}")
        await _asyncio.gather(*(_one(f) for f in folders), return_exceptions=True)

    # blocks detach 和 folders detach 本身也可以并行
    await _asyncio.gather(_detach_shared_blocks(), _detach_all_folders(), return_exceptions=True)

    with use_db() as db:
        db.execute(
            "DELETE FROM user_agent_map WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        )
    try:
        await letta_async.agents.delete(agent_id=old_agent_id)
    except Exception as e:
        logging.warning(f"delete agent {old_agent_id} failed (continuing): {e}")
    # get_or_create_agent 仍是 sync，用 to_thread 避免阻塞 event loop
    return await _asyncio.to_thread(get_or_create_agent, user_id, project_id)


def _rebuild_agent(user_id: str, project_id: str, old_agent_id: str):
    """同步版本，保留给内部脚本（test regression 等）调用。生产路径用 _rebuild_agent_async。"""
    import asyncio as _asyncio
    try:
        loop = _asyncio.get_running_loop()
        # 在 event loop 里不能直接 asyncio.run
        raise RuntimeError("_rebuild_agent should not be called from async context; use _rebuild_agent_async")
    except RuntimeError:
        return _asyncio.run(_rebuild_agent_async(user_id, project_id, old_agent_id))


@router.delete("/personal/conversations/{project_id}")
async def clear_project_conversations(project_id: str, request: Request):
    """清空指定项目的对话历史。实现：删 agent + 重建（共享 human block 保留）。"""
    user = await extract_user_from_admin(request)
    with use_db() as db:
        row = db.execute(
            "SELECT agent_id FROM user_agent_map WHERE user_id = ? AND project_id = ?",
            (user["id"], project_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "无此项目的对话")
    new_agent_id = await _rebuild_agent_async(user["id"], project_id, row["agent_id"])
    _audit(user["id"], "clear_conversations", scope=project_id,
           details=f"old={row['agent_id']} new={new_agent_id}")
    return {"status": "ok", "project_id": project_id, "new_agent_id": new_agent_id}


@router.delete("/personal/conversations")
async def clear_all_conversations(request: Request):
    """清空当前用户所有项目的对话历史。多项目并行清空。"""
    import asyncio as _asyncio
    user = await extract_user_from_admin(request)
    with use_db() as db:
        rows = db.execute(
            "SELECT project_id, agent_id FROM user_agent_map WHERE user_id = ?",
            (user["id"],),
        ).fetchall()

    async def _clear_one(project_id, agent_id):
        try:
            await _rebuild_agent_async(user["id"], project_id, agent_id)
            return (project_id, True)
        except Exception as e:
            logging.warning(f"clear all: rebuild {project_id} failed: {e}")
            return (project_id, False)

    results = await _asyncio.gather(*(_clear_one(r["project_id"], r["agent_id"]) for r in rows))
    cleared = [p for p, ok in results if ok]
    failed = [p for p, ok in results if not ok]

    _audit(user["id"], "clear_all_conversations", scope=",".join(cleared),
           details=f"failed={failed}" if failed else "")
    return {"status": "ok", "cleared": cleared, "failed": failed}


# ===== 知识建议 =====


@router.get("/project/{project_id}/suggestions")
async def list_suggestions(project_id: str, request: Request):
    await require_project_member(request, project_id)
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
    user = await require_project_admin(request, project_id)
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
    user = await require_project_admin(request, project_id)
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
        # 防重：同 (project, user, content, pending) 已存在则返回已有 id（AI 超时重试不会重复）
        dup = db.execute(
            "SELECT id FROM knowledge_suggestions WHERE project_id=? AND user_id=? AND content=? AND status='pending'",
            (project_id, user_id, content),
        ).fetchone()
        if dup:
            return {"status": "ok", "id": dup["id"], "deduped": True}
        db.execute(
            "INSERT INTO knowledge_suggestions (project_id, user_id, content) VALUES (?, ?, ?)",
            (project_id, user_id, content),
        )
    return {"status": "ok"}


# ===== 项目 TODO =====


ALLOWED_STATUS = {"awaiting_user", "awaiting_admin", "open", "in_progress", "done", "cancelled"}
ALLOWED_PRIORITY = {"low", "medium", "high"}
ALLOWED_APPROVAL_MODES = {"ai_only", "strict", "open"}
# 成员可流转的主看板状态
MEMBER_WORKFLOW = {"open", "in_progress", "done"}


def _is_project_admin(db, user_id: str, project_id: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM project_members WHERE user_id = ? AND project_id = ? AND role = 'admin'",
        (user_id, project_id),
    ).fetchone()
    return bool(row)


def _get_approval_mode(db, project_id: str) -> str:
    row = db.execute(
        "SELECT todo_approval_mode FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    return (row["todo_approval_mode"] if row and row["todo_approval_mode"] else "ai_only")


def _todo_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "project_id": r["project_id"],
        "title": r["title"],
        "description": r["description"] or "",
        "status": r["status"],
        "priority": r["priority"],
        "source": r["source"],
        "created_by": r["created_by"],
        "assigned_to": r["assigned_to"] or "",
        "due_date": r["due_date"] or "",
        "cancel_reason": r["cancel_reason"] or "",
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "done_at": r["done_at"] or "",
        "done_by": r["done_by"] or "",
    }


@router.get("/project/{project_id}/todos")
async def list_project_todos(project_id: str, request: Request, status: str = "", assigned: str = ""):
    await require_project_member(request, project_id)
    q = "SELECT * FROM project_todos WHERE project_id = ?"
    params = [project_id]
    if status:
        q += " AND status = ?"
        params.append(status)
    if assigned:
        q += " AND assigned_to = ?"
        params.append(assigned)
    q += " ORDER BY CASE status "
    q += "WHEN 'awaiting_user' THEN 0 WHEN 'awaiting_admin' THEN 1 "
    q += "WHEN 'in_progress' THEN 2 WHEN 'open' THEN 3 WHEN 'done' THEN 4 WHEN 'cancelled' THEN 5 END, "
    q += "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 END, created_at DESC"
    with use_db() as db:
        rows = db.execute(q, params).fetchall()
    return [_todo_to_dict(r) for r in rows]


@router.post("/project/{project_id}/todos")
async def create_todo(project_id: str, request: Request):
    user = await require_project_member(request, project_id)
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title 不能为空")
    if len(title) > 200:
        raise HTTPException(400, "title 过长")
    description = (body.get("description") or "").strip()
    priority = body.get("priority") or "medium"
    if priority not in ALLOWED_PRIORITY:
        raise HTTPException(400, f"priority 必须是 {ALLOWED_PRIORITY}")
    assigned_to = body.get("assigned_to") or None
    due_date = body.get("due_date") or None
    source = body.get("source") or "manual"
    if source not in ("manual", "ai"):
        raise HTTPException(400, "source 必须是 manual 或 ai")

    with use_db() as db:
        is_admin = _is_project_admin(db, user["id"], project_id)
        mode = _get_approval_mode(db, project_id)
        # 决定初始 status
        if source == "ai":
            status = "awaiting_user"
        elif is_admin:
            status = "open"
        elif mode == "strict":
            status = "awaiting_admin"
        else:  # ai_only / open
            status = "open"
        cur = db.execute(
            "INSERT INTO project_todos (project_id, title, description, status, priority, source, "
            "created_by, assigned_to, due_date) VALUES (?,?,?,?,?,?,?,?,?)",
            (project_id, title, description, status, priority, source, user["id"], assigned_to, due_date),
        )
        todo_id = cur.lastrowid
        row = db.execute("SELECT * FROM project_todos WHERE id = ?", (todo_id,)).fetchone()
    return _todo_to_dict(row)


@router.put("/project/{project_id}/todos/{todo_id}")
async def update_todo(project_id: str, todo_id: int, request: Request):
    user = await require_project_member(request, project_id)
    body = await request.json()

    with use_db() as db:
        row = db.execute(
            "SELECT * FROM project_todos WHERE id = ? AND project_id = ?", (todo_id, project_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "TODO 不存在")
        is_admin = _is_project_admin(db, user["id"], project_id)
        is_creator = row["created_by"] == user["id"]
        is_assignee = row["assigned_to"] == user["id"]

        updates = {}
        # 状态修改：成员只能在主看板流转，且必须是自己创建或被指派
        if "status" in body:
            ns = body["status"]
            if ns not in ALLOWED_STATUS:
                raise HTTPException(400, "非法 status")
            if not is_admin:
                if ns not in MEMBER_WORKFLOW or row["status"] not in MEMBER_WORKFLOW:
                    raise HTTPException(403, "无权流转该状态")
                if not (is_creator or is_assignee):
                    raise HTTPException(403, "非创建者或负责人")
            updates["status"] = ns
            if ns == "done":
                updates["done_at"] = "CURRENT_TIMESTAMP"
                updates["done_by"] = user["id"]
            elif row["status"] == "done":
                # 重新打开
                updates["done_at"] = None
                updates["done_by"] = None

        # 其他字段：成员只能改自己创建的，admin 能改任何
        if any(k in body for k in ("title", "description", "priority", "assigned_to", "due_date")):
            if not (is_admin or is_creator):
                raise HTTPException(403, "非创建者或管理员")
            if "title" in body:
                t = (body["title"] or "").strip()
                if not t:
                    raise HTTPException(400, "title 不能为空")
                updates["title"] = t
            if "description" in body:
                updates["description"] = (body["description"] or "").strip()
            if "priority" in body:
                if body["priority"] not in ALLOWED_PRIORITY:
                    raise HTTPException(400, "非法 priority")
                updates["priority"] = body["priority"]
            if "assigned_to" in body:
                updates["assigned_to"] = body["assigned_to"] or None
            if "due_date" in body:
                updates["due_date"] = body["due_date"] or None

        if not updates:
            return _todo_to_dict(row)

        # 构造 UPDATE
        set_parts = []
        params = []
        for k, v in updates.items():
            if v == "CURRENT_TIMESTAMP":
                set_parts.append(f"{k} = CURRENT_TIMESTAMP")
            else:
                set_parts.append(f"{k} = ?")
                params.append(v)
        set_parts.append("updated_at = CURRENT_TIMESTAMP")
        params.append(todo_id)
        db.execute(f"UPDATE project_todos SET {', '.join(set_parts)} WHERE id = ?", params)
        new_row = db.execute("SELECT * FROM project_todos WHERE id = ?", (todo_id,)).fetchone()
    return _todo_to_dict(new_row)


@router.delete("/project/{project_id}/todos/{todo_id}")
async def delete_todo(project_id: str, todo_id: int, request: Request):
    user = await require_project_member(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT * FROM project_todos WHERE id = ? AND project_id = ?", (todo_id, project_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "TODO 不存在")
        is_admin = _is_project_admin(db, user["id"], project_id)
        is_creator = row["created_by"] == user["id"]
        # 成员只能删自己未确认的 awaiting_*（相当于撤回）；admin 随意
        if not is_admin:
            if not is_creator:
                raise HTTPException(403, "非创建者或管理员")
            if row["status"] not in ("awaiting_user", "awaiting_admin"):
                raise HTTPException(403, "已进入看板的 TODO 不能删除，请先驳回/取消")
        db.execute("DELETE FROM project_todos WHERE id = ?", (todo_id,))
    return {"status": "ok"}


@router.post("/project/{project_id}/todos/{todo_id}/confirm")
async def confirm_todo(project_id: str, todo_id: int, request: Request):
    """member 把 awaiting_user 转正：按 approval_mode 进 awaiting_admin 或直接 open"""
    user = await require_project_member(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT * FROM project_todos WHERE id = ? AND project_id = ?", (todo_id, project_id),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "awaiting_user":
            raise HTTPException(400, "状态不是 awaiting_user")
        if row["created_by"] != user["id"] and not _is_project_admin(db, user["id"], project_id):
            raise HTTPException(403, "非创建者")
        mode = _get_approval_mode(db, project_id)
        is_admin = _is_project_admin(db, user["id"], project_id)
        next_status = "open" if (mode != "strict" or is_admin) else "awaiting_admin"
        db.execute(
            "UPDATE project_todos SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (next_status, todo_id),
        )
        new_row = db.execute("SELECT * FROM project_todos WHERE id = ?", (todo_id,)).fetchone()
    return _todo_to_dict(new_row)


@router.post("/project/{project_id}/todos/{todo_id}/approve")
async def approve_todo(project_id: str, todo_id: int, request: Request):
    await require_project_admin(request, project_id)
    with use_db() as db:
        row = db.execute(
            "SELECT * FROM project_todos WHERE id = ? AND project_id = ?", (todo_id, project_id),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        if row["status"] != "awaiting_admin":
            raise HTTPException(400, "状态不是 awaiting_admin")
        db.execute(
            "UPDATE project_todos SET status = 'open', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (todo_id,),
        )
        new_row = db.execute("SELECT * FROM project_todos WHERE id = ?", (todo_id,)).fetchone()
    return _todo_to_dict(new_row)


@router.post("/project/{project_id}/todos/{todo_id}/reject")
async def reject_todo(project_id: str, todo_id: int, request: Request):
    """member 拒自己的 awaiting_user；admin 驳回任何 awaiting_*"""
    user = await require_project_member(request, project_id)
    body = await request.json() if request.headers.get("content-length") else {}
    reason = (body.get("reason") or "").strip() if isinstance(body, dict) else ""
    with use_db() as db:
        row = db.execute(
            "SELECT * FROM project_todos WHERE id = ? AND project_id = ?", (todo_id, project_id),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        is_admin = _is_project_admin(db, user["id"], project_id)
        if row["status"] == "awaiting_user":
            if row["created_by"] != user["id"] and not is_admin:
                raise HTTPException(403)
        elif row["status"] == "awaiting_admin":
            if not is_admin:
                raise HTTPException(403, "只有管理员可驳回待审核")
        else:
            raise HTTPException(400, "状态不是 awaiting_*")
        db.execute(
            "UPDATE project_todos SET status = 'cancelled', cancel_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (reason, todo_id),
        )
        new_row = db.execute("SELECT * FROM project_todos WHERE id = ?", (todo_id,)).fetchone()
    return _todo_to_dict(new_row)


@router.post("/project/{project_id}/todos/ai-submit")
async def ai_submit_todo(project_id: str, request: Request):
    """Letta agent 工具内部调用：提交一个 AI 建议的 TODO（status=awaiting_user, source=ai）。
    无 JWT；body 必须含 user_id + title；校验 (user_id, project_id) 是 project 成员。"""
    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    title = (body.get("title") or "").strip()
    description = (body.get("description") or "").strip()
    priority = body.get("priority") or "medium"
    if not user_id or not title:
        raise HTTPException(400, "user_id 和 title 必填")
    if priority not in ALLOWED_PRIORITY:
        priority = "medium"
    if len(title) > 200:
        title = title[:200]
    with use_db() as db:
        member = db.execute(
            "SELECT 1 FROM project_members WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        ).fetchone()
        if not member:
            raise HTTPException(403, "非项目成员")
        # 防重：同 (project, user, title, source=ai, 还在 awaiting_user) 返回已有 id
        dup = db.execute(
            "SELECT id FROM project_todos WHERE project_id=? AND created_by=? AND title=? "
            "AND source='ai' AND status='awaiting_user'",
            (project_id, user_id, title),
        ).fetchone()
        if dup:
            return {"status": "ok", "todo_id": dup["id"], "deduped": True}
        cur = db.execute(
            "INSERT INTO project_todos (project_id, title, description, status, priority, source, created_by) "
            "VALUES (?, ?, ?, 'awaiting_user', ?, 'ai', ?)",
            (project_id, title, description, priority, user_id),
        )
        todo_id = cur.lastrowid
    return {"status": "ok", "todo_id": todo_id}


@router.get("/my-todos")
async def my_todos(request: Request):
    """跨项目：与我相关的 TODO（我创建的 OR 分配给我的），排除 cancelled"""
    user = await extract_user_from_admin(request)
    with use_db() as db:
        rows = db.execute(
            "SELECT t.*, p.name AS project_name "
            "FROM project_todos t LEFT JOIN projects p ON p.project_id = t.project_id "
            "WHERE (t.created_by = ? OR t.assigned_to = ?) AND t.status != 'cancelled' "
            "ORDER BY CASE t.status WHEN 'awaiting_user' THEN 0 WHEN 'awaiting_admin' THEN 1 "
            "WHEN 'in_progress' THEN 2 WHEN 'open' THEN 3 WHEN 'done' THEN 4 END, t.updated_at DESC",
            (user["id"], user["id"]),
        ).fetchall()
    out = []
    for r in rows:
        d = _todo_to_dict(r)
        d["project_name"] = r["project_name"] or r["project_id"]
        out.append(d)
    return out


@router.put("/project/{project_id}/settings/todo")
async def update_todo_setting(project_id: str, request: Request):
    await require_project_admin(request, project_id)
    body = await request.json()
    mode = body.get("approval_mode")
    if mode not in ALLOWED_APPROVAL_MODES:
        raise HTTPException(400, f"approval_mode 必须是 {ALLOWED_APPROVAL_MODES}")
    with use_db() as db:
        db.execute(
            "UPDATE projects SET todo_approval_mode = ? WHERE project_id = ?",
            (mode, project_id),
        )
    return {"status": "ok", "approval_mode": mode}


@router.get("/project/{project_id}/settings/todo")
async def get_todo_setting(project_id: str, request: Request):
    await require_project_member(request, project_id)
    with use_db() as db:
        mode = _get_approval_mode(db, project_id)
    return {"approval_mode": mode}


# ===== 用户搜索（添加成员用） =====


@router.get("/users/search")
async def search_users(request: Request, q: str = ""):
    await extract_user_from_admin(request)
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
