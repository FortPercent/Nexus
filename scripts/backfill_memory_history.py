"""Backfill memory_history from existing knowledge_mirrors.

V1 设计取舍:memory_id = file:<letta_file_id>(逻辑文件,不是 user-mirror 维度的 knowledge_id)。
理由:
  1. 同一个文件分享给 N 个项目成员会产生 N 条 knowledge_mirrors,但是 memory 只有 1 条
  2. 文件级粒度跟"版本冲突"治理直接对得上(同 display_name 不同 letta_file_id)
  3. adapter 自有元数据,无需枚举 Letta passage

将来扩到 passage 级 memory 时,memory_id 命名空间用前缀区分:
  - file:<letta_file_id>     ← V1
  - passage:<passage_id>     ← V2+
  - decision:<decision_id>   ← V2+(决策追溯)

用法:
    python scripts/backfill_memory_history.py            # dry-run 看会写多少
    python scripts/backfill_memory_history.py --apply    # 真正写
    python scripts/backfill_memory_history.py --reset    # 清空 history 重灌(危险)
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

# 让脚本能 import adapter 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_db, _ensure_wal


def _scope_to_project_id(scope: str, scope_id: str, owner_id: str) -> str:
    """把 scope/scope_id 映射回 project_id 用作 history.project_id 维度。

    - scope='project' → scope_id 即 project_id
    - scope='personal' → 'personal:' + user_id
    - scope='org' → 'org'
    """
    if scope == "project":
        return scope_id or owner_id or ""
    if scope == "personal":
        return f"personal:{scope_id or owner_id}"
    if scope == "org":
        return "org"
    return scope_id or owner_id or "unknown"


def backfill(apply: bool = False, reset: bool = False) -> dict:
    _ensure_wal()
    db = get_db()
    db.row_factory = sqlite3.Row

    if reset:
        if not apply:
            print("ERROR: --reset 必须配合 --apply 使用")
            return {"reset": False}
        db.execute("DELETE FROM memory_history WHERE event_id LIKE 'backfill:%'")
        db.commit()
        print("已清空 backfill 写入的 history 行")

    # 按 letta_file_id 去重,取每个逻辑文件最早的镜像作为 ADD 来源
    cur = db.execute("""
        SELECT letta_file_id,
               MIN(created_at) AS created_at,
               -- 同 letta_file_id 下不同 mirror 的 scope/scope_id 应一致,取任一即可
               scope, scope_id, owner_id, for_user_id, display_name
        FROM knowledge_mirrors
        GROUP BY letta_file_id
        ORDER BY MIN(created_at) ASC
    """)
    mirrors = cur.fetchall()

    written = 0
    skipped = 0
    plan = []
    for m in mirrors:
        memory_id = f"file:{m['letta_file_id']}"
        project_id = _scope_to_project_id(m["scope"], m["scope_id"] or "", m["owner_id"] or "")
        event_id = f"backfill:{m['letta_file_id']}"

        # 已存在则跳过
        existing = db.execute(
            "SELECT 1 FROM memory_history WHERE memory_id = ? AND event_id = ?",
            (memory_id, event_id),
        ).fetchone()
        if existing:
            skipped += 1
            continue

        plan.append({
            "memory_id": memory_id,
            "project_id": project_id,
            "display_name": m["display_name"],
            "created_at": m["created_at"],
        })

        if apply:
            # source_messages 里塞个 backfill 占位(JSON,显式标记来源,方便排查)
            src = json.dumps([{"role": "system", "content": "backfill_from_knowledge_mirrors"}], ensure_ascii=False)
            db.execute(
                """INSERT INTO memory_history
                   (memory_id, project_id, event_type, new_memory, event_id,
                    source_messages, actor_user_id, changed_at)
                   VALUES (?, ?, 'ADD', ?, ?, ?, ?, ?)""",
                (
                    memory_id,
                    project_id,
                    f"[文件] {m['display_name']}",
                    event_id,
                    src,
                    m["owner_id"] or m["for_user_id"] or "",
                    m["created_at"],
                ),
            )
            written += 1

    if apply:
        db.commit()

    db.close()

    return {
        "mirrors_total": len(mirrors),
        "would_write": len(plan) if not apply else 0,
        "written": written,
        "skipped_existing": skipped,
        "sample_first_3": plan[:3],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写入(不加是 dry-run)")
    parser.add_argument("--reset", action="store_true", help="先清空 backfill: 开头的 history 行(需配 --apply)")
    args = parser.parse_args()

    result = backfill(apply=args.apply, reset=args.reset)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
