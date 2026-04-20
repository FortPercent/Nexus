#!/usr/bin/env python3
"""PoC: 对 kb-poc test agent 发 3 条 security 规范类问题, 打印工具调用 + 回答.

成功判据 (4 条):
  ① agent 首轮是否主动调 list_project_files
  ② 是否随后调 read_project_file 且选对文件
  ③ 回答是否命中条款
  ④ 是否跨 scope (应全 project, 不碰 personal/org)
"""
import argparse
import sys

sys.path.insert(0, "/app")

from routing import letta


QUESTIONS = [
    "DLP 安装卸载有什么要求？",
    "对外交付时现场设备要注意什么？",
    "安全开发规范里对密码应用说了什么？",
]


def _extract_text(msg) -> str:
    """尽力从 Letta message 对象抽出可见文本."""
    c = getattr(msg, "content", None)
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            t = getattr(b, "text", None) or getattr(b, "content", None)
            if isinstance(t, str):
                parts.append(t)
        return "\n".join(parts)
    return ""


def _print_message(m):
    mtype = getattr(m, "message_type", None) or str(type(m).__name__)
    if mtype in ("tool_call_message", "ToolCallMessage"):
        tc = getattr(m, "tool_call", None)
        name = getattr(tc, "name", "") if tc else ""
        args = getattr(tc, "arguments", "") if tc else ""
        print(f"  🔧 [tool_call] {name}({args[:200]})")
    elif mtype in ("tool_return_message", "ToolReturnMessage"):
        ret = getattr(m, "tool_return", "") or getattr(m, "content", "") or ""
        status = getattr(m, "status", "")
        preview = str(ret)[:300].replace("\n", " ")
        print(f"  ↩️  [tool_return] status={status} | {preview}...")
    elif mtype in ("reasoning_message", "ReasoningMessage"):
        r = getattr(m, "reasoning", "") or ""
        if r.strip():
            print(f"  💭 [reasoning] {r[:200]}")
    elif mtype in ("assistant_message", "AssistantMessage"):
        t = _extract_text(m)
        if t.strip():
            print(f"\n  🤖 Answer:\n{t}")
    else:
        t = _extract_text(m)
        if t.strip():
            print(f"  [{mtype}] {t[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", required=True)
    args = ap.parse_args()

    print(f"Agent: {args.agent_id}")
    print(f"Questions: {len(QUESTIONS)}\n")

    for i, q in enumerate(QUESTIONS, 1):
        print(f"{'='*80}")
        print(f"Q{i}: {q}")
        print(f"{'='*80}")
        try:
            resp = letta.agents.messages.create(
                agent_id=args.agent_id,
                messages=[{"role": "user", "content": q}],
            )
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            continue

        msgs = getattr(resp, "messages", [])
        print(f"  response has {len(msgs)} message(s)\n")
        for m in msgs:
            _print_message(m)
        print()


if __name__ == "__main__":
    main()
