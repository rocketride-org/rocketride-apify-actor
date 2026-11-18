# RocketRide - Meta harness: Server Actor for Apify.
# Pinned to engine 3.6.0.38. scripts/prepare_image.py patches engine internals, so bump this
# deliberately and let CI (tests/integration.sh) prove the new engine before shipping.
ARG ENGINE_IMAGE=ghcr.io/rocketride-org/rocketride-engine@sha256:ccb906861322f967a5c896940f2ef6bdc80898fee9ac866c473d921e1eb9b6cc
FROM ${ENGINE_IMAGE}

USER root
COPY --chown=rocketride:rocketride src/ /opt/actor/src/
COPY --chown=rocketride:rocketride scripts/ /opt/actor/scripts/
USER rocketride
WORKDIR /opt/rocketride

# The wrapper runs on the engine's embedded Python; fail the build early if a dependency is missing.
RUN ./engine -c "import starlette, uvicorn, httpx, cryptography; print('actor wrapper deps OK')"

ENV ROCKETRIDE_HOME=/opt/rocketride \
    PYTHONUNBUFFERED=1

# 1. Switch the engine to CPU-only PyTorch (Apify has no GPUs; saves ~6 GB of image).
# 2. Run every ready-made pipeline once so node dependencies and OCR/NER models are baked in;
#    otherwise a user's first request waits for downloads. LLM pipelines use a built-in mock LLM.
# 3. Drop download caches.
RUN ./engine /opt/actor/scripts/prepare_image.py \
 && ROCKETRIDE_ACTOR_MODE=prebake ACTOR_INPUT_JSON='{}' ./engine /opt/actor/src/main.py \
 && ./engine -c "import torch, langchain_text_splitters; assert not torch.cuda.is_available(); assert '+cpu' in torch.__version__, torch.__version__; print('torch', torch.__version__)" \
 && rm -rf /home/rocketride/.cache/uv /home/rocketride/.cache/pip /tmp/*

ENTRYPOINT ["/usr/bin/tini", "--", "./engine", "/opt/actor/src/main.py"]
