"""Sync helpers for memory_history / memory_conflicts.

供 knowledge_mirror / webui_hook 等同步路径调用。memory_api.py 是 async,
两边公用的写入逻辑放在这里避免循环导入。

关键约定:
  memory_id 命名空间(用前缀区分),W2 阶段只用 file: 前缀。
    - file:<letta_file_id>   ← 一个逻辑文件
    - passage:<passage_id>   ← 将来扩
    - decision:<decision_id> ← 将来扩
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, Optional

from db import use_db

logger = logging.getLogger(__name__)


def record_memory_event(
    *,
    memory_id: str,
    project_id: str,
    event_type: str,            # ADD / UPDATE / DELETE
    new_memory: str,
    event_id: str = "",
    source_messages: Optional[list] = None,
    actor_user_id: str = "",
) -> Optional[int]:
    """写一条 memory_history。同 (memory_id, event_id) 已存在则跳过(幂等)。

    返回 history_id 或 None(已存在)。
    """
    if event_type not in ("ADD", "UPDATE", "DELETE"):
        raise ValueError(f"invalid event_type: {event_type}")

    msgs_json = json.dumps(source_messages or [], ensure_ascii=False)
    with use_db() as db:
        existing = db.execute(
            "SELECT history_id FROM memory_history WHERE memory_id = ? AND event_id = ?",
            (memory_id, event_id),
        ).fetchone()
        if existing:
            return None
        cur = db.execute(
            """INSERT INTO memory_history
               (memory_id, project_id, event_type, new_memory, event_id,
                source_messages, actor_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (memory_id, project_id, event_type, new_memory, event_id, msgs_json, actor_user_id),
        )
        return cur.lastrowid


def detect_and_record_conflict(
    *,
    project_id: str,
    new_memory_id: str,
    display_name: str,
) -> Optional[int]:
    """检测同 project_id 内是否有 display_name 相同 / memory_id 不同的活动 memory。

    判定:其它 memory_id 最近一条事件不是 DELETE 且 new_memory 命中相同 display_name。

    每个 (project_id, display_name) 至多一条 unresolved 工单:
      - 已存在同名未解决工单 → UPDATE memory_ids = union(existing, all_ids)
      - 没有 → INSERT 新工单
    返回 conflict_id 或 None(< 2 个成员,无冲突)。
    """
    needle = f"[文件] {display_name}"
    reason = f"same_display_name:{display_name}"
    with use_db() as db:
        # 找出当前 project 里 new_memory 命中相同 display_name 的所有 memory_id
        # 用 latest event per memory_id 的子查询去掉已 DELETE 的
        rows = db.execute(
            """
            WITH latest AS (
              SELECT memory_id, event_type, new_memory,
                     ROW_NUMBER() OVER (PARTITION BY memory_id ORDER BY changed_at DESC, history_id DESC) AS rn
              FROM memory_history
              WHERE project_id = ?
            )
            SELECT memory_id FROM latest
            WHERE rn = 1 AND event_type != 'DELETE' AND new_memory = ?
            """,
            (project_id, needle),
        ).fetchall()
        all_ids = sorted({r["memory_id"] for r in rows} | {new_memory_id})
        if len(all_ids) < 2:
            return None

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
            merged = sorted(old_ids | set(all_ids))
            if merged != sorted(old_ids):
                db.execute(
                    "UPDATE memory_conflicts SET memory_ids = ? WHERE conflict_id = ?",
                    (json.dumps(merged, ensure_ascii=False), existing["conflict_id"]),
                )
                logger.info(
                    f"[memory] conflict updated project={project_id} display={display_name!r} "
                    f"members={len(merged)} conflict_id={existing['conflict_id']}"
                )
            return existing["conflict_id"]

        cur = db.execute(
            """INSERT INTO memory_conflicts
               (project_id, memory_ids, detection_reason)
               VALUES (?, ?, ?)""",
            (project_id, json.dumps(all_ids, ensure_ascii=False), reason),
        )
        cid = cur.lastrowid
        logger.info(
            f"[memory] conflict detected project={project_id} display={display_name!r} "
            f"members={len(all_ids)} conflict_id={cid}"
        )
        return cid


def scope_to_project_id(scope: str, scope_id: str, owner_id: str = "") -> str:
    """统一 scope/scope_id → project_id 维度,跟 backfill 脚本一致。"""
    if scope == "project":
        return scope_id or owner_id or ""
    if scope == "personal":
        return f"personal:{scope_id or owner_id}"
    if scope == "org":
        return "org"
    return scope_id or owner_id or "unknown"
