"""End-to-end test: routing.get_or_create_agent 把 user 的 org_block chain 加进
agent block_ids (Issue #14 Day 5).

不真打 letta SDK (避免依赖 spike letta server 可用性), 直接 mock letta.agents.create
+ get_or_create_personal_human_block, 验 block_ids 参数包含 chain.

T1  user 挂 org 且 org 绑了 block → block_ids = [human, org_block]
T2  user 没挂任何 org → block_ids = [human] (chain 空, 行为同改造前)
T3  3 层 org 树, user 挂 leaf, 上层都绑了 block → block_ids 含整链 root → leaf
T4  org_tree.get_user_org_block_chain_sync 抛异常 → fallback 空 chain, 不阻塞 agent 创建
"""
import os
import sys
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

_tmp = tempfile.mkdtemp(prefix="routing-org-")
os.environ.setdefault("ADAPTER_API_KEY", "test")
os.environ.setdefault("OPENWEBUI_JWT_SECRET", "test")
os.environ.setdefault("OPENWEBUI_ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("OPENWEBUI_ADMIN_PASSWORD", "test")
os.environ.setdefault("VLLM_ENDPOINT", "http://localhost")
os.environ.setdefault("VLLM_API_KEY", "test")
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "adapter.db"))
os.environ.setdefault("WEBUI_DB_PATH", os.path.join(_tmp, "webui.db"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for m in ["db", "config", "routing", "org_tree", "letta_sql_tools",
              "kb", "kb.letta_tools", "kb.endpoints"]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    yield


@pytest.fixture
def db_with_data(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import db as db_mod
    db_mod.init_db()

    c = sqlite3.connect(str(db_path))
    c.execute("INSERT INTO user_cache (user_id, name, email) VALUES (?, ?, ?)",
              ("u1", "User 1", "u1@x.com"))
    c.execute("INSERT INTO projects (project_id, name, created_by) VALUES (?, ?, ?)",
              ("p1", "P1", "u1"))
    c.execute("INSERT INTO project_members (user_id, project_id, role) VALUES (?, ?, ?)",
              ("u1", "p1", "admin"))
    c.commit(); c.close()
    return str(db_path)


def _add_org(db_path, oid, parent, code, block_id=None):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO organizations (id, parent_id, name, code, letta_block_id) VALUES (?, ?, ?, ?, ?)",
              (oid, parent, code, code, block_id))
    c.commit(); c.close()


def _add_org_member(db_path, oid, uid):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'member')",
              (oid, uid))
    c.commit(); c.close()


def _patched_routing(monkeypatch):
    """
    Patch routing.py 的所有外部依赖:
      - letta.agents.create / retrieve (假 letta SDK)
      - get_or_create_personal_human_block (返一个固定 block id)
      - tool getters (返空)
      - 跳过 user_agent_map 里的现有 agent (强制走创建分支)
    """
    import routing
    fake_agent = MagicMock()
    fake_agent.id = "agent-fake"
    fake_agent.metadata = {"owner": "u1", "project": "p1"}
    fake_create = MagicMock(return_value=fake_agent)
    monkeypatch.setattr(routing.letta.agents, "create", fake_create)
    monkeypatch.setattr(routing, "get_or_create_personal_human_block",
                        lambda uid: "block-human-of-" + uid)
    monkeypatch.setattr(routing, "_get_suggest_tool_id", lambda: "tool-suggest")
    monkeypatch.setattr(routing, "_get_suggest_todo_tool_id", lambda: "tool-todo")
    return fake_create


def test_t1_user_in_org_with_block(db_with_data, monkeypatch):
    _add_org(db_with_data, "bureau", None, "bureau", block_id="block-bureau")
    _add_org_member(db_with_data, "bureau", "u1")

    fake_create = _patched_routing(monkeypatch)
    import routing
    routing.get_or_create_agent("u1", "p1")
    args, kwargs = fake_create.call_args
    block_ids = kwargs["block_ids"]
    assert block_ids[0] == "block-human-of-u1"
    assert "block-bureau" in block_ids


def test_t2_user_no_org(db_with_data, monkeypatch):
    fake_create = _patched_routing(monkeypatch)
    import routing
    routing.get_or_create_agent("u1", "p1")
    _, kwargs = fake_create.call_args
    assert kwargs["block_ids"] == ["block-human-of-u1"]


def test_t3_three_layer_chain_order(db_with_data, monkeypatch):
    """root (block-r) → bureau (block-b) → division (block-d). user u1 挂 division.
    block_ids should be [human, block-r, block-b, block-d] (root → leaf)."""
    _add_org(db_with_data, "root", None, "root", block_id="block-r")
    _add_org(db_with_data, "bureau", "root", "bureau", block_id="block-b")
    _add_org(db_with_data, "division", "bureau", "division", block_id="block-d")
    _add_org_member(db_with_data, "division", "u1")

    fake_create = _patched_routing(monkeypatch)
    import routing
    routing.get_or_create_agent("u1", "p1")
    _, kwargs = fake_create.call_args
    bs = kwargs["block_ids"]
    assert bs[0] == "block-human-of-u1"
    assert bs[1:] == ["block-r", "block-b", "block-d"]


def test_t4_chain_load_failure_does_not_block_agent_creation(db_with_data, monkeypatch):
    """get_user_org_block_chain_sync 抛异常 → fallback 空 chain, agent 仍创建."""
    fake_create = _patched_routing(monkeypatch)
    import routing
    import org_tree
    monkeypatch.setattr(org_tree, "get_user_org_block_chain_sync",
                        lambda uid: (_ for _ in ()).throw(RuntimeError("boom")))
    routing.get_or_create_agent("u1", "p1")
    _, kwargs = fake_create.call_args
    assert kwargs["block_ids"] == ["block-human-of-u1"]
