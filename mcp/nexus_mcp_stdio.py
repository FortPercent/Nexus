#!/usr/bin/env python3
"""Nexus MCP stdio server — bridge from Cursor / Claude Desktop / Continue 到 Nexus REST API.

Cursor / Claude Desktop 通过 MCP 协议(JSONRPC 2.0 over stdio)调本脚本,
脚本内部走 HTTPS / HTTP 调 Nexus 的 /memory/v1/* 端点。

环境变量:
  NEXUS_URL    Nexus 入口 URL,默认 http://192.168.151.46:9800
               (内网用户用此默认值;外网用户走 VPN 后同样)
  NEXUS_TOKEN  Open WebUI JWT。获取方式:登录 Open WebUI 后,浏览器 F12
               → Application → Local Storage → 拷 token 字段值

本 V1 提供工具:
  search_memory(project_id, query, kind?, limit?)
    跨 decisions 和 memory_history 全文搜,trigram FTS5 backend,
    返带 snippet 高亮和 bm25 rank 的混合结果

未来 V2 / V3 会扩 get_decision / get_trace / list_conflicts 等。

使用方式参见同目录 README.md。
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

TOOLS = [
    {
        "name": "search_memory",
        "description": (
            "在 Nexus 项目里全文搜索决策(decisions)和记忆事件(memory_history)。"
            "支持中英文混合查询,FTS5 trigram 索引,返回带 <mark> 高亮 snippet "
            "+ bm25 rank 的混合结果。kind=decisions 仅搜决策,kind=memories "
            "仅搜事件,kind=all (默认)两者并查。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "项目 ID,如 'ai-infra' / 'org' / 'personal:<user_id>'",
                },
                "query": {"type": "string", "description": "搜索词,3+ 字符效果最好"},
                "kind": {
                    "type": "string",
                    "enum": ["decisions", "memories", "all"],
                    "default": "all",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["project_id", "query"],
        },
    }
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


def tool_search_memory(args: dict) -> dict:
    project_id = args.get("project_id") or ""
    query = args.get("query") or ""
    if not project_id or not query:
        return {"error": "project_id 和 query 必填"}
    return _call_nexus(
        f"/memory/v1/projects/{urllib.parse.quote(project_id, safe='')}/search",
        {
            "q": query,
            "kind": args.get("kind") or "all",
            "limit": args.get("limit") or 20,
        },
    )


TOOL_HANDLERS = {"search_memory": tool_search_memory}


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
