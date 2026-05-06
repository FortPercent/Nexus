"""多组织树 (Issue #14) 权限解析 + 迁移辅助.

设计: docs/multi-org-tree-design.md
迁移策略 A (一刀切): 建 root org "AI 研究院", 所有现有 project / user 挂 root.

核心 API:
  resolve_user_projects_async(user_id) → set[(project_id, access_level)]
    用户能访问的 project 集合 (递归祖先 org + 直接 project_members 并集)
  resolve_project_users_async(project_id) → set[user_id]
    project 可见用户集合 (project_orgs 下行 + project_members 直接)
  ensure_root_org_migration() → str
    幂等迁移: 建 root org + 拷贝所有 project / user 进 project_orgs / org_members
    返 root_org_id

权限解析在 hot path, LRU 缓存 5min (org 树变化频率低).
"""
from __future__ import annotations

import functools
import logging
import time
import uuid
from typing import Optional

from db import use_db, use_db_async


ROOT_ORG_CODE = "ai-infra-root"
ROOT_ORG_NAME = "中国电信人工智能研究院"


# --------- 缓存 ---------
# (user_id, project_id) → (access_level, expire_ts) 简单 LRU+TTL,
# 不上 functools.lru_cache 因为要支持 invalidate.
_PERM_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_CACHE_TTL = 300  # 5min
_CACHE_MAX = 5000


def _cache_get(user_id: str, project_id: str) -> Optional[str]:
    key = (user_id, project_id)
    v = _PERM_CACHE.get(key)
    if v is None:
        return None
    access, expire = v
    if time.time() > expire:
        _PERM_CACHE.pop(key, None)
        return None
    return access


