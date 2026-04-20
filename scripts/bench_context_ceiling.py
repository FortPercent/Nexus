#!/usr/bin/env python3
"""Context window ceiling 压测：复现 04-20 资产管理小助手 "silent stop" bug。

目的：
1. 用触发长工具返回的 query 复现 LLMBadRequestError
2. 打印 *全部* 流事件类型（含被 adapter 默认忽略的 stop_reason / usage_statistics）
3. 确认 Letta 是否通过事件暴露了 error / stop_reason，还是只能从日志里看到

Run: docker exec teleai-adapter python scripts/bench_context_ceiling.py
"""
from routing import letta_async
import asyncio, time, json

AGENT_ID = "agent-1bd1fdba-0417-446b-8a9a-dd3be0a6a0d0"  # 资产管理小助手

TRIGGER_QUERIES = [
    "根据知识库的内容，目前研究院有多少件固定资产，有多少台机器人",  # 04-20 崩掉的原 query
    "请搜索所有包含'机器人'的条目并列出详情",                          # 变体 1: 长 grep 结果
    "你好",                                                             # 控制组: 不触发工具
]

async def run(query):
    print(f"\n{'='*70}\nQuery: {query}\n{'='*70}")
    t0 = time.perf_counter()
    event_counts = {}
    tool_returns = []
    stop_reasons = []
    errors = []
    usage = None
    final_assistant = ""

    try:
        stream = await letta_async.agents.messages.stream(
            agent_id=AGENT_ID,
            messages=[{"role": "user", "content": query}],
            stream_tokens=True,
            include_pings=False,
        )
        async for ev in stream:
            mtype = getattr(ev, "message_type", None) or type(ev).__name__
            event_counts[mtype] = event_counts.get(mtype, 0) + 1

            if mtype == "tool_return_message":
                ret = getattr(ev, "tool_return", "") or ""
                tool_returns.append(len(ret))
            elif mtype == "assistant_message":
                c = getattr(ev, "content", "") or ""
                if isinstance(c, list):
                    c = "".join(getattr(p, "text", "") for p in c if hasattr(p, "text"))
                final_assistant += str(c)
            elif mtype == "stop_reason" or hasattr(ev, "stop_reason"):
                sr = getattr(ev, "stop_reason", None)
                if sr:
                    stop_reasons.append(str(sr))
            elif mtype in ("error_message", "LettaMessageUnion"):
                errors.append({
                    "type": getattr(ev, "error_type", "") or "",
                    "msg": (getattr(ev, "message", "") or "")[:200],
                })
            elif mtype == "usage_statistics":
                usage = {
                    "prompt_tokens": getattr(ev, "prompt_tokens", None),
                    "completion_tokens": getattr(ev, "completion_tokens", None),
                    "total_tokens": getattr(ev, "total_tokens", None),
                    "step_count": getattr(ev, "step_count", None),
                }
    except Exception as e:
        print(f"EXCEPTION in stream: {type(e).__name__}: {str(e)[:300]}")

    elapsed = time.perf_counter() - t0
    print(f"\n[stream closed in {elapsed:.2f}s]")
    print(f"event_counts: {json.dumps(event_counts, ensure_ascii=False)}")
    print(f"tool_returns char lens: {tool_returns}  (total {sum(tool_returns)} chars)")
    print(f"stop_reasons: {stop_reasons}")
    print(f"errors from stream: {errors}")
    print(f"usage: {usage}")
    print(f"final assistant_message: {repr(final_assistant[:300])}{' ...(truncated)' if len(final_assistant)>300 else ''}")
    print(f"assistant msg total chars: {len(final_assistant)}")


async def main():
    for q in TRIGGER_QUERIES:
        await run(q)
        await asyncio.sleep(3)  # 给 vLLM 喘息

asyncio.run(main())
