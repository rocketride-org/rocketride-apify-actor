"""Stand-in for the RocketRide engine, used to test the Actor wrapper without the real image."""
import json, os, uuid
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

KEY = os.environ['ROCKETRIDE_APIKEY']
TASKS = {}

def authed(req):
    return req.headers.get('authorization') == f'Bearer {KEY}'

async def version(req):
    return JSONResponse({'version': 'mock-3.6.0'})

async def task(req):
    if not authed(req): return JSONResponse({'status': 'Error', 'error': {'message': 'bad key'}}, 401)
    if req.method == 'POST':
        pipe = await req.json(); assert 'components' in pipe
        tok = str(uuid.uuid4()); TASKS[tok] = pipe
        return JSONResponse({'status': 'OK', 'data': {'token': tok}})
    TASKS.pop(req.query_params.get('token'), None)
    return JSONResponse({'status': 'OK'})

async def webhook(req):
    if not authed(req): return JSONResponse({'status': 'Error', 'error': {'message': 'bad key'}}, 401)
    tok = req.query_params.get('token')
    if tok not in TASKS: return JSONResponse({'status': 'Error', 'error': {'message': 'no task'}})
    body = (await req.body()).decode('utf-8', 'replace')
    return JSONResponse({'status': 'OK', 'data': {'objectsRequested': 1, 'objectsCompleted': 1,
        'objects': {'body': {'text': [f"[{req.headers.get('content-type')}|{os.environ.get('ROCKETRIDE_OPENAI_KEY','-')}] {body}"]}}}})

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
    Route('/webhook', webhook, methods=['POST']), Route('/mcp', mcp, methods=['POST'])])
uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('ROCKETRIDE_ENGINE_PORT', '5565')), log_level='warning')
