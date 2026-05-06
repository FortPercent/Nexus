"""Local bench: 量化 metrics middleware 给请求路径加多少 overhead.

设计预算 (docs/operating-dashboard-design.md): < 5ms / 请求.

跑法:
    cd adapter && python3 scripts/bench_metrics_overhead.py

输出: 100 次请求的 mean / p50 / p99 延迟, 对比挂 vs 不挂 middleware.
本地 mac SQLite + WAL 大概 0.5-2ms/req. 上 .46 大表 + 高并发数字会变, 上线后重测.
"""
from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
sys.path.insert(0, _PARENT)

# Env 必须在 import 之前
_tmp = tempfile.mkdtemp(prefix="bench-metrics-")
os.environ.setdefault("ADAPTER_API_KEY", "test")
os.environ.setdefault("OPENWEBUI_JWT_SECRET", "test")
os.environ.setdefault("OPENWEBUI_ADMIN_EMAIL", "t@t")
os.environ.setdefault("OPENWEBUI_ADMIN_PASSWORD", "t")
os.environ.setdefault("VLLM_ENDPOINT", "http://l")
os.environ.setdefault("VLLM_API_KEY", "t")
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "adapter.db"))
os.environ.setdefault("WEBUI_DB_PATH", os.path.join(_tmp, "webui.db"))

import db
db.init_db()

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request as StRequest
from starlette.responses import JSONResponse
from starlette.routing import Route

from middleware_metrics import metrics_middleware


def _build_app(with_metrics: bool) -> FastAPI:
    app = FastAPI()
    if with_metrics:
        app.middleware("http")(metrics_middleware)

    async def echo(request: StRequest):
        return JSONResponse({"ok": True})

    app.router.routes.append(Route("/v1/echo", echo, methods=["GET"]))
    return app


def _run(app: FastAPI, n: int = 100, warmup: int = 10) -> dict:
    client = TestClient(app)
    # Warmup
    for _ in range(warmup):
        client.get("/v1/echo")
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = client.get("/v1/echo")
        samples.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200
    return {
        "n": n,
        "mean_ms": statistics.mean(samples),
        "p50_ms": statistics.median(samples),
        "p99_ms": sorted(samples)[max(0, int(0.99 * n) - 1)],
        "max_ms": max(samples),
    }


def main():
    print("[bench] 100 requests against /v1/echo  (TestClient, sync, single-thread)")
    print(f"[bench] DB: {os.environ['DB_PATH']}")
    print()

    print("[bench] WITHOUT metrics middleware (baseline)")
    base = _run(_build_app(with_metrics=False))
    print(f"  mean={base['mean_ms']:.3f}ms  p50={base['p50_ms']:.3f}ms  p99={base['p99_ms']:.3f}ms  max={base['max_ms']:.3f}ms")
    print()

    print("[bench] WITH metrics middleware (+ async aiosqlite write)")
    with_mw = _run(_build_app(with_metrics=True))
    print(f"  mean={with_mw['mean_ms']:.3f}ms  p50={with_mw['p50_ms']:.3f}ms  p99={with_mw['p99_ms']:.3f}ms  max={with_mw['max_ms']:.3f}ms")
    print()

    delta_mean = with_mw["mean_ms"] - base["mean_ms"]
    delta_p99 = with_mw["p99_ms"] - base["p99_ms"]
    print("[bench] === overhead ===")
    print(f"  Δ mean = {delta_mean:+.3f} ms  (predicted ~3-5ms for SQLite WAL single-row insert)")
    print(f"  Δ p99  = {delta_p99:+.3f} ms")

    rows_written = 0
    import sqlite3
    c = sqlite3.connect(os.environ["DB_PATH"])
    rows_written = c.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    c.close()
    print(f"[bench] rows written: {rows_written} (should equal {with_mw['n'] + 10} = bench n + warmup)")

    if delta_mean > 5.0:
        print(f"[bench] ⚠️ overhead {delta_mean:.1f}ms > 5ms budget — design doc threshold breached")
    else:
        print(f"[bench] ✅ overhead within 5ms budget")


if __name__ == "__main__":
    main()
