"""Shared fixtures for upstream client and pool tests."""

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from fixtures.mock_mcp import MockMCPHandle, start_mock_mcp  # noqa: E402


@pytest.fixture(scope="session")
async def mock_mcp() -> AsyncIterator[MockMCPHandle]:
    """One shared mock for the whole session.

    Starting/stopping the mock more than twice in one process trips a
    streamable-HTTP/SSE teardown race (third initialize never completes),
    so tests share a single long-lived instance and reset counters instead.
    """
    async with start_mock_mcp() as handle:
        yield handle


@pytest.fixture
async def fresh_counts(mock_mcp: MockMCPHandle) -> MockMCPHandle:
    mock_mcp.call_counts.clear()
    return mock_mcp


@pytest.fixture(scope="session")
async def mock_mcp_authed() -> AsyncIterator[MockMCPHandle]:
    async with start_mock_mcp(require_token="test-secret") as handle:
        yield handle
