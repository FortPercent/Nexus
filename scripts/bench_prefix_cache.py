#!/usr/bin/env python3
"""vLLM prefix caching 验证：
- 组 A：10 次不同的随机 prompt (baseline，无缓存)
- 组 B：10 次 "相同长前缀 + 不同尾巴" (测缓存命中)
- 对比 TTFT 分布
"""
import json, time, urllib.request, statistics
import random

VLLM = "http://116.238.240.2:32002/job-5c2ba1cbb8e4-20260417081320/v1/completions"
AUTH = "Bearer 6GPt1QUNbe8UHHOE_jtMdaTguZO5M5Uk"

# 一个长的"系统/角色"前缀（~200 tokens，模拟真实系统 prompt）
LONG_PREFIX = (
    "你是 TeleAI Nexus 的智能助手，一个为中国电信 AI 研究院团队内部使用的"
    "多功能助手。你会根据用户的问题提供准确、简洁的回答。在回答时请注意：\n"
    "1. 保持专业但亲切的语气\n"
    "2. 如果涉及代码，使用正确的语法和最佳实践\n"
    "3. 对于不确定的信息，明确说明\n"
    "4. 优先使用中文回答，除非用户明确要求英文\n"
    "5. 涉及敏感话题时保持中立客观\n\n"
    "团队成员包括算法工程师、AI 基础设施工程师、产品经理等。"
    "本次对话的上下文：用户正在使用系统进行日常工作咨询。\n\n"
    "用户的问题是："
)

DIFFERENT_QUESTIONS = [
    "如何设计一个分布式缓存系统？",
    "介绍 Transformer 架构",
    "什么是 RAG",
    "Python 装饰器怎么写",
    "Kubernetes 的 Pod 是什么",
    "讲讲 LSM-tree",
    "TCP 三次握手",
    "Redis 持久化",
    "Linux inode 是什么",
    "给我一个 SQL 优化建议",
]

def call(prompt):
    t0 = time.perf_counter()
    first_tok_t = None
    body = {"model": "Qwen3.5-122B-A10B", "prompt": prompt, "max_tokens": 100,
            "temperature": 0.7, "stream": True}
    data = json.dumps(body).encode()
    req = urllib.request.Request(VLLM, data=data,
                                 headers={"Content-Type": "application/json", "Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=60) as r:
        for line in r:
            line = line.decode()
            if not line.startswith("data: "): continue
            payload = line[6:].strip()
            if payload == "[DONE]": break
            try: j = json.loads(payload)
            except: continue
            choices = j.get("choices") or []
            if not choices: continue
            text = choices[0].get("text") or ""
            if text and first_tok_t is None:
                first_tok_t = time.perf_counter() - t0
    return first_tok_t

def bench(name, prompts):
    # warmup
    try: call(prompts[0])
    except: pass
    ttfts = []
    for p in prompts:
        try:
            t = call(p)
            if t is not None: ttfts.append(t)
        except Exception as e:
            print(f"  err: {e}")
    ttfts.sort()
    median = statistics.median(ttfts) if ttfts else 0
    print(f"{name}: n={len(ttfts)} TTFT p50={median*1000:.0f}ms  p95={ttfts[int(len(ttfts)*0.95)]*1000:.0f}ms  "
          f"mean={statistics.mean(ttfts)*1000:.0f}ms  min={ttfts[0]*1000:.0f}ms")

# 组 A: 完全不同的 prompt (模拟无缓存基线)
set_a = [f"{random.randint(1000,9999)}-" + q for q in DIFFERENT_QUESTIONS * 2]
# 组 B: 相同长前缀 + 不同尾 (应命中 prefix cache)
set_b = [LONG_PREFIX + q for q in DIFFERENT_QUESTIONS * 2]
# 组 C: 完全相同的 prompt (连续调用同一 prompt，最强缓存命中)
set_c = [LONG_PREFIX + DIFFERENT_QUESTIONS[0]] * 20

print("=== A: 随机不同 prompt（无共享前缀）===")
bench("A", set_a)
print("=== B: 长共享前缀 + 不同尾巴（应命中 prefix cache）===")
bench("B", set_b)
print("=== C: 完全相同 prompt（强缓存）===")
bench("C", set_c)
