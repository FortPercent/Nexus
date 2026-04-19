"""认证模块 —— 聊天 API 和管理页面两条链路。

2026-04-19 改造：`extract_user_from_admin` + `require_project_*` 全部改 async，
内部 DB 调用走 `use_db_async`，消除 event loop 阻塞。
同步版本保留（`*_sync` 后缀）供非 async 调用方使用。
"""
import asyncio
import logging
import jwt
import httpx
from fastapi import Request, HTTPException
from config import (
    ADAPTER_API_KEY, OPENWEBUI_JWT_SECRET, ORG_ADMIN_EMAILS,
    OPENWEBUI_URL, OPENWEBUI_ADMIN_EMAIL, OPENWEBUI_ADMIN_PASSWORD,
)
from db import use_db, use_db_async

_admin_token_cache = {"token": None}


def _login_openwebui_admin() -> str:
    try:
        resp = httpx.post(
            f"{OPENWEBUI_URL}/api/v1/auths/signin",
            json={"email": OPENWEBUI_ADMIN_EMAIL, "password": OPENWEBUI_ADMIN_PASSWORD},
            timeout=5,
        )
        if resp.status_code == 200:
            _admin_token_cache["token"] = resp.json().get("token", "")
            return _admin_token_cache["token"]
    except Exception as e:
        logging.warning(f"admin login failed: {e}")
    return ""


def _get_openwebui_admin_token(force_refresh: bool = False) -> str:
    if _admin_token_cache["token"] and not force_refresh:
        return _admin_token_cache["token"]
    return _login_openwebui_admin()


def _admin_api_get(path: str) -> dict | None:
    """同步实现；async 调用方用 asyncio.to_thread 包一层，保留这里不重写，
    因为只在 user_cache miss 时才走，是冷路径。"""
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
        logging.warning(f"admin API GET {path} returned {resp.status_code}")
    except Exception as e:
        logging.warning(f"admin API GET {path} failed: {e}")
    return None


async def extract_user_from_chat(request: Request, body: dict) -> dict:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != ADAPTER_API_KEY:
        raise HTTPException(401, "Invalid API key")

    user_id = body.get("user_id") or body.get("user")
    if not user_id:
        user_id = request.headers.get("x-openwebui-user-id") or request.headers.get("x-user-id")
    if not user_id:
        raise HTTPException(401, "Missing user identity")

    name = body.get("user_name", "")
    email = body.get("user_email", "")

    if not name or not email:
        async with use_db_async() as db:
            async with db.execute(
                "SELECT name, email FROM user_cache WHERE user_id = ?", (user_id,)
            ) as cur:
                cached = await cur.fetchone()
        if cached and cached["name"]:
            name = cached["name"]
            email = cached["email"] or ""
        else:
            u = await asyncio.to_thread(_admin_api_get, f"/api/v1/users/{user_id}")
            if u:
                name = u.get("name", "")
                email = u.get("email", "")

    user = {"id": user_id, "name": name, "email": email, "role": body.get("user_role", "user")}

    # 只在首次见到该用户时 insert；已存在就不覆盖，防止脚本/测试传进来的假名字污染真实姓名
    if name or email:
        async with use_db_async() as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
                (user["id"], user["name"], user["email"]),
            )

    return user


async def extract_user_from_admin(request: Request) -> dict:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        raise HTTPException(401, "请先登录 Open WebUI")
    try:
        payload = jwt.decode(token, OPENWEBUI_JWT_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(401, "JWT 无效或过期，请重新登录 Open WebUI")

    user_id = payload["id"]

    async with use_db_async() as db:
        async with db.execute(
            "SELECT name, email FROM user_cache WHERE user_id = ?", (user_id,)
        ) as cur:
            cached = await cur.fetchone()

    if cached and cached["email"]:
        return {"id": user_id, "name": cached["name"], "email": cached["email"], "role": "user"}

    u = await asyncio.to_thread(_admin_api_get, f"/api/v1/users/{user_id}")
    if u:
        name = u.get("name", "")
        email = u.get("email", "")
        async with use_db_async() as db:
            await db.execute(
                "INSERT OR REPLACE INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
                (user_id, name, email),
            )
        return {"id": user_id, "name": name, "email": email, "role": u.get("role", "user")}

    # 2026-04-19: JWT 签名合法但 user_id 在 Open WebUI 里查不到 → 401
    # 防止有 secret 的人伪造任意 user_id 访问管理 API
    raise HTTPException(401, "用户不存在或已被删除，请重新登录 Open WebUI")


async def get_current_user(request: Request, body: dict = None) -> dict:
    path = request.url.path
    if path.startswith("/v1/"):
        return await extract_user_from_chat(request, body)
    elif path.startswith("/admin/"):
        return await extract_user_from_admin(request)
    else:
        raise HTTPException(404)


async def require_project_member(request: Request, project_id: str) -> dict:
    user = await extract_user_from_admin(request)
    async with use_db_async() as db:
        async with db.execute(
            "SELECT role FROM project_members WHERE user_id = ? AND project_id = ?",
            (user["id"], project_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(403, "你不是该项目的成员")
    return user


async def require_project_admin(request: Request, project_id: str) -> dict:
    user = await extract_user_from_admin(request)
    async with use_db_async() as db:
        async with db.execute(
            "SELECT role FROM project_members WHERE user_id = ? AND project_id = ? AND role = 'admin'",
            (user["id"], project_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(403, "需要项目管理员权限")
    return user


async def require_org_admin(request: Request) -> dict:
    user = await extract_user_from_admin(request)
    if user.get("email") not in ORG_ADMIN_EMAILS:
        raise HTTPException(403, "需要组织管理员权限")
    return user
