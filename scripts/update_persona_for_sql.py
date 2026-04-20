#!/usr/bin/env python3
"""L2 M3 一次性脚本：把新版 PERSONA_TEXT 刷到所有已有 agent 的 persona block。

用法（容器里）:
    docker exec -w /app teleai-adapter python scripts/update_persona_for_sql.py

幂等：同一份 value 写第二次不会出问题。
"""
import logging
import sys
sys.path.insert(0, "/app")

from routing import letta, PERSONA_TEXT
from db import get_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main():
    db = get_db()
    try:
        agents = db.execute("SELECT agent_id FROM user_agent_map").fetchall()
    finally:
        db.close()

    total = len(agents)
    ok = failed = 0
    for row in agents:
        aid = row["agent_id"]
        try:
            letta.agents.blocks.update(
                agent_id=aid,
                block_label="persona",
                value=PERSONA_TEXT,
            )
            ok += 1
            if ok % 10 == 0:
                logging.info(f"  [{ok}/{total}] updated")
        except Exception as e:
            failed += 1
            logging.warning(f"{aid}: {type(e).__name__}: {e}")

    logging.info(f"done: {ok}/{total} updated, {failed} failed")


if __name__ == "__main__":
    main()
