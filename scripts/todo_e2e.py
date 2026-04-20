"""项目 TODO 面板 P0 端到端测试。
覆盖：CRUD、状态流转、approval_mode 三档、权限拒绝、my-todos、撤回。

容器内跑：
  docker exec teleai-adapter python /app/scripts/todo_e2e.py
"""
import os
import sys
import httpx
import sqlite3
from datetime import datetime, timedelta, timezone

import jwt as pyjwt

BASE = os.getenv("ADAPTER_URL", "http://localhost:8000/admin/api")
SECRET = os.getenv("JWT_SECRET") or os.getenv("OPENWEBUI_JWT_SECRET") or "6WYGSa8e7EBsSeG3"
DB_PATH = os.getenv("DB_PATH", "/data/serving/adapter/adapter.db")

# 已有的成员
WUXN5 = "ce1d405b-0b5c-4faf-8864-010e2611b900"     # org admin + 项目 admin (ai-infra)
BIANY4 = "f1dfb0ed-0c2b-4337-922a-cbc86859dfde"    # ai-infra 成员（非 admin）
QIRUOLING = "3cfb6688-9362-4afb-963e-e8b4cc4474f3"  # ai-infra 成员（非 admin，测"非创建者非 admin"场景）
PROJECT = "ai-infra"


