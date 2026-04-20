"""Letta 自定义工具：SQL 查询 (L2)。

三个薄壳 HTTP 客户端：list_tables / describe_table / query_table。
真实 SQL 执行在 adapter /internal/project/{pid}/sql/* endpoint。

照 routing.py::suggest_project_knowledge 同模式：
- 从 agent_state.metadata 拿 project / owner
- 通过 urllib.request POST 到 http://teleai-adapter:8000/internal/...
- 带 Authorization: Bearer $ADAPTER_API_KEY（Letta 容器通过 docker-compose env 注入）

工具通过 letta.tools.upsert_from_function 注册, 返回的 tool_id 模块级缓存。
"""
import logging

from routing import letta


_sql_list_tables_id: str | None = None
_sql_describe_table_id: str | None = None
_sql_query_table_id: str | None = None


def _get_list_tables_id() -> str:
    global _sql_list_tables_id
    if _sql_list_tables_id:
        return _sql_list_tables_id

    def list_tables(agent_state: "AgentState") -> str:
        """List all structured data tables available in this project (from uploaded xlsx/csv files).

        Call this FIRST when the user asks about data counts, sums, aggregations, or
        any statistics that could be answered via a table query. The returned table
        names can then be passed to describe_table or used in query_table SQL.

        Returns:
            Markdown table of (table_name, source_file, row_count), or a message
            that no structured data is available in this project.
        """
        import os
        import urllib.request
        import urllib.parse
        import json as _json

        project_id = agent_state.metadata.get("project", "")
        user_id = agent_state.metadata.get("owner", "")
        api_key = os.environ.get("ADAPTER_API_KEY", "")
        if not project_id or not user_id or not api_key:
            return "Tool misconfigured: missing project, user, or API key."

        url = (
            f"http://teleai-adapter:8000/internal/project/"
            f"{urllib.parse.quote(project_id, safe='')}/sql/list-tables"
        )
        try:
            body = _json.dumps({"user_id": user_id}).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
            return data.get("text", "(no response text)")
        except Exception as e:
            return f"list_tables call failed: {e}"

    tool = letta.tools.upsert_from_function(func=list_tables)
    _sql_list_tables_id = tool.id
    logging.info(f"sql list_tables tool: {tool.id}")
    return _sql_list_tables_id


def _get_describe_table_id() -> str:
    global _sql_describe_table_id
    if _sql_describe_table_id:
        return _sql_describe_table_id

    def describe_table(table_name: str, agent_state: "AgentState") -> str:
        """Return the schema (columns + types) and first 3 sample rows of a table.

        Call this after list_tables to understand columns before writing SQL.

        Args:
            table_name: Exact table name from list_tables

        Returns:
            Markdown with schema table and sample rows, or an error if table not found.
        """
        import os
        import urllib.request
        import urllib.parse
        import json as _json

        project_id = agent_state.metadata.get("project", "")
        user_id = agent_state.metadata.get("owner", "")
        api_key = os.environ.get("ADAPTER_API_KEY", "")
        if not project_id or not user_id or not api_key:
            return "Tool misconfigured: missing project, user, or API key."

        url = (
            f"http://teleai-adapter:8000/internal/project/"
            f"{urllib.parse.quote(project_id, safe='')}/sql/describe-table"
        )
        try:
            body = _json.dumps({"user_id": user_id, "table_name": table_name}).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
            return data.get("text", "(no response text)")
        except Exception as e:
            return f"describe_table call failed: {e}"

    tool = letta.tools.upsert_from_function(func=describe_table)
    _sql_describe_table_id = tool.id
    logging.info(f"sql describe_table tool: {tool.id}")
    return _sql_describe_table_id


def _get_query_table_id() -> str:
    global _sql_query_table_id
    if _sql_query_table_id:
        return _sql_query_table_id

    def query_table(sql: str, agent_state: "AgentState") -> str:
        """Execute a read-only DuckDB query on this project's structured data tables.

        Prefer this over grep_files for aggregation questions (counts, sums, averages,
        groupings, rankings). Use list_tables and describe_table first to know the
        schema.

        Sandbox:
          - Top-level must be SELECT / WITH(-SELECT) / UNION / INTERSECT / EXCEPT
          - Tables referenced in FROM/JOIN must be from list_tables (CTE names exempted)
          - No DDL/DML, no ATTACH/PRAGMA/COPY/INSTALL/LOAD, no table functions
            (read_csv / read_parquet / read_text / generate_series etc.)
          - Dangerous file/IO scalar functions (read_*, copy_*, load_*, install_*, pragma_*)
            are rejected anywhere in the query
          - LIMIT 100 is auto-injected only when top-level is a plain SELECT without one;
            for UNION/INTERSECT/EXCEPT rely on the 4KB result truncation instead
          - 10s timeout
          - 4KB result size cap; if truncated, refine with aggregation or narrower projection

        Args:
            sql: A DuckDB-dialect SELECT (or UNION/INTERSECT/EXCEPT), e.g.
                 SELECT COUNT(*) FROM "固定资产清单" WHERE 资产名称 LIKE '%机器人%'

        Returns:
            Markdown table of the result rows, or an error message guiding you to fix the SQL.
        """
        import os
        import urllib.request
        import urllib.parse
        import json as _json

        project_id = agent_state.metadata.get("project", "")
        user_id = agent_state.metadata.get("owner", "")
        api_key = os.environ.get("ADAPTER_API_KEY", "")
        if not project_id or not user_id or not api_key:
            return "Tool misconfigured: missing project, user, or API key."

        url = (
            f"http://teleai-adapter:8000/internal/project/"
            f"{urllib.parse.quote(project_id, safe='')}/sql/query"
        )
        try:
            body = _json.dumps({"user_id": user_id, "sql": sql}).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
            return data.get("text", "(no response text)")
        except Exception as e:
            return f"query_table call failed: {e}"

    tool = letta.tools.upsert_from_function(func=query_table)
    _sql_query_table_id = tool.id
    logging.info(f"sql query_table tool: {tool.id}")
    return _sql_query_table_id


