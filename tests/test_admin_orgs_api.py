"""End-to-end tests for admin_orgs_api (Issue #14 Day 4).

走真路径: 真 JWT + 真 user_cache + 真 require_org_admin + 真 SQLite + 真 SQL.
不 bypass 鉴权.

T1  POST /orgs 创建 org
T2  POST /orgs code 重复 → 409
T3  POST /orgs invalid code → 400
T4  POST /orgs reserved code (root) → 400
T5  POST /orgs invalid parent → 400
T6  GET /orgs 返扁平列表 + member_count / child_count / project_count
T7  PATCH /orgs/{id} rename
T8  PATCH /orgs/{id} change parent (no cycle) → 通
T9  PATCH /orgs/{id} self-parent → 400
T10 PATCH /orgs/{id} cycle → 400
T11 PATCH /orgs/{id} bind/unbind letta_block_id
T12 DELETE /orgs/{id} 有子节点 → 400
T13 DELETE /orgs/{id} 有成员 → 400
T14 DELETE /orgs/{id} 有 project_orgs → 400
T15 DELETE /orgs/{id} 干净 → 200
T16 DELETE /orgs/{root_id} → 400
T17 POST/DELETE/GET members 端点
T18 POST/DELETE/GET project_orgs 端点
T19 非 admin 访问 → 403
T20 加成员后, 该用户的 LRU cache 被 invalidate
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="admin-orgs-")
os.environ.setdefault("ADAPTER_API_KEY", "test")
os.environ.setdefault("OPENWEBUI_JWT_SECRET", "test")
os.environ.setdefault("OPENWEBUI_ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("OPENWEBUI_ADMIN_PASSWORD", "test")
os.environ.setdefault("VLLM_ENDPOINT", "http://localhost")
os.environ.setdefault("VLLM_API_KEY", "test")
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "adapter.db"))
os.environ.setdefault("WEBUI_DB_PATH", os.path.join(_tmp, "webui.db"))
os.environ.setdefault("ORG_ADMIN_EMAILS", "admin@example.com")

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for m in ["db", "config", "auth", "org_tree", "admin_orgs_api"]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    yield


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("WEBUI_DB_PATH", str(tmp_path / "webui.db"))

    import db as db_mod
    db_mod.init_db()

    c = sqlite3.connect(str(db_path))
    c.execute("INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
              ("admin-id", "Admin", "admin@example.com"))
    c.execute("INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
              ("regular-id", "Regular", "regular@example.com"))
    c.execute("INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
              ("alice-id", "Alice", "alice@example.com"))
    c.execute("INSERT INTO projects (project_id, name, created_by) VALUES (?, ?, ?)",
              ("proj-1", "Project 1", "admin-id"))
    c.commit(); c.close()

    # 跑 root org 迁移 (模拟生产 startup)
    import org_tree
    root_id = org_tree.ensure_root_org_migration()

    from fastapi import FastAPI
    from admin_orgs_api import router
    app = FastAPI()
    app.include_router(router)
    return app, str(db_path), root_id


def _jwt(uid):
    import jwt
    return jwt.encode({"id": uid}, "test", algorithm="HS256")


def _hdrs(uid="admin-id"):
    return {"Authorization": f"Bearer {_jwt(uid)}"}


def _client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


# ---------------- POST /orgs ----------------

def test_t1_create_org(app_db):
    app, _, _ = app_db
    r = _client(app).post("/admin/api/orgs",
                          json={"name": "Bureau A", "code": "bureau-a", "org_type": "bureau"},
                          headers=_hdrs())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == "bureau-a"
    assert body["id"].startswith("org-")


def test_t2_create_duplicate_code_409(app_db):
    app, _, _ = app_db
    c = _client(app)
    c.post("/admin/api/orgs", json={"name": "A", "code": "dup-code"}, headers=_hdrs())
    r = c.post("/admin/api/orgs", json={"name": "B", "code": "dup-code"}, headers=_hdrs())
    assert r.status_code == 409


def test_t3_create_invalid_code_400(app_db):
    app, _, _ = app_db
    r = _client(app).post("/admin/api/orgs",
                          json={"name": "A", "code": "Bad Code With Spaces"},
                          headers=_hdrs())
    assert r.status_code == 400


def test_t4_create_reserved_code_400(app_db):
    app, _, _ = app_db
    r = _client(app).post("/admin/api/orgs",
                          json={"name": "Try", "code": "ai-infra-root"},
                          headers=_hdrs())
    assert r.status_code == 400


def test_t5_create_invalid_parent_400(app_db):
    app, _, _ = app_db
    r = _client(app).post("/admin/api/orgs",
                          json={"name": "A", "code": "code-a", "parent_id": "no-such-org"},
                          headers=_hdrs())
    assert r.status_code == 400


# ---------------- GET /orgs ----------------

def test_t6_list_orgs_with_counts(app_db):
    app, db_path, root_id = app_db
    c = _client(app)
    # 在 root 下建一个 child
    r = c.post("/admin/api/orgs",
               json={"name": "Child", "code": "child", "parent_id": root_id},
               headers=_hdrs())
    child_id = r.json()["id"]

    # 加一个成员
    c.post(f"/admin/api/orgs/{child_id}/members",
           json={"user_id": "alice-id"}, headers=_hdrs())

    r = c.get("/admin/api/orgs", headers=_hdrs())
    orgs = {o["id"]: o for o in r.json()["orgs"]}
    assert root_id in orgs
    assert child_id in orgs
    assert orgs[child_id]["member_count"] == 1
    assert orgs[root_id]["child_count"] >= 1


# ---------------- PATCH /orgs/{id} ----------------

def test_t7_patch_rename(app_db):
    app, _, _ = app_db
    c = _client(app)
    org = c.post("/admin/api/orgs", json={"name": "Old", "code": "old"}, headers=_hdrs()).json()
    r = c.patch(f"/admin/api/orgs/{org['id']}", json={"name": "New"}, headers=_hdrs())
    assert r.status_code == 200
    rows = c.get("/admin/api/orgs", headers=_hdrs()).json()["orgs"]
    assert next(o for o in rows if o["id"] == org["id"])["name"] == "New"


def test_t8_patch_change_parent(app_db):
    app, _, root_id = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p8a"}, headers=_hdrs()).json()
    b = c.post("/admin/api/orgs", json={"name": "B", "code": "p8b"}, headers=_hdrs()).json()
    r = c.patch(f"/admin/api/orgs/{b['id']}", json={"parent_id": a["id"]}, headers=_hdrs())
    assert r.status_code == 200


def test_t9_patch_self_parent_400(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p9"}, headers=_hdrs()).json()
    r = c.patch(f"/admin/api/orgs/{a['id']}", json={"parent_id": a["id"]}, headers=_hdrs())
    assert r.status_code == 400


def test_t10_patch_cycle_400(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p10a"}, headers=_hdrs()).json()
    b = c.post("/admin/api/orgs", json={"name": "B", "code": "p10b", "parent_id": a["id"]}, headers=_hdrs()).json()
    # a → b → a 会形成 cycle
    r = c.patch(f"/admin/api/orgs/{a['id']}", json={"parent_id": b["id"]}, headers=_hdrs())
    assert r.status_code == 400


def test_t11_patch_letta_block_bind_unbind(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p11"}, headers=_hdrs()).json()
    c.patch(f"/admin/api/orgs/{a['id']}", json={"letta_block_id": "block-xyz"}, headers=_hdrs())
    rows = c.get("/admin/api/orgs", headers=_hdrs()).json()["orgs"]
    assert next(o for o in rows if o["id"] == a["id"])["letta_block_id"] == "block-xyz"
    # 解绑 (空字符串)
    c.patch(f"/admin/api/orgs/{a['id']}", json={"letta_block_id": ""}, headers=_hdrs())
    rows = c.get("/admin/api/orgs", headers=_hdrs()).json()["orgs"]
    assert next(o for o in rows if o["id"] == a["id"])["letta_block_id"] is None


# ---------------- DELETE /orgs/{id} ----------------

def test_t12_delete_with_child_400(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p12a"}, headers=_hdrs()).json()
    c.post("/admin/api/orgs", json={"name": "B", "code": "p12b", "parent_id": a["id"]}, headers=_hdrs())
    r = c.delete(f"/admin/api/orgs/{a['id']}", headers=_hdrs())
    assert r.status_code == 400


def test_t13_delete_with_member_400(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p13"}, headers=_hdrs()).json()
    c.post(f"/admin/api/orgs/{a['id']}/members", json={"user_id": "alice-id"}, headers=_hdrs())
    r = c.delete(f"/admin/api/orgs/{a['id']}", headers=_hdrs())
    assert r.status_code == 400


def test_t14_delete_with_project_org_400(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p14"}, headers=_hdrs()).json()
    c.post("/admin/api/projects/proj-1/orgs",
           json={"org_id": a["id"], "access_level": "shared_read"}, headers=_hdrs())
    r = c.delete(f"/admin/api/orgs/{a['id']}", headers=_hdrs())
    assert r.status_code == 400


def test_t15_delete_clean(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p15"}, headers=_hdrs()).json()
    r = c.delete(f"/admin/api/orgs/{a['id']}", headers=_hdrs())
    assert r.status_code == 200


def test_t16_delete_root_400(app_db):
    app, _, root_id = app_db
    # root 当前有所有 user_cache 用户挂着 (3 个), 要先删完才能跑到 "cannot delete root org" 检查
    # 不过我们直接验 code 路径: root 有成员 → 400 (但是是 has members 错误, 不是 root 错误)
    # 为了测 root 保护, 先把 root members 全删
    c = _client(app)
    r = c.get(f"/admin/api/orgs/{root_id}/members", headers=_hdrs())
    for m in r.json()["members"]:
        c.delete(f"/admin/api/orgs/{root_id}/members/{m['user_id']}", headers=_hdrs())
    # 现在 root 干净, 试图删
    r = c.delete(f"/admin/api/orgs/{root_id}", headers=_hdrs())
    assert r.status_code == 400
    assert "root" in r.text.lower()


# ---------------- /orgs/{id}/members ----------------

def test_t17_member_crud(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p17"}, headers=_hdrs()).json()
    # 加
    r = c.post(f"/admin/api/orgs/{a['id']}/members",
               json={"user_id": "alice-id", "role": "admin"}, headers=_hdrs())
    assert r.status_code == 200
    # 列
    r = c.get(f"/admin/api/orgs/{a['id']}/members", headers=_hdrs())
    members = r.json()["members"]
    assert len(members) == 1
    assert members[0]["user_id"] == "alice-id"
    assert members[0]["role"] == "admin"
    assert members[0]["email"] == "alice@example.com"
    # 删
    r = c.delete(f"/admin/api/orgs/{a['id']}/members/alice-id", headers=_hdrs())
    assert r.status_code == 200
    r = c.get(f"/admin/api/orgs/{a['id']}/members", headers=_hdrs())
    assert r.json()["members"] == []
    # 重复删 → 404
    r = c.delete(f"/admin/api/orgs/{a['id']}/members/alice-id", headers=_hdrs())
    assert r.status_code == 404


def test_t17b_add_member_unknown_user_400(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p17b"}, headers=_hdrs()).json()
    r = c.post(f"/admin/api/orgs/{a['id']}/members",
               json={"user_id": "ghost"}, headers=_hdrs())
    assert r.status_code == 400


# ---------------- /projects/{pid}/orgs ----------------

def test_t18_project_org_crud(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p18"}, headers=_hdrs()).json()
    # attach
    r = c.post("/admin/api/projects/proj-1/orgs",
               json={"org_id": a["id"], "access_level": "shared_write"}, headers=_hdrs())
    assert r.status_code == 200
    # list
    r = c.get("/admin/api/projects/proj-1/orgs", headers=_hdrs())
    rows = r.json()["orgs"]
    assert len(rows) == 1
    assert rows[0]["org_id"] == a["id"]
    assert rows[0]["access_level"] == "shared_write"
    # detach
    r = c.delete(f"/admin/api/projects/proj-1/orgs/{a['id']}", headers=_hdrs())
    assert r.status_code == 200
    # 重复 detach 404
    r = c.delete(f"/admin/api/projects/proj-1/orgs/{a['id']}", headers=_hdrs())
    assert r.status_code == 404


def test_t18b_attach_invalid_access_level_400(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p18b"}, headers=_hdrs()).json()
    r = c.post("/admin/api/projects/proj-1/orgs",
               json={"org_id": a["id"], "access_level": "GOD_MODE"}, headers=_hdrs())
    assert r.status_code == 400


def test_t18c_attach_to_nonexistent_project_404(app_db):
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p18c"}, headers=_hdrs()).json()
    r = c.post("/admin/api/projects/no-such-proj/orgs",
               json={"org_id": a["id"]}, headers=_hdrs())
    assert r.status_code == 404


# ---------------- 鉴权 ----------------

def test_t19_non_admin_403(app_db):
    app, _, _ = app_db
    r = _client(app).get("/admin/api/orgs", headers=_hdrs("regular-id"))
    assert r.status_code == 403


def test_t19b_no_token_401(app_db):
    app, _, _ = app_db
    r = _client(app).get("/admin/api/orgs")
    assert r.status_code == 401


# ---------------- cache invalidation ----------------

def test_t22_list_members_filters_test_users(app_db):
    """list_org_members 应过滤掉 bench_* 测试用户 + @local.test 邮箱 + admin@aiinfra.local 系统账号."""
    app, db_path, root_id = app_db
    c = _client(app)
    # 插入垃圾 user 到 user_cache + 直接挂 root org_members
    raw = sqlite3.connect(db_path)
    raw.execute("INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
                ("bench_999", "bench_clear_999", "bench_999@local.test"))
    raw.execute("INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
                ("aiinfra-admin", "Admin", "admin@aiinfra.local"))
    raw.execute("INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
                ("real-user-1", "Real User", "real@example.com"))
    raw.execute("INSERT OR IGNORE INTO org_members (org_id, user_id, role) VALUES (?, ?, 'member')",
                (root_id, "bench_999"))
    raw.execute("INSERT OR IGNORE INTO org_members (org_id, user_id, role) VALUES (?, ?, 'member')",
                (root_id, "aiinfra-admin"))
    raw.execute("INSERT OR IGNORE INTO org_members (org_id, user_id, role) VALUES (?, ?, 'member')",
                (root_id, "real-user-1"))
    raw.commit(); raw.close()

    r = c.get(f"/admin/api/orgs/{root_id}/members", headers=_hdrs())
    members = r.json()["members"]
    user_ids = {m["user_id"] for m in members}
    assert "bench_999" not in user_ids  # bench_* 过滤
    assert "aiinfra-admin" not in user_ids  # @aiinfra.local 过滤
    assert "real-user-1" in user_ids  # 真实用户保留


def test_t23_block_content_get_empty_org(app_db):
    """org 没绑 block → GET 返 content=''."""
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name":"A", "code":"p23"}, headers=_hdrs()).json()
    r = c.get(f"/admin/api/orgs/{a['id']}/block-content", headers=_hdrs())
    assert r.status_code == 200
    body = r.json()
    assert body["block_id"] is None
    assert body["content"] == ""


def test_t24_block_content_404_on_unknown_org(app_db):
    app, _, _ = app_db
    r = _client(app).get("/admin/api/orgs/no-such-org/block-content", headers=_hdrs())
    assert r.status_code == 404


def test_t21_list_org_projects_reverse(app_db):
    """GET /orgs/{id}/projects 反向列 org 挂的 projects."""
    app, _, _ = app_db
    c = _client(app)
    a = c.post("/admin/api/orgs", json={"name": "A", "code": "p21"}, headers=_hdrs()).json()
    # 没挂任何 project
    r = c.get(f"/admin/api/orgs/{a['id']}/projects", headers=_hdrs())
    assert r.status_code == 200
    assert r.json()["projects"] == []
    # 挂上 proj-1
    c.post("/admin/api/projects/proj-1/orgs",
           json={"org_id": a["id"], "access_level": "shared_write"}, headers=_hdrs())
    r = c.get(f"/admin/api/orgs/{a['id']}/projects", headers=_hdrs())
    rows = r.json()["projects"]
    assert len(rows) == 1
    assert rows[0]["project_id"] == "proj-1"
    assert rows[0]["access_level"] == "shared_write"
    assert rows[0]["name"] == "Project 1"


def test_t20_add_member_invalidates_user_cache(app_db):
    """加 alice 进 org, 之前 alice 缓存里 (alice, proj-1) → None 的项要被清."""
    app, _, _ = app_db
    c = _client(app)
    import org_tree

    # alice 之前查 proj-1 没权限, 缓存 __none__
    asyncio.run(org_tree.can_user_access_project_async("alice-id", "proj-1"))
    assert org_tree._cache_get("alice-id", "proj-1") == "__none__"

    # 建 org + 把 alice 加进去 + project 挂 org
    a = c.post("/admin/api/orgs", json={"name": "X", "code": "p20"}, headers=_hdrs()).json()
    c.post(f"/admin/api/orgs/{a['id']}/members", json={"user_id": "alice-id"}, headers=_hdrs())
    c.post("/admin/api/projects/proj-1/orgs",
           json={"org_id": a["id"], "access_level": "shared_read"}, headers=_hdrs())

    # alice 的 cache 应该已经被清 (add_member 调了 invalidate_cache(alice-id))
    assert org_tree._cache_get("alice-id", "proj-1") is None

    # 重新查 → 应该看到了 (因为 alice 现在挂 org 了)
    lvl = asyncio.run(org_tree.can_user_access_project_async("alice-id", "proj-1"))
    assert lvl == "shared_read"
