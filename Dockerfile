FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "uv run --no-sync alembic upgrade head && exec uv run --no-sync uvicorn cam.api.main:app --host 0.0.0.0 --port 8000"]
