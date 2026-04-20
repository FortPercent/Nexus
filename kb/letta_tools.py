"""Letta 自定义工具：知识库访问 (PoC v0).

两个薄壳 HTTP 客户端 → adapter /internal/project/{pid}/kb/*。
结构照 letta_sql_tools.py（SQL 工具）完全复制。

- 从 agent_state.metadata 拿 project / owner
- urllib.request POST 到 http://teleai-adapter:8000/internal/...
- 带 Authorization: Bearer $ADAPTER_API_KEY (Letta 容器通过 docker-compose env 注入)

工具通过 letta.tools.upsert_from_function 注册, 返回的 tool_id 模块级缓存。
"""
import logging

from routing import letta


_list_tool_id: str | None = None
_read_tool_id: str | None = None
_grep_tool_id: str | None = None


def _get_list_files_tool_id() -> str:
    global _list_tool_id
    if _list_tool_id:
        return _list_tool_id

    def list_project_files(agent_state: "AgentState") -> str:
        """List all knowledge files available in the current project.

        Call this FIRST when the user asks about documents, policies, procedures,
        regulations, specs, or any content that might be in project files. Returns
        a markdown table of (file name, source, quality, size). Then call
        read_project_file with a specific file name to read its content.

        Returns:
            Markdown table of files, or a message that no files are available.
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
            f"{urllib.parse.quote(project_id, safe='')}/kb/list-files"
        )
        try:
            body = _json.dumps({"user_id": user_id, "scope": "project"}).encode()
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
            return data.get("text", "(no response text)")
        except Exception as e:
            return f"list_project_files call failed: {e}"

    tool = letta.tools.upsert_from_function(func=list_project_files)
    _list_tool_id = tool.id
    logging.info(f"kb list_project_files tool: {tool.id}")
    return _list_tool_id


def _get_read_tool_id() -> str:
    global _read_tool_id
    if _read_tool_id:
        return _read_tool_id

    def read_project_file(
        file_name: str,
        agent_state: "AgentState",
        offset: int = 0,
        max_chars: int = 8000,
    ) -> str:
        """Read the content of a specific knowledge file from the current project.

        Call this after list_project_files to open a file and see its content.
        The file_name argument can be either the display name (e.g.,
        "DLP安装卸载说明.docx") or the raw name with .md suffix (e.g.,
        "DLP安装卸载说明.docx.md"). Both work.

        Args:
            file_name: File name from list_project_files (display or raw).
            offset: Start reading at this character offset (default 0).
            max_chars: Max characters to read in one call (default 8000).

        Returns:
            File content with a header showing source and total length.
            If eof=False in the footer, call again with the suggested offset to
            read the rest.
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
            f"{urllib.parse.quote(project_id, safe='')}/kb/read"
        )
        try:
            body = _json.dumps(
                {
                    "user_id": user_id,
                    "file_name": file_name,
                    "scope": "project",
                    "offset": offset,
                    "max_chars": max_chars,
                }
            ).encode()
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.loads(resp.read().decode())
            return data.get("text", "(no response text)")
        except Exception as e:
            return f"read_project_file call failed: {e}"

    tool = letta.tools.upsert_from_function(func=read_project_file)
    _read_tool_id = tool.id
    logging.info(f"kb read_project_file tool: {tool.id}")
    return _read_tool_id


def _get_grep_tool_id() -> str:
    global _grep_tool_id
    if _grep_tool_id:
        return _grep_tool_id

    def grep_project_files(pattern: str, agent_state: "AgentState") -> str:
        """Search for a regex/literal pattern across all knowledge files in the current project.

        Use this when the user asks 'which document mentions X' or 'is there anywhere
        it talks about Y'. Returns up to 20 hits grouped by file, each with line number
        and a snippet. After finding hits, you can call read_project_file on the most
        relevant file to see the full context.

        Args:
            pattern: The regex or literal string to search for. Case-insensitive by default.
                e.g. "消防|火灾" or "DLP.*安装"

        Returns:
            Markdown of hits grouped by file, or a message saying no matches found.
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
            f"{urllib.parse.quote(project_id, safe='')}/kb/grep"
        )
        try:
            body = _json.dumps({
                "user_id": user_id,
                "pattern": pattern,
                "scope": "project",
                "max_hits": 20,
                "ignore_case": True,
            }).encode()
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode())
            return data.get("text", "(no response text)")
        except Exception as e:
            return f"grep_project_files call failed: {e}"

    tool = letta.tools.upsert_from_function(func=grep_project_files)
    _grep_tool_id = tool.id
    logging.info(f"kb grep_project_files tool: {tool.id}")
    return _grep_tool_id


def get_kb_tool_ids() -> list[str]:
    """返回 [list_tool_id, read_tool_id, grep_tool_id]。首次调用注册，后续返回缓存。"""
    return [_get_list_files_tool_id(), _get_read_tool_id(), _get_grep_tool_id()]
