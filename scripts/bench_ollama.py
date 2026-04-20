#!/usr/bin/env python3
"""Ollama nomic-embed-text 直测吞吐 — 判定 embedding 是不是上传瓶颈。

对比方法：
  - 这里测出 Ollama 极限吞吐 N ops/s
  - 上传压测 bench_upload 是 2.2 ops/s
  - 如果 N >> 2.2 → 瓶颈在 Letta/adapter 不在 Ollama
  - 如果 N ≈ 2.2 → 瓶颈就是 Ollama

env:
  OLLAMA_URL   默认 http://ollama:11434
  MODEL        默认 nomic-embed-text

usage:
  python bench_ollama.py                      # tiers 1,3,5,10
  python bench_ollama.py --tiers 1,5,10,20
"""
import argparse
import asyncio
import os
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

# 模拟 CSV 一行变成 md 片段的规模（~1KB 文本，中文+数字混合）
SAMPLE = (
    "员工编号,姓名,部门,城市,备注\n" +
    "\n".join(
        f"{i},员工{i:04d},{['研发','市场','运营','财务'][i%4]},"
        f"{['北京','上海','深圳','杭州','广州'][i%5]},"
        f"第{i}号员工的工作备注，主要负责一些常规业务处理工作，内容较长以增加嵌入难度"
        for i in range(20)
    )
)


@dataclass
class Result:
    ok: bool
    status: int = 0
    rt: float = 0.0
    dim: int = 0
    error: str = ""


@dataclass
class TierStat:
    concurrency: int
    total: int
    results: list = field(default_factory=list)
    wall: float = 0.0


async def embed_one(client: httpx.AsyncClient, url: str, model: str, text: str) -> Result:
    t0 = time.perf_counter()
    try:
        r = await client.post(url, json={"model": model, "prompt": text}, timeout=60)
        rt = time.perf_counter() - t0
        if r.status_code != 200:
            return Result(False, r.status_code, rt, 0, r.text[:200])
        dim = len(r.json().get("embedding", []))
        return Result(True, 200, rt, dim, "")
    except Exception as e:
        return Result(False, 0, time.perf_counter() - t0, 0, f"{type(e).__name__}:{e}")


async def run_tier(url: str, model: str, concurrency: int, total: int, text: str) -> TierStat:
    sem = asyncio.Semaphore(concurrency)
    stat = TierStat(concurrency=concurrency, total=total)
    limits = httpx.Limits(max_connections=concurrency + 5, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(limits=limits) as client:
        async def worker():
            async with sem:
                return await embed_one(client, url, model, text)
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
    succ = 100 * len(ok) / max(1, len(stat.results))
    rts = [r.rt for r in ok]
    ops = len(ok) / stat.wall if stat.wall > 0 else 0

    hist = {}
    for r in bad:
        k = r.status if r.status else r.error.split(":")[0]
        hist[k] = hist.get(k, 0) + 1

    lines = [
        f"== C={stat.concurrency:2d}  N={stat.total:2d}  wall={stat.wall:.1f}s  success={len(ok)}/{len(stat.results)} ({succ:.0f}%)  embed/s={ops:.2f}",
        f"   rt (s)  p50={pct(rts,50):.2f}  p95={pct(rts,95):.2f}  p99={pct(rts,99):.2f}  mean={(statistics.mean(rts) if rts else 0):.2f}  max={(max(rts) if rts else 0):.2f}",
    ]
    if hist:
        lines.append(f"   errors: {dict(hist)}")
        for r in bad[:2]:
            lines.append(f"     - {r.status} {r.error[:120]}")
    return "\n".join(lines)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="1,3,5,10")
    ap.add_argument("--total", type=int, help="每档请求数（默认 max(10, C*3)）")
    ap.add_argument("--ollama-url", default=os.getenv("OLLAMA_URL", "http://ollama:11434"))
    ap.add_argument("--model", default=os.getenv("MODEL", "nomic-embed-text"))
    args = ap.parse_args()

    url = args.ollama_url.rstrip("/") + "/api/embeddings"
    print(f"target: {url}")
    print(f"model:  {args.model}")
    print(f"payload: {len(SAMPLE)} chars (~{len(SAMPLE.encode())/1024:.1f}KB)\n")

    tiers = [int(x) for x in args.tiers.split(",") if x.strip()]
    for c in tiers:
        n = args.total if args.total else max(10, c * 3)
        stat = await run_tier(url, args.model, c, n, SAMPLE)
        print(report(stat), "\n")


if __name__ == "__main__":
    asyncio.run(main())
