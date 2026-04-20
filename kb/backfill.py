"""一次性 backfill：Letta file_contents.text → <out_root>/<slug>/.legacy/<name>[.md]
                 同步写 adapter.db project_files 表索引行

数据来源 (显式, 避免 "跑完发现依赖缺失"):
  - 直连 Letta PostgreSQL; 不走 Letta API (SDK 不暴露 file_contents.text)
  - psycopg2, host=letta-db, dbname=letta, user=letta
  - password 从 env POSTGRES_PASSWORD 读
  - adapter 容器通过 docker-compose env_file: .env 自动注入
  - 只读: 全程 SELECT, 不触碰 Letta 任何写操作

输出路径 (Phase 1 生产 namespace):
  - project scope: <out_root>/<slug>/.legacy/<name>[.md]
  - personal scope: <out_root>/.personal/<user_uuid>/.legacy/<name>[.md]
  - org scope: <out_root>/.org/.legacy/<name>[.md]

命名规则 (修 *.md.md / .pdf 被 endpoint 415 两种陷阱):
  - original_file_name 已以 '.md' 结尾 → 原样落盘 (不加二次后缀)
  - 否则追加 '.md' 后缀 (让 endpoint ext 校验通过; display_name 折叠回原扩展名)

同步索引:
  - 每写成功一份文件, 写一行 adapter.db project_files
  - 幂等: PRIMARY KEY (project_id, scope, scope_id, file_name), ON CONFLICT UPDATE

用法:
    # 单 project
    docker exec teleai-adapter python3 /app/kb/backfill.py --project security-management [--dry-run]

    # 单 personal (需要 user_uuid 作 slug)
    docker exec teleai-adapter python3 /app/kb/backfill.py --scope personal --project <user_uuid>

    # org
    docker exec teleai-adapter python3 /app/kb/backfill.py --scope org --project org

    # 全量 (遍历 adapter.db 所有 project + 所有 user + org)
    docker exec teleai-adapter python3 /app/kb/backfill.py --all
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

import psycopg2

CID_PATTERN = re.compile(r"\(cid:\d+\)")
CID_DIRTY_THRESHOLD = 20

_OFFICE_EXTS = (".xlsx", ".xls", ".csv", ".docx", ".doc", ".pptx", ".ppt", ".pdf")


def _source_name_for_project(slug: str, scope: str = "project") -> str:
    if scope == "project":
        return f"proj-{slug}"
    if scope == "personal":
        return f"personal-{slug}"  # slug = user_uuid
    if scope == "org":
        return "org-shared"
    raise ValueError(f"bad scope: {scope}")


def _count_cid(text: str) -> int:
    return len(CID_PATTERN.findall(text))


def _target_dir(out_root: str, project_slug: str, scope: str) -> str:
    """Phase 1 生产 namespace (无 .poc/ 这层)."""
    if scope == "project":
        return os.path.join(out_root, project_slug)
    if scope == "personal":
        return os.path.join(out_root, ".personal", project_slug)
    if scope == "org":
        return os.path.join(out_root, ".org")
    raise ValueError(f"bad scope: {scope}")


def _legacy_dst_name(original_file_name: str) -> str:
    """已是 .md 原样, 否则加 .md 后缀."""
    safe = os.path.basename(original_file_name or "") or "unnamed"
    if safe.endswith(".md"):
        return safe
    return safe + ".md"


def _display_name(name: str) -> str:
    """foo.docx.md → foo.docx; 其他原样."""
    if name.endswith(".md"):
        base = name[:-3]
        for ext in _OFFICE_EXTS:
            if base.endswith(ext):
                return base
    return name


def _upsert_project_files(
    adapter_db: str,
    project_id: str,
    scope: str,
    scope_id: str,
    entries: list[dict],
) -> None:
    """写一批 project_files 索引行 (幂等)."""
    if not entries:
        return
    conn = sqlite3.connect(adapter_db)
    try:
        for e in entries:
            conn.execute(
                """
                INSERT INTO project_files
                    (project_id, scope, scope_id, file_name, display_name,
                     source, quality, size_bytes, webui_file_id, uploaded_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '')
                ON CONFLICT (project_id, scope, scope_id, file_name) DO UPDATE SET
                    display_name = excluded.display_name,
                    source = excluded.source,
                    quality = excluded.quality,
                    size_bytes = excluded.size_bytes
                """,
                (
                    project_id,
                    scope,
                    scope_id,
                    e["file_name"],
                    e["display_name"],
                    "legacy",
                    e["quality"],
                    e["size_bytes"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def backfill(
    project_slug: str,
    out_root: str,
    scope: str,
    pg_password: str,
    adapter_db: str,
    dry_run: bool,
    force: bool,
) -> dict:
    source_name = _source_name_for_project(project_slug, scope)
    target_dir = _target_dir(out_root, project_slug, scope)
    legacy_dir = os.path.join(target_dir, ".legacy")
    quality_dir = os.path.join(legacy_dir, ".quality")

    print(f"[{scope}/{project_slug}] source={source_name}  legacy_dir={legacy_dir}  dry_run={dry_run}")

    if not dry_run:
        os.makedirs(legacy_dir, exist_ok=True)
        os.makedirs(quality_dir, exist_ok=True)

    conn = psycopg2.connect(
        host="letta-db", dbname="letta", user="letta", password=pg_password
    )
    cur = conn.cursor()
    cur.execute(
        """
        SELECT f.original_file_name, f.file_type,
               LENGTH(COALESCE(fc.text, '')) AS tlen, fc.text
        FROM files f
        JOIN sources s ON s.id = f.source_id
        LEFT JOIN file_contents fc ON fc.file_id = f.id
        WHERE s.name = %s AND NOT f.is_deleted
        ORDER BY f.original_file_name
        """,
        (source_name,),
    )
    rows = cur.fetchall()
    conn.close()

    stats = {"written": 0, "skipped_empty": 0, "skipped_exists": 0, "cid_dirty": 0}
    cid_dirty_names: list[str] = []
    index_entries: list[dict] = []

    for name, ftype, tlen, text in rows:
        if not (name or "").strip():
            continue
        dst_name = _legacy_dst_name(name)
        if tlen == 0 or not text:
            stats["skipped_empty"] += 1
            continue

        dst = os.path.join(legacy_dir, dst_name)
        if os.path.exists(dst) and not force:
            stats["skipped_exists"] += 1
            # 即使 skip 文件, 也确保 index 存在 (幂等补齐)
            index_entries.append({
                "file_name": dst_name,
                "display_name": _display_name(dst_name),
                "quality": "clean",
                "size_bytes": os.path.getsize(dst),
            })
            continue

        cid_count = _count_cid(text) if ftype == "application/pdf" else 0
        is_dirty = cid_count >= CID_DIRTY_THRESHOLD
        quality = "cid_dirty" if is_dirty else "clean"

        if dry_run:
            pass
        else:
            with open(dst, "w", encoding="utf-8") as f:
                f.write(text)
            stats["written"] += 1
            if is_dirty:
                stats["cid_dirty"] += 1
                cid_dirty_names.append(dst_name)
            index_entries.append({
                "file_name": dst_name,
                "display_name": _display_name(dst_name),
                "quality": quality,
                "size_bytes": len(text.encode("utf-8")),
            })

    # 写 cid_dirty 清单
    if not dry_run and cid_dirty_names:
        qf = os.path.join(quality_dir, "cid_dirty.list")
        with open(qf, "w", encoding="utf-8") as f:
            for n in cid_dirty_names:
                f.write(n + "\n")

    # 写 adapter.db 索引
    if not dry_run:
        scope_id = project_slug if scope == "personal" else ""
        _upsert_project_files(adapter_db, project_slug, scope, scope_id, index_entries)

    print(f"  {stats}")
    return stats


def _list_all_targets(adapter_db: str) -> list[tuple[str, str]]:
    """遍历 adapter.db, 返回 [(scope, slug/user_uuid), ...]"""
    targets: list[tuple[str, str]] = []
    conn = sqlite3.connect(adapter_db)
    try:
        # 所有 project
        for row in conn.execute("SELECT project_id FROM projects"):
            targets.append(("project", row[0]))
        # 所有有 personal_folder 的 user
        for row in conn.execute(
            "SELECT user_id FROM user_cache WHERE personal_folder_id IS NOT NULL AND personal_folder_id != ''"
        ):
            targets.append(("personal", row[0]))
        # org
        targets.append(("org", "org"))
    finally:
        conn.close()
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="project slug or user_uuid (for personal scope)")
    ap.add_argument("--scope", default="project", choices=["project", "personal", "org"])
    ap.add_argument("--all", action="store_true", help="遍历 adapter.db 所有 target 批量跑")
    ap.add_argument("--out-root", default="/data/serving/adapter/projects")
    ap.add_argument("--adapter-db", default="/data/serving/adapter/adapter.db")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="覆盖已存在文件")
    args = ap.parse_args()

    pg_password = os.environ.get("POSTGRES_PASSWORD")
    if not pg_password:
        print("ERROR: POSTGRES_PASSWORD env not set", file=sys.stderr)
        sys.exit(1)

    if args.all:
        targets = _list_all_targets(args.adapter_db)
        print(f"全量 backfill: {len(targets)} 个 target")
        total = {"written": 0, "skipped_empty": 0, "skipped_exists": 0, "cid_dirty": 0}
        for scope, slug in targets:
            try:
                s = backfill(slug, args.out_root, scope, pg_password, args.adapter_db, args.dry_run, args.force)
                for k in total:
                    total[k] += s.get(k, 0)
            except Exception as e:
                print(f"  [ERROR] {scope}/{slug}: {e}")
        print(f"\n全量汇总: {total}")
    else:
        if not args.project:
            print("ERROR: --project required unless --all", file=sys.stderr)
            sys.exit(1)
        backfill(args.project, args.out_root, args.scope, pg_password, args.adapter_db, args.dry_run, args.force)


if __name__ == "__main__":
    main()
