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
    c = httpx.Client(base_url=base, timeout=30, headers={'Authorization': 'Bearer apify_api_FAKE'})
    check('readiness probe returns OK', c.get('/', headers={'x-apify-container-server-readiness-probe': '1'}).text == 'OK')
    for _ in range(50):
        if c.get('/health').status_code == 200: break
        time.sleep(0.2)
    check('health ok', c.get('/health').json()['ok'] is True)
    info = c.get('/').json(); check('root lists mcp endpoint', info['endpoints']['mcp'].endswith('/mcp'), info)
    pl = c.get('/pipelines').json(); check('two featured pipelines', {p['name'] for p in pl} == {'doc-to-text', 'redact-pii'}, pl)
    r = c.post('/pipelines/doc-to-text', json={'text': 'hello world'}).json()
    check('doc-to-text via JSON text', r.get('text', '').endswith('hello world') and '[text/plain|' in r['text'], r)
    check('secret OpenAI key decrypted into engine env', 'sk-test-123' in r.get('text', ''), r)
    r = c.post('/pipelines/redact-pii?filename=a.pdf', content=b'%PDF raw', headers={'Content-Type': 'application/pdf'}).json()
    check('redact-pii raw upload', 'application/pdf' in (r.get('text') or '') and r['filename'] == 'a.pdf', r)
    r = c.post('/pipelines/doc-to-text', files={'file': ('n.txt', b'multi part', 'text/plain')}).json()
    check('multipart upload', 'multi part' in (r.get('text') or ''), r)
    check('unknown pipeline 404', c.post('/pipelines/nope', json={'text': 'x'}).status_code == 404)
    check('empty body 400', c.post('/pipelines/doc-to-text', content=b'').status_code == 400)
    pipe = json.loads((ROOT / 'pipelines/doc-to-text.pipe').read_text())
    r = c.post('/run', json={'pipeline': pipe, 'text': 'adhoc'}).json(); check('/run ad-hoc', 'adhoc' in (r.get('text') or ''), r)
    r = c.post('/mcp?token=apify_api_FAKE', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    check('MCP initialize proxied with engine key', r.status_code == 200 and r.json()['result']['method'] == 'initialize', r.text)
    r = c.post('/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, headers={'Accept': 'application/json, text/event-stream'})
    check('MCP SSE stream proxied', r.headers['content-type'].startswith('text/event-stream') and 'tools/list' in r.text, r.text)
    check('engine passthrough', c.get('/engine/version').json()['version'] == 'mock-3.6.0')
finally:
    proc.terminate()
    out = proc.communicate(timeout=10)[0]
check('reserved env var not overridable', "Skipping additionalEnv entry 'ROCKETRIDE_APIKEY'" in out, out[-1500:])
check('provider key names logged, values not', 'ROCKETRIDE_OPENAI_KEY' in out and 'sk-test-123' not in out, out[-1500:])
print('\nALL PASSED' if not failures else f'\n{len(failures)} FAILED: {failures}')
sys.exit(1 if failures else 0)
