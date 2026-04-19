#!/usr/bin/env python3
"""长对话 agent TTFT: 比对高 msg_count vs 低 msg_count 的 agent。"""
from routing import letta_async
import asyncio, time, statistics

AGENTS = {
    "477 msgs (wuxn5/ai-infra)": "agent-86577deb-eea1-453a-8baf-ea4e51ad31af",
    "86 msgs (biany4/资产管理)": "agent-1bd1fdba-0417-446b-8a9a-dd3be0a6a0d0",
    "80 msgs (jinyx5/cache)": "agent-750036e8-2fba-4496-bc04-ee99e20bd84f",
    "18 msgs (liuyr17/ai-infra)": "agent-aa7f189f-3d24-4ed9-bf33-9d3652c3da13",
}

async def measure(name, agent_id, runs=3):
    ttfts = []
    totals = []
    for i in range(runs):
        t0 = time.perf_counter()
        ttft = 0.0
        try:
            # streaming create
            stream = await letta_async.agents.messages.stream(
                agent_id=agent_id,
                messages=[{"role":"user", "content":"你好"}],
                stream_tokens=True,
            )
            async for event in stream:
                etype = getattr(event, "message_type", None) or getattr(event, "event", None)
                if etype in ("assistant_message", "reasoning_message"):
                    # first real token
                    if ttft == 0.0:
                        txt = getattr(event, "content", None) or getattr(event, "reasoning", None) or ""
                        if txt:
                            ttft = time.perf_counter() - t0
            total = time.perf_counter() - t0
            if ttft > 0:
                ttfts.append(ttft)
                totals.append(total)
            print(f"  [{i+1}] TTFT={ttft*1000:.0f}ms total={total*1000:.0f}ms")
        except Exception as e:
            print(f"  [{i+1}] ERR: {type(e).__name__}: {str(e)[:100]}")
        await asyncio.sleep(2)
    if ttfts:
        print(f"  => median TTFT={statistics.median(ttfts)*1000:.0f}ms  median total={statistics.median(totals)*1000:.0f}ms")
    return ttfts, totals

async def main():
    for name, agent_id in AGENTS.items():
        print(f"\n--- {name} ---")
        await measure(name, agent_id)

asyncio.run(main())
