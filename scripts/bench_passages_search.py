#!/usr/bin/env python3
"""passages.search 吞吐测试：Letta archival memory search。"""
import asyncio, time, statistics, sys
import httpx

QUERIES = ["人工智能", "用户信息", "项目进展", "系统设计", "测试方法",
           "部署流程", "性能优化", "知识管理"]

LETTA_URL = "http://letta-server:8283"

async def one(client, query):
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{LETTA_URL}/v1/passages/search",
                              json={"query": query, "limit": 10}, timeout=30)
        rt = time.perf_counter()-t0
        if r.status_code != 200:
            return False, r.status_code, rt, r.text[:150]
        data = r.json()
        n = len(data) if isinstance(data, list) else 0
        return True, 200, rt, n
    except Exception as e:
        return False, 0, time.perf_counter()-t0, f"{type(e).__name__}"

def pct(xs, p):
    if not xs: return 0
    xs = sorted(xs); return xs[min(len(xs)-1, int(round(p/100*(len(xs)-1))))]

async def tier(c, n):
    sem = asyncio.Semaphore(c)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=c+5)) as client:
        async def w(i):
            async with sem:
                return await one(client, QUERIES[i % len(QUERIES)])
        t0 = time.perf_counter()
        res = await asyncio.gather(*(w(i) for i in range(n)))
        wall = time.perf_counter()-t0
    ok = [r for r in res if r[0]]
    bad = [r for r in res if not r[0]]
    rts = [r[2] for r in ok]
    counts = [r[3] for r in ok if isinstance(r[3], int)]
    print(f"C={c} N={n} wall={wall:.1f}s succ={len(ok)}/{n} qps={len(ok)/wall:.1f}  "
          f"rt p50={pct(rts,50)*1000:.0f}ms/p95={pct(rts,95)*1000:.0f}ms/p99={pct(rts,99)*1000:.0f}ms  "
          f"avg_results={statistics.mean(counts) if counts else 0:.0f}")
    if bad:
        hist = {}
        for r in bad: hist[r[1] or r[3]] = hist.get(r[1] or r[3], 0)+1
        print(f"  errors: {hist}")

async def main():
    tiers = [int(x) for x in sys.argv[1].split(",")] if len(sys.argv) > 1 else [1,5,10,25,50]
    for c in tiers:
        await tier(c, max(20, c*3))

asyncio.run(main())
