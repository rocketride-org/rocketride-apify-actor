"""RocketRide Server Actor for Apify.

Runs the RocketRide engine on loopback and fronts it with a small HTTP layer that:

* answers Apify's readiness probe (GET / with x-apify-container-server-readiness-probe),
* swaps the caller's Apify token for the engine's internal rr_ key,
* streams the engine's built-in MCP endpoint (/mcp) and HTTP API (/engine/...),
* serves ready-made pipelines (/pipelines/{name}): document pipelines that need no keys,
  and LLM pipelines (summarize, extract-data, ask, agent) built per request,
* runs any webhook-source pipeline once (/run).

In Standby (Server) mode it serves forever. Started any other way on the platform
(e.g. Apify's daily default-input test) it runs a quick self-test, writes the
result to the dataset and OUTPUT, and exits.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import logging
import mimetypes
import os
import re
import secrets
import signal
import sys
import time
import uuid
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
import pipelines  # noqa: E402

ACTOR_VERSION = '0.2'
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', stream=sys.stdout)
log = logging.getLogger('rocketride-actor')
logging.getLogger('httpx').setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parent.parent
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

CATALOG = pipelines.CATALOG
STATIC = [n for n, c in CATALOG.items() if not c['needsLlm']]
MAX_WARM_LLM = 3  # LLM pipelines kept running at once; the oldest is stopped beyond this
MOCK_LLM_PORT = 9911


class State:
    engine_key = 'rr_' + secrets.token_urlsafe(24)
    engine_proc: asyncio.subprocess.Process | None = None
    engine_ready = asyncio.Event()
    engine_version: Any = None
    client: httpx.AsyncClient | None = None
    env: dict[str, str] = {}
    warm: 'OrderedDict[str, str]' = None  # cache key -> task token
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
    mapping.update({
        'llmBaseUrl': pipelines.COMPAT_BASE_URL_ENV,
        'llmModel': pipelines.COMPAT_MODEL_ENV,
        'llmApiKey': pipelines.PROVIDERS['openai-compatible']['key_env'],
        'defaultModel': pipelines.DEFAULT_MODEL_ENV,
    })
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
    # Nodes outside the ready-made pipelines install their packages on first use. uv's cache would keep a
    # second copy of each one, and an Actor run's disk is small: without this, a few agent frameworks fill
    # it and the next install fails with "No space left on device".
    env.setdefault('UV_NO_CACHE', '1')
    configured = sorted(k for k in env if k.startswith('ROCKETRIDE_') and k.endswith(('_KEY', '_TOKEN')) and k != 'ROCKETRIDE_APIKEY')
    log.info('Provider credentials configured (names only): %s', ', '.join(configured) or 'none')
    if env.get(pipelines.COMPAT_BASE_URL_ENV) and not env.get(pipelines.PROVIDERS['openai-compatible']['key_env']):
        env[pipelines.PROVIDERS['openai-compatible']['key_env']] = 'none'  # keyless local endpoints
    S.env = env
    log.info('LLM providers available to ready-made pipelines: %s', ', '.join(pipelines.configured_providers(env)) or 'none')
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


async def warm_token(name: str, pipeline: dict, refresh: bool = False) -> str:
    """Token of a running task for this exact pipeline, starting it if needed."""
    assert S.warm_lock
    key = pipelines.cache_key(name, pipeline)
    async with S.warm_lock:
        if refresh and key in S.warm:
            await stop_task(S.warm.pop(key))
        if key in S.warm:
            S.warm.move_to_end(key)
            return S.warm[key]
        token = await start_task(pipeline)
        S.warm[key] = token
        log.info('Pipeline %s is running.', name)
        llm_keys = [k for k in S.warm if k.split(':', 1)[0] not in STATIC]
        while len(llm_keys) > MAX_WARM_LLM:
            await stop_task(S.warm.pop(llm_keys.pop(0)))
        return token


async def drop_warm(name: str, pipeline: dict) -> None:
    """Stop and forget this pipeline's warm task, so the next request starts a fresh one."""
    assert S.warm_lock
    key = pipelines.cache_key(name, pipeline)
    async with S.warm_lock:
        if key in S.warm:
            await stop_task(S.warm.pop(key))


