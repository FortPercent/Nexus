#!/usr/bin/env python3
"""同步所有 Letta agent 的 llm_config.model_endpoint 到当前 VLLM_ENDPOINT。

踩坑记录（2026-04-17 晚）：
  - agent 的 llm_config 是**建 agent 时 snapshot**，不会自动跟随 .env 变化
  - 当临港 vLLM job ID 变化后，adapter 的 .env 更新了，但 agent 还指向旧 URL
  - 症状：letta-* 聊天全部 500 "no available server" 或 403 "无该推理任务的访问权限"
  - 外加坑：Letta 使用 `OPENAI_API_KEY` env（不是 VLLM_API_KEY）做 openai 兼容调用

用法（vLLM endpoint 换新时）：
  1. 改 .env 的 VLLM_ENDPOINT + VLLM_API_KEY + OPENAI_API_KEY（3 个都要改）
  2. `docker compose up -d letta-server adapter`（recreate 才能重载 env）
  3. `docker exec teleai-adapter python /app/scripts/sync_agent_endpoints.py`
  4. 立刻打一次 letta-* 聊天验活

  干跑（不改数据）：加 --dry-run
"""
import argparse
import os
import sys

# 允许脚本在 /app/scripts/ 下运行时找到 /app/routing.py
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from routing import letta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("VLLM_ENDPOINT"),
                    help="新 endpoint；默认从 VLLM_ENDPOINT env 读")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.endpoint:
        print("ERROR: 需要 VLLM_ENDPOINT env 或 --endpoint", file=sys.stderr)
        sys.exit(2)

    print(f"target endpoint: {args.endpoint}")
    print(f"dry_run: {args.dry_run}\n")

    updated = skipped = failed = 0
    cursor = None
    while True:
        page = letta.agents.list(limit=100) if cursor is None else letta.agents.list(limit=100, after=cursor)
        items = list(page.items) if hasattr(page, "items") else list(page)
        if not items:
            break

        for a in items:
            lc = a.llm_config
            current = getattr(lc, "model_endpoint", None)
            if current == args.endpoint:
                skipped += 1
                continue

            print(f"agent {a.id} ({a.name[:40]}): {current} → {args.endpoint}")
            if args.dry_run:
                updated += 1
                continue

            try:
                new_lc = lc.model_dump() if hasattr(lc, "model_dump") else dict(lc)
                new_lc["model_endpoint"] = args.endpoint
                letta.agents.update(agent_id=a.id, llm_config=new_lc)
                updated += 1
            except Exception as e:
                failed += 1
                print(f"  FAIL {type(e).__name__}: {str(e)[:120]}")

        if len(items) < 100:
            break
        cursor = items[-1].id

    print(f"\n{'(dry-run) would update' if args.dry_run else 'updated'}={updated}  skipped={skipped}  failed={failed}")


if __name__ == "__main__":
    main()
