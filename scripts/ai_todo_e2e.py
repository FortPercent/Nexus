import sys, sqlite3, httpx, jwt, time
from datetime import datetime, timedelta, timezone
sys.path.insert(0, '/app')
from routing import letta

SECRET='6WYGSa8e7EBsSeG3'
USER='ce1d405b-0b5c-4faf-8864-010e2611b900'
ADMIN_BASE='http://localhost:8000/admin/api'
CHAT_BASE='http://localhost:8000/v1'

tok = jwt.encode({'id':USER,'exp':datetime.now(timezone.utc)+timedelta(hours=1)},SECRET,algorithm='HS256')
H_ADMIN = {'Authorization':f'Bearer {tok}'}
H_CHAT = {'Authorization':'Bearer teleai-adapter-key-2026','Content-Type':'application/json'}

def chat(msg):
    r = httpx.post(f'{CHAT_BASE}/chat/completions', json={
        'model':'letta-ai-infra','stream':False,
        'messages':[{'role':'user','content':msg}],
        'user_id':USER,'user_email':'wuxn5@chinatelecom.cn',
    }, headers=H_CHAT, timeout=180)
    return r.json()['choices'][0]['message']['content']

def count_todos(status=None):
    q = f'{ADMIN_BASE}/project/ai-infra/todos' + (f'?status={status}' if status else '')
    return len(httpx.get(q, headers=H_ADMIN, timeout=10).json())

# 清理：删之前测试遗留
c = sqlite3.connect('/data/serving/adapter/adapter.db')
c.execute("DELETE FROM project_todos WHERE project_id='ai-infra' AND (source='ai' OR title LIKE 'TEST%')")
c.commit()
c.close()

fails = 0
def assert_eq(name, got, exp):
    global fails
    if got == exp:
        print(f'[PASS] {name}: {got}')
    else:
        print(f'[FAIL] {name}: got {got}, exp {exp}'); fails += 1

def assert_contains(name, text, needle):
    global fails
    if needle in text:
        print(f'[PASS] {name}: 含「{needle}」')
    else:
        print(f'[FAIL] {name}: 不含「{needle}」'); fails += 1

print('== case 1: 时间+动作 应建 TODO ==')
b0 = count_todos('awaiting_user')
r = chat('五点半我要写周报')
b1 = count_todos('awaiting_user')
assert_eq('新增 awaiting_user TODO', b1 - b0, 1)
print(f'  AI reply: {r[:200]}')

print()
print('== case 2: 个人偏好 不应建 TODO ==')
b2 = count_todos('awaiting_user')
r = chat('我叫吴煊佴，喜欢用 Python')
b3 = count_todos('awaiting_user')
assert_eq('TODO 数不变', b3 - b2, 0)

print()
print('== case 3: 项目决策 不应建 TODO ==')
b4 = count_todos('awaiting_user')
r = chat('这个项目现在用 vLLM 作为推理引擎')
b5 = count_todos('awaiting_user')
assert_eq('TODO 数不变', b5 - b4, 0)

print()
print('== case 4: 明确 TODO 用户确认后入看板 ==')
# 拿最新的 awaiting_user TODO
rows = httpx.get(f'{ADMIN_BASE}/project/ai-infra/todos?status=awaiting_user', headers=H_ADMIN).json()
if rows:
    tid = rows[0]['id']
    r = httpx.post(f'{ADMIN_BASE}/project/ai-infra/todos/{tid}/confirm', headers=H_ADMIN, timeout=10)
    assert_eq('confirm 返回 200', r.status_code, 200)
    new_status = r.json()['status']
    assert_eq('confirm 后变 open', new_status, 'open')
else:
    print('[FAIL] case 4 setup: 没有 awaiting_user todo 可测试'); fails += 1

# 清理
c = sqlite3.connect('/data/serving/adapter/adapter.db')
c.execute("DELETE FROM project_todos WHERE project_id='ai-infra' AND (source='ai' OR title LIKE 'TEST%')")
c.commit()
c.close()

print()
print(f'==== {4 - fails}/4 PASS ====')
sys.exit(1 if fails else 0)
