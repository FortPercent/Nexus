#!/usr/bin/env python3
"""上传 ~20MB CSV × C=10 并发，测大文件端到端延迟 + 成功率。"""
import asyncio, time, os, uuid, json, urllib.request
import httpx
import sys

ADAPTER = "http://localhost:8000"
BENCH_PREFIX = f"bigfile_{os.getpid()}_{int(time.time())}"

def signin():
    d=json.dumps({"email":"admin@aiinfra.local","password":"AIinfra@2026"}).encode()
    req=urllib.request.Request("http://172.17.0.1:3000/api/v1/auths/signin",data=d,headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req,timeout=10).read())["token"]

async def upload_one(client, jwt, fname, data):
    t0 = time.perf_counter()
    try:
        files = {"file": (fname, data, "text/csv")}
        r = await client.post(f"{ADAPTER}/admin/api/personal/files",
                              headers={"Authorization": f"Bearer {jwt}"}, files=files, timeout=600)
        rt = time.perf_counter()-t0
        if r.status_code != 200:
            return False, r.status_code, rt, r.text[:200]
        return True, 200, rt, ""
    except Exception as e:
        return False, 0, time.perf_counter()-t0, f"{type(e).__name__}:{e}"

async def cleanup(jwt):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{ADAPTER}/admin/api/personal/files",
                             headers={"Authorization": f"Bearer {jwt}"}, timeout=30)
        files = [f for f in r.json() if BENCH_PREFIX in f.get("name","")]
        deleted = 0
        for f in files:
            try:
                await client.delete(f"{ADAPTER}/admin/api/personal/files/{f['id']}",
                                     headers={"Authorization": f"Bearer {jwt}"}, timeout=30)
                deleted += 1
            except: pass
        return deleted

async def main():
    concurrency = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    total = int(sys.argv[2]) if len(sys.argv) > 2 else concurrency * 2
    jwt = signin()
    with open("/tmp/big.csv","rb") as f: data = f.read()
    size_mb = len(data)/1024/1024
    print(f"bench: C={concurrency} N={total} file={size_mb:.1f}MB prefix={BENCH_PREFIX}")

    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=concurrency+5)) as client:
        async def w(i):
            async with sem:
                fname = f"{BENCH_PREFIX}_{i}_{uuid.uuid4().hex[:6]}.csv"
                return await upload_one(client, jwt, fname, data)
        t0 = time.perf_counter()
        results = await asyncio.gather(*(w(i) for i in range(total)))
        wall = time.perf_counter()-t0

    ok = [r for r in results if r[0]]
    rts = sorted([r[2] for r in ok])
    def pct(p):
        if not rts: return 0
        return rts[min(len(rts)-1, int(round(p/100*(len(rts)-1))))]
    print(f"result: wall={wall:.1f}s ok={len(ok)}/{total} upload/s={len(ok)/wall:.2f}")
    print(f"  rt p50={pct(50):.1f}s p95={pct(95):.1f}s p99={pct(99):.1f}s max={rts[-1] if rts else 0:.1f}s")
    bad = [r for r in results if not r[0]]
    if bad:
        hist = {}
        for r in bad: hist[r[1] or r[3][:30]] = hist.get(r[1] or r[3][:30], 0)+1
        print(f"  errors: {hist}")

    print("-- cleanup --")
    n = await cleanup(jwt)
    print(f"deleted {n} files")

asyncio.run(main())
