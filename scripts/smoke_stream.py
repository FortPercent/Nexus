"""Smoke test：打 /v1/chat/completions stream=true，验证真流式行为。

覆盖两类模型：
  - qwen-no-mem       vLLM 直连（原本就是真流式，回归检查）
  - letta-<project>   Letta 记忆链路（今天改成真流式）

环境变量：
  ADAPTER_URL   默认 http://localhost:8000
  API_KEY       默认 teleai-adapter-key-2026
  USER_ID       letta 模型必填（Open WebUI 里的用户 id）
  USER_EMAIL    letta 模型必填
  USER_NAME     letta 模型可选
  MODEL         默认两个都跑；指定则只跑一个
  TIMEOUT       单请求超时，默认 120s
  TTFT_LIMIT    首 token 最大延迟秒数，默认 15s（Letta 的首 token 包含 archival search，放宽点）

用法：
  python scripts/smoke_stream.py
  MODEL=letta-teleai USER_ID=xxx USER_EMAIL=xxx@... python scripts/smoke_stream.py
"""
import json
import os
import sys
import time
from typing import Optional

import httpx

ADAPTER_URL = os.getenv("ADAPTER_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("API_KEY", "teleai-adapter-key-2026")
USER_ID = os.getenv("USER_ID", "")
USER_EMAIL = os.getenv("USER_EMAIL", "")
USER_NAME = os.getenv("USER_NAME", "smoke-test")
ONLY_MODEL = os.getenv("MODEL", "")
TIMEOUT = float(os.getenv("TIMEOUT", "120"))
TTFT_LIMIT = float(os.getenv("TTFT_LIMIT", "15"))

PROMPT = "用一句话介绍 TeleAI Nexus 是什么，要求中文回答。"


class SmokeError(Exception):
    pass


def run_stream(model: str) -> dict:
    """打一次流式请求，返回指标 dict。抛 SmokeError 表示失败。"""
    body = {
        "model": model,
        "stream": True,
        "messages": [{"role": "user", "content": PROMPT}],
    }
    if model.startswith("letta-"):
        if not USER_ID or not USER_EMAIL:
            raise SmokeError(f"{model} 需要 USER_ID + USER_EMAIL 环境变量")
        body["user_id"] = USER_ID
        body["user_email"] = USER_EMAIL
        body["user_name"] = USER_NAME

    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    start = time.monotonic()
    ttft: Optional[float] = None
    content_parts: list[str] = []
    got_done = False
    chunks = 0
    stop_seen = False

    with httpx.Client(timeout=TIMEOUT) as client:
        with client.stream("POST", f"{ADAPTER_URL}/v1/chat/completions",
                           json=body, headers=headers) as resp:
            if resp.status_code != 200:
                raise SmokeError(f"HTTP {resp.status_code}: {resp.read().decode(errors='replace')[:300]}")
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    got_done = True
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                chunks += 1
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    if ttft is None:
                        ttft = time.monotonic() - start
                    content_parts.append(text)
                if choice.get("finish_reason") == "stop":
                    stop_seen = True

    total = time.monotonic() - start
    content = "".join(content_parts)
    return {
        "model": model,
        "ttft": ttft,
        "total": total,
        "chunks": chunks,
        "len": len(content),
        "content": content,
        "got_done": got_done,
        "stop_seen": stop_seen,
    }


def check(result: dict) -> list[str]:
    """对返回做断言，返回失败原因列表（空列表 = 通过）。"""
    errs = []
    model = result["model"]
    if result["ttft"] is None:
        errs.append("没收到任何 content chunk")
    elif result["ttft"] > TTFT_LIMIT:
        errs.append(f"首 token {result['ttft']:.2f}s 超过阈值 {TTFT_LIMIT}s（可能回到伪流式）")
    if not result["got_done"]:
        errs.append("没收到 [DONE]")
    if not result["stop_seen"]:
        errs.append("没收到 finish_reason=stop")
    if result["len"] < 5:
        errs.append(f"回复过短（{result['len']} 字符）")

    # <think> 平衡：开合数量相等
    content = result["content"]
    open_n = content.count("<think>")
    close_n = content.count("</think>")
    if open_n != close_n:
        errs.append(f"<think> 未闭合：open={open_n} close={close_n}")

    # 真流式检查：如果 chunks == 1，明显是一次性吐完（伪流式的嫌疑）
    if result["chunks"] < 3 and result["len"] > 20:
        errs.append(f"chunk 数 {result['chunks']} 过少（len={result['len']}），疑似一次性返回")

    return errs


def main():
    models = []
    if ONLY_MODEL:
        models = [ONLY_MODEL]
    else:
        models = ["qwen-no-mem"]
        if USER_ID and USER_EMAIL:
            # 默认尝试 letta-default，不存在就跳过
            models.append("letta-default")

    print(f"adapter: {ADAPTER_URL}")
    print(f"models : {models}\n")

    failed = 0
    for m in models:
        print(f"===== {m} =====")
        try:
            r = run_stream(m)
        except SmokeError as e:
            print(f"  FAIL  {e}\n")
            failed += 1
            continue
        except Exception as e:
            print(f"  FAIL  exception: {type(e).__name__}: {e}\n")
            failed += 1
            continue

        errs = check(r)
        status = "PASS" if not errs else "FAIL"
        print(f"  {status}")
        print(f"  ttft      : {r['ttft']:.2f}s" if r["ttft"] else "  ttft      : -")
        print(f"  total     : {r['total']:.2f}s")
        print(f"  chunks    : {r['chunks']}")
        print(f"  resp len  : {r['len']}")
        print(f"  preview   : {r['content'][:80]!r}")
        for e in errs:
            print(f"  !! {e}")
        print()
        if errs:
            failed += 1

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