def lanes(result: Any) -> dict[str, list]:
    """Group a webhook result's output by lane (text, answers, ...) across objects."""
    out: dict[str, list] = {}
    objects = result.get('objects') if isinstance(result, dict) else None
    for obj in (objects or {}).values():
        if not isinstance(obj, dict):
            continue
        for lane in ('text', 'answers', 'documents', 'table'):
            value = obj.get(lane)
            if value is None:
                continue
            out.setdefault(lane, []).extend(value if isinstance(value, list) else [value])
    return out


LLM_ERROR_PREFIXES = ('**LLM error**', 'LLM error:')


def object_errors(result: Any) -> list[str]:
    """Errors the engine reports per object, including LLM failures it returns as an answer."""
    errs = []
    objects = result.get('objects') if isinstance(result, dict) else None
    for obj in (objects or {}).values():
        if not isinstance(obj, dict):
            continue
        err = obj.get('error')
        if err:
            errs.append(str(err.get('message', err)) if isinstance(err, dict) else str(err))
        elif str(obj.get('status', 'OK')).upper() not in ('OK', 'SUCCESS'):
            errs.append(str(obj.get('message') or obj.get('status')))
        for a in obj.get('answers') or []:
            if isinstance(a, str) and a.startswith(LLM_ERROR_PREFIXES):
                errs.append(a.replace('**', '').strip())
    return errs


# Errors the engine reports when the task itself is gone: 'Connection closed' when it dies mid-request
# (for example, killed for running out of memory), 'Stopped' on every request after that.
TASK_DIED_ERRORS = ('Stopped', 'Connection closed')
TASK_DIED_HINT = ('The pipeline stopped while processing this document. This usually means the server ran out of '
                  'memory: OCR of large, image-heavy files can need 8 GB. Retry without "ocr", or raise the memory '
                  'in your task\'s Server settings. The pipeline restarts on the next request.')


def task_died(result: Any) -> str | None:
    """The engine error showing the pipeline's task is no longer running, if the result has one."""
    objects = result.get('objects') if isinstance(result, dict) else None
    for obj in (objects or {}).values():
        err = obj.get('error') if isinstance(obj, dict) else None
        message = err.get('message') if isinstance(err, dict) else err
        if message in TASK_DIED_ERRORS:
            return message
    return None


OBJECT_METADATA = {'name', 'path', 'objectId', 'status', 'error', 'message'}


def has_output(result: Any) -> bool:
    """True if any object came back with something besides metadata (text, answers, audio, ...)."""
    objects = result.get('objects') if isinstance(result, dict) else None
    return any(isinstance(o, dict) and any(v for k, v in o.items() if k not in OBJECT_METADATA)
               for o in (objects or {}).values())


def answer_text(value: Any) -> str:
    if isinstance(value, dict):
        for k in ('answer', 'text', 'content'):
            if value.get(k) is not None:
                return answer_text(value[k])
        return json.dumps(value)
    return str(value)


def shape_output(result: Any) -> dict[str, Any]:
    """Friendly top-level fields: text (text lane), answer (answers lane) and data (answer parsed as JSON)."""
    by_lane = lanes(result)
    out: dict[str, Any] = {}
    if by_lane.get('text'):
        out['text'] = '\n'.join(str(t).strip('\n') for t in by_lane['text']).strip()
    if by_lane.get('answers'):
        answers = by_lane['answers']
        first = answers[0]
        if isinstance(first, (list, dict)) and not (isinstance(first, dict) and 'answer' in first):
            out['data'] = first if len(answers) == 1 else answers  # structured answer, e.g. extract-data rows
        else:
            texts = [answer_text(a) for a in answers]
            out['answer'] = texts[0] if len(texts) == 1 else texts
            try:
                parsed = json.loads(texts[0])
                if isinstance(parsed, (list, dict)):
                    out['data'] = parsed
            except (ValueError, TypeError):
                pass
    return out


def extract_text(result: Any) -> str | None:
    """Collect 'text' lane output from a webhook result (used by /run and the self-test)."""
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


def _pick_params(source: Any) -> dict[str, Any]:
    return {k: source[k] for k in pipelines.PARAM_NAMES if k in source and source[k] is not None}


