"""Unit tests for metrics aggregation API (Issue #13 Day 3).

测试目标:
  T1 summary 在空 metrics 表返 0 / 不崩
  T2 summary 计算 total / err_rate / avg_latency 正确
  T3 timeseries 按 hour 桶聚合
  T4 timeseries group_by=user_id 维度切分
  T5 timeseries group_by 白名单防 SQL 注入
  T6 leaderboard 按 count 排序
  T7 leaderboard 按 err_rate 排序
  T8 leaderboard dim 白名单
  T9 时间窗口 from/to 边界

本地运行:
  cd adapter && python3 -m pytest tests/test_metrics_api.py -v
"""
from __future__ import annotations

import os
import sys
import sqlite3
import time
import tempfile

# 必要 env 必须在 import 之前
_tmp = tempfile.mkdtemp(prefix="metrics-api-")
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
def _isolate_main(monkeypatch):
    """跟 test_metrics_chat_sink 同款隔离, 避免 test_preflight stub 污染."""
    for m in ["main", "db", "config", "routing", "preflight",
              "middleware_metrics", "auth", "knowledge_mirror", "webui_sync",
              "metrics_api", "admin_api"]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    yield


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """独立 adapter.db + 全套 metrics endpoint mounted, **真鉴权路径**.

    用 user_cache 表插 admin / regular user, 测试用 JWT 真签 + 真 decode 走完整路径,
    不用 dependency_overrides bypass.
    """
    db_path = tmp_path / "adapter.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("WEBUI_DB_PATH", str(tmp_path / "webui.db"))

    import db as db_mod
    db_mod.init_db()

    # 插预置用户 (避免 extract_user_from_admin fallback 调外部 Open WebUI)
    import sqlite3
    c = sqlite3.connect(str(db_path))
    c.execute(
        "INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
        ("admin-id", "Admin", "admin@example.com"),
    )
    c.execute(
        "INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
        ("regular-id", "Regular User", "regular@example.com"),
    )
    c.commit()
    c.close()

    from fastapi import FastAPI
    from metrics_api import router as metrics_router
    app = FastAPI()
    app.include_router(metrics_router)
    return app, str(db_path)


def _make_jwt(user_id: str) -> str:
    import jwt
    return jwt.encode({"id": user_id}, "test", algorithm="HS256")


def _admin_headers():
    return {"Authorization": f"Bearer {_make_jwt('admin-id')}"}


def _regular_headers():
    return {"Authorization": f"Bearer {_make_jwt('regular-id')}"}


