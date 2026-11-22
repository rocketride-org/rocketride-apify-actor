"""End-to-end test of the Actor wrapper against tests/mock_engine.py.

Run: python3 tests/test_local.py   (needs starlette, uvicorn, httpx, cryptography, python-multipart)
"""
import base64, json, os, secrets, subprocess, sys, time
from pathlib import Path
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent.parent
PORT = 4399

def apify_encrypt(value, pub):
    key, iv = secrets.token_hex(16).encode(), secrets.token_hex(8).encode()
    enc = Cipher(algorithms.AES(key), modes.GCM(iv)).encryptor()
    data = enc.update(value.encode()) + enc.finalize() + enc.tag
    pw = pub.encrypt(key + iv, padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
    return f'ENCRYPTED_VALUE:{base64.b64encode(pw).decode()}:{base64.b64encode(data).decode()}'

priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                         serialization.BestAvailableEncryption(b'pass'))
env = dict(os.environ,
    ACTOR_WEB_SERVER_PORT=str(PORT), ROCKETRIDE_ENGINE_PORT='5599', ROCKETRIDE_ACTOR_MODE='server',
    ROCKETRIDE_ENGINE_CMD=f'{sys.executable} {ROOT}/tests/mock_engine.py', ROCKETRIDE_HOME=str(ROOT),
    APIFY_INPUT_SECRETS_PRIVATE_KEY_FILE=base64.b64encode(pem).decode(),
    APIFY_INPUT_SECRETS_PRIVATE_KEY_PASSPHRASE='pass',
    ROCKETRIDE_VERIFY_CALLER='1', APIFY_USER_ID='owner1',
    ACTOR_INPUT_JSON=json.dumps({'openaiApiKey': apify_encrypt('sk-test-123', priv.public_key()),
                                 'additionalEnv': {'ROCKETRIDE_APIKEY': 'hijack', 'ROCKETRIDE_QDRANT_HOST': 'q'}}))
proc = subprocess.Popen([sys.executable, str(ROOT / 'src/main.py')], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
base = f'http://127.0.0.1:{PORT}'
failures = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (f'  -> {detail}' if not cond else ''))
    if not cond: failures.append(name)
