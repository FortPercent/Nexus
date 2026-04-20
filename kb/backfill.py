"""一次性 backfill：Letta file_contents.text → <out_root>/<slug>/.poc/.legacy/<name>.md

数据来源 (显式, 避免 "跑完发现依赖缺失"):
  - 直连 Letta PostgreSQL; 不走 Letta API (SDK 不暴露 file_contents.text)
  - psycopg2, host=letta-db, dbname=letta, user=letta
  - password 从 env POSTGRES_PASSWORD 读, adapter 容器通过 docker-compose env_file: .env 注入
  - 只读: 全程 SELECT, 不触碰 Letta 任何写操作

Namespace 隔离:
  - 默认落 <out_root>/<slug>/.poc/.legacy/<name>, 不污染 Phase 1 的生产 slug 目录
  - 清理: rm -rf <out_root>/<slug>/.poc/

命名规则 (修 *.md.md 陷阱 + endpoint ext 校验):
  - original_file_name 已以 '.md' 结尾 → 原样落盘
  - 否则追加 '.md' 后缀 (让 endpoint 文本 ext 校验通过, _display_name 自然折叠回原扩展名)

用法:
    docker exec teleai-adapter python3 /app/kb/backfill.py --project security-management [--dry-run]
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


def _namespaced_target(out_root: str, project_slug: str, scope: str) -> str:
    """生产 slug 的子 namespace: <out_root>/<slug>/.poc/ (隔离 PoC, 不踩 Phase 1 生产目录)"""
    if scope == "project":
        return os.path.join(out_root, project_slug, ".poc")
    return os.path.join(out_root, f".{scope}", project_slug, ".poc")


def _legacy_dst_name(original_file_name: str) -> str:
    """统一落盘名: 已是 .md 原样, 否则追加 .md (修 *.md.md 陷阱)"""
    safe = os.path.basename(original_file_name or "") or "unnamed"
    if safe.endswith(".md"):
        return safe
    return safe + ".md"


def backfill(project_slug: str, out_root: str, scope: str, pg_password: str, dry_run: bool, force: bool):
    source_name = _source_name_for_project(project_slug, scope)
    target_dir = _namespaced_target(out_root, project_slug, scope)
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
        if not (name or "").strip():
            print(f"  [skip empty name]")
            continue
        dst_name = _legacy_dst_name(name)  # 保证以 .md 结尾
        if tlen == 0 or not text:
            print(f"  [skip empty text] {dst_name} ({ftype})")
            stats["skipped_empty"] += 1
            continue

        dst = os.path.join(legacy_dir, dst_name)
        safe = dst_name  # 保留 safe 变量名兼容下方 cid_dirty 记录
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
