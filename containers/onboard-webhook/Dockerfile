# DevBrain Onboarding Webhook — minimal Python service.
#
# Stdlib HTTP server + psycopg2-binary; no FastAPI / Flask / uvicorn.
# Image footprint is the python:slim base + psycopg2-binary, ~150 MB.
#
# The Mac Studio's docker-compose.yml builds this image as service
# `devbrain-onboard`, mounts the factory/ source as /app/factory,
# binds 127.0.0.1:8000 on the host. A reverse SSH tunnel from Mac
# Studio to the LHT VPS exposes that port behind Traefik at
# https://devbrain.lighthouse-therapy.com/onboard/*.

FROM python:3.12-slim

# psycopg2-binary needs libpq runtime; nothing else.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pin matching the host devbrain venv so the schema-touching code in
# state_machine.py / invitations.py behaves identically.
RUN pip install --no-cache-dir psycopg2-binary==2.9.10

# `factory/` is mounted at runtime via docker-compose so we don't have
# to rebuild the image on every code change. We run the script from
# inside that directory so its sibling modules (invitations.py,
# state_machine.py, etc.) resolve as flat imports — matching how the
# rest of DevBrain is invoked from the venv on the host.
WORKDIR /app/factory

# Make /app/factory's siblings importable.
ENV PYTHONPATH=/app/factory

EXPOSE 8000

# Run as non-root inside the container. The image's `nobody` user
# (UID 65534) is fine — we don't need to write to the filesystem.
USER nobody

CMD ["python", "/app/factory/onboard_webhook.py"]