def mk_token(uid):
    return pyjwt.encode({"id": uid, "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, SECRET, algorithm="HS256")


def H(uid):
    return {"Authorization": f"Bearer {mk_token(uid)}", "Content-Type": "application/json"}


fails = []
passes = 0


def T(name, fn):
    global passes
    try:
        fn()
        print(f"[PASS] {name}")
        passes += 1
    except AssertionError as e:
        print(f"[FAIL] {name} — {e}")
        fails.append(name)
    except Exception as e:
        print(f"[FAIL] {name} — {type(e).__name__}: {e}")
        fails.append(name)


def _set_mode(mode):
    r = httpx.put(f"{BASE}/project/{PROJECT}/settings/todo", headers=H(WUXN5), json={"approval_mode": mode}, timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["approval_mode"] == mode


def _cleanup_test_todos():
    c = sqlite3.connect(DB_PATH)
    c.execute("DELETE FROM project_todos WHERE title LIKE 'TEST_%' OR title LIKE 'test_%'")
    c.commit()
    c.close()


# ===== tests =====

def t_create_manual_ai_only_mode_goes_open():
    _set_mode("ai_only")
    # biany4 (非 admin) 手动建：应该直接 open
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(BIANY4),
                   json={"title": "TEST_member_manual_open", "priority": "high"}, timeout=10)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "open", f"expected open, got {d['status']}"
    assert d["priority"] == "high"
    assert d["source"] == "manual"
    return d["id"]


def t_admin_manual_always_open():
    # strict 模式下 admin 手动仍直接 open
    _set_mode("strict")
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(WUXN5),
                   json={"title": "TEST_admin_manual"}, timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "open"


def t_strict_mode_member_goes_awaiting_admin():
    _set_mode("strict")
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(BIANY4),
                   json={"title": "TEST_member_strict"}, timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "awaiting_admin", f"expected awaiting_admin, got {d['status']}"
    return d["id"]


def t_approve_awaiting_admin():
    tid = t_strict_mode_member_goes_awaiting_admin()
    # member 尝试 approve，应 403
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos/{tid}/approve", headers=H(BIANY4), timeout=10)
    assert r.status_code == 403, f"member approve: {r.status_code}"
    # admin approve 成功
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos/{tid}/approve", headers=H(WUXN5), timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "open"


def t_reject_awaiting_admin_with_reason():
    tid = t_strict_mode_member_goes_awaiting_admin()
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos/{tid}/reject", headers=H(WUXN5),
                   json={"reason": "范围不对"}, timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "cancelled"
    assert d["cancel_reason"] == "范围不对"


def t_confirm_awaiting_user_to_open_ai_only():
    _set_mode("ai_only")
    # 直接用 SQL 造一个 AI 建议
    c = sqlite3.connect(DB_PATH)
    c.execute("INSERT INTO project_todos (project_id, title, status, source, created_by) VALUES (?,?,?,?,?)",
              (PROJECT, "TEST_ai_suggest_confirm", "awaiting_user", "ai", BIANY4))
    tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit(); c.close()

    # 非创建者确认应 403
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos/{tid}/confirm", headers=H(QIRUOLING), timeout=10)
    assert r.status_code == 403

    # 创建者确认：ai_only 模式下直接 open
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos/{tid}/confirm", headers=H(BIANY4), timeout=10)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"


def t_confirm_awaiting_user_to_awaiting_admin_strict():
    _set_mode("strict")
    c = sqlite3.connect(DB_PATH)
    c.execute("INSERT INTO project_todos (project_id, title, status, source, created_by) VALUES (?,?,?,?,?)",
              (PROJECT, "TEST_ai_suggest_strict", "awaiting_user", "ai", BIANY4))
    tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit(); c.close()
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos/{tid}/confirm", headers=H(BIANY4), timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_admin"


def t_member_workflow_status_change():
    _set_mode("ai_only")
    # 自己建一个 open 的
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(BIANY4),
                   json={"title": "TEST_workflow"}, timeout=10)
    tid = r.json()["id"]
    # 创建者可以 open → in_progress
    r = httpx.put(f"{BASE}/project/{PROJECT}/todos/{tid}", headers=H(BIANY4),
                  json={"status": "in_progress"}, timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"
    # → done
    r = httpx.put(f"{BASE}/project/{PROJECT}/todos/{tid}", headers=H(BIANY4),
                  json={"status": "done"}, timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    assert r.json()["done_by"] == BIANY4


def t_member_cannot_change_others_status():
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(BIANY4),
                   json={"title": "TEST_owned_by_biany"}, timeout=10)
    tid = r.json()["id"]
    # liuyr17 试图改状态应 403
    r = httpx.put(f"{BASE}/project/{PROJECT}/todos/{tid}", headers=H(QIRUOLING),
                  json={"status": "in_progress"}, timeout=10)
    assert r.status_code == 403


def t_admin_can_change_anyones_status():
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(BIANY4),
                   json={"title": "TEST_admin_override"}, timeout=10)
    tid = r.json()["id"]
    r = httpx.put(f"{BASE}/project/{PROJECT}/todos/{tid}", headers=H(WUXN5),
                  json={"status": "done"}, timeout=10)
    assert r.status_code == 200


def t_member_assigned_can_move_status():
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(WUXN5),
                   json={"title": "TEST_assigned", "assigned_to": QIRUOLING}, timeout=10)
    tid = r.json()["id"]
    # liuyr17 作为被指派者应该能改
    r = httpx.put(f"{BASE}/project/{PROJECT}/todos/{tid}", headers=H(QIRUOLING),
                  json={"status": "in_progress"}, timeout=10)
    assert r.status_code == 200, r.text


def t_delete_awaiting_by_creator():
    _set_mode("strict")
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(BIANY4),
                   json={"title": "TEST_delete_awaiting"}, timeout=10)
    tid = r.json()["id"]
    assert r.json()["status"] == "awaiting_admin"
    # 创建者可以删（相当于撤回）
    r = httpx.delete(f"{BASE}/project/{PROJECT}/todos/{tid}", headers=H(BIANY4), timeout=10)
    assert r.status_code == 200


def t_member_cannot_delete_open():
    _set_mode("ai_only")
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(BIANY4),
                   json={"title": "TEST_no_delete_open"}, timeout=10)
    tid = r.json()["id"]
    r = httpx.delete(f"{BASE}/project/{PROJECT}/todos/{tid}", headers=H(BIANY4), timeout=10)
    assert r.status_code == 403


