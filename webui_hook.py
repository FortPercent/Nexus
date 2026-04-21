"""WebUI 原生 knowledge upload 兜底 hook (Phase 5c).

nginx 反代把 POST /api/v1/knowledge/{kid}/file/add 镜像到这里. 我们:
  - 解析 X-Original-URI 拿 kid
  - 从请求 body JSON 拿 file_id (webui.file.id)
  - 查 knowledge_mirrors 反推 (scope, scope_id, uploader)
  - asyncio.create_task 异步跑 ingest_webui_file, 不阻塞 mirror

前端请求正常经 nginx proxy_pass 到 WebUI 不受影响 (mirror fire-and-forget).

安全: location 用 internal; 外部直接 404. Semaphore(3) 限并发 ingest 防内存炸.
"""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Request

from db import use_db

router = APIRouter()

# 防并发 ingest 打爆内存 (每个 ingest 跑 file_processor / libreoffice / openpyxl / DuckDB)
# libreoffice 已有独立 semaphore=2, 这里是更早的 gate (ingest 本身调度并发数)
_ingest_sema = asyncio.Semaphore(3)


async def _do_ingest(webui_file_id: str, scope: str, scope_id: str, uploader: str):
    async with _ingest_sema:
        try:
            from kb.ingest import ingest_webui_file
            result = await asyncio.to_thread(
                ingest_webui_file, webui_file_id, scope, scope_id, uploader,
            )
            logging.info(f"[webui-hook] ingest {webui_file_id[:8]}: {result}")
        except Exception as e:
            logging.warning(f"[webui-hook] ingest {webui_file_id[:8]} failed: {e}")


@router.post("/internal/webui-hook/knowledge-add")
async def webui_knowledge_add_hook(request: Request):
    """nginx mirror target. nginx internal;  directive 保证外部 404."""
    original_uri = request.headers.get("X-Original-URI", "")
    m = re.match(r"^/api/v1/knowledge/([^/]+)/file/add", original_uri)
    if not m:
        return {"status": "skip_no_kid", "uri": original_uri[:80]}
    kid = m.group(1)

    try:
        body = await request.json()
    except Exception:
        body = {}
    webui_file_id = body.get("file_id") or ""
    if not webui_file_id:
        return {"status": "skip_no_file_id", "kid": kid}

    # 反推 scope: 优先 knowledge_mirrors (adapter 主动镜像的 collection),
    # fallback knowledge_scope_registry (Phase 5b: 用户 Svelte 新建 collection 时 register 的 scope)
    scope = None
    scope_id = ""
    uploader = ""
    with use_db() as db:
        row = db.execute(
            "SELECT scope, scope_id, owner_id, for_user_id "
            "FROM knowledge_mirrors WHERE knowledge_id=? LIMIT 1",
            (kid,),
        ).fetchone()
        if row:
            scope = row["scope"]
            if scope == "project":
                scope_id = row["scope_id"] or ""
            elif scope == "personal":
                scope_id = row["owner_id"] or row["for_user_id"]
            uploader = row["for_user_id"]
        else:
            reg = db.execute(
                "SELECT scope, scope_id, owner_id FROM knowledge_scope_registry WHERE knowledge_id=? LIMIT 1",
                (kid,),
            ).fetchone()
            if reg:
                scope = reg["scope"]
                if scope == "project":
                    scope_id = reg["scope_id"] or ""
                elif scope == "personal":
                    scope_id = reg["owner_id"]
                uploader = reg["owner_id"]
    if not scope:
        logging.info(f"[webui-hook] unknown kid {kid[:8]} (neither mirrors nor registry), skip")
        return {"status": "skip_unknown_kid", "kid": kid}

    asyncio.create_task(_do_ingest(webui_file_id, scope, scope_id, uploader))
    return {"status": "queued", "kid": kid, "scope": scope, "scope_id": scope_id}
