"""Runs the Phase 7.2 load script in pytest mode: few agents, all assertions.

The full 50/100/200 sweep stays in scripts/load_test.py; this covers the
non-blocking and audit-completeness checks without adding minutes to CI.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


async def test_load_script_pytest_mode() -> None:
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    spec = importlib.util.spec_from_file_location(
        "cam_load_test", REPO_ROOT / "scripts" / "load_test.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert await module.main([8], pytest_mode=True) == 0
