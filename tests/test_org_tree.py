"""End-to-end tests for org_tree (Issue #14 Day 1).

测真路径: 真 init_db + 真 SQLite + 真递归 CTE + 真权限解析 + 真缓存.
不 mock 任何 SQL 行为.

T1  一刀切迁移幂等
T2  简单 root 层级 (用户挂 root → 看到所有 root project)
T3  3 层树 (root → bureau → division), 用户挂 division 能看到 root project
T4  project_members 直接成员授权 (跟 project_orgs 是并集)
T5  平级联合 project (project_orgs 多行)
T6  反向 resolve_project_users 下行继承
T7  access_level 优先级 (owner > shared_write > shared_read)
T8  缓存命中 / invalidate
T9  树形不连通的隔离 (用户挂另一棵树 → 看不到本树 project)

本地运行:
  cd adapter && python3 -m pytest tests/test_org_tree.py -v
"""
import asyncio
import os
import sys
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp(prefix="orgtree-")
os.environ.setdefault("ADAPTER_API_KEY", "test")
os.environ.setdefault("OPENWEBUI_JWT_SECRET", "test")
os.environ.setdefault("OPENWEBUI_ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("OPENWEBUI_ADMIN_PASSWORD", "test")
os.environ.setdefault("VLLM_ENDPOINT", "http://localhost")
os.environ.setdefault("VLLM_API_KEY", "test")
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "adapter.db"))
os.environ.setdefault("WEBUI_DB_PATH", os.path.join(_tmp, "webui.db"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """避免跨文件 sys.modules stub 污染."""
    for m in ["db", "config", "org_tree"]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    yield


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """每测试独立 DB, 跑过 init_db."""
    db_path = tmp_path / "adapter.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import db as db_mod
    db_mod.init_db()
    return str(db_path)


def _seed_projects(db_path, project_ids):
    c = sqlite3.connect(db_path)
    for pid in project_ids:
        c.execute(
            "INSERT INTO projects (project_id, name, created_by) VALUES (?, ?, ?)",
            (pid, f"Project {pid}", "creator"),
        )
    c.commit(); c.close()


def _seed_users(db_path, user_ids):
    c = sqlite3.connect(db_path)
    for uid in user_ids:
        c.execute(
            "INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
            (uid, uid.upper(), f"{uid}@x.com"),
        )
    c.commit(); c.close()


def _seed_org(db_path, org_id, parent_id, name, code):
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO organizations (id, parent_id, name, code) VALUES (?, ?, ?, ?)",
        (org_id, parent_id, name, code),
    )
    c.commit(); c.close()


def _seed_org_member(db_path, org_id, user_id, role="member"):
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
        (org_id, user_id, role),
    )
    c.commit(); c.close()


def _seed_project_org(db_path, project_id, org_id, access="shared_read"):
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO project_orgs (project_id, org_id, access_level) VALUES (?, ?, ?)",
        (project_id, org_id, access),
    )
    c.commit(); c.close()


def _seed_project_member(db_path, project_id, user_id, role="member"):
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO project_members (project_id, user_id, role) VALUES (?, ?, ?)",
        (project_id, user_id, role),
    )
    c.commit(); c.close()


# ----------------- 测试 -----------------

def test_t1_migration_idempotent(fresh_db):
    """ensure_root_org_migration 跑两次, 只迁 user → root, *不*把 project 自动挂 root.
    project 自动挂 root 会让 root 全体成员 (= 所有 user) 看见所有 project, 破坏隔离.
    """
    _seed_projects(fresh_db, ["p1", "p2"])
    _seed_users(fresh_db, ["u1", "u2"])

    import org_tree
    root_id_1 = org_tree.ensure_root_org_migration()
    root_id_2 = org_tree.ensure_root_org_migration()
    assert root_id_1 == root_id_2

    c = sqlite3.connect(fresh_db)
    n_org = c.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
    n_proj_org = c.execute("SELECT COUNT(*) FROM project_orgs").fetchone()[0]
    n_orgmem = c.execute("SELECT COUNT(*) FROM org_members").fetchone()[0]
    c.close()
    assert n_org == 1
    assert n_proj_org == 0   # 关键: project 不自动挂 root
    assert n_orgmem == 2     # user → root org


