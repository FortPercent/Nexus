"""# 引用处理: chat 历史加载 + dedup cache + disk lookup.

Issue #1 + #2 修补提取成独立模块, 方便单元测试 (不拉 FastAPI 依赖).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time


def _find_kb_file_on_disk(base: str, file_name: str) -> tuple[str | None, str | None]:
    """在 <base>/ 和 <base>/.legacy/ 里找 file_name 对应的实际盘文件.

    处理两类语义错配:
      1. display_name 带 "[scope_prefix] " (来自 knowledge_mirrors.display_name),
         盘上无前缀 → 剥 "[XXX] " 前缀
      2. 非 .md 的上传也会有 `.md` 派生文件 (file_processor 转换), 加 candidate

    搜索顺序:
      1. base/ (Phase 5a 新上传主目录)
      2. base/.legacy/ (Phase 1 backfill 老文件)
    任一目录 × 任一候选名命中即返回, 否则返回 (None, None).

    Returns: (full_path, basename) or (None, None)
    """
    raw_name = re.sub(r"^\[[^\]]+\]\s*", "", file_name)
    # 优先级排序: .md 派生 > 原 binary. 避免 pdf binary 被当 UTF-8 读出 PDF header 乱码.
    # 2026-04-21 bug: tester 传 pdf 后 agent 只看到 PDF metadata, 因为没 .md 派生,
    # resolver 随机返 binary. 现加严格优先级 — .md > 原名.
    md_candidates = []
    bin_candidates = []
    for n in [file_name, raw_name]:
        if not n:
            continue
        if n.endswith(".md"):
            md_candidates.append(n)
        else:
            md_candidates.append(n + ".md")
            bin_candidates.append(n)
    ordered = []
    seen = set()
    for c in md_candidates + bin_candidates:
        if c in seen:
            continue
        seen.add(c)
        ordered.append(c)
    for d in (base, os.path.join(base, ".legacy")):
        for cand in ordered:
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p, os.path.basename(p)
    return None, None

# Issue #2: per-(agent_id, ref_id) TTL dedup 防 WebUI # chip 跨消息累积
_ref_injection_cache: dict = {}
_REF_INJECTION_TTL_SEC = 3600


def _should_inject_ref(agent_id: str, ref_id: str) -> bool:
    """True 表示本次应该注入 (首次 or TTL 过期); False 表示最近刚注过, skip."""
    key = (agent_id, ref_id)
    now = time.time()
    ts = _ref_injection_cache.get(key)
    if ts and now - ts < _REF_INJECTION_TTL_SEC:
        return False
    if len(_ref_injection_cache) > 5000:
        expired = [k for k, t in _ref_injection_cache.items() if now - t > _REF_INJECTION_TTL_SEC]
        for k in expired:
            _ref_injection_cache.pop(k, None)
    _ref_injection_cache[key] = now
    return True


def _load_chat_ref_context(
    chat_id: str,
    current_user_id: str,
    max_chars: int = 6000,
    max_messages: int = 10,
) -> str:
    """读 webui.db chat 表, 返回 '[role]\\ncontent' 拼接的历史对话字符串.

    Issue #1 修补: type=chat 引用以前在 adapter 静默丢弃 → 现加载真实内容注入.

    权限: owner == current_user_id 才返内容, 否则空串 (防跨用户窥视).
    截断: 取最近 max_messages 条, 超 max_chars 从尾部保留, 开头加省略标记.
    """
    from config import WEBUI_DB_PATH
    try:
        c = sqlite3.connect(WEBUI_DB_PATH, timeout=5)
        row = c.execute(
            "SELECT user_id, title, chat FROM chat WHERE id = ?",
            (chat_id,),
        ).fetchone()
        c.close()
    except Exception as e:
        logging.warning(f"_load_chat_ref_context read {chat_id[:8]}: {e}")
        return ""
    if not row:
        return ""
    owner, _title, chat_json = row[0], row[1], row[2]
    if owner != current_user_id:
        logging.warning(
            f"# ref chat {chat_id[:8]} owner={owner[:8]} != requester={current_user_id[:8]}, DENY"
        )
        return ""
    try:
        j = json.loads(chat_json or "{}")
        messages = j.get("messages") or []
    except Exception:
        return ""
    if not messages:
        return ""
    recent = messages[-max_messages:]
    parts = []
    for m in recent:
        role = m.get("role", "?")
        raw = m.get("content")
        content = str(raw).strip() if raw else ""
        if not content:
            continue
        parts.append(f"[{role}]\n{content}")
    combined = "\n\n".join(parts)
    if len(combined) > max_chars:
        combined = "(...历史更早内容已省略)\n\n" + combined[-max_chars:]
    return combined
