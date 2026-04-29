#!/usr/bin/env python3
"""Nexus MCP stdio server — bridge from Cursor / Claude Desktop / Continue 到 Nexus REST API.

Cursor / Claude Desktop 通过 MCP 协议(JSONRPC 2.0 over stdio)调本脚本,
脚本内部走 HTTPS / HTTP 调 Nexus 的 /memory/v1/* 端点。

环境变量:
  NEXUS_URL    Nexus 入口 URL,默认 http://192.168.151.46:9800
               (内网用户用此默认值;外网用户走 VPN 后同样)
  NEXUS_TOKEN  Open WebUI JWT。获取方式:登录 Open WebUI 后,浏览器 F12
               → Application → Local Storage → 拷 token 字段值

提供的工具 (V2,7 个 read-only):
  search_memory(project_id, query, kind?, limit?)
    跨 decisions 和 memory_history 全文搜
  list_decisions(project_id, owner?, status?, decided_from?, decided_to?, limit?)
    按条件列项目的决策
  get_decision(project_id, decision_id)
    决策详情 + parent + children + 完整 trace
  get_trace(project_id, memory_id)
    任意 memory 的完整事件链, 含触发对话
  list_conflicts(project_id, only_unresolved?)
    项目内冲突工单
  get_conflict(project_id, conflict_id)
    单个冲突详情 (memory_ids / detection_reason / 解决状态)
  get_protection(project_id, memory_id)
    Safety Memory 设置 (read_only / append_only / mutable)

未来 V3 会加长效 API key + 限流。使用方式参见同目录 README.md。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


NEXUS_URL = os.environ.get("NEXUS_URL", "http://192.168.151.46:9800").rstrip("/")
NEXUS_TOKEN = os.environ.get("NEXUS_TOKEN")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "nexus-mcp"
SERVER_VERSION = "0.1.0"


# ---------- tool definitions ----------

# Tool schema 公共片段
_PROJECT_ID_FIELD = {
    "type": "string",
    "description": "项目 ID, 如 'ai-infra' / 'org' / 'personal:<user_id>'",
}
_MEMORY_ID_FIELD = {
    "type": "string",
    "description": "memory_id, 如 'file:<letta_file_id>' 或 'decision:<id>'",
}

TOOLS = [
    {
        "name": "search_memory",
        "description": (
            "全文搜决策 + 事件流。FTS5 trigram, 中英混合, 返带 <mark> 高亮 snippet "
            "+ bm25 rank。kind=decisions / memories / all (默认)。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_FIELD,
                "query": {"type": "string", "description": "搜索词, 3+ 字符效果最好"},
                "kind": {"type": "string", "enum": ["decisions", "memories", "all"], "default": "all"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
            "required": ["project_id", "query"],
        },
    },
    {
        "name": "list_decisions",
        "description": "列项目的决策, 支持 owner / status / 决策日期范围筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_FIELD,
                "owner": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["proposed", "approved", "executing", "done", "reverted"],
                },
                "decided_from": {"type": "string", "description": "YYYY-MM-DD"},
                "decided_to": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_decision",
        "description": "决策详情 + parent (取代的上游) + children (取代它的下游) + 完整 trace 事件链。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_FIELD,
                "decision_id": {"type": "integer"},
            },
            "required": ["project_id", "decision_id"],
        },
    },
    {
        "name": "get_trace",
        "description": (
            "取任意 memory 的完整事件链 (ADD/UPDATE/DELETE), 每条带触发对话原文。"
            "可回答'这条 memory 为什么长这样'。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_FIELD,
                "memory_id": _MEMORY_ID_FIELD,
            },
            "required": ["project_id", "memory_id"],
        },
    },
    {
        "name": "list_conflicts",
        "description": "项目内冲突工单 (同名 memory 的版本冲突等)。only_unresolved=true (默认) 只返未解决的。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_FIELD,
                "only_unresolved": {"type": "boolean", "default": True},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_conflict",
        "description": "单个冲突详情:涉及的 memory_ids / 检测原因 / 是否解决 / 解决策略。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_FIELD,
                "conflict_id": {"type": "integer"},
            },
            "required": ["project_id", "conflict_id"],
        },
    },
    {
        "name": "get_protection",
        "description": (
            "查 memory 的 Safety 保护级别 (read_only / append_only / mutable)。"
            "未显式设过返默认 mutable。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": _PROJECT_ID_FIELD,
                "memory_id": _MEMORY_ID_FIELD,
            },
            "required": ["project_id", "memory_id"],
        },
    },
]


# ---------- HTTP bridge to Nexus ----------

def _call_nexus(path: str, params: dict | None = None) -> dict:
    if not NEXUS_TOKEN:
        return {"error": "NEXUS_TOKEN 未设置, 请在 Open WebUI 登录后取 JWT 设到环境变量"}
    qs = "?" + urllib.parse.urlencode(params) if params else ""
    url = f"{NEXUS_URL}{path}{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {NEXUS_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {
            "error": f"HTTP {e.code}",
            "detail": e.read().decode(errors="replace")[:500],
        }
    except Exception as e:
        return {"error": str(e)}


def _q(s: str) -> str:
    """URL-quote 一个 path segment (e.g. project_id)。"""
    return urllib.parse.quote(s, safe="")


def _project_path(project_id: str, suffix: str) -> str:
    return f"/memory/v1/projects/{_q(project_id)}{suffix}"


def tool_search_memory(args: dict) -> dict:
    pid = args.get("project_id") or ""
    q = args.get("query") or ""
    if not pid or not q:
        return {"error": "project_id 和 query 必填"}
    params = {"q": q, "kind": args.get("kind") or "all", "limit": args.get("limit") or 20}
    return _call_nexus(_project_path(pid, "/search"), params)


def tool_list_decisions(args: dict) -> dict:
    pid = args.get("project_id") or ""
    if not pid:
        return {"error": "project_id 必填"}
    params: dict = {"limit": args.get("limit") or 50}
    for k in ("owner", "status", "decided_from", "decided_to"):
        if args.get(k):
            params[k] = args[k]
    return _call_nexus(_project_path(pid, "/decisions"), params)


def tool_get_decision(args: dict) -> dict:
    pid = args.get("project_id") or ""
    did = args.get("decision_id")
    if not pid or did is None:
        return {"error": "project_id 和 decision_id 必填"}
    return _call_nexus(_project_path(pid, f"/decisions/{int(did)}"))


def tool_get_trace(args: dict) -> dict:
    pid = args.get("project_id") or ""
    mid = args.get("memory_id") or ""
    if not pid or not mid:
        return {"error": "project_id 和 memory_id 必填"}
    return _call_nexus(_project_path(pid, f"/memories/{_q(mid)}/trace"))


def tool_list_conflicts(args: dict) -> dict:
    pid = args.get("project_id") or ""
    if not pid:
        return {"error": "project_id 必填"}
    only_unresolved = args.get("only_unresolved")
    if only_unresolved is None:
        only_unresolved = True
    return _call_nexus(
        _project_path(pid, "/conflicts"),
        {"only_unresolved": "true" if only_unresolved else "false"},
    )


def tool_get_conflict(args: dict) -> dict:
    pid = args.get("project_id") or ""
    cid = args.get("conflict_id")
    if not pid or cid is None:
        return {"error": "project_id 和 conflict_id 必填"}
    return _call_nexus(_project_path(pid, f"/conflicts/{int(cid)}"))


def tool_get_protection(args: dict) -> dict:
    pid = args.get("project_id") or ""
    mid = args.get("memory_id") or ""
    if not pid or not mid:
        return {"error": "project_id 和 memory_id 必填"}
    return _call_nexus(_project_path(pid, f"/memories/{_q(mid)}/protection"))


TOOL_HANDLERS = {
    "search_memory": tool_search_memory,
    "list_decisions": tool_list_decisions,
    "get_decision": tool_get_decision,
    "get_trace": tool_get_trace,
    "list_conflicts": tool_list_conflicts,
    "get_conflict": tool_get_conflict,
    "get_protection": tool_get_protection,
}


# ---------- MCP JSONRPC handlers ----------

def handle_request(msg: dict):
    """处理一条 JSONRPC 请求, 返 result dict 或 raise Exception。

    notification (没 id) 返 None。
    """
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    if method == "notifications/initialized":
        return None  # notification, no response needed

    if method == "tools/list":
        return {"tools": TOOLS}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            raise ValueError(f"unknown tool: {name}")
        result = handler(arguments)
        # MCP 约定 tool 返 content array, 我们把 dict 序列化进 text part
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}
            ],
            "isError": "error" in result,
        }

    raise ValueError(f"unknown method: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            # 无 id 也无法回 error, 写到 stderr
            print(f"[nexus-mcp] bad JSON: {e}", file=sys.stderr)
            continue

        msg_id = msg.get("id")
        try:
            result = handle_request(msg)
        except Exception as e:
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32000, "message": str(e)},
            }
            print(json.dumps(response, ensure_ascii=False), flush=True)
            continue

        if result is None:
            continue  # notification, no response

        response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
