# RocketRide Server Actor for Apify.
# Pin ENGINE_IMAGE to a released tag (e.g. :3.6.1) before publishing to the Store.
ARG ENGINE_IMAGE=ghcr.io/rocketride-org/rocketride-engine:latest
FROM ${ENGINE_IMAGE}

USER root
COPY --chown=rocketride:rocketride src/ /opt/actor/src/
COPY --chown=rocketride:rocketride pipelines/ /opt/actor/pipelines/
USER rocketride
WORKDIR /opt/rocketride

# The wrapper runs on the engine's embedded Python; fail the build early if a dependency is missing.
RUN ./engine -c "import starlette, uvicorn, httpx, cryptography; print('actor wrapper deps OK')"

ENV ROCKETRIDE_HOME=/opt/rocketride \
    ROCKETRIDE_ACTOR_PIPELINES=/opt/actor/pipelines \
    PYTHONUNBUFFERED=1

# Run the featured pipelines once at build time so node dependencies and OCR/NER
# models are baked into the image; otherwise a user's first request waits ~80s.
RUN ROCKETRIDE_ACTOR_MODE=prebake ACTOR_INPUT_JSON='{}' ./engine /opt/actor/src/main.py

ENTRYPOINT ["/usr/bin/tini", "--", "./engine", "/opt/actor/src/main.py"]
