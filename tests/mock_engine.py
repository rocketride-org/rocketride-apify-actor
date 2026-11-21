"""Stand-in for the RocketRide engine, used to test the Actor wrapper without the real image."""
import json, os, uuid
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

KEY = os.environ['ROCKETRIDE_APIKEY']
TASKS = {}
DEAD = set()  # tasks the real engine would report as gone (crashed or killed)

def died(message, line):
    obj = {'status': 'Error', 'error': {'message': message, 'file': 'data.py', 'lineno': line}}
    return JSONResponse({'status': 'OK', 'data': {'objectsRequested': 1, 'objectsCompleted': 0, 'objects': {'body': obj}}})

def authed(req):
    return req.headers.get('authorization') == f'Bearer {KEY}'

async def version(req):
    return JSONResponse({'version': 'mock-3.6.0'})

async def task(req):
    if not authed(req): return JSONResponse({'status': 'Error', 'error': {'message': 'bad key'}}, 401)
    if req.method == 'POST':
        pipe = await req.json(); assert 'components' in pipe
        if any(t.get('project_id') == pipe.get('project_id') for t in TASKS.values()):  # as the real engine does
            return JSONResponse({'status': 'Error', 'error': {'message': 'Pipeline is already running.'}})
        tok = str(uuid.uuid4()); TASKS[tok] = pipe
        return JSONResponse({'status': 'OK', 'data': {'token': tok}})
    TASKS.pop(req.query_params.get('token'), None)
    return JSONResponse({'status': 'OK'})

async def webhook(req):
    if not authed(req): return JSONResponse({'status': 'Error', 'error': {'message': 'bad key'}}, 401)
    tok = req.query_params.get('token')
    if tok not in TASKS: return JSONResponse({'status': 'Error', 'error': {'message': 'no task'}})
    if tok in DEAD: return died('Stopped', 247)
    body = (await req.body()).decode('utf-8', 'replace')
    if 'TRIGGER_TASK_DIES' in body:  # e.g. the OCR worker is killed for running out of memory
        DEAD.add(tok)
        return died('Connection closed', 342)
    providers = [c['provider'] for c in TASKS[tok]['components']]
    obj = {'name': 'x', 'status': 'OK'}
    if 'TRIGGER_LLM_ERROR' in body:
        obj['answers'] = ['**LLM error** \u2014 OpenAIAuthenticationError: Error code: 401']
    elif 'TRIGGER_NODE_ERROR' in body:
        obj['error'] = {'message': 'node exploded', 'code': 19}
    elif 'extract_data' in providers:
        cols = [f['column'] for c in TASKS[tok]['components'] if c['provider'] == 'extract_data' for f in c['config']['fields']]
        obj['answers'] = [[{c: 'v' for c in cols}]]
    elif 'response_answers' in providers:
        llm = [p for p in providers if p.startswith('llm_')][0]
        obj['answers'] = [f'ANSWER via {llm}: {body}']
    else:
        obj['text'] = [f"[{req.headers.get('content-type')}|{os.environ.get('ROCKETRIDE_OPENAI_KEY','-')}|{','.join(providers)}] {body}\n\n"]
    return JSONResponse({'status': 'OK', 'data': {'objectsRequested': 1, 'objectsCompleted': 1, 'objects': {'body': obj}}})

async def tasks(req):
    return JSONResponse({'count': len(TASKS), 'pipelines': [[c['provider'] for c in t['components']] for t in TASKS.values()]})

async def kill(req):
    DEAD.update(TASKS)  # every running task stops behind the wrapper's back
    return JSONResponse({'killed': len(TASKS)})

async def mcp(req):
    if not authed(req): return JSONResponse({'error': 'unauthorized'}, 401)
    msg = await req.json()
    res = {'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {'serverInfo': {'name': 'rocketride'}, 'method': msg.get('method')}}
    if 'text/event-stream' in req.headers.get('accept', '') and msg.get('method') == 'tools/list':
        async def gen():
            yield f'event: message\ndata: {json.dumps(res)}\n\n'
        return StreamingResponse(gen(), media_type='text/event-stream')
    return JSONResponse(res)

app = Starlette(routes=[Route('/version', version), Route('/task', task, methods=['POST', 'DELETE']),
    Route('/webhook', webhook, methods=['POST']), Route('/_tasks', tasks), Route('/_kill', kill, methods=['POST']),
    Route('/mcp', mcp, methods=['POST'])])
uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('ROCKETRIDE_ENGINE_PORT', '5565')), log_level='warning')