async def read_request(request: Request, input_kind: str = 'document') -> tuple[bytes, str, str | None, dict[str, Any]]:
    """Payload and pipeline parameters from raw bytes, multipart, or JSON.

    JSON: {"text": ...} or {"url": ...} plus parameters. Raw/multipart: parameters in the query string.
    For input_kind 'question' the payload is the question itself.
    """
    ctype = request.headers.get('content-type', 'application/octet-stream').split(';')[0].strip().lower()
    filename = request.query_params.get('filename')
    params = _pick_params(request.query_params)

    if ctype == 'application/json':
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('JSON body must be an object.')
        params.update(_pick_params(data))
        if input_kind == 'question':
            question = data.get('question') or data.get('text')
            if not question:
                raise ValueError('JSON body must include "question".')
            return str(question).encode('utf-8'), 'text/plain', 'question.txt', params
        if data.get('text') is not None:
            return str(data['text']).encode('utf-8'), 'text/plain', data.get('filename') or filename or 'input.txt', params
        if data.get('url'):
            return (*await download(str(data['url']), data.get('filename') or filename), params)
        raise ValueError('JSON body must include "text" or "url".')

    if input_kind == 'question':
        question = params.get('question') or (await request.body()).decode('utf-8', 'replace').strip()
        if not question:
            raise ValueError('Send the question as JSON {"question": ...} or as a plain-text body.')
        return str(question).encode('utf-8'), 'text/plain', 'question.txt', params

    if ctype == 'multipart/form-data':
        form = await request.form()
        params.update(_pick_params({k: v for k, v in form.items() if isinstance(v, str)}))
        for value in form.values():
            if hasattr(value, 'read'):
                content = await value.read()
                return content, (value.content_type or 'application/octet-stream'), value.filename or filename, params
        raise ValueError('Multipart body has no file field.')

    body = await request.body()
    if not body:
        raise ValueError('Empty request body. Send a file, or JSON {"text": ...} or {"url": ...}.')
    return body, ctype, filename, params


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
        'pipelines': list(CATALOG),
        'llmProviders': pipelines.configured_providers(S.env),
        'auth': 'Authorization: Bearer <APIFY_TOKEN>',
        'docs': 'https://docs.rocketride.org/',
    })


async def health(request: Request) -> Response:
    ok = S.engine_ready.is_set() and S.engine_proc is not None and S.engine_proc.returncode is None
    return JSONResponse({'ok': ok, 'engineVersion': S.engine_version}, status_code=200 if ok else 503)


def describe(name: str, url: str) -> dict[str, Any]:
    c = CATALOG[name]
    return {
        'name': name,
        'title': c['title'],
        'description': c['description'],
        'needsLlm': c['needsLlm'],
        'input': c['input'],
        'params': c['params'],
        'endpoint': f'{url}/pipelines/{name}',
    }


async def list_pipelines(request: Request) -> Response:
    url = base_url(request)
    return JSONResponse({
        'llmProviders': pipelines.configured_providers(S.env),
        'pipelines': [describe(n, url) for n in CATALOG],
    })


async def get_pipeline(request: Request) -> Response:
    """The .pipe definition this endpoint would run for the given parameters (keys stay as ${VARS})."""
    name = request.path_params['name']
    if name not in CATALOG:
        return JSONResponse({'error': f'Unknown pipeline {name!r}', 'available': list(CATALOG)}, status_code=404)
    info = describe(name, base_url(request))
    try:
        info['pipeline'] = pipelines.build(name, _pick_params(request.query_params), S.env)
    except pipelines.PipelineError as e:
        info['pipeline'] = None
        info['note'] = str(e)
    return JSONResponse(info)


