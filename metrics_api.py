"""Metrics 聚合 API (Issue #13 Day 3).

提供给 admin-dashboard.html 的"运营"tab. 数据源是 metrics 表 (middleware_metrics 写入).

端点:
  GET /admin/api/metrics/timeseries  — 按时间桶聚合 (count / avg / err_rate)
  GET /admin/api/metrics/leaderboard — 按维度排序 (user / project / agent)
  GET /admin/api/metrics/summary     — 全局汇总 (最近 N 小时)

V1 不上 percentile (SQLite 没原生 quantile 函数). 用 count / avg / max + err_rate 够 90% 监控
场景. p50 / p99 留 V2 切 DuckDB sqlite_scanner 时一起做.
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import require_org_admin as require_admin
from db import use_db_async

router = APIRouter(prefix="/admin/api/metrics")


# 允许的 group_by 维度白名单, 防 SQL 注入
_ALLOWED_GROUP_BY = {"endpoint", "user_id", "project_id", "agent_id", "model", "status"}
_ALLOWED_LEADERBOARD_DIM = {"user_id", "project_id", "agent_id", "endpoint", "model"}
# 时间桶粒度
_BUCKET_SQL = {
    "minute": "strftime('%Y-%m-%d %H:%M:00', datetime(ts_unix, 'unixepoch', 'localtime'))",
    "hour":   "strftime('%Y-%m-%d %H:00:00', datetime(ts_unix, 'unixepoch', 'localtime'))",
    "day":    "strftime('%Y-%m-%d 00:00:00', datetime(ts_unix, 'unixepoch', 'localtime'))",
}


def _resolve_window(from_ts: Optional[float], to_ts: Optional[float], default_hours: int = 24):
    """缺省最近 N 小时. 返回 (from_ts_unix, to_ts_unix)."""
    now = time.time()
    if to_ts is None:
        to_ts = now
    if from_ts is None:
        from_ts = to_ts - default_hours * 3600
    if from_ts >= to_ts:
        raise HTTPException(400, "from_ts must be < to_ts")
    if to_ts - from_ts > 90 * 24 * 3600:
        raise HTTPException(400, "window too large (max 90 days)")
    return from_ts, to_ts


@router.get("/summary")
async def metrics_summary(
    hours: int = Query(24, ge=1, le=720),
    user=Depends(require_admin),
):
    """最近 N 小时的全局汇总: 总请求数 / 平均延迟 / 错误率 / token 总量."""
    from_ts = time.time() - hours * 3600
    async with use_db_async() as db:
        async with db.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                COALESCE(MAX(latency_ms), 0) AS max_latency_ms,
                SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) AS err_5xx,
                SUM(CASE WHEN status BETWEEN 400 AND 499 THEN 1 ELSE 0 END) AS err_4xx,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COUNT(DISTINCT user_id) AS unique_users,
                COUNT(DISTINCT project_id) AS unique_projects
            FROM metrics
            WHERE ts_unix >= ?
            """,
            (from_ts,),
        ) as cur:
            row = await cur.fetchone()

    if not row or row["total"] == 0:
        return {
            "window_hours": hours,
            "total": 0,
            "avg_latency_ms": 0,
            "max_latency_ms": 0,
            "err_rate": 0.0,
            "err_5xx": 0,
            "err_4xx": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "unique_users": 0,
            "unique_projects": 0,
        }
    total = row["total"]
    return {
        "window_hours": hours,
        "total": total,
        "avg_latency_ms": round(row["avg_latency_ms"], 1),
        "max_latency_ms": row["max_latency_ms"],
        "err_rate": round((row["err_5xx"] + row["err_4xx"]) / total, 4),
        "err_5xx": row["err_5xx"],
        "err_4xx": row["err_4xx"],
        "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"],
        "unique_users": row["unique_users"],
        "unique_projects": row["unique_projects"],
    }


