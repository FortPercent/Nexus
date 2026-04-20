"""内部 SQL endpoints for Letta custom tools.

路由: POST /internal/project/{project_id}/sql/{list-tables,describe-table,query}
鉴权: Authorization: Bearer $ADAPTER_API_KEY + body.user_id 必须是 project_members

沙箱 (多层深度防御):
  1. sqlglot AST 顶层类型: 允许 SELECT / WITH(-SELECT) / UNION / INTERSECT / EXCEPT
  2. 禁用节点: DDL (Create/Drop/Alter/TruncateTable) / DML (Insert/Update/Delete/Merge)
     / Command (ATTACH/PRAGMA/SET/INSTALL/LOAD/COPY/CALL) / Attach/Detach/Pragma
  3. 表名 allowlist: 所有 FROM/JOIN 来源必须是 __nexus_meta 已注册表; CTE 别名豁免;
     不允许 table function (read_csv / read_parquet / generate_series 等)
  4. 危险函数 denylist: AST 任意位置的 Anonymous 节点 (未知函数) 命中
     read_* / copy_* / load_* / install_* / pragma_* 前缀 或 exact name 都拒
  5. LIMIT 100 仅对顶层 Select 自动注入 (集合运算靠 4KB 结果截断兜底)
  6. DuckDB ro 连接 + 10s 超时 + 4KB 结果截断
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel

from config import ADAPTER_API_KEY
from db import use_db_async
from table_ingest import _duckdb_path, _quoted

router = APIRouter(prefix="/internal/project/{project_id}/sql", tags=["internal-sql"])

MAX_RESULT_BYTES = 4096
QUERY_TIMEOUT_SECONDS = 10
DEFAULT_LIMIT = 100
SAMPLE_ROWS = 3


# ----- 鉴权 -----

async def _require_api_key(authorization: Optional[str] = Header(None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing API key")
    token = authorization[len("Bearer "):].strip()
    if token != ADAPTER_API_KEY:
        raise HTTPException(401, "Invalid API key")


async def _require_member(user_id: str, project_id: str) -> None:
    if not user_id:
        raise HTTPException(400, "user_id required")
    async with use_db_async() as db:
        async with db.execute(
            "SELECT 1 FROM project_members WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(403, f"user {user_id} is not a member of project {project_id}")


# ----- 请求模型 -----

class BaseBody(BaseModel):
    user_id: str


class DescribeBody(BaseBody):
    table_name: str


class QueryBody(BaseBody):
    sql: str


# ----- SQL 沙箱 -----

class SandboxError(Exception):
    pass


_FORBIDDEN_NODE_NAMES = {
    # DDL
    "Create", "Drop", "Alter", "TruncateTable",
    # DML
    "Insert", "Update", "Delete", "Merge",
    # sqlglot Command: ATTACH / DETACH / PRAGMA / SET / INSTALL / LOAD / COPY / CALL
    "Command",
    # 独立解析出的危险节点
    "Attach", "Detach", "Pragma",
}

# P1 深度防御 (2026-04-20 审查补丁): 即使 DuckDB 当前拒绝 scalar 用法, 也不要依赖它.
# 任何 DuckDB 文件 / IO / 扩展相关函数一律从 AST 层拒.
# 维护 denylist 比 whitelist 可行 (DuckDB 内置函数数百个). 未识别的函数在 sqlglot 里是 Anonymous 节点.
_DANGEROUS_FN_EXACT = {
    "read_text", "read_blob",
    "read_csv", "read_csv_auto",
    "read_parquet",
    "read_json", "read_json_auto", "read_ndjson", "read_ndjson_auto",
    "read_xlsx", "read_excel",
    "glob",
    "copy", "copy_from_database", "copy_to_database",
    "load_extension", "install_extension", "install", "load",
    "attach", "detach",
}
_DANGEROUS_FN_PREFIX = ("read_", "copy_", "load_", "install_", "pragma_")


def _collect_and_validate(ast, allowlist: set[str]) -> None:
    """遍历 AST: 禁节点检查 + FROM/JOIN 来源检查。
    CTE 别名 (WITH t AS (...)) 视为合法引用, 但 CTE body 里引用的表仍要在 allowlist。"""
    import sqlglot.expressions as exp

    for node in ast.walk():
        cls_name = type(node).__name__
        if cls_name in _FORBIDDEN_NODE_NAMES:
            raise SandboxError(f"forbidden node: {cls_name}")
        # P1: 任何位置的危险函数调用 (SELECT 列表 / WHERE / ORDER BY 等非 FROM 位置都要拦)
        # sqlglot 把未识别的函数调用归为 exp.Anonymous, name 属性是函数名.
        if isinstance(node, exp.Anonymous):
            fn = (node.name or "").lower()
            if fn in _DANGEROUS_FN_EXACT or any(fn.startswith(p) for p in _DANGEROUS_FN_PREFIX):
                raise SandboxError(f"forbidden function call: {fn}()")

    # 收集本查询及所有嵌套 Select 里 WITH 声明的 CTE 别名,
    # 作为 allowlist 的动态补集 (仅在本 SQL 作用域内有效)
    cte_aliases: set[str] = set()
    for cte in ast.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            cte_aliases.add(alias)
    effective_allowlist = allowlist | cte_aliases

    # 所有 From / Join 的来源 (this) 必须是 exp.Table 或 exp.Subquery;
    # Anonymous / TableFromRows / 其他 = table function, 拒绝
    for source_parent in list(ast.find_all(exp.From)) + list(ast.find_all(exp.Join)):
        source = source_parent.this
        if isinstance(source, exp.Subquery):
            continue  # 子查询递归由 ast.walk 覆盖
        if not isinstance(source, exp.Table):
            raise SandboxError(
                f"table function / unsupported source in FROM: {type(source).__name__}"
            )
        tname = source.name
        if tname not in effective_allowlist:
            raise SandboxError(f"table '{tname}' not in this project")


def _parse_and_rewrite(sql: str, allowlist: set[str]) -> str:
    """解析 → 沙箱校验 → 无 LIMIT 则注入 LIMIT DEFAULT_LIMIT → 返回重写后的 SQL。"""
    import sqlglot
    import sqlglot.expressions as exp

    try:
        ast = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception as e:
        raise SandboxError(f"SQL parse error: {e}")

    if ast is None:
        raise SandboxError("empty SQL")

    # 顶层允许 SELECT / WITH(-SELECT) / UNION / INTERSECT / EXCEPT
    # 集合运算的每个分支 SELECT 的 FROM 来源会被 _collect_and_validate 里
    # find_all(exp.From) 递归覆盖, allowlist 检查仍然有效
    top = ast
    if isinstance(top, exp.With):
        top = top.this
    if not isinstance(top, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise SandboxError(
            f"only SELECT / UNION / INTERSECT / EXCEPT allowed at top level (got {type(ast).__name__})"
        )

    _collect_and_validate(ast, allowlist)

    # LIMIT 注入只对 Select 顶层做: 集合运算的 LIMIT 语义易产生歧义
    # (DuckDB 语法上允许 Union 外层 LIMIT, 但 sqlglot 的 exp.Union 不一定有 limit slot);
    # 留给 4KB 结果截断兜底
    if isinstance(top, exp.Select) and not top.args.get("limit"):
        top.set("limit", exp.Limit(expression=exp.Literal.number(DEFAULT_LIMIT)))

    return ast.sql(dialect="duckdb")


# ----- DuckDB 执行 -----

def _load_allowlist(project_id: str) -> tuple[set[str], Optional[str], str]:
    """返回 (allowlist, db_path_str, status)。
    status: 'no_db'  = project 没上传过结构化文件, 纯文档 project (正常)
            'empty' = db 存在, meta 为空（上传过又全删了, 正常）
            'loaded'= 正常有表
            'corrupt'= db 文件存在但打不开或读 meta 失败, 需要给用户显式反馈"""
    import duckdb
    db_path = _duckdb_path(project_id)
    if not db_path.exists():
        return set(), None, "no_db"
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute("SELECT table_name FROM __nexus_meta").fetchall()
        finally:
            con.close()
    except Exception as e:
        logging.warning(f"sql_endpoints: load allowlist failed for {project_id}: {e}")
        return set(), str(db_path), "corrupt"
    if not rows:
        return set(), str(db_path), "empty"
    return {r[0] for r in rows}, str(db_path), "loaded"


def _exec_ro(db_path: str, sql: str) -> tuple[list[str], list[tuple]]:
    import duckdb
    con = duckdb.connect(db_path, read_only=True)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        return cols, rows
    finally:
        con.close()


def _format_md(cols: list[str], rows: list[tuple], truncated_msg: Optional[str] = None) -> str:
    if not cols:
        return "(empty result)"
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        cells = [_cell(c) for c in r]
        lines.append("| " + " | ".join(cells) + " |")
    out = "\n".join(lines)
    if len(out.encode("utf-8")) > MAX_RESULT_BYTES:
        # 按字节截断
        b = out.encode("utf-8")[:MAX_RESULT_BYTES]
        out = b.decode("utf-8", errors="ignore")
        out += "\n\n⚠️ Query OK but result truncated (>4KB). Refine with aggregation or narrower SELECT."
    elif truncated_msg:
        out += "\n\n" + truncated_msg
    return out


def _cell(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return s.replace("|", "\\|").replace("\n", " ").replace("\r", "")


async def _run_query(db_path: str, sql: str) -> tuple[list[str], list[tuple]]:
    return await asyncio.wait_for(
        asyncio.to_thread(_exec_ro, db_path, sql),
        timeout=QUERY_TIMEOUT_SECONDS,
    )


# ----- 路由 -----

@router.post("/list-tables", dependencies=[Depends(_require_api_key)])
async def list_tables(project_id: str, body: BaseBody):
    await _require_member(body.user_id, project_id)
    allowlist, db_path, status = _load_allowlist(project_id)
    if status in ("no_db", "empty"):
        return {"ok": True, "text": "No tables available in this project."}
    if status == "corrupt":
        return {"ok": False, "text": "Project database unavailable (file exists but cannot be opened). Contact admin."}
    try:
        cols, rows = await _run_query(
            db_path,
            "SELECT table_name, source_file, row_count FROM __nexus_meta ORDER BY table_name",
        )
    except asyncio.TimeoutError:
        return {"ok": False, "text": f"Timeout after {QUERY_TIMEOUT_SECONDS}s"}
    except Exception as e:
        return {"ok": False, "text": f"Internal error: {e}"}
    return {"ok": True, "text": _format_md(cols, rows)}


@router.post("/describe-table", dependencies=[Depends(_require_api_key)])
async def describe_table(project_id: str, body: DescribeBody):
    await _require_member(body.user_id, project_id)
    allowlist, db_path, status = _load_allowlist(project_id)
    if status == "corrupt":
        return {"ok": False, "text": "Project database unavailable (file exists but cannot be opened). Contact admin."}
    if body.table_name not in allowlist:
        return {"ok": False, "text": f"Table not found: {body.table_name}. Use list_tables to see available."}
    try:
        # schema
        schema_sql = (
            f"SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = '{body.table_name.replace(chr(39), chr(39) * 2)}' "
            f"ORDER BY ordinal_position"
        )
        cols, rows = await _run_query(db_path, schema_sql)
        schema_md = _format_md(cols, rows)

        # sample
        sample_sql = f"SELECT * FROM {_quoted(body.table_name)} LIMIT {SAMPLE_ROWS}"
        scols, srows = await _run_query(db_path, sample_sql)
        sample_md = _format_md(scols, srows)
    except asyncio.TimeoutError:
        return {"ok": False, "text": f"Timeout after {QUERY_TIMEOUT_SECONDS}s"}
    except Exception as e:
        return {"ok": False, "text": f"Internal error: {e}"}

    text = f"## Schema of `{body.table_name}`\n\n{schema_md}\n\n## Sample ({SAMPLE_ROWS} rows)\n\n{sample_md}"
    return {"ok": True, "text": text}


@router.post("/query", dependencies=[Depends(_require_api_key)])
async def query_table(project_id: str, body: QueryBody):
    await _require_member(body.user_id, project_id)
    allowlist, db_path, status = _load_allowlist(project_id)
    if status == "corrupt":
        return {"ok": False, "text": "Project database unavailable (file exists but cannot be opened). Contact admin."}
    if status in ("no_db", "empty"):
        return {"ok": False, "text": "No tables available in this project. Upload xlsx/csv first."}
    try:
        rewritten = _parse_and_rewrite(body.sql, allowlist)
    except SandboxError as e:
        return {"ok": False, "text": f"SQL rejected: {e}"}

    try:
        cols, rows = await _run_query(db_path, rewritten)
    except asyncio.TimeoutError:
        return {"ok": False, "text": f"SQL timeout after {QUERY_TIMEOUT_SECONDS}s"}
    except Exception as e:
        return {"ok": False, "text": f"SQL error: {e}"}

    return {"ok": True, "text": _format_md(cols, rows)}
