"""Unit test for _load_chat_ref_context (Issue #1 fix).

测标准:
  REF-CHAT-001 (from agent_frontend_issues_repro.md 建议):
    agent 能从被引用的历史 chat 取回独特 token/title.

本地运行:
  cd adapter && python3 -m pytest tests/test_chat_ref.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

os.environ.setdefault("ADAPTER_API_KEY", "test")
os.environ.setdefault("OPENWEBUI_JWT_SECRET", "test")
os.environ.setdefault("OPENWEBUI_ADMIN_EMAIL", "test@example.com")
os.environ.setdefault("OPENWEBUI_ADMIN_PASSWORD", "test")
os.environ.setdefault("VLLM_ENDPOINT", "http://localhost")
os.environ.setdefault("VLLM_API_KEY", "test")

import pytest


@pytest.fixture
def fake_webui_db(tmp_path, monkeypatch):
    """临时 webui.db, 预置一条 chat 记录."""
    db_path = tmp_path / "webui.db"
    c = sqlite3.connect(str(db_path))
    c.execute("""
        CREATE TABLE chat (
            id VARCHAR(255) NOT NULL, user_id VARCHAR(255) NOT NULL,
            title TEXT NOT NULL, share_id VARCHAR(255), archived INTEGER NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            chat JSON, pinned BOOLEAN, meta JSON DEFAULT '{}' NOT NULL, folder_id TEXT
        )
    """)
    chat_json = json.dumps({
        "id": "chat-abc",
        "title": "📂 File Analysis Request",
        "messages": [
            {"role": "user", "content": "第一条用户提问 UNIQ_TOKEN_XY1"},
            {"role": "assistant", "content": "我告诉你这个答案是 UNIQ_TOKEN_AB2"},
            {"role": "user", "content": "追问"},
            {"role": "assistant", "content": "最终回答"},
        ],
    })
    c.execute(
        "INSERT INTO chat (id, user_id, title, archived, created_at, updated_at, chat) "
        "VALUES (?, ?, ?, 0, datetime('now'), datetime('now'), ?)",
        ("chat-abc", "user-uploader", "📂 File Analysis Request", chat_json),
    )
    c.commit()
    c.close()
    # 替 WEBUI_DB_PATH
    import config as _config
    monkeypatch.setattr(_config, "WEBUI_DB_PATH", str(db_path), raising=False)
    return str(db_path)


def test_load_chat_ref_context_happy_path(fake_webui_db):
    """正常读 chat 历史, 返回 4 条消息格式化串."""
    # 因为 config 被 monkey patch 了, main 需要重新 import 确保拿到新 WEBUI_DB_PATH
    # 但 _load_chat_ref_context 内部是运行时 from config import, 每次调都取最新值
    from chat_ref import _load_chat_ref_context
    result = _load_chat_ref_context("chat-abc", "user-uploader")
    assert result, "expected non-empty result"
    # 应包含 4 条消息的 role + content
    assert "[user]" in result
    assert "[assistant]" in result
    assert "UNIQ_TOKEN_XY1" in result
    assert "UNIQ_TOKEN_AB2" in result


def test_load_chat_ref_context_deny_other_user(fake_webui_db):
    """非 owner 请求 → 空串 (deny)."""
    from chat_ref import _load_chat_ref_context
    result = _load_chat_ref_context("chat-abc", "user-attacker")
    assert result == "", f"expected empty deny, got {result[:80]!r}"


def test_load_chat_ref_context_nonexistent_chat(fake_webui_db):
    """不存在的 chat_id → 空串."""
    from chat_ref import _load_chat_ref_context
    result = _load_chat_ref_context("chat-does-not-exist", "user-uploader")
    assert result == ""


def test_load_chat_ref_context_truncate_long(fake_webui_db, tmp_path, monkeypatch):
    """> max_chars 的 chat 应该尾部保留 + 加"历史更早省略"标记."""
    # 另起一个 chat 塞很长内容
    db_path = fake_webui_db
    c = sqlite3.connect(db_path)
    long_msg = "Z" * 10000  # 远超 max_chars=6000
    chat_json = json.dumps({
        "messages": [{"role": "user", "content": long_msg}, {"role": "assistant", "content": "回答"}],
    })
    c.execute(
        "INSERT INTO chat (id, user_id, title, archived, created_at, updated_at, chat) "
        "VALUES (?, ?, ?, 0, datetime('now'), datetime('now'), ?)",
        ("chat-long", "user-uploader", "long chat", chat_json),
    )
    c.commit()
    c.close()

    from chat_ref import _load_chat_ref_context
    result = _load_chat_ref_context("chat-long", "user-uploader", max_chars=200)
    assert "历史更早内容已省略" in result, f"expected truncation marker, got: {result[:200]}"
    # 应保留尾部 "回答"
    assert "回答" in result


# ======================================================================
# _find_kb_file_on_disk — 2026-04-21 regression 覆盖 # ref 读盘 bug
# ======================================================================
# Bug reproduced in prod: tester uploaded teleai-scenario-d-final.txt via Chat [+]
# popup, agent responded "无法分析". Root cause:
#   - Phase 5a 新上传落到 projects/<slug>/<filename> (主目录)
#   - 老 # ref resolver 只查 <base>/.legacy/<display_name>
#   - display_name = "[AI Infra] xxx" 带 scope 前缀, 盘上 filename 无前缀
#   → 双重不匹配, 永远找不到, agent 看空引用
# Fix: _find_kb_file_on_disk 剥前缀 + 查主目录 + .legacy + .md 派生

import pytest


@pytest.fixture
def kb_tmp(tmp_path):
    """构造 <base>/ 和 <base>/.legacy/ 两个目录的临时布局"""
    base = tmp_path / "ai-infra"
    base.mkdir()
    (base / ".legacy").mkdir()
    return str(base)


def test_find_kb_file_main_dir_exact_name(kb_tmp):
    """Phase 5a 新上传场景: 盘文件在主目录, 传入 raw filename"""
    import os
    from chat_ref import _find_kb_file_on_disk
    p = os.path.join(kb_tmp, "foo.pdf")
    open(p, "w").write("x")
    found, base = _find_kb_file_on_disk(kb_tmp, "foo.pdf")
    assert found == p
    assert base == "foo.pdf"


def test_find_kb_file_display_name_with_scope_prefix(kb_tmp):
    """**关键 regression**: mirror.display_name 带 "[AI Infra] " 前缀, 盘文件无前缀.
    这是之前 # ref resolver 的 bug: 用 display_name 找, 盘上是 raw name, 双重错配."""
    import os
    from chat_ref import _find_kb_file_on_disk
    p = os.path.join(kb_tmp, "teleai-scenario-d-final.txt")
    open(p, "w").write("the unique content")
    # tester 场景: knowledge_mirrors.display_name 是带 prefix 的
    found, _ = _find_kb_file_on_disk(kb_tmp, "[AI Infra] teleai-scenario-d-final.txt")
    assert found == p, "剥前缀后应能在主目录找到"


