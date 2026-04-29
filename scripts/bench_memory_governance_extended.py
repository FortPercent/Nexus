#!/usr/bin/env python3
"""扩展压测 — 混合负载 / race 持续 / 长跑内存。

跑在容器内 (localhost:8000), 不打 vLLM, 不打 chat 链路。
"""
import asyncio, json, os, random, statistics, time
from collections import Counter
import sqlite3
import httpx
import jwt

ADAPTER = "http://localhost:8000"
SECRET = os.environ["OPENWEBUI_JWT_SECRET"]
USER_ID = "ce1d405b-0b5c-4faf-8864-010e2611b900"
TOKEN = jwt.encode({"id": USER_ID, "exp": int(time.time()) + 7200}, SECRET, algorithm="HS256")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
DB = "/data/serving/adapter/adapter.db"
PID = "ai-infra"


def percentile(latencies, q):
    s = sorted(latencies)
    return s[int(len(s) * q)] * 1000 if s else 0


# ===== 实验 1: 4 端点混合并发 60s =====
async def mixed_workload():
    print("\n=== 实验 1: 4 端点混合并发 60s ===")
    paths = [
        f"/memory/v1/projects/{PID}/decisions",
        f"/memory/v1/projects/{PID}/decisions/2",
        f"/memory/v1/projects/{PID}/memories/decision:2/trace",
        f"/memory/v1/projects/{PID}/conflicts",
    ]
    # 每端点 25 worker = 总 100 并发
    per_path = 25
    duration = 60

    by_path: dict[str, dict] = {p: {"lat": [], "codes": []} for p in paths}

    async def worker(client, path, stop_at):
        d = by_path[path]
        while time.perf_counter() < stop_at:
            t0 = time.perf_counter()
            try:
                r = await client.get(f"{ADAPTER}{path}", headers=HEADERS, timeout=15)
                d["codes"].append(r.status_code)
            except Exception:
                d["codes"].append(-1)
            d["lat"].append(time.perf_counter() - t0)

    async with httpx.AsyncClient() as client:
        stop_at = time.perf_counter() + duration
        tasks = []
        for p in paths:
            for _ in range(per_path):
                tasks.append(asyncio.create_task(worker(client, p, stop_at)))
        await asyncio.gather(*tasks)

    print(f"{'path':<55} {'QPS':>7} {'P50':>7} {'P95':>7} {'P99':>7} {'err':>5}")
    total_n = 0
    for p in paths:
        d = by_path[p]
        n = len(d["lat"])
        if not n: continue
        total_n += n
        codes = Counter(d["codes"])
        err = sum(c for k, c in codes.items() if k != 200) / n * 100
        short = p.split('/')[-1] if '/' in p else p
        print(f"{p:<55} {n/duration:>7.1f} {percentile(d['lat'], 0.5):>7.1f} "
              f"{percentile(d['lat'], 0.95):>7.1f} {percentile(d['lat'], 0.99):>7.1f} "
              f"{err:>4.1f}%")
    print(f"  total {duration}s, {total_n} requests")


