"""Letta 文件 → Open WebUI Knowledge Collection 镜像同步

只同步元数据（名字），不同步正文。
镜像创建在对应用户名下，确保 # 下拉可见。
正文在聊天时由 Adapter 从 Letta 实时检索。
"""
import logging
import httpx
import jwt

from config import OPENWEBUI_URL, OPENWEBUI_ADMIN_EMAIL, OPENWEBUI_ADMIN_PASSWORD, OPENWEBUI_JWT_SECRET
from db import get_db

logger = logging.getLogger(__name__)

_admin_token_cache = {"token": None}


def _get_admin_token(force_refresh=False):
    if _admin_token_cache["token"] and not force_refresh:
        return _admin_token_cache["token"]
    try:
        resp = httpx.post(
            f"{OPENWEBUI_URL}/api/v1/auths/signin",
            json={"email": OPENWEBUI_ADMIN_EMAIL, "password": OPENWEBUI_ADMIN_PASSWORD},
            timeout=10,
        )
        if resp.status_code == 200:
            _admin_token_cache["token"] = resp.json().get("token", "")
            return _admin_token_cache["token"]
    except Exception as e:
        logger.error(f"admin login failed: {e}")
    return ""


def _make_user_token(user_id):
    """生成用户的 JWT token（和 Open WebUI 共享 secret，需要 jti 字段）"""
    import uuid
    return jwt.encode({"id": user_id, "jti": str(uuid.uuid4())}, OPENWEBUI_JWT_SECRET, algorithm="HS256")


def _api(method, path, json_data=None, token=None):
    if not token:
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


def mirror_file_for_user(letta_file_id, letta_folder_id, filename, scope, scope_id, user_id, project_name=""):
    """为某个用户创建一份镜像 Knowledge Collection"""
    display_name = _make_display_name(filename, scope, project_name)

    # 用 admin token 创建，然后直接在 SQLite 里改 user_id
    result = _api("POST", "/api/v1/knowledge/create", {
        "name": display_name,
        "description": f"letta-mirror:{letta_file_id}",
    })

    if not result:
        return None

    knowledge_id = result.get("id", "")

    # 直接改 Open WebUI SQLite，把 user_id 改成目标用户
    try:
        import sqlite3
        conn = sqlite3.connect("/data/open-webui/webui.db", timeout=5)
        conn.execute("UPDATE knowledge SET user_id = ? WHERE id = ?", (user_id, knowledge_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"failed to update knowledge user_id, rolling back: {e}")
        # 回滚：删除刚创建的 Knowledge Collection，不写本地映射
        _api("DELETE", f"/api/v1/knowledge/{knowledge_id}/delete")
        return None

    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO knowledge_mirrors "
            "(letta_file_id, letta_folder_id, knowledge_id, scope, scope_id, owner_id, display_name, for_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (letta_file_id, letta_folder_id, knowledge_id, scope, scope_id, user_id, display_name, user_id),
        )
        db.commit()
    finally:
        db.close()

    return knowledge_id


def mirror_file(letta_file_id, letta_folder_id, filename, scope, scope_id="", owner_id="", project_name=""):
    """为所有应该看到这个文件的用户创建镜像"""
    db = get_db()
    try:
        if scope == "personal":
            # 个人文件：只给文件所有者
            if owner_id:
                mirror_file_for_user(letta_file_id, letta_folder_id, filename, scope, scope_id, owner_id, project_name)
        elif scope == "project":
            # 项目文件：给所有项目成员
            members = db.execute(
                "SELECT user_id FROM project_members WHERE project_id = ?", (scope_id,)
            ).fetchall()
            for m in members:
                mirror_file_for_user(letta_file_id, letta_folder_id, filename, scope, scope_id, m["user_id"], project_name)
        elif scope == "org":
            # 组织文件：给所有用户
            import sqlite3
            webui_conn = sqlite3.connect("/data/open-webui/webui.db", timeout=5)
            webui_conn.row_factory = sqlite3.Row
            users = webui_conn.execute("SELECT id FROM user").fetchall()
            webui_conn.close()
            for u in users:
                mirror_file_for_user(letta_file_id, letta_folder_id, filename, scope, scope_id, u["id"], project_name)
    finally:
        db.close()

    logger.info(f"mirrored {filename} ({scope})")


def unmirror_file(letta_file_id):
    """删除某个文件的所有镜像"""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT knowledge_id, for_user_id FROM knowledge_mirrors WHERE letta_file_id = ?",
            (letta_file_id,),
        ).fetchall()
        admin_token = _get_admin_token()
        for row in rows:
            _api("DELETE", f"/api/v1/knowledge/{row['knowledge_id']}/delete", token=admin_token)
        db.execute("DELETE FROM knowledge_mirrors WHERE letta_file_id = ?", (letta_file_id,))
        db.commit()
        logger.info(f"unmirrored {letta_file_id} ({len(rows)} copies)")
    finally:
        db.close()


