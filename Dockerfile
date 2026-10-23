# Base images pinned by digest for reproducible builds (R37). The trailing
# comment records the human-readable tag the digest came from.
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS web-build

WORKDIR /web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:7cf77f594be8042dab6daa9fe326f90962252268b4f120a7f5dccce4d947e6c1

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

COPY . .
COPY --from=web-build /web/dist ./web/dist

# Run the app as an unprivileged user (R29). Nothing after install needs root;
# the writable home dir keeps the uv cache usable at runtime.
RUN useradd --create-home --home-dir /home/cam --uid 10001 --user-group cam \
    && chown -R cam:cam /app
USER cam
ENV HOME=/home/cam

ENV CAM_UVICORN_WORKERS=1

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=45s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"]

# Schema migrations are NOT run here (R30): running `alembic upgrade head` on
# every container start races across replicas and blocks startup. Migrations run
# once, ahead of the rollout, via deploy/gke/migration-job.yaml (K8s) or the
# one-shot `migrate` service in docker-compose.yml.
CMD ["sh", "-c", "exec uv run --no-sync uvicorn cam.api.main:app --host 0.0.0.0 --port 8000 --workers \"${CAM_UVICORN_WORKERS:-1}\""]