async def run_featured(request: Request) -> Response:
    name = request.path_params['name']
    if name not in CATALOG:
        return JSONResponse({'error': f'Unknown pipeline {name!r}', 'available': list(CATALOG)}, status_code=404)
    if (resp := await ensure_ready()) is not None:
        return resp
    try:
        body, ctype, filename, params = await read_request(request, CATALOG[name]['input'])
        if 'ocr' in CATALOG[name]['params'] and 'ocr' not in params and ctype.startswith('image/'):
            params['ocr'] = True  # an image has no text without OCR
        pipeline = pipelines.build(name, params, S.env)
    except (ValueError, httpx.HTTPError) as e:  # PipelineError is a ValueError
        return JSONResponse({'error': str(e)}, status_code=400)

    started = time.time()
    try:
        token = await warm_token(name, pipeline)
        try:
            result = await send_to_task(token, body, ctype, filename)
        except (EngineError, httpx.HTTPError):
            token = await warm_token(name, pipeline, refresh=True)  # task may have been recycled; retry once
            result = await send_to_task(token, body, ctype, filename)
        else:
            if task_died(result) == 'Stopped':  # the warm task died on an earlier request; start a fresh one
                log.warning('Pipeline %s had stopped; restarting it.', name)
                token = await warm_token(name, pipeline, refresh=True)
                result = await send_to_task(token, body, ctype, filename)
        if task_died(result):  # died on this request: never hand the dead task to the next one
            log.warning('Pipeline %s stopped while processing %s (%d bytes); it restarts on the next request.', name, filename, len(body))
            await drop_warm(name, pipeline)
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
        **shape_output(result),
    }
    errors = object_errors(result)
    if errors:
        for k in ('answer', 'data'):
            out.pop(k, None)
        out['error'] = errors[0]
        if task_died(result):
            out['hint'] = TASK_DIED_HINT
        out['result'] = result
        return JSONResponse(out, status_code=502)
    if request.query_params.get('save') in ('1', 'true'):
        await apify_env.push_data(dict(out))
    out['result'] = result
    return JSONResponse(out)


def source_component(pipeline: dict, wants_question: bool = False) -> dict | None:
    """The component a pipeline takes its input from: its "source" id, else its Source-mode component.

    A pipeline can have several sources (say a dropper for files and a chat for questions); the engine
    then needs one named, so pick the chat source for a question and the other one for text or a file.
    """
    comps = [c for c in pipeline.get('components') or [] if isinstance(c, dict)]
    by_id = {c.get('id'): c for c in comps}
    if pipeline.get('source') in by_id:
        return by_id[pipeline['source']]
    srcs = [c for c in comps if (c.get('config') or {}).get('mode') == 'Source']
    if len(srcs) > 1:
        preferred = [c for c in srcs if (c.get('provider') == 'chat') == wants_question]
        return (preferred or srcs)[0]
    return srcs[0] if srcs else None


QUESTION_MIME = 'application/rocketride-question'


def question_payload(question: Any) -> bytes:
    """A chat question as the engine expects it: the SDK's Question JSON (a plain string, or a full Question object)."""
    if isinstance(question, dict):
        return json.dumps(question).encode()
    return json.dumps({'type': 'question', 'questions': [{'text': str(question)}], 'expectJson': False}).encode()


async def ask_task(project_id: str, source_id: str, payload: bytes) -> Any:
    """Send a question to a running chat source. The path-addressed webhook routes it to that source."""
    assert S.client
    r = await S.client.post(f'/webhook/{project_id}/{source_id}', content=payload,
                            headers=engine_headers({'Content-Type': QUESTION_MIME}))
    return unwrap(r.json())


