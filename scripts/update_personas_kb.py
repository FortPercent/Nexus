#!/usr/bin/env python3
"""Phase 1 收尾: 批量更新 29 生产 agent 的 persona block.

老 persona 引用 `grep_files / semantic_search_files / open_files`, Phase 1 detach folder 后
这些工具下线, agent 会尝试调用 → Letta ToolConstraintError. 需要把 persona 换成新的 kb 工具名.

做法:
  1. 读 user_agent_map 所有 agent
  2. 对每个 agent, 查其挂的 persona block (label='persona')
  3. 把 block.value 替换成 PERSONA_TEXT (routing.py 最新版, 已用 kb 工具名)
  4. 跳过 test-kb-poc-* agent

用法:
    docker exec teleai-adapter python3 /app/scripts/update_personas_kb.py [--dry-run]
"""
import argparse
import sqlite3
import sys

sys.path.insert(0, "/app")

from routing import letta, PERSONA_TEXT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    c = sqlite3.connect("/data/serving/adapter/adapter.db")
    rows = c.execute("SELECT project_id, user_id, agent_id FROM user_agent_map").fetchall()
    c.close()

    stats = {"updated": 0, "skipped_already": 0, "no_persona_block": 0, "error": 0}

    for pid, uid, aid in rows:
        try:
            agent = letta.agents.retrieve(agent_id=aid)
            # retrieve block by label=persona
            blocks_page = letta.agents.blocks.list(agent_id=aid)
            blocks = getattr(blocks_page, "items", blocks_page) if hasattr(blocks_page, "items") or not isinstance(blocks_page, list) else blocks_page
            persona_block = None
            for b in blocks:
                if getattr(b, "label", "") == "persona":
                    persona_block = b
                    break
            if not persona_block:
                stats["no_persona_block"] += 1
                print(f"  [no persona] {pid:30s} {aid[-12:]}")
                continue

            current = persona_block.value or ""
            if current == PERSONA_TEXT:
                stats["skipped_already"] += 1
                print(f"  [same]       {pid:30s} {aid[-12:]}")
                continue

            if args.dry_run:
                print(f"  [would_upd]  {pid:30s} {aid[-12:]}  old {len(current)} chars → new {len(PERSONA_TEXT)} chars")
                continue

            letta.blocks.modify(block_id=persona_block.id, value=PERSONA_TEXT)
            stats["updated"] += 1
            print(f"  [updated]    {pid:30s} {aid[-12:]}  {len(current)} → {len(PERSONA_TEXT)} chars")
        except Exception as e:
            stats["error"] += 1
            print(f"  ERROR {pid} {aid[-12:]}: {type(e).__name__}: {str(e)[:150]}")

    print(f"\nTotal: {stats}")


if __name__ == "__main__":
    main()
