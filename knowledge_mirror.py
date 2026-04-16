"""Letta 文件 → Open WebUI Knowledge Collection 镜像同步

只同步元数据（名字），不同步正文。
正文在聊天时由 Pipeline/Adapter 从 Letta 实时检索。
"""
import logging
import httpx

from config import OPENWEBUI_URL, OPENWEBUI_ADMIN_EMAIL, OPENWEBUI_ADMIN_PASSWORD
from db import get_db

logger = logging.getLogger(__name__)

_token_cache = {"token": None}


def _get_admin_token(force_refresh=False):
    if _token_cache["token"] and not force_refresh:
        return _token_cache["token"]
    try:
        resp = httpx.post(
            f"{OPENWEBUI_URL}/api/v1/auths/signin",
            json={"email": OPENWEBUI_ADMIN_EMAIL, "password": OPENWEBUI_ADMIN_PASSWORD},
            timeout=10,
        )
        if resp.status_code == 200:
            _token_cache["token"] = resp.json().get("token", "")
            return _token_cache["token"]
    except Exception as e:
        logger.error(f"login failed: {e}")
    return ""


def _api(method, path, json_data=None):
    token = _get_admin_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if method == "GET":
            resp = httpx.get(f"{OPENWEBUI_URL}{path}", headers=headers, timeout=10)
        elif method == "POST":
            resp = httpx.post(f"{OPENWEBUI_URL}{path}", headers=headers, json=json_data, timeout=10)
        elif method == "DELETE":
            resp = httpx.delete(f"{OPENWEBUI_URL}{path}", headers=headers, timeout=10)
        else:
            return None
        if resp.status_code in (401, 403):
            token = _get_admin_token(force_refresh=True)
            if not token:
                return None
            headers = {"Authorization": f"Bearer {token}"}
            if method == "GET":
                resp = httpx.get(f"{OPENWEBUI_URL}{path}", headers=headers, timeout=10)
            elif method == "POST":
                resp = httpx.post(f"{OPENWEBUI_URL}{path}", headers=headers, json=json_data, timeout=10)
            elif method == "DELETE":
                resp = httpx.delete(f"{OPENWEBUI_URL}{path}", headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"API {method} {path} returned {resp.status_code}")
    except Exception as e:
        logger.error(f"API {method} {path} failed: {e}")
    return None


def _make_display_name(filename, scope, project_name=""):
    if scope == "personal":
        return f"[个人] {filename}"
    elif scope == "project":
        return f"[{project_name}] {filename}"
    elif scope == "org":
        return f"[组织] {filename}"
    return filename


def mirror_file(letta_file_id, letta_folder_id, filename, scope, scope_id="", owner_id="", project_name=""):
    """上传文件后，创建对应的 Open WebUI Knowledge Collection 镜像"""
    display_name = _make_display_name(filename, scope, project_name)

    # 创建 Knowledge Collection
    result = _api("POST", "/api/v1/knowledge/create", {
        "name": display_name,
        "description": f"letta-mirror:{letta_file_id}",
    })
    if not result:
        logger.error(f"failed to create knowledge mirror for {filename}")
        return None

    knowledge_id = result.get("id", "")

    # 写入镜像表
    db = get_db()
    try:
        db.execute(
            "INSERT OR REPLACE INTO knowledge_mirrors "
            "(letta_file_id, letta_folder_id, knowledge_id, scope, scope_id, owner_id, display_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (letta_file_id, letta_folder_id, knowledge_id, scope, scope_id, owner_id, display_name),
        )
        db.commit()
    finally:
        db.close()

    logger.info(f"mirrored {filename} -> {knowledge_id}")
    return knowledge_id


def unmirror_file(letta_file_id):
    """删除文件后，清理 Knowledge Collection 镜像"""
    db = get_db()
    try:
        row = db.execute(
            "SELECT knowledge_id FROM knowledge_mirrors WHERE letta_file_id = ?",
            (letta_file_id,),
        ).fetchone()
        if not row:
            return
        knowledge_id = row["knowledge_id"]
        _api("DELETE", f"/api/v1/knowledge/{knowledge_id}/delete")
        db.execute("DELETE FROM knowledge_mirrors WHERE letta_file_id = ?", (letta_file_id,))
        db.commit()
        logger.info(f"unmirrored {letta_file_id}")
    finally:
        db.close()


