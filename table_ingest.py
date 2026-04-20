"""结构化数据入库 (L2)。

xlsx/xls/csv 上传时由 admin_api._process_and_upload 调用，best-effort 写入
per-project DuckDB。失败只 warning，不阻断文件上传。

DuckDB 文件布局：/data/serving/adapter/duckdb/{project_id}.duckdb
表命名：{file_stem}__{sheet_name}（中文保留，非法字符转 _）
元数据：__nexus_meta (table_name, source_file, source_sheet, row_count,
                       ingested_at, letta_file_id)
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from pathlib import Path
from typing import Optional

DUCKDB_ROOT = Path(os.getenv("DUCKDB_ROOT", "/data/serving/adapter/duckdb"))
MAX_ROWS_PER_TABLE = 100_000  # 与 file_processor 的 5000 行保持独立：SQL 侧可容纳更多
SUPPORTED_EXTS = {"xlsx", "csv"}


def _duckdb_path(project_id: str) -> Path:
    # project_id 通常是 uuid，安全；仍然做 basename 防路径穿越
    safe = os.path.basename(project_id)
    return DUCKDB_ROOT / f"{safe}.duckdb"


_NAME_CLEAN_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


def _sanitize_name(raw: str) -> str:
    """表/列名清洗：保留字母数字下划线中文，其他替换为下划线；收缩连续下划线；去首尾 _"""
    s = _NAME_CLEAN_RE.sub("_", raw or "").strip("_")
    s = re.sub(r"_+", "_", s)
    if not s:
        s = "unnamed"
    if s[0].isdigit():
        s = "_" + s
    return s


def _dedup(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 1
            out.append(n)
    return out


def _ext(filename: str) -> str:
    return (filename.rsplit(".", 1)[-1] or "").lower() if "." in filename else ""


def _quoted(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _ensure_meta(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS __nexus_meta (
            table_name VARCHAR PRIMARY KEY,
            source_file VARCHAR,
            source_sheet VARCHAR,
            row_count BIGINT,
            ingested_at TIMESTAMP,
            letta_file_id VARCHAR
        )
        """
    )
    # Phase 3: 加 webui_file_id 列 (幂等). 老行 letta_file_id 保留过渡,
    # 新 ingest 同时写两个, drop 支持双 key. Phase 4 再谈清老列.
    try:
        con.execute("ALTER TABLE __nexus_meta ADD COLUMN webui_file_id VARCHAR")
    except Exception:
        pass


