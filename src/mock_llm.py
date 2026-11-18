"""A tiny OpenAI-compatible chat server that returns canned answers.

Used at image build time (so LLM-node dependencies are installed and exercised
without a real key) and by the tests. Never started when the Actor serves users.
"""

from __future__ import annotations

import json
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

REQUESTS: list[dict] = []


def canned_answer(blob: str) -> str:
    if 'document summarizer' in blob:
        return json.dumps({
            'summary': 'MOCK SUMMARY of the document.',
            'keyPoints': ['Mock key point one', 'Mock key point two'],
            'entities': ['RocketRide', 'Apify'],
        })
    if 'extracting data from text' in blob:
        return json.dumps([{'invoice_number': 'INV-001', 'total': '42.00'}])
    if 'tool_calls' in blob and 'done' in blob and 'thought' in blob:
        return json.dumps({'thought': 'mock', 'scratch': '', 'remove': [], 'done': True, 'answer': 'MOCK AGENT ANSWER'})
    return 'MOCK ANSWER'


async def chat_completions(request: Request):
    body = await request.json()
    blob = json.dumps(body.get('messages', body))
    REQUESTS.append({'path': request.url.path, 'model': body.get('model'), 'stream': bool(body.get('stream')), 'chars': len(blob), 'head': blob[:240]})
    content = canned_answer(blob)
    base = {'id': 'chatcmpl-mock', 'created': int(time.time()), 'model': body.get('model') or 'mock-model'}
    if body.get('stream'):
        async def gen():
            first = {**base, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content}, 'finish_reason': None}]}
            last = {**base, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}
            yield f'data: {json.dumps(first)}\n\n'
            yield f'data: {json.dumps(last)}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(gen(), media_type='text/event-stream')
    return JSONResponse({
        **base,
        'object': 'chat.completion',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
    })


async def models(request: Request):
    return JSONResponse({'object': 'list', 'data': [{'id': 'mock-model', 'object': 'model', 'owned_by': 'mock'}]})


async def log(request: Request):
    return JSONResponse(REQUESTS)


async def fallback(request: Request):
    REQUESTS.append({'path': request.url.path, 'method': request.method, 'unhandled': True})
    return JSONResponse({'error': {'message': f'mock LLM: no route for {request.url.path}'}}, status_code=404)


app = Starlette(routes=[
    Route('/v1/chat/completions', chat_completions, methods=['POST']),
    Route('/chat/completions', chat_completions, methods=['POST']),
    Route('/v1/models', models, methods=['GET']),
    Route('/_log', log, methods=['GET']),
    Route('/{rest:path}', fallback, methods=['GET', 'POST']),
])

if __name__ == '__main__':
    import sys

    import uvicorn

    uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]) if len(sys.argv) > 1 else 9911, log_level='warning')
