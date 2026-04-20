#!/usr/bin/env python3
"""adapter 文件上传并发压测 —— 测 file_processor + Ollama embedding + SQLite 写锁。

策略：
  - 生成 ~50KB CSV（500 行合成数据）
  - 文件名前缀 bench_<pid>_<ts>_，跑完按前缀清理
  - 默认阶梯 1, 3, 5, 10（Ollama 单实例，别太狠）

env:
  ADAPTER_URL   默认 http://localhost:8000
  WEBUI_URL     默认 http://172.17.0.1:3000
  ADMIN_EMAIL/ADMIN_PASSWORD

usage:
  python bench_upload.py --tiers 1,3,5,10
  python bench_upload.py --tiers 1 --total 3   # 冒烟
"""
import argparse
import asyncio
import csv
import io
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx


BENCH_PREFIX = f"bench_{os.getpid()}_{int(time.time())}"


@dataclass
class Result:
    ok: bool
    status: int = 0
    rt: float = 0.0
    uploaded: list = field(default_factory=list)
    error: str = ""


@dataclass
class TierStat:
    concurrency: int
    total: int
    results: list = field(default_factory=list)
    wall: float = 0.0


def make_csv_bytes(rows: int = 500) -> bytes:
    """生成 ~50KB CSV"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "name", "dept", "city", "note"])
    for i in range(rows):
        w.writerow([
            i,
            f"员工{i:04d}",
            ["研发", "市场", "运营", "财务"][i % 4],
            ["北京", "上海", "深圳", "杭州", "广州"][i % 5],
            f"第{i}号员工的工作备注，主要负责一些常规业务处理工作，内容较长以增加嵌入难度",
        ])
    return buf.getvalue().encode("utf-8")


async def signin(webui_url: str, email: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{webui_url}/api/v1/auths/signin",
                         json={"email": email, "password": password})
        r.raise_for_status()
        return r.json()["token"]


async def upload_one(client: httpx.AsyncClient, url: str, jwt: str,
                      filename: str, data: bytes) -> Result:
    t0 = time.perf_counter()
    try:
        files = {"file": (filename, data, "text/csv")}
        r = await client.post(url, headers={"Authorization": f"Bearer {jwt}"},
                              files=files, timeout=180)
        rt = time.perf_counter() - t0
        if r.status_code != 200:
            return Result(False, r.status_code, rt, [], r.text[:200])
        body = r.json()
        return Result(True, 200, rt, body.get("uploaded", []), "")
    except Exception as e:
        return Result(False, 0, time.perf_counter() - t0, [], f"{type(e).__name__}:{e}")


async def list_bench_files(client: httpx.AsyncClient, list_url: str, jwt: str) -> list:
    r = await client.get(list_url, headers={"Authorization": f"Bearer {jwt}"}, timeout=30)
    r.raise_for_status()
    return [f for f in r.json() if BENCH_PREFIX in (f.get("name") or "")]


async def delete_file(client: httpx.AsyncClient, del_url_tpl: str, jwt: str, file_id: str) -> bool:
    try:
        r = await client.delete(del_url_tpl.format(id=file_id),
                                headers={"Authorization": f"Bearer {jwt}"}, timeout=30)
        return r.status_code == 200
    except Exception:
        return False


async def run_tier(upload_url: str, jwt: str, concurrency: int, total: int,
                   csv_data: bytes) -> TierStat:
    sem = asyncio.Semaphore(concurrency)
    stat = TierStat(concurrency=concurrency, total=total)
    limits = httpx.Limits(max_connections=concurrency + 5, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(limits=limits) as client:
        async def worker(i):
            async with sem:
                fname = f"{BENCH_PREFIX}_c{concurrency}_{i}_{uuid.uuid4().hex[:6]}.csv"
                return await upload_one(client, upload_url, jwt, fname, csv_data)
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
    succ = 100 * len(ok) / max(1, len(stat.results))
    rts = [r.rt for r in ok]
    rps = len(ok) / stat.wall if stat.wall > 0 else 0

    hist = {}
    for r in bad:
        k = r.status if r.status else r.error.split(":")[0]
        hist[k] = hist.get(k, 0) + 1

    lines = [
        f"== C={stat.concurrency:2d}  N={stat.total:2d}  wall={stat.wall:.1f}s  success={len(ok)}/{len(stat.results)} ({succ:.0f}%)  upload/s={rps:.2f}",
        f"   rt (s)  p50={pct(rts,50):.2f}  p95={pct(rts,95):.2f}  p99={pct(rts,99):.2f}  mean={(statistics.mean(rts) if rts else 0):.2f}  max={(max(rts) if rts else 0):.2f}",
    ]
    if hist:
        lines.append(f"   errors: {dict(hist)}")
        for r in bad[:2]:
            lines.append(f"     - {r.status} {r.error[:120]}")
    return "\n".join(lines)


async def cleanup(adapter_url: str, jwt: str) -> int:
    list_url = f"{adapter_url}/admin/api/personal/files"
    del_tpl = f"{adapter_url}/admin/api/personal/files/{{id}}"
    async with httpx.AsyncClient() as client:
        files = await list_bench_files(client, list_url, jwt)
        if not files:
            return 0
        results = await asyncio.gather(*(delete_file(client, del_tpl, jwt, f["id"]) for f in files))
        return sum(1 for x in results if x)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="1,3,5,10")
    ap.add_argument("--total", type=int, help="覆盖每档请求数（默认 max(3, C*2)）")
    ap.add_argument("--rows", type=int, default=500, help="CSV 行数 (默认 500 ≈ 50KB)")
    ap.add_argument("--adapter-url", default=os.getenv("ADAPTER_URL", "http://localhost:8000"))
    ap.add_argument("--webui-url", default=os.getenv("WEBUI_URL", "http://172.17.0.1:3000"))
    ap.add_argument("--email", default=os.getenv("ADMIN_EMAIL", "admin@aiinfra.local"))
    ap.add_argument("--password", default=os.getenv("ADMIN_PASSWORD", "AIinfra@2026"))
    ap.add_argument("--skip-cleanup", action="store_true")
    args = ap.parse_args()

    print(f"bench prefix: {BENCH_PREFIX}")
    print(f"signin {args.email} ...")
    try:
        jwt = await signin(args.webui_url, args.email, args.password)
    except Exception as e:
        print(f"signin failed: {e}", file=sys.stderr)
        sys.exit(2)

    csv_data = make_csv_bytes(args.rows)
    upload_url = f"{args.adapter_url}/admin/api/personal/files"
    print(f"target: POST {upload_url}")
    print(f"payload: {len(csv_data)} bytes csv ({args.rows} rows)\n")

    tiers = [int(x) for x in args.tiers.split(",") if x.strip()]
    try:
        for c in tiers:
            n = args.total if args.total else max(3, c * 2)
            stat = await run_tier(upload_url, jwt, c, n, csv_data)
            print(report(stat), "\n")
    finally:
        if not args.skip_cleanup:
            print("-- cleanup --")
            n_deleted = await cleanup(args.adapter_url, jwt)
            print(f"deleted {n_deleted} bench files")


if __name__ == "__main__":
    asyncio.run(main())