def test_t2_root_user_does_not_see_unscoped_projects(fresh_db):
    """迁移后 user 在 root, project 没挂任何 org → 用户看不到 (project_members 严格授权)."""
    _seed_projects(fresh_db, ["p1", "p2"])
    _seed_users(fresh_db, ["u1"])
    import org_tree
    org_tree.ensure_root_org_migration()
    perms = asyncio.run(org_tree.resolve_user_projects_async("u1"))
    assert perms == {}  # 没 project_members + project_orgs 没挂 → 看不见


def test_t3_three_layer_tree_inheritance(fresh_db):
    """root → bureau → division. 用户挂 division 能访问 root + bureau project."""
    _seed_projects(fresh_db, ["p_root", "p_bureau", "p_division"])
    _seed_users(fresh_db, ["u_division"])

    _seed_org(fresh_db, "root", None, "Root", "root")
    _seed_org(fresh_db, "bureau", "root", "Bureau A", "bureau-a")
    _seed_org(fresh_db, "division", "bureau", "Division 1", "div-1")

    _seed_org_member(fresh_db, "division", "u_division")

    _seed_project_org(fresh_db, "p_root", "root")
    _seed_project_org(fresh_db, "p_bureau", "bureau")
    _seed_project_org(fresh_db, "p_division", "division", access="owner")

    import org_tree
    perms = asyncio.run(org_tree.resolve_user_projects_async("u_division"))
    assert set(perms.keys()) == {"p_root", "p_bureau", "p_division"}
    assert perms["p_division"] == "owner"


def test_t4_project_members_direct_grant(fresh_db):
    """user 不在任何 org, 但是 project_members 直接成员 → 仍能访问."""
    _seed_projects(fresh_db, ["p1"])
    _seed_users(fresh_db, ["u1"])
    _seed_project_member(fresh_db, "p1", "u1", role="admin")

    import org_tree
    perms = asyncio.run(org_tree.resolve_user_projects_async("u1"))
    assert perms == {"p1": "admin"}


def test_t5_cross_org_collab(fresh_db):
    """市城运 + 市科委联合 project, 两个 bureau 的成员都能访问."""
    _seed_projects(fresh_db, ["p_collab"])
    _seed_users(fresh_db, ["u_chengyun", "u_kewei"])

    _seed_org(fresh_db, "chengyun", None, "市城运", "chengyun")
    _seed_org(fresh_db, "kewei", None, "市科委", "kewei")
    _seed_org_member(fresh_db, "chengyun", "u_chengyun")
    _seed_org_member(fresh_db, "kewei", "u_kewei")

    _seed_project_org(fresh_db, "p_collab", "chengyun", access="shared_write")
    _seed_project_org(fresh_db, "p_collab", "kewei", access="shared_write")

    import org_tree
    p1 = asyncio.run(org_tree.resolve_user_projects_async("u_chengyun"))
    p2 = asyncio.run(org_tree.resolve_user_projects_async("u_kewei"))
    assert "p_collab" in p1 and "p_collab" in p2


def test_t6_reverse_resolve_descendant_inheritance(fresh_db):
    """project 挂 root org, 所有下属 division 用户都该被 resolve 出来."""
    _seed_projects(fresh_db, ["p_root"])
    _seed_users(fresh_db, ["u_root", "u_div_a", "u_div_b"])

    _seed_org(fresh_db, "root", None, "Root", "root")
    _seed_org(fresh_db, "div_a", "root", "Div A", "div-a")
    _seed_org(fresh_db, "div_b", "root", "Div B", "div-b")
    _seed_org_member(fresh_db, "root", "u_root")
    _seed_org_member(fresh_db, "div_a", "u_div_a")
    _seed_org_member(fresh_db, "div_b", "u_div_b")

    _seed_project_org(fresh_db, "p_root", "root")

    import org_tree
    users = asyncio.run(org_tree.resolve_project_users_async("p_root"))
    assert users == {"u_root", "u_div_a", "u_div_b"}


