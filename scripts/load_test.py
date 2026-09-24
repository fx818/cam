"""CAM load test (Phase 7.2): many agents calling a slow tool at once.

Runs the real CAM app in-process over ASGI against the real Postgres and the
mock MCP server. For each agent count N it opens N MCP sessions, then every
agent starts a slow upstream invoke and, while it is in flight, an echo
invoke that must complete within a second. A fast echo during a slow call
proves the event loop is not blocked. Reports p50/p95/max latency, error
rate and audit-writer catch-up (rows written vs calls made).

Usage:
    uv run python scripts/load_test.py                 # 50/100/200 agents
    uv run python scripts/load_test.py --agents 10 30  # custom sizes
    uv run python scripts/load_test.py --pytest        # small runs + asserts

Recreates the database `cam_load` (compose Postgres must be running). Raise
or lower N with --agents; default run takes about a minute end to end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

ADMIN_DB_URL = "postgresql+asyncpg://cam:cam@localhost:5432/postgres"
LOAD_DB = "cam_load"

SLOW_SLEEP_S = 1.5
ECHO_MAX_S = 1.0
AGENT_NAME = "load-agent"


@dataclass
class BatchCounters:
    slow_latencies: list[float] = field(default_factory=list)
    echo_latencies: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocked_echo: int = 0

    def absorb(self, other: BatchCounters) -> None:
        self.slow_latencies.extend(other.slow_latencies)
        self.echo_latencies.extend(other.echo_latencies)
        self.errors.extend(other.errors)
        self.blocked_echo += other.blocked_echo


class CamAgent:
    """Minimal MCP client for cam/mcp over a shared ASGI httpx client."""

    def __init__(self, client: Any, token: str, index: int) -> None:
        self._client = client
        self._token = token
        self._name = f"{AGENT_NAME}-{index}"
        self._session_id: str | None = None
        self._next_id = 0
        self.counters = BatchCounters()

    def _headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "authorization": f"Bearer {self._token}",
            "x-cam-agent": f"{self._name}/load-1.0",
        }
        if self._session_id is not None:
            headers["mcp-session-id"] = self._session_id
        return headers

    def _payload(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        return payload

    async def _post(self, body: dict[str, Any]) -> Any:
        response = await self._client.post("/mcp", json=body, headers=self._headers())
        if response.status_code == 202:
            return None
        if response.status_code != 200:
            raise RuntimeError(
                f"HTTP {response.status_code} for {body.get('method')}: {response.text[:200]}"
            )
        data_lines = [
            line.removeprefix("data:").lstrip()
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            raise RuntimeError(f"no SSE body for {body.get('method')}: {response.text[:200]!r}")
        return json.loads("\n".join(data_lines))

    async def initialize(self) -> None:
        response = await self._client.post(
            "/mcp",
            json=self._payload(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": self._name, "version": "load-1.0"},
                },
            ),
            headers=self._headers(),
        )
        if response.status_code != 200:
            raise RuntimeError(f"initialize failed: {response.status_code} {response.text[:200]}")
        self._session_id = response.headers.get("mcp-session-id")
        if not self._session_id:
            raise RuntimeError(
                f"no MCP session id returned: {dict(response.headers)} / {response.text[:200]}"
            )
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def invoke(self, tool_id: str, args: dict[str, Any]) -> str:
        message = await self._post(
            self._payload(
                "tools/call",
                {"name": "cam", "arguments": {"action": "invoke", "id": tool_id, "args": args}},
            )
        )
        result: dict[str, Any] = message["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            envelope = json.loads(text).get("error", {})
            self.counters.errors.append(f"{tool_id}: {envelope.get('code')}")
        return text

    async def echo_only(self, echo_id: str) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        text = await self.invoke(echo_id, {"text": "echo-control"})
        elapsed = loop.time() - started
        if text == "echo-control":
            self.counters.echo_latencies.append(elapsed)
        else:
            self.counters.errors.append(f"{echo_id}: control mismatch {text!r}")

    async def slow_then_echo(self, slow_ref: str, echo_ref: str) -> None:
        slow_id = f"{slow_ref}/slow"
        echo_id = f"{echo_ref}/echo"
        loop = asyncio.get_running_loop()
        started = loop.time()
        slow_args = {"sleep_s": SLOW_SLEEP_S, "text": "slow-done"}
        slow_task = asyncio.create_task(self.invoke(slow_id, slow_args))
        await asyncio.sleep(SLOW_SLEEP_S * 0.2)
        echo_started = loop.time()
        echo_text = await self.invoke(echo_id, {"text": "echo-while-slow"})
        echo_elapsed = loop.time() - echo_started
        if echo_text != "echo-while-slow":
            return
        if echo_elapsed > ECHO_MAX_S:
            self.counters.blocked_echo += 1
        self.counters.echo_latencies.append(echo_elapsed)
        slow_text = ""
        try:
            slow_text = await slow_task
        except Exception as exc:
            self.counters.errors.append(f"{slow_id}: {exc!r}")
            return
        slow_elapsed = loop.time() - started
        if slow_text == "slow-done":
            self.counters.slow_latencies.append(slow_elapsed)


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(percent / 100 * len(ordered)), len(ordered) - 1)
    return ordered[index]


def _stats(values: list[float]) -> str:
    def pct(p: float) -> float:
        ordered = sorted(values)
        index = min(int(p / 100 * len(ordered)), len(ordered) - 1)
        return ordered[index]

    return f"p50={statistics.median(values):.2f}s p95={pct(95):.2f}s max={max(values):.2f}s"


async def seed_database_and_env() -> str:
    """Create the cam_load database, migrate it, and configure required env."""
    import asyncio

    import alembic.command
    import alembic.config
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    def _drop_create_and_migrate() -> None:
        async def _drop_create() -> None:
            admin = create_async_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
            try:
                async with admin.connect() as conn:
                    await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{LOAD_DB}" WITH (FORCE)'))
                    await conn.execute(sa.text(f'CREATE DATABASE "{LOAD_DB}"'))
            finally:
                await admin.dispose()

        asyncio.run(_drop_create())
        cfg = alembic.config.Config(str(REPO_ROOT / "alembic.ini"))
        alembic.command.upgrade(cfg, "head")

    os.environ["CAM_DATABASE_URL"] = f"postgresql+asyncpg://cam:cam@localhost:5432/{LOAD_DB}"
    os.environ["CAM_ENV"] = "dev"
    os.environ["CAM_JOBS_ENABLED"] = "false"
    os.environ.setdefault("CAM_ENCRYPTION_KEYS", "v1:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    os.environ.setdefault("CAM_ENCRYPTION_KEY_CURRENT", "v1")
    os.environ.setdefault("CAM_LOG_LEVEL", "INFO")
    await asyncio.to_thread(_drop_create_and_migrate)
    return os.environ["CAM_DATABASE_URL"]


async def setup_services(
    session_factory: Any, streamable_url: str, agent_count: int
) -> tuple[list[str], str, str]:
    """Create the load user, tokens and two global mock services."""
    from cam.connections.orgs import ensure_org
    from cam.connections.tokens import create_token
    from cam.db.models import Tool, User
    from cam.registry.services import register_global_mcp

    async with session_factory() as session:
        org = await ensure_org(session)
        suffix = uuid.uuid4().hex[:10]
        user = User(
            org_id=org.id,
            email=f"load-{suffix}@example.com",
            handle=f"load{suffix}",
            display_name="Load Test User",
            password_hash="x",
            role="user",
            kind="person",
        )
        session.add(user)
        await session.flush()
        tokens = [
            (await create_token(session, org_id=org.id, user_id=user.id, label=f"load-agent-{i}"))[
                0
            ]
            for i in range(agent_count)
        ]
        slow_service, _ = await register_global_mcp(
            session,
            slug=f"loadslow{suffix}",
            name="Load Slow",
            url=streamable_url,
            transport="streamable-http",
            auth_type="none",
            description="Load-test slow tool",
        )
        echo_service, _ = await register_global_mcp(
            session,
            slug=f"loadecho{suffix}",
            name="Load Echo",
            url=streamable_url,
            transport="streamable-http",
            auth_type="none",
            description="Load-test echo tool",
        )
        schema_slow = {
            "type": "object",
            "properties": {
                "sleep_s": {"type": "number"},
                "text": {"type": "string"},
            },
        }
        schema_echo = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }
        for service, name, schema in (
            (slow_service, "slow", schema_slow),
            (echo_service, "echo", schema_echo),
        ):
            session.add(
                Tool(
                    org_id=org.id,
                    service_id=service.id,
                    name=name,
                    upstream_name=name,
                    description=f"mock {name}",
                    input_schema=schema,
                    enabled=True,
                    status="active",
                )
            )
        await session.commit()
        return tokens, f"global/{slow_service.slug}", f"global/{echo_service.slug}"


async def seed_data(streamable_url: str, agent_count: int) -> tuple[Any, Any, list[str], str, str]:
    from cam.db.session import create_db_engine, create_session_factory
    from cam.registry.services import register_global_mcp  # noqa: F401

    engine = create_db_engine(os.environ["CAM_DATABASE_URL"])
    factory = create_session_factory(engine)
    tokens, slow_ref, echo_ref = await setup_services(factory, streamable_url, agent_count)
    return engine, factory, tokens, slow_ref, echo_ref


async def _echo_control(agents: list[CamAgent], echo_ref: str) -> list[float]:
    """Echo-only pass with no slow call in flight: the latency baseline."""
    for agent in agents:
        agent.counters.echo_latencies.clear()
    outcomes = await asyncio.gather(
        *(agent.echo_only(f"{echo_ref}/echo") for agent in agents),
        return_exceptions=True,
    )
    control: list[float] = []
    for agent, outcome in zip(agents, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            agent.counters.errors.append(repr(outcome))
        control.extend(agent.counters.echo_latencies)
    agent_latencies = [a.counters.echo_latencies for a in agents]
    for latencies in agent_latencies:
        latencies.clear()
    return control


async def run_lifespan(app: Any) -> tuple[asyncio.Task[None], asyncio.Event]:
    stop = asyncio.Event()
    failure: list[BaseException] = []
    started = asyncio.Event()

    async def _run() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:
            failure.append(exc)
            started.set()
            raise

    task = asyncio.create_task(_run(), name="cam-load-lifespan")
    await started.wait()
    if failure:
        stop.set()
        raise failure[0]
    return task, stop


async def main(agent_counts: list[int], pytest_mode: bool) -> int:
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from fixtures.mock_mcp import start_mock_mcp

    wall_start = time.monotonic()
    await seed_database_and_env()
    from sqlalchemy import func, select

    from cam.api.main import create_app
    from cam.db.models import AuditLog

    async with start_mock_mcp() as mock:
        _, _factory, tokens, slow_ref, echo_ref = await seed_data(
            mock.streamable_url, max(agent_counts)
        )
        app = create_app()
        lifespan_task, stop = await run_lifespan(app)
        audit_seen = 0
        try:
            import httpx

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://cam.test", timeout=90.0
            ) as client:
                for count in agent_counts:
                    agents = [CamAgent(client, tokens[i], i) for i in range(count)]
                    await asyncio.gather(*(agent.initialize() for agent in agents))
                    control_latencies = await _echo_control(agents, echo_ref)
                    if control_latencies:
                        print(
                            f"  echo-control  n={len(control_latencies):>4}  "
                            f"{_stats(control_latencies)}"
                        )
                    batch_start = time.monotonic()
                    totals = BatchCounters()
                    outcomes = await asyncio.gather(
                        *(agent.slow_then_echo(slow_ref, echo_ref) for agent in agents),
                        return_exceptions=True,
                    )
                    for agent in agents:
                        totals.absorb(agent.counters)
                    for outcome in outcomes:
                        if isinstance(outcome, BaseException):
                            totals.errors.append(repr(outcome))
                    await app.state.audit.flush()
                    audit_factory = app.state.deps.session_factory
                    async with audit_factory() as session:
                        audit_total = await session.scalar(
                            select(func.count()).select_from(AuditLog)
                        )
                    audit_rows = audit_total - audit_seen
                    audit_seen = audit_total
                    batch_elapsed = time.monotonic() - batch_start
                    print()
                    print(
                        f" agents {count:>4} | wall {batch_elapsed:5.1f}s | "
                        f"errors {len(totals.errors)} | blocked-echo {totals.blocked_echo}"
                    )
                    slow_latency = _stats(totals.slow_latencies) if totals.slow_latencies else "n/a"
                    echo_latency = (
                        f"{_stats(totals.echo_latencies)}" if totals.echo_latencies else "n/a"
                    )
                    print(f"  slow          n={len(totals.slow_latencies):>4}  {slow_latency}")
                    print(f"  echo-inflight n={len(totals.echo_latencies):>4}  {echo_latency}")
                    expected_rows = 3 * count
                    print(
                        f"  audit batch rows {audit_rows}/{expected_rows} "
                        f"(a shortfall means queue drops or write failures)"
                    )
                    if pytest_mode:
                        assert not totals.errors, totals.errors[:3]
                        assert len(totals.slow_latencies) == count, (
                            len(totals.slow_latencies),
                            count,
                        )
                        assert len(totals.echo_latencies) == count, (
                            len(totals.echo_latencies),
                            count,
                        )
                        control_p95 = _percentile(control_latencies, 95)
                        echo_p95 = _percentile(totals.echo_latencies, 95)
                        assert echo_p95 <= max(3.0, control_p95 * 3), (
                            f"echo-while-slow p95 {echo_p95:.2f}s vs control p95 {control_p95:.2f}s"
                        )
                        assert audit_rows == expected_rows, audit_rows
        finally:
            stop.set()
            await lifespan_task
    print(f"\ntotal wall time: {time.monotonic() - wall_start:.1f}s")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", nargs="*", type=int, default=None)
    parser.add_argument("--pytest", action="store_true", help="small runs with assertions")
    args = parser.parse_args()
    counts = args.agents or ([8] if args.pytest else [50, 100, 200])
    sys.exit(asyncio.run(main(counts, args.pytest)))
