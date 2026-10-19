from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

from tests.process_support.runtime import base_environment
from tests.process_support.runtime import ProcessRuntime


def test_process_environment_prefers_the_active_worktree_source(
    tmp_path: Path,
) -> None:
    environment = base_environment(tmp_path)

    assert environment["PYTHONPATH"].split(os.pathsep) == [
        str(tmp_path / "src"),
        str(tmp_path),
    ]


def test_process_runtime_waits_only_for_new_log_content(tmp_path: Path) -> None:
    runtime = ProcessRuntime(tmp_path / "runtime")
    runtime.start(
        "worker",
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(0.2)",
        ],
        environment=dict(os.environ),
    )
    try:
        runtime.wait_for_log("worker", "ready", timeout_seconds=2)
        size = runtime.log_size("worker")
        assert size > 0
        try:
            runtime.wait_for_log(
                "worker", "missing", after_size=size, timeout_seconds=0.05
            )
        except AssertionError as exc:
            assert "missing" in str(exc)
        else:  # pragma: no cover - assertion helper must fail closed
            raise AssertionError("wait_for_log accepted stale content")
    finally:
        runtime.close()


def test_process_runtime_force_kills_a_process_that_ignores_graceful_shutdown(
) -> None:
    process = Mock(pid=12345)
    process.wait.side_effect = (
        subprocess.TimeoutExpired(("worker",), timeout=10),
        subprocess.TimeoutExpired(("worker",), timeout=10),
        0,
    )
    ProcessRuntime._stop_process(process)

    assert [entry.args for entry in process.send_signal.call_args_list] == [
        (signal.SIGINT,),
        (signal.SIGTERM,),
        (signal.SIGKILL,),
    ]
    assert process.wait.call_count == 3
