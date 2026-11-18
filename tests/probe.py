"""One-off exploration of the real engine (runs in the probe workflow)."""
import json, sys, time, urllib.request, urllib.error
sys.path.insert(0, 'src')
import pipelines

B = 'http://127.0.0.1:5565'
AUTH = {'Authorization': 'Bearer rr_local'}

def call(method, path, body=None, headers=None, timeout=1200):
    h = dict(AUTH); h.update(headers or {})
    data = None
    if body is not None:
        if isinstance(body, (bytes, str)):
            data = body.encode() if isinstance(body, str) else body
        else:
            data = json.dumps(body).encode(); h.setdefault('Content-Type', 'application/json')
    r = urllib.request.Request(B + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return 0, f'{type(e).__name__}: {e}'

def mcp(tool, args):
    st, txt = call('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': tool, 'arguments': args}},
                   {'Accept': 'application/json, text/event-stream'})
    for line in txt.splitlines():
        if line.startswith('data:'):
            txt = line[5:].strip()
    try:
        d = json.loads(txt)
        c = d.get('result', {}).get('content')
        if c: return c[0].get('text', json.dumps(c))
        return json.dumps(d)
    except Exception:
        return f'[{st}] {txt}'

def show(title, text, n=1500):
    print(f'\n##### {title}\n{str(text)[:n]}', flush=True)

st, txt = call('GET', '/services')
try:
    d = json.loads(txt); data = d.get('data', d)
    show('/services shape', f'status={st} type={type(data).__name__} keys/len={list(data)[:12] if isinstance(data, dict) else len(data)}')
    def find(node, name, depth=0):
        if isinstance(node, dict):
            if name in node: return node[name]
            for v in node.values():
                r = find(v, name, depth + 1)
                if r is not None: return r
        return None
    lo = find(data, 'llm_openai')
    if isinstance(lo, dict):
        show('llm_openai service keys', list(lo))
        pre = lo.get('preconfig', {}); show('llm_openai preconfig', {'default': pre.get('default'), 'nprofiles': len(pre.get('profiles', {})), 'sample': list(pre.get('profiles', {}))[:6]})
    else:
        show('services raw head', txt[:1500])
except Exception as e:
    show('/services parse fail', f'{e} :: {txt[:800]}')

for prov, cfg in [
    ('llm_openai', {'apikey': 'k'}),
    ('llm_openai', {'profile': 'custom', 'custom': {'model': 'gpt-x', 'apikey': 'k'}}),
    ('llm_openai', {'profile': 'openai-4o-mini', 'openai-4o-mini': {'apikey': 'k'}}),
    ('llm_anthropic', {'profile': 'claude', 'claude': {'apikey': 'k'}}),
    ('llm_openai_api', {'profile': 'custom', 'custom': {'model': 'm', 'base_url': '${ROCKETRIDE_LLM_BASE_URL}', 'apikey': 'k', 'modelTotalTokens': 32768}}),
    ('summarization', {'numberOfSummaries': 3, 'numberOfSummaryWords': 250, 'numberOfKeyPointWords': 40, 'numberOfEntities': 15}),
    ('extract_data', {'fields': [{'column': 'total', 'type': 'number', 'defval': ''}]}),
]:
    show(f'resolve_config {prov} {json.dumps(cfg)[:80]}', mcp('resolve_config', {'provider': prov, 'config': cfg}), 900)

show('describe extract_data', mcp('describe_component', {'name': 'extract_data'}), 2500)

ENV = {'ROCKETRIDE_LLM_BASE_URL': 'x', 'ROCKETRIDE_LLM_MODEL': 'x', 'ROCKETRIDE_LLM_KEY': 'x',
       'ROCKETRIDE_OPENAI_KEY': 'x', 'ROCKETRIDE_ANTHROPIC_KEY': 'x', 'ROCKETRIDE_GEMINI_KEY': 'x'}
DOC = ('Invoice INV-2026-0042 from Acme Robotics to Jane Doe, dated 2026-11-03. '
       'Two Model X servo kits at 480.00 USD each. Total due: 960.00 USD by 2026-12-01.')

def run(label, name, params, payload, ctype='text/plain'):
    try:
        pipe = pipelines.build(name, params, ENV)
    except Exception as e:
        show(f'{label}: build failed', e); return
    show(f'{label}: validate', mcp('validate_pipeline', {'pipeline': pipe}), 700)
    t = time.time(); st, txt = call('POST', '/task', pipe)
    show(f'{label}: start [{st}] {time.time()-t:.1f}s', txt, 700)
    try: token = json.loads(txt)['data']['token']
    except Exception: return
    t = time.time(); st, txt = call('POST', f'/webhook?token={token}', payload, {'Content-Type': ctype, 'Content-Disposition': 'attachment; filename="doc.txt"'})
    show(f'{label}: send [{st}] {time.time()-t:.1f}s', txt, 1800)
    t = time.time(); st, txt = call('POST', f'/webhook?token={token}', payload, {'Content-Type': ctype, 'Content-Disposition': 'attachment; filename="doc2.txt"'})
    show(f'{label}: send again [{st}] {time.time()-t:.1f}s', txt, 400)
    call('DELETE', f'/task?token={token}')

C = {'provider': 'openai-compatible'}
run('summarize/mock', 'summarize', dict(C), DOC)
run('extract/mock', 'extract-data', dict(C, fields=['invoice_number', {'name': 'total', 'type': 'number'}]), DOC)
run('ask/mock', 'ask', dict(C, question='What is the total due?'), DOC)
run('agent/mock', 'agent', dict(C), 'What is RocketRide?')
run('ask/openai-dummy', 'ask', {'provider': 'openai', 'question': 'q'}, DOC)
run('ask/openai-custom-dummy', 'ask', {'provider': 'openai', 'model': 'gpt-4o-mini', 'question': 'q'}, DOC)
run('ask/anthropic-dummy', 'ask', {'provider': 'anthropic', 'question': 'q'}, DOC)
run('ask/gemini-dummy', 'ask', {'provider': 'gemini', 'question': 'q'}, DOC)
try:
    show('mock LLM log', urllib.request.urlopen('http://127.0.0.1:9911/_log', timeout=10).read().decode(), 4000)
except Exception as e:
    show('mock log fail', e)
