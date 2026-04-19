#!/usr/bin/env python3
"""持续负载：C=30 worker 并发聊天 30 分钟，每分钟输出一行统计。"""
import asyncio, json, time, statistics, sys
import httpx

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 1800  # 秒
CONCURRENCY = int(sys.argv[2]) if len(sys.argv) > 2 else 30

PROMPTS = ["简要介绍量子计算", "写个快排 Python", "什么是 MCP", "今天天气怎么样"]

buckets_lock = asyncio.Lock()
current_bucket = {"ttfts": [], "totals": [], "ok": 0, "bad": 0, "errors": {}}

async def one(client, i):
    body = {"model": "qwen-no-mem", "messages": [{"role": "user", "content": PROMPTS[i % len(PROMPTS)]}],
            "max_tokens": 150, "temperature": 0.7, "stream": True}
    t0 = time.perf_counter()
    ttft = 0.0; ok = False; err = ""
    try:
        async with client.stream("POST", "http://localhost:8000/v1/chat/completions",
                                  json=body, headers={"Authorization": "Bearer teleai-adapter-key-2026",
                                                      "Content-Type": "application/json"},
                                  timeout=60) as r:
            if r.status_code != 200:
                err = f"HTTP{r.status_code}"
            else:
                async for line in r.aiter_lines():
                    if not line.startswith("data:"): continue
                    data = line[5:].strip()
                    if data == "[DONE]": break
                    try: j = json.loads(data)
                    except: continue
                    if not j.get("choices"): continue
                    d = j["choices"][0].get("delta", {}).get("content") or ""
                    if d:
                        if ttft == 0.0: ttft = time.perf_counter()-t0
                ok = True
    except Exception as e:
        err = type(e).__name__
    total = time.perf_counter()-t0
    async with buckets_lock:
        if ok:
            current_bucket["ok"] += 1
            current_bucket["ttfts"].append(ttft)
            current_bucket["totals"].append(total)
        else:
            current_bucket["bad"] += 1
            current_bucket["errors"][err] = current_bucket["errors"].get(err, 0) + 1

def pct(xs, p):
    if not xs: return 0
    xs = sorted(xs); return xs[min(len(xs)-1, int(round(p/100*(len(xs)-1))))]

async def reporter(start, end):
    global current_bucket
    next_t = start + 60
    bucket_start = start
    while time.time() < end:
        await asyncio.sleep(1)
        if time.time() >= next_t:
            async with buckets_lock:
                b = current_bucket
                current_bucket = {"ttfts": [], "totals": [], "ok": 0, "bad": 0, "errors": {}}
            elapsed = int(time.time() - start)
            dur = time.time() - bucket_start
            rps = (b["ok"] + b["bad"]) / dur if dur > 0 else 0
            print(f"[t={elapsed:4d}s] ok={b['ok']:3d} bad={b['bad']:2d} rps={rps:.2f}  "
                  f"TTFT p50={pct(b['ttfts'],50):.2f}/p95={pct(b['ttfts'],95):.2f}  "
                  f"total p50={pct(b['totals'],50):.2f}/p95={pct(b['totals'],95):.2f}  "
                  f"err={b['errors'] if b['errors'] else '-'}", flush=True)
            bucket_start = time.time()
            next_t += 60

async def worker(client, i):
    end = time.time() + DURATION
    k = 0
    while time.time() < end:
        await one(client, i * 1000 + k)
        k += 1

async def main():
    print(f"sustained: C={CONCURRENCY} duration={DURATION}s", flush=True)
    start = time.time()
    end = start + DURATION
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=CONCURRENCY+10)) as client:
        tasks = [asyncio.create_task(worker(client, i)) for i in range(CONCURRENCY)]
        report_task = asyncio.create_task(reporter(start, end))
        await asyncio.gather(*tasks)
        report_task.cancel()
    print("== DONE ==", flush=True)

asyncio.run(main())
