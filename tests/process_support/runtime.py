from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

import httpx


def free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def base_environment(repo_root: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "ALL_PROXY",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "all_proxy",
            "https_proxy",
            "http_proxy",
            "CAD2BIM_ENV_FILE",
            "DEEPSEEK_API_KEY",
            "KIMI_API_KEY",
            "AUTHING_APP_SECRET",
            "CAD2BIM_FAKE_MODEL_MODE",
            "CAD2BIM_FAKE_MODEL_SIGNAL_DIR",
        }
    }
    python_paths = [str(repo_root / "src"), str(repo_root)]
    provider_free_site = str(repo_root / "tests/process_support/provider_free_site")
    if provider_free_site in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        python_paths.append(provider_free_site)
    environment.update(
        {
            # Console scripts can come from a shared editable virtualenv. Put this
            # worktree's source tree first so child processes execute the same
            # revision that the test collected, not the checkout that installed
            # the entry point.
            "PYTHONPATH": os.pathsep.join(python_paths),
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }
    )
    return environment


class ProcessRuntime:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.log_dir = run_dir / "logs"
        self.log_dir.mkdir(parents=True)
        self._stack = ExitStack()
        self._processes: list[tuple[str, subprocess.Popen[bytes]]] = []
        self.pg_data: Path | None = None
        self._closed = False
        self._previous_sigterm = signal.getsignal(signal.SIGTERM)
        self._sigterm_handler = self._handle_sigterm
        signal.signal(signal.SIGTERM, self._sigterm_handler)

    def start(
        self,
        name: str,
        command: list[str],
        *,
        environment: dict[str, str],
    ) -> subprocess.Popen[bytes]:
        if any(existing == name and process.poll() is None for existing, process in self._processes):
            raise ValueError(f"process is already running: {name}")
        log = self._stack.enter_context((self.log_dir / f"{name}.log").open("ab"))
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self._processes.append((name, process))
        return process

    def stop(self, name: str) -> int:
        for existing, process in reversed(self._processes):
            if existing != name or process.poll() is not None:
                continue
            self._stop_process(process)
            return int(process.returncode or 0)
        raise ValueError(f"process is not running: {name}")

    def kill(self, name: str) -> int:
        for existing, process in reversed(self._processes):
            if existing != name or process.poll() is not None:
                continue
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=10)
            return int(process.returncode or 0)
        raise ValueError(f"process is not running: {name}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for _name, process in reversed(self._processes):
            if process.poll() is not None:
                continue
            self._stop_process(process)
        if self.pg_data is not None:
            subprocess.run(
                ["pg_ctl", "-D", str(self.pg_data), "stop", "-m", "fast"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self._stack.close()
        if signal.getsignal(signal.SIGTERM) is self._sigterm_handler:
            signal.signal(signal.SIGTERM, self._previous_sigterm)

    def _handle_sigterm(self, _signum, _frame) -> None:
        self.close()
        raise SystemExit(128 + signal.SIGTERM)

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGKILL)
                process.wait(timeout=10)

    def tail(self, name: str, *, lines: int = 80) -> str:
        path = self.log_dir / f"{name}.log"
        if not path.exists():
            return ""
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

    def log_size(self, name: str) -> int:
        path = self.log_dir / f"{name}.log"
        return path.stat().st_size if path.exists() else 0

    def wait_for_log(
        self,
        name: str,
        marker: str,
        *,
        after_size: int = 0,
        timeout_seconds: float = 30,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        path = self.log_dir / f"{name}.log"
        while time.monotonic() < deadline:
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(after_size)
                    if marker.encode("utf-8") in stream.read():
                        return
            process = next(
                (
                    candidate
                    for existing, candidate in reversed(self._processes)
                    if existing == name and candidate.poll() is None
                ),
                None,
            )
            if process is None:
                raise AssertionError(
                    f"process exited before log marker {marker!r}: {self.tail(name)}"
                )
            time.sleep(0.05)
        raise AssertionError(
            f"timed out waiting for {name} log marker {marker!r}: {self.tail(name)}"
        )


def wait_http(
    client: httpx.Client,
    url: str,
    *,
    accepted: set[int],
    timeout_seconds: float = 20,
) -> httpx.Response:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = client.get(url)
            if response.status_code in accepted:
                return response
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"service did not become ready: {url}; last_error={last_error!r}")


def run_command(command: list[str], *, environment: dict[str, str], log: Path) -> None:
    with log.open("wb") as output:
        subprocess.run(
            command,
            check=True,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )


__all__ = ["ProcessRuntime", "base_environment", "free_port", "run_command", "wait_http"]
