"""LLM 抽取 helpers — 决策 / fact / preference / event。

W3 阶段只实现 extract_decisions。封装 vLLM guided_json 调用 + Pydantic schema,
让上层 memory_api 端点逻辑干净。

设计注意:
- 用 vLLM 顶层 guided_json (Kimi 是 thinking 模型,普通 response_format=json_object
  会让答案全跑去 reasoning 字段, content 为空)
- max_tokens 按预期产出动态算, 但留 4K 下限 / 16K 上限,保护 GPU 公平调度
- Kimi 偶发把 JSON 包 ```json ... ```, 进入 _strip_md_fence 兜底
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

import httpx
from pydantic import BaseModel, Field

from config import VLLM_ENDPOINT, VLLM_API_KEY


# ---------- Decision schema ----------

class Decision(BaseModel):
    """一条决策的结构化表达,字段对齐 decisions 表。"""
    content: str = Field(description="决策一句话陈述, ≤60 字")
    owner: Optional[str] = Field(default=None, description="负责人, 文本未明示则 null")
    decided_at: Optional[str] = Field(default=None, description="决策日期 YYYY-MM-DD, 未明示则 null")
    deadline: Optional[str] = Field(default=None, description="落地截止日 YYYY-MM-DD, 未明示则 null")
    rationale: Optional[str] = Field(default=None, description="理由, 文本未明示则 null")
    status: str = Field(default="proposed", description="proposed / approved / executing / done / reverted")


class DecisionList(BaseModel):
    """guided_json 顶层 wrapper。"""
    decisions: List[Decision]


# ---------- prompt ----------

_SYSTEM_INSTRUCTIONS = """你是企业会议纪要的决策抽取器。给定一段会议纪要 / 对话文本,
从中抽取所有"决策项"(decision):明确产生了行动 / 选择 / 责任分配的语句。

要求:
1. 只抽真正的决策, 不抽讨论 / 提议 / 待评估项
2. owner 必须是文本中明确提到的人(姓名 / 工号 / 邮箱), 不要推测
3. deadline 只在文本明示截止时填; 含糊措辞("尽快","近期")一律 null
4. rationale 只在文本给出原因时填, 不要编造
5. 每条决策的 content 用一句陈述句, 不超过 60 字

输出 JSON, 顶层结构 {"decisions": [<decision>, ...]}, 每条 decision 字段:
- content   (str, 必填): 决策一句话陈述
- owner     (str|null):  负责人, 文本未明示则 null
- decided_at(str|null):  决策日期 YYYY-MM-DD, 文本未明示则 null
- deadline  (str|null):  截止日 YYYY-MM-DD, 文本未明示则 null
- rationale (str|null):  理由, 文本未明示则 null
- status    (str):       proposed / approved / executing / done / reverted, 默认 proposed

只输出 JSON, 不要任何前后说明文字。"""


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_md_fence(s: str) -> str:
    """Kimi / Qwen 经常把 JSON 包进 ```json ... ``` 里,这里剥掉。"""
    m = _FENCE_RE.match(s)
    return m.group(1) if m else s


def _calc_max_tokens(input_tokens_est: int, expected_decisions: int = 10) -> int:
    """动态算 max_tokens:
    - 单决策 ~150 tokens, thinking 是 output 的 2-3 倍
    - 至少 4K 让 thinking 跑得开
    - 至多 16K (硬上限,保护 GPU 公平调度)
    - 不超 (32K - input - 500 buffer)
    """
    expected_output = max(4096, expected_decisions * 450)
    model_remaining = 32768 - input_tokens_est - 500
    return max(2048, min(16384, expected_output, model_remaining))


# ---------- API ----------

async def extract_decisions(
    messages: List[dict],
    *,
    model: str = "Kimi-K2.6",
    expected_decisions: int = 10,
    timeout: float = 600.0,
) -> List[Decision]:
    """从 OpenAI 格式 messages 抽决策。

    messages: [{"role": "user|assistant|system", "content": "..."}, ...]
    expected_decisions: 估计批量, 决定 max_tokens, 默认 10
    """
    text = "\n\n".join(
        f"[{m.get('role', 'msg')}] {m.get('content', '')}" for m in messages
    )
    # 粗估 input tokens (中文按 ~2 字/token, 英文按 ~4 字/token, 折中 3)
    input_tokens_est = (len(_SYSTEM_INSTRUCTIONS) + len(text)) // 3

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_INSTRUCTIONS},
            {"role": "user", "content": f"以下是待抽取的文本:\n\n---\n{text}\n---\n\n请抽取所有决策项。"},
        ],
        "temperature": 0.1,
        "max_tokens": _calc_max_tokens(input_tokens_est, expected_decisions),
        "guided_json": DecisionList.model_json_schema(),
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{VLLM_ENDPOINT}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
        )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"].get("content") or ""
    if not content:
        raise RuntimeError(
            f"vLLM content 为空 (Kimi thinking 模型在 guided_json 模式下不应空), "
            f"message keys={list(data['choices'][0]['message'].keys())}"
        )
    parsed = json.loads(_strip_md_fence(content))
    if isinstance(parsed, list):
        parsed = {"decisions": parsed}
    return DecisionList(**parsed).decisions
