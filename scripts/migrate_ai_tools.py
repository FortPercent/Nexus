"""一次性 migration：给所有已存在 agent 挂上 suggest_todo 工具 + 升级 persona。

安全：幂等，重复跑 OK。只 attach / update，不删除现有工具。

用法（容器内）：
  docker exec teleai-adapter python /app/scripts/migrate_ai_tools.py
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routing import letta, _get_suggest_tool_id, _get_suggest_todo_tool_id, PERSONA_TEXT
from config import DB_PATH


def main():
    suggest_tool_id = _get_suggest_tool_id()
    suggest_todo_id = _get_suggest_todo_tool_id()
    print(f"suggest_project_knowledge: {suggest_tool_id}")
    print(f"suggest_todo:             {suggest_todo_id}")

    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT user_id, project_id, agent_id FROM user_agent_map").fetchall()

    stats = {"attached_suggest": 0, "attached_todo": 0, "persona_updated": 0, "errors": 0}
    for r in rows:
        aid = r["agent_id"]
        try:
            agent = letta.agents.retrieve(agent_id=aid, include=["agent.tools"])
            existing = {getattr(t, "name", "") for t in (agent.tools or [])}

            if "suggest_project_knowledge" not in existing:
                try:
                    letta.agents.tools.attach(agent_id=aid, tool_id=suggest_tool_id)
                    stats["attached_suggest"] += 1
                    print(f"  {aid[-12:]} + suggest_project_knowledge")
                except Exception as e:
                    print(f"  {aid[-12:]} ! attach suggest failed: {e}")
                    stats["errors"] += 1

            if "suggest_todo" not in existing:
                try:
                    letta.agents.tools.attach(agent_id=aid, tool_id=suggest_todo_id)
                    stats["attached_todo"] += 1
                    print(f"  {aid[-12:]} + suggest_todo")
                except Exception as e:
                    print(f"  {aid[-12:]} ! attach suggest_todo failed: {e}")
                    stats["errors"] += 1

            # 升级 persona block
            blocks = letta.agents.blocks.list(agent_id=aid)
            for b in blocks:
                if b.label == "persona":
                    if (b.value or "").strip() != PERSONA_TEXT.strip():
                        letta.blocks.update(block_id=b.id, value=PERSONA_TEXT)
                        stats["persona_updated"] += 1
                        print(f"  {aid[-12:]} ~ persona updated")
                    break

        except Exception as e:
            print(f"  {aid[-12:]} ! agent retrieve failed: {e}")
            stats["errors"] += 1

    c.close()
    print()
    print(f"== agents: {len(rows)} | attached suggest: {stats['attached_suggest']} "
          f"| attached todo: {stats['attached_todo']} "
          f"| persona updated: {stats['persona_updated']} "
          f"| errors: {stats['errors']}")


if __name__ == "__main__":
    main()
