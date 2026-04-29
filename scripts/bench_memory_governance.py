#!/usr/bin/env python3
"""治理 API 吞吐 / 延迟压测。

跑在 .46 容器内,直连 localhost:8000 (绕过 nginx, 因为我们想测 adapter 本身)。

负载分四档:
  T1: list /decisions          ── 列表 + filter, SQLite 主路径
  T2: detail /decisions/{id}   ── 含 parent + children + trace 复杂查询
  T3: trace /memories/{id}/trace ── 单 memory 的完整事件链
  T4: list /conflicts          ── 简单状态过滤

每档 N=100 并发 持续 D=20 秒, 输出 QPS / P50 / P95 / P99 / 错误率。

不测 infer (单调用 5 分钟, 压不动且占 GPU 影响其他用户)。
不测 resolve concurrent (已经单独验过 race fix, 不需重测)。

用法:
  docker exec teleai-adapter python /app/scripts/bench_memory_governance.py
"""
import asyncio
import json
import os
import statistics
import sys
import time
import urllib.parse
from collections import Counter

import httpx
import jwt

ADAPTER = "http://localhost:8000"
SECRET = os.environ["OPENWEBUI_JWT_SECRET"]
WUXN5_USER_ID = "ce1d405b-0b5c-4faf-8864-010e2611b900"  # ai-infra admin

# JWT 1 小时有效, 不走 webui signin 省一次网络
TOKEN = jwt.encode({"id": WUXN5_USER_ID, "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


async def hit(client: httpx.AsyncClient, path: str) -> tuple[int, float]:
    t0 = time.perf_counter()
    try:
        r = await client.get(f"{ADAPTER}{path}", headers=HEADERS, timeout=30)
        return r.status_code, time.perf_counter() - t0
    except Exception:
        return -1, time.perf_counter() - t0


async def worker(client, path, stop_at, latencies, codes):
    while time.perf_counter() < stop_at:
        code, dt = await hit(client, path)
        latencies.append(dt)
        codes.append(code)


async def run_tier(name: str, path: str, concurrency: int, duration_s: int) -> dict:
    latencies: list[float] = []
    codes: list[int] = []
    async with httpx.AsyncClient() as client:
        # warmup
        await hit(client, path)
        stop_at = time.perf_counter() + duration_s
        tasks = [
            asyncio.create_task(worker(client, path, stop_at, latencies, codes))
            for _ in range(concurrency)
        ]
        await asyncio.gather(*tasks)

    n = len(latencies)
    if n == 0:
        return {"name": name, "n": 0}
    qps = n / duration_s
    sorted_lat = sorted(latencies)
    p = lambda q: sorted_lat[int(n * q)] * 1000  # ms
    code_dist = dict(Counter(codes))
    err_rate = sum(c for v, c in code_dist.items() if v != 200) / n
    return {
        "name": name,
        "path": path,
        "n": n,
        "concurrency": concurrency,
        "duration_s": duration_s,
        "qps": round(qps, 1),
        "p50_ms": round(p(0.5), 1),
        "p95_ms": round(p(0.95), 1),
        "p99_ms": round(p(0.99), 1),
        "max_ms": round(sorted_lat[-1] * 1000, 1),
        "codes": code_dist,
        "err_rate": round(err_rate * 100, 2),
    }


async def main():
    pid = "ai-infra"
    print(f"压测目标: {ADAPTER}/memory/v1 (project={pid}, user=wuxn5)")
    print(f"开始: {time.strftime('%H:%M:%S')}\n")

    tiers = [
        ("T1 list /decisions",       f"/memory/v1/projects/{pid}/decisions",                      100, 20),
        ("T2 detail /decisions/2",   f"/memory/v1/projects/{pid}/decisions/2",                    50,  20),
        ("T3 trace /decision:2",     f"/memory/v1/projects/{pid}/memories/decision:2/trace",      50,  20),
        ("T4 list /conflicts",       f"/memory/v1/projects/{pid}/conflicts",                      100, 20),
        ("T5 search Kimi",           f"/memory/v1/projects/{pid}/search?q=Kimi",                  100, 20),
        ("T6 search 推理底座",        f"/memory/v1/projects/{pid}/search?q=" + urllib.parse.quote("推理底座"), 50, 20),
    ]

    results = []
    for name, path, conc, dur in tiers:
        print(f"[{name}] concurrency={conc} duration={dur}s ...", flush=True)
        r = await run_tier(name, path, conc, dur)
        results.append(r)
        print(f"  → QPS {r['qps']}  P50 {r['p50_ms']}ms  P95 {r['p95_ms']}ms  P99 {r['p99_ms']}ms  "
              f"max {r['max_ms']}ms  err {r['err_rate']}%  codes={r['codes']}")
        await asyncio.sleep(2)  # 间隙让 SQLite WAL 写盘

    print("\n=== 汇总 ===")
    print(f"{'tier':<28} {'QPS':>7} {'P50':>7} {'P95':>7} {'P99':>7} {'err':>6}")
    for r in results:
        if r.get("n"):
            print(f"{r['name']:<28} {r['qps']:>7.1f} {r['p50_ms']:>7.1f} {r['p95_ms']:>7.1f} "
                  f"{r['p99_ms']:>7.1f} {r['err_rate']:>5.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
