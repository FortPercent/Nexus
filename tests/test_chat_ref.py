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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