try:
    for _ in range(100):
        try:
            if httpx.get(base + '/', headers={'x-apify-container-server-readiness-probe': '1'}).text == 'OK': break
        except httpx.HTTPError: time.sleep(0.2)
    c = httpx.Client(base_url=base, timeout=30, headers={'Authorization': 'Bearer apify_api_FAKE', 'X-Apify-User-ID': 'owner1'})  # as Apify's gateway forwards it
    check('readiness probe returns OK', c.get('/', headers={'x-apify-container-server-readiness-probe': '1'}).text == 'OK')
    for _ in range(50):
        if c.get('/health').status_code == 200: break
        time.sleep(0.2)
    check('health ok', c.get('/health').json()['ok'] is True)
    info = c.get('/').json(); check('root lists mcp endpoint', info['endpoints']['mcp'].endswith('/mcp'), info)
    sys.path.insert(0, str(ROOT / 'src')); sys.path.insert(0, str(ROOT / 'scripts'))
    import export_pipes
    check('pipelines/*.pipe examples are in sync with the builders', export_pipes.export(check=True) == [], export_pipes.export(check=True))
    pl = c.get('/pipelines').json()
    check('six ready-made pipelines', [p['name'] for p in pl['pipelines']] == ['doc-to-text', 'redact-pii', 'summarize', 'extract-data', 'ask', 'agent'], pl)
    check('only the configured LLM provider is offered', pl['llmProviders'] == ['openai'], pl)
    r = c.post('/pipelines/doc-to-text', json={'text': 'hello world'}).json()
    check('doc-to-text via JSON text', r.get('text', '').endswith('hello world') and '[text/plain|' in r['text'], r)
    check('secret OpenAI key decrypted into engine env', 'sk-test-123' in r.get('text', ''), r)
    r = c.post('/pipelines/redact-pii?filename=a.pdf', content=b'%PDF raw', headers={'Content-Type': 'application/pdf'}).json()
    check('redact-pii raw upload', 'application/pdf' in (r.get('text') or '') and 'ner' in r['text'] and r['filename'] == 'a.pdf', r)
    r = c.post('/pipelines/doc-to-text', files={'file': ('n.txt', b'multi part', 'text/plain')}).json()
    check('multipart upload', 'multi part' in (r.get('text') or ''), r)
    r = c.post('/pipelines/doc-to-text', json={'text': 'no ocr please'}).json()
    check('doc-to-text skips OCR by default', ',ocr,' not in (r.get('text') or '') and 'parse' in r['text'], r)
    r = c.post('/pipelines/doc-to-text', json={'text': 'scan', 'ocr': True}).json()
    check('doc-to-text ocr=true adds the OCR node', ',ocr,' in (r.get('text') or ''), r)
    r = c.post('/pipelines/redact-pii', content=b'\x89PNG fake', headers={'Content-Type': 'image/png'}).json()
    check('image uploads are OCRed automatically', ',ocr,' in (r.get('text') or '') and 'image/png' in r['text'], r)

    # A pipeline that dies (the real engine: OCR worker killed for running out of memory) must not stay broken.
    r = c.post('/pipelines/doc-to-text', json={'text': 'TRIGGER_TASK_DIES'})
    check('pipeline dying mid-request is a 502 with a hint', r.status_code == 502 and r.json()['error'] == 'Connection closed' and 'memory' in r.json().get('hint', ''), r.text[:400])
    r = c.post('/pipelines/doc-to-text', json={'text': 'after the crash'})
    check('the next request gets a fresh pipeline', r.status_code == 200 and 'after the crash' in r.json().get('text', ''), r.text[:400])
    c.post('/engine/_kill')
    r = c.post('/pipelines/redact-pii', json={'text': 'after an outside stop'})
    check('a pipeline stopped behind our back is restarted and the request retried', r.status_code == 200 and 'after an outside stop' in r.json().get('text', ''), r.text[:400])
    check('unknown pipeline 404', c.post('/pipelines/nope', json={'text': 'x'}).status_code == 404)
    check('empty body 400', c.post('/pipelines/doc-to-text', content=b'').status_code == 400)

    # LLM pipelines
    r = c.get('/pipelines/ask', params={'question': 'why?'}).json()
    dumped = json.dumps(r)
    check('GET /pipelines/ask shows the .pipe with key as a variable', '${ROCKETRIDE_OPENAI_KEY}' in dumped and 'sk-test-123' not in dumped and 'Question: why?' in dumped, dumped[:600])
    r = c.post('/pipelines/ask', json={'text': 'the doc', 'question': 'why?'}).json()
    check('ask returns an answer', r.get('answer') == 'ANSWER via llm_openai: the doc', r)
    check('ask without question is a 400', c.post('/pipelines/ask', json={'text': 'the doc'}).status_code == 400)
    r = c.post('/pipelines/ask', json={'text': 'd', 'question': 'q', 'provider': 'anthropic'})
    check('unconfigured provider is a 400', r.status_code == 400 and 'no credentials' in r.json()['error'], r.text)
    r = c.post('/pipelines/extract-data', json={'text': 'd', 'fields': ['invoice_number', {'name': 'total', 'type': 'number'}]}).json()
    check('extract-data returns rows', r.get('data') == [{'invoice_number': 'v', 'total': 'v'}], r)
    r = c.post('/pipelines/extract-data?fields=name,email', content=b'raw doc', headers={'Content-Type': 'text/plain'}).json()
    check('extract-data fields from query string', r.get('data') == [{'name': 'v', 'email': 'v'}], r)
    check('extract-data without fields is a 400', c.post('/pipelines/extract-data', json={'text': 'd'}).status_code == 400)
    r = c.post('/pipelines/summarize', json={'text': 'long doc'}).json()
    check('summarize returns text', 'summarization' in (r.get('text') or '') and 'ocr' not in r['text'], r)
    r = c.post('/pipelines/summarize', json={'text': 'long doc', 'ocr': True}).json()
    check('ocr option adds the OCR node', ',ocr,' in (r.get('text') or ''), r)
    r = c.post('/pipelines/agent', json={'question': 'What is RocketRide?'}).json()
    check('agent answers a question', r.get('answer') == 'ANSWER via llm_openai: What is RocketRide?', r)
    r = c.post('/pipelines/ask', json={'text': 'TRIGGER_LLM_ERROR', 'question': 'q'})
    check('LLM failure is a 502 with the error, not an answer', r.status_code == 502 and 'LLM error' in r.json()['error'] and 'answer' not in r.json(), r.text)
    r = c.post('/pipelines/summarize', json={'text': 'TRIGGER_NODE_ERROR'})
    check('node failure is a 502', r.status_code == 502 and r.json()['error'] == 'node exploded', r.text)
    for i in range(5):
        c.post('/pipelines/ask', json={'text': 'd', 'question': f'question {i}'})
    running = c.get('/engine/_tasks').json()
    n, n_llm = running['count'], sum(any(p.startswith('llm_') for p in t) for t in running['pipelines'])
    check('at most 3 LLM pipelines stay running', n_llm <= 3, running)
    again = c.post('/pipelines/ask', json={'text': 'd', 'question': 'question 4'}).json()
    check('a repeated request reuses the running pipeline', c.get('/engine/_tasks').json()['count'] == n and again.get('answer'), again)

    pipe = __import__('json').loads((ROOT / 'pipelines/doc-to-text.pipe').read_text())
    r = c.post('/run', json={'pipeline': pipe, 'text': 'adhoc'}).json(); check('/run ad-hoc', 'adhoc' in (r.get('text') or ''), r)
    c.post('/pipelines/doc-to-text', json={'text': 'warm'})
    served = c.get('/pipelines/doc-to-text').json()['pipeline']
    r = c.post('/run', json={'pipeline': served, 'text': 'while warm'}).json()
    check('/run accepts a GET /pipelines definition while that pipeline is warm', 'while warm' in (r.get('text') or ''), r)
    # Chat-source pipelines (most of the examples in the RocketRide repos) only read questions.
    chat = {'components': [
        {'id': 'chat_1', 'provider': 'chat', 'config': {'mode': 'Source', 'type': 'chat', 'parameters': {}}},
        {'id': 'llm_1', 'provider': 'llm_anthropic', 'config': {'apikey': '${ROCKETRIDE_ANTHROPIC_KEY}'}, 'input': [{'lane': 'questions', 'from': 'chat_1'}]},
        {'id': 'response_answers_1', 'provider': 'response_answers', 'config': {}, 'input': [{'lane': 'answers', 'from': 'llm_1'}]}],
        'source': 'chat_1', 'version': 1}
    r = c.post('/run', json={'pipeline': chat, 'question': 'What is 2+2?'}).json()
    check('/run sends "question" to a chat pipeline', r.get('answer') == 'CHAT via llm_anthropic: What is 2+2?', r)
    r = c.post('/run', json={'pipeline': chat, 'text': 'plain text to a chat pipeline'}).json()
    check('/run treats "text" as the question for a chat source', r.get('answer') == 'CHAT via llm_anthropic: plain text to a chat pipeline', r)
    r = c.post('/run', json={'pipeline': chat, 'question': {'questions': [{'text': 'full object'}], 'expectJson': True}}).json()
    check('/run accepts a full Question object', r.get('answer') == 'CHAT via llm_anthropic: full object', r)
    r = c.post('/run', json={'pipeline': chat, 'question': 'TRIGGER_LLM_ERROR'})
    check('/run reports an LLM failure as a 502', r.status_code == 502 and 'LLM error' in r.json().get('error', ''), r.text[:400])
    both = {**chat, 'components': [{'id': 'webhook_1', 'provider': 'webhook', 'config': {'mode': 'Source', 'type': 'webhook'}}] + chat['components']}
    del both['source']
    r = c.post('/run', json={'pipeline': both, 'question': 'which source?'}).json()
    check('/run picks the chat source of a two-source pipeline for a question', r.get('answer') == 'CHAT via llm_anthropic: which source?', r)
    r = c.post('/run', json={'pipeline': both, 'text': 'a document'}).json()
    check('/run picks the other source for text', r.get('answer') == 'ANSWER via llm_anthropic: a document', r)
    r = c.post('/run', json={'pipeline': both, 'source': 'chat_1', 'text': 'named source'}).json()
    check('/run honours an explicit "source"', r.get('answer') == 'CHAT via llm_anthropic: named source', r)
    r = c.post('/run', json={'pipeline': pipe, 'text': 'TRIGGER_NO_OUTPUT'}).json()
    check('/run warns when the pipeline returns nothing', 'no output' in r.get('warning', ''), r)
    r = c.post('/run', json={'pipeline': pipe, 'text': 'TRIGGER_NODE_ERROR'})
    check('/run reports a node failure as a 502', r.status_code == 502 and r.json()['error'] == 'node exploded', r.text[:400])
    check('/run tasks are stopped afterwards', not any('chat' in t for t in c.get('/engine/_tasks').json()['pipelines']), c.get('/engine/_tasks').json())
    before = c.get('/engine/_tasks').json()['count']
    tok = c.post('/engine/task', json={**pipe, 'project_id': 'passthrough-test'}).json()['data']['token']
    r = c.post('/engine/webhook', params={'token': tok}, content=b'via passthrough', headers={'Content-Type': 'text/plain'})
    check('engine passthrough keeps an engine task token', r.status_code == 200 and 'via passthrough' in r.text, r.text[:400])
    c.request('DELETE', '/engine/task', params={'token': tok})
    check('engine passthrough can stop that task by token', c.get('/engine/_tasks').json()['count'] == before, c.get('/engine/_tasks').json())
    r = c.post('/mcp?token=apify_api_FAKE', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    check('MCP initialize proxied with engine key', r.status_code == 200 and r.json()['result']['method'] == 'initialize', r.text)
    r = c.post('/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, headers={'Accept': 'application/json, text/event-stream'})
    check('MCP SSE stream proxied', r.headers['content-type'].startswith('text/event-stream') and 'tools/list' in r.text, r.text)
    check('engine passthrough', c.get('/engine/version').json()['version'] == 'mock-3.6.0')

    # Apify's gateway removes the token and adds X-Apify-User-ID. The server requires the run owner's ID,
    # so code inside the container (a pipeline's HTTP tool) cannot just call this port directly.
    raw = httpx.Client(base_url=base, timeout=30)
    check('no account ID is a 401', raw.get('/pipelines').status_code == 401)
    check('no account ID on /engine is a 401', raw.get('/engine/version').status_code == 401)
    check('no account ID on /run is a 401', raw.post('/run', json={'pipeline': pipe, 'text': 'x'}).status_code == 401)
    check('no account ID on /mcp is a 401', raw.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).status_code == 401)
    check("another account's ID is a 401", raw.get('/pipelines', headers={'X-Apify-User-ID': 'someone-else'}).status_code == 401)
    check('a token without the gateway header is a 401', raw.get('/pipelines', headers={'Authorization': 'Bearer apify_api_FAKE'}).status_code == 401)
    check("the owner's ID is accepted", raw.get('/pipelines', headers={'X-Apify-User-ID': 'owner1'}).status_code == 200)
    check('the readiness probe needs no account ID', raw.get('/', headers={'x-apify-container-server-readiness-probe': '1'}).text == 'OK')
    for target in ['http://127.0.0.1:5599/version', 'http://localhost:4399/pipelines', 'http://169.254.169.254/', 'http://[::1]:4399/']:
        r = c.post('/pipelines/doc-to-text', json={'url': target})
        check(f'url to an internal address is refused: {target}', r.status_code == 400 and 'internal' in r.json().get('error', ''), r.text[:300])
    r = c.post('/pipelines/ask', json={'text': 'd', 'question': ['a', 'b']})
    check('a non-string question is a 400', r.status_code == 400 and 'string' in r.json().get('error', ''), r.text[:300])
finally:
    proc.terminate()
    out = proc.communicate(timeout=10)[0]
check('reserved env var not overridable', "Skipping additionalEnv entry 'ROCKETRIDE_APIKEY'" in out, out[-1500:])
check('provider key names logged, values not', 'ROCKETRIDE_OPENAI_KEY' in out and 'sk-test-123' not in out, out[-1500:])
print('\nALL PASSED' if not failures else f'\n{len(failures)} FAILED: {failures}')
sys.exit(1 if failures else 0)
