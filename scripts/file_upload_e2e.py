import httpx, jwt, io, os
from datetime import datetime, timedelta, timezone
import openpyxl, zipfile

SECRET='6WYGSa8e7EBsSeG3'
USER='ce1d405b-0b5c-4faf-8864-010e2611b900'
tok = jwt.encode({'id':USER,'exp':datetime.now(timezone.utc)+timedelta(hours=1)},SECRET,algorithm='HS256')
H = {'Authorization':f'Bearer {tok}'}
BASE = 'http://localhost:8000/admin/api'

fails = 0
def run(name, fn):
    global fails
    try:
        note = fn() or ''
        print(f'[PASS] {name} {note}')
    except AssertionError as e:
        print(f'[FAIL] {name} - {e}'); fails += 1
    except Exception as e:
        print(f'[FAIL] {name} - {type(e).__name__}: {e}'); fails += 1

def upload(filename, content, mime='application/octet-stream'):
    files = {'file': (filename, content, mime)}
    r = httpx.post(f'{BASE}/personal/files', headers=H, files=files, timeout=60)
    if r.status_code != 200:
        raise AssertionError(f'status={r.status_code} body={r.text[:200]}')
    return r.json()

def list_personal():
    r = httpx.get(f'{BASE}/personal/files', headers=H, timeout=10)
    return [f['name'] for f in r.json()]

def delete_test_files():
    rows = httpx.get(f'{BASE}/personal/files', headers=H, timeout=10).json()
    for f in rows:
        if f['name'].startswith('TEST_'):
            httpx.delete(f"{BASE}/personal/files/{f['id']}", headers=H, timeout=10)

# 清旧
delete_test_files()

def t_xlsx():
    wb = openpyxl.Workbook()
    ws = wb.active; ws.title = 'Sheet1'
    ws.append(['项目','模型','状态'])
    ws.append(['AI Infra','Qwen3.5-122B','稳定'])
    ws.append(['AI Infra Cache','DeepSeek-V3','测试中'])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    r = upload('TEST_sheet.xlsx', buf.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    assert 'TEST_sheet.xlsx.md' in (r.get('uploaded') or []), f'expected .md in uploaded, got {r}'
    files = list_personal()
    assert 'TEST_sheet.xlsx.md' in files, f'not in list: {files}'
    return f'→ {r["uploaded"]}'

def t_csv():
    content = '项目,负责人\nAI Infra,wuxn5\nCache,jinyx5'.encode('utf-8')
    r = upload('TEST_team.csv', content, 'text/csv')
    assert 'TEST_team.csv.md' in (r.get('uploaded') or [])
    return f'→ {r["uploaded"]}'

def t_zip():
    # 造 zip：内含 csv + md
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('data.csv', 'a,b\n1,2')
        z.writestr('notes.md', '# 注释\n内容')
    r = upload('TEST_pack.zip', buf.getvalue(), 'application/zip')
    uploaded = r.get('uploaded') or []
    # 预期：data.csv.md + notes.md 被展开 (路径前缀为 TEST_pack__)
    has_csv = any('data.csv' in n for n in uploaded)
    has_md = any('notes.md' in n for n in uploaded)
    assert has_csv and has_md, f'missing expected: {uploaded}'
    return f'→ {uploaded}'

def t_pdf_passthrough():
    # 最小 pdf header
    content = b'%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Size 1/Root 1 0 R>>\n%%EOF'
    r = upload('TEST_pass.pdf', content, 'application/pdf')
    assert 'TEST_pass.pdf' in (r.get('uploaded') or [])
    return f'→ {r["uploaded"]}'

def t_unsupported_ext():
    try:
        upload('TEST_x.exe', b'noop')
        raise AssertionError('应 400')
    except AssertionError as e:
        if '400' in str(e):
            return 'rejected 400'
        raise

run('xlsx → markdown', t_xlsx)
run('csv → markdown', t_csv)
run('zip 递归展开', t_zip)
run('pdf 原样透传', t_pdf_passthrough)
run('未知扩展 → 400', t_unsupported_ext)

delete_test_files()
print()
print(f'==== {5-fails}/5 ====')
import sys; sys.exit(fails)
