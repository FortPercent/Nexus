#!/usr/bin/env python3
"""100 并发混合工作负载 3 分钟。
    60 chat workers (qwen-no-mem stream 100 tokens)
    20 admin read workers (/admin/api/me)
    10 upload workers (70KB CSV)
    10 passages.search workers
"""
import asyncio, json, os, time, uuid, io, csv
from collections import defaultdict
import httpx

DURATION = int(os.getenv("DURATION", "180"))
ADAPTER = "http://localhost:8000"
LETTA = "http://letta-server:8283"
API_KEY = "teleai-adapter-key-2026"

import jwt
SECRET = os.environ["OPENWEBUI_JWT_SECRET"]
ADMIN_JWT = jwt.encode({"id": "27187de5-15a3-41d1-b733-5b117f3578e6", "exp": int(time.time())+3600}, SECRET, algorithm="HS256")

# 70KB csv 放进内存共享
_buf = io.StringIO(); _w = csv.writer(_buf)
_w.writerow(["id","name","dept","city","note"])
for i in range(500): _w.writerow([i, f"员工{i}", "研发", "北京", "普通备注"])
CSV_DATA = _buf.getvalue().encode()

PROMPTS = ["你好", "介绍 Python", "讲讲 RAG", "解释 GIL"]
SEARCH_QS = ["人工智能", "项目", "系统", "测试"]
BENCH_PREFIX = f"mix100_{int(time.time())}"

buckets = defaultdict(lambda: defaultdict(lambda: {"ok":0, "bad":0, "rts":[]}))
lock = asyncio.Lock()


async def record(kind, ok, rt):
    sec = int(time.time() - START)
    minute = sec // 60
    async with lock:
        b = buckets[minute][kind]
        if ok: b["ok"] += 1
        else: b["bad"] += 1
        b["rts"].append(rt)


async def chat_worker(client, i):
    while time.time() - START < DURATION:
        t0 = time.perf_counter()
        body = {"model":"qwen-no-mem","messages":[{"role":"user","content":PROMPTS[i%len(PROMPTS)]}],
                "max_tokens":100,"stream":True}
        ok = False
        try:
            async with client.stream("POST", f"{ADAPTER}/v1/chat/completions", json=body,
                                      headers={"Authorization":f"Bearer {API_KEY}","Content-Type":"application/json"},
                                      timeout=60) as r:
                if r.status_code == 200:
                    async for line in r.aiter_lines():
                        pass
                    ok = True
        except Exception: pass
        await record("chat", ok, time.perf_counter()-t0)


async def read_worker(client, i):
    while time.time() - START < DURATION:
        t0 = time.perf_counter()
        ok = False
        try:
            r = await client.get(f"{ADAPTER}/admin/api/me", headers={"Authorization":f"Bearer {ADMIN_JWT}"}, timeout=30)
            ok = (r.status_code == 200)
        except Exception: pass
        await record("read", ok, time.perf_counter()-t0)


async def upload_worker(client, i):
    while time.time() - START < DURATION:
        t0 = time.perf_counter()
        ok = False
        try:
            fname = f"{BENCH_PREFIX}_{i}_{uuid.uuid4().hex[:6]}.csv"
            r = await client.post(f"{ADAPTER}/admin/api/personal/files",
                                   headers={"Authorization":f"Bearer {ADMIN_JWT}"},
                                   files={"file":(fname, CSV_DATA, "text/csv")}, timeout=120)
            ok = (r.status_code == 200)
        except Exception: pass
        await record("upload", ok, time.perf_counter()-t0)


async def search_worker(client, i):
    while time.time() - START < DURATION:
        t0 = time.perf_counter()
        ok = False
        try:
            r = await client.post(f"{LETTA}/v1/passages/search",
                                   json={"query": SEARCH_QS[i%len(SEARCH_QS)], "limit": 10},
                                   timeout=30)
            ok = (r.status_code == 200)
        except Exception: pass
        await record("search", ok, time.perf_counter()-t0)


def pct(xs, p):
    if not xs: return 0
    xs = sorted(xs); return xs[min(len(xs)-1, int(round(p/100*(len(xs)-1))))]


async def reporter():
    last_reported = -1
    while time.time() - START < DURATION:
        await asyncio.sleep(10)
        sec = int(time.time() - START)
        minute = sec // 60
        if minute == last_reported: continue
        async with lock:
            snap = {k: dict(v) for k, v in buckets[minute].items()}
        if not snap: continue
        print(f"[t={sec:3d}s / min={minute}]", flush=True)
        for k in ("chat","read","upload","search"):
            if k not in snap: continue
            b = snap[k]
            total = b["ok"]+b["bad"]
            rate = 100*b["ok"]/max(1,total)
            print(f"  {k:7s} ok={b['ok']:5d}/{total:5d} ({rate:.0f}%) "
                  f"rt p50={pct(b['rts'],50):.2f}s p95={pct(b['rts'],95):.2f}s", flush=True)
        last_reported = minute


async def main():
    global START
    print(f"=== mixed 100 concurrent × {DURATION}s ===")
    print(f"  60 chat + 20 read + 10 upload + 10 search\n", flush=True)
    START = time.time()
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=120)) as client:
        tasks = (
            [chat_worker(client, i) for i in range(60)] +
            [read_worker(client, i) for i in range(20)] +
            [upload_worker(client, i) for i in range(10)] +
            [search_worker(client, i) for i in range(10)]
        )
        report = asyncio.create_task(reporter())
        await asyncio.gather(*tasks)
        report.cancel()

    print("\n=== FINAL SUMMARY (by minute) ===")
    for m in sorted(buckets):
        print(f"\n--- minute {m} ---")
        for k in ("chat","read","upload","search"):
            if k not in buckets[m]: continue
            b = buckets[m][k]
            total = b["ok"]+b["bad"]
            print(f"  {k:7s} ok={b['ok']:5d}/{total:5d} ({100*b['ok']/max(1,total):3.0f}%) "
                  f"p50={pct(b['rts'],50):.2f} p95={pct(b['rts'],95):.2f} p99={pct(b['rts'],99):.2f}")

    # cleanup uploaded files
    print("\n=== cleanup ===")
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{ADAPTER}/admin/api/personal/files",
                             headers={"Authorization":f"Bearer {ADMIN_JWT}"}, timeout=30)
        files = [f for f in r.json() if BENCH_PREFIX in f.get("name","")]
        deleted = 0
        for f in files:
            try:
                await client.delete(f"{ADAPTER}/admin/api/personal/files/{f['id']}",
                                    headers={"Authorization":f"Bearer {ADMIN_JWT}"}, timeout=30)
                deleted += 1
            except: pass
        print(f"deleted {deleted} mix100 files")

asyncio.run(main())
