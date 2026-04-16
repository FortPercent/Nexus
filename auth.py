"""认证模块 —— 聊天 API 和管理页面两条链路"""
import jwt
import httpx
from fastapi import Request, HTTPException
from config import (
    ADAPTER_API_KEY, OPENWEBUI_JWT_SECRET, ORG_ADMIN_EMAILS,
    OPENWEBUI_URL, OPENWEBUI_ADMIN_EMAIL, OPENWEBUI_ADMIN_PASSWORD,
)
from db import get_db

# Open WebUI admin token 缓存
_admin_token_cache = {"token": None}


def _login_openwebui_admin() -> str:
    """登录 Open WebUI 获取 admin token"""
    try:
        resp = httpx.post(
            f"{OPENWEBUI_URL}/api/v1/auths/signin",
            json={"email": OPENWEBUI_ADMIN_EMAIL, "password": OPENWEBUI_ADMIN_PASSWORD},
            timeout=5,
        )
        if resp.status_code == 200:
            _admin_token_cache["token"] = resp.json().get("token", "")
            return _admin_token_cache["token"]
    except Exception:
        pass
    return ""


def _get_openwebui_admin_token(force_refresh: bool = False) -> str:
    """获取 Open WebUI admin token（缓存，支持强制刷新）"""
    if _admin_token_cache["token"] and not force_refresh:
        return _admin_token_cache["token"]
    return _login_openwebui_admin()


def _admin_api_get(path: str) -> dict | None:
    """用 admin token 调 Open WebUI API，401/403 时自动刷新 token 重试一次"""
    token = _get_openwebui_admin_token()
    if not token:
        return None
    try:
        resp = httpx.get(
            f"{OPENWEBUI_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code in (401, 403):
            token = _get_openwebui_admin_token(force_refresh=True)
            if not token:
                return None
            resp = httpx.get(
                f"{OPENWEBUI_URL}{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def extract_user_from_chat(request: Request, body: dict) -> dict:
    """聊天 API 认证：API Key 校验来源 + body 中的 user_id"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != ADAPTER_API_KEY:
        raise HTTPException(401, "Invalid API key")

    # Open WebUI 有时在 body["user"] 里传 user_id，有时不传
    user_id = body.get("user_id") or body.get("user")

    # 如果都没有，尝试从 request header 里找
    if not user_id:
        user_id = request.headers.get("x-openwebui-user-id") or request.headers.get("x-user-id")

    if not user_id:
        raise HTTPException(401, "Missing user identity")

    name = body.get("user_name", "")
    email = body.get("user_email", "")

    # 如果 body 里没有 name/email，从缓存或 Open WebUI API 查
    if not name or not email:
        db = get_db()
        cached = db.execute(
            "SELECT name, email FROM user_cache WHERE user_id = ?", (user_id,)
        ).fetchone()
        if cached and cached["name"]:
            name = cached["name"]
            email = cached["email"] or ""
        else:
            # 从 Open WebUI API 查
            u = _admin_api_get(f"/api/v1/users/{user_id}")
            if u:
                name = u.get("name", "")
                email = u.get("email", "")
        db.close()

    user = {"id": user_id, "name": name, "email": email, "role": body.get("user_role", "user")}

    # 写入用户缓存
    if name or email:
        db = get_db()
        db.execute(
            "INSERT OR REPLACE INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
            (user["id"], user["name"], user["email"]),
        )
        db.commit()
        db.close()

    return user


def extract_user_from_admin(request: Request) -> dict:
    """管理页面认证：共享 Open WebUI 的 JWT Secret，再查用户详情"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        raise HTTPException(401, "请先登录 Open WebUI")
    try:
        payload = jwt.decode(token, OPENWEBUI_JWT_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(401, "JWT 无效或过期，请重新登录 Open WebUI")

    user_id = payload["id"]

    # Open WebUI JWT 只有 id，需要查用户详情拿 name/email
    # 先查缓存
    db = get_db()
    cached = db.execute(
        "SELECT name, email FROM user_cache WHERE user_id = ?", (user_id,)
    ).fetchone()
    if cached and cached["email"]:
        db.close()
        return {"id": user_id, "name": cached["name"], "email": cached["email"], "role": "user"}

    # 缓存没有，用 admin token 从 Open WebUI 查
    u = _admin_api_get(f"/api/v1/users/{user_id}")
    if u:
        name = u.get("name", "")
        email = u.get("email", "")
        db.execute(
            "INSERT OR REPLACE INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
            (user_id, name, email),
        )
        db.commit()
        db.close()
        return {"id": user_id, "name": name, "email": email, "role": u.get("role", "user")}

    db.close()
    return {"id": user_id, "name": "", "email": "", "role": "user"}


def get_current_user(request: Request, body: dict = None) -> dict:
    """统一认证入口。注意：/v1/models 不需要用户身份，单独处理。"""
    path = request.url.path
    if path.startswith("/v1/"):
        return extract_user_from_chat(request, body)
    elif path.startswith("/admin/"):
        return extract_user_from_admin(request)
    else:
        raise HTTPException(404)


def require_project_member(request: Request, project_id: str) -> dict:
    user = extract_user_from_admin(request)
    db = get_db()
    row = db.execute(
        "SELECT role FROM project_members WHERE user_id = ? AND project_id = ?",
        (user["id"], project_id),
    ).fetchone()
    db.close()
    if not row:
        raise HTTPException(403, "你不是该项目的成员")
    return user


def require_project_admin(request: Request, project_id: str) -> dict:
    user = extract_user_from_admin(request)
    db = get_db()
    row = db.execute(
        "SELECT role FROM project_members WHERE user_id = ? AND project_id = ? AND role = 'admin'",
        (user["id"], project_id),
    ).fetchone()
    db.close()
    if not row:
        raise HTTPException(403, "需要项目管理员权限")
    return user


def require_org_admin(request: Request) -> dict:
    user = extract_user_from_admin(request)
    if user.get("email") not in ORG_ADMIN_EMAILS:
        raise HTTPException(403, "需要组织管理员权限")
    return user