def test_find_kb_file_legacy_dir(kb_tmp):
    """老数据场景: 文件在 .legacy/ 目录"""
    import os
    from chat_ref import _find_kb_file_on_disk
    p = os.path.join(kb_tmp, ".legacy", "old.md")
    open(p, "w").write("legacy content")
    found, _ = _find_kb_file_on_disk(kb_tmp, "old.md")
    assert found == p


def test_find_kb_file_md_derivative(kb_tmp):
    """xlsx 场景: 用户传 foo.xlsx, file_processor 产出 foo.xlsx.md, 盘存 .md.
    # ref 传入的 display_name 可能是 'foo.xlsx' 或 '[AI Infra] foo.xlsx', 应能找到 .md"""
    import os
    from chat_ref import _find_kb_file_on_disk
    p = os.path.join(kb_tmp, "report.xlsx.md")
    open(p, "w").write("xlsx as md")
    found, _ = _find_kb_file_on_disk(kb_tmp, "report.xlsx")
    assert found == p, "应该加 .md 后缀后找到"
    # 前缀版本也应 work
    found2, _ = _find_kb_file_on_disk(kb_tmp, "[AI Infra] report.xlsx")
    assert found2 == p


def test_find_kb_file_main_dir_wins_over_legacy(kb_tmp):
    """同时在主目录和 .legacy/ 都有, 应优先返主目录 (Phase 5a 新数据)."""
    import os
    from chat_ref import _find_kb_file_on_disk
    p_main = os.path.join(kb_tmp, "ambi.md")
    p_legacy = os.path.join(kb_tmp, ".legacy", "ambi.md")
    open(p_main, "w").write("main")
    open(p_legacy, "w").write("legacy")
    found, _ = _find_kb_file_on_disk(kb_tmp, "ambi.md")
    assert found == p_main, "主目录优先"


def test_find_kb_file_not_found(kb_tmp):
    """盘上完全没有, 返 (None, None)."""
    from chat_ref import _find_kb_file_on_disk
    found, base = _find_kb_file_on_disk(kb_tmp, "ghost.pdf")
    assert found is None
    assert base is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
