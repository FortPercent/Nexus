#!/usr/bin/env python3
"""Phase 1 canary: 挑 1 个生产 agent, detach folder + attach kb 工具.

流程:
  1. retrieve agent + list folders + list tools → 打印 before 状态
  2. 把 before 状态存 /tmp/canary_backup_<agent_id>.json (rollback 用)
  3. detach 所有 folder (通常 3 个: org / project / personal)
  4. attach kb 3 工具 (list / read / grep) + 显式 attach 循环 (Letta bug)
  5. 再次 retrieve → 打印 after 状态
  6. 输出 agent id 供手工 chat 测

不改 persona (第一次不改, 看 agent 能否靠 tool description 自动切换)
不删 folder (只 detach, folder 本身留在 Letta, 支持 rollback)

用法:
    docker exec teleai-adapter python3 /app/scripts/canary_swap.py \\
        --agent-id agent-xxx \\
        --project ai-infra-cache \\
        [--rollback]

Rollback:
    python3 /app/scripts/canary_swap.py --agent-id agent-xxx --project ai-infra-cache --rollback
"""
import argparse
import json
import os
import sys

sys.path.insert(0, "/app")

from routing import letta, _attach_agent_resources, get_db
from kb.letta_tools import get_kb_tool_ids


BACKUP_DIR = "/tmp"


def _backup_path(agent_id: str) -> str:
    return os.path.join(BACKUP_DIR, f"canary_backup_{agent_id[-12:]}.json")


def _inspect(agent_id: str) -> dict:
    agent = letta.agents.retrieve(agent_id=agent_id, include=["agent.tools"])
    tools = [{"id": t.id, "name": t.name} for t in (agent.tools or [])]
    folders_page = letta.agents.folders.list(agent_id=agent_id)
    folders = [{"id": f.id, "name": f.name} for f in getattr(folders_page, "items", folders_page)]
    return {
        "agent_id": agent_id,
        "agent_name": agent.name,
        "metadata": agent.metadata,
        "tools": tools,
        "folders": folders,
    }


def _print_state(label: str, state: dict):
    print(f"\n{label}:")
    print(f"  agent: {state['agent_id']}  ({state['agent_name']})")
    print(f"  folders ({len(state['folders'])}):")
    for f in state["folders"]:
        print(f"    - {f['name']:40s} {f['id']}")
    print(f"  tools ({len(state['tools'])}):")
    for t in state["tools"]:
        print(f"    - {t['name']:40s} {t['id']}")


def swap_to_kb(agent_id: str, project_id: str, user_id: str):
    print(f"\n========== canary swap: {agent_id} ==========")
    before = _inspect(agent_id)
    _print_state("BEFORE", before)

    # 保存 before 状态
    with open(_backup_path(agent_id), "w") as f:
        json.dump({"before": before, "project_id": project_id, "user_id": user_id}, f, indent=2, default=str)
    print(f"\n  state backed up to {_backup_path(agent_id)}")

    # Detach 所有 folder
    print(f"\n-- detach {len(before['folders'])} folders --")
    for f in before["folders"]:
        try:
            letta.agents.folders.detach(agent_id=agent_id, folder_id=f["id"])
            print(f"  - {f['name']}")
        except Exception as e:
            print(f"  ! {f['name']}: {e}")

    # Attach kb tools
    print(f"\n-- attach kb tools --")
    tool_ids = get_kb_tool_ids()
    for tid in tool_ids:
        try:
            letta.agents.tools.attach(agent_id=agent_id, tool_id=tid)
            print(f"  + {tid[-16:]}")
        except Exception as e:
            msg = str(e).lower()
            if "conflict" in msg or "already" in msg:
                print(f"  = {tid[-16:]} (already attached)")
            else:
                print(f"  ! {tid[-16:]}: {e}")

    # After state
    after = _inspect(agent_id)
    _print_state("AFTER", after)

    # Diff 摘要
    print(f"\n-- summary --")
    print(f"  folders: {len(before['folders'])} → {len(after['folders'])}")
    print(f"  tools:   {len(before['tools'])} → {len(after['tools'])}")
    kb_attached = sum(1 for t in after["tools"] if t["name"] in ("list_project_files", "read_project_file", "grep_project_files"))
    print(f"  kb 3 工具 attached: {kb_attached}/3")
    print(f"\n手工测: 问这个 agent '这个 project 里有啥文件'")
    print(f"Rollback: python3 /app/scripts/canary_swap.py --agent-id {agent_id} --project {project_id} --rollback")


def rollback(agent_id: str, project_id: str):
    print(f"\n========== rollback: {agent_id} ==========")
    bp = _backup_path(agent_id)
    if not os.path.exists(bp):
        print(f"  ERROR: backup not found at {bp}")
        sys.exit(1)
    with open(bp) as f:
        bak = json.load(f)
    user_id = bak.get("user_id", "")
    before_folders = bak["before"]["folders"]
    before_tools = bak["before"]["tools"]

    # detach kb tools (撤销本次 attach)
    current = _inspect(agent_id)
    current_kb_tools = [t for t in current["tools"] if t["name"] in ("list_project_files", "read_project_file", "grep_project_files")]
    print(f"\n-- detach kb tools ({len(current_kb_tools)}) --")
    for t in current_kb_tools:
        try:
            letta.agents.tools.detach(agent_id=agent_id, tool_id=t["id"])
            print(f"  - {t['name']}")
        except Exception as e:
            print(f"  ! {t['name']}: {e}")

    # reattach folder (用 _attach_agent_resources 的路径保证一致)
    print(f"\n-- re-attach folders via _attach_agent_resources --")
    db = get_db()
    try:
        _attach_agent_resources(db, agent_id, user_id, project_id)
        print(f"  OK")
    finally:
        db.close()

    after = _inspect(agent_id)
    _print_state("AFTER ROLLBACK", after)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--user-id", default="", help="required for rollback path; optional for swap")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    if args.rollback:
        rollback(args.agent_id, args.project)
    else:
        # swap 路径不需要 user_id (retrieve agent 能拿到), 但 rollback 需要
        if not args.user_id:
            agent = letta.agents.retrieve(agent_id=args.agent_id)
            args.user_id = (agent.metadata or {}).get("owner", "")
        swap_to_kb(args.agent_id, args.project, args.user_id)


if __name__ == "__main__":
    main()
