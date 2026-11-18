"""Catalog and builders for the Actor's ready-made RocketRide pipelines.

Every pipeline here is an ordinary RocketRide ``.pipe`` definition with a webhook
source, so it can be opened in the RocketRide VS Code canvas or sent to ``/run``.
LLM pipelines are built per request from a few parameters (question, fields,
provider, model); provider keys are referenced as ``${ROCKETRIDE_...}`` variables
and never written into the pipeline.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

# --------------------------------------------------------------------------- LLM providers

PROVIDERS: dict[str, dict[str, str]] = {
    'openai': {'node': 'llm_openai', 'key_env': 'ROCKETRIDE_OPENAI_KEY'},
    'anthropic': {'node': 'llm_anthropic', 'key_env': 'ROCKETRIDE_ANTHROPIC_KEY'},
    'gemini': {'node': 'llm_gemini', 'key_env': 'ROCKETRIDE_GEMINI_KEY'},
    'openai-compatible': {'node': 'llm_openai_api', 'key_env': 'ROCKETRIDE_LLM_KEY'},
}
COMPAT_BASE_URL_ENV = 'ROCKETRIDE_LLM_BASE_URL'
COMPAT_MODEL_ENV = 'ROCKETRIDE_LLM_MODEL'
DEFAULT_MODEL_ENV = 'ROCKETRIDE_DEFAULT_MODEL'


class PipelineError(ValueError):
    """A request that cannot be turned into a pipeline (bad or missing parameters)."""


def configured_providers(env: dict[str, str]) -> list[str]:
    out = []
    for name, p in PROVIDERS.items():
        if name == 'openai-compatible':
            if env.get(COMPAT_BASE_URL_ENV) and env.get(COMPAT_MODEL_ENV):
                out.append(name)
        elif env.get(p['key_env']):
            out.append(name)
    return out


def resolve_provider(requested: str | None, env: dict[str, str]) -> str:
    available = configured_providers(env)
    if requested:
        requested = requested.strip().lower()
        if requested not in PROVIDERS:
            raise PipelineError(f'Unknown provider {requested!r}. Use one of: {", ".join(PROVIDERS)}.')
        if requested not in available:
            raise PipelineError(
                f'Provider {requested!r} has no credentials. Add its key in the Actor input, then restart the server.'
            )
        return requested
    if not available:
        raise PipelineError(
            'This pipeline needs an LLM. Add an OpenAI, Anthropic or Gemini API key (or an OpenAI-compatible '
            'endpoint) in the Actor input, then restart the server.'
        )
    return available[0]


def llm_component(provider: str, model: str | None) -> dict[str, Any]:
    """The LLM node for a provider."""
    p = PROVIDERS[provider]
    node = p['node']
    key = '${' + p['key_env'] + '}'
    if provider == 'openai-compatible':
        cfg = {
            'profile': 'custom',
            'custom': {
                'model': model or '${' + COMPAT_MODEL_ENV + '}',
                'base_url': '${' + COMPAT_BASE_URL_ENV + '}',
                'apikey': key,
                'modelTotalTokens': 32768,
            },
        }
    elif model:
        # An explicit model id runs on the node's "custom" profile.
        cfg = {'profile': 'custom', 'custom': {'model': model, 'apikey': key, 'modelTotalTokens': 128000}}
    else:
        # No profile: the engine applies the node's default model.
        cfg = {'apikey': key}
    cfg['parameters'] = {}
    return {'id': 'llm_1', 'provider': node, 'config': cfg}


# --------------------------------------------------------------------------- building blocks


def _webhook() -> dict[str, Any]:
    return {
        'id': 'webhook_1',
        'provider': 'webhook',
        'config': {'hideForm': True, 'mode': 'Source', 'parameters': {}, 'type': 'webhook'},
    }


def _ingest(ocr: bool) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """webhook -> parse (-> OCR for images). Returns components and the text-lane inputs for the next node."""
    comps = [_webhook(), {'id': 'parse_1', 'provider': 'parse', 'config': {}, 'input': [{'lane': 'tags', 'from': 'webhook_1'}]}]
    text = [{'lane': 'text', 'from': 'parse_1'}]
    if ocr:
        comps.append({'id': 'ocr_1', 'provider': 'ocr', 'config': {}, 'input': [{'lane': 'image', 'from': 'parse_1'}]})
        text.append({'lane': 'text', 'from': 'ocr_1'})
    return comps, text


def _response(lane: str, source: str) -> dict[str, Any]:
    return {
        'id': f'response_{lane}_1',
        'provider': f'response_{lane}',
        'config': {'laneName': lane},
        'input': [{'lane': lane, 'from': source}],
    }


def _finish(name: str, comps: list[dict[str, Any]]) -> dict[str, Any]:
    body = json.dumps(comps, sort_keys=True)
    project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'rocketride-apify/{name}/{hashlib.sha256(body.encode()).hexdigest()}'))
    return {'components': comps, 'source': 'webhook_1', 'project_id': project_id, 'version': 1}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _as_int(value: Any, default: int, low: int, high: int, name: str) -> int:
    if value is None or value == '':
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise PipelineError(f'"{name}" must be a whole number.') from None
    return max(low, min(high, n))


# --------------------------------------------------------------------------- static pipelines


def doc_to_text(params: dict[str, Any], llm: dict[str, Any] | None) -> dict[str, Any]:
    comps, text = _ingest(ocr=True)
    comps.append({'id': 'response_text_1', 'provider': 'response_text', 'config': {'laneName': 'text'}, 'input': text})
    return _finish('doc-to-text', comps)


def redact_pii(params: dict[str, Any], llm: dict[str, Any] | None) -> dict[str, Any]:
    comps, text = _ingest(ocr=True)
    comps.append({'id': 'ner_1', 'provider': 'ner', 'config': {}, 'input': text})
    comps.append({'id': 'anonymize_text_1', 'provider': 'anonymize_text', 'config': {}, 'input': [{'lane': 'text', 'from': 'ner_1'}]})
    comps.append(_response('text', 'anonymize_text_1'))
    return _finish('redact-pii', comps)


# --------------------------------------------------------------------------- LLM pipelines


def summarize(params: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    comps, text = _ingest(_as_bool(params.get('ocr'), False))
    comps.append({
        'id': 'summarization_1',
        'provider': 'summarization',
        'config': {
            'numberOfSummaries': _as_int(params.get('chunks'), 3, 1, 20, 'chunks'),
            'numberOfSummaryWords': _as_int(params.get('summaryWords'), 250, 0, 3000, 'summaryWords'),
            'numberOfKeyPointWords': 40 if _as_bool(params.get('keyPoints'), True) else 0,
            'numberOfEntities': _as_int(params.get('entities'), 15, 0, 100, 'entities'),
            'parameters': {},
        },
        'input': text,
    })
    comps.append({**llm, 'control': [{'classType': 'llm', 'from': 'summarization_1'}]})
    comps.append(_response('text', 'summarization_1'))
    return _finish('summarize', comps)


FIELD_TYPES = ('text', 'decimal', 'int', 'date', 'time', 'datetime', 'timestamp', 'binary', 'json', 'html', 'url', 'email', 'phone', 'ipv4', 'ipv6', 'uuid', 'guid')
TYPE_ALIASES = {'number': 'decimal', 'float': 'decimal', 'integer': 'int', 'string': 'text', 'str': 'text'}


def _fields(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith('['):
            try:
                raw = json.loads(raw)
            except ValueError:
                raise PipelineError('"fields" is not valid JSON.') from None
        else:
            raw = [f for f in (part.strip() for part in raw.split(',')) if f]
    if not isinstance(raw, list) or not raw:
        raise PipelineError(
            '"fields" is required: a list of column names, e.g. ["invoice_number", "total"], '
            'or objects like {"name": "total", "type": "number"}.'
        )
    if len(raw) > 32:
        raise PipelineError('"fields" can have at most 32 entries.')
    out = []
    for f in raw:
        if isinstance(f, str):
            f = {'name': f}
        if not isinstance(f, dict):
            raise PipelineError('Each field must be a string or an object.')
        name = str(f.get('name') or f.get('column') or '').strip()
        if not name:
            raise PipelineError('Each field needs a "name".')
        if not 2 <= len(name) <= 32:
            raise PipelineError(f'Field name {name!r} must be 2 to 32 characters.')
        ftype = str(f.get('type') or 'text').strip().lower()
        ftype = TYPE_ALIASES.get(ftype, ftype)
        if ftype not in FIELD_TYPES:
            raise PipelineError(f'Field type {ftype!r} is not supported. Use one of: {", ".join(FIELD_TYPES)}.')
        out.append({'column': name, 'type': ftype, 'defval': str(f.get('default', f.get('defval', '')) or '')})
    return out


def extract_data(params: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    fields = _fields(params.get('fields'))
    comps, text = _ingest(_as_bool(params.get('ocr'), False))
    comps.append({'id': 'extract_data_1', 'provider': 'extract_data', 'config': {'fields': fields, 'parameters': {}}, 'input': text})
    comps.append({**llm, 'control': [{'classType': 'llm', 'from': 'extract_data_1'}]})
    comps.append(_response('answers', 'extract_data_1'))
    return _finish('extract-data', comps)


def ask(params: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    question = str(params.get('question') or '').strip()
    if not question:
        raise PipelineError('"question" is required: what do you want to know about the document?')
    if len(question) > 4000:
        raise PipelineError('"question" is too long (4,000 characters max).')
    comps, text = _ingest(_as_bool(params.get('ocr'), False))
    comps.append({
        'id': 'prompt_1',
        'provider': 'prompt',
        'config': {
            'instructions': [
                'Answer the question using only the document provided in the context. '
                'If the document does not contain the answer, say so. Be concise.',
                f'Question: {question}',
            ],
            'parameters': {},
        },
        'input': text,
    })
    comps.append({**llm, 'input': [{'lane': 'questions', 'from': 'prompt_1'}]})
    comps.append(_response('answers', 'llm_1'))
    return _finish('ask', comps)


DEFAULT_AGENT_INSTRUCTIONS = (
    'You are a helpful research assistant. Use the HTTP request tool to fetch web pages or APIs when you need '
    'current or specific information. Answer clearly and say which URLs you used.'
)


def agent(params: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    instructions = str(params.get('instructions') or DEFAULT_AGENT_INSTRUCTIONS).strip()[:4000]
    comps = [
        _webhook(),
        {'id': 'parse_1', 'provider': 'parse', 'config': {}, 'input': [{'lane': 'tags', 'from': 'webhook_1'}]},
        {'id': 'question_1', 'provider': 'question', 'config': {}, 'input': [{'lane': 'text', 'from': 'parse_1'}]},
        {
            'id': 'agent_rocketride_1',
            'provider': 'agent_rocketride',
            'config': {'instructions': [instructions], 'max_waves': _as_int(params.get('maxSteps'), 6, 1, 15, 'maxSteps'), 'parameters': {}},
            'input': [{'lane': 'questions', 'from': 'question_1'}],
        },
        {**llm, 'control': [{'classType': 'llm', 'from': 'agent_rocketride_1'}]},
        {'id': 'memory_internal_1', 'provider': 'memory_internal', 'config': {'type': 'memory_internal'}, 'control': [{'classType': 'memory', 'from': 'agent_rocketride_1'}]},
    ]
    if _as_bool(params.get('web'), True):
        comps.append({
            'id': 'tool_http_request_1',
            'provider': 'tool_http_request',
            'config': {'type': 'tool_http_request'},
            'control': [{'classType': 'tool', 'from': 'agent_rocketride_1'}],
        })
    comps.append(_response('answers', 'agent_rocketride_1'))
    return _finish('agent', comps)


# --------------------------------------------------------------------------- catalog

LLM_PARAMS = {
    'provider': 'openai | anthropic | gemini | openai-compatible. Default: the first one you configured.',
    'model': 'Model id for that provider. Default: the provider node\'s default model.',
}
DOC_PARAMS = {'ocr': 'true to also OCR scanned pages and images (slower). Default: false.'}

CATALOG: dict[str, dict[str, Any]] = {
    'doc-to-text': {
        'title': 'Document to text',
        'description': 'Any document (PDF, DOCX, PPTX, XLSX, HTML, images, ...) to clean text, with OCR for embedded images.',
        'needsLlm': False,
        'input': 'document',
        'params': {},
        'build': doc_to_text,
    },
    'redact-pii': {
        'title': 'Redact PII',
        'description': 'Parse a document, detect named entities and return the text with personal data masked.',
        'needsLlm': False,
        'input': 'document',
        'params': {},
        'build': redact_pii,
    },
    'summarize': {
        'title': 'Summarize a document',
        'description': 'Summary, key points and named entities for any document, in one LLM pass.',
        'needsLlm': True,
        'input': 'document',
        'params': {
            'summaryWords': 'Target summary length in words (default 250).',
            'keyPoints': 'Include key points (default true).',
            'entities': 'How many entities to list (default 15, 0 to skip).',
            'chunks': 'How many context-window-sized chunks of a long document to cover (default 3).',
            **DOC_PARAMS,
            **LLM_PARAMS,
        },
        'build': summarize,
    },
    'extract-data': {
        'title': 'Extract structured data',
        'description': 'Pull the fields you name out of a document and get them back as JSON rows.',
        'needsLlm': True,
        'input': 'document',
        'params': {
            'fields': 'Required. Column names, e.g. ["invoice_number", "total"], or objects {"name", "type", "default"}. Types: text, decimal, int, date, datetime, email, url, phone, json and more.',
            **DOC_PARAMS,
            **LLM_PARAMS,
        },
        'build': extract_data,
    },
    'ask': {
        'title': 'Ask a document',
        'description': 'Ask a question and get an answer grounded in the document you send.',
        'needsLlm': True,
        'input': 'document',
        'params': {'question': 'Required. The question to answer from the document.', **DOC_PARAMS, **LLM_PARAMS},
        'build': ask,
    },
    'agent': {
        'title': 'Research agent',
        'description': 'A RocketRide agent that plans, fetches web pages or APIs over HTTP, and answers your question.',
        'needsLlm': True,
        'input': 'question',
        'params': {
            'question': 'Required. What you want the agent to do or find out.',
            'instructions': 'Optional system instructions for the agent.',
            'web': 'Allow the HTTP request tool (default true).',
            'maxSteps': 'Maximum plan/act rounds (default 6).',
            **LLM_PARAMS,
        },
        'build': agent,
    },
}

PARAM_NAMES = sorted({k for c in CATALOG.values() for k in c['params']})


def build(name: str, params: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    entry = CATALOG[name]
    llm = None
    if entry['needsLlm']:
        provider = resolve_provider(params.get('provider'), env)
        model = str(params.get('model') or env.get(DEFAULT_MODEL_ENV) or '').strip() or None
        llm = llm_component(provider, model)
    return entry['build'](params, llm)


def cache_key(name: str, pipeline: dict[str, Any]) -> str:
    return f'{name}:{pipeline["project_id"]}'