def _cache_put(user_id: str, project_id: str, access: str) -> None:
    if len(_PERM_CACHE) > _CACHE_MAX:
        # 简单 fifo 驱逐: 删一半 (不算法严谨但够用)
        for k in list(_PERM_CACHE.keys())[: _CACHE_MAX // 2]:
            _PERM_CACHE.pop(k, None)
    _PERM_CACHE[(user_id, project_id)] = (access, time.time() + _CACHE_TTL)


def invalidate_cache(user_id: Optional[str] = None) -> None:
    """org 树 / 成员关系变更时调用. 不传 user_id 清全部."""
    if user_id is None:
        _PERM_CACHE.clear()
        return
    for k in list(_PERM_CACHE.keys()):
        if k[0] == user_id:
            _PERM_CACHE.pop(k, None)


# --------- 核心 SQL ---------
# 递归 CTE: 用户所属 org → 所有祖先 org → project_orgs 命中
# UNION 上 project_members 直接成员授权
_RESOLVE_USER_PROJECTS_SQL = """
WITH RECURSIVE
  user_orgs(org_id) AS (
    SELECT org_id FROM org_members WHERE user_id = ?
  ),
  ancestor_orgs(org_id) AS (
    SELECT org_id FROM user_orgs
    UNION
    SELECT o.parent_id FROM organizations o
      JOIN ancestor_orgs a ON o.id = a.org_id
      WHERE o.parent_id IS NOT NULL
  )
SELECT DISTINCT po.project_id, po.access_level
  FROM project_orgs po
  JOIN ancestor_orgs ao ON po.org_id = ao.org_id

UNION

SELECT pm.project_id, pm.role AS access_level
  FROM project_members pm
  WHERE pm.user_id = ?
"""

# 反向: project 可见用户 (org_orgs 下行 + project_members)
_RESOLVE_PROJECT_USERS_SQL = """
WITH RECURSIVE descendant_orgs(org_id) AS (
  SELECT org_id FROM project_orgs WHERE project_id = ?
  UNION
  SELECT o.id FROM organizations o
    JOIN descendant_orgs d ON o.parent_id = d.org_id
)
SELECT DISTINCT om.user_id
  FROM org_members om
  JOIN descendant_orgs d ON om.org_id = d.org_id

UNION

SELECT pm.user_id
  FROM project_members pm
  WHERE pm.project_id = ?
"""


async def resolve_user_projects_async(user_id: str) -> dict[str, str]:
    """返 {project_id: access_level} dict. 多源同 project 时取最强 access_level."""
    rank = {"shared_read": 1, "member": 1, "shared_write": 2, "admin": 3, "owner": 3}
    out: dict[str, str] = {}
    async with use_db_async() as db:
        async with db.execute(_RESOLVE_USER_PROJECTS_SQL, (user_id, user_id)) as cur:
            rows = await cur.fetchall()
    for r in rows:
        pid, lvl = r["project_id"], r["access_level"]
        if pid not in out or rank.get(lvl, 0) > rank.get(out[pid], 0):
            out[pid] = lvl
    return out


async def can_user_access_project_async(user_id: str, project_id: str) -> Optional[str]:
    """返 access_level 或 None. 命中 LRU cache."""
    cached = _cache_get(user_id, project_id)
    if cached is not None:
        return cached if cached != "__none__" else None

    perms = await resolve_user_projects_async(user_id)
    lvl = perms.get(project_id)
    _cache_put(user_id, project_id, lvl or "__none__")
    return lvl


async def resolve_project_users_async(project_id: str) -> set[str]:
    async with use_db_async() as db:
        async with db.execute(_RESOLVE_PROJECT_USERS_SQL, (project_id, project_id)) as cur:
            rows = await cur.fetchall()
    return {r["user_id"] for r in rows}


# --------- Day 3: org_block 链解析 ---------
# 用户所属 org 的祖先链 (含自身), 按 root → leaf 顺序返 letta_block_id 列表.
# routing.py 创建 agent 时把这条链拼进 block_ids, 让 agent 系统提示按部门
# 上下文层级展开: 上层组织通用知识 (root 部门 block) 靠前, 具体部门特定知识 (leaf
# block) 靠后. 多源 (用户挂多个不连通 org) 时 union 去重.
#
# depth 语义: 用户直接挂的 org 是 depth=0, 父 org depth=1, 祖父 depth=2, ... root 最大.
# 注入顺序按 depth 倒序 (root → leaf), root 在最前.
_USER_ORG_BLOCK_CHAIN_SQL = """
WITH RECURSIVE
  user_orgs(org_id, depth) AS (
    SELECT org_id, 0 FROM org_members WHERE user_id = ?
    UNION
    SELECT o.parent_id, uo.depth + 1
      FROM organizations o
      JOIN user_orgs uo ON o.id = uo.org_id
      WHERE o.parent_id IS NOT NULL
  ),
  max_depth_per_org AS (
    SELECT org_id, MAX(depth) AS d FROM user_orgs GROUP BY org_id
  )
SELECT o.id AS org_id, o.letta_block_id, md.d AS depth
  FROM max_depth_per_org md
  JOIN organizations o ON o.id = md.org_id
  WHERE o.letta_block_id IS NOT NULL AND o.letta_block_id != ''
  ORDER BY md.d DESC, o.id
"""


async def get_user_org_block_chain_async(user_id: str) -> list[str]:
    """返用户所属 org 链上所有 letta_block_id, root → leaf 顺序.

    用法: routing.py 建 agent 时:
        org_blocks = await get_user_org_block_chain_async(user_id)
        block_ids = [human_block_id, *org_blocks, project_block_id]

    Letta agent.create 接受 block_ids 列表, 按列表顺序编进 system prompt.
    顺序约定:
      [user 私人 human]  →  [org root]  →  ...  →  [org leaf]  →  [project]
    """
    async with use_db_async() as db:
        async with db.execute(_USER_ORG_BLOCK_CHAIN_SQL, (user_id,)) as cur:
            rows = await cur.fetchall()
    return [r["letta_block_id"] for r in rows]


def get_user_org_block_chain_sync(user_id: str) -> list[str]:
    """同步版, 给 routing.py 等 sync 上下文用.

    routing.py 的 get_or_create_agent 是 sync 函数 (sqlite3 直查), agent.create
    也是 sync letta SDK, 所以这里同 db 风格 sync 查.
    """
    from db import get_db
    db = get_db()
    try:
        rows = db.execute(_USER_ORG_BLOCK_CHAIN_SQL, (user_id,)).fetchall()
        return [r["letta_block_id"] for r in rows]
    finally:
        db.close()


def set_org_letta_block(org_id: str, letta_block_id: Optional[str]) -> None:
    """admin 用: 给 org 绑定 / 解绑 letta block (Day 4 admin UI 调).

    传 None 解绑. block 自身的创建 / 内容 update 走 letta SDK, 不在这一层管.
    """
    with use_db() as db:
        db.execute(
            "UPDATE organizations SET letta_block_id = ? WHERE id = ?",
            (letta_block_id, org_id),
        )
    # block 变更不影响 (user, project) 权限链, 但保险清缓存
    invalidate_cache()


# --------- 迁移 ---------
def ensure_root_org_migration() -> str:
    """幂等: 建 root org + 所有用户挂 root org_members.

    *不*自动把 project 挂 root_org_id — 那会让 root org 全体成员 (= 所有用户) 都
    能访问所有 project, 破坏原 project_members 严格隔离. project 该挂哪个部门
    由 admin 后续 UI 手动配 (Day 4-5).

    所以现有授权语义保持不变:
      - project_members 直接成员 (admin 创建 project 时自动 add) — 主要授权
      - project_orgs (递归祖先 org) — 后续 admin 配置才生效, 默认空

    安全: 只 INSERT OR IGNORE, 重跑无副作用. 返 root_org_id.
    """
    with use_db() as db:
        cur = db.execute("SELECT id FROM organizations WHERE code = ?", (ROOT_ORG_CODE,))
        row = cur.fetchone()
        if row:
            root_id = row["id"]
        else:
            root_id = "org-" + uuid.uuid4().hex[:12]
            db.execute(
                "INSERT INTO organizations (id, parent_id, name, code, org_type) VALUES (?, NULL, ?, ?, 'bureau')",
                (root_id, ROOT_ORG_NAME, ROOT_ORG_CODE),
            )
            logging.info(f"[org_tree] created root org {root_id} ({ROOT_ORG_NAME})")

        user_rows = db.execute("SELECT user_id FROM user_cache").fetchall()
        n_user = 0
        for u in user_rows:
            cur = db.execute(
                "INSERT OR IGNORE INTO org_members (org_id, user_id, role) VALUES (?, ?, 'member')",
                (root_id, u["user_id"]),
            )
            n_user += cur.rowcount

        if n_user:
            logging.info(f"[org_tree] migration: +{n_user} org_members to root")
        invalidate_cache()
    return root_id
