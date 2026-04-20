#!/usr/bin/env python3
"""Phase 1 收尾: 删除所有 test-kb-poc-* agent (不在 user_agent_map, 只在 Letta 里孤儿).

判据: metadata._test == 'kb-poc-v0' 或 name startswith 'test-kb-poc' 或 'probe-'

用法:
    docker exec teleai-adapter python3 /app/scripts/cleanup_poc_agents.py [--dry-run]
"""
import argparse
import sys

sys.path.insert(0, "/app")

from routing import letta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # Letta agents.list 没 filter 参数, 先拉全部
    all_agents = []
    after = None
    while True:
        page = letta.agents.list(after=after, limit=100) if after else letta.agents.list(limit=100)
        items = list(page) if hasattr(page, "__iter__") else getattr(page, "items", [])
        if not items:
            break
        all_agents.extend(items)
        if len(items) < 100:
            break
        after = items[-1].id

    print(f"total agents in Letta: {len(all_agents)}")

    to_delete = []
    for a in all_agents:
        md = getattr(a, "metadata", None) or {}
        name = getattr(a, "name", "") or ""
        if (md.get("_test") == "kb-poc-v0"
            or name.startswith("test-kb-poc")
            or name.startswith("probe-")):
            to_delete.append((a.id, name))

    print(f"\nwill delete {len(to_delete)}:")
    for aid, name in to_delete:
        print(f"  {name:40s} {aid}")

    if args.dry_run or not to_delete:
        return

    for aid, name in to_delete:
        try:
            letta.agents.delete(agent_id=aid)
            print(f"  deleted {name}")
        except Exception as e:
            print(f"  ERROR {name}: {e}")


if __name__ == "__main__":
    main()
