#!/usr/bin/env python3
"""慢客户端测试：stream 连接建立后，每 100ms 只读 1 行（模拟 1KB/s 弱网）。
验证 adapter event loop 不被单个慢客户端拖垮。

- 组 A: 5 个慢客户端同时在线接 stream（每个读 500 tok，每 100ms 读 1 chunk）
- 组 B: 同时 5 个正常客户端 chat
- 观察 B 组是否被 A 拖慢
"""
import asyncio, time, json, statistics
import httpx

ADAPTER = "http://localhost:8000/v1/chat/completions"
API_KEY = "teleai-adapter-key-2026"

async def slow_client(client, i):
    body = {"model": "qwen-no-mem", "messages":[{"role":"user","content":"写一篇 500 字的短文"}],
            "max_tokens": 500, "temperature":0.7, "stream": True}
    t0 = time.perf_counter()
    n = 0
    try:
        async with client.stream("POST", ADAPTER, json=body,
                                  headers={"Authorization":f"Bearer {API_KEY}", "Content-Type":"application/json"},
                                  timeout=120) as r:
            async for line in r.aiter_lines():
                await asyncio.sleep(0.1)  # 模拟慢读
                n += 1
        return time.perf_counter()-t0, n
    except Exception as e:
        return time.perf_counter()-t0, -1

async def fast_client(client, i):
    body = {"model": "qwen-no-mem", "messages":[{"role":"user","content":"你好"}],
            "max_tokens": 50, "temperature":0.7, "stream": True}
    t0 = time.perf_counter()
    ttft = 0.0
    try:
        async with client.stream("POST", ADAPTER, json=body,
                                  headers={"Authorization":f"Bearer {API_KEY}", "Content-Type":"application/json"},
                                  timeout=30) as r:
            async for line in r.aiter_lines():
                if not line.startswith("data:"): continue
                data = line[5:].strip()
                if data == "[DONE]": break
                try: j = json.loads(data)
                except: continue
                if j.get("choices") and j["choices"][0].get("delta",{}).get("content"):
                    if ttft == 0.0: ttft = time.perf_counter()-t0
        return True, ttft, time.perf_counter()-t0
    except Exception as e:
        return False, 0, 0

async def main():
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=20)) as client:
        # baseline: 10 fast clients alone
        print("=== baseline: 10 fast clients alone ===")
        results = await asyncio.gather(*[fast_client(client, i) for i in range(10)])
        ttfts = sorted([r[1] for r in results if r[0]])
        totals = sorted([r[2] for r in results if r[0]])
        if ttfts:
            print(f"  TTFT p50={statistics.median(ttfts)*1000:.0f}ms  total p50={statistics.median(totals)*1000:.0f}ms")

        # B: 5 slow + 5 fast concurrent
        print("=== 5 slow clients (1KB/s-ish) + 5 fast clients concurrent ===")
        slow_tasks = [asyncio.create_task(slow_client(client, i)) for i in range(5)]
        await asyncio.sleep(1)  # 让 slow 先连上
        t0 = time.perf_counter()
        fast_results = await asyncio.gather(*[fast_client(client, i) for i in range(5)])
        fast_wall = time.perf_counter()-t0
        # 后台等 slow 结束不阻塞 fast
        ttfts = sorted([r[1] for r in fast_results if r[0]])
        totals = sorted([r[2] for r in fast_results if r[0]])
        if ttfts:
            print(f"  fast under slow load: TTFT p50={statistics.median(ttfts)*1000:.0f}ms  total p50={statistics.median(totals)*1000:.0f}ms  wall={fast_wall:.1f}s")
        # cleanup slow
        await asyncio.gather(*slow_tasks, return_exceptions=True)

asyncio.run(main())