async def run_adhoc(request: Request) -> Response:
    """POST /run  {"pipeline": {...}} plus "text", "url" or (for chat pipelines) "question"."""
    if (resp := await ensure_ready()) is not None:
        return resp
    try:
        data = await request.json()
        pipeline = data.get('pipeline') if isinstance(data, dict) else None
        if not isinstance(pipeline, dict) or 'components' not in pipeline:
            raise ValueError('Body must be JSON with a "pipeline" object (a .pipe file) plus "text", "url" or "question".')
        # The engine keys tasks by project_id, so a definition taken from GET /pipelines/{name} would clash
        # with that pipeline's warm task ("Pipeline is already running"). A one-shot run gets its own.
        pipeline = {**pipeline, 'project_id': str(uuid.uuid4())}
        if data.get('source'):
            pipeline['source'] = str(data['source'])
        question = data.get('question')
        source = source_component(pipeline, wants_question=question is not None) or {}
        if question is None and source.get('provider') == 'chat' and data.get('text') is not None:
            question = data['text']  # a chat source only reads questions, so plain text is the question
        if source.get('id'):
            pipeline['source'] = source['id']  # the engine refuses to start a multi-source pipeline without one
        if question is not None:
            if not source.get('id'):
                raise ValueError('"question" needs a pipeline with a source component (for example "chat").')
            body, ctype, filename = question_payload(question), QUESTION_MIME, None
        elif data.get('text') is not None:
            body, ctype, filename = str(data['text']).encode(), 'text/plain', data.get('filename') or 'input.txt'
        elif data.get('url'):
            body, ctype, filename = await download(str(data['url']), data.get('filename'))
        else:
            raise ValueError('Include "text" or "url" (or "question" for a chat pipeline) to send into the pipeline.')
    except (ValueError, httpx.HTTPError) as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    token = None
    try:
        token = await start_task(pipeline)
        if ctype == QUESTION_MIME:
            result = await ask_task(pipeline['project_id'], source['id'], body)
        else:
            result = await send_to_task(token, body, ctype, filename)
        out = {'text': extract_text(result), **shape_output(result)}
        errors = object_errors(result)
        if errors:
            return JSONResponse({'error': errors[0], **out, 'result': result}, status_code=502)
        if not has_output(result):
            out['warning'] = ('The pipeline returned no output. Check that a response node is wired to the lane you '
                              'expect, and that the input matches the source (chat pipelines take "question").')
        return JSONResponse({**out, 'result': result})
    except EngineError as e:
        return JSONResponse({'error': str(e), 'engine': e.payload}, status_code=502)
    except httpx.HTTPError as e:
        return JSONResponse({'error': f'Engine request failed: {e}'}, status_code=502)
    finally:
        if token:
            await stop_task(token)


ENGINE_TOKEN_PREFIXES = ('tk_', 'pk_')


async def proxy(request: Request) -> Response:
    """Stream any request through to the engine with the internal key."""
    if (resp := await ensure_ready()) is not None:
        return resp
    assert S.client
    path = request.url.path
    if path.startswith('/engine'):
        path = path[len('/engine'):] or '/'
    # Drop the Apify token, but keep an engine task token (tk_/pk_), which engine calls like /webhook and /task need.
    params = [(k, v) for k, v in request.query_params.multi_items() if k != 'token' or v.startswith(ENGINE_TOKEN_PREFIXES)]
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
    Route('/pipelines/{name}', get_pipeline, methods=['GET']),
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
    report: dict[str, Any] = {'mode': 'self-test', 'engineVersion': S.engine_version, 'pipelines': list(CATALOG)}
    try:
        token = await warm_token('doc-to-text', pipelines.build('doc-to-text', {}, S.env))
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
        import importlib
        import io

        importlib.invalidate_caches()  # Pillow may have been installed after this process started
        from PIL import Image, ImageDraw

        img = Image.new('RGB', (640, 160), 'white')
        ImageDraw.Draw(img).text((20, 60), 'RocketRide on Apify OCR warm-up 12345', fill='black')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


