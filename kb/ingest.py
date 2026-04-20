"""Phase 2: 新文件 ingest — 从 WebUI uploads copy binary 到 projects/<slug>/, 生成 .md 派生, 写 project_files, DuckDB ingest.

契约:
  - 显式传 webui_file_id + scope + scope_id + original_name (不自动发现 scope, 避免走 knowledge 描述 parsing 的弱路径)
  - 幂等: 同 webui_file_id 再跑 skip (project_files PRIMARY KEY 保证)
  - 失败不抛: 返回 dict {status, ...}, 让调用方决定
  - 文件落盘: <slug>/<name>  (原 binary) + <slug>/<name>.md 或同名 .md (派生文本)
  - DuckDB ingest: xlsx/csv 时触发 table_ingest, 主键仍用 webui_file_id (Phase 3 统一)
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from typing import Optional

from config import DB_PATH


KB_ROOT = "/data/serving/adapter/projects"
_OFFICE_EXTS = (".xlsx", ".xls", ".csv", ".docx", ".doc", ".pptx", ".ppt", ".pdf")


def _target_dir(scope: str, scope_id: str) -> str:
    if scope == "project":
        return os.path.join(KB_ROOT, scope_id)
    if scope == "personal":
        return os.path.join(KB_ROOT, ".personal", scope_id)
    if scope == "org":
        return os.path.join(KB_ROOT, ".org")
    raise ValueError(f"bad scope: {scope}")


def _display_name(name: str) -> str:
    if name.endswith(".md"):
        base = name[:-3]
        for ext in _OFFICE_EXTS:
            if base.endswith(ext):
                return base
    return name


def _lookup_webui_file(webui_file_id: str) -> Optional[dict]:
    """查 webui.file 表拿 path + filename."""
    webui_db = "/data/open-webui/webui.db"
    if not os.path.exists(webui_db):
        return None
    c = sqlite3.connect(webui_db)
    row = c.execute(
        "SELECT id, filename, path FROM file WHERE id = ?",
        (webui_file_id,),
    ).fetchone()
    c.close()
    if not row:
        return None
    fid, filename, webui_path = row
    # 路径翻译: WebUI 视角 /app/backend/data/... → adapter 视角 /data/open-webui/...
    adapter_path = (webui_path or "").replace("/app/backend/data/", "/data/open-webui/", 1)
    return {"id": fid, "filename": filename, "adapter_path": adapter_path}


def _already_ingested(webui_file_id: str) -> bool:
    c = sqlite3.connect(DB_PATH)
    row = c.execute(
        "SELECT 1 FROM project_files WHERE webui_file_id = ? LIMIT 1",
        (webui_file_id,),
    ).fetchone()
    c.close()
    return row is not None


def _insert_project_files_row(
    project_id: str, scope: str, scope_id: str,
    file_name: str, display_name: str,
    size_bytes: int, webui_file_id: str, uploaded_by: str,
):
    c = sqlite3.connect(DB_PATH)
    try:
        c.execute("""
            INSERT INTO project_files
                (project_id, scope, scope_id, file_name, display_name,
                 source, quality, size_bytes, webui_file_id, uploaded_by)
            VALUES (?, ?, ?, ?, ?, 'current', 'clean', ?, ?, ?)
            ON CONFLICT (project_id, scope, scope_id, file_name) DO UPDATE SET
                display_name = excluded.display_name,
                source = excluded.source,
                size_bytes = excluded.size_bytes,
                webui_file_id = excluded.webui_file_id,
                uploaded_by = COALESCE(NULLIF(excluded.uploaded_by, ''), uploaded_by)
        """, (
            project_id, scope, scope_id, file_name, display_name,
            size_bytes, webui_file_id, uploaded_by,
        ))
        c.commit()
    finally:
        c.close()


def ingest_webui_file(
    webui_file_id: str,
    scope: str,
    scope_id: str,
    uploaded_by: str = "",
    force: bool = False,
) -> dict:
    """核心入口. 把一个 WebUI 上传的文件 ingest 到 adapter 知识层.

    Args:
      webui_file_id: webui.file.id (UUID)
      scope: 'project' / 'personal' / 'org'
      scope_id: project_slug / user_uuid / 'org'
      uploaded_by: 原上传用户 user_id (审计用)
      force: 已 ingest 也重跑

    Returns:
      {"status": str, ...}
    """
    # 1. 幂等 check
    if not force and _already_ingested(webui_file_id):
        return {"status": "already_ingested", "webui_file_id": webui_file_id}

    # 2. 查 webui 文件信息
    wf = _lookup_webui_file(webui_file_id)
    if not wf:
        return {"status": "no_webui_file", "webui_file_id": webui_file_id}
    src = wf["adapter_path"]
    original_name = wf["filename"]
    if not src or not os.path.exists(src):
        return {"status": "binary_missing", "webui_file_id": webui_file_id, "path_tried": src}

    # 3. 落盘 binary
    target_dir = _target_dir(scope, scope_id)
    os.makedirs(target_dir, exist_ok=True)
    dst_bin = os.path.join(target_dir, original_name)
    try:
        shutil.copy2(src, dst_bin)
    except Exception as e:
        return {"status": "copy_failed", "error": str(e)}

    size_bytes = os.path.getsize(dst_bin)

    # 4. 生成 .md 派生 (让 agent 能读)
    #    优先级: file_processor 转 (docx/xlsx/pptx/csv) → Letta pg file_contents.text (pdf 等透传格式) → skip
    dst_md = None
    with open(src, "rb") as f:
        data = f.read()
    try:
        from file_processor import process_upload
        processed = process_upload(original_name, data)
        if processed:
            first_name, first_content, first_mime = processed[0]
            # file_processor 对透传格式 (pdf/txt/md/png) 会 echo binary, 这种情况不算派生
            if first_mime == "text/x-markdown" and first_name != original_name:
                dst_md = os.path.join(target_dir, first_name)
                with open(dst_md, "wb") as f:
                    f.write(first_content)
    except Exception as e:
        logging.warning(f"ingest file_processor failed for {original_name}: {e}")

    # Fallback: file_processor 没产出 md (典型 pdf 透传), 查 Letta pg file_contents.text
    if dst_md is None:
        try:
            import psycopg2
            pg = psycopg2.connect(
                host="letta-db", dbname="letta", user="letta",
                password=os.environ["POSTGRES_PASSWORD"],
            )
            pg_cur = pg.cursor()
            # 按 original_file_name 最精确匹配 (Letta 里 biany 上传的 pdf original_file_name 就是原名)
            pg_cur.execute("""
                SELECT fc.text FROM file_contents fc
                JOIN files f ON f.id = fc.file_id
                WHERE f.original_file_name = %s
                  AND NOT f.is_deleted
                  AND LENGTH(COALESCE(fc.text, '')) > 0
                LIMIT 1
            """, (original_name,))
            row = pg_cur.fetchone()
            pg.close()
            if row and row[0]:
                dst_md = os.path.join(target_dir, original_name + ".md")
                with open(dst_md, "w", encoding="utf-8") as f:
                    f.write(row[0])
                logging.info(f"ingest: used Letta pg file_contents for {original_name}")
        except Exception as e:
            logging.warning(f"ingest pg fallback failed for {original_name}: {e}")

    # 5. 确定 project_id (对 project scope 就是 scope_id, 其他 scope 用 scope_id 做 project_id 字段值)
    project_id_for_row = scope_id if scope == "project" else scope_id

    # 6. 写 project_files 索引
    # display_name: 如果 .md 在则用 _display_name(md_name) 折叠回原扩展名, 否则用 original_name
    disp_name = _display_name(os.path.basename(dst_md)) if dst_md else original_name
    # file_name: 用 .md 文件名 (read 是读 .md; 跟 .legacy/ 那套保持一致)
    file_name = os.path.basename(dst_md) if dst_md else original_name
    try:
        _insert_project_files_row(
            project_id_for_row, scope, scope_id if scope == "personal" else "",
            file_name, disp_name,
            size_bytes, webui_file_id, uploaded_by,
        )
    except Exception as e:
        return {"status": "db_insert_failed", "error": str(e), "binary_written": dst_bin}

    # 7. DuckDB ingest (project + xlsx/csv 才触发)
    duckdb_stats = None
    if scope == "project":
        try:
            from table_ingest import _ext, SUPPORTED_EXTS, _ingest_sync
            if _ext(original_name) in SUPPORTED_EXTS:
                duckdb_stats = _ingest_sync(scope_id, webui_file_id, original_name, data)
        except Exception as e:
            logging.warning(f"DuckDB ingest failed for {original_name}: {e}")

    return {
        "status": "ingested",
        "webui_file_id": webui_file_id,
        "binary": dst_bin,
        "md": dst_md,
        "display_name": disp_name,
        "size_bytes": size_bytes,
        "duckdb": duckdb_stats,
    }
