#!/usr/bin/env python3
"""Phase 1 收尾补: detach 老 file tools (grep_files / open_files / semantic_search_files).

问题: rollout_all.py 只 attach 了 kb 新工具, 没 detach Letta 自带的老 file tools.
生产 agent 现在同时挂着新老两套, agent 会先试老工具 → ToolConstraintError
(因为 folder 已 detach, 老工具依赖 folder). 得干净去掉.

注意: ai-infra-cache canary swap 时观察到这 3 个消失, 可能是 Letta 自己顺带 detach. 但 security
等 agent 上并没消失, 行为不一致. 统一批量清理保险.

用法:
    docker exec teleai-adapter python3 /app/scripts/detach_legacy_file_tools.py [--dry-run]
"""
import argparse
import sqlite3
import sys

sys.path.insert(0, "/app")

from routing import letta

LEGACY_FILE_TOOLS = {"grep_files", "open_files", "semantic_search_files"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    c = sqlite3.connect("/data/serving/adapter/adapter.db")
    rows = c.execute("SELECT project_id, user_id, agent_id FROM user_agent_map").fetchall()
    c.close()

    stats = {"detached_total": 0, "agents_touched": 0, "error": 0, "already_clean": 0}

    for pid, uid, aid in rows:
        try:
            agent = letta.agents.retrieve(agent_id=aid, include=["agent.tools"])
            legacy_on_agent = [t for t in (agent.tools or []) if t.name in LEGACY_FILE_TOOLS]
            if not legacy_on_agent:
                stats["already_clean"] += 1
                continue

            if args.dry_run:
                print(f"  [would_detach] {pid:30s} {aid[-12:]}  {[t.name for t in legacy_on_agent]}")
                continue

            for t in legacy_on_agent:
                try:
                    letta.agents.tools.detach(agent_id=aid, tool_id=t.id)
                    stats["detached_total"] += 1
                except Exception as e:
                    print(f"  ! {aid[-12:]} detach {t.name}: {e}")
            stats["agents_touched"] += 1
            print(f"  [cleaned]      {pid:30s} {aid[-12:]}  -{len(legacy_on_agent)}")
        except Exception as e:
            stats["error"] += 1
            print(f"  ERROR {pid} {aid[-12:]}: {e}")

    print(f"\nTotal: {stats}")


if __name__ == "__main__":
    main()