def get_letta_file_id_by_knowledge(knowledge_id):
    """通过 Knowledge Collection ID 反查 Letta 文件 ID"""
    db = get_db()
    try:
        row = db.execute(
            "SELECT letta_file_id, letta_folder_id, scope, scope_id, display_name FROM knowledge_mirrors WHERE knowledge_id = ?",
            (knowledge_id,),
        ).fetchone()
        if row:
            return {
                "letta_file_id": row["letta_file_id"],
                "letta_folder_id": row["letta_folder_id"],
                "scope": row["scope"],
                "scope_id": row["scope_id"],
                "display_name": row["display_name"],
            }
    finally:
        db.close()
    return None


def _get_file_name(f):
    source = getattr(f, "source", None)
    if source and getattr(source, "filename", None):
        return source.filename
    return getattr(f, "file_name", "") or getattr(f, "original_file_name", "") or ""


def _list_folder_files(letta, folder_id):
    try:
        page = letta.folders.files.list(folder_id=folder_id)
        return list(getattr(page, "items", page))
    except Exception:
        return []


def reconcile_mirrors():
    """全量对账：确保 Letta 文件和镜像一致"""
    from routing import letta

    db = get_db()
    try:
        # 收集所有 Letta 文件 + 应该看到它们的用户
        # {letta_file_id: {folder_id, filename, scope, scope_id, user_ids, project_name}}
        letta_files = {}

        # 个人文件（先补填缺失的 personal_folder_id）
        from routing import get_or_create_personal_folder
        missing_folder_users = db.execute(
            "SELECT user_id FROM user_cache WHERE personal_folder_id IS NULL OR personal_folder_id = ''"
        ).fetchall()
        for u in missing_folder_users:
            try:
                fid = get_or_create_personal_folder(u["user_id"])
                db.execute("UPDATE user_cache SET personal_folder_id = ? WHERE user_id = ?", (fid, u["user_id"]))
            except Exception:
                pass
        if missing_folder_users:
            db.commit()

        personal_rows = db.execute(
            "SELECT user_id, personal_folder_id FROM user_cache WHERE personal_folder_id IS NOT NULL AND personal_folder_id != ''"
        ).fetchall()
        for row in personal_rows:
            for f in _list_folder_files(letta, row["personal_folder_id"]):
                letta_files[f.id] = {
                    "folder_id": row["personal_folder_id"],
                    "filename": _get_file_name(f),
                    "scope": "personal", "scope_id": "",
                    "user_ids": [row["user_id"]],
                    "project_name": "",
                }

        # 项目文件
        projects = db.execute("SELECT project_id, name, project_folder_id FROM projects").fetchall()
        for proj in projects:
            members = [r["user_id"] for r in db.execute(
                "SELECT user_id FROM project_members WHERE project_id = ?", (proj["project_id"],)
            ).fetchall()]
            for f in _list_folder_files(letta, proj["project_folder_id"]):
                letta_files[f.id] = {
                    "folder_id": proj["project_folder_id"],
                    "filename": _get_file_name(f),
                    "scope": "project", "scope_id": proj["project_id"],
                    "user_ids": members,
                    "project_name": proj["name"],
                }

        # 组织文件
        org = db.execute("SELECT org_folder_id FROM org_resources WHERE singleton = 1").fetchone()
        if org and org["org_folder_id"]:
            import sqlite3
            webui_conn = sqlite3.connect("/data/open-webui/webui.db", timeout=5)
            webui_conn.row_factory = sqlite3.Row
            all_users = [r["id"] for r in webui_conn.execute("SELECT id FROM user").fetchall()]
            webui_conn.close()
            for f in _list_folder_files(letta, org["org_folder_id"]):
                letta_files[f.id] = {
                    "folder_id": org["org_folder_id"],
                    "filename": _get_file_name(f),
                    "scope": "org", "scope_id": "",
                    "user_ids": all_users,
                    "project_name": "",
                }

        # 现有镜像 {(letta_file_id, for_user_id): knowledge_id}
        existing = {}
        for row in db.execute("SELECT letta_file_id, for_user_id, knowledge_id FROM knowledge_mirrors").fetchall():
            existing[(row["letta_file_id"], row["for_user_id"])] = row["knowledge_id"]

        # 补建缺失的
        created = 0
        for fid, info in letta_files.items():
            for uid in info["user_ids"]:
                if (fid, uid) not in existing:
                    mirror_file_for_user(fid, info["folder_id"], info["filename"],
                                         info["scope"], info["scope_id"], uid, info["project_name"])
                    created += 1

        # 删除多余的（文件已删或用户已移出项目）
        deleted = 0
        admin_token = _get_admin_token()
        for (fid, uid), kid in existing.items():
            if fid not in letta_files or uid not in letta_files[fid]["user_ids"]:
                _api("DELETE", f"/api/v1/knowledge/{kid}/delete", token=admin_token)
                db.execute("DELETE FROM knowledge_mirrors WHERE knowledge_id = ?", (kid,))
                deleted += 1
        if deleted:
            db.commit()

        logger.info(f"reconcile_mirrors: {len(letta_files)} files, created {created}, deleted {deleted}")
    finally:
        db.close()
