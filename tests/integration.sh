#!/usr/bin/env bash
# Builds the Actor image with the real RocketRide engine and exercises it the way Apify will.
set -euo pipefail
IMAGE=${IMAGE:-rocketride-apify-actor:ci}
PORT=4321
docker build -t "$IMAGE" .
docker rm -f rr-actor >/dev/null 2>&1 || true
docker run -d --name rr-actor -p $PORT:$PORT \
  -e ACTOR_WEB_SERVER_PORT=$PORT -e ROCKETRIDE_ACTOR_MODE=server -e ACTOR_INPUT_JSON='{}' "$IMAGE"
trap 'echo "---- container logs ----"; docker logs rr-actor 2>&1 | tail -150' EXIT
B=http://localhost:$PORT
for i in $(seq 1 60); do curl -fsS -H 'x-apify-container-server-readiness-probe: 1' $B/ >/dev/null 2>&1 && break; sleep 2; done
echo "readiness: $(curl -fsS -H 'x-apify-container-server-readiness-probe: 1' $B/)"
for i in $(seq 1 150); do curl -fsS $B/health >/dev/null 2>&1 && break; sleep 2; done
curl -fsS $B/health; echo
echo "== doc-to-text";  curl -fsS -X POST $B/pipelines/doc-to-text -H 'Content-Type: application/json' -d '{"text":"Hello from the Apify CI."}' | tee /tmp/d2t.json; echo
python3 -c "import json;d=json.load(open('/tmp/d2t.json'));assert 'Apify CI' in (d.get('text') or json.dumps(d['result'])), d"
echo "== redact-pii";   curl -fsS -X POST $B/pipelines/redact-pii -H 'Content-Type: application/json' -d '{"text":"Contact John Smith at john.smith@example.com in Seattle."}' | tee /tmp/pii.json; echo
echo "== MCP initialize"
curl -fsS -X POST $B/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"ci","version":"0"}}}' | tee /tmp/init.txt; echo
grep -q '"result"' /tmp/init.txt
echo "== MCP tools/list"
curl -fsS -X POST $B/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' > /tmp/tools.txt
python3 - <<'PY'
import json,re
raw=open('/tmp/tools.txt').read()
m=re.search(r'\{.*\}', raw, re.S); tools=[t['name'] for t in json.loads(m.group(0))['result']['tools']]
print(len(tools), 'MCP tools:', ', '.join(tools)); assert 'validate_pipeline' in tools
PY
python3 - <<'PY'
import json
for f in ('/tmp/d2t.json','/tmp/pii.json'):
    d=json.load(open(f)); print(d['pipeline'], 'first-call ms:', d['durationMs'])
pii=json.load(open('/tmp/pii.json'))['text']
assert 'John Smith' not in pii and 'example.com' not in pii, pii
PY
echo "INTEGRATION PASSED"

echo "== IMAGE SIZE REPORT"
BASE=$(grep -m1 '^ARG ENGINE_IMAGE=' Dockerfile | cut -d= -f2)
docker image ls --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep -i rocketride || true
echo "-- layers of $IMAGE"; docker history --format '{{.Size}}\t{{.CreatedBy}}' "$IMAGE" | cut -c1-150 | head -25
docker run --rm --entrypoint sh "$IMAGE" -c '
  echo "-- top dirs"; du -xh --max-depth=2 /opt /home /tmp /root 2>/dev/null | sort -h | tail -25
  echo "-- biggest python packages"; SP=$(find /opt/rocketride -type d -name site-packages | head -1); echo "$SP"; du -sh "$SP"/* 2>/dev/null | sort -h | tail -40
  echo "-- caches"; du -sh ~/.cache/* /opt/data/* /opt/rocketride/.cache/* 2>/dev/null | sort -h | tail -20
  echo "-- model files >50MB"; find / -xdev -type f -size +50M 2>/dev/null | xargs -r du -h | sort -h | tail -40
' || true