def _read_sheets(original_name: str, data: bytes) -> list[tuple[str, "pandas.DataFrame"]]:
    """返回 [(sheet_name, df), ...]。csv 固定 sheet_name='sheet1'。"""
    import pandas as pd
    ext = _ext(original_name)
    if ext == "xlsx":
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, engine="openpyxl")
        return [(str(name), df) for name, df in sheets.items() if not df.empty]
    if ext == "csv":
        text = None
        for enc in ("utf-8", "gbk", "utf-16"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = data.decode("utf-8", errors="replace")
        df = pd.read_csv(io.StringIO(text))
        if df.empty:
            return []
        return [("sheet1", df)]
    return []


def _write_table(con, table_name: str, df) -> int:
    """写一张表，返回写入行数。已存在则 DROP 重建。"""
    # 列名清洗 + 去重
    clean_cols = _dedup([_sanitize_name(str(c)) for c in df.columns])
    df = df.copy()
    df.columns = clean_cols

    # 行数上限
    truncated = False
    if len(df) > MAX_ROWS_PER_TABLE:
        df = df.head(MAX_ROWS_PER_TABLE)
        truncated = True

    con.register("__ingest_df", df)
    try:
        con.execute(f"DROP TABLE IF EXISTS {_quoted(table_name)}")
        con.execute(f"CREATE TABLE {_quoted(table_name)} AS SELECT * FROM __ingest_df")
    finally:
        con.unregister("__ingest_df")

    if truncated:
        logging.warning(f"table_ingest: {table_name} truncated to {MAX_ROWS_PER_TABLE} rows")
    return len(df)


def _ingest_sync(project_id: str, letta_file_id: str, original_name: str, data: bytes) -> Optional[dict]:
    """同步实现。外层 async 包一层 to_thread。"""
    import duckdb
    ext = _ext(original_name)
    if ext not in SUPPORTED_EXTS:
        return None

    try:
        sheets = _read_sheets(original_name, data)
    except Exception as e:
        logging.warning(f"table_ingest: read failed for {original_name}: {e}")
        return None

    if not sheets:
        return None

    DUCKDB_ROOT.mkdir(parents=True, exist_ok=True)
    db_path = _duckdb_path(project_id)

    file_stem = _sanitize_name(original_name.rsplit(".", 1)[0])

    ingested: list[dict] = []
    is_first_ingest = False  # True → 是该 project 第一次有表, 触发 F1 attach hook
    try:
        con = duckdb.connect(str(db_path))
        try:
            _ensure_meta(con)
            # 写入前查是否空; 空则本次为首次 ingest (后续 F1 hook 判据)
            existing_count = con.execute("SELECT COUNT(*) FROM __nexus_meta").fetchone()[0]
            is_first_ingest = existing_count == 0
            for sheet_name, df in sheets:
                safe_sheet = _sanitize_name(sheet_name)
                table_name = f"{file_stem}__{safe_sheet}"

                # 同名不同文件的冲突：极少见（project 内文件名唯一），保守加后缀
                existing = con.execute(
                    "SELECT letta_file_id FROM __nexus_meta WHERE table_name = ?",
                    [table_name],
                ).fetchone()
                if existing and existing[0] != letta_file_id:
                    idx = 2
                    while con.execute(
                        "SELECT 1 FROM __nexus_meta WHERE table_name = ?",
                        [f"{table_name}_{idx}"],
                    ).fetchone():
                        idx += 1
                    table_name = f"{table_name}_{idx}"

                row_count = _write_table(con, table_name, df)

                con.execute(
                    """
                    INSERT INTO __nexus_meta
                        (table_name, source_file, source_sheet, row_count, ingested_at, letta_file_id)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                    ON CONFLICT (table_name) DO UPDATE SET
                        source_file = EXCLUDED.source_file,
                        source_sheet = EXCLUDED.source_sheet,
                        row_count = EXCLUDED.row_count,
                        ingested_at = EXCLUDED.ingested_at,
                        letta_file_id = EXCLUDED.letta_file_id
                    """,
                    [table_name, original_name, sheet_name, row_count, letta_file_id],
                )
                ingested.append({"table": table_name, "rows": row_count})
        finally:
            con.close()
    except Exception as e:
        logging.warning(f"table_ingest: write failed for {original_name} (project={project_id}): {e}")
        return None

    logging.info(
        f"table_ingest: {original_name} → {len(ingested)} table(s) in project {project_id}; "
        f"rows={sum(i['rows'] for i in ingested)}; first_ingest={is_first_ingest}"
    )
    return {"tables": ingested, "is_first_ingest": is_first_ingest}


async def ingest_if_structured(
    project_id: str,
    letta_file_id: str,
    original_name: str,
    data: bytes,
) -> Optional[dict]:
    """主入口。非结构化或失败返回 None，best-effort 契约。"""
    if _ext(original_name) not in SUPPORTED_EXTS:
        return None
    return await asyncio.to_thread(_ingest_sync, project_id, letta_file_id, original_name, data)


def _drop_sync(project_id: str, letta_file_id: str) -> int:
    import duckdb
    db_path = _duckdb_path(project_id)
    if not db_path.exists():
        return 0
    try:
        con = duckdb.connect(str(db_path))
        try:
            _ensure_meta(con)
            rows = con.execute(
                "SELECT table_name FROM __nexus_meta WHERE letta_file_id = ?",
                [letta_file_id],
            ).fetchall()
            for (table_name,) in rows:
                con.execute(f"DROP TABLE IF EXISTS {_quoted(table_name)}")
            con.execute(
                "DELETE FROM __nexus_meta WHERE letta_file_id = ?", [letta_file_id]
            )
            return len(rows)
        finally:
            con.close()
    except Exception as e:
        logging.warning(f"table_ingest: drop by letta_file_id={letta_file_id} failed: {e}")
        return 0


async def drop_by_letta_file_id(project_id: str, letta_file_id: str) -> int:
    return await asyncio.to_thread(_drop_sync, project_id, letta_file_id)


def drop_project_db(project_id: str) -> None:
    """project 删除级联：整库删除。同步函数，调用方自己决定放哪个 thread。"""
    db_path = _duckdb_path(project_id)
    try:
        if db_path.exists():
            db_path.unlink()
            logging.info(f"table_ingest: removed duckdb for project {project_id}")
    except Exception as e:
        logging.warning(f"table_ingest: remove duckdb for {project_id} failed: {e}")
