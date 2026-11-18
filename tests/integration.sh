#!/usr/bin/env bash
# Builds the Actor image with the real RocketRide engine and exercises it the way Apify will.
# LLM pipelines talk to the mock LLM in src/mock_llm.py, so no provider key is needed.
set -euo pipefail
IMAGE=${IMAGE:-rocketride-apify-actor:ci}
python3 -m pip install -q starlette uvicorn
python3 src/mock_llm.py 9911 &
MOCK_PID=$!
docker build -t "$IMAGE" .
docker rm -f rr-actor >/dev/null 2>&1 || true
docker run -d --name rr-actor --network host \
  -e ACTOR_WEB_SERVER_PORT=4321 -e ROCKETRIDE_ACTOR_MODE=server \
  -e ACTOR_INPUT_JSON='{"llmBaseUrl":"http://127.0.0.1:9911/v1","llmModel":"mock-model","llmApiKey":"sk-mock"}' "$IMAGE"
trap 'kill $MOCK_PID 2>/dev/null || true; echo "---- container logs ----"; docker logs rr-actor 2>&1 | grep -v "GET /version" | tail -120 | cut -c1-400' EXIT
RC=0; python3 tests/integration_check.py || RC=$?

echo "== MEMORY with both document pipelines and three LLM pipelines running"
docker stats --no-stream --format '{{.Name}}  mem={{.MemUsage}}  cpu={{.CPUPerc}}' rr-actor || true

echo "== IMAGE SIZE REPORT"
docker image ls --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep -i rocketride || true
echo "-- layers of $IMAGE"; docker history --format '{{.Size}}\t{{.CreatedBy}}' "$IMAGE" | cut -c1-150 | head -8
docker run --rm --entrypoint sh "$IMAGE" -c '
  echo "-- top dirs"; du -xh --max-depth=2 /opt /home 2>/dev/null | sort -h | tail -12
  echo "-- biggest python packages"; SP=$(find /opt/rocketride -type d -name site-packages | head -1); du -sh "$SP"/* 2>/dev/null | sort -h | tail -12
  echo "-- models"; du -sh ~/.cache/* ~/.EasyOCR 2>/dev/null | sort -h | tail -10
' || true
exit $RC
