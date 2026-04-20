#!/usr/bin/env python3
"""PoC: 建一个隔离的 kb-poc test agent.

明确约束:
  - 不挂任何 folder (Letta 默认 folder 是炸 compact 的元凶, PoC 要证 agent 没 folder 也能回答)
  - 不写 user_agent_map (生产入口看不到这个 test agent)
  - 挂 owner 的 human block + 一个专用 persona + 2 个 kb 工具
  - tool_ids 传入 + 显式 agents.tools.attach() (Letta bug workaround, 见 routing.py:233)

用法:
    docker exec teleai-adapter python3 /app/scripts/create_test_agent.py \\
        --owner f1dfb0ed-0c2b-4337-922a-cbc86859dfde \\
        --project security-management

输出: agent id (可用于后续 scripts/poc_ask.py 发问).
清理: letta.agents.delete(agent_id=...)  — 不会污染 user_agent_map.
"""
import argparse
import sys
import time

sys.path.insert(0, "/app")

from config import VLLM_ENDPOINT
from routing import letta, get_or_create_personal_human_block
from kb.letta_tools import get_kb_tool_ids


TEST_PERSONA = """你是 TeleAI Nexus 知识层重构 PoC 的测试 Agent（kb-poc v0.6）。

【工作模式 — 严格遵守】
用户但凡问到项目文档 / 规范 / 条款 / 流程 / 要求相关的问题，你必须按这个流程来：
1. **先**调 list_project_files 看当前 project 有哪些文件
2. 从列表里挑**最相关**的 1-2 份文件名
3. 调 read_project_file 读那份文件的内容
4. 基于原文回答，**引用文件名**做出处

【严格禁止】
- 不要凭记忆直接回答文档类问题；必须先 list → read
- 不要跨 scope（工具只支持 project；personal/org 不在本 PoC 范围）
- 不要暴露内部 id（agent/tool/block id）

【回答风格】
简短直接，引用原文即可。不堆废话。
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True, help="user_uuid (不会写进 user_agent_map)")
    ap.add_argument("--project", required=True, help="project slug, e.g. security-management")
    args = ap.parse_args()

    ts = int(time.time())
    agent_name = f"test-kb-poc-{ts}"

    print(f"[1/4] 获取 kb 工具 id ...")
    tool_ids = get_kb_tool_ids()
    print(f"      list + read tool ids: {[t[-16:] for t in tool_ids]}")

    print(f"[2/4] 获取/建 owner human block (user={args.owner[:8]}) ...")
    human_block_id = get_or_create_personal_human_block(args.owner)
    print(f"      block: {human_block_id[-16:]}")

    print(f"[3/4] 创建 agent '{agent_name}' (不挂 folder, 不写 user_agent_map) ...")
    agent = letta.agents.create(
        name=agent_name,
        metadata={
            "owner": args.owner,
            "project": args.project,
            "_test": "kb-poc-v0",
            "_created_at": str(ts),
        },
        tool_ids=tool_ids,
        block_ids=[human_block_id],
        memory_blocks=[{"label": "persona", "value": TEST_PERSONA}],
        llm_config={
            "model": "Qwen3.5-122B-A10B",
            "model_endpoint_type": "openai",
            "model_endpoint": VLLM_ENDPOINT,
            "context_window": 60000,
            "enable_reasoner": True,
        },
        embedding_config={
            "embedding_model": "nomic-embed-text",
            "embedding_endpoint_type": "openai",
            "embedding_endpoint": "http://ollama:11434/v1",
            "embedding_dim": 768,
            "embedding_chunk_size": 300,
            "batch_size": 32,
        },
    )
    print(f"      agent: {agent.id}")

    print(f"[4/4] 显式 attach 2 个工具 (Letta tool_ids=创建时不入 prompt 的 bug 修正) ...")
    for tid in tool_ids:
        try:
            letta.agents.tools.attach(agent_id=agent.id, tool_id=tid)
            print(f"      + attached {tid[-16:]}")
        except Exception as e:
            msg = str(e).lower()
            if "conflict" in msg or "already" in msg or "409" in msg:
                print(f"      = {tid[-16:]} (already attached)")
            else:
                print(f"      ! {tid[-16:]}: {e}")

    print()
    print(f"========== PoC test agent ready ==========")
    print(f"  id:      {agent.id}")
    print(f"  name:    {agent_name}")
    print(f"  owner:   {args.owner}")
    print(f"  project: {args.project}")
    print(f"  tools:   list_project_files + read_project_file")
    print(f"  folder:  (none)")
    print(f"  user_agent_map: (not written — 生产入口看不到)")
    print()
    print(f"下一步: docker exec teleai-adapter python3 /app/scripts/poc_ask.py --agent-id {agent.id}")
    print(f"清理:   docker exec teleai-adapter python3 -c \\")
    print(f"        \"from routing import letta; letta.agents.delete(agent_id='{agent.id}')\"")


if __name__ == "__main__":
    main()
