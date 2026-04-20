#!/usr/bin/env python3
"""adapter 只读 API 并发压测 — 测 uvicorn 单 worker + SQLite 读锁在高并发下的表现。

env:
  ADAPTER_URL   默认 http://localhost:8000
  WEBUI_URL     默认 http://172.17.0.1:3000
  ADMIN_EMAIL   默认 admin@aiinfra.local
  ADMIN_PASSWORD 默认 AIinfra@2026

usage:
  python bench_adapter_read.py                     # 默认 1,10,25,50,100
  python bench_adapter_read.py --tiers 1,50,100 --path /admin/api/me

端点候选：
  /admin/api/me        最轻：JWT decode + 1 次 SQLite 读
  /admin/api/projects  稍重：JWT decode + SQLite JOIN
"""
import argparse
import asyncio
import os
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class Result:
    ok: bool
    status: int = 0
    rt: float = 0.0      # seconds, round-trip
    error: str = ""


@dataclass
class TierStat:
    concurrency: int
    total: int
    results: list = field(default_factory=list)
    wall: float = 0.0


async def signin(webui_url: str, email: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{webui_url}/api/v1/auths/signin",
                              json={"email": email, "password": password})
        r.raise_for_status()
        return r.json()["token"]


async def one_request(client: httpx.AsyncClient, url: str, jwt: str) -> Result:
    t0 = time.perf_counter()
    try:
        r = await client.get(url, headers={"Authorization": f"Bearer {jwt}"}, timeout=60)
        rt = time.perf_counter() - t0
        if r.status_code != 200:
            return Result(False, r.status_code, rt, r.text[:200])
        return Result(True, 200, rt, "")
    except Exception as e:
        return Result(False, 0, time.perf_counter() - t0, f"{type(e).__name__}:{e}")


async def run_tier(url: str, jwt: str, concurrency: int, total: int) -> TierStat:
    sem = asyncio.Semaphore(concurrency)
    stat = TierStat(concurrency=concurrency, total=total)
    limits = httpx.Limits(max_connections=concurrency + 10, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(limits=limits) as client:
        async def worker():
            async with sem:
                return await one_request(client, url, jwt)
        t0 = time.perf_counter()
        stat.results = await asyncio.gather(*(worker() for _ in range(total)))
        stat.wall = time.perf_counter() - t0
    return stat


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1)))))
    return xs[k]


def report(stat: TierStat) -> str:
    ok = [r for r in stat.results if r.ok]
    bad = [r for r in stat.results if not r.ok]
    succ_rate = 100.0 * len(ok) / max(1, len(stat.results))
    rts = [r.rt for r in ok]
    agg_qps = len(ok) / stat.wall if stat.wall > 0 else 0.0

    hist = {}
    for r in bad:
        k = r.status if r.status else r.error.split(":")[0]
        hist[k] = hist.get(k, 0) + 1

    lines = [
        f"== C={stat.concurrency:3d}  N={stat.total:3d}  wall={stat.wall:.2f}s  success={len(ok)}/{len(stat.results)} ({succ_rate:.0f}%)  qps={agg_qps:.0f}",
        f"   rt (ms)  p50={pct(rts,50)*1000:.0f}  p95={pct(rts,95)*1000:.0f}  p99={pct(rts,99)*1000:.0f}  mean={(statistics.mean(rts)*1000 if rts else 0):.0f}  max={(max(rts)*1000 if rts else 0):.0f}",
    ]
    if hist:
        lines.append(f"   errors: {dict(hist)}")
        for r in bad[:2]:
            lines.append(f"     - {r.status} {r.error[:120]}")
    return "\n".join(lines)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="1,10,25,50,100")
    ap.add_argument("--path", default="/admin/api/me")
    ap.add_argument("--adapter-url", default=os.getenv("ADAPTER_URL", "http://localhost:8000"))
    ap.add_argument("--webui-url", default=os.getenv("WEBUI_URL", "http://172.17.0.1:3000"))
    ap.add_argument("--email", default=os.getenv("ADMIN_EMAIL", "admin@aiinfra.local"))
    ap.add_argument("--password", default=os.getenv("ADMIN_PASSWORD", "AIinfra@2026"))
    args = ap.parse_args()

    print(f"signin {args.email} @ {args.webui_url} ...")
    try:
        jwt = await signin(args.webui_url, args.email, args.password)
    except Exception as e:
        print(f"signin failed: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"got JWT len={len(jwt)}")

    url = args.adapter_url.rstrip("/") + args.path
    print(f"target: {url}\n")

    tiers = [int(x) for x in args.tiers.split(",") if x.strip()]
    for c in tiers:
        n = max(20, c * 3)
        stat = await run_tier(url, jwt, c, n)
        print(report(stat), "\n")


if __name__ == "__main__":
    asyncio.run(main())
