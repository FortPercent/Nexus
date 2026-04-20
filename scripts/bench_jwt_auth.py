#!/usr/bin/env python3
"""JWT 认证错误路径测试：
- 过期 JWT → 期望 401 快速返回
- 无效签名 → 期望 401
- 空/畸形 JWT → 期望 401
- 合法 JWT → 期望 200
对每种打 10 次测 p50/p95 延迟。
"""
import json, os, time, urllib.request, statistics
import jwt  # PyJWT
import uuid

ADAPTER = "http://localhost:8000/admin/api/me"
SECRET = os.getenv("OPENWEBUI_JWT_SECRET", "6WYGSa8e7EBsSeG3")
VALID_UID = "ce1d405b-0b5c-4faf-8864-010e2611b900"  # wuxn5

def mint(exp_delta_seconds, secret=SECRET, uid=VALID_UID):
    payload = {"id": uid, "exp": int(time.time()) + exp_delta_seconds}
    return jwt.encode(payload, secret, algorithm="HS256")

def probe(authorization_header):
    t0 = time.perf_counter()
    req = urllib.request.Request(ADAPTER, headers={"Authorization": authorization_header} if authorization_header else {})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, time.perf_counter()-t0
    except urllib.error.HTTPError as e:
        return e.code, time.perf_counter()-t0
    except Exception as e:
        return -1, time.perf_counter()-t0

def bench(name, auth_header, n=10):
    codes = []
    rts = []
    for _ in range(n):
        c, rt = probe(auth_header)
        codes.append(c)
        rts.append(rt)
    c0 = codes[0]
    uniform = all(c == c0 for c in codes)
    rts.sort()
    median = statistics.median(rts)
    p95 = rts[int(n*0.95)] if n > 1 else rts[0]
    max_rt = rts[-1]
    print(f"{name:35s}  status={'all '+str(c0) if uniform else codes}  median={median*1000:.0f}ms  p95={p95*1000:.0f}ms  max={max_rt*1000:.0f}ms")

print(f"target: {ADAPTER}\n")

# 1. 合法
bench("合法 JWT (exp=+1h)", f"Bearer {mint(3600)}")

# 2. 过期 (exp 已过)
bench("过期 JWT (exp=-60s)", f"Bearer {mint(-60)}")

# 3. 即将过期但刚过
bench("刚过期 (exp=-1s)", f"Bearer {mint(-1)}")

# 4. 签名错 (wrong secret)
bench("签名错 (wrong secret)", f"Bearer {mint(3600, secret='hacker-secret-1234')}")

# 5. 畸形 (bad string)
bench("畸形 JWT", f"Bearer abc.def.ghi")

# 6. 空 Bearer
bench("空 Bearer", "Bearer ")

# 7. 无 auth header
bench("无 Authorization header", None)

# 8. 不存在的 user_id (签名对，但用户不存在)
bench("有效签名 + 未知 user_id", f"Bearer {mint(3600, uid=str(uuid.uuid4()))}")

# 9. Scheme 错 (Basic 而非 Bearer)
bench("非 Bearer scheme", f"Basic {mint(3600)}")
