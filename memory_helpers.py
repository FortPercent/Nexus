"""Sync helpers for memory_history / memory_conflicts / Safety Memory enforce.

供 knowledge_mirror / webui_hook 等同步路径调用。memory_api.py 是 async,
两边公用的写入逻辑放在这里避免循环导入。

关键约定:
  memory_id 命名空间(用前缀区分),W2 阶段只用 file: 前缀。
    - file:<letta_file_id>   ← 一个逻辑文件
    - passage:<passage_id>   ← 将来扩
    - decision:<decision_id> ← 将来扩

Safety Memory protection_level 语义 (此处 enforce):
  - mutable    (默认): 任何 ADD/UPDATE/DELETE 放行
  - append_only:       允许 ADD/UPDATE, 拒绝 DELETE
  - read_only:         允许 ADD (新建用), 但拒绝 UPDATE/DELETE
                       ADD 不拦截是因为我们 INSERT OR IGNORE 模式下
                       新事件如果 (memory_id, event_id) 已存在自然跳过,
                       重复 ADD 实际上不会真写入 (uq_mh_memory_event 兜底)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from db import use_db

logger = logging.getLogger(__name__)


class ProtectionViolation(Exception):
    """memory protection_level 拒绝写动作。caller 应捕获并决定降级行为。"""

    def __init__(self, memory_id: str, level: str, event_type: str):
        self.memory_id = memory_id
        self.level = level
        self.event_type = event_type
        super().__init__(
            f"memory_id={memory_id} 设置为 {level}, 拒绝 {event_type}"
        )


def protection_blocks(level: Optional[str], event_type: str) -> bool:
    """pure 规则函数:protection_level 是否拒绝该 event_type。

    单一定义, sync 和 async 调用方共用 (避免规则漂移)。
      mutable / 空 / 未知:不拒绝
      read_only:拒绝 UPDATE / DELETE
      append_only:拒绝 DELETE
    """
    if not level or level == "mutable":
        return False
    if level == "read_only":
        return event_type in ("UPDATE", "DELETE")
    if level == "append_only":
        return event_type == "DELETE"
    return False  # 未知 level → 当 mutable 处理 (defensive)


def _enforce_protection_sync(db, memory_id: str, event_type: str) -> None:
    """sync 版 protection 检查。共用调用方的 db 连接。

    raise ProtectionViolation 表示拒绝;返回 None 表示放行。
    """
    row = db.execute(
        "SELECT protection_level FROM memory_protection WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()
    level = row["protection_level"] if row else None
    if protection_blocks(level, event_type):
        raise ProtectionViolation(memory_id, level, event_type)


def _audit_protection_block_sync(
    db, *, memory_id: str, level: str, event_type: str, actor_user_id: str
) -> None:
    """拒绝事件写 audit_log 留痕。"""
    db.execute(
        "INSERT INTO audit_log (user_id, action, scope, details) VALUES (?, ?, ?, ?)",
        (
            actor_user_id or "",
            "memory.protection.block",
            "",
            json.dumps(
                {"memory_id": memory_id, "protection_level": level, "blocked_event": event_type},
                ensure_ascii=False,
            ),
        ),
    )


def check_protection_for_delete(memory_id: str) -> Optional[str]:
    """上游入口用:DELETE 操作发起前检查 protection。

    返回 protection_level (read_only / append_only) 表示 block;None 表示允许。

    设计意图:让 admin_api 的 file delete 端点能在调 Letta 删除前判断 protection,
    而不是等到事后 record_memory_event 拒绝时已经无法回滚 Letta 状态。
    """
    with use_db() as db:
        row = db.execute(
            "SELECT protection_level FROM memory_protection WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
    if not row:
        return None
    level = row["protection_level"]
    return level if level in ("read_only", "append_only") else None


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

    Safety Memory: 写入前检查 protection_level, 拒绝时:
      - 写一条 memory.protection.block audit
      - raise ProtectionViolation, caller 决定降级行为(skip / propagate)

    返回 history_id;已存在返 None;被拒绝 raise ProtectionViolation。
    """
    if event_type not in ("ADD", "UPDATE", "DELETE"):
        raise ValueError(f"invalid event_type: {event_type}")

    msgs_json = json.dumps(source_messages or [], ensure_ascii=False)
    with use_db() as db:
        # 幂等:同 (memory_id, event_id) 已写过则跳过
        existing = db.execute(
            "SELECT history_id FROM memory_history WHERE memory_id = ? AND event_id = ?",
            (memory_id, event_id),
        ).fetchone()
        if existing:
            return None

        # Safety Memory enforce
        try:
            _enforce_protection_sync(db, memory_id, event_type)
        except ProtectionViolation as exc:
            _audit_protection_block_sync(
                db,
                memory_id=memory_id,
                level=exc.level,
                event_type=event_type,
                actor_user_id=actor_user_id,
            )
            logger.warning(
                f"[memory] protection block: memory_id={memory_id} level={exc.level} "
                f"event_type={event_type} actor={actor_user_id}"
            )
            raise

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
    """统一 scope/scope_id → project_id 维度,跟 backfill 脚本一致。

    未知 scope 直接 raise, 不返垃圾值 (e.g. "unknown") 污染 project_id 列。
    """
    if scope == "project":
        if not scope_id:
            raise ValueError("scope=project 必须提供 scope_id")
        return scope_id
    if scope == "personal":
        owner = scope_id or owner_id
        if not owner:
            raise ValueError("scope=personal 必须提供 scope_id 或 owner_id")
        return f"personal:{owner}"
    if scope == "org":
        return "org"
    raise ValueError(f"未知 scope: {scope!r}")
