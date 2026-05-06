"""End-to-end tests for #14 Day 2: auth.require_project_* 走 org_tree.

测真路径: 真 JWT + 真 user_cache + 真 project_members + 真 project_orgs +
真递归 CTE. 不 bypass 任何鉴权.

T1  project_members 直接成员 → require_project_member 通过
T2  project_orgs 通过祖先 org 命中 → require_project_member 通过 (跨部门继承)
T3  无 project_members 也无 org 关系 → 403
T4  project_members.role='member' 但 require_project_admin → 403
T5  project_orgs.access_level='owner' → require_project_admin 通过
T6  没 token → 401
T7  invalid JWT → 401
T8  user 通过 ancestor org 拿 admin level (project_orgs.access_level='admin') → require_project_admin 通过

本地运行:
  cd adapter && python3 -m pytest tests/test_auth_org_tree.py -v
"""
import os
import sys
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp(prefix="auth-orgtree-")
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
    for m in ["db", "config", "auth", "org_tree"]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    yield


@pytest.fixture
def app_and_db(tmp_path, monkeypatch):
    """构建 minimal app, 挂载几条要鉴权的 endpoint, 用真 require_project_* helper."""
    db_path = tmp_path / "adapter.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("WEBUI_DB_PATH", str(tmp_path / "webui.db"))

    import db as db_mod
    db_mod.init_db()

    # 预置 user_cache
    c = sqlite3.connect(str(db_path))
    c.execute("INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
              ("u-alice", "Alice", "alice@x.com"))
    c.execute("INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
              ("u-bob", "Bob", "bob@x.com"))
    # 预置 project (rough — db.init_db 创建 projects 表的 NOT NULL 列要给)
    c.execute("INSERT INTO projects (project_id, name, created_by) VALUES (?, ?, ?)",
              ("p1", "Project 1", "u-alice"))
    c.commit()
    c.close()

    from fastapi import FastAPI, Request, Depends
    from auth import require_project_member, require_project_admin

    app = FastAPI()

    @app.get("/test/projects/{project_id}/read")
    async def read_endpoint(project_id: str, request: Request):
        user = await require_project_member(request, project_id)
        return {"user_id": user["id"], "project_id": project_id}

    @app.get("/test/projects/{project_id}/manage")
    async def manage_endpoint(project_id: str, request: Request):
        user = await require_project_admin(request, project_id)
        return {"user_id": user["id"], "project_id": project_id}

    return app, str(db_path)


def _make_jwt(user_id):
    import jwt
    return jwt.encode({"id": user_id}, "test", algorithm="HS256")


def _hdrs(user_id):
    return {"Authorization": f"Bearer {_make_jwt(user_id)}"}


def _add_member(db_path, project_id, user_id, role="member"):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (?, ?, ?)",
              (project_id, user_id, role))
    c.commit(); c.close()


def _add_org(db_path, org_id, parent_id, code):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO organizations (id, parent_id, name, code) VALUES (?, ?, ?, ?)",
              (org_id, parent_id, code, code))
    c.commit(); c.close()


def _add_org_member(db_path, org_id, user_id):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'member')",
              (org_id, user_id))
    c.commit(); c.close()


def _add_project_org(db_path, project_id, org_id, access="shared_read"):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO project_orgs (project_id, org_id, access_level) VALUES (?, ?, ?)",
              (project_id, org_id, access))
    c.commit(); c.close()


def _clear_cache():
    """每个测试前后清 org_tree LRU cache, 避免跨测试污染."""
    try:
        import org_tree
        org_tree.invalidate_cache()
    except ImportError:
        pass


# ---------------- 测试 ----------------

def test_t1_direct_project_member(app_and_db):
    app, db_path = app_and_db
    _add_member(db_path, "p1", "u-alice", role="member")
    _clear_cache()
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/test/projects/p1/read", headers=_hdrs("u-alice"))
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == "u-alice"


def test_t2_via_ancestor_org(app_and_db):
    """user 挂 division org, project 挂祖先 bureau org → 递归命中."""
    app, db_path = app_and_db
    _add_org(db_path, "bureau", None, "bureau")
    _add_org(db_path, "division", "bureau", "division")
    _add_org_member(db_path, "division", "u-alice")
    _add_project_org(db_path, "p1", "bureau", access="shared_read")
    _clear_cache()
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/test/projects/p1/read", headers=_hdrs("u-alice"))
    assert r.status_code == 200, r.text


def test_t3_no_membership_403(app_and_db):
    app, _ = app_and_db
    _clear_cache()
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/test/projects/p1/read", headers=_hdrs("u-alice"))
    assert r.status_code == 403


def test_t4_member_role_not_admin(app_and_db):
    """role='member' 走 require_project_admin → 403."""
    app, db_path = app_and_db
    _add_member(db_path, "p1", "u-alice", role="member")
    _clear_cache()
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/test/projects/p1/manage", headers=_hdrs("u-alice"))
    assert r.status_code == 403


def test_t5_project_orgs_owner_grants_admin(app_and_db):
    """project_orgs.access_level='owner' → require_project_admin 通过."""
    app, db_path = app_and_db
    _add_org(db_path, "bureau", None, "bureau")
    _add_org_member(db_path, "bureau", "u-alice")
    _add_project_org(db_path, "p1", "bureau", access="owner")
    _clear_cache()
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/test/projects/p1/manage", headers=_hdrs("u-alice"))
    assert r.status_code == 200, r.text


def test_t6_no_token_401(app_and_db):
    app, _ = app_and_db
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/test/projects/p1/read")
    assert r.status_code == 401


def test_t7_invalid_jwt_401(app_and_db):
    app, _ = app_and_db
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/test/projects/p1/read",
                            headers={"Authorization": "Bearer invalid"})
    assert r.status_code == 401


def test_t8_admin_via_ancestor(app_and_db):
    """user 挂 division, project 挂 bureau access='admin' → 通过 require_project_admin."""
    app, db_path = app_and_db
    _add_org(db_path, "bureau", None, "bureau")
    _add_org(db_path, "division", "bureau", "division")
    _add_org_member(db_path, "division", "u-alice")
    _add_project_org(db_path, "p1", "bureau", access="admin")
    _clear_cache()
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/test/projects/p1/manage", headers=_hdrs("u-alice"))
    # 注意: org_tree 的 _RESOLVE_USER_PROJECTS_SQL 把 access_level 原样传出, "admin" 在 _ADMIN_LEVELS 里
    assert r.status_code == 200, r.text