def get_sql_tool_ids() -> list[str]:
    """返回三个工具 id（首次调用会注册；后续直接返回缓存）。
    任一 upsert 失败会抛出，调用方自己决定是否 swallow。"""
    return [_get_list_tables_id(), _get_describe_table_id(), _get_query_table_id()]


def attach_sql_tools_for_project(project_id: str) -> dict:
    """上传成功后的快速 hook: 只对该 project 的 agents attach 三工具（不 detach）。

    调用点: admin_api._process_and_upload 在 ingest 成功后。
    用途: 让老 agent 立即拿到新能力, 不等 300s reconcile。
    幂等: 已 attach 再 attach 会返 conflict, 被 swallow。
    不做 detach: 上传只会增加表, 不会让 should_attach 从 True 变 False。
    """
    from db import get_db
    stats = {"attached": 0, "skipped": 0, "errors": 0}
    if not should_attach_sql_tools(project_id):
        return stats

    try:
        tool_ids = get_sql_tool_ids()
    except Exception as e:
        logging.error(f"attach_sql_tools_for_project: cannot load tool ids: {e}")
        stats["errors"] = 1
        return stats

    db = get_db()
    try:
        agents = db.execute(
            "SELECT agent_id FROM user_agent_map WHERE project_id = ?", (project_id,)
        ).fetchall()
    finally:
        db.close()

    for row in agents:
        agent_id = row["agent_id"]
        for tid in tool_ids:
            try:
                letta.agents.tools.attach(agent_id=agent_id, tool_id=tid)
                stats["attached"] += 1
            except Exception as e:
                msg = str(e).lower()
                if "conflict" in msg or "already" in msg or "409" in msg:
                    stats["skipped"] += 1
                else:
                    stats["errors"] += 1
                    logging.warning(f"attach_sql_tools_for_project: {agent_id}/{tid}: {e}")

    logging.info(f"attach_sql_tools_for_project({project_id}): {stats}")
    return stats


def reconcile_sql_tool_attachments() -> dict:
    """全量对账：按每个 agent 的 project 状态 attach/detach SQL 三工具。

    singleton leader 保证：调用方（main.py._reconcile_loop）已在 fcntl 锁下。
    返回 {"attached": N, "detached": N, "skipped": N, "errors": N} 用于日志统计。
    """
    from db import get_db
    stats = {"attached": 0, "detached": 0, "skipped": 0, "errors": 0}

    try:
        tool_ids = get_sql_tool_ids()
    except Exception as e:
        logging.error(f"reconcile_sql_tools: cannot load tool ids: {e}")
        return stats

    db = get_db()
    try:
        agents = db.execute(
            "SELECT DISTINCT project_id, agent_id FROM user_agent_map"
        ).fetchall()
    finally:
        db.close()

    # project 判定结果缓存，避免同一 project 多次查 DuckDB
    attach_cache: dict[str, bool] = {}

    for row in agents:
        project_id = row["project_id"]
        agent_id = row["agent_id"]
        if project_id not in attach_cache:
            attach_cache[project_id] = should_attach_sql_tools(project_id)
        should = attach_cache[project_id]

        for tid in tool_ids:
            try:
                if should:
                    letta.agents.tools.attach(agent_id=agent_id, tool_id=tid)
                    stats["attached"] += 1
                else:
                    letta.agents.tools.detach(agent_id=agent_id, tool_id=tid)
                    stats["detached"] += 1
            except Exception as e:
                # conflict（已 attached）和 not-attached（detach 已挂过的）都算正常
                msg = str(e).lower()
                if "conflict" in msg or "not" in msg and "attach" in msg or "404" in msg:
                    stats["skipped"] += 1
                else:
                    stats["errors"] += 1
                    logging.warning(f"reconcile_sql_tools: {agent_id}/{tid}: {e}")

    return stats


def should_attach_sql_tools(project_id: str) -> bool:
    """决定 agent 是否应挂 SQL 工具。

    语义（与设计文档 §11.4 failure isolation 对齐）：
      loaded / corrupt → True（corrupt 时保留工具, 工具自己返 "project database unavailable"
                              让用户/agent 能看到明确的降级信号, 而不是"工具消失"）
      no_db / empty   → False（纯文档 project, 不污染 agent 工具面）
    """
    from sql_endpoints import _load_allowlist
    _, _, status = _load_allowlist(project_id)
    return status in ("loaded", "corrupt")
