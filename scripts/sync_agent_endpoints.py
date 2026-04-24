#!/usr/bin/env python3
"""同步所有 Letta agent 的 llm_config.model_endpoint + model 到当前 vLLM 配置。

踩坑记录（2026-04-17 晚）：
  - agent 的 llm_config 是**建 agent 时 snapshot**，不会自动跟随 .env 变化
  - 当临港 vLLM job ID 变化后，adapter 的 .env 更新了，但 agent 还指向旧 URL
  - 症状：letta-* 聊天全部 500 "no available server" 或 403 "无该推理任务的访问权限"
  - 外加坑：Letta 使用 `OPENAI_API_KEY` env（不是 VLLM_API_KEY）做 openai 兼容调用

2026-04-24 扩展：vLLM 换模型（Qwen3.5-122B-A10B → Kimi-K2.6）也得同步 model 字段，
  否则 agent 还按旧 model 名请求，新 vLLM 直接 404 NotFoundError。

用法（vLLM endpoint/model 换新时）：
  1. 改 .env 的 VLLM_ENDPOINT + VLLM_API_KEY + OPENAI_API_KEY (+ VLLM_MODEL 可选)
  2. `docker compose up -d letta-server adapter`（recreate 才能重载 env）
  3. `docker exec teleai-adapter python /app/scripts/sync_agent_endpoints.py --model Kimi-K2.6`
  4. 立刻打一次 letta-* 聊天验活

  干跑（不改数据）：加 --dry-run
  只改 endpoint 不改 model：不传 --model
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from routing import letta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("VLLM_ENDPOINT"),
                    help="新 endpoint；默认从 VLLM_ENDPOINT env 读")
    ap.add_argument("--model", default=os.environ.get("VLLM_MODEL"),
                    help="新 model 名（如 Kimi-K2.6）；不传则不改 model")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.endpoint:
        print("ERROR: 需要 VLLM_ENDPOINT env 或 --endpoint", file=sys.stderr)
        sys.exit(2)

    print(f"target endpoint: {args.endpoint}")
    print(f"target model:    {args.model or '(unchanged)'}")
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
            cur_endpoint = getattr(lc, "model_endpoint", None)
            cur_model = getattr(lc, "model", None)

            need_endpoint = cur_endpoint != args.endpoint
            need_model = args.model is not None and cur_model != args.model
            if not (need_endpoint or need_model):
                skipped += 1
                continue

            changes = []
            if need_endpoint:
                changes.append(f"endpoint {cur_endpoint} → {args.endpoint}")
            if need_model:
                changes.append(f"model {cur_model} → {args.model}")
            print(f"agent {a.id} ({a.name[:40]}): {'; '.join(changes)}")

            if args.dry_run:
                updated += 1
                continue

            try:
                new_lc = lc.model_dump() if hasattr(lc, "model_dump") else dict(lc)
                if need_endpoint:
                    new_lc["model_endpoint"] = args.endpoint
                if need_model:
                    new_lc["model"] = args.model
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
