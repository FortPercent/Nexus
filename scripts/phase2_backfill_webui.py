#!/usr/bin/env python3
"""Phase 2: 一次性补 biany 16 份 asset 文件 (以及其他 webui-only 文件) 的 ingest.

问题: WebUI 原生 knowledge 上传 (POST /api/v1/files/ + POST /{kid}/file/add) 不走 adapter,
所以 adapter 从没把这些文件 ingest 到 /data/serving/adapter/projects/ + DuckDB.

做法: 扫 webui.file 表, 对每条 ID:
  - 如果 project_files 里已有该 webui_file_id → skip (幂等)
  - 否则根据用户显式传入的 --project / --scope 做 ingest

用法:
    # 干跑看哪些会被 ingest
    docker exec teleai-adapter python3 /app/scripts/phase2_backfill_webui.py \
        --filter-user f1dfb0ed-0c2b-4337-922a-cbc86859dfde \
        --scope project --scope-id asset-management --dry-run

    # 真跑
    docker exec teleai-adapter python3 /app/scripts/phase2_backfill_webui.py \
        --filter-user f1dfb0ed-0c2b-4337-922a-cbc86859dfde \
        --scope project --scope-id asset-management
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, "/app")


def _list_webui_files_for_user(user_id: str, filename_pattern: str = "") -> list[dict]:
    c = sqlite3.connect("/data/open-webui/webui.db")
    q = "SELECT id, filename, path FROM file WHERE user_id = ?"
    args = [user_id]
    if filename_pattern:
        q += " AND filename LIKE ?"
        args.append(f"%{filename_pattern}%")
    rows = c.execute(q, args).fetchall()
    c.close()
    return [{"id": r[0], "filename": r[1], "path": r[2]} for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter-user", required=True, help="webui user_id 过滤, 只 ingest 这个用户上传的")
    ap.add_argument("--scope", required=True, choices=["project", "personal", "org"])
    ap.add_argument("--scope-id", required=True, help="project slug / user_uuid / 'org'")
    ap.add_argument("--filter-filename", default="", help="文件名 LIKE 过滤, 防误 ingest 个人笔记")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="已 ingested 强制重 ingest")
    args = ap.parse_args()

    from kb.ingest import ingest_webui_file, _already_ingested

    files = _list_webui_files_for_user(args.filter_user, args.filter_filename)
    print(f"candidate webui files: {len(files)}")

    stats = {"would_ingest": 0, "ingested": 0, "skipped_existing": 0, "error": 0, "binary_missing": 0}

    for f in files:
        name = f["filename"] or "(unnamed)"
        fid = f["id"]
        if not args.force and _already_ingested(fid):
            stats["skipped_existing"] += 1
            print(f"  [skip existing] {name[:60]:60s}  {fid[:8]}")
            continue
        if args.dry_run:
            stats["would_ingest"] += 1
            print(f"  [would_ingest ] {name[:60]:60s}  {fid[:8]}")
            continue
        r = ingest_webui_file(fid, args.scope, args.scope_id, uploaded_by=args.filter_user, force=args.force)
        status = r.get("status")
        stats[status] = stats.get(status, 0) + 1
        summary = ""
        if status == "ingested":
            summary = f"→ {os.path.basename(r.get('md') or r.get('binary', ''))}  {r.get('size_bytes', 0)//1024}KB"
            if r.get("duckdb"):
                summary += f"  duckdb:{r['duckdb'].get('tables', [])}"
        elif status == "binary_missing":
            stats["binary_missing"] += 1
        print(f"  [{status:14s}] {name[:60]:60s}  {summary}")

    print(f"\nTotal: {stats}")


if __name__ == "__main__":
    main()
