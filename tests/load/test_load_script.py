"""Runs the Phase 7.2 load script in pytest mode: few agents, all assertions.

The full 50/100/200 sweep stays in scripts/load_test.py; this covers the
non-blocking and audit-completeness checks without adding minutes to CI.
"""

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROFILED_ENV_KEYS = (
    "CAM_DATABASE_URL",
    "CAM_ENV",
    "CAM_JOBS_ENABLED",
    "CAM_ENCRYPTION_KEYS",
    "CAM_ENCRYPTION_KEY_CURRENT",
    "CAM_LOG_LEVEL",
)


async def test_load_script_pytest_mode() -> None:
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    spec = importlib.util.spec_from_file_location(
        "cam_load_test", REPO_ROOT / "scripts" / "load_test.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    saved = {key: os.environ.get(key) for key in _PROFILED_ENV_KEYS}
    try:
        spec.loader.exec_module(module)
        assert await module.main([8], pytest_mode=True) == 0
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
