#!/usr/bin/env bash
set -uo pipefail
pip install -q starlette uvicorn httpx
python3 src/mock_llm.py 9911 & sleep 1
docker run -d --name eng --network host \
  -e ROCKETRIDE_APIKEY=rr_local -e ROCKETRIDE_URI=ws://127.0.0.1:5565 \
  -e ROCKETRIDE_LLM_BASE_URL=http://127.0.0.1:9911/v1 -e ROCKETRIDE_LLM_MODEL=mock-model -e ROCKETRIDE_LLM_KEY=sk-mock \
  -e ROCKETRIDE_OPENAI_KEY=sk-dummy -e ROCKETRIDE_ANTHROPIC_KEY=sk-ant-dummy -e ROCKETRIDE_GEMINI_KEY=dummy \
  ghcr.io/rocketride-org/rocketride-engine:latest --host=127.0.0.1
for i in $(seq 1 90); do curl -fsS http://127.0.0.1:5565/version >/dev/null 2>&1 && break; sleep 2; done
curl -sS http://127.0.0.1:5565/version; echo
echo "##### engine cache / constraints"
docker exec eng sh -c 'ls -la /opt/rocketride/cache; echo; head -12 /opt/rocketride/cache/constraints.txt; echo ...; grep -n "torch\|cu128\|index-url\|^triton\|^nvidia\|^onnxruntime" /opt/rocketride/cache/*.txt | cut -c1-170 | head -60; echo; cat /opt/rocketride/ai/common/torch/requirements.txt; find /opt/rocketride -maxdepth 2 -name "*.hash" -o -maxdepth 2 -name "*verdict*" | head'
docker exec eng ./engine -c "import depends,inspect; print(depends.engine_cache_dir()); print(inspect.getsource(depends._compute_hash)); print(inspect.getsource(depends._save_hash)); print(inspect.getsource(depends._combine_requirements))" 2>&1 | head -80
python3 tests/probe.py
echo "##### engine log tail"; docker logs eng 2>&1 | grep -v "GET /version" | tail -150 | cut -c1-400
