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
    if path.startswith("/v1/") or path.startswith("/v2/"):
        return await extract_user_from_chat(request, body)
    elif path.startswith("/admin/"):
        return await extract_user_from_admin(request)
    else:
        raise HTTPException(404)


# Issue #14 Day 2 (2026-05-05): 鉴权走 org_tree.can_user_access_project_async,
# 自动覆盖 project_members 直接授权 + project_orgs 跨组织/递归继承授权.
# 旧 SQL 直查 project_members 路径作 fallback (避免 org_tree 模块循环 import / 启动竞态).
_ADMIN_LEVELS = {"admin", "owner"}
_WRITE_LEVELS = {"admin", "owner", "shared_write", "member"}
_READ_LEVELS = {"admin", "owner", "shared_write", "shared_read", "member"}


async def _get_user_project_access(user_id: str, project_id: str):
    """返 access_level 字符串 or None. 复用 org_tree LRU cache."""
    try:
        from org_tree import can_user_access_project_async
        return await can_user_access_project_async(user_id, project_id)
    except ImportError:
        # fallback: org_tree 没装 → 走老逻辑直查 project_members
        async with use_db_async() as db:
            async with db.execute(
                "SELECT role FROM project_members WHERE user_id = ? AND project_id = ?",
                (user_id, project_id),
            ) as cur:
                row = await cur.fetchone()
        return row["role"] if row else None


async def require_project_member(request: Request, project_id: str) -> dict:
    """读权限. 命中 project_members / project_orgs (递归祖先 org) 任一即可."""
    user = await extract_user_from_admin(request)
    lvl = await _get_user_project_access(user["id"], project_id)
    if lvl not in _READ_LEVELS:
        raise HTTPException(403, "你不是该项目的成员")
    return user


async def require_project_admin(request: Request, project_id: str) -> dict:
    """admin 权限. project_members.role='admin' / project_orgs.access_level in (admin/owner) 任一即可."""
    user = await extract_user_from_admin(request)
    lvl = await _get_user_project_access(user["id"], project_id)
    if lvl not in _ADMIN_LEVELS:
        raise HTTPException(403, "需要项目管理员权限")
    return user


async def require_project_write(request: Request, project_id: str) -> dict:
    """写权限. project_orgs shared_write 也算 (Day 2 新增, 现有代码可不用)."""
    user = await extract_user_from_admin(request)
    lvl = await _get_user_project_access(user["id"], project_id)
    if lvl not in _WRITE_LEVELS:
        raise HTTPException(403, "需要项目写入权限")
    return user


async def require_org_admin(request: Request) -> dict:
    user = await extract_user_from_admin(request)
    if user.get("email") not in ORG_ADMIN_EMAILS:
        raise HTTPException(403, "需要组织管理员权限")
    return user
