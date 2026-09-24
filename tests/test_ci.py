from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
COMPOSE = REPO_ROOT / "docker-compose.yml"


def test_ci_workflow_exists() -> None:
    assert WORKFLOW.is_file()


def test_ci_runs_required_checks_on_push_and_pr() -> None:
    content = WORKFLOW.read_text()
    for command in ("ruff check", "ruff format", "pyright", "pytest"):
        assert command in content
    assert "push" in content
    assert "pull_request" in content
    assert "postgres:16" in content


def test_compose_starts_postgres_and_app_with_healthchecks() -> None:
    content = COMPOSE.read_text()
    assert "postgres:16" in content
    assert "pg_isready" in content
    assert "/api/health" in content
    assert "healthcheck" in content
