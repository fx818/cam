"""R28: cardinality-safe route labels and bounded invoke-outcome metrics."""

from __future__ import annotations

from cam.api.metrics import (
    OTHER_LABEL,
    REGISTRY,
    UNKNOWN_LABEL,
    UNMATCHED_ROUTE,
    record_invoke_outcome,
    route_template,
)


def test_unmatched_path_uses_fixed_label() -> None:
    assert route_template({"path": "/totally/unknown/path"}) == UNMATCHED_ROUTE
    assert "/totally/unknown/path" not in UNMATCHED_ROUTE


def test_matched_route_template_preferred() -> None:
    class _Route:
        path = "/api/health"

    assert route_template({"route": _Route(), "path": "/api/health"}) == "/api/health"


def test_record_invoke_outcome_counts_ok_and_error() -> None:
    record_invoke_outcome("invoke", is_error=False)
    record_invoke_outcome("invoke", is_error=True, error_code="TIMEOUT")
    ok = REGISTRY.get_sample_value(
        "cam_invoke_actions_total", {"action": "invoke", "outcome": "ok"}
    )
    err = REGISTRY.get_sample_value(
        "cam_invoke_actions_total", {"action": "invoke", "outcome": "error"}
    )
    assert ok is not None and ok >= 1
    assert err is not None and err >= 1
    timeout = REGISTRY.get_sample_value(
        "cam_invoke_errors_total", {"action": "invoke", "code": "TIMEOUT"}
    )
    assert timeout is not None and timeout >= 1


def test_record_invoke_outcome_bounds_labels() -> None:
    record_invoke_outcome("definitely-not-an-action", is_error=False)
    record_invoke_outcome("invoke", is_error=True, error_code="NOT_A_REAL_CODE")
    assert (
        REGISTRY.get_sample_value(
            "cam_invoke_actions_total", {"action": OTHER_LABEL, "outcome": "ok"}
        )
        or 0
    ) >= 1
    unknown = REGISTRY.get_sample_value(
        "cam_invoke_errors_total", {"action": "invoke", "code": UNKNOWN_LABEL}
    )
    assert unknown is not None and unknown >= 1