def test_t7_access_level_priority(fresh_db):
    """同一 project 通过 org 是 shared_read, 通过 project_members 是 admin → 取 admin."""
    _seed_projects(fresh_db, ["p1"])
    _seed_users(fresh_db, ["u1"])
    _seed_org(fresh_db, "root", None, "Root", "root")
    _seed_org_member(fresh_db, "root", "u1")
    _seed_project_org(fresh_db, "p1", "root", access="shared_read")
    _seed_project_member(fresh_db, "p1", "u1", role="admin")

    import org_tree
    perms = asyncio.run(org_tree.resolve_user_projects_async("u1"))
    assert perms["p1"] == "admin"


def test_t8_cache_hits_and_invalidate(fresh_db):
    """缓存命中后, invalidate 让下次走 DB."""
    _seed_projects(fresh_db, ["p1"])
    _seed_users(fresh_db, ["u1"])
    _seed_org(fresh_db, "root", None, "Root", "root")
    _seed_org_member(fresh_db, "root", "u1")
    _seed_project_org(fresh_db, "p1", "root", access="shared_read")

    import org_tree
    org_tree.invalidate_cache()  # 确保干净

    lvl = asyncio.run(org_tree.can_user_access_project_async("u1", "p1"))
    assert lvl == "shared_read"
    assert org_tree._cache_get("u1", "p1") == "shared_read"

    # 写一个不存在 project, 缓存负值
    lvl_none = asyncio.run(org_tree.can_user_access_project_async("u1", "nonexist"))
    assert lvl_none is None
    assert org_tree._cache_get("u1", "nonexist") == "__none__"

    # invalidate 该 user, 缓存清空
    org_tree.invalidate_cache("u1")
    assert org_tree._cache_get("u1", "p1") is None


def test_t9_disjoint_tree_isolation(fresh_db):
    """用户挂 tree A, project 在 tree B → 看不到."""
    _seed_projects(fresh_db, ["p_b"])
    _seed_users(fresh_db, ["u_a"])
    _seed_org(fresh_db, "tree_a_root", None, "Tree A", "tree-a")
    _seed_org(fresh_db, "tree_b_root", None, "Tree B", "tree-b")
    _seed_org_member(fresh_db, "tree_a_root", "u_a")
    _seed_project_org(fresh_db, "p_b", "tree_b_root")

    import org_tree
    perms = asyncio.run(org_tree.resolve_user_projects_async("u_a"))
    assert perms == {}


# ---------------- Day 3: org_block chain ----------------

def _set_block(db_path, org_id, block_id):
    c = sqlite3.connect(db_path)
    c.execute("UPDATE organizations SET letta_block_id = ? WHERE id = ?", (block_id, org_id))
    c.commit(); c.close()


def test_t10_block_chain_root_to_leaf_order(fresh_db):
    """3 层树: root → bureau → division. user 挂 division.
    返 [block-root, block-bureau, block-division] (root → leaf 顺序)."""
    _seed_users(fresh_db, ["u1"])
    _seed_org(fresh_db, "root", None, "Root", "root")
    _seed_org(fresh_db, "bureau", "root", "Bureau", "bureau")
    _seed_org(fresh_db, "division", "bureau", "Division", "division")
    _set_block(fresh_db, "root", "block-root")
    _set_block(fresh_db, "bureau", "block-bureau")
    _set_block(fresh_db, "division", "block-division")
    _seed_org_member(fresh_db, "division", "u1")

    import org_tree
    chain = asyncio.run(org_tree.get_user_org_block_chain_async("u1"))
    assert chain == ["block-root", "block-bureau", "block-division"]


