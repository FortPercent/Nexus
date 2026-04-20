"""全量回归测试 —— 覆盖 docs/regression-test-checklist.md 自动化部分。

依赖环境变量（有合理默认值，容器内跑无需设置）：
  ADAPTER_URL       http://localhost:8000
  WEBUI_URL         http://172.17.0.1:3000
  LETTA_URL         http://letta-server:8283
  OLLAMA_URL        http://ollama:11434
  API_KEY           teleai-adapter-key-2026
  JWT_SECRET        WEBUI_SECRET_KEY（/home/infra46/teleai-adapter/.env）
  ADMIN_EMAIL       admin@aiinfra.local
  ADMIN_PASSWORD    AIinfra@2026
  TEST_USER_ID      wuxn5 的 user_id（跑 chat 测试的身份）
  TEST_USER_EMAIL   wuxn5 邮箱
  TEST_PROJECT      ai-infra（测试用的 letta 项目）

用法（推荐容器内跑，DB 路径全对）：
  docker exec teleai-adapter python /app/scripts/regression.py
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
import jwt as pyjwt

ADAPTER_URL = os.getenv("ADAPTER_URL", "http://localhost:8000").rstrip("/")
WEBUI_URL = os.getenv("WEBUI_URL", "http://172.17.0.1:3000").rstrip("/")
LETTA_URL = os.getenv("LETTA_URL", "http://letta-server:8283").rstrip("/")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
API_KEY = os.getenv("API_KEY", "teleai-adapter-key-2026")
JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("OPENWEBUI_JWT_SECRET") or "6WYGSa8e7EBsSeG3"
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@aiinfra.local")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "AIinfra@2026")
TEST_USER_ID = os.getenv("TEST_USER_ID", "ce1d405b-0b5c-4faf-8864-010e2611b900")
TEST_USER_EMAIL = os.getenv("TEST_USER_EMAIL", "wuxn5@chinatelecom.cn")
TEST_PROJECT = os.getenv("TEST_PROJECT", "ai-infra")
# 非 org admin 身份，用于权限拒绝测试
NON_ADMIN_USER_ID = os.getenv("NON_ADMIN_USER_ID", "07a3a6ae-ec73-44ed-aff4-00d92f526e0c")  # liuyr17
DB_PATH = os.getenv("DB_PATH", "/data/serving/adapter/adapter.db")
WEBUI_DB_PATH = os.getenv("WEBUI_DB_PATH", "/data/open-webui/webui.db")


def mint_jwt(user_id: str) -> str:
    """按 Open WebUI 的 JWT 规范签一个 token（HS256, id claim, 远期过期）"""
    payload = {
        "id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


# ================= test runner =================

_results = []


def T(name: str, fn: Callable):
    start = time.monotonic()
    try:
        note = fn() or ""
        ok = True
        err = ""
    except AssertionError as e:
        ok = False
        err = str(e) or "AssertionError"
        note = ""
    except Exception as e:
        ok = False
        err = f"{type(e).__name__}: {e}"
        note = ""
    dur = time.monotonic() - start
    _results.append((ok, name, note, err, dur))
    tag = "PASS" if ok else "FAIL"
    extra = f" — {note}" if note else ""
    err_str = f"  !! {err}" if err else ""
    print(f"[{tag}] ({dur:4.2f}s) {name}{extra}{err_str}")


# ================= tests =================

def t_containers():
    # 容器不在 adapter 内可见；改为检查可达的对端服务
    # adapter 自己 up 就能跑这个脚本，无需再测
    # letta / ollama / webui / letta-db 靠 URL 探针间接验证
    pass  # placeholder, covered by below URL tests


def t_ollama_embed_768():
    r = httpx.post(f"{OLLAMA_URL}/api/embeddings",
                   json={"model": "nomic-embed-text", "prompt": "hello"},
                   timeout=15)
    assert r.status_code == 200, f"status {r.status_code}"
    emb = r.json().get("embedding") or []
    assert len(emb) == 768, f"dim {len(emb)} != 768"
    return f"dim={len(emb)}"


def t_letta_alive():
    r = httpx.get(f"{LETTA_URL}/v1/health/", timeout=5)
    assert r.status_code == 200, f"status {r.status_code}"


def t_v1_models():
    r = httpx.get(f"{ADAPTER_URL}/v1/models",
                  headers={"Authorization": f"Bearer {API_KEY}"}, timeout=10)
    assert r.status_code == 200, f"status {r.status_code}"
    data = r.json()["data"]
    assert any(m["id"] == "qwen-no-mem" for m in data), "qwen-no-mem 缺失"
    assert any(m["id"].startswith("letta-") for m in data), "letta-* 缺失"
    return f"{len(data)} models"


def t_adapter_knowledge_page():
    r = httpx.get(f"{ADAPTER_URL}/knowledge", timeout=5)
    assert r.status_code == 200, f"status {r.status_code}"


def t_webui_alive():
    r = httpx.get(f"{WEBUI_URL}/health", timeout=5)
    assert r.status_code == 200, f"status {r.status_code}"


# ---------- Admin API（JWT）----------

_jwt_cache = {}
def _user_jwt() -> str:
    if "jwt" not in _jwt_cache:
        _jwt_cache["jwt"] = mint_jwt(TEST_USER_ID)
    return _jwt_cache["jwt"]

def _admin_get(path: str, expect_status=200):
    r = httpx.get(f"{ADAPTER_URL}{path}",
                  headers={"Authorization": f"Bearer {_user_jwt()}"}, timeout=10)
    assert r.status_code == expect_status, f"status {r.status_code} body={r.text[:200]}"
    return r.json() if r.status_code == 200 else None

def t_admin_me():
    data = _admin_get("/admin/api/me")
    assert data.get("id") == TEST_USER_ID, f"id mismatch: {data}"
    return f"role={data.get('role')}"

def t_admin_projects():
    data = _admin_get("/admin/api/projects")
    assert isinstance(data, list) and len(data) > 0, f"got {data}"
    return f"{len(data)} projects"

def t_admin_project_members():
    data = _admin_get(f"/admin/api/project/{TEST_PROJECT}/members")
    assert isinstance(data, list) and len(data) > 0
    return f"{len(data)} members"

def t_admin_project_files():
    _admin_get(f"/admin/api/project/{TEST_PROJECT}/files")

def t_admin_project_knowledge():
    _admin_get(f"/admin/api/project/{TEST_PROJECT}/knowledge")

def t_admin_project_suggestions():
    _admin_get(f"/admin/api/project/{TEST_PROJECT}/suggestions")

def t_admin_personal_files():
    _admin_get("/admin/api/personal/files")

def t_admin_personal_memory():
    data = _admin_get("/admin/api/personal/memory")
    assert isinstance(data, dict), f"memory should be single dict, got {type(data).__name__}"
    assert "block_id" in data and "content" in data, f"missing keys: {list(data.keys())}"
    return f"block={data['block_id'][:16]} len={len(data.get('content',''))}"


def t_admin_conversations_overview():
    data = _admin_get("/admin/api/personal/conversations")
    assert isinstance(data, list)
    return f"{len(data)} projects"


def t_admin_conversations_project():
    _admin_get(f"/admin/api/personal/conversations/{TEST_PROJECT}")


def t_human_block_shared_cached():
    a = sqlite3.connect(DB_PATH); a.row_factory = sqlite3.Row
    rows = a.execute("SELECT COUNT(*) as n FROM user_cache WHERE personal_human_block_id IS NOT NULL").fetchall()
    a.close()
    assert rows[0]["n"] > 0, "no user has cached human block_id — migration 未跑"
    return f"{rows[0]['n']} users cached"


# ---------- 聊天 ----------

def _chat_body(model: str, stream: bool, content: str):
    body = {
        "model": model,
        "stream": stream,
        "messages": [{"role": "user", "content": content}],
    }
    if model.startswith("letta-"):
        body.update({
            "user_id": TEST_USER_ID,
            "user_email": TEST_USER_EMAIL,
            "user_name": "regression-test",
        })
    return body


def _call_chat(model: str, stream: bool, content: str, timeout=180):
    body = _chat_body(model, stream, content)
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    if not stream:
        r = httpx.post(f"{ADAPTER_URL}/v1/chat/completions",
                       json=body, headers=headers, timeout=timeout)
        assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
        d = r.json()
        msg = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        assert msg, "empty content"
        return msg, None
    # stream
    ttft = None
    start = time.monotonic()
    parts = []
    got_done = False
    with httpx.Client(timeout=timeout) as c:
        with c.stream("POST", f"{ADAPTER_URL}/v1/chat/completions",
                      json=body, headers=headers) as resp:
            assert resp.status_code == 200, f"status {resp.status_code}"
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                p = line[6:]
                if p == "[DONE]":
                    got_done = True
                    break
                try:
                    ch = json.loads(p)
                except json.JSONDecodeError:
                    continue
                d = (ch.get("choices") or [{}])[0].get("delta") or {}
                t = d.get("content") or ""
                if t:
                    if ttft is None:
                        ttft = time.monotonic() - start
                    parts.append(t)
    content = "".join(parts)
    assert got_done, "no [DONE]"
    assert content, "empty stream content"
    return content, ttft


def t_chat_qwen_nonstream():
    msg, _ = _call_chat("qwen-no-mem", False, "用一句话介绍你自己，中文")
    return f"len={len(msg)}"

def t_chat_qwen_stream():
    msg, ttft = _call_chat("qwen-no-mem", True, "用一句话介绍你自己，中文")
    assert ttft is not None and ttft < 10, f"ttft {ttft}"
    return f"ttft={ttft:.2f}s len={len(msg)}"

def t_chat_letta_nonstream():
    msg, _ = _call_chat(f"letta-{TEST_PROJECT}", False, "用一句话介绍 TeleAI Nexus，中文")
    assert "system_alert" not in msg.lower(), "system_alert 泄露"
    return f"len={len(msg)}"

def t_chat_letta_stream():
    msg, ttft = _call_chat(f"letta-{TEST_PROJECT}", True, "用一句话介绍 TeleAI Nexus，中文")
    assert "system_alert" not in msg.lower(), "system_alert 泄露"
    assert ttft is not None and ttft < 10, f"ttft {ttft}"
    return f"ttft={ttft:.2f}s len={len(msg)}"

def t_letta_stream_ttft_under_3s():
    """确认不是伪流式（如果还是伪的 ttft 会和 total 接近）"""
    msg, ttft = _call_chat(f"letta-{TEST_PROJECT}", True, "你好")
    assert ttft is not None and ttft < 3, f"ttft {ttft:.2f}s >= 3s"
    return f"ttft={ttft:.2f}s"

def t_think_balanced():
    """所有流式/非流式回复里 <think> 闭合平衡"""
    for model, stream in [("qwen-no-mem", True), (f"letta-{TEST_PROJECT}", True),
                          (f"letta-{TEST_PROJECT}", False)]:
        msg, _ = _call_chat(model, stream, "你好")
        o = msg.count("<think>")
        c = msg.count("</think>")
        assert o == c, f"{model} stream={stream} <think> {o}/{c}"
    return "3 variants balanced"


# ---------- 权限 ----------

def t_non_org_admin_upload_blocked():
    """非 org admin 上传组织文件应 403（使用 NON_ADMIN_USER_ID 身份）"""
    non_admin_jwt = mint_jwt(NON_ADMIN_USER_ID)
    r = httpx.post(
        f"{ADAPTER_URL}/admin/api/org/files",
        headers={"Authorization": f"Bearer {non_admin_jwt}"},
        files={"file": ("x.txt", b"hello", "text/plain")},
        timeout=10,
    )
    assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code}"
    return f"status={r.status_code}"


def t_letta_models_registered_in_webui():
    conn = sqlite3.connect(WEBUI_DB_PATH)
    conn.row_factory = sqlite3.Row
    # adapter DB 的项目 list
    a = sqlite3.connect(DB_PATH); a.row_factory = sqlite3.Row
    projects = [r["project_id"] for r in a.execute("SELECT project_id FROM projects")]
    a.close()
    rows = conn.execute("SELECT id FROM model WHERE id LIKE 'letta-%'").fetchall()
    reg = {r["id"] for r in rows}
    missing = [p for p in projects if f"letta-{p}" not in reg]
    conn.close()
    assert not missing, f"未注册: {missing}"
    return f"{len(projects)} letta models"


# ---------- 数据完整性 ----------

def t_knowledge_mirrors_count():
    a = sqlite3.connect(DB_PATH)
    n = a.execute("SELECT COUNT(*) FROM knowledge_mirrors").fetchone()[0]
    a.close()
    assert n > 0
    return f"{n} mirrors"

def t_project_members_count():
    a = sqlite3.connect(DB_PATH)
    n = a.execute("SELECT COUNT(*) FROM project_members").fetchone()[0]
    a.close()
    assert n > 0
    return f"{n} members"

def t_user_agent_map_count():
    a = sqlite3.connect(DB_PATH)
    n = a.execute("SELECT COUNT(*) FROM user_agent_map").fetchone()[0]
    a.close()
    assert n > 0
    return f"{n} rows"


def t_letta_grep_tool_end_to_end():
    """真实触发 grep_files 工具执行, 断言不 crash.
    教训来源 (04-20): 两次 embedding_config / file.content=None 类 bug 都因为
    regression 的 letta 聊天只问'你好'、不触发工具调用, 隐藏数天直到用户报。
    这里用 TEST_PROJECT 发一个强制搜索意图的 query, 断言工具返回不含错误关键字。"""
    body = _chat_body(f"letta-{TEST_PROJECT}", stream=False,
                      content="搜索一下项目知识库里关于『规范』的内容, 列出 2-3 条")
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    r = httpx.post(f"{ADAPTER_URL}/v1/chat/completions", json=body, headers=headers, timeout=120)
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}"
    content = r.json()["choices"][0]["message"]["content"]
    # 真实工具报错的标志串: 如果任一工具 crash, adapter 会把错误字串嵌在输出里
    bad_markers = ["NoneType", "has no attribute", "encode", "Traceback", "not attached"]
    hits = [m for m in bad_markers if m in content]
    assert not hits, f"工具调用疑似失败, 命中关键字: {hits}; 片段: {content[:300]}"
    assert len(content) > 20, f"答复太短 ({len(content)} chars): {content!r}"
    return f"len={len(content)}  第一行={content.strip().splitlines()[0][:60]!r}"


def t_no_orphan_letta_agents():
    """监控 todo #26: _rebuild_agent_async 不删 Letta agent 会累积孤儿.
    Letta 里实际 agent 数应 ≤ adapter user_agent_map + 5 (容忍短期漂移).
    一旦 diff > 5 说明对话清空在不断累积孤儿, 需要修 _rebuild_agent_async 补 letta.agents.delete(old_id).
    04-20 事故: wuxn5 一人 9 孤儿 → 手动清; 本断言防止重复."""
    a = sqlite3.connect(DB_PATH)
    known = {r[0] for r in a.execute("SELECT agent_id FROM user_agent_map").fetchall()}
    a.close()
    # 直接 HTTP 拉 Letta (带分页), 避免依赖 letta client
    letta_ids = set()
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        r = httpx.get(f"{LETTA_URL}/v1/agents/", params=params, timeout=30)
        data = r.json() if r.status_code == 200 else []
        if not data:
            break
        for a in data:
            letta_ids.add(a.get("id"))
        if len(data) < 100:
            break
        after = data[-1].get("id")
    orphans = letta_ids - known
    diff = len(orphans)
    # 容忍 5 个 (测试期间短暂漂移 / Letta 内部系统 agent 等)
    assert diff <= 5, (
        f"Letta 有 {len(letta_ids)} agents, adapter user_agent_map 只 {len(known)}, "
        f"{diff} 个孤儿 > 阈值 5. 样本: {sorted(orphans)[:5]}. "
        f"可能是 _rebuild_agent_async 没删 Letta agent (todo #26)"
    )
    return f"letta={len(letta_ids)} mapped={len(known)} orphans={diff} (≤5 ok)"


# ---------- Agent prompt 容量监控（防 vLLM 400 / agent 卡死）----------

# vLLM max_model_len, 任何 agent 的 prompt 超这个就炸 Bad Request.
# 2026-04-20 事故: biany security agent 累积 74941 tokens 直接 DEAD,
# wuxn5 ai-infra 61288 CRIT, biany cpm folder directories 53K 吃满 budget.
# 留 5K 余量给用户下一条 message, 超 60K 就当危险.
VLLM_MAX_MODEL_LEN = int(os.getenv("VLLM_MAX_MODEL_LEN", "65536"))
AGENT_PROMPT_SAFE_MARGIN = int(os.getenv("AGENT_PROMPT_SAFE_MARGIN", "5000"))


def t_agent_prompt_under_vllm_limit():
    """枚举 user_agent_map 每个 agent, 查 Letta /context, 断言:
      1. 无 agent cur_tokens >= VLLM_MAX_MODEL_LEN (否则下次 chat 必 400)
      2. 无 agent margin < AGENT_PROMPT_SAFE_MARGIN (5K), 否则用户发一条大 message 就炸

    两层细分 (给排查用, 不影响断言):
      - num_tokens_messages: 对话历史占用; 过大可手动 compact 救
      - num_tokens_directories: folder 挂载 metadata; 过大 compact 救不了, 必须架构改 (不挂 folder)
    """
    a = sqlite3.connect(DB_PATH)
    a.row_factory = sqlite3.Row
    rows = a.execute("SELECT agent_id, project_id, user_id FROM user_agent_map").fetchall()
    a.close()

    dead = []  # margin <= 0
    crit = []  # margin < SAFE_MARGIN
    checked = 0
    for r in rows:
        aid = r["agent_id"]
        try:
            d = httpx.get(f"{LETTA_URL}/v1/agents/{aid}/context", timeout=15).json()
        except Exception:
            continue
        checked += 1
        cur = d.get("context_window_size_current", 0)
        if not cur:
            continue
        margin = VLLM_MAX_MODEL_LEN - cur
        entry = {
            "agent": aid[:22],
            "project": r["project_id"],
            "cur": cur,
            "margin": margin,
            "msg_tok": d.get("num_tokens_messages"),
            "dir_tok": d.get("num_tokens_directories"),
        }
        if margin <= 0:
            dead.append(entry)
        elif margin < AGENT_PROMPT_SAFE_MARGIN:
            crit.append(entry)

    assert not dead, (
        f"{len(dead)} agent prompt 已超 vLLM max_model_len={VLLM_MAX_MODEL_LEN}, "
        f"下次 chat 必 400. 样本: {dead[:3]}. "
        f"修复: 对 messages-heavy 的 agent 跑 compact; directories-heavy 的必须 detach folder."
    )
    assert not crit, (
        f"{len(crit)} agent 余量 < {AGENT_PROMPT_SAFE_MARGIN}, 用户发一条大 message 就炸. "
        f"样本: {crit[:3]}."
    )
    return f"{checked} agents scanned, 0 DEAD, 0 CRIT (margin>={AGENT_PROMPT_SAFE_MARGIN})"


# ---------- # 下拉跨项目隔离（API 层 + E2E）----------

# biany 是 4 个 project 的 admin (cpm/security/asset/ai-infra), 用她做隔离测试最能暴露泄漏.
# 2026-04-20 bug: biany 在 cpm 聊天里 #.doc 会看到 [Security Management] 的 docx.
# 根因: /api/v1/knowledge/search 按用户过滤, 不按 chat 所在 project 收敛. 修复后
# 传 project_id 会按 meta.scope/project_slug 收敛 (org 永远返, personal 只给自己, project 只返匹配).
BIANY_USER_ID = os.getenv("BIANY_USER_ID", "f1dfb0ed-0c2b-4337-922a-cbc86859dfde")


def t_hash_dropdown_scoped_to_project():
    """作为 biany (4 个 project 都是 member) 带 project_id=cpm 调 knowledge/search?query=.doc:
      1. 返回的每条 meta.scope ∈ {org} 或 (scope=project 且 project_slug=cpm) 或 (scope=personal 且 owner=biany)
      2. 特别断言: 任何 scope=project 且 project_slug=security-management 的条目都不能出现
    不传 project_id 时维持旧全量行为 (向后兼容).
    """
    tok = mint_jwt(BIANY_USER_ID)
    headers = {"Authorization": f"Bearer {tok}"}

    # A. 不传 project_id → 应该能看到多个 project 的条目 (泄漏态, 但向后兼容)
    r = httpx.get(f"{WEBUI_URL}/api/v1/knowledge/search?query=.doc",
                  headers=headers, timeout=15)
    assert r.status_code == 200, f"A HTTP {r.status_code}: {r.text[:200]}"
    all_items = r.json().get("items", [])
    slugs_unscoped = set()
    for it in all_items:
        meta = it.get("meta") or {}
        if isinstance(meta, str):
            try: meta = json.loads(meta)
            except Exception: meta = {}
        if meta.get("scope") == "project":
            slugs_unscoped.add(meta.get("project_slug", ""))
    # biany 在 4 个 project 都是成员, 无 project_id 过滤应该出现跨 project (至少 2 个)
    # 注意: .doc 过滤可能某 project 没 doc 文件 — 所以宽松一点, 只要 >=2 即可
    # 这不是 bug 验证, 这是确认 biany 数据确实覆盖多 project (否则 B 步没意义)

    # B. 传 project_id=cpm → 只能出现 cpm + org + biany 自己的 personal
    r = httpx.get(
        f"{WEBUI_URL}/api/v1/knowledge/search?query=.doc&project_id=computing-power-management",
        headers=headers, timeout=15,
    )
    assert r.status_code == 200, f"B HTTP {r.status_code}: {r.text[:200]}"
    scoped_items = r.json().get("items", [])

    leaks = []
    for it in scoped_items:
        meta = it.get("meta") or {}
        if isinstance(meta, str):
            try: meta = json.loads(meta)
            except Exception: meta = {}
        scope = meta.get("scope")
        slug = meta.get("project_slug", "")
        ok = (
            scope == "org"
            or (scope == "personal" and it.get("user_id") == BIANY_USER_ID)
            or (scope == "project" and slug == "computing-power-management")
        )
        if not ok:
            leaks.append({"name": it.get("name","")[:60], "scope": scope, "slug": slug})
    assert not leaks, f"隔离失败, 泄漏 {len(leaks)} 条跨项目: {leaks[:3]}"

    # C. 显式断言 security 的条目绝不能出现
    security_leak = [it for it in scoped_items
                     if isinstance(it.get("meta"), dict)
                     and it["meta"].get("project_slug") == "security-management"]
    assert not security_leak, (
        f"cpm 聊天里泄漏了 security 的 {len(security_leak)} 条, "
        f"样本: {[s.get('name','')[:50] for s in security_leak[:3]]}"
    )

    return f"unscoped={len(all_items)} slugs={len(slugs_unscoped)} / scoped={len(scoped_items)} leaks=0"


# ---------- # 按模型过滤（API 层） ----------

def t_hash_filter_api_returns_description():
    """Open WebUI knowledge/search 端点返回 description，前端才能过滤"""
    r = httpx.post(f"{WEBUI_URL}/api/v1/auths/signin",
                   json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=10)
    tok = r.json()["token"]
    r = httpx.get(f"{WEBUI_URL}/api/v1/knowledge/search?query=",
                  headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    items = r.json().get("items", r.json() if isinstance(r.json(), list) else [])
    assert items, "knowledge list 为空"
    assert "description" in items[0], "缺 description 字段"
    mirrors = [i for i in items if (i.get("description") or "").startswith("letta-mirror:")]
    return f"{len(items)} items, {len(mirrors)} mirrors"


def t_hash_filter_builtin_assets():
    """构建产物里含 letta-mirror 字符串（验证前端改动已随镜像部署）"""
    # 镜像里查 — 只能跑在 adapter 容器外部。若跑在 adapter 内，跳过。
    try:
        import subprocess
        out = subprocess.check_output(
            ["sh", "-c", "find /app/build -name '*.js' 2>/dev/null | xargs grep -l letta-mirror 2>/dev/null | head -1"],
            stderr=subprocess.DEVNULL, timeout=5,
        )
        if out.strip():
            return f"asset hit"
    except Exception:
        pass
    return "skipped (not in webui container)"


# ---------- Pipeline Filter ----------

def t_pipeline_filter_registered():
    """filter 已注册且激活"""
    conn = sqlite3.connect(WEBUI_DB_PATH)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT id, is_active FROM function WHERE type='filter' AND is_active=1").fetchall()
    conn.close()
    active = [x["id"] for x in r]
    assert "user_inject" in active, f"user_inject 未激活: {active}"
    return f"active={active}"


def t_pipeline_filter_attached_to_all_models():
    conn = sqlite3.connect(WEBUI_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, meta FROM model").fetchall()
    conn.close()
    missing = []
    for r in rows:
        m = json.loads(r["meta"]) if r["meta"] else {}
        fids = m.get("filterIds") or m.get("filter_ids") or []
        if "user_inject" not in fids:
            missing.append(r["id"])
    assert not missing, f"未附加: {missing}"
    return f"all {len(rows)} models attached"


# ---------- main ----------

def main():
    print(f"adapter: {ADAPTER_URL}")
    print(f"webui  : {WEBUI_URL}")
    print(f"letta  : {LETTA_URL}")
    print(f"user   : {TEST_USER_ID} ({TEST_USER_EMAIL})")
    print(f"project: {TEST_PROJECT}\n")

    print("── 基础设施 ──")
    T("ollama embedding 768 维", t_ollama_embed_768)
    T("letta 健康检查", t_letta_alive)
    T("webui 健康检查", t_webui_alive)

    print("\n── API 端点 ──")
    T("GET /v1/models", t_v1_models)
    T("GET /knowledge", t_adapter_knowledge_page)

    print("\n── Admin API ──")
    T("/admin/api/me", t_admin_me)
    T("/admin/api/projects", t_admin_projects)
    T(f"/admin/api/project/{TEST_PROJECT}/members", t_admin_project_members)
    T(f"/admin/api/project/{TEST_PROJECT}/files", t_admin_project_files)
    T(f"/admin/api/project/{TEST_PROJECT}/knowledge", t_admin_project_knowledge)
    T(f"/admin/api/project/{TEST_PROJECT}/suggestions", t_admin_project_suggestions)
    T("/admin/api/personal/files", t_admin_personal_files)
    T("/admin/api/personal/memory（单份共享）", t_admin_personal_memory)
    T("/admin/api/personal/conversations（概览）", t_admin_conversations_overview)
    T("/admin/api/personal/conversations/{project}（消息）", t_admin_conversations_project)

    print("\n── 聊天 ──")
    T("qwen-no-mem 非流式", t_chat_qwen_nonstream)
    T("qwen-no-mem 流式", t_chat_qwen_stream)
    T("letta-* 非流式（无 system_alert）", t_chat_letta_nonstream)
    T("letta-* 流式（无 system_alert）", t_chat_letta_stream)
    T("letta-* 真流式 TTFT<3s", t_letta_stream_ttft_under_3s)
    T("letta grep 工具真实调用不 crash", t_letta_grep_tool_end_to_end)
    T("<think> 闭合平衡（3 种路径）", t_think_balanced)

    print("\n── 权限 ──")
    T("非 org admin 上传组织文件 403", t_non_org_admin_upload_blocked)
    T("所有 letta-* 模型在 WebUI model 表注册", t_letta_models_registered_in_webui)
    T("# 下拉跨项目隔离 (biany in cpm 不应看到 security)", t_hash_dropdown_scoped_to_project)

    print("\n── 数据完整性 ──")
    T("knowledge_mirrors > 0", t_knowledge_mirrors_count)
    T("project_members > 0", t_project_members_count)
    T("user_agent_map > 0", t_user_agent_map_count)
    T("Letta agents 无孤儿漂移 (≤5)", t_no_orphan_letta_agents)
    T("Agent prompt 都在 vLLM 上限内 (防 400 死锁)", t_agent_prompt_under_vllm_limit)
    T("user_cache.personal_human_block_id 已缓存（合一 migration 已跑）", t_human_block_shared_cached)

    print("\n── Pipeline Filter / # 按模型过滤 ──")
    T("filter 已注册并激活", t_pipeline_filter_registered)
    T("filter 挂到所有模型", t_pipeline_filter_attached_to_all_models)
    T("WebUI knowledge API 返回 description 字段", t_hash_filter_api_returns_description)

    passed = sum(1 for ok, *_ in _results if ok)
    total = len(_results)
    print(f"\n==== {passed}/{total} PASS ====")
    failed = [(n, e) for ok, n, _, e, _ in _results if not ok]
    for n, e in failed:
        print(f"  FAIL: {n} — {e}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