def t_list_ordering():
    """列表按状态 + 优先级 + created_at 排序"""
    r = httpx.get(f"{BASE}/project/{PROJECT}/todos", headers=H(WUXN5), timeout=10)
    assert r.status_code == 200
    todos = r.json()
    statuses = [t["status"] for t in todos]
    # awaiting_user / awaiting_admin 应在最前
    if statuses:
        priorities = {s: i for i, s in enumerate(
            ["awaiting_user", "awaiting_admin", "in_progress", "open", "done", "cancelled"])}
        vals = [priorities.get(s, 99) for s in statuses]
        assert vals == sorted(vals), f"order wrong: {statuses[:10]}"


def t_my_todos_across_projects():
    r = httpx.get(f"{BASE}/my-todos", headers=H(BIANY4), timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    # 应该包含 biany4 创建或被指派的
    for t in data:
        assert t["created_by"] == BIANY4 or t["assigned_to"] == BIANY4
        assert t["status"] != "cancelled"
        assert "project_name" in t


def t_approval_mode_open_direct():
    _set_mode("open")
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(BIANY4),
                   json={"title": "TEST_mode_open"}, timeout=10)
    assert r.json()["status"] == "open"


def t_non_admin_cannot_change_mode():
    r = httpx.put(f"{BASE}/project/{PROJECT}/settings/todo", headers=H(BIANY4),
                  json={"approval_mode": "strict"}, timeout=10)
    assert r.status_code == 403


def t_invalid_inputs():
    # 空 title
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(BIANY4),
                   json={"title": "  "}, timeout=10)
    assert r.status_code == 400
    # 非法 priority
    r = httpx.post(f"{BASE}/project/{PROJECT}/todos", headers=H(BIANY4),
                   json={"title": "TEST_x", "priority": "extreme"}, timeout=10)
    assert r.status_code == 400
    # 非法 mode
    r = httpx.put(f"{BASE}/project/{PROJECT}/settings/todo", headers=H(WUXN5),
                  json={"approval_mode": "bogus"}, timeout=10)
    assert r.status_code == 400


if __name__ == "__main__":
    _cleanup_test_todos()
    print("── P0 TODO 端到端测试 ──")
    T("create manual (ai_only) → open", t_create_manual_ai_only_mode_goes_open)
    T("admin manual → open（即使 strict）", t_admin_manual_always_open)
    T("strict 模式 member 手动 → awaiting_admin", t_strict_mode_member_goes_awaiting_admin)
    T("admin approve awaiting_admin（member 不能）", t_approve_awaiting_admin)
    T("reject 带 reason", t_reject_awaiting_admin_with_reason)
    T("awaiting_user confirm → open (ai_only)", t_confirm_awaiting_user_to_open_ai_only)
    T("awaiting_user confirm → awaiting_admin (strict)", t_confirm_awaiting_user_to_awaiting_admin_strict)
    T("member 可流转自己创建的 open→in_progress→done", t_member_workflow_status_change)
    T("member 不能改别人的状态", t_member_cannot_change_others_status)
    T("admin 可改任何人的状态", t_admin_can_change_anyones_status)
    T("被指派者可流转状态", t_member_assigned_can_move_status)
    T("删自己 awaiting_* 等于撤回", t_delete_awaiting_by_creator)
    T("member 不能删 open", t_member_cannot_delete_open)
    T("列表排序（待处理优先）", t_list_ordering)
    T("my-todos 跨项目筛选", t_my_todos_across_projects)
    T("approval_mode=open 直接 open", t_approval_mode_open_direct)
    T("member 不能改 approval_mode", t_non_admin_cannot_change_mode)
    T("非法入参 → 400", t_invalid_inputs)
    _cleanup_test_todos()
    print(f"\n==== {passes}/{passes + len(fails)} PASS ====")
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        sys.exit(1)
