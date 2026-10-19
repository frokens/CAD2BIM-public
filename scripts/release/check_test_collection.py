"""Validate the explicit current CAD/Data pytest allowlist.

This checker never inventories the superseded repository-wide Agent/IFC test
suite. It only asks pytest to collect the exact nodes owned by the current
phase and rejects an empty or out-of-scope result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

from scripts.release.run_cad_data_gates import (  # noqa: E402
    _CURRENT_MARKER_EXPRESSION,
    FOCUSED_TEST_NODES,
    REPOSITORY_ROOT,
)


_FORBIDDEN_NODE_MARKERS = (
    "tests/unit/agent/",
    "tests/release/compose_api_smoke.py",
    "tests/fakes/process_model.py",
)
_ALLOWED_PROCESS_TEST = "tests/integration/process/test_m2_direct_stdio_m2b.py::"


def _safe_environment() -> dict[str, str]:
    return {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join((str(REPOSITORY_ROOT / "src"), str(REPOSITORY_ROOT))),
        "PYTHONNOUSERSITE": "1",
    }


def check_test_collection() -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            _CURRENT_MARKER_EXPRESSION,
            *FOCUSED_TEST_NODES,
        ],
        cwd=REPOSITORY_ROOT,
        env=_safe_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise RuntimeError("CAD/Data pytest collection failed:\n" + output[-4_000:])
    node_ids = tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    )
    if not node_ids:
        raise RuntimeError("CAD/Data pytest allowlist collected no tests")
    forbidden = tuple(
        node_id
        for node_id in node_ids
        if any(
            marker in node_id.split("::", 1)[0]
            for marker in _FORBIDDEN_NODE_MARKERS
        )
        or (
            node_id.startswith("tests/integration/process/")
            and not node_id.startswith(_ALLOWED_PROCESS_TEST)
        )
    )
    if forbidden:
        raise RuntimeError("CAD/Data allowlist reached legacy product tests")
    return {
        "status": "passed",
        "configured_nodes": len(FOCUSED_TEST_NODES),
        "collected_tests": len(node_ids),
    }


def main() -> int:
    try:
        result = check_test_collection()
    except RuntimeError as error:
        result = {
            "status": "failed",
            "error": str(error),
        }
        print(json.dumps(result, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
