"""一次性 backfill 脚本：Letta file_contents.text → /data/serving/adapter/projects/<slug>/.legacy/

用法:
    python3 kb/backfill.py --project security-management [--dry-run]
    python3 kb/backfill.py --project security-management --out-root /data/serving/adapter/projects

契约：
  - 从 Letta pg 查 (files, file_contents) JOIN sources (name='proj-<slug>')
  - 对每份有文本的文件，写 .legacy/<original_file_name>（不加 .md 后缀，照搬 Letta 存的名字）
  - 检测 pdf 里的 (cid:XXX) 乱码，写入 .legacy/.quality/cid_dirty.list
  - 幂等：同名 skip（带 --force 时覆盖）
  - 只读 Letta，不改任何 Letta 数据
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import psycopg2

CID_PATTERN = re.compile(r"\(cid:\d+\)")
CID_DIRTY_THRESHOLD = 20  # 文件里 >= 20 个 (cid:N) 标记 cid_dirty


def _source_name_for_project(slug: str, scope: str = "project") -> str:
    """adapter 的 project slug → Letta folder name"""
    if scope == "project":
        return f"proj-{slug}"
    if scope == "personal":
        return f"personal-{slug}"  # slug 这里是 user_uuid
    if scope == "org":
        return "org-shared"
    raise ValueError(f"bad scope: {scope}")


def _safe_filename(name: str) -> str:
    """防止 Letta 里的 file_name 带 '/'；adapter 需要的是 basename 落盘"""
    return os.path.basename(name) or "unnamed"


def _count_cid(text: str) -> int:
    return len(CID_PATTERN.findall(text))


def backfill(project_slug: str, out_root: str, scope: str, pg_password: str, dry_run: bool, force: bool):
    source_name = _source_name_for_project(project_slug, scope)
    target_dir = os.path.join(out_root, project_slug if scope == "project" else f".{scope}/{project_slug}")
    legacy_dir = os.path.join(target_dir, ".legacy")
    quality_dir = os.path.join(legacy_dir, ".quality")

    print(f"source:       {source_name}")
    print(f"legacy_dir:   {legacy_dir}")
    print(f"dry_run:      {dry_run}")
    print()

    if not dry_run:
        os.makedirs(legacy_dir, exist_ok=True)
        os.makedirs(quality_dir, exist_ok=True)

    conn = psycopg2.connect(host="letta-db", dbname="letta", user="letta", password=pg_password)
    cur = conn.cursor()
    cur.execute("""
        SELECT f.original_file_name, f.file_type, LENGTH(COALESCE(fc.text, '')) AS tlen, fc.text
        FROM files f
        JOIN sources s ON s.id = f.source_id
        LEFT JOIN file_contents fc ON fc.file_id = f.id
        WHERE s.name = %s AND NOT f.is_deleted
        ORDER BY f.original_file_name
    """, (source_name,))
    rows = cur.fetchall()
    print(f"Letta 记录数: {len(rows)}")

    stats = {"written": 0, "skipped_empty": 0, "skipped_exists": 0, "cid_dirty": 0}
    cid_dirty_names: list[str] = []

    for name, ftype, tlen, text in rows:
        safe = _safe_filename(name or "")
        if not safe:
            print(f"  [skip empty name]")
            continue
        if tlen == 0 or not text:
            print(f"  [skip empty text] {safe} ({ftype})")
            stats["skipped_empty"] += 1
            continue

        dst = os.path.join(legacy_dir, safe)
        if os.path.exists(dst) and not force:
            print(f"  [skip exists]     {safe}")
            stats["skipped_exists"] += 1
            continue

        cid_count = _count_cid(text) if ftype == "application/pdf" else 0
        is_dirty = cid_count >= CID_DIRTY_THRESHOLD
        marker = " [cid_dirty]" if is_dirty else ""

        if dry_run:
            print(f"  [dry-run]         {safe}  len={tlen}  cid={cid_count}{marker}")
        else:
            with open(dst, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"  [wrote]           {safe}  len={tlen}  cid={cid_count}{marker}")
            stats["written"] += 1
            if is_dirty:
                stats["cid_dirty"] += 1
                cid_dirty_names.append(safe)

    conn.close()

    # 写质量标记清单
    if not dry_run and cid_dirty_names:
        qf = os.path.join(quality_dir, "cid_dirty.list")
        with open(qf, "w", encoding="utf-8") as f:
            for n in cid_dirty_names:
                f.write(n + "\n")
        print(f"\ncid_dirty list: {qf}  ({len(cid_dirty_names)} files)")

    print()
    print(f"统计: {stats}")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="project slug, e.g. security-management")
    ap.add_argument("--scope", default="project", choices=["project", "personal", "org"])
    ap.add_argument("--out-root", default="/data/serving/adapter/projects")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="覆盖已存在文件")
    args = ap.parse_args()

    pg_password = os.environ.get("POSTGRES_PASSWORD")
    if not pg_password:
        print("ERROR: POSTGRES_PASSWORD env not set", file=sys.stderr)
        sys.exit(1)

    backfill(args.project, args.out_root, args.scope, pg_password, args.dry_run, args.force)


if __name__ == "__main__":
    main()
