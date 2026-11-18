"""RocketRide Server Actor for Apify.

Runs the RocketRide engine on loopback and fronts it with a small HTTP layer that:

* answers Apify's readiness probe (GET / with x-apify-container-server-readiness-probe),
* swaps the caller's Apify token for the engine's internal rr_ key,
* streams the engine's built-in MCP endpoint (/mcp) and HTTP API (/engine/...),
* serves warm, ready-made "featured pipelines" (/pipelines/{name}) and ad-hoc runs (/run).

In Standby (Server) mode it serves forever. Started any other way on the platform
(e.g. Apify's daily default-input test) it runs a quick self-test, writes the
result to the dataset and OUTPUT, and exits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import secrets
import signal
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.background import BackgroundTask  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

import apify_env  # noqa: E402

ACTOR_VERSION = '0.1'
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', stream=sys.stdout)
log = logging.getLogger('rocketride-actor')
logging.getLogger('httpx').setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIR = Path(os.environ.get('ROCKETRIDE_ACTOR_PIPELINES', ROOT / 'pipelines'))
ENGINE_HOME = Path(os.environ.get('ROCKETRIDE_HOME', '/opt/rocketride'))
ENGINE_PORT = int(os.environ.get('ROCKETRIDE_ENGINE_PORT', '5565'))
ENGINE_URL = f'http://127.0.0.1:{ENGINE_PORT}'
PORT = int(os.environ.get('ACTOR_WEB_SERVER_PORT') or os.environ.get('APIFY_CONTAINER_PORT') or '4321')
READINESS_HEADER = 'x-apify-container-server-readiness-probe'
MAX_URL_BYTES = 100 * 1024 * 1024

# Engine-reserved env vars the user may not override through additionalEnv.
RESERVED_ENV = {'ROCKETRIDE_APIKEY', 'ROCKETRIDE_AUTH', 'ROCKETRIDE_URI', 'ROCKETRIDE_ENGINE_PORT', 'ROCKETRIDE_HOME'}
ENV_NAME_RE = re.compile(r'^ROCKETRIDE_[A-Z0-9_]+$')
HOP_BY_HOP = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
    'transfer-encoding', 'upgrade', 'host', 'content-length',
}

FEATURED: dict[str, dict[str, str]] = {
    'doc-to-text': {
        'title': 'Document to text',
        'description': 'Any document (PDF, DOCX, PPTX, XLSX, HTML, images, ...) to clean text, with OCR for embedded images. No API keys needed.',
    },
    'redact-pii': {
        'title': 'Redact PII',
        'description': 'Parse a document, OCR images, detect named entities and return text with personal data anonymized. No API keys needed.',
    },
}


class State:
    engine_key = 'rr_' + secrets.token_urlsafe(24)
    engine_proc: asyncio.subprocess.Process | None = None
    engine_ready = asyncio.Event()
    engine_version: Any = None
    client: httpx.AsyncClient | None = None
    pipelines: dict[str, dict] = {}
    warm_tokens: dict[str, str] = {}
    warm_lock: asyncio.Lock | None = None
    started_at = time.time()


S = State()


# --------------------------------------------------------------------------- engine lifecycle


def build_engine_env(actor_input: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    mapping = {
        'openaiApiKey': 'ROCKETRIDE_OPENAI_KEY',
        'anthropicApiKey': 'ROCKETRIDE_ANTHROPIC_KEY',
        'geminiApiKey': 'ROCKETRIDE_GEMINI_KEY',
    }
    for field, var in mapping.items():
        if actor_input.get(field):
            env[var] = str(actor_input[field])
    extra = actor_input.get('additionalEnv') or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            log.warning('additionalEnv is not valid JSON; ignoring it.')
            extra = {}
    for name, value in (extra or {}).items():
        if not ENV_NAME_RE.match(str(name)) or name in RESERVED_ENV:
            log.warning('Skipping additionalEnv entry %r (must match ROCKETRIDE_* and not be reserved).', name)
            continue
        env[name] = str(value)
    env['ROCKETRIDE_APIKEY'] = S.engine_key
    env['ROCKETRIDE_URI'] = f'ws://127.0.0.1:{ENGINE_PORT}'
    configured = sorted(k for k in env if k.startswith('ROCKETRIDE_') and k.endswith(('_KEY', '_TOKEN')) and k != 'ROCKETRIDE_APIKEY')
    log.info('Provider credentials configured (names only): %s', ', '.join(configured) or 'none')
    return env


async def start_engine(actor_input: dict[str, Any]) -> None:
    cmd_override = os.environ.get('ROCKETRIDE_ENGINE_CMD')
    if cmd_override:
        cmd = cmd_override.split()
    else:
        cmd = [str(ENGINE_HOME / 'engine'), './ai/eaas.py', '--host=127.0.0.1', f'--port={ENGINE_PORT}']
        if actor_input.get('verboseLogging'):
            cmd.append('--verbose')
    log.info('Starting RocketRide engine: %s', ' '.join(cmd))
    S.engine_proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(ENGINE_HOME) if ENGINE_HOME.exists() else None, env=build_engine_env(actor_input)
    )


async def wait_engine_ready(timeout: float = 300) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(base_url=ENGINE_URL, timeout=5) as c:
        while time.monotonic() < deadline:
            if S.engine_proc and S.engine_proc.returncode is not None:
                raise RuntimeError(f'Engine exited during startup with code {S.engine_proc.returncode}')
            try:
                r = await c.get('/version')
                if r.status_code == 200:
                    try:
                        v = r.json()
                        S.engine_version = v.get('data', v) if isinstance(v, dict) else v
                    except ValueError:
                        S.engine_version = r.text.strip()
                    S.engine_ready.set()
                    log.info('Engine ready in %.1fs, version: %s', time.time() - S.started_at, S.engine_version)
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise TimeoutError('RocketRide engine did not become ready in time')


async def watch_engine() -> None:
    """Exit the Actor if the engine dies, so the platform replaces the run."""
    if not S.engine_proc:
        return
    code = await S.engine_proc.wait()
    log.error('RocketRide engine exited with code %s; stopping the Actor.', code)
    os._exit(1)


def load_pipelines() -> None:
    for name in FEATURED:
        path = PIPELINE_DIR / f'{name}.pipe'
        S.pipelines[name] = json.loads(path.read_text())


# --------------------------------------------------------------------------- engine HTTP helpers


def engine_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {'Authorization': f'Bearer {S.engine_key}'}
    if extra:
        h.update(extra)
    return h


def unwrap(payload: Any) -> Any:
    """Engine responses look like {"status": "OK", "data": ...} or {"status": "Error", "error": ...}."""
    if isinstance(payload, dict) and 'status' in payload:
        if str(payload.get('status')).upper() not in ('OK', 'SUCCESS'):
            err = payload.get('error') or payload
            raise EngineError(err.get('message') if isinstance(err, dict) else str(err), payload)
        return payload.get('data')
    return payload


class EngineError(Exception):
    def __init__(self, message: str, payload: Any = None):
        super().__init__(message)
        self.payload = payload


async def start_task(pipeline: dict) -> str:
    assert S.client
    r = await S.client.post('/task', json=pipeline, headers=engine_headers())
    data = unwrap(r.json())
    token = (data or {}).get('token')
    if not token:
        raise EngineError('Engine did not return a task token', r.json())
    return token


async def stop_task(token: str) -> None:
    if not S.client:
        return
    try:
        await S.client.delete('/task', params={'token': token}, headers=engine_headers())
    except httpx.HTTPError:
        pass


async def send_to_task(token: str, body: bytes, content_type: str, filename: str | None) -> Any:
    assert S.client
    headers = engine_headers({'Content-Type': content_type})
    if filename:
        headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    r = await S.client.post('/webhook', params={'token': token}, content=body, headers=headers)
    return unwrap(r.json())


async def warm_token(name: str, refresh: bool = False) -> str:
    assert S.warm_lock
    async with S.warm_lock:
        if refresh and name in S.warm_tokens:
            await stop_task(S.warm_tokens.pop(name))
        if name not in S.warm_tokens:
            S.warm_tokens[name] = await start_task(S.pipelines[name])
            log.info('Featured pipeline %s is warm.', name)
        return S.warm_tokens[name]


def extract_text(result: Any) -> str | None:
    """Best-effort: collect 'text' lane output from a webhook DataResult."""
    texts: list[str] = []

    def walk(node: Any, key: str | None = None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, str) and key == 'text':
            texts.append(node)

    objects = result.get('objects') if isinstance(result, dict) else None
    walk(objects if objects is not None else result)
    return '\n'.join(t.strip('\n') for t in texts).strip() if texts else None


async def read_payload(request: Request) -> tuple[bytes, str, str | None]:
    """Accept raw bytes, multipart (first file), or JSON {"text": ...} / {"url": ...}."""
    ctype = request.headers.get('content-type', 'application/octet-stream').split(';')[0].strip().lower()
    filename = request.query_params.get('filename')

    if ctype == 'application/json':
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('JSON body must be an object with "text" or "url".')
        if data.get('text') is not None:
            return str(data['text']).encode('utf-8'), 'text/plain', data.get('filename') or filename or 'input.txt'
        if data.get('url'):
            return await download(str(data['url']), data.get('filename') or filename)
        raise ValueError('JSON body must include "text" or "url".')

    if ctype == 'multipart/form-data':
        form = await request.form()
        for value in form.values():
            if hasattr(value, 'read'):
                content = await value.read()
                return content, (value.content_type or 'application/octet-stream'), value.filename or filename
        raise ValueError('Multipart body has no file field.')

    body = await request.body()
    if not body:
        raise ValueError('Empty request body. Send a file, or JSON {"text": ...} or {"url": ...}.')
    return body, ctype, filename


async def download(url: str, filename: str | None) -> tuple[bytes, str, str | None]:
    if not url.startswith(('http://', 'https://')):
        raise ValueError('url must start with http:// or https://')
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        async with c.stream('GET', url) as r:
            r.raise_for_status()
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > MAX_URL_BYTES:
                    raise ValueError('Remote file is larger than 100 MB.')
                chunks.append(chunk)
            ctype = r.headers.get('content-type', '').split(';')[0].strip().lower()
    name = filename or (url.split('?')[0].rstrip('/').rsplit('/', 1)[-1] or 'download')
    if not ctype or ctype == 'application/octet-stream':
        ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    return b''.join(chunks), ctype, name


# --------------------------------------------------------------------------- HTTP routes


async def ensure_ready() -> Response | None:
    try:
        await asyncio.wait_for(S.engine_ready.wait(), timeout=180)
        return None
    except asyncio.TimeoutError:
        return JSONResponse({'error': 'RocketRide engine is still starting, retry shortly.'}, status_code=503)


def base_url(request: Request) -> str:
    return os.environ.get('ACTOR_STANDBY_URL') or str(request.base_url).rstrip('/')


async def root(request: Request) -> Response:
    if READINESS_HEADER in request.headers:
        return PlainTextResponse('OK')
    url = base_url(request)
    return JSONResponse({
        'name': 'RocketRide',
        'actorVersion': ACTOR_VERSION,
        'engineReady': S.engine_ready.is_set(),
        'engineVersion': S.engine_version,
        'endpoints': {
            'mcp': f'{url}/mcp',
            'featuredPipelines': f'{url}/pipelines',
            'runPipeline': f'{url}/run',
            'engineApi': f'{url}/engine/...',
            'health': f'{url}/health',
        },
        'auth': 'Authorization: Bearer <APIFY_TOKEN>',
        'docs': 'https://docs.rocketride.org/',
    })


async def health(request: Request) -> Response:
    ok = S.engine_ready.is_set() and S.engine_proc is not None and S.engine_proc.returncode is None
    return JSONResponse({'ok': ok, 'engineVersion': S.engine_version}, status_code=200 if ok else 503)


async def list_pipelines(request: Request) -> Response:
    url = base_url(request)
    return JSONResponse([
        {'name': n, **meta, 'endpoint': f'{url}/pipelines/{n}', 'pipeline': S.pipelines.get(n)}
        for n, meta in FEATURED.items()
    ])


async def run_featured(request: Request) -> Response:
    name = request.path_params['name']
    if name not in FEATURED:
        return JSONResponse({'error': f'Unknown pipeline {name!r}', 'available': list(FEATURED)}, status_code=404)
    if (resp := await ensure_ready()) is not None:
        return resp
    try:
        body, ctype, filename = await read_payload(request)
    except (ValueError, httpx.HTTPError) as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    started = time.time()
    try:
        token = await warm_token(name)
        try:
            result = await send_to_task(token, body, ctype, filename)
        except (EngineError, httpx.HTTPError):
            token = await warm_token(name, refresh=True)  # task may have been recycled; retry once
            result = await send_to_task(token, body, ctype, filename)
    except EngineError as e:
        return JSONResponse({'error': str(e), 'engine': e.payload}, status_code=502)
    except httpx.HTTPError as e:
        return JSONResponse({'error': f'Engine request failed: {e}'}, status_code=502)

    out = {
        'pipeline': name,
        'filename': filename,
        'contentType': ctype,
        'bytes': len(body),
        'durationMs': int((time.time() - started) * 1000),
        'text': extract_text(result),
        'result': result,
    }
    if request.query_params.get('save') in ('1', 'true'):
        await apify_env.push_data({k: v for k, v in out.items() if k != 'result'})
    return JSONResponse(out)


async def run_adhoc(request: Request) -> Response:
    """POST /run  {"pipeline": {...}, "text": "..."} or {"pipeline": {...}, "url": "..."}"""
    if (resp := await ensure_ready()) is not None:
        return resp
    try:
        data = await request.json()
        pipeline = data.get('pipeline') if isinstance(data, dict) else None
        if not isinstance(pipeline, dict) or 'components' not in pipeline:
            raise ValueError('Body must be JSON with a "pipeline" object (a .pipe file) plus "text" or "url".')
        if data.get('text') is not None:
            body, ctype, filename = str(data['text']).encode(), 'text/plain', data.get('filename') or 'input.txt'
        elif data.get('url'):
            body, ctype, filename = await download(str(data['url']), data.get('filename'))
        else:
            raise ValueError('Include "text" or "url" to send into the pipeline.')
    except (ValueError, httpx.HTTPError) as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    token = None
    try:
        token = await start_task(pipeline)
        result = await send_to_task(token, body, ctype, filename)
        return JSONResponse({'text': extract_text(result), 'result': result})
    except EngineError as e:
        return JSONResponse({'error': str(e), 'engine': e.payload}, status_code=502)
    except httpx.HTTPError as e:
        return JSONResponse({'error': f'Engine request failed: {e}'}, status_code=502)
    finally:
        if token:
            await stop_task(token)


async def proxy(request: Request) -> Response:
    """Stream any request through to the engine with the internal key."""
    if (resp := await ensure_ready()) is not None:
        return resp
    assert S.client
    path = request.url.path
    if path.startswith('/engine'):
        path = path[len('/engine'):] or '/'
    params = [(k, v) for k, v in request.query_params.multi_items() if k != 'token']  # drop the Apify token
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != 'authorization'}
    headers = {k: v for k, v in headers.items() if not k.lower().startswith('x-apify-')}
    headers['Authorization'] = f'Bearer {S.engine_key}'

    body = await request.body()
    upstream = S.client.build_request(request.method, path, params=params, headers=headers, content=body or None)
    try:
        r = await S.client.send(upstream, stream=True)
    except httpx.HTTPError as e:
        return JSONResponse({'error': f'Engine request failed: {e}'}, status_code=502)
    out_headers = {k: v for k, v in r.headers.items() if k.lower() not in HOP_BY_HOP}
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out_headers, background=BackgroundTask(r.aclose))


ALL = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD']
routes = [
    Route('/', root, methods=['GET', 'HEAD']),
    Route('/health', health, methods=['GET']),
    Route('/pipelines', list_pipelines, methods=['GET']),
    Route('/pipelines/{name}', run_featured, methods=['POST']),
    Route('/run', run_adhoc, methods=['POST']),
    Route('/mcp', proxy, methods=ALL),
    Route('/mcp/{rest:path}', proxy, methods=ALL),
    Route('/.well-known/{rest:path}', proxy, methods=['GET']),
    Route('/engine', proxy, methods=ALL),
    Route('/engine/{rest:path}', proxy, methods=ALL),
]
app = Starlette(routes=routes)


# --------------------------------------------------------------------------- entry points


async def self_test() -> int:
    """Non-standby run: prove the engine and a featured pipeline work, then exit."""
    report: dict[str, Any] = {'mode': 'self-test', 'engineVersion': S.engine_version, 'featuredPipelines': list(FEATURED)}
    try:
        token = await warm_token('doc-to-text')
        result = await send_to_task(token, b'RocketRide on Apify: self-test document.', 'text/plain', 'self-test.txt')
        report['docToText'] = {'ok': True, 'text': extract_text(result)}
    except Exception as e:  # noqa: BLE001
        report['docToText'] = {'ok': False, 'error': str(e)}
        log.warning('Featured pipeline self-test failed: %s', e)
    report['standbyUrl'] = os.environ.get('ACTOR_STANDBY_URL')
    report['ok'] = True
    await apify_env.push_data(report)
    await apify_env.set_output(report)
    log.info('Self-test finished. Use the Standby URL for MCP/HTTP access: %s', report['standbyUrl'])
    return 0


def sample_png() -> bytes | None:
    """A small image with text, to pull OCR models at build time. Best effort."""
    try:
        import io

        from PIL import Image, ImageDraw

        img = Image.new('RGB', (640, 160), 'white')
        ImageDraw.Draw(img).text((20, 60), 'RocketRide on Apify OCR warm-up 12345', fill='black')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


async def prebake() -> int:
    """Docker build step: run each featured pipeline once so node dependencies and
    models are installed into the image instead of on a user's first request."""
    samples: list[tuple[bytes, str, str]] = [
        (b'Contact John Smith at john.smith@example.com in Seattle.', 'text/plain', 'warmup.txt')
    ]
    png = sample_png()
    if png:
        samples.append((png, 'image/png', 'warmup.png'))
    for name in FEATURED:
        for body, ctype, filename in samples:
            for attempt in range(3):
                try:
                    token = await warm_token(name, refresh=attempt > 0)
                    started = time.time()
                    await send_to_task(token, body, ctype, filename)
                    log.info('Prebaked %s with %s in %.1fs', name, filename, time.time() - started)
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning('Prebake %s/%s attempt %d failed: %s', name, filename, attempt + 1, e)
    for token in list(S.warm_tokens.values()):
        await stop_task(token)
    return 0


