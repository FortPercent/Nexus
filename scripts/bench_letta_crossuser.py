#!/usr/bin/env python3
"""letta-* 跨用户并发 —— 8 个真实用户，每次请求轮询到不同 user/agent。"""
import asyncio, json, statistics, sys, time
import httpx

USERS = [
    ("f1dfb0ed-0c2b-4337-922a-cbc86859dfde", "biany4@chinatelecom.cn", "biany4", "ai-infra"),
    ("45df5b44-4f30-43df-aab6-255a29230936", "jinyx5@chinatelecom.cn", "jinyx5", "ai-infra-cache"),
    ("07a3a6ae-ec73-44ed-aff4-00d92f526e0c", "liuyr17@chinatelecom.cn", "liuyr17", "ai-infra"),
    ("3cfb6688-9362-4afb-963e-e8b4cc4474f3", "qiruoling760@gmail.com", "qiruoling", "01"),
    ("29141fcf-101c-4d1e-a136-98de1dab0c34", "wanglw11@chinatelecom.cn", "wanglw11", "infra-intern"),
    ("0245fc8f-e9b0-41a4-9921-13dba97fe875", "wengqzh@chinatelecom.cn", "wengqzh", "ai-infra"),
    ("ce1d405b-0b5c-4faf-8864-010e2611b900", "wuxn5@chinatelecom.cn", "wuxn5", "ai-infra"),
]

PROMPTS = ["你好", "介绍一下自己", "今天天气怎么样"]

async def one(client, user, prompt):
    uid, email, name, proj = user
    body = {
        "model": f"letta-{proj}",
        "messages": [{"role": "user", "content": prompt}],
        "user_id": uid, "user_email": email, "user_name": name,
        "stream": True,
    }
    t0 = time.perf_counter()
    ttft = 0.0; chunks = 0
    try:
        async with client.stream("POST", "http://localhost:8000/v1/chat/completions",
                                  json=body, headers={"Authorization": "Bearer teleai-adapter-key-2026",
                                                      "Content-Type": "application/json"},
                                  timeout=180) as r:
            if r.status_code != 200:
                body_b = (await r.aread()).decode(errors="ignore")[:120]
                return False, r.status_code, 0, time.perf_counter()-t0, 0, body_b, name
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
                    chunks += 1
        return True, 200, ttft, time.perf_counter()-t0, chunks, "", name
    except Exception as e:
        return False, 0, 0, time.perf_counter()-t0, 0, f"{type(e).__name__}", name

def pct(xs, p):
    if not xs: return 0
    xs = sorted(xs); return xs[min(len(xs)-1, int(round(p/100*(len(xs)-1))))]

async def tier(c, n):
    sem = asyncio.Semaphore(c)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=c+5)) as client:
        async def w(i):
            async with sem:
                u = USERS[i % len(USERS)]
                return await one(client, u, PROMPTS[i % len(PROMPTS)])
        t0 = time.perf_counter()
        res = await asyncio.gather(*(w(i) for i in range(n)))
        wall = time.perf_counter()-t0
    ok = [r for r in res if r[0]]
    bad = [r for r in res if not r[0]]
    ttfts = [r[2] for r in ok]
    totals = [r[3] for r in ok]
    print(f"C={c} N={n} wall={wall:.1f}s succ={len(ok)}/{n} "
          f"TTFT p50={pct(ttfts,50):.2f}/p95={pct(ttfts,95):.2f}/p99={pct(ttfts,99):.2f}  "
          f"total p50={pct(totals,50):.2f}/p95={pct(totals,95):.2f}")
    if bad:
        hist = {}
        for r in bad: hist[f"{r[6]}:{r[1] or r[5]}"] = hist.get(f"{r[6]}:{r[1] or r[5]}", 0)+1
        print(f"  errors: {hist}")

async def main():
    tiers = [int(x) for x in sys.argv[1].split(",")] if len(sys.argv) > 1 else [5, 10, 15]
    for c in tiers:
        await tier(c, max(10, c*2))

asyncio.run(main())
