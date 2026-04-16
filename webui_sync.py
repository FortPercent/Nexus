"""同步适配层项目成员到 Open WebUI 模型权限（V2: API 版）

通过 Open WebUI 的 Model API 控制模型可见性，不再直连 SQLite。
要求 Open WebUI >= 0.8.x（支持 /api/v1/models/model/access/update）。
"""
import logging
import httpx

from config import OPENWEBUI_URL, OPENWEBUI_ADMIN_EMAIL, OPENWEBUI_ADMIN_PASSWORD

logger = logging.getLogger(__name__)

# Admin token 缓存
_token_cache = {"token": None}


def _get_admin_token(force_refresh: bool = False) -> str:
    """获取 Open WebUI admin token，缓存 + 自动刷新。"""
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
        logger.error(f"login Open WebUI failed: {e}")
    return ""


def _api_call(method: str, path: str, json_data: dict = None) -> dict | None:
    """调 Open WebUI API，401/403 时自动刷新 token 重试一次。"""
    token = _get_admin_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if method == "GET":
            resp = httpx.get(f"{OPENWEBUI_URL}{path}", headers=headers, timeout=10)
        else:
            resp = httpx.post(f"{OPENWEBUI_URL}{path}", headers=headers, json=json_data, timeout=10)
        if resp.status_code in (401, 403):
            token = _get_admin_token(force_refresh=True)
            if not token:
                return None
            headers = {"Authorization": f"Bearer {token}"}
            if method == "GET":
                resp = httpx.get(f"{OPENWEBUI_URL}{path}", headers=headers, timeout=10)
            else:
                resp = httpx.post(f"{OPENWEBUI_URL}{path}", headers=headers, json=json_data, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"API {method} {path} returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"API {method} {path} failed: {e}")
    return None


def _get_model_grants(model_id: str) -> list | None:
    """获取模型当前的 access_grants 列表。"""
    data = _api_call("GET", "/api/v1/models")
    if not data:
        return None
    for m in data.get("data", []):
        if m["id"] == model_id:
            return m.get("info", {}).get("access_grants", [])
    return None  # 模型不存在


def _update_model_grants(model_id: str, grants: list) -> bool:
    """全量覆盖模型的 access_grants。
    注意：如果模型不存在，此 API 会自动创建模型。调用前应确认模型存在。"""
    result = _api_call("POST", "/api/v1/models/model/access/update", {
        "id": model_id,
        "access_grants": grants,
    })
    return result is not None


# ===== 增量操作 =====


def grant_model_access(user_id: str, model_id: str):
    """给用户授予模型 read 权限（增量添加，不影响其他用户的 grant）。"""
    current = _get_model_grants(model_id)
    if current is None:
        raise RuntimeError(f"model {model_id} not found or API unreachable")
    for g in current:
        if g["principal_type"] == "user" and g["principal_id"] == user_id and g["permission"] == "read":
            return  # 已存在
    new_grants = [
        {"principal_type": g["principal_type"], "principal_id": g["principal_id"], "permission": g["permission"]}
        for g in current
    ]
    new_grants.append({"principal_type": "user", "principal_id": user_id, "permission": "read"})
    if not _update_model_grants(model_id, new_grants):
        raise RuntimeError(f"failed to update grants for {model_id}")


def revoke_model_access(user_id: str, model_id: str):
    """撤销用户的模型 read 权限。"""
    current = _get_model_grants(model_id)
    if current is None:
        raise RuntimeError(f"model {model_id} not found or API unreachable")
    new_grants = [
        {"principal_type": g["principal_type"], "principal_id": g["principal_id"], "permission": g["permission"]}
        for g in current
        if not (g["principal_type"] == "user" and g["principal_id"] == user_id and g["permission"] == "read")
    ]
    if not _update_model_grants(model_id, new_grants):
        raise RuntimeError(f"failed to update grants for {model_id}")


def revoke_all_model_access(model_id: str):
    """清空模型的所有 read 权限。用于删除项目时清理。"""
    if not _update_model_grants(model_id, []):
        raise RuntimeError(f"failed to clear grants for {model_id}")


# ===== 全量对账 =====


def reconcile_common_model(model_id: str = "qwen-no-mem"):
    """对账通用模型：设为全员公开（通配符 *）。
    一条 grant 搞定，新用户自动可见，不再需要逐用户扫描。"""
    if not _update_model_grants(model_id, [
        {"principal_type": "user", "principal_id": "*", "permission": "read"},
    ]):
        raise RuntimeError(f"failed to reconcile common model {model_id}")
    logger.info(f"reconcile_common_model: set {model_id} to public")


def reconcile_project_model(project_id: str, model_id: str, member_user_ids: list):
    """对账项目模型：access_grants 全量覆盖为成员列表。"""
    grants = [
        {"principal_type": "user", "principal_id": uid, "permission": "read"}
        for uid in member_user_ids
    ]
    if not _update_model_grants(model_id, grants):
        raise RuntimeError(f"failed to reconcile project model {model_id}")
    logger.info(f"reconcile_project_model: synced {len(member_user_ids)} members for {model_id}")


def reconcile_all():
    """全量对账入口：同步所有通用模型 + 所有项目模型。"""
    from db import get_db

    reconcile_common_model("qwen-no-mem")

    adapter_db = get_db()
    projects = adapter_db.execute("SELECT project_id FROM projects").fetchall()
    for proj in projects:
        pid = proj["project_id"]
        members = adapter_db.execute(
            "SELECT user_id FROM project_members WHERE project_id = ?", (pid,)
        ).fetchall()
        member_ids = [m["user_id"] for m in members]
        reconcile_project_model(pid, f"letta-{pid}", member_ids)
    adapter_db.close()