@router.get("/timeseries")
async def metrics_timeseries(
    bucket: str = Query("hour", pattern="^(minute|hour|day)$"),
    hours: int = Query(24, ge=1, le=720),
    group_by: Optional[str] = Query(None, description="可选维度: endpoint/user_id/project_id/agent_id/model/status"),
    user=Depends(require_admin),
):
    """按时间桶聚合的折线图数据.

    返回:
      [{bucket: "2026-05-05 10:00:00", group: "<value or null>", count: N,
        avg_latency_ms: ..., err_rate: ..., tokens_in: ..., tokens_out: ...}, ...]
    """
    if group_by and group_by not in _ALLOWED_GROUP_BY:
        raise HTTPException(400, f"group_by must be one of {sorted(_ALLOWED_GROUP_BY)}")

    from_ts = time.time() - hours * 3600
    bucket_sql = _BUCKET_SQL[bucket]
    group_col = group_by or "'__all__'"

    sql = f"""
        SELECT
            {bucket_sql} AS bucket,
            {group_col} AS grp,
            COUNT(*) AS count,
            COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
            COALESCE(MAX(latency_ms), 0) AS max_latency_ms,
            SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS err_count,
            COALESCE(SUM(tokens_in), 0) AS tokens_in,
            COALESCE(SUM(tokens_out), 0) AS tokens_out
        FROM metrics
        WHERE ts_unix >= ?
        GROUP BY bucket, grp
        ORDER BY bucket, grp
    """
    async with use_db_async() as db:
        async with db.execute(sql, (from_ts,)) as cur:
            rows = await cur.fetchall()

    out = []
    for r in rows:
        cnt = r["count"]
        out.append({
            "bucket": r["bucket"],
            "group": r["grp"],
            "count": cnt,
            "avg_latency_ms": round(r["avg_latency_ms"], 1),
            "max_latency_ms": r["max_latency_ms"],
            "err_rate": round(r["err_count"] / cnt, 4) if cnt else 0,
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
        })
    return {
        "bucket": bucket,
        "group_by": group_by,
        "window_hours": hours,
        "series": out,
    }


@router.get("/leaderboard")
async def metrics_leaderboard(
    dim: str = Query(..., description="user_id/project_id/agent_id/endpoint/model"),
    hours: int = Query(24, ge=1, le=720),
    metric: str = Query("count", pattern="^(count|avg_latency|err_rate|tokens)$"),
    top: int = Query(10, ge=1, le=100),
    user=Depends(require_admin),
):
    """按维度排行 — 哪个 user / project / agent / endpoint 调用最多 / 最慢 / 错误率最高."""
    if dim not in _ALLOWED_LEADERBOARD_DIM:
        raise HTTPException(400, f"dim must be one of {sorted(_ALLOWED_LEADERBOARD_DIM)}")

    from_ts = time.time() - hours * 3600
    order_clause = {
        "count": "count DESC",
        "avg_latency": "avg_latency_ms DESC",
        "err_rate": "err_rate DESC, count DESC",
        "tokens": "(tokens_in + tokens_out) DESC",
    }[metric]

    # 按维度 join 友好显示名 (user_id → user_cache.name/email; project_id → projects.name)
    if dim == "user_id":
        join_clause = "LEFT JOIN user_cache uc ON uc.user_id = m.user_id"
        display_select = "uc.name AS display_name, uc.email AS display_email"
    elif dim == "project_id":
        join_clause = "LEFT JOIN projects p ON p.project_id = m.project_id"
        display_select = "p.name AS display_name, NULL AS display_email"
    else:
        join_clause = ""
        display_select = "NULL AS display_name, NULL AS display_email"

    sql = f"""
        SELECT
            m.{dim} AS dim_value,
            {display_select},
            COUNT(*) AS count,
            COALESCE(AVG(m.latency_ms), 0) AS avg_latency_ms,
            COALESCE(MAX(m.latency_ms), 0) AS max_latency_ms,
            SUM(CASE WHEN m.status >= 400 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS err_rate,
            COALESCE(SUM(m.tokens_in), 0) AS tokens_in,
            COALESCE(SUM(m.tokens_out), 0) AS tokens_out
        FROM metrics m
        {join_clause}
        WHERE m.ts_unix >= ?
        GROUP BY m.{dim}
        HAVING m.{dim} IS NOT NULL AND m.{dim} != ''
        ORDER BY {order_clause}
        LIMIT ?
    """
    async with use_db_async() as db:
        async with db.execute(sql, (from_ts, top)) as cur:
            rows = await cur.fetchall()

    return {
        "dim": dim,
        "metric": metric,
        "window_hours": hours,
        "rows": [
            {
                "value": r["dim_value"],
                "display_name": r["display_name"],
                "display_email": r["display_email"],
                "count": r["count"],
                "avg_latency_ms": round(r["avg_latency_ms"], 1),
                "max_latency_ms": r["max_latency_ms"],
                "err_rate": round(r["err_rate"], 4),
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
            }
            for r in rows
        ],
    }
