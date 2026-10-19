from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from CAD2BIM_backend.infrastructure.data_transform import python_arrow_sandbox
from CAD2BIM_backend.infrastructure.data_transform.python_arrow_sandbox import (
    DarwinSeatbeltPythonArrowSandbox,
)
from CAD2BIM_backend.infrastructure.data_transform.python_worker_environment import (
    DATA_PYTHON_CHILD_SOURCE_PATHS,
    DataPythonWorkerDiagnosticCategory,
    DataPythonWorkerEnvironmentError,
)


def _sandbox() -> DarwinSeatbeltPythonArrowSandbox:
    return DarwinSeatbeltPythonArrowSandbox(sys.executable)


def _run_in_sandbox(tmp_path: Path, code: str) -> subprocess.CompletedProcess[str]:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    command = _sandbox().command(scratch=scratch, child_args=("-I", "-B", "-c", code))
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)


def test_profile_is_fail_closed_for_host_network_and_processes(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    profile = _sandbox().profile(scratch=scratch)
    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert "(deny process-fork)" in profile
    assert "(allow process-exec" in profile
    assert "(allow process*)" not in profile
    assert os.fspath(scratch.resolve()) in profile
    assert os.fspath(Path(sys.executable).resolve()) in profile


@pytest.mark.parametrize("descriptor", (-1, True, "3"))
def test_profile_rejects_invalid_metadata_descriptor(tmp_path, descriptor):
    with pytest.raises(ValueError, match="metadata descriptor"):
        _sandbox().profile(scratch=tmp_path, metadata_read_fds=(descriptor,))


def test_os_boundary_limits_metadata_to_exact_inherited_descriptor(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = tmp_path / "source.ifc"
    source.write_bytes(b"exact source")
    other = tmp_path / "other.ifc"
    other.write_bytes(b"unrelated source")
    with source.open("rb") as source_stream, other.open("rb") as other_stream:
        source_fd, other_fd = source_stream.fileno(), other_stream.fileno()
        source.unlink()
        code = f"""
import errno, os
from pathlib import Path
source_fd, other_fd = {source_fd}, {other_fd}
assert os.fstat(source_fd).st_nlink == 0
assert Path(f'/dev/fd/{{source_fd}}').stat().st_size == 12
with open(f'/dev/fd/{{source_fd}}', 'rb') as stream:
    assert stream.read() == b'exact source'
for action, expected in (
    (lambda: Path(f'/dev/fd/{{other_fd}}').stat(), errno.EPERM),
    (lambda: open({str(other)!r}, 'rb'), errno.EPERM),
    (lambda: open(f'/dev/fd/{{source_fd}}', 'wb'), errno.EACCES),
    (lambda: os.write(source_fd, b'x'), errno.EBADF),
    (lambda: open('/dev/fd/99999', 'rb'), errno.EBADF),
):
    try:
        action()
    except OSError as exc:
        assert exc.errno == expected, (exc, expected)
    else:
        raise AssertionError('unexpected permission')
"""
        command = _sandbox().command(scratch=scratch, child_args=("-I", "-B", "-c", code),
                                     metadata_read_fds=(source_fd,))
        result = subprocess.run(command, pass_fds=(source_fd, other_fd), check=False,
                                capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_editable_child_sources_are_the_authoritative_execution_subset() -> None:
    source_root = Path(__file__).resolve().parents[3] / "src"
    relative = tuple(
        path.relative_to(source_root).as_posix()
        for path in python_arrow_sandbox._trusted_source_files()
    )

    assert relative == DATA_PYTHON_CHILD_SOURCE_PATHS


@pytest.mark.parametrize("installed", [False, True])
def test_child_bootstrap_uses_host_source_even_with_an_installed_copy(tmp_path, monkeypatch, installed):
    executable = tmp_path / "environment" / "bin" / "python"
    if installed:
        child = executable.parent.parent / "lib/python3.11/site-packages/CAD2BIM_backend/infrastructure/data_transform/python_arrow_child.py"
        child.parent.mkdir(parents=True)
        child.write_text("# installed environment source\n")
    monkeypatch.setattr(python_arrow_sandbox.DarwinSeatbeltSandbox, "__init__", lambda *args, **kwargs: None)
    sandbox = DarwinSeatbeltPythonArrowSandbox(str(executable))
    args = sandbox.child_arguments
    assert args[:2] == ("-I", "-B")
    assert args[-1] == "CAD2BIM_backend.infrastructure.data_transform.python_arrow_child"
    assert args[2] == "-c"
    assert Path(args[-2]) == Path(python_arrow_sandbox.__file__).resolve().parents[3]


def test_missing_approved_isolator_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        python_arrow_sandbox,
        "DARWIN_SANDBOX_EXECUTABLE",
        tmp_path / "missing-sandbox-exec",
    )
    with pytest.raises(
        DataPythonWorkerEnvironmentError,
        match="Seatbelt sandbox-exec is unavailable",
    ) as captured:
        DarwinSeatbeltPythonArrowSandbox(sys.executable)
    assert captured.value.category is DataPythonWorkerDiagnosticCategory.SANDBOX_FAILURE
    assert len(captured.value.recovery_commands) == 1
    assert "--check" in captured.value.recovery_commands[0]


def test_platform_without_approved_isolator_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(python_arrow_sandbox.sys, "platform", "linux")
    with pytest.raises(
        DataPythonWorkerEnvironmentError,
        match="only Darwin Seatbelt is currently approved",
    ) as captured:
        DarwinSeatbeltPythonArrowSandbox(sys.executable)
    assert captured.value.category is DataPythonWorkerDiagnosticCategory.SANDBOX_FAILURE


@pytest.mark.parametrize(
    "code, expected",
    (
        ("open('/etc/hosts', 'rb').read(1)", "Operation not permitted"),
        ("open('/etc/passwd', 'rb').read(1)", "Operation not permitted"),
        (
            "import socket; "
            "socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(('127.0.0.1', 9))",
            "Operation not permitted",
        ),
        (
            "import subprocess; subprocess.run(['/usr/bin/true'], check=True)",
            "Operation not permitted",
        ),
        ("import os; os.fork()", "Operation not permitted"),
        (
            "import os, sys; "
            "os.execv(sys.executable, "
            "[sys.executable, '-I', '-c', \"print('unexpected exec')\"])",
            "Operation not permitted",
        ),
    ),
)
def test_os_boundary_rejects_file_network_and_subprocess(
    tmp_path: Path,
    code: str,
    expected: str,
) -> None:
    result = _run_in_sandbox(tmp_path, code)
    assert result.returncode != 0
    assert expected in result.stderr


def test_os_boundary_allows_approved_native_library_and_private_scratch(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    output = scratch / "native.arrow"
    code = (
        "import pyarrow as pa; "
        f"path={str(output)!r}; "
        "table=pa.table({'value':[1,2]}); "
        "sink=pa.output_stream(path); "
        "writer=pa.ipc.new_stream(sink, table.schema); "
        "writer.write_table(table); writer.close(); sink.close(); "
        "reader=pa.input_stream; source=reader(path); "
        "print(pa.ipc.open_stream(source).read_all().num_rows)"
    )
    command = _sandbox().command(scratch=scratch, child_args=("-I", "-B", "-c", code))
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"
