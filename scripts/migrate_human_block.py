"""60.0 Migration — 合一用户跨项目的 human block。

目标：每个用户跨项目的所有 agent 共享同一个 human block。

执行规则：
  - 列出每个用户的所有 agent
  - 找到每个 agent 当前 attach 的 human block
  - 选取 truth：user_cache.personal_human_block_id（已设置）> 最新 updated_at > 第一个
  - 确保 truth block 的 value 是"最合并"的内容（取最长非默认的那个）
  - 其他 agent：detach 旧 human，attach truth；旧 block 若不被其他 agent 引用则删除
  - 把 truth block_id 写回 user_cache.personal_human_block_id

幂等。可重复跑。

用法（容器内）：
  docker exec teleai-adapter python /app/scripts/migrate_human_block.py
  docker exec teleai-adapter python /app/scripts/migrate_human_block.py --dry-run
"""
import argparse
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routing import letta
from config import DB_PATH

DEFAULT_VALUE = "(新用户，信息未知)"


def list_user_agent_blocks(agent_id):
    """返回 [(block_id, label, value, updated_at)] for one agent"""
    blocks = letta.agents.blocks.list(agent_id=agent_id)
    return list(getattr(blocks, "items", blocks))


def pick_truth(human_blocks, preset_id=None):
    """从多个 human block 里选一个 truth。preset_id 优先（user_cache 已存的）。"""
    if preset_id:
        for b in human_blocks:
            if b.id == preset_id:
                return b
    # 选"最长非默认"的那个
    meaningful = [b for b in human_blocks if (b.value or "").strip() != DEFAULT_VALUE]
    if meaningful:
        return max(meaningful, key=lambda b: len(b.value or ""))
    # 全是默认值，选第一个
    return human_blocks[0] if human_blocks else None


def migrate(dry_run: bool):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    users = conn.execute(
        "SELECT DISTINCT user_id FROM user_agent_map"
    ).fetchall()

    total_users = 0
    already_unified = 0
    merged = 0
    errors = []

    for u in users:
        user_id = u["user_id"]
        total_users += 1

        rows = conn.execute(
            "SELECT agent_id, project_id FROM user_agent_map WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        preset = conn.execute(
            "SELECT personal_human_block_id FROM user_cache WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        preset_id = preset["personal_human_block_id"] if preset else None

        human_by_agent = {}
        all_human_blocks = []
        for r in rows:
            try:
                blocks = list_user_agent_blocks(r["agent_id"])
            except Exception as e:
                errors.append(f"{user_id}/{r['project_id']}: list blocks failed: {e}")
                continue
            for b in blocks:
                if b.label == "human":
                    human_by_agent[r["agent_id"]] = b
                    all_human_blocks.append(b)

        if not all_human_blocks:
            print(f"[skip] user={user_id[:8]} — 无 human block")
            continue

        unique_ids = {b.id for b in all_human_blocks}
        if len(unique_ids) == 1 and preset_id == list(unique_ids)[0]:
            already_unified += 1
            print(f"[ok]   user={user_id[:8]} — 已合一 block={list(unique_ids)[0][:12]}")
            continue

        truth = pick_truth(all_human_blocks, preset_id=preset_id)
        if not truth:
            continue

        truth_id = truth.id
        other_ids = unique_ids - {truth_id}
        print(f"[merge]user={user_id[:8]} truth={truth_id[:12]} others={[x[:12] for x in other_ids]} agents={len(rows)}")

        if dry_run:
            continue

        # detach 旧 + attach truth
        for agent_id, hb in human_by_agent.items():
            if hb.id == truth_id:
                continue
            try:
                letta.agents.blocks.detach(agent_id=agent_id, block_id=hb.id)
            except Exception as e:
                errors.append(f"{agent_id}: detach {hb.id} failed: {e}")
            try:
                letta.agents.blocks.attach(agent_id=agent_id, block_id=truth_id)
            except Exception as e:
                errors.append(f"{agent_id}: attach {truth_id} failed: {e}")

        # 删 orphan block（它们现在没 agent 引用了）
        for oid in other_ids:
            try:
                letta.blocks.delete(block_id=oid)
            except Exception as e:
                errors.append(f"delete orphan {oid}: {e}")

        # 写回 user_cache
        conn.execute(
            "INSERT INTO user_cache (user_id, personal_human_block_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET personal_human_block_id = excluded.personal_human_block_id, "
            "updated_at = CURRENT_TIMESTAMP",
            (user_id, truth_id),
        )
        conn.commit()
        merged += 1

    # 把已经只有一个但 user_cache 没记录的也 backfill 一下
    for u in users:
        user_id = u["user_id"]
        preset = conn.execute(
            "SELECT personal_human_block_id FROM user_cache WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if preset and preset["personal_human_block_id"]:
            continue
        # 重新扫一次现在的 human block
        rows = conn.execute(
            "SELECT agent_id FROM user_agent_map WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
        if not rows:
            continue
        try:
            blocks = list_user_agent_blocks(rows["agent_id"])
            human = next((b for b in blocks if b.label == "human"), None)
            if human:
                if dry_run:
                    print(f"[cache]user={user_id[:8]} would cache {human.id[:12]}")
                else:
                    conn.execute(
                        "INSERT INTO user_cache (user_id, personal_human_block_id) VALUES (?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET personal_human_block_id = excluded.personal_human_block_id, "
                        "updated_at = CURRENT_TIMESTAMP",
                        (user_id, human.id),
                    )
                    conn.commit()
                    print(f"[cache]user={user_id[:8]} cached {human.id[:12]}")
        except Exception as e:
            errors.append(f"cache {user_id}: {e}")

    conn.close()
    print()
    print(f"==== {'DRY RUN' if dry_run else 'APPLIED'}: users={total_users} already_unified={already_unified} merged={merged} ====")
    if errors:
        print(f"errors ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
    sys.exit(0 if not errors else 1)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    migrate(args.dry_run)
