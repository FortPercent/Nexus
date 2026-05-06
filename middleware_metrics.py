"""请求级 metrics 采集 middleware (Issue #13 Day 1).

设计要点 (见 docs/operating-dashboard-design.md):
- 拦 /v1/*, /admin/api/*, /internal/*, /memory/v1/* 请求, 其它路径 (HTML / 静态) 不采.
- middleware 在 finally 阶段 await 写入 (单行 INSERT). _persist_metrics 内 try/except,
  失败不冒泡到业务. SQLite WAL 单行 insert ~3-5ms, 在 design doc 5ms middleware overhead 预算内.
- 不用 asyncio.create_task fire-and-forget 因为在 TestClient 同步 loop / 短命 loop 下 task 会被取消.
- request.state 提供子端点回填字段:
    - state.metrics_user_id        (auth 解出后写)
    - state.metrics_project_id     (letta-* 路由解出后写)
    - state.metrics_agent_id       (preflight 解出后写)
    - state.metrics_model          (chat path 写)
    - state.metrics_variant_id     (A/B 实验, V2)
    - state.metrics_ttft_ms        (streaming wrapper 第一个 chunk 时写)
    - state.metrics_tokens_in      (streaming wrapper / non-stream 写)
    - state.metrics_tokens_out     (同上)
    - state.metrics_err_class      (异常时分类, e.g. 'vllm_timeout')
- TTFT / tokens day1 先留空 (字段允许 NULL), day2 让 streaming wrapper 回填.
"""
import asyncio
import logging
import time
import uuid

import aiosqlite
from fastapi import Request

from config import DB_PATH

# 白名单 — 只这些前缀的请求会落 metrics
_TRACKED_PREFIXES = ("/v1/", "/admin/api/", "/internal/", "/memory/v1/")


async def _persist_metrics(
    *,
    request_id: str,
    ts_unix: float,
    user_id: str,
    project_id: str | None,
    agent_id: str | None,
    model: str | None,
    endpoint: str,
    method: str,
    status: int,
    latency_ms: int,
    ttft_ms: int | None,
    tokens_in: int,
    tokens_out: int,
    variant_id: str | None,
    err_class: str | None,
) -> None:
    """单行 INSERT to metrics. 失败只记日志, 不抛."""
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute(
                """
                INSERT INTO metrics (
                    request_id, ts_unix, user_id, project_id, agent_id,
                    model, endpoint, method, status, latency_ms,
                    ttft_ms, tokens_in, tokens_out, variant_id, err_class
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id, ts_unix, user_id, project_id, agent_id,
                    model, endpoint, method, status, latency_ms,
                    ttft_ms, tokens_in, tokens_out, variant_id, err_class,
                ),
            )
            await conn.commit()
    except Exception as e:
        logging.warning(f"[metrics] persist failed (request_id={request_id}): {e}")


def _should_track(path: str) -> bool:
    return any(path.startswith(p) for p in _TRACKED_PREFIXES)


async def metrics_middleware(request: Request, call_next):
    """采集 path/status/latency 三件套, 子端点用 request.state 回填扩展字段."""
    if not _should_track(request.url.path):
        return await call_next(request)

    request_id = (
        request.headers.get("x-request-id")
        or uuid.uuid4().hex[:16]
    )
    request.state.request_id = request_id
    request.state.metrics_user_id = ""
    request.state.metrics_project_id = None
    request.state.metrics_agent_id = None
    request.state.metrics_model = None
    request.state.metrics_variant_id = None
    request.state.metrics_ttft_ms = None
    request.state.metrics_tokens_in = 0
    request.state.metrics_tokens_out = 0
    request.state.metrics_err_class = None

    started_perf = time.perf_counter()
    started_ts = time.time()
    err_class: str | None = None
    status = 500
    response = None

    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as e:
        err_class = type(e).__name__
        raise
    finally:
        latency_ms = int((time.perf_counter() - started_perf) * 1000)
        # state 上的 err_class 可能由路由代码主动写 (e.g. vllm_timeout / letta_500)
        # 优先用 state 的, 其次用异常的
        st_err = getattr(request.state, "metrics_err_class", None)
        final_err = st_err or err_class
        # 直接 await 写入. _persist_metrics 已经 try/except 把错误吞掉,
        # 不会回溯到业务路径; SQLite WAL 单行 insert ~3-5ms, 在 5ms middleware overhead 预算内.
        # (之前用 asyncio.create_task fire-and-forget 在 TestClient 同步 loop 下会被取消, 也不写入.)
        await _persist_metrics(
            request_id=request_id,
            ts_unix=started_ts,
            user_id=getattr(request.state, "metrics_user_id", "") or "",
            project_id=getattr(request.state, "metrics_project_id", None),
            agent_id=getattr(request.state, "metrics_agent_id", None),
            model=getattr(request.state, "metrics_model", None),
            endpoint=request.url.path,
            method=request.method,
            status=status,
            latency_ms=latency_ms,
            ttft_ms=getattr(request.state, "metrics_ttft_ms", None),
            tokens_in=int(getattr(request.state, "metrics_tokens_in", 0) or 0),
            tokens_out=int(getattr(request.state, "metrics_tokens_out", 0) or 0),
            variant_id=getattr(request.state, "metrics_variant_id", None),
            err_class=final_err,
        )

    return response
