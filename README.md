# CAM — Central Agent Management

Internal IndiaMART hub. Agents connect to one MCP tool, `cam`, to list, describe and invoke
tools on upstream MCP servers and REST APIs, and to load text skills.

See `docs/architecture.md` for the full architecture and build plan, and `AGENTS.md` for
working rules.

## Development

```bash
uv sync                  # install dependencies
cp .env.example .env     # fill in required secrets for prod; dev works without
docker compose up --build  # Postgres 16 + CAM app
uv run pytest            # tests (needs the compose Postgres running)
uv run ruff check        # lint
uv run pyright           # type check
```

Health endpoint: `GET http://localhost:8000/api/health`.

Web frontend (React) lives in `web/` — run it separately:

```bash
cd web
npm install
npm run dev
```
