#!/usr/bin/env python3
"""安全 rebuild 指定 agent: detach blocks → delete agent → clean user_agent_map.

适用场景:
  - Letta summarizer bug 导致 messages 累积撞 65K vLLM 上限
  - 用户手动要求重置对话历史
  - admin 排查某 agent 异常状态

关键: 先 detach 共享 blocks (human/org/project), 否则 letta.agents.delete 会 cascade 删,
      祸及其他使用这些 block 的 agent.

下次用户打开同 project chat → adapter get_or_create_agent 自动走 Phase 1 新路径
  (不挂 folder, 挂 kb 3 工具, 挂 block via _attach_agent_blocks_only) 新建 agent.

用法:
    docker exec teleai-adapter python3 /app/scripts/rebuild_agent.py \\
        --user-id <uuid> --project <slug> [--dry-run]
"""
import argparse
import sqlite3
import sys

sys.path.insert(0, "/app")

from routing import letta
from config import DB_PATH


def rebuild(user_id: str, project: str, dry_run: bool) -> dict:
    c = sqlite3.connect(DB_PATH)
    row = c.execute(
        "SELECT agent_id FROM user_agent_map WHERE user_id = ? AND project_id = ?",
        (user_id, project),
    ).fetchone()
    c.close()
    if not row:
        return {"status": "no_agent_found"}
    aid = row[0]
    print("agent: " + aid)

    # 1. list attached blocks
    try:
        bp = letta.agents.blocks.list(agent_id=aid)
        blocks = list(getattr(bp, "items", bp) if hasattr(bp, "items") else bp)
    except Exception as e:
        print("  list blocks err: " + str(e))
        blocks = []
    print("  attached blocks: " + str(len(blocks)))

    if dry_run:
        for b in blocks:
            print("    [would detach] " + str(getattr(b, "label", "?")) + " " + str(getattr(b, "id", "?"))[-16:])
        print("  [would delete] agent " + aid)
        print("  [would delete] user_agent_map row")
        return {"status": "dry_run", "blocks": len(blocks)}

    # 2. detach each block (防 cascade)
    for b in blocks:
        bid = getattr(b, "id", None)
        if not bid:
            continue
        try:
            letta.agents.blocks.detach(agent_id=aid, block_id=bid)
            print("  detached [" + str(getattr(b, "label", "?")) + "] " + bid[-16:])
        except Exception as e:
            print("  ! detach " + bid[-16:] + ": " + str(e))

    # 3. delete agent
    try:
        letta.agents.delete(agent_id=aid)
        print("  deleted " + aid)
    except Exception as e:
        return {"status": "delete_failed", "error": str(e)}

    # 4. delete user_agent_map row
    c = sqlite3.connect(DB_PATH)
    n = c.execute(
        "DELETE FROM user_agent_map WHERE user_id = ? AND project_id = ?",
        (user_id, project),
    ).rowcount
    c.commit()
    c.close()
    print("  user_agent_map: removed " + str(n) + " row(s)")

    return {"status": "rebuilt", "old_agent_id": aid, "blocks_detached": len(blocks)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    r = rebuild(args.user_id, args.project, args.dry_run)
    print("\nresult: " + str(r))


if __name__ == "__main__":
    main()
