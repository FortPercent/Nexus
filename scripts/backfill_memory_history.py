"""Backfill memory_history from existing knowledge_mirrors.

V1 设计取舍:V1 把"一条上传文件"作为 memory 的最小单元(memory_id = knowledge_id),
而不是 archival passage_id。理由:
  1. 文件级粒度跟"版本冲突"治理直接对得上(同 source_file 不同版本)
  2. adapter 自己有完整元数据,无需枚举 Letta passage
  3. trace 的演示价值就在文件维度("制度 v1 → v2 → v3")

将来扩到 passage 级 memory 时,memory_id 命名空间用前缀区分:
  - file:<knowledge_id>      ← V1
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

    cur = db.execute("""
        SELECT knowledge_id, letta_file_id, scope, scope_id, owner_id,
               for_user_id, display_name, created_at
        FROM knowledge_mirrors
        ORDER BY created_at ASC
    """)
    mirrors = cur.fetchall()

    written = 0
    skipped = 0
    plan = []
    for m in mirrors:
        memory_id = f"file:{m['knowledge_id']}"
        project_id = _scope_to_project_id(m["scope"], m["scope_id"] or "", m["owner_id"] or "")
        event_id = f"backfill:{m['knowledge_id']}"

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
            db.execute(
                """INSERT INTO memory_history
                   (memory_id, project_id, event_type, new_memory, event_id,
                    source_messages, actor_user_id, changed_at)
                   VALUES (?, ?, 'ADD', ?, ?, '[]', ?, ?)""",
                (
                    memory_id,
                    project_id,
                    f"[文件] {m['display_name']}",
                    event_id,
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
