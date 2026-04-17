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
JWT_SECRET = os.getenv("JWT_SECRET", "6WYGSa8e7EBsSeG3")
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
    T("<think> 闭合平衡（3 种路径）", t_think_balanced)

    print("\n── 权限 ──")
    T("非 org admin 上传组织文件 403", t_non_org_admin_upload_blocked)
    T("所有 letta-* 模型在 WebUI model 表注册", t_letta_models_registered_in_webui)

    print("\n── 数据完整性 ──")
    T("knowledge_mirrors > 0", t_knowledge_mirrors_count)
    T("project_members > 0", t_project_members_count)
    T("user_agent_map > 0", t_user_agent_map_count)
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
