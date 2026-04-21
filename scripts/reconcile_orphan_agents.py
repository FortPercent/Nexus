#!/usr/bin/env python3
"""孤儿 Letta agent 回收.

场景:
  - _rebuild_agent_async 里 letta.agents.delete 失败被吞 (network/letta down)
  - project rename 路径 adapter 删了 map 行但 Letta agent 没删
  - 其他异常退出留下的残留

判据: Letta agent 不在 user_agent_map.agent_id 集合里.
安全: created_at < now - min_age_hours (防 race: 新 agent 刚建、map 还没 insert)
     detach 共享 block (human/org_knowledge/project_knowledge_*) 防 cascade

用法:
    docker exec teleai-adapter python3 /app/scripts/reconcile_orphan_agents.py --dry-run
    docker exec teleai-adapter python3 /app/scripts/reconcile_orphan_agents.py
"""
import argparse
import datetime as _dt
import logging
import sqlite3
import sys

sys.path.insert(0, "/app")

from routing import letta
from config import DB_PATH


_SHARED_LABELS = ("human", "org_knowledge")
_SHARED_PREFIX = "project_knowledge_"


def _is_shared_block(label: str) -> bool:
    return label in _SHARED_LABELS or (label or "").startswith(_SHARED_PREFIX)


def _list_all_agents() -> list:
    agents = []
    after = None
    while True:
        page = letta.agents.list(after=after, limit=100) if after else letta.agents.list(limit=100)
        items = list(getattr(page, "items", page) if hasattr(page, "items") else page)
        if not items:
            break
        agents.extend(items)
        if len(items) < 100:
            break
        after = items[-1].id
    return agents


def _tracked_agent_ids() -> set:
    c = sqlite3.connect(DB_PATH)
    try:
        rows = c.execute("SELECT agent_id FROM user_agent_map").fetchall()
    finally:
        c.close()
    return {r[0] for r in rows if r[0]}


def _agent_age_ok(agent, min_age_hours: float) -> bool:
    ca = getattr(agent, "created_at", None)
    if ca is None:
        return True
    if isinstance(ca, str):
        try:
            ca = _dt.datetime.fromisoformat(ca.replace("Z", "+00:00"))
        except Exception:
            return True
    now = _dt.datetime.now(_dt.timezone.utc)
    if ca.tzinfo is None:
        ca = ca.replace(tzinfo=_dt.timezone.utc)
    return (now - ca).total_seconds() >= min_age_hours * 3600


def reconcile_orphans(dry_run: bool = False, min_age_hours: float = 1.0) -> dict:
    agents = _list_all_agents()
    tracked = _tracked_agent_ids()
    orphans = [a for a in agents if a.id not in tracked]

    eligible = []
    too_young = 0
    for a in orphans:
        if _agent_age_ok(a, min_age_hours):
            eligible.append(a)
        else:
            too_young += 1

    stats = {
        "listed": len(agents),
        "tracked": len(tracked),
        "orphans_total": len(orphans),
        "orphans_too_young": too_young,
        "eligible": len(eligible),
        "blocks_detached": 0,
        "deleted": 0,
        "delete_failed": 0,
    }

    for a in eligible:
        aid = a.id
        name = getattr(a, "name", "?") or "?"
        try:
            page = letta.agents.blocks.list(agent_id=aid)
            blocks = list(getattr(page, "items", page) if hasattr(page, "items") else page)
        except Exception as e:
            logging.warning(f"[orphan {aid[-12:]}] list blocks: {e}")
            blocks = []
        shared = [b for b in blocks if _is_shared_block(getattr(b, "label", ""))]

        if dry_run:
            print(f"  [would delete] {name[:40]:40s} {aid}  shared_blocks={len(shared)}")
            continue

        for b in shared:
            try:
                letta.agents.blocks.detach(agent_id=aid, block_id=b.id)
                stats["blocks_detached"] += 1
            except Exception as e:
                logging.warning(f"[orphan {aid[-12:]}] detach {b.id[-12:]}: {e}")

        try:
            letta.agents.delete(agent_id=aid)
            stats["deleted"] += 1
            logging.info(f"[orphan] deleted {aid} ({name[:40]})")
        except Exception as e:
            stats["delete_failed"] += 1
            logging.warning(f"[orphan] delete {aid} failed: {e}")

    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列要删的,不真删")
    ap.add_argument("--min-age-hours", type=float, default=1.0,
                    help="孤儿需存在多久才能删 (防 race,默认 1h)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(f"mode: {'DRY-RUN' if args.dry_run else 'REAL'}  min_age_hours={args.min_age_hours}")
    stats = reconcile_orphans(dry_run=args.dry_run, min_age_hours=args.min_age_hours)
    print(f"\nstats: {stats}")


if __name__ == "__main__":
    main()