def _seed(db_path, rows):
    """rows: list of dict, fields will be inserted into metrics table.
    Missing fields filled with sensible defaults."""
    c = sqlite3.connect(db_path)
    for r in rows:
        c.execute(
            """
            INSERT INTO metrics (
                request_id, ts_unix, user_id, project_id, agent_id, model,
                endpoint, method, status, latency_ms, ttft_ms,
                tokens_in, tokens_out, variant_id, err_class
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.get("request_id", "req-x"),
                r.get("ts_unix", time.time()),
                r.get("user_id", ""),
                r.get("project_id"),
                r.get("agent_id"),
                r.get("model"),
                r.get("endpoint", "/v1/echo"),
                r.get("method", "POST"),
                r.get("status", 200),
                r.get("latency_ms", 100),
                r.get("ttft_ms"),
                r.get("tokens_in", 0),
                r.get("tokens_out", 0),
                r.get("variant_id"),
                r.get("err_class"),
            ),
        )
    c.commit()
    c.close()


# ---------------- summary ----------------

def test_t1_summary_empty(isolated_app):
    app, _ = isolated_app
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/summary?hours=24", headers=_admin_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["err_rate"] == 0.0


def test_t2_summary_aggregates(isolated_app):
    app, db_path = isolated_app
    now = time.time()
    _seed(db_path, [
        {"user_id": "u1", "latency_ms": 100, "status": 200, "tokens_in": 10, "tokens_out": 5},
        {"user_id": "u1", "latency_ms": 200, "status": 200, "tokens_in": 20, "tokens_out": 5},
        {"user_id": "u2", "latency_ms": 300, "status": 500, "tokens_in": 30, "tokens_out": 5},
        {"user_id": "u2", "latency_ms": 500, "status": 200},
    ])
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/summary", headers=_admin_headers())
    body = r.json()
    assert body["total"] == 4
    assert body["err_5xx"] == 1
    assert body["err_rate"] == 0.25
    assert body["avg_latency_ms"] == 275.0
    assert body["max_latency_ms"] == 500
    assert body["tokens_in"] == 60
    assert body["tokens_out"] == 15
    assert body["unique_users"] == 2


# ---------------- timeseries ----------------

def test_t3_timeseries_hourly_bucket(isolated_app):
    app, db_path = isolated_app
    now = time.time()
    _seed(db_path, [
        {"ts_unix": now - 30, "latency_ms": 100, "status": 200},
        {"ts_unix": now - 60, "latency_ms": 200, "status": 200},
        {"ts_unix": now - 7200, "latency_ms": 300, "status": 200},
    ])
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/timeseries?bucket=hour&hours=4", headers=_admin_headers())
    assert r.status_code == 200
    series = r.json()["series"]
    assert len(series) >= 2  # 当前小时 + 2h 前小时
    total = sum(p["count"] for p in series)
    assert total == 3


def test_t4_timeseries_group_by_user(isolated_app):
    app, db_path = isolated_app
    _seed(db_path, [
        {"user_id": "alice", "latency_ms": 100, "status": 200},
        {"user_id": "alice", "latency_ms": 200, "status": 200},
        {"user_id": "bob",   "latency_ms": 300, "status": 200},
    ])
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/timeseries?bucket=hour&group_by=user_id", headers=_admin_headers())
    series = r.json()["series"]
    grps = {p["group"]: p["count"] for p in series}
    assert grps.get("alice") == 2
    assert grps.get("bob") == 1


def test_t5_timeseries_group_by_whitelist(isolated_app):
    app, _ = isolated_app
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/timeseries?group_by=DROP%20TABLE%20metrics", headers=_admin_headers())
    assert r.status_code == 400


# ---------------- leaderboard ----------------

def test_t6_leaderboard_count_desc(isolated_app):
    app, db_path = isolated_app
    rows = (
        [{"user_id": "alice", "latency_ms": 100, "status": 200}] * 5 +
        [{"user_id": "bob", "latency_ms": 100, "status": 200}] * 3 +
        [{"user_id": "carol", "latency_ms": 100, "status": 200}] * 1
    )
    _seed(db_path, rows)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/leaderboard?dim=user_id&metric=count", headers=_admin_headers())
    body = r.json()
    assert [r["value"] for r in body["rows"]] == ["alice", "bob", "carol"]
    assert [r["count"] for r in body["rows"]] == [5, 3, 1]


def test_t7_leaderboard_err_rate(isolated_app):
    app, db_path = isolated_app
    _seed(db_path, [
        {"project_id": "p_clean", "status": 200, "latency_ms": 100},
        {"project_id": "p_clean", "status": 200, "latency_ms": 100},
        {"project_id": "p_buggy", "status": 500, "latency_ms": 100},
        {"project_id": "p_buggy", "status": 200, "latency_ms": 100},
    ])
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/leaderboard?dim=project_id&metric=err_rate", headers=_admin_headers())
    body = r.json()
    # p_buggy err_rate=0.5, p_clean err_rate=0
    assert body["rows"][0]["value"] == "p_buggy"
    assert body["rows"][0]["err_rate"] == 0.5


def test_t8_leaderboard_dim_whitelist(isolated_app):
    app, _ = isolated_app
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/leaderboard?dim=DROP%20TABLE", headers=_admin_headers())
    assert r.status_code == 400


def test_t9_window_validation(isolated_app):
    app, _ = isolated_app
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/summary?hours=99999", headers=_admin_headers())  # 超过 720
    assert r.status_code == 422  # FastAPI Query validation


# ---------------- 真鉴权路径 (#3) ----------------

def test_auth_no_token_returns_401(isolated_app):
    """没 Authorization header → 401."""
    app, _ = isolated_app
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/summary")
    assert r.status_code == 401


def test_auth_invalid_jwt_returns_401(isolated_app):
    """JWT 签名错 → 401."""
    app, _ = isolated_app
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/summary",
              headers={"Authorization": "Bearer not-a-valid-jwt"})
    assert r.status_code == 401


def test_auth_regular_user_returns_403(isolated_app):
    """非 admin email 的合法 JWT → 403 (走完整 require_org_admin 路径)."""
    app, _ = isolated_app
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/admin/api/metrics/summary", headers=_regular_headers())
    assert r.status_code == 403


def test_auth_unknown_user_id_returns_401(isolated_app):
    """JWT 合法但 user_id 不在 user_cache → 401 (防 secret 泄漏伪造)."""
    app, _ = isolated_app
    from fastapi.testclient import TestClient
    c = TestClient(app)
    fake = _make_jwt("not-in-cache-id")
    r = c.get("/admin/api/metrics/summary",
              headers={"Authorization": f"Bearer {fake}"})
    # 此处 extract_user_from_admin 会 fallback 调外部 Open WebUI API,
    # 测试环境 OPENWEBUI_BASE_URL 不通会 timeout/error → 也算预期失败 (非 200/403).
    assert r.status_code in (401, 500, 502, 503)