async def main() -> int:
    actor_input = apify_env.load_input()
    load_pipelines()
    S.warm_lock = asyncio.Lock()
    S.client = httpx.AsyncClient(base_url=ENGINE_URL, timeout=httpx.Timeout(600, connect=10))

    mode = os.environ.get('ROCKETRIDE_ACTOR_MODE') or (
        'selftest' if apify_env.is_at_home() and not apify_env.is_standby() else 'server'
    )
    await start_engine(actor_input)
    engine_watch: asyncio.Task | None = None

    if mode in ('selftest', 'prebake'):
        await wait_engine_ready()
        code = await (prebake() if mode == 'prebake' else self_test())
        if S.engine_proc and S.engine_proc.returncode is None:
            S.engine_proc.terminate()
        return code

    server = uvicorn.Server(uvicorn.Config(app, host='0.0.0.0', port=PORT, log_level='info', access_log=False))

    async def boot() -> None:
        nonlocal engine_watch
        try:
            await wait_engine_ready()
        except Exception as e:  # noqa: BLE001
            log.error('Engine failed to start: %s', e)
            os._exit(1)
        engine_watch = asyncio.create_task(watch_engine())
        if actor_input.get('warmFeaturedPipelines', True):
            for name in FEATURED:
                for attempt in range(2):
                    try:
                        await warm_token(name)
                        break
                    except Exception as e:  # noqa: BLE001
                        log.warning('Could not pre-warm %s (attempt %d): %s', name, attempt + 1, e)

    asyncio.create_task(boot())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: setattr(server, 'should_exit', True))
        except NotImplementedError:
            pass
    log.info('RocketRide Actor listening on port %s (standby=%s)', PORT, apify_env.is_standby())
    await server.serve()
    if S.engine_proc and S.engine_proc.returncode is None:
        S.engine_proc.terminate()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
