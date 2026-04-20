#!/usr/bin/env python3
"""崩溃恢复测试：kill 一个目标容器，观察服务恢复行为。

流程：
  1. 起 C=5 持续聊天 worker（qwen-no-mem stream，轻载不 DoS）
  2. 10 秒后触发 `docker kill` 目标容器
  3. 继续打 60 秒，每秒记录成功/失败
  4. 输出：kill 瞬间失败率 / 首个 200 响应时间 / 稳态恢复时间（>95% 成功）

usage (container 内跑不了 docker kill，必须宿主机跑):
  ssh infra46@192.168.151.46 'python3 /home/infra46/teleai-adapter/scripts/test_crash_recovery.py letta'
  参数：letta | adapter | ollama
"""
import asyncio
import json
import subprocess
import sys
import time
from collections import defaultdict

import httpx

TARGET_CONTAINER = {
    "letta": "teleai-letta",
    "adapter": "teleai-adapter",
    "ollama": "ollama",
}

ADAPTER_URL = "http://localhost:9800"  # 宿主机走 nginx → adapter:8000
API_KEY = "teleai-adapter-key-2026"
CONCURRENCY = 5
KILL_AT = 10          # 秒：开始后多久触发 kill
DURATION = 90         # 秒：总观察时长

PROMPTS = ["你好", "介绍自己", "写个 hello world"]

# 默认测 qwen-no-mem；加 --model letta-ai-infra 切到 letta 路径
MODEL = "qwen-no-mem"
LETTA_USER = {
    "user_id": "ce1d405b-0b5c-4faf-8864-010e2611b900",
    "user_email": "wuxn5@chinatelecom.cn",
    "user_name": "wuxn5",
}


async def one_request(client, second_bucket, i):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPTS[i % len(PROMPTS)]}],
        "max_tokens": 50, "temperature": 0.7, "stream": True,
    }
    if MODEL.startswith("letta-"):
        body.update(LETTA_USER)
    t0 = time.perf_counter()
    sec = int(time.time() - START)
    ok = False
    err = ""
    try:
        async with client.stream("POST", f"{ADAPTER_URL}/v1/chat/completions",
                                  json=body,
                                  headers={"Authorization": f"Bearer {API_KEY}",
                                           "Content-Type": "application/json"},
                                  timeout=30) as r:
            if r.status_code != 200:
                err = f"HTTP{r.status_code}"
            else:
                got_content = False
                async for line in r.aiter_lines():
                    if not line.startswith("data:"): continue
                    data = line[5:].strip()
                    if data == "[DONE]": break
                    try: j = json.loads(data)
                    except: continue
                    if not j.get("choices"): continue
                    if j["choices"][0].get("delta", {}).get("content"):
                        got_content = True
                if got_content:
                    ok = True
                else:
                    err = "empty"
    except httpx.ConnectError as e:
        err = "ConnErr"
    except Exception as e:
        err = type(e).__name__

    rt = time.perf_counter() - t0
    second_bucket[sec]["total"] += 1
    if ok:
        second_bucket[sec]["ok"] += 1
    else:
        second_bucket[sec]["bad"] += 1
        second_bucket[sec]["errs"][err] = second_bucket[sec]["errs"].get(err, 0) + 1


async def worker(client, second_bucket, i):
    k = 0
    while time.time() - START < DURATION:
        await one_request(client, second_bucket, i * 1000 + k)
        k += 1


async def kill_target(container):
    await asyncio.sleep(KILL_AT)
    sec = int(time.time() - START)
    print(f"[t={sec}s] >>> docker kill {container}", flush=True)
    subprocess.run(["docker", "kill", container], capture_output=True)


async def main():
    global START, MODEL
    target_key = sys.argv[1] if len(sys.argv) > 1 else "letta"
    container = TARGET_CONTAINER.get(target_key, target_key)
    if len(sys.argv) > 2:
        MODEL = sys.argv[2]
    print(f"model: {MODEL}")
    print(f"target: {container}  C={CONCURRENCY}  kill_at={KILL_AT}s  duration={DURATION}s")
    print(f"will kill container {container} at t={KILL_AT}s, observe until t={DURATION}s\n")

    second_bucket = defaultdict(lambda: {"total": 0, "ok": 0, "bad": 0, "errs": {}})
    START = time.time()

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=CONCURRENCY + 5)) as client:
        tasks = [asyncio.create_task(worker(client, second_bucket, i)) for i in range(CONCURRENCY)]
        kill_task = asyncio.create_task(kill_target(container))
        await asyncio.gather(*tasks)
        await kill_task

    # 聚合 10 秒为一个桶
    print("\n========== Result ==========")
    print("sec  | total ok  bad | ok%   | errors")
    print("-----|---------------|-------|--------")
    for s in sorted(second_bucket.keys()):
        b = second_bucket[s]
        pct = 100 * b["ok"] / b["total"] if b["total"] else 0
        err_str = ", ".join(f"{k}:{v}" for k, v in b["errs"].items()) if b["errs"] else "-"
        marker = "  <<< KILL" if s == KILL_AT else ""
        print(f"{s:4d} | {b['total']:4d}  {b['ok']:3d} {b['bad']:3d}  | {pct:3.0f}%  | {err_str}{marker}")

    # 关键指标：kill 后首次恢复到 >95% 成功的时间
    post_kill = [(s, b) for s, b in sorted(second_bucket.items()) if s >= KILL_AT]
    first_recovery = None
    stable_recovery = None
    win = []
    for s, b in post_kill:
        if b["total"] == 0: continue
        if b["ok"] > 0 and first_recovery is None:
            first_recovery = s - KILL_AT
        win.append((s, b["ok"] / b["total"]))
        if len(win) > 5: win.pop(0)
        if len(win) >= 3 and all(r >= 0.95 for _, r in win[-3:]) and stable_recovery is None:
            stable_recovery = win[-3][0] - KILL_AT

    print(f"\n>> 首个成功请求: {first_recovery}s 后" if first_recovery is not None else "\n>> 未观察到恢复")
    print(f">> 稳态恢复 (连续 3 秒 >95% 成功): {stable_recovery}s 后" if stable_recovery is not None else ">> 未达稳态恢复")


if __name__ == "__main__":
    asyncio.run(main())
