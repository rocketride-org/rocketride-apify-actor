"""Checks the built Actor image the way Apify will call it. Run by tests/integration.sh."""
import json, sys, time, urllib.error, urllib.request

B = 'http://127.0.0.1:4321'
PROBE = {'x-apify-container-server-readiness-probe': '1'}
failures = []


def call(method, path, body=None, headers=None, timeout=900):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(B + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:  # noqa: BLE001
        return 0, f'{type(e).__name__}: {e}'


def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + ('' if cond else f'  -> {str(detail)[:900]}'), flush=True)
    if not cond:
        failures.append(name)


def wait(path, headers=None, tries=180):
    for _ in range(tries):
        st, _ = call('GET', path, headers=headers, timeout=5)
        if st == 200:
            return True
        time.sleep(2)
    return False


def pipeline(name, body):
    t = time.time()
    st, txt = call('POST', f'/pipelines/{name}', body)
    try:
        d = json.loads(txt)
    except ValueError:
        d = {'raw': txt}
    print(f'   {name}: HTTP {st} in {time.time() - t:.1f}s -> {json.dumps({k: v for k, v in d.items() if k != "result"})[:300]}', flush=True)
    return st, d


DOC = ('Invoice INV-2026-0042 from Acme Robotics to John Smith (john.smith@example.com), Seattle. '
       'Two servo kits at 480.00 USD each. Total due: 960.00 USD by 2026-12-01.')

check('readiness probe', wait('/', PROBE, 60))
check('engine healthy', wait('/health'))
st, d = pipeline('doc-to-text', {'text': 'Hello from the Apify CI.'})
check('doc-to-text', st == 200 and d.get('text') == 'Hello from the Apify CI.', d)
st, d = pipeline('redact-pii', {'text': DOC})
print('   redact-pii text:', d.get('text'))
check('redact-pii masks the name and the email', st == 200 and '\u2588' in d.get('text', '') and 'John Smith' not in d['text'] and 'john.smith' not in d['text'], d)
st, d = pipeline('summarize', {'text': DOC})
check('summarize', st == 200 and 'MOCK SUMMARY' in d.get('text', '') and 'Mock key point one' in d['text'], d)
st, d = pipeline('extract-data', {'text': DOC, 'fields': ['invoice_number', {'name': 'total', 'type': 'number'}]})
check('extract-data', st == 200 and d.get('data') == [{'invoice_number': 'INV-001', 'total': '42.00'}], d)
st, d = pipeline('ask', {'text': DOC, 'question': 'What is the total due?'})
check('ask', st == 200 and d.get('answer') == 'MOCK ANSWER', d)
st, d = pipeline('ask', {'text': DOC, 'question': 'What is the total due?'})
check('ask again is fast (pipeline stays warm)', st == 200 and d.get('durationMs', 99999) < 5000, d)
st, d = pipeline('agent', {'question': 'Say hello.'})
check('agent', st == 200 and d.get('answer') == 'MOCK AGENT ANSWER', d)
st, d = pipeline('ask', {'text': DOC, 'question': 'q', 'provider': 'openai'})
check('unconfigured provider is rejected', st == 400, d)

mcp_headers = {'Accept': 'application/json, text/event-stream'}
st, txt = call('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'ci', 'version': '0'}}}, mcp_headers)
check('MCP initialize', st == 200 and '"result"' in txt, txt)
st, txt = call('POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, mcp_headers)
payload = [line[5:] for line in txt.splitlines() if line.startswith('data:')] or [txt]
try:
    tools = [t['name'] for t in json.loads(payload[-1])['result']['tools']]
except Exception:  # noqa: BLE001
    tools = []
print(f'   {len(tools)} MCP tools: {", ".join(tools)}')
check('MCP tools/list has validate_pipeline and run_pipeline', {'validate_pipeline', 'run_pipeline'} <= set(tools), txt)

print('\nINTEGRATION PASSED' if not failures else f'\nINTEGRATION FAILED: {failures}')
sys.exit(1 if failures else 0)
