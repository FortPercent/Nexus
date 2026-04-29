"""W3-1 PoC:从对话/会议纪要文本里抽取结构化决策。

调用部署中的 vLLM (Kimi-K2.6) + JSON 模式, 用 Pydantic 校验输出。

用法:
    python scripts/poc_decision_extract.py                    # 用默认 sample 文本
    python scripts/poc_decision_extract.py --text "..."       # 自定义文本
    python scripts/poc_decision_extract.py --file path.md     # 从文件读

成功标准:
    - 返回 JSON 列表, 每项 schema 正确
    - 至少抽出文本里"明显决策" 的 70%
    - 字段(content/owner/deadline/rationale)可解析
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

import requests
from pydantic import BaseModel, Field, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import VLLM_ENDPOINT, VLLM_API_KEY


# ---------- Decision schema ----------

class Decision(BaseModel):
    """一条决策的结构化表达。

    字段对齐 Nexus W3 决策追溯 use case:谁在何时为什么做了 X 决定 + deadline。
    """
    content: str = Field(description="决策的核心内容,一句话陈述")
    owner: Optional[str] = Field(default=None, description="决策主负责人,姓名或邮箱;若文本未明确则 null")
    decided_at: Optional[str] = Field(default=None, description="决策日期 YYYY-MM-DD;若文本未明确则 null")
    deadline: Optional[str] = Field(default=None, description="落地截止日 YYYY-MM-DD;若无截止则 null")
    rationale: Optional[str] = Field(default=None, description="为什么这么决定,一句话理由;若文本未提则 null")
    status: str = Field(default="proposed", description="proposed / approved / executing / done / reverted")


class DecisionList(BaseModel):
    """guided_json 顶层 schema 用 list 包装,vLLM 部分实现对裸 list 不友好。"""
    decisions: List[Decision]


# ---------- prompt ----------

EXTRACT_PROMPT_SYSTEM = """你是企业会议纪要的决策抽取器。给定一段会议纪要 / 对话文本,
从中抽取所有"决策项"(decision):明确产生了行动 / 选择 / 责任分配的语句。

要求:
1. 只抽真正的决策, 不抽讨论 / 提议 / 待评估项
2. owner 必须是文本中明确提到的人(姓名 / 工号 / 邮箱), 不要推测
3. deadline 只在文本明示截止时填; 含糊措辞("尽快","近期")一律 null
4. rationale 只在文本给出原因时填, 不要编造
5. 每条决策的 content 用一句陈述句, 不超过 60 字

输出严格遵守如下 JSON schema (放在 {"decisions": [...]} 顶层):
{schema}

只输出 JSON,不要任何前后说明文字。"""


EXTRACT_PROMPT_USER = """以下是待抽取的文本:

---
{text}
---

请抽取所有决策项。"""


# ---------- sample text ----------

DEFAULT_SAMPLE = """\
2026 年 4 月 25 日, AI Infra 周会纪要

参会人:王立伟、陈银、翁祈桢、吴煊佴

讨论事项:

1. 推理底座选择。经讨论, 决定采用 vLLM + Kimi-K2.6 作为生产推理底座,
   由王立伟负责 5 月 10 日前完成切换。理由:Kimi-K2.6 在中文 benchmark 上
   比之前的 Qwen2.5 高 8 个点, 且推理速度提升 30%。

2. 安全管理项目交接。陈银接手安全管理项目, 5 月 1 日前完成与翁祈桢的交接。
   原因:翁祈桢调岗到具身智能项目, 变更负责人。

3. 知识库治理 Sprint 1 上线时间。决定 5 月 6 日上线 Nexus 2.0 治理模块
   (trace + conflict + 决策追溯), 由吴煊佴负责。

4. 算力配额。提议把 ai-infra 项目的 GPU 配额从 4 卡升到 8 卡, 但需要先评估
   现有使用率。下次会议讨论。

5. 飞书会议纪要自动同步问题。讨论是否要做 connector, 暂不决定。
"""


# ---------- runner ----------

def call_vllm(text: str, *, model: str = "Kimi-K2.6", use_guided_json: bool = False) -> dict:
    """调 vLLM 抽决策。返回原始 response JSON。"""
    schema = DecisionList.model_json_schema()

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": EXTRACT_PROMPT_SYSTEM.format(schema=json.dumps(schema, ensure_ascii=False))},
            {"role": "user", "content": EXTRACT_PROMPT_USER.format(text=text)},
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
        # 禁用 thinking,让模型直接给结构化答案
        "chat_template_kwargs": {"enable_thinking": False},
    }

    if use_guided_json:
        body["extra_body"] = {"guided_json": schema}
    else:
        body["response_format"] = {"type": "json_object"}

    resp = requests.post(
        f"{VLLM_ENDPOINT}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def extract(text: str) -> List[Decision]:
    raw = call_vllm(text)
    content = raw["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    # 容忍两种顶层形态:{"decisions":[...]} 或 [...]
    if isinstance(parsed, list):
        parsed = {"decisions": parsed}
    return DecisionList(**parsed).decisions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", help="直接传文本")
    parser.add_argument("--file", help="从文件读文本")
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    else:
        text = DEFAULT_SAMPLE

    print("=" * 60)
    print("INPUT:")
    print(text[:300] + ("..." if len(text) > 300 else ""))
    print("=" * 60)

    try:
        decisions = extract(text)
    except ValidationError as e:
        print(f"⚠️ schema validation failed: {e}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        print(f"⚠️ JSON parse failed: {e}", file=sys.stderr)
        sys.exit(3)
    except requests.HTTPError as e:
        print(f"⚠️ vLLM error: {e}\n{e.response.text[:500]}", file=sys.stderr)
        sys.exit(4)

    print(f"\nEXTRACTED {len(decisions)} decision(s):\n")
    for i, d in enumerate(decisions, 1):
        print(f"  [{i}] {d.content}")
        print(f"      owner    = {d.owner}")
        print(f"      decided  = {d.decided_at}")
        print(f"      deadline = {d.deadline}")
        print(f"      status   = {d.status}")
        if d.rationale:
            print(f"      理由     = {d.rationale}")
        print()


if __name__ == "__main__":
    main()