def get_letta_file_id_by_knowledge(knowledge_id):
    """通过 Knowledge Collection ID 反查 Letta 文件 ID"""
    db = get_db()
    try:
        row = db.execute(
            "SELECT letta_file_id, letta_folder_id, scope, scope_id FROM knowledge_mirrors WHERE knowledge_id = ?",
            (knowledge_id,),
        ).fetchone()
        if row:
            return {
                "letta_file_id": row["letta_file_id"],
                "letta_folder_id": row["letta_folder_id"],
                "scope": row["scope"],
                "scope_id": row["scope_id"],
            }
    finally:
        db.close()
    return None


def reconcile_mirrors():
    """定时对账：确保 Letta Folder 和 Knowledge 镜像一致"""
    from routing import letta

    db = get_db()
    try:
        # 收集所有 Letta 文件
        letta_files = {}  # letta_file_id -> {folder_id, filename, scope, scope_id, project_name}

        # 个人文件夹
        personal_rows = db.execute(
            "SELECT user_id, personal_folder_id FROM user_cache WHERE personal_folder_id IS NOT NULL"
        ).fetchall()
        for row in personal_rows:
            try:
                files = list(getattr(
                    letta.folders.files.list(folder_id=row["personal_folder_id"]),
                    "items", letta.folders.files.list(folder_id=row["personal_folder_id"])
                ))
                for f in files:
                    fname = getattr(getattr(f, "source", None), "filename", None) or getattr(f, "file_name", "") or ""
                    letta_files[f.id] = {
                        "folder_id": row["personal_folder_id"],
                        "filename": fname,
                        "scope": "personal",
                        "scope_id": "",
                        "owner_id": row["user_id"],
                        "project_name": "",
                    }
            except Exception:
                pass

        # 项目文件夹
        projects = db.execute("SELECT project_id, name, project_folder_id FROM projects").fetchall()
        for proj in projects:
            try:
                files = list(getattr(
                    letta.folders.files.list(folder_id=proj["project_folder_id"]),
                    "items", letta.folders.files.list(folder_id=proj["project_folder_id"])
                ))
                for f in files:
                    fname = getattr(getattr(f, "source", None), "filename", None) or getattr(f, "file_name", "") or ""
                    letta_files[f.id] = {
                        "folder_id": proj["project_folder_id"],
                        "filename": fname,
                        "scope": "project",
                        "scope_id": proj["project_id"],
                        "owner_id": "",
                        "project_name": proj["name"],
                    }
            except Exception:
                pass

        # 组织文件夹
        org = db.execute("SELECT org_folder_id FROM org_resources WHERE singleton = 1").fetchone()
        if org and org["org_folder_id"]:
            try:
                files = list(getattr(
                    letta.folders.files.list(folder_id=org["org_folder_id"]),
                    "items", letta.folders.files.list(folder_id=org["org_folder_id"])
                ))
                for f in files:
                    fname = getattr(getattr(f, "source", None), "filename", None) or getattr(f, "file_name", "") or ""
                    letta_files[f.id] = {
                        "folder_id": org["org_folder_id"],
                        "filename": fname,
                        "scope": "org",
                        "scope_id": "",
                        "owner_id": "",
                        "project_name": "",
                    }
            except Exception:
                pass

        # 现有镜像
        existing = {row["letta_file_id"]: row["knowledge_id"]
                    for row in db.execute("SELECT letta_file_id, knowledge_id FROM knowledge_mirrors").fetchall()}

        # 补建缺失的
        for fid, info in letta_files.items():
            if fid not in existing:
                mirror_file(fid, info["folder_id"], info["filename"], info["scope"],
                            info["scope_id"], info["owner_id"], info["project_name"])

        # 删除多余的（Letta 里已经没有的文件）
        for fid, kid in existing.items():
            if fid not in letta_files:
                unmirror_file(fid)

        logger.info(f"reconcile_mirrors: {len(letta_files)} letta files, {len(existing)} existing mirrors")
    finally:
        db.close()
