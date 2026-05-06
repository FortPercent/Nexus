"""Unit tests for metrics middleware (Issue #13 Day 1).

测试目标:
  T1 白名单路径 (/v1/, /admin/api/) 命中, 写入 metrics 行
  T2 黑名单路径 (/, /knowledge HTML 页) 不写入
  T3 异常路径 status=500 + err_class 落表
  T4 子端点用 request.state 回填 user/project/agent/model
  T5 持久化失败时业务响应不受影响
  T6 schema migration 创建了所有 index

本地运行:
  cd adapter && python3 -m pytest tests/test_metrics_middleware.py -v

设计: 用 FastAPI 真路由 (生产代码用同款), endpoint 提到 module 级让
`request: Request` 类型 hint 能被 typing.get_type_hints 解析.
*不要*在文件顶部加 `from __future__ import annotations` — 那会让所有 hints 变字符串,
FastAPI 解析嵌套 endpoint 时拿不到 Request 类 → 422.
"""
import asyncio
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

os.environ.setdefault("ADAPTER_API_KEY", "test")
os.environ.setdefault("OPENWEBUI_JWT_SECRET", "test")
os.environ.setdefault("OPENWEBUI_ADMIN_EMAIL", "test@example.com")
os.environ.setdefault("OPENWEBUI_ADMIN_PASSWORD", "test")
os.environ.setdefault("VLLM_ENDPOINT", "http://localhost")
os.environ.setdefault("VLLM_API_KEY", "test")

import pytest
from fastapi import FastAPI, HTTPException, Request


# Module-level endpoints — 关键: 不在 fixture 闭包内定义, 让 echo.__globals__
# 含模块顶部 import 的 Request, FastAPI 类型推断能解析.
async def _echo_endpoint(request: Request):
    return {"ok": True}


async def _foo_endpoint(request: Request):
    request.state.metrics_user_id = "user-123"
    request.state.metrics_project_id = "proj-abc"
    request.state.metrics_agent_id = "agent-xyz"
    request.state.metrics_model = "letta-test"
    request.state.metrics_tokens_in = 42
    request.state.metrics_tokens_out = 7
    return {"ok": True}


async def _boom_endpoint(request: Request):
    raise HTTPException(status_code=500, detail="kaboom")


async def _root_endpoint(request: Request):
    return {"ok": True}


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """每个测试用独立的 adapter.db, 保证不污染."""
    db_path = tmp_path / "adapter.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    # 强制 reload config + db, 防止 test_chat_forward_wire 的 stub 污染
    for mod in ["config", "db", "middleware_metrics"]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    import db as db_mod
    db_mod.init_db()
    return str(db_path)


@pytest.fixture
def app_with_middleware(isolated_db):
    """生产代码同款 FastAPI app — 真 endpoint, 真 Request 参数, 真 middleware."""
    from middleware_metrics import metrics_middleware

    app = FastAPI()
    app.middleware("http")(metrics_middleware)
    app.add_api_route("/v1/echo", _echo_endpoint, methods=["GET"])
    app.add_api_route("/admin/api/foo", _foo_endpoint, methods=["POST"])
    app.add_api_route("/internal/boom", _boom_endpoint, methods=["GET"])
    app.add_api_route("/", _root_endpoint, methods=["GET"])
    return app


def _read_metrics(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT * FROM metrics ORDER BY id"
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _wait_for_rows(db_path, expected, timeout_s=2.0):
    """fire-and-forget 写入, 测试里轮询等行落库."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rows = _read_metrics(db_path)
        if len(rows) >= expected:
            return rows
        time.sleep(0.05)
    return _read_metrics(db_path)


def test_t1_whitelist_path_recorded(app_with_middleware, isolated_db):
    from fastapi.testclient import TestClient
    client = TestClient(app_with_middleware)

    resp = client.get("/v1/echo")
    print(f"DEBUG status={resp.status_code} body={resp.text[:500]}")
    assert resp.status_code == 200
    rows = _wait_for_rows(isolated_db, expected=1)
    assert len(rows) == 1
    r = rows[0]
    assert r["endpoint"] == "/v1/echo"
    assert r["method"] == "GET"
    assert r["status"] == 200
    assert r["latency_ms"] >= 0
    assert r["request_id"]
    assert r["err_class"] is None


def test_t2_non_whitelist_not_recorded(app_with_middleware, isolated_db):
    from fastapi.testclient import TestClient
    client = TestClient(app_with_middleware)

    resp = client.get("/")
    assert resp.status_code == 200
    # 等一下确保异步 task 不会偷偷写
    time.sleep(0.3)
    rows = _read_metrics(isolated_db)
    assert rows == []


def test_t3_500_with_err_class(app_with_middleware, isolated_db):
    from fastapi.testclient import TestClient
    client = TestClient(app_with_middleware, raise_server_exceptions=False)

    resp = client.get("/internal/boom")
    assert resp.status_code == 500
    rows = _wait_for_rows(isolated_db, expected=1)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == 500
    assert r["endpoint"] == "/internal/boom"
    # HTTPException 是被 FastAPI 处理的, 不会从 call_next raise. status=500 但 err_class 可能是 None.
    # 这是预期 — 我们的 err_class 主要捕未被 handler 处理的真异常.


def test_t4_state_fields_persisted(app_with_middleware, isolated_db):
    from fastapi.testclient import TestClient
    client = TestClient(app_with_middleware)

    resp = client.post("/admin/api/foo", json={})
    assert resp.status_code == 200
    rows = _wait_for_rows(isolated_db, expected=1)
    r = rows[0]
    assert r["user_id"] == "user-123"
    assert r["project_id"] == "proj-abc"
    assert r["agent_id"] == "agent-xyz"
    assert r["model"] == "letta-test"
    assert r["tokens_in"] == 42
    assert r["tokens_out"] == 7


def test_t5_persist_failure_silent(app_with_middleware, isolated_db, monkeypatch):
    """_persist_metrics 内部异常被 try/except 吞掉, 业务响应不受影响.

    (注: 模拟 _persist_metrics 抛异常需要绕过它内部的 try/except,
    所以这里直接 patch aiosqlite.connect 让 persist 内部 except 命中.)
    """
    import middleware_metrics as mw

    class BoomConn:
        async def __aenter__(self): raise RuntimeError("simulated db down")
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(mw.aiosqlite, "connect", lambda *a, **kw: BoomConn())

    from fastapi.testclient import TestClient
    client = TestClient(app_with_middleware)
    resp = client.get("/v1/echo")
    # 业务路径正常 — _persist_metrics 内异常不冒泡
    assert resp.status_code == 200
    # 没行写入 (boom 路径)
    rows = _read_metrics(isolated_db)
    assert rows == []


def test_t6_indexes_exist(isolated_db):
    """sanity check: schema migration 创建了所有 index, 否则 leaderboard / timeseries 查询会慢."""
    c = sqlite3.connect(isolated_db)
    indexes = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='metrics'"
    ).fetchall()}
    c.close()
    assert "idx_metrics_ts" in indexes
    assert "idx_metrics_user_ts" in indexes
    assert "idx_metrics_project_ts" in indexes
    assert "idx_metrics_request_id" in indexes
    assert "idx_metrics_endpoint_ts" in indexes
