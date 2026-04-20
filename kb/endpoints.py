"""内部 knowledge base endpoints for Letta custom tools — PoC v0.

路由: POST /internal/project/{project_id}/kb/{list-files,read}
鉴权: Authorization: Bearer $ADAPTER_API_KEY

Phase 1 约束:
  - scope 支持 "project" / "personal" / "org" (personal 需要 user_id, 读对应用户目录)
  - read 只处理 .md / .txt / 无后缀 (on-the-fly 转换 Phase 2 做)
  - 读 <slug>/.legacy/* (生产路径, 已退掉 PoC 的 .poc/ 子 namespace)
  - Phase 2 新上传会进 <slug>/ 主目录, 届时 list/read 合并两层
  - 显示名 = _display_name 规则 (foo.docx.md → foo.docx), 内联, 不 import admin_api
  - Phase 1 起查 project_members ACL (project scope), personal 限 owner
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

from config import ADAPTER_API_KEY

router = APIRouter(prefix="/internal/project/{project_id}/kb", tags=["internal-kb"])

KB_ROOT = "/data/serving/adapter/projects"

_OFFICE_EXTS = (".xlsx", ".xls", ".csv", ".docx", ".doc", ".pptx", ".ppt", ".pdf")


def _display_name(name: str) -> str:
    """跟 admin_api._display_name 一致: foo.docx.md → foo.docx"""
    if name.endswith(".md"):
        base = name[:-3]
        for ext in _OFFICE_EXTS:
            if base.endswith(ext):
                return base
    return name


def _safe_filename(name: str) -> str:
    if not name or "/" in name or "\x00" in name or ".." in name:
        raise HTTPException(400, f"unsafe filename: {name!r}")
    return os.path.basename(name)


def _resolve_base(project_id: str, scope: str, user_id: str) -> str:
    # project_id 也要防路径遍历
    if "/" in project_id or ".." in project_id or project_id.startswith("."):
        raise HTTPException(400, f"unsafe project_id: {project_id!r}")
    if scope == "project":
        return os.path.join(KB_ROOT, project_id)
    if scope == "personal":
        if not user_id or "/" in user_id or ".." in user_id:
            raise HTTPException(400, f"unsafe user_id: {user_id!r}")
        return os.path.join(KB_ROOT, ".personal", user_id)
    if scope == "org":
        return os.path.join(KB_ROOT, ".org")
    raise HTTPException(400, f"bad scope: {scope}")


def _check_project_member(project_id: str, user_id: str) -> None:
    """project scope ACL: user_id 必须是该 project 的成员."""
    from db import use_db
    with use_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM project_members WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        ).fetchone()
    if not row:
        raise HTTPException(403, f"user {user_id[:8]}... is not a member of project {project_id}")


async def _require_api_key(authorization: Optional[str] = Header(None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing API key")
    if authorization[7:] != ADAPTER_API_KEY:
        raise HTTPException(403, "invalid API key")


class ListFilesReq(BaseModel):
    user_id: str
    scope: str = "project"


class ReadReq(BaseModel):
    user_id: str
    file_name: str
    scope: str = "project"
    offset: int = 0
    max_chars: int = 8000


class GrepReq(BaseModel):
    user_id: str
    pattern: str
    scope: str = "project"
    max_hits: int = 20
    ignore_case: bool = True


def _scan_dir(dirpath: str, source: str, cid_dirty: set[str]) -> list[dict]:
    items = []
    if not os.path.isdir(dirpath):
        return items
    for name in sorted(os.listdir(dirpath)):
        if name.startswith("."):
            continue  # skip .legacy, .quality, hidden
        full = os.path.join(dirpath, name)
        if not os.path.isfile(full):
            continue
        st = os.stat(full)
        items.append({
            "name": _display_name(name),
            "raw": name,
            "source": source,
            "quality": "cid_dirty" if name in cid_dirty else "clean",
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    return items


@router.post("/list-files", dependencies=[Depends(_require_api_key)])
async def list_files(project_id: str, req: ListFilesReq):
    base = _resolve_base(project_id, req.scope, req.user_id)
    if req.scope == "project":
        _check_project_member(project_id, req.user_id)

    # Phase 1: <slug>/.legacy/ (存量 backfill). Phase 2 再加 <slug>/ 主目录 (新上传)
    legacy_dir = os.path.join(base, ".legacy")
    quality_file = os.path.join(legacy_dir, ".quality", "cid_dirty.list")
    cid_dirty: set[str] = set()
    if os.path.exists(quality_file):
        try:
            with open(quality_file, encoding="utf-8") as f:
                cid_dirty = {ln.strip() for ln in f if ln.strip()}
        except Exception as e:
            logging.warning(f"kb.list_files: read quality failed: {e}")

    items = _scan_dir(legacy_dir, "legacy", cid_dirty)

    if not items:
        return {"text": f"当前 project「{project_id}」暂无文件。请让用户上传。", "items": items}

    md = f"## Project {project_id} 知识文件（{len(items)} 份）\n\n"
    md += "| 文件名 | 来源 | 质量 | 大小 |\n|---|---|---|---|\n"
    for it in items:
        q_icon = "⚠️" if it["quality"] == "cid_dirty" else "✓"
        size_kb = max(1, it["size"] // 1024)
        md += f"| {it['name']} | {it['source']} | {q_icon} | {size_kb}KB |\n"
    if any(it["quality"] == "cid_dirty" for it in items):
        md += "\n⚠️ `cid_dirty` 文件 pdf 解析质量受限, 可能有乱码, 回答时谨慎引用原文。\n"

    return {"text": md, "items": items}


def _find_file(base: str, name: str) -> Optional[tuple[str, str, str]]:
    """找文件: 返回 (path, source, raw_name) 或 None.

    Phase 1: 在 <base>/.legacy/ 查. Phase 2 扩到 <base>/ 主目录.
    支持两种传法:
      - raw 名（foo.docx.md）→ 直接命中
      - display 名（foo.docx）→ 尝试加 .md 后缀命中
    """
    d = os.path.join(base, ".legacy")
    if not os.path.isdir(d):
        return None
    # 原名直接命中
    p = os.path.join(d, name)
    if os.path.isfile(p):
        return (p, "legacy", name)
    # display name → 尝试加 .md 后缀
    if not name.endswith(".md"):
        p_md = os.path.join(d, name + ".md")
        if os.path.isfile(p_md):
            return (p_md, "legacy", name + ".md")
    return None


import re as _re


@router.post("/grep", dependencies=[Depends(_require_api_key)])
async def grep_files(project_id: str, req: GrepReq):
    """简易全文 grep — 在 <slug>/.legacy/ 所有文件里搜 pattern.

    PoC 范围不追求 rg 的速度 (文件小, 纯 Python 正则 < 10ms/文件), 字面匹配 + 大小写默认忽略.
    返回 max_hits 条命中, 每条 (file, line_no, line_snippet).
    """
    base = _resolve_base(project_id, req.scope, req.user_id)
    if req.scope == "project":
        _check_project_member(project_id, req.user_id)

    legacy_dir = os.path.join(base, ".legacy")
    if not os.path.isdir(legacy_dir):
        return {"text": f"project {project_id} 暂无文件可 grep", "items": []}

    try:
        pattern = _re.compile(req.pattern, _re.IGNORECASE if req.ignore_case else 0)
    except _re.error as e:
        raise HTTPException(400, f"invalid regex pattern: {e}")

    items: list[dict] = []
    files_scanned = 0
    for name in sorted(os.listdir(legacy_dir)):
        if name.startswith("."):
            continue
        if len(items) >= req.max_hits:
            break
        full = os.path.join(legacy_dir, name)
        if not os.path.isfile(full):
            continue
        files_scanned += 1
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if pattern.search(line):
                        snippet = line.rstrip("\n")[:180]
                        items.append({
                            "file": _display_name(name),
                            "raw": name,
                            "line": lineno,
                            "snippet": snippet,
                        })
                        if len(items) >= req.max_hits:
                            break
        except Exception as e:
            logging.warning(f"kb.grep: read {name} failed: {e}")

    if not items:
        return {
            "text": f"在 {files_scanned} 个文件里没找到匹配 `{req.pattern}` 的内容。可以换关键词再试。",
            "items": [],
        }

    md = f"## grep `{req.pattern}` 在 project {project_id} (命中 {len(items)} 条, 扫了 {files_scanned} 个文件)\n\n"
    by_file: dict[str, list[dict]] = {}
    for it in items:
        by_file.setdefault(it["file"], []).append(it)
    for fname, hits in by_file.items():
        md += f"### {fname}\n"
        for h in hits:
            md += f"- L{h['line']}: `{h['snippet']}`\n"
        md += "\n"

    return {"text": md, "items": items, "files_scanned": files_scanned}


@router.post("/read", dependencies=[Depends(_require_api_key)])
async def read_file(project_id: str, req: ReadReq):
    base = _resolve_base(project_id, req.scope, req.user_id)
    if req.scope == "project":
        _check_project_member(project_id, req.user_id)
    name = _safe_filename(req.file_name)

    found = _find_file(base, name)
    if not found:
        raise HTTPException(404, f"file not found: {name}  (scope={req.scope}, project={project_id})")
    path, source, raw = found

    # PoC v0 只读文本格式
    ext = os.path.splitext(path)[1].lower()
    if ext not in ("", ".md", ".txt"):
        raise HTTPException(
            415,
            f"PoC v0 only reads .md/.txt; got {ext}. 存量全已转 md, 新 binary 等 Phase 2",
        )

    # 读全文再切片（PoC 文件都不大, 不用 streaming）
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            full = f.read()
    except Exception as e:
        raise HTTPException(500, f"read failed: {e}")

    total = len(full)
    if req.offset < 0 or req.offset > total:
        raise HTTPException(400, f"offset out of range (total={total})")
    chunk = full[req.offset : req.offset + req.max_chars]
    next_offset = req.offset + len(chunk)
    eof = next_offset >= total

    header = f"=== {raw} (source: {source}, total {total} 字) ===\n"
    if eof:
        footer = f"\n--- 文件结束 ---"
    else:
        footer = f"\n--- 已显示 {req.offset}..{next_offset} / {total} 字, 后文调 read(offset={next_offset}) ---"

    return {
        "text": header + chunk + footer,
        "source": source,
        "raw": raw,
        "total_chars": total,
        "next_offset": next_offset,
        "eof": eof,
    }
