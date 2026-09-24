from api.helpers import UserEnv, login
from cam.db.models import Connection, Service


async def test_services_api_surfaces_failing_health(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    service, connection = personal_service
    factory = env_user.app.state.session_factory
    from cam.jobs.health import record_invoke_health

    await record_invoke_health(
        factory,
        service_id=service.id,
        connection_id=connection.id,
        ok=False,
        error_code="UPSTREAM_UNAVAILABLE",
    )
    await login(env_user.client, env_user.user.email, user_password)
    response = await env_user.client.get("/api/services")
    entries = {entry["id"]: entry for entry in response.json()["services"]}
    entry = next(
        entry
        for entry in entries.values()
        if entry["id"] == f"{env_user.user.handle}/{service.slug}"
    )
    assert entry["health"] == "failing"


async def test_services_api_surfaces_needs_reconnect(
    env_user: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    service, connection = personal_service
    factory = env_user.app.state.session_factory
    from cam.jobs.health import record_invoke_health

    await record_invoke_health(
        factory,
        service_id=service.id,
        connection_id=connection.id,
        ok=False,
        error_code="CONNECTION_EXPIRED",
    )
    await login(env_user.client, env_user.user.email, user_password)
    response = await env_user.client.get("/api/services")
    entries = {entry["id"]: entry for entry in response.json()["services"]}
    entry = entries[f"{env_user.user.handle}/{service.slug}"]
    assert entry["health"] == "needs_reconnect"


async def test_admin_overview_counts_failing_services(
    admin_env: UserEnv,
    user_password: str,
    personal_service: tuple[Service, Connection],
) -> None:
    service, connection = personal_service
    factory = admin_env.app.state.session_factory
    from cam.jobs.health import record_invoke_health

    await record_invoke_health(
        factory,
        service_id=service.id,
        connection_id=connection.id,
        ok=False,
        error_code="UPSTREAM_AUTH",
    )
    await login(admin_env.client, admin_env.user.email, user_password)
    response = await admin_env.client.get("/api/admin/overview")
    assert response.status_code == 200
    by_health = response.json()["services"]["by_health"]
    assert by_health.get("failing", 0) >= 1
