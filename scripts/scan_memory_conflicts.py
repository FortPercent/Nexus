"""扫描 memory_history,把同 (project_id, display_name) 多个活动 memory_id
合并成一条 memory_conflicts 工单。

backfill 跑完之后跑一遍这个,把历史里同名多版本的文件都暴露成工单。
此后 mirror_file hook 会增量维护(在 memory_helpers.detect_and_record_conflict 里)。

幂等:重复跑只会更新现有 unresolved 工单的 memory_ids 并集,不会重复建。

用法:
    python scripts/scan_memory_conflicts.py            # dry-run 看会建多少
    python scripts/scan_memory_conflicts.py --apply    # 真正写
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_db, _ensure_wal


def scan(apply: bool = False) -> dict:
    _ensure_wal()
    db = get_db()
    db.row_factory = sqlite3.Row

    # 找出每个 project 内, "[文件] xxx" 形式的活动 memory(忽略最新事件是 DELETE 的)
    rows = db.execute("""
        WITH latest AS (
          SELECT memory_id, project_id, event_type, new_memory,
                 ROW_NUMBER() OVER (
                   PARTITION BY memory_id
                   ORDER BY changed_at DESC, history_id DESC
                 ) AS rn
          FROM memory_history
        )
        SELECT memory_id, project_id, new_memory
        FROM latest
        WHERE rn = 1 AND event_type != 'DELETE' AND new_memory LIKE '[文件] %'
    """).fetchall()

    # 按 (project_id, new_memory) 聚簇 → 候选冲突组
    groups: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        key = (r["project_id"], r["new_memory"])
        groups.setdefault(key, []).append(r["memory_id"])

    candidates = {k: sorted(v) for k, v in groups.items() if len(v) > 1}

    plan_create = 0
    plan_update = 0
    written_create = 0
    written_update = 0
    sample = []

    for (project_id, new_memory), ids in candidates.items():
        display_name = new_memory[len("[文件] "):]
        reason = f"same_display_name:{display_name}"

        existing = db.execute(
            """SELECT conflict_id, memory_ids FROM memory_conflicts
               WHERE project_id = ? AND detection_reason = ? AND resolved_at IS NULL
               ORDER BY conflict_id ASC LIMIT 1""",
            (project_id, reason),
        ).fetchone()

        if existing:
            try:
                old_ids = set(json.loads(existing["memory_ids"] or "[]"))
            except Exception:
                old_ids = set()
            merged = sorted(old_ids | set(ids))
            if merged == sorted(old_ids):
                continue
            plan_update += 1
            if apply:
                db.execute(
                    "UPDATE memory_conflicts SET memory_ids = ? WHERE conflict_id = ?",
                    (json.dumps(merged, ensure_ascii=False), existing["conflict_id"]),
                )
                written_update += 1
        else:
            plan_create += 1
            if len(sample) < 5:
                sample.append({"project_id": project_id, "display_name": display_name, "members": len(ids)})
            if apply:
                db.execute(
                    """INSERT INTO memory_conflicts
                       (project_id, memory_ids, detection_reason)
                       VALUES (?, ?, ?)""",
                    (project_id, json.dumps(ids, ensure_ascii=False), reason),
                )
                written_create += 1

    if apply:
        db.commit()
    db.close()

    return {
        "candidate_groups": len(candidates),
        "plan_create": plan_create,
        "plan_update": plan_update,
        "written_create": written_create,
        "written_update": written_update,
        "sample_first_5_create": sample,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    print(json.dumps(scan(apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