async def prebake() -> int:
    """Docker build step: run every ready-made pipeline once so node dependencies and models are
    installed into the image instead of on a user's first request. LLM pipelines talk to the
    built-in mock LLM, so no real key is involved."""
    doc = b'Invoice INV-2026-0042 from Acme Robotics to John Smith (john.smith@example.com), Seattle. Total due: 960.00 USD.'
    # The image sample is rendered lazily: Pillow is only importable once a pipeline has installed it.
    samples: list[tuple[bytes | None, str, str]] = [(doc, 'text/plain', 'warmup.txt'), (None, 'image/png', 'warmup.png')]
    compat = {'provider': 'openai-compatible'}
    jobs: list[tuple[str, dict[str, Any], list[tuple[bytes | None, str, str]]]] = [
        ('doc-to-text', {}, samples[:1]),
        ('doc-to-text', {'ocr': True}, samples[1:]),  # the image sample pulls the OCR models into the image
        ('redact-pii', {}, samples[:1]),
        ('redact-pii', {'ocr': True}, samples[1:]),
        ('summarize', dict(compat), samples[:1]),
        ('extract-data', dict(compat, fields=['invoice_number', 'total']), samples[:1]),
        ('ask', dict(compat, question='What is the total due?'), samples[:1]),
        ('agent', dict(compat), [(b'Say hello.', 'text/plain', 'question.txt')]),
    ]
    failed: list[str] = []
    for name, params, payloads in jobs:
        pipeline = pipelines.build(name, params, S.env)
        for body, ctype, filename in payloads:
            ok = False
            for attempt in range(3):
                try:
                    token = await warm_token(name, pipeline, refresh=attempt > 0)
                    if body is None:  # render only once the pipeline has started and installed Pillow
                        body = sample_png()
                        if body is None:
                            log.warning('Prebake %s/%s skipped: could not render a sample image.', name, filename)
                            break
                    started = time.time()
                    result = await send_to_task(token, body, ctype, filename)
                    errors = object_errors(result)
                    if errors:
                        raise EngineError('; '.join(errors)[:300])
                    log.info('Prebaked %s with %s in %.1fs -> %s', name, filename, time.time() - started, json.dumps(shape_output(result))[:160])
                    ok = True
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning('Prebake %s/%s attempt %d failed: %s', name, filename, attempt + 1, str(e)[:300])
            if not ok:
                failed.append(f'{name}/{filename}')
        for key in [k for k in S.warm if k.startswith(name + ':')]:
            await stop_task(S.warm.pop(key))
    # Install the dedicated provider nodes' dependencies: starting the pipeline loads the node.
    for provider in ('openai', 'anthropic', 'gemini'):
        try:
            pipeline = pipelines.build('ask', {'provider': provider, 'question': 'warm-up'}, S.env)
            started = time.time()
            token = await start_task(pipeline)
            await stop_task(token)
            log.info('Prebaked %s node in %.1fs', pipelines.PROVIDERS[provider]['node'], time.time() - started)
        except Exception as e:  # noqa: BLE001
            log.warning('Prebake of provider %s failed: %s', provider, str(e)[:300])
            failed.append(f'provider/{provider}')
    if failed:
        log.error('Prebake finished with failures: %s', ', '.join(failed))
    must_work = [f for f in failed if f.split('/')[0] in STATIC and f.endswith('.txt')]
    return 1 if must_work else 0


async def serve(server: uvicorn.Server) -> None:
    """Run a uvicorn server without its signal capture.

    uvicorn saves and restores signal handlers around serve(); the engine's embedded Python has
    native handlers it cannot restore, which raises on shutdown. We install our own handlers.
    """
    inner = getattr(server, '_serve', None)
    await (inner() if inner else server.serve())


async def main() -> int:
    actor_input = apify_env.load_input()
    S.warm = OrderedDict()
    S.warm_lock = asyncio.Lock()
    S.client = httpx.AsyncClient(base_url=ENGINE_URL, timeout=httpx.Timeout(600, connect=10))

    mode = os.environ.get('ROCKETRIDE_ACTOR_MODE') or (
        'selftest' if apify_env.is_at_home() and not apify_env.is_standby() else 'server'
    )
    mock_server = None
    if mode == 'prebake':
        import mock_llm

        mock_server = uvicorn.Server(uvicorn.Config(mock_llm.app, host='127.0.0.1', port=MOCK_LLM_PORT, log_level='warning'))
        asyncio.create_task(serve(mock_server))
        os.environ.update({
            pipelines.COMPAT_BASE_URL_ENV: f'http://127.0.0.1:{MOCK_LLM_PORT}/v1',
            pipelines.COMPAT_MODEL_ENV: 'mock-model',
            'ROCKETRIDE_LLM_KEY': 'sk-mock',
            'ROCKETRIDE_OPENAI_KEY': 'sk-build-placeholder',
            'ROCKETRIDE_ANTHROPIC_KEY': 'sk-ant-build-placeholder',
            'ROCKETRIDE_GEMINI_KEY': 'build-placeholder',
        })
    await start_engine(actor_input)
    engine_watch: asyncio.Task | None = None

    if mode in ('selftest', 'prebake'):
        await wait_engine_ready()
        code = await (prebake() if mode == 'prebake' else self_test())
        if mock_server is not None:
            mock_server.should_exit = True
            await asyncio.sleep(0.5)
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
            for name in STATIC:
                for attempt in range(2):
                    try:
                        await warm_token(name, pipelines.build(name, {}, S.env))
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
    await serve(server)
    if S.engine_proc and S.engine_proc.returncode is None:
        S.engine_proc.terminate()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
