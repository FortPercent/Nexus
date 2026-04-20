"""批量把所有 agent 的 llm_config.context_window 从 32K 拔到 60K。
安全性：保留 llm_config 其他字段，只改 context_window。"""
import sys, sqlite3
sys.path.insert(0, "/app")
from routing import letta

TARGET = 60000

c = sqlite3.connect("/data/serving/adapter/adapter.db")
agents = [r[0] for r in c.execute("SELECT agent_id FROM user_agent_map").fetchall()]
print(f"total agents: {len(agents)}")

fixed = already_ok = failed = 0
for aid in agents:
    try:
        a = letta.agents.retrieve(agent_id=aid)
        lc = a.llm_config
        if lc.context_window and lc.context_window >= TARGET:
            already_ok += 1
            continue
        new_lc = {
            "model": lc.model,
            "model_endpoint_type": lc.model_endpoint_type,
            "model_endpoint": lc.model_endpoint,
            "context_window": TARGET,
            "enable_reasoner": lc.enable_reasoner,
        }
        letta.agents.update(agent_id=aid, llm_config=new_lc)
        fixed += 1
        if fixed % 5 == 0:
            print(f"  progress: fixed={fixed}")
    except Exception as e:
        failed += 1
        print(f"  FAIL {aid[-8:]}: {type(e).__name__}: {str(e)[:120]}")

print(f"\nresult: fixed={fixed}  already_ok={already_ok}  failed={failed}")
