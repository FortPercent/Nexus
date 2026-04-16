"""同步适配层项目成员到 Open WebUI 模型权限

直接操作 Open WebUI 的私有 SQLite，属于短期方案。
Open WebUI 升级时需要回归测试 access_grant 表结构和行为。

托管边界:
- letta-* 项目模型由适配层全权托管，reconcile 全量覆盖所有 grant
- qwen-no-mem 通用模型的 grant 用 teleai-adapter- 前缀标识，保留手工配置
"""
import sqlite3
import uuid
import time
import logging

from config import WEBUI_DB_PATH

GRANT_SOURCE = "teleai-adapter"


def _get_webui_db():
    """获取 Open WebUI SQLite 连接，设置 5 秒超时防并发锁"""
    conn = sqlite3.connect(WEBUI_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _make_grant_id():
    return f"{GRANT_SOURCE}-{uuid.uuid4()}"


def grant_model_access(user_id: str, model_id: str):
    """给用户授予模型的 read 权限。异常上抛，由调用方决定是否阻塞。"""
    db = _get_webui_db()
    try:
        existing = db.execute(
            "SELECT id FROM access_grant WHERE resource_type='model' AND resource_id=? "
            "AND principal_type='user' AND principal_id=? AND permission='read'",
            (model_id, user_id)
        ).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, created_at) "
                "VALUES (?, 'model', ?, 'user', ?, 'read', ?)",
                (_make_grant_id(), model_id, user_id, int(time.time()))
            )
            db.commit()
    finally:
        db.close()


def revoke_model_access(user_id: str, model_id: str):
    """撤销用户的模型 read 权限。
    letta-* 项目模型由适配层全权托管，删除所有 grant（不限来源）。"""
    db = _get_webui_db()
    try:
        db.execute(
            "DELETE FROM access_grant WHERE resource_type='model' AND resource_id=? "
            "AND principal_type='user' AND principal_id=? AND permission='read'",
            (model_id, user_id)
        )
        db.commit()
    finally:
        db.close()


def revoke_all_model_access(model_id: str):
    """删除某个模型的所有 access_grant。
    用于删除项目时清理，letta-* 模型由适配层全权托管。"""
    db = _get_webui_db()
    try:
        db.execute(
            "DELETE FROM access_grant WHERE resource_type='model' AND resource_id=? "
            "AND permission='read'",
            (model_id,)
        )
        db.commit()
    finally:
        db.close()


def reconcile_common_model(model_id: str = "qwen-no-mem"):
    """全量对账：确保所有 Open WebUI 用户都能看到通用模型。幂等。"""
    db = _get_webui_db()
    try:
        users = db.execute("SELECT id FROM user").fetchall()
        for row in users:
            uid = row["id"]
            existing = db.execute(
                "SELECT id FROM access_grant WHERE resource_type='model' AND resource_id=? "
                "AND principal_type='user' AND principal_id=? AND permission='read'",
                (model_id, uid)
            ).fetchone()
            if not existing:
                db.execute(
                    "INSERT INTO access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, created_at) "
                    "VALUES (?, 'model', ?, 'user', ?, 'read', ?)",
                    (_make_grant_id(), model_id, uid, int(time.time()))
                )
        db.commit()
        logging.info(f"reconcile_common_model: synced {len(users)} users for {model_id}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


MODEL_META = '{"profile_image_url": "/static/favicon.png", "filterIds": ["user_inject"]}'


def _ensure_model_registered(db, model_id: str, name: str):
    """确保模型在 Open WebUI 的 model 表中注册。
    Open WebUI 只对 model 表中存在的模型检查 access_grant，
    未注册的"连接模型"只有 admin 可见。
    自动绑定 user_inject Filter，注入用户身份到请求体。"""
    existing = db.execute("SELECT id, meta FROM model WHERE id = ?", (model_id,)).fetchone()
    if not existing:
        now = int(time.time())
        db.execute(
            "INSERT INTO model (id, user_id, base_model_id, name, meta, params, created_at, updated_at, is_active) "
            "VALUES (?, '', NULL, ?, ?, '{}', ?, ?, 1)",
            (model_id, name, MODEL_META, now, now)
        )
        logging.info(f"_ensure_model_registered: registered {model_id} as '{name}'")
    elif existing["meta"] and '"filterIds"' not in existing["meta"]:
        # 已注册但没绑 Filter，补上
        db.execute("UPDATE model SET meta = ? WHERE id = ?", (MODEL_META, model_id))
        logging.info(f"_ensure_model_registered: added filterIds to {model_id}")


def reconcile_project_model(project_id: str, model_id: str, model_name: str, member_user_ids: list):
    """全量对账：确保项目模型的 access_grant 和成员列表完全一致。
    letta-* 项目模型由适配层全权托管：先删该模型的所有 read grant，再按成员列表重建。幂等。"""
    db = _get_webui_db()
    try:
        # 确保模型在 model 表中注册，否则 access_grant 不生效
        _ensure_model_registered(db, model_id, model_name)

        db.execute(
            "DELETE FROM access_grant WHERE resource_type='model' AND resource_id=? "
            "AND permission='read'",
            (model_id,)
        )
        now = int(time.time())
        for uid in member_user_ids:
            db.execute(
                "INSERT INTO access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, created_at) "
                "VALUES (?, 'model', ?, 'user', ?, 'read', ?)",
                (_make_grant_id(), model_id, uid, now)
            )
        db.commit()
        logging.info(f"reconcile_project_model: synced {len(member_user_ids)} members for {model_id}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def reconcile_all():
    """全量对账入口：同步所有通用模型 + 所有项目模型。"""
    from db import get_db

    # 1. 通用模型
    reconcile_common_model("qwen-no-mem")

    # 2. 所有项目模型
    adapter_db = get_db()
    projects = adapter_db.execute("SELECT project_id, name FROM projects").fetchall()
    for proj in projects:
        pid = proj["project_id"]
        members = adapter_db.execute(
            "SELECT user_id FROM project_members WHERE project_id = ?", (pid,)
        ).fetchall()
        member_ids = [m["user_id"] for m in members]
        reconcile_project_model(pid, f"letta-{pid}", f"Nexus · {proj['name']}", member_ids)
    adapter_db.close()
