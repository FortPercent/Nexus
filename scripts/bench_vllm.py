#!/usr/bin/env python3
"""vLLM 直连压测 — 阶梯并发，流式采样 TTFT / 总延迟 / 吞吐。

env:
  VLLM_ENDPOINT  必填  形如 http://.../v1
  VLLM_API_KEY   必填
  VLLM_MODEL     可选  默认 Qwen3.5-122B-A10B

usage:
  python bench_vllm.py                         # 默认阶梯 1,5,10,25,50,100
  python bench_vllm.py --tiers 1,10,50
  python bench_vllm.py --concurrency 50 --total 100
  python bench_vllm.py --max-tokens 300 --warmup 2
"""
import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

PROMPTS = [
    "简要介绍一下量子计算的基本原理。",
    "帮我写一段 Python 代码，实现快速排序。",
    "解释一下 Transformer 架构中 self-attention 的计算过程。",
    "用一段话总结《三体》这本书讲了什么。",
    "2026 年人工智能领域有哪些值得关注的趋势？",
    "给我列 5 个提高睡眠质量的方法。",
    "Kubernetes 的 Pod 和 Deployment 有什么区别？",
    "请用中文翻译：The quick brown fox jumps over the lazy dog.",
]


@dataclass
class Result:
    ok: bool
    status: int = 0
    ttft: float = 0.0          # seconds
    total: float = 0.0         # seconds
    out_tokens: int = 0
    error: str = ""


@dataclass
class TierStat:
    concurrency: int
    total: int
    results: list = field(default_factory=list)
    wall: float = 0.0


async def one_request(client: httpx.AsyncClient, url: str, api_key: str, model: str,
                      prompt: str, max_tokens: int, timeout: float) -> Result:
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    t0 = time.perf_counter()
    ttft = 0.0
    out_tokens = 0
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
                text = choices[0].get("text") or choices[0].get("delta", {}).get("content") or ""
                if text:
                    if ttft == 0.0:
                        ttft = time.perf_counter() - t0
                    out_tokens += 1  # 粗估：一个 SSE chunk ≈ 一个 token（vLLM 流式默认如此）
        total = time.perf_counter() - t0
        return Result(True, 200, ttft, total, out_tokens, "")
    except (httpx.TimeoutException, asyncio.TimeoutError) as e:
        return Result(False, 0, 0, time.perf_counter() - t0, 0, f"timeout:{e}")
    except Exception as e:
        return Result(False, 0, 0, time.perf_counter() - t0, 0, f"{type(e).__name__}:{e}")


async def run_tier(url: str, api_key: str, model: str, concurrency: int,
                   total: int, max_tokens: int, timeout: float) -> TierStat:
    sem = asyncio.Semaphore(concurrency)
    stat = TierStat(concurrency=concurrency, total=total)
    limits = httpx.Limits(max_connections=concurrency + 10, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(limits=limits) as client:
        async def worker(i: int) -> Result:
            async with sem:
                prompt = PROMPTS[i % len(PROMPTS)]
                return await one_request(client, url, api_key, model, prompt, max_tokens, timeout)

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


def report(stat: TierStat) -> str:
    ok = [r for r in stat.results if r.ok]
    bad = [r for r in stat.results if not r.ok]
    succ_rate = 100.0 * len(ok) / max(1, len(stat.results))

    ttfts = [r.ttft for r in ok]
    totals = [r.total for r in ok]
    toks = [r.out_tokens for r in ok]
    tok_rates = [r.out_tokens / r.total for r in ok if r.total > 0 and r.out_tokens > 0]
    agg_tps = sum(toks) / stat.wall if stat.wall > 0 else 0.0

    status_hist = {}
    for r in bad:
        key = r.status if r.status else r.error.split(":")[0]
        status_hist[key] = status_hist.get(key, 0) + 1

    lines = [
        f"== C={stat.concurrency:3d}  N={stat.total:3d}  wall={stat.wall:.1f}s  success={len(ok)}/{len(stat.results)} ({succ_rate:.0f}%)",
        f"   TTFT    (s)  p50={pct(ttfts, 50):.2f}  p95={pct(ttfts, 95):.2f}  p99={pct(ttfts, 99):.2f}  mean={statistics.mean(ttfts) if ttfts else 0:.2f}",
        f"   total   (s)  p50={pct(totals, 50):.2f}  p95={pct(totals, 95):.2f}  p99={pct(totals, 99):.2f}  mean={statistics.mean(totals) if totals else 0:.2f}",
        f"   out tok      p50={pct(toks, 50):.0f}    p95={pct(toks, 95):.0f}    mean={statistics.mean(toks) if toks else 0:.0f}",
        f"   per-req tok/s  p50={pct(tok_rates, 50):.1f}  p95={pct(tok_rates, 95):.1f}  mean={statistics.mean(tok_rates) if tok_rates else 0:.1f}",
        f"   aggregate throughput:  {agg_tps:.1f} tok/s",
    ]
    if status_hist:
        lines.append(f"   errors: {dict(status_hist)}")
        for r in bad[:3]:
            lines.append(f"     - {r.status} {r.error[:120]}")
    return "\n".join(lines)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="1,5,10,25,50,100",
                    help="并发阶梯，逗号分隔（当 --concurrency 未指定时生效）")
    ap.add_argument("--concurrency", type=int, help="单点压：并发数（指定后忽略 --tiers）")
    ap.add_argument("--total", type=int, help="单点压：总请求数（默认 max(10, C*2)）")
    ap.add_argument("--max-tokens", type=int, default=300)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--warmup", type=int, default=2, help="正式阶梯前预热请求数")
    ap.add_argument("--endpoint", default=os.environ.get("VLLM_ENDPOINT", ""))
    ap.add_argument("--api-key", default=os.environ.get("VLLM_API_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("VLLM_MODEL", "Qwen3.5-122B-A10B"))
    args = ap.parse_args()

    if not args.endpoint or not args.api_key:
        print("ERROR: VLLM_ENDPOINT 和 VLLM_API_KEY 必填（env 或 --endpoint/--api-key）", file=sys.stderr)
        sys.exit(2)

    url = args.endpoint.rstrip("/") + "/completions"
    print(f"target: {url}")
    print(f"model:  {args.model}")
    print(f"max_tokens={args.max_tokens}  timeout={args.timeout}s\n")

    if args.warmup > 0:
        print(f"-- warmup ({args.warmup}) --")
        w = await run_tier(url, args.api_key, args.model, args.warmup, args.warmup, args.max_tokens, args.timeout)
        print(report(w), "\n")

    if args.concurrency:
        tiers = [args.concurrency]
    else:
        tiers = [int(x) for x in args.tiers.split(",") if x.strip()]

    for c in tiers:
        n = args.total if args.concurrency and args.total else max(10, c * 2)
        stat = await run_tier(url, args.api_key, args.model, c, n, args.max_tokens, args.timeout)
        print(report(stat), "\n")


if __name__ == "__main__":
    asyncio.run(main())
