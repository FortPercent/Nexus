#!/usr/bin/env python3
"""冷启动测试：restart letta-server，测首个 200 响应时间 + 稳态时间。
每轮做 3 次迭代取中位数。"""
import subprocess, time, json, urllib.request, statistics

def probe(endpoint, body, jwt=None, timeout=10):
    headers = {"Content-Type": "application/json"}
    if body.get("model"):
        headers["Authorization"] = "Bearer teleai-adapter-key-2026"
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return 200 if r.status == 200 else r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return -1

def run_iteration(iteration):
    body = {
        "model": "letta-ai-infra",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
        "user_id": "ce1d405b-0b5c-4faf-8864-010e2611b900",
        "user_email": "wuxn5@chinatelecom.cn",
        "user_name": "wuxn5",
        "stream": False,
    }
    print(f"--- iteration {iteration} ---")
    # 等 letta 健康
    while probe("http://localhost:9800/v1/chat/completions", body, timeout=20) != 200:
        time.sleep(0.5)
    print("  letta healthy, restarting...")

    t0 = time.perf_counter()
    subprocess.run(["docker", "restart", "teleai-letta"], capture_output=True)
    restart_cmd_returned = time.perf_counter() - t0

    first_200 = None
    health_probes = 0
    while True:
        health_probes += 1
        status = probe("http://localhost:9800/v1/chat/completions", body, timeout=15)
        now = time.perf_counter() - t0
        if status == 200:
            first_200 = now
            break
        if now > 120:
            print(f"  TIMEOUT at {now:.1f}s (last status {status})")
            break
        time.sleep(1)

    # 稳态：3 次连续 200
    stable = None
    consecutive = 0
    while consecutive < 3:
        s = probe("http://localhost:9800/v1/chat/completions", body, timeout=15)
        now = time.perf_counter() - t0
        if s == 200:
            consecutive += 1
            if consecutive == 1 and first_200 is not None:
                stable_start = now
        else:
            consecutive = 0
        if now > 180:
            break
        time.sleep(1)
    stable = time.perf_counter() - t0

    print(f"  restart cmd returned: {restart_cmd_returned:.1f}s")
    print(f"  first 200 response: {first_200:.1f}s ({health_probes} probes)")
    print(f"  stable (3x consecutive): {stable:.1f}s")
    return restart_cmd_returned, first_200, stable

results = []
for i in range(3):
    results.append(run_iteration(i + 1))
    time.sleep(5)

print("\n========== Summary ==========")
first_200s = [r[1] for r in results if r[1] is not None]
stables = [r[2] for r in results]
print(f"first 200 response (3 iters): {first_200s}")
print(f"  median={statistics.median(first_200s):.1f}s, min={min(first_200s):.1f}s, max={max(first_200s):.1f}s")
print(f"稳态 (3 次连续): {stables}")
print(f"  median={statistics.median(stables):.1f}s")
