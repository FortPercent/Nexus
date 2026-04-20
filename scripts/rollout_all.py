#!/usr/bin/env python3
"""Phase 1 Chunk 7: 对 user_agent_map 里**所有** agent 跑 canary_swap.

幂等:
  - 已 detach 所有 folder 且已挂 3 个 kb 工具 → skip
  - 否则 detach folder + attach kb 工具 + 写 /tmp/canary_backup_<id>.json

排除:
  - test-kb-poc-* (PoC 测试 agent 不在 user_agent_map, 天然不会处理)

Rollback:
  - 单个: python3 /app/scripts/canary_swap.py --agent-id <aid> --project <pid> --rollback
  - 全量: python3 /app/scripts/rollout_all.py --rollback-all

用法:
    # 干跑看影响
    python3 /app/scripts/rollout_all.py --dry-run

    # 真推
    python3 /app/scripts/rollout_all.py

    # 全量回滚
    python3 /app/scripts/rollout_all.py --rollback-all
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/app")

from routing import letta, _attach_agent_resources, get_db
from kb.letta_tools import get_kb_tool_ids

KB_TOOL_NAMES = {"list_project_files", "read_project_file", "grep_project_files"}
BACKUP_DIR = "/tmp"


def _backup_path(agent_id: str) -> str:
    return os.path.join(BACKUP_DIR, f"canary_backup_{agent_id[-12:]}.json")


def _inspect(agent_id: str):
    """返回 (folders_list, tool_names_set, tools_full_list)."""
    agent = letta.agents.retrieve(agent_id=agent_id, include=["agent.tools"])
    folders_page = letta.agents.folders.list(agent_id=agent_id)
    folders = [{"id": f.id, "name": f.name} for f in getattr(folders_page, "items", folders_page)]
    tools = [{"id": t.id, "name": t.name} for t in (agent.tools or [])]
    return folders, {t["name"] for t in tools}, tools


def swap_one(agent_id: str, project_id: str, user_id: str, dry_run: bool) -> dict:
    folders, tool_names, tools = _inspect(agent_id)

    # 判断是否已 swap
    if not folders and KB_TOOL_NAMES.issubset(tool_names):
        return {"status": "already_swapped", "folders": 0, "tools": len(tools)}

    if dry_run:
        return {"status": "would_swap", "folders": len(folders), "tools": len(tools)}

    # 存 backup
    with open(_backup_path(agent_id), "w") as f:
        json.dump({
            "agent_id": agent_id, "project_id": project_id, "user_id": user_id,
            "folders": folders, "tools": tools,
        }, f, default=str)

    # detach folders
    detached = 0
    for fl in folders:
        try:
            letta.agents.folders.detach(agent_id=agent_id, folder_id=fl["id"])
            detached += 1
        except Exception as e:
            print(f"    ! detach {fl['name']}: {e}")

    # attach kb tools
    tool_ids = get_kb_tool_ids()
    attached = 0
    for tid in tool_ids:
        try:
            letta.agents.tools.attach(agent_id=agent_id, tool_id=tid)
            attached += 1
        except Exception as e:
            msg = str(e).lower()
            if "conflict" not in msg and "already" not in msg:
                print(f"    ! attach {tid[-16:]}: {e}")
            else:
                attached += 1  # conflict = already attached, 算成功

    return {"status": "swapped", "detached": detached, "attached": attached}


def rollback_one(agent_id: str, project_id: str) -> dict:
    bp = _backup_path(agent_id)
    if not os.path.exists(bp):
        return {"status": "no_backup"}
    with open(bp) as f:
        bak = json.load(f)
    user_id = bak.get("user_id", "")

    # detach kb 工具
    folders, tool_names, tools = _inspect(agent_id)
    kb_tools = [t for t in tools if t["name"] in KB_TOOL_NAMES]
    for t in kb_tools:
        try:
            letta.agents.tools.detach(agent_id=agent_id, tool_id=t["id"])
        except Exception:
            pass

    # reattach folders via standard path
    db = get_db()
    try:
        _attach_agent_resources(db, agent_id, user_id, project_id)
    finally:
        db.close()

    return {"status": "rolled_back"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback-all", action="store_true")
    ap.add_argument("--db-path", default="/data/serving/adapter/adapter.db")
    args = ap.parse_args()

    c = sqlite3.connect(args.db_path)
    rows = c.execute("SELECT project_id, user_id, agent_id FROM user_agent_map ORDER BY project_id").fetchall()
    c.close()
    print(f"Target agents: {len(rows)}")

    if args.rollback_all:
        stats = {"rolled_back": 0, "no_backup": 0, "error": 0}
        for pid, uid, aid in rows:
            try:
                r = rollback_one(aid, pid)
                s = r["status"]
                stats[s] = stats.get(s, 0) + 1
                print(f"  [{s:14s}] {pid:35s} {aid[-12:]}")
            except Exception as e:
                stats["error"] += 1
                print(f"  ERROR {pid} {aid[-12:]}: {e}")
        print(f"\nRollback stats: {stats}")
        return

    stats = {"swapped": 0, "already_swapped": 0, "would_swap": 0, "error": 0}
    for pid, uid, aid in rows:
        try:
            r = swap_one(aid, pid, uid, args.dry_run)
            s = r["status"]
            stats[s] = stats.get(s, 0) + 1
            extra = ""
            if s == "swapped":
                extra = f"  (detached={r['detached']}, attached kb={r['attached']})"
            elif s == "would_swap":
                extra = f"  (folders={r['folders']}, tools={r['tools']})"
            print(f"  [{s:16s}] {pid:35s} {aid[-12:]}{extra}")
        except Exception as e:
            stats["error"] += 1
            print(f"  ERROR {pid} {aid[-12:]}: {e}")

    print(f"\nTotal: {stats}")


if __name__ == "__main__":
    main()
