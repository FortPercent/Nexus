#!/usr/bin/env python3
"""L2 M5 对比压测：grep 路径 vs SQL 路径。

场景：对资产管理 agent 问"机器人总数 + 各团队持有量"，分两次：
  (A) detach 三 SQL 工具 → agent 只能用 grep / semantic_search
  (B) 重新 attach → agent 按 persona 走 SQL 路径

度量：events / tool_calls / prompt_tokens / stop_reason / 延迟 / 文本长度

测完自动恢复 attach 状态（让资产 agent 继续可用）。
需要 project DuckDB 里有数据（先跑 M3 E2E 测试或用户重传 xlsx）。

用法（容器里）：
    docker exec -w /app teleai-adapter python scripts/bench_structured_query.py
"""
import asyncio
import sys
import time

sys.path.insert(0, "/app")

from routing import letta, letta_async
from letta_sql_tools import get_sql_tool_ids

AGENT_ID = "agent-1bd1fdba-0417-446b-8a9a-dd3be0a6a0d0"
QUERY = "帮我统计一下固定资产清单里机器人类的设备一共多少台，各团队分别持有多少"


async def run(label):
    t0 = time.perf_counter()
    events = 0
    tool_calls = []
    tool_return_lens = []
    prompt_tokens = None
    stop_reason = None
    final = []
    try:
        stream = await letta_async.agents.messages.stream(
            agent_id=AGENT_ID,
            messages=[{"role": "user", "content": QUERY}],
            stream_tokens=True,
            include_pings=False,
        )
        async for ev in stream:
            events += 1
            mt = getattr(ev, "message_type", None) or type(ev).__name__
            if mt == "tool_call_message":
                tc = getattr(ev, "tool_call", None)
                if tc:
                    n = getattr(tc, "name", "") or ""
                    if n:
                        tool_calls.append(n)
            elif mt == "tool_return_message":
                ret = getattr(ev, "tool_return", "") or ""
                tool_return_lens.append(len(ret))
            elif mt == "assistant_message":
                c = getattr(ev, "content", "") or ""
                if isinstance(c, list):
                    c = "".join(getattr(p, "text", "") for p in c if hasattr(p, "text"))
                final.append(str(c))
            elif mt == "stop_reason":
                stop_reason = getattr(ev, "stop_reason", None)
            elif mt == "usage_statistics":
                prompt_tokens = getattr(ev, "prompt_tokens", None)
    except Exception as e:
        stop_reason = f"exception: {type(e).__name__}"
    elapsed = time.perf_counter() - t0
    final_text = "".join(final)
    return {
        "label": label,
        "elapsed_s": round(elapsed, 2),
        "events": events,
        "tool_calls": tool_calls,
        "tool_return_total_chars": sum(tool_return_lens),
        "prompt_tokens": prompt_tokens,
        "stop_reason": stop_reason,
        "final_chars": len(final_text),
        "final_preview": final_text[:150].replace("\n", " "),
    }


def detach_sql(tool_ids):
    for tid in tool_ids:
        try:
            letta.agents.tools.detach(agent_id=AGENT_ID, tool_id=tid)
        except Exception:
            pass


def attach_sql(tool_ids):
    for tid in tool_ids:
        try:
            letta.agents.tools.attach(agent_id=AGENT_ID, tool_id=tid)
        except Exception:
            pass


def reset_messages():
    """每次跑前 reset, 避免上轮 state 污染。"""
    try:
        letta.agents.messages.reset(agent_id=AGENT_ID, add_default_initial_messages=True)
    except Exception as e:
        print(f"⚠️ reset messages failed: {e}")


def print_row(r):
    print(f"  label         : {r['label']}")
    print(f"  elapsed       : {r['elapsed_s']}s")
    print(f"  events        : {r['events']}")
    print(f"  tool_calls    : {r['tool_calls']}")
    print(f"  tool_ret_chars: {r['tool_return_total_chars']}")
    print(f"  prompt_tokens : {r['prompt_tokens']}")
    print(f"  stop_reason   : {r['stop_reason']}")
    print(f"  final_chars   : {r['final_chars']}")
    print(f"  preview       : {r['final_preview']!r}")


async def ensure_mock_data():
    """bench 自带 mock 数据：947 行假资产，10 类设备，两团队。
    若 DuckDB 已有表（用户真实上传），保留不覆盖。跑完也不删（方便复用）。"""
    from table_ingest import ingest_if_structured, _duckdb_path
    import duckdb, io
    import pandas as pd

    PID = "资产管理小助手"
    dp = _duckdb_path(PID)
    if dp.exists():
        try:
            con = duckdb.connect(str(dp), read_only=True)
            try:
                has = con.execute("SELECT COUNT(*) FROM __nexus_meta").fetchone()[0]
            finally:
                con.close()
            if has > 0:
                print(f"[bench] 检测到已有 {has} 张表，直接复用（不覆盖用户数据）")
                return
        except Exception:
            pass

    print("[bench] 生成 947 行 mock 资产数据")
    names = ["小笨智能机器人", "轮足机器人A", "人形机器人X1", "服务器A100", "服务器H100",
             "笔记本ThinkPad", "笔记本MacBook", "打印机", "投影仪", "会议桌"]
    rows = [{
        "资产编号": f"{i+1:012d}",
        "资产名称": names[i % 10],
        "数量": 1,
        "保管团队": "AI Infra" if i % 2 == 0 else "Ops",
        "原值(元)": 1000 + (i * 17) % 100000,
    } for i in range(947)]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame(rows).to_excel(w, sheet_name="Sheet1", index=False)
    await ingest_if_structured(PID, "file-bench-mock", "A668_bench_mock.xlsx", buf.getvalue())
    print("[bench] mock 数据已 ingest")


async def main():
    await ensure_mock_data()
    tool_ids = get_sql_tool_ids()

    print("=" * 70)
    print("A) grep-only 路径（SQL 工具被 detach）")
    print("=" * 70)
    detach_sql(tool_ids)
    reset_messages()
    grep_result = await run("grep-only")
    print_row(grep_result)

    print()
    print("=" * 70)
    print("B) SQL 路径（SQL 工具已挂）")
    print("=" * 70)
    attach_sql(tool_ids)
    reset_messages()
    sql_result = await run("sql-tools")
    print_row(sql_result)

    print()
    print("=" * 70)
    print("对比结论")
    print("=" * 70)
    fields = [
        ("prompt_tokens", "prompt 总 tokens"),
        ("tool_return_total_chars", "工具返回总字符"),
        ("elapsed_s", "端到端延迟 (s)"),
        ("final_chars", "最终回答字符"),
    ]
    for key, label in fields:
        a, b = grep_result.get(key), sql_result.get(key)
        print(f"  {label:<25} grep={a}  sql={b}")
    print(f"  {'stop_reason':<25} grep={grep_result['stop_reason']}  sql={sql_result['stop_reason']}")
    print(f"  {'tool_calls':<25} grep={grep_result['tool_calls']}")
    print(f"  {'':<25} sql ={sql_result['tool_calls']}")


asyncio.run(main())