def test_t11_block_chain_skips_org_without_block(fresh_db):
    """中间层 org 没绑 block → 跳过, 链只含绑了 block 的层."""
    _seed_users(fresh_db, ["u1"])
    _seed_org(fresh_db, "root", None, "Root", "root")
    _seed_org(fresh_db, "bureau", "root", "Bureau", "bureau")  # 没绑 block
    _seed_org(fresh_db, "division", "bureau", "Division", "division")
    _set_block(fresh_db, "root", "block-root")
    _set_block(fresh_db, "division", "block-division")
    _seed_org_member(fresh_db, "division", "u1")

    import org_tree
    chain = asyncio.run(org_tree.get_user_org_block_chain_async("u1"))
    assert chain == ["block-root", "block-division"]


def test_t12_block_chain_user_in_multiple_orgs(fresh_db):
    """user 同时挂两个不相连 org tree, 两条祖先链都返."""
    _seed_users(fresh_db, ["u1"])
    _seed_org(fresh_db, "tree_a_root", None, "TreeA", "tree-a")
    _seed_org(fresh_db, "tree_b_root", None, "TreeB", "tree-b")
    _set_block(fresh_db, "tree_a_root", "block-a")
    _set_block(fresh_db, "tree_b_root", "block-b")
    _seed_org_member(fresh_db, "tree_a_root", "u1")
    _seed_org_member(fresh_db, "tree_b_root", "u1")

    import org_tree
    chain = asyncio.run(org_tree.get_user_org_block_chain_async("u1"))
    assert set(chain) == {"block-a", "block-b"}
    assert len(chain) == 2  # 无重复


def test_t13_block_chain_user_in_no_org(fresh_db):
    """user 不挂任何 org → 返空链, 不崩."""
    _seed_users(fresh_db, ["u1"])
    import org_tree
    chain = asyncio.run(org_tree.get_user_org_block_chain_async("u1"))
    assert chain == []


def test_t14_set_org_letta_block(fresh_db):
    """admin 绑 / 解绑 block 后链自动反映."""
    _seed_users(fresh_db, ["u1"])
    _seed_org(fresh_db, "bureau", None, "Bureau", "bureau")
    _seed_org_member(fresh_db, "bureau", "u1")

    import org_tree
    # 先空
    chain = asyncio.run(org_tree.get_user_org_block_chain_async("u1"))
    assert chain == []

    # 绑 block
    org_tree.set_org_letta_block("bureau", "block-bureau-v1")
    chain = asyncio.run(org_tree.get_user_org_block_chain_async("u1"))
    assert chain == ["block-bureau-v1"]

    # 换 block
    org_tree.set_org_letta_block("bureau", "block-bureau-v2")
    chain = asyncio.run(org_tree.get_user_org_block_chain_async("u1"))
    assert chain == ["block-bureau-v2"]

    # 解绑
    org_tree.set_org_letta_block("bureau", None)
    chain = asyncio.run(org_tree.get_user_org_block_chain_async("u1"))
    assert chain == []


def test_t15_block_chain_disjoint_user_no_leak(fresh_db):
    """另一用户挂别的 tree → 不会把别人的 block 给当前用户."""
    _seed_users(fresh_db, ["u1", "u2"])
    _seed_org(fresh_db, "tree_a_root", None, "TreeA", "tree-a")
    _seed_org(fresh_db, "tree_b_root", None, "TreeB", "tree-b")
    _set_block(fresh_db, "tree_a_root", "block-a")
    _set_block(fresh_db, "tree_b_root", "block-b")
    _seed_org_member(fresh_db, "tree_a_root", "u1")
    _seed_org_member(fresh_db, "tree_b_root", "u2")

    import org_tree
    chain1 = asyncio.run(org_tree.get_user_org_block_chain_async("u1"))
    chain2 = asyncio.run(org_tree.get_user_org_block_chain_async("u2"))
    assert chain1 == ["block-a"]
    assert chain2 == ["block-b"]