# ===== 实验 2: race fix 持续验证 =====
async def race_sustained():
    print("\n=== 实验 2: race 持续 30s ===")
    # 持续创建 conflict + 50 并发抢解决, 看 winner 是否一致 + 数据完整
    duration = 30
    rounds = 0
    bad_rounds = 0
    total_resolves = 0

    async def attempt_resolve(client, cid):
        try:
            r = await client.post(
                f"{ADAPTER}/memory/v1/projects/{PID}/conflicts/{cid}/resolve",
                json={"strategy": "dismiss"},
                headers=HEADERS,
                timeout=10,
            )
            return r.status_code
        except Exception:
            return -1

    stop_at = time.perf_counter() + duration
    async with httpx.AsyncClient() as client:
        while time.perf_counter() < stop_at:
            # 创 conflict
            c = sqlite3.connect(DB)
            c.execute(
                """INSERT INTO memory_conflicts (project_id, memory_ids, detection_reason)
                   VALUES (?, '["file:race-a","file:race-b"]', 'race_sustained_test')""",
                (PID,),
            )
            c.commit()
            cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.close()

            # 50 并发抢
            results = await asyncio.gather(*(attempt_resolve(client, cid) for _ in range(50)))
            counts = Counter(results)
            total_resolves += len(results)
            rounds += 1
            # 期望: 1 个 200, 49 个 409
            if counts.get(200, 0) != 1 or counts.get(409, 0) != 49:
                bad_rounds += 1
                print(f"  ⚠️  round {rounds}: cid={cid} codes={dict(counts)}")

    print(f"  总轮数: {rounds}, 总 resolve 请求: {total_resolves}")
    print(f"  异常轮数: {bad_rounds} ({'✅ race fix 稳定' if bad_rounds == 0 else '❌ 仍有 race'})")

    # 清理 (保留 audit 用 cleanup 一并)
    c = sqlite3.connect(DB)
    n1 = c.execute("DELETE FROM memory_conflicts WHERE detection_reason='race_sustained_test'").rowcount
    n2 = c.execute("DELETE FROM audit_log WHERE action='memory.conflict.resolve' AND scope=?", (PID,)).rowcount
    c.commit()
    c.close()
    print(f"  cleaned {n1} conflicts + {n2} audit rows")


# ===== 实验 3: 5 分钟低并发持续 (内存 sanity) =====
async def long_haul():
    print("\n=== 实验 3: 5 分钟 20 并发持续 ===")
    duration = 300
    concurrency = 20
    paths = [
        f"/memory/v1/projects/{PID}/decisions",
        f"/memory/v1/projects/{PID}/decisions/2",
        f"/memory/v1/projects/{PID}/memories/decision:2/trace",
    ]
    lat = []
    codes = []
    last_report = time.perf_counter()
    report_every = 60

    async def worker(client, stop_at):
        nonlocal last_report
        while time.perf_counter() < stop_at:
            p = random.choice(paths)
            t0 = time.perf_counter()
            try:
                r = await client.get(f"{ADAPTER}{p}", headers=HEADERS, timeout=15)
                codes.append(r.status_code)
            except Exception:
                codes.append(-1)
            lat.append(time.perf_counter() - t0)

    async def reporter(stop_at):
        nonlocal last_report
        while time.perf_counter() < stop_at:
            await asyncio.sleep(report_every)
            elapsed = int(time.perf_counter() - (stop_at - duration))
            n = len(lat)
            if n:
                p95 = percentile(lat[-2000:], 0.95)
                err = sum(1 for c in codes[-2000:] if c != 200) / max(1, len(codes[-2000:])) * 100
                print(f"  [{elapsed}s] cumul {n} reqs, last 2000: P95 {p95:.0f}ms err {err:.1f}%")

    async with httpx.AsyncClient() as client:
        stop_at = time.perf_counter() + duration
        tasks = [asyncio.create_task(worker(client, stop_at)) for _ in range(concurrency)]
        rep = asyncio.create_task(reporter(stop_at))
        await asyncio.gather(*tasks, rep, return_exceptions=True)

    n = len(lat)
    err = sum(1 for c in codes if c != 200) / n * 100
    print(f"  total: {n} reqs in {duration}s = {n/duration:.0f} QPS")
    print(f"  全程 P50 {percentile(lat, 0.5):.0f}ms / P95 {percentile(lat, 0.95):.0f}ms / "
          f"P99 {percentile(lat, 0.99):.0f}ms / err {err:.2f}%")


async def main():
    print(f"扩展压测 开始 {time.strftime('%H:%M:%S')}")
    await mixed_workload()
    await asyncio.sleep(3)
    await race_sustained()
    await asyncio.sleep(3)
    await long_haul()
    print(f"\n结束 {time.strftime('%H:%M:%S')}")


if __name__ == "__main__":
    asyncio.run(main())
