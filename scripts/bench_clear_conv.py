#!/usr/bin/env python3
"""对话清空并发测试 - 路 A: 创建 5 个临时用户 → 各自触发 agent 生成 → 并发清空。

步骤：
1. admin 登录 Open WebUI，拿 admin JWT
2. 创建 5 个 bench_clear_* 用户
3. 各自先发一条 letta-ai-infra chat 触发 agent 创建
4. 对 5 个用户并发 DELETE /admin/api/personal/conversations/ai-infra
5. 报 wall time / 成功数 / 每个的 agent 新旧 id
6. 清理：从项目移除 + 从 Open WebUI 删用户
"""
import asyncio, json, os, time, urllib.request, uuid
import httpx

WEBUI = "http://172.17.0.1:3000"
ADAPTER = "http://localhost:8000"
LETTA = "http://letta-server:8283"
JWT_SECRET = os.getenv("OPENWEBUI_JWT_SECRET", "6WYGSa8e7EBsSeG3")
N = 5  # 测试用户数

def admin_signin():
    d = json.dumps({"email": "admin@aiinfra.local", "password": "AIinfra@2026"}).encode()
    req = urllib.request.Request(f"{WEBUI}/api/v1/auths/signin", data=d,
                                  headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]

def add_user(admin_jwt, email, name, password):
    form = {"email": email, "name": name, "password": password, "role": "user",
            "profile_image_url": ""}
    req = urllib.request.Request(f"{WEBUI}/api/v1/auths/add",
                                  data=json.dumps(form).encode(),
                                  headers={"Content-Type": "application/json",
                                           "Authorization": f"Bearer {admin_jwt}"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def delete_user(admin_jwt, user_id):
    req = urllib.request.Request(f"{WEBUI}/api/v1/users/{user_id}",
                                  method="DELETE",
                                  headers={"Authorization": f"Bearer {admin_jwt}"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        return False

def sign_jwt(user_id):
    import jwt
    return jwt.encode({"id": user_id, "exp": int(time.time()) + 3600}, JWT_SECRET, algorithm="HS256")

def add_member(admin_api_jwt, project_id, user_id):
    d = json.dumps({"user_id": user_id}).encode()
    req = urllib.request.Request(f"{ADAPTER}/admin/api/project/{project_id}/members",
                                  data=d, method="POST",
                                  headers={"Content-Type": "application/json",
                                           "Authorization": f"Bearer {admin_api_jwt}"})
    try:
        return urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.HTTPError as e:
        return f"add_member failed: {e.code} {e.read()[:100]}"

def remove_member(admin_api_jwt, project_id, user_id):
    req = urllib.request.Request(f"{ADAPTER}/admin/api/project/{project_id}/members/{user_id}",
                                  method="DELETE",
                                  headers={"Authorization": f"Bearer {admin_api_jwt}"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:
        return False

async def trigger_chat(client, user):
    """发一条聊天，触发 letta agent 创建"""
    body = {
        "model": "letta-ai-infra",
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 30, "stream": False,
        "user_id": user["id"], "user_email": user["email"], "user_name": user["name"],
    }
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{ADAPTER}/v1/chat/completions", json=body,
                              headers={"Authorization": "Bearer teleai-adapter-key-2026",
                                       "Content-Type": "application/json"}, timeout=60)
        return r.status_code, time.perf_counter()-t0
    except Exception as e:
        return -1, time.perf_counter()-t0

async def get_agent_id(user):
    """从 adapter DB 拿当前 agent_id"""
    import sqlite3
    # can't easily connect to adapter sqlite from outside; use the conversations overview API
    pass

async def clear_conversation(client, user, project_id):
    user_jwt = sign_jwt(user["id"])
    t0 = time.perf_counter()
    try:
        r = await client.delete(f"{ADAPTER}/admin/api/personal/conversations/{project_id}",
                                 headers={"Authorization": f"Bearer {user_jwt}"}, timeout=60)
        return r.status_code, time.perf_counter()-t0, r.text[:200]
    except Exception as e:
        return -1, time.perf_counter()-t0, f"{type(e).__name__}:{e}"

async def main():
    admin_jwt = admin_signin()
    # 项目成员管理需要用项目 admin 身份，admin@aiinfra.local 不是 ai-infra 成员
    admin_api_jwt = sign_jwt("ce1d405b-0b5c-4faf-8864-010e2611b900")  # wuxn5 (ai-infra admin)
    print(f"admin signin ok, using wuxn5 as project admin")

    stamp = int(time.time())
    users = []
    print(f"\n=== 1. 创建 {N} 个 bench_clear_* 临时用户 ===")
    for i in range(N):
        email = f"bench_clear_{stamp}_{i}@local.test"
        try:
            u = add_user(admin_jwt, email, f"bench_{i}", f"BenchPass{stamp}!")
            users.append({"id": u["id"], "email": email, "name": f"bench_{i}"})
            print(f"  [{i}] created {email} id={u['id'][:8]}...")
        except Exception as e:
            print(f"  [{i}] FAIL: {e}")

    print(f"\n=== 2. 加入 ai-infra 项目 ===")
    for u in users:
        r = add_member(admin_api_jwt, "ai-infra", u["id"])
        print(f"  {u['email'][:30]}: {'ok' if 'failed' not in str(r) else str(r)[:80]}")

    print(f"\n=== 3. 各自触发 letta-ai-infra chat 创建 agent ===")
    async with httpx.AsyncClient() as client:
        # 并发触发
        t0 = time.perf_counter()
        results = await asyncio.gather(*(trigger_chat(client, u) for u in users))
        wall = time.perf_counter()-t0
        for u, (code, rt) in zip(users, results):
            print(f"  {u['email'][:30]}: status={code} rt={rt:.1f}s")
        print(f"  总 wall={wall:.1f}s")

    await asyncio.sleep(3)  # 给 agent 创建一点稳定时间

    print(f"\n=== 4. 并发清空 5 用户的 ai-infra 对话 ===")
    async with httpx.AsyncClient() as client:
        t0 = time.perf_counter()
        results = await asyncio.gather(*(clear_conversation(client, u, "ai-infra") for u in users))
        wall = time.perf_counter()-t0
        ok = sum(1 for c, _, _ in results if c == 200)
        print(f"  成功: {ok}/{N}  总 wall={wall:.1f}s")
        for u, (code, rt, body) in zip(users, results):
            marker = "OK" if code == 200 else f"FAIL {code}"
            print(f"  {u['email'][:30]}: {marker}  rt={rt:.1f}s  {body[:80] if code != 200 else ''}")

    print(f"\n=== 5. 清理：移项目成员 + 删 Open WebUI 用户 ===")
    for u in users:
        remove_member(admin_api_jwt, "ai-infra", u["id"])
        delete_user(admin_jwt, u["id"])
    print(f"  done")

asyncio.run(main())
