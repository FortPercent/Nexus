#!/usr/bin/env python3
"""adapter 聊天链路 E2E 压测 —— 测 vLLM 放开后 adapter 层真实吞吐。

环境：VLLM_ENDPOINT / VLLM_API_KEY 已经通过 .env 传给 adapter
目标：adapter /v1/chat/completions 的 qwen-no-mem 流式吞吐 vs 直连 vLLM

usage:
  python bench_adapter_chat.py                   # 默认 1,10,25,50,100
  python bench_adapter_chat.py --tiers 100,200
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

PROMPTS = [
    "简要介绍一下量子计算的基本原理。",
    "帮我写一段 Python 快速排序。",
    "解释 Transformer self-attention 计算过程。",
    "用一段话总结《三体》。",
    "2026 年 AI 有哪些趋势？",
    "列 5 个提高睡眠的方法。",
    "K8s Pod 和 Deployment 区别？",
    "中文翻译: The quick brown fox.",
]


@dataclass
class Result:
    ok: bool
    status: int = 0
    ttft: float = 0.0
    total: float = 0.0
    chunks: int = 0
    error: str = ""


@dataclass
class TierStat:
    concurrency: int
    total: int
    results: list = field(default_factory=list)
    wall: float = 0.0


async def one_request(client, url, api_key, prompt, max_tokens, timeout):
    payload = {
        "model": "qwen-no-mem",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    t0 = time.perf_counter()
    ttft = 0.0
    chunks = 0
    try:
        async with client.stream("POST", url, json=payload, headers=headers, timeout=timeout) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode(errors="ignore")[:200]
                return Result(False, r.status_code, 0, time.perf_counter() - t0, 0, body)
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = j.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                text = delta.get("content") or ""
                if text:
                    if ttft == 0.0:
                        ttft = time.perf_counter() - t0
                    chunks += 1
        return Result(True, 200, ttft, time.perf_counter() - t0, chunks, "")
    except Exception as e:
        return Result(False, 0, 0, time.perf_counter() - t0, 0, f"{type(e).__name__}:{e}")


async def run_tier(url, api_key, concurrency, total, max_tokens, timeout):
    sem = asyncio.Semaphore(concurrency)
    stat = TierStat(concurrency=concurrency, total=total)
    limits = httpx.Limits(max_connections=concurrency + 10, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits) as client:
        async def worker(i):
            async with sem:
                return await one_request(client, url, api_key, PROMPTS[i % len(PROMPTS)], max_tokens, timeout)
        t0 = time.perf_counter()
        stat.results = await asyncio.gather(*(worker(i) for i in range(total)))
        stat.wall = time.perf_counter() - t0
    return stat


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1)))))
    return xs[k]


def report(stat):
    ok = [r for r in stat.results if r.ok]
    bad = [r for r in stat.results if not r.ok]
    ttfts = [r.ttft for r in ok]
    totals = [r.total for r in ok]
    chunks = [r.chunks for r in ok]
    agg = sum(chunks) / stat.wall if stat.wall > 0 else 0
    hist = {}
    for r in bad:
        k = r.status if r.status else r.error.split(":")[0]
        hist[k] = hist.get(k, 0) + 1
    lines = [
        f"== C={stat.concurrency:3d} N={stat.total:3d} wall={stat.wall:.1f}s succ={len(ok)}/{len(stat.results)} ({100*len(ok)/max(1,len(stat.results)):.0f}%)",
        f"   TTFT    p50={pct(ttfts,50):.2f}  p95={pct(ttfts,95):.2f}  p99={pct(ttfts,99):.2f}  mean={(statistics.mean(ttfts) if ttfts else 0):.2f}",
        f"   total   p50={pct(totals,50):.2f}  p95={pct(totals,95):.2f}  p99={pct(totals,99):.2f}",
        f"   chunks  mean={(statistics.mean(chunks) if chunks else 0):.0f}   agg chunks/s={agg:.0f}",
    ]
    if hist:
        lines.append(f"   errors: {dict(hist)}")
        for r in bad[:2]:
            lines.append(f"     - {r.status} {r.error[:120]}")
    return "\n".join(lines)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="1,10,25,50,100")
    ap.add_argument("--max-tokens", type=int, default=300)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--adapter-url", default=os.getenv("ADAPTER_URL", "http://localhost:8000"))
    ap.add_argument("--api-key", default=os.getenv("API_KEY", "teleai-adapter-key-2026"))
    args = ap.parse_args()

    url = args.adapter_url.rstrip("/") + "/v1/chat/completions"
    print(f"target: {url}  model=qwen-no-mem stream=true")
    print(f"max_tokens={args.max_tokens}\n")

    for c in [int(x) for x in args.tiers.split(",") if x.strip()]:
        n = max(10, c * 2)
        stat = await run_tier(url, args.api_key, c, n, args.max_tokens, args.timeout)
        print(report(stat), "\n")


if __name__ == "__main__":
    asyncio.run(main())
