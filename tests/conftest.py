"""Hermetic defaults for the fast test suite."""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path

import pytest

from scripts.release.current_test_manifest import (
    CAD_DATA_TEST_NODES,
    IFC_DIRECT_ENTRY_TEST_NODES,
    IFC_MODEL_KERNEL_TEST_NODES,
    REPOSITORY_HYGIENE_TEST_NODES,
)


_PROXY_ENV_VARS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)
_ACTIVE_TEST_FILES = frozenset(
    node_id.split("::", 1)[0]
    for node_id in (
        *CAD_DATA_TEST_NODES,
        *IFC_MODEL_KERNEL_TEST_NODES,
        *IFC_DIRECT_ENTRY_TEST_NODES,
        *REPOSITORY_HYGIENE_TEST_NODES,
    )
)


def _has_explicit_test_selection(config: pytest.Config) -> bool:
    """Keep exact developer selections possible while making bare pytest safe."""

    return any(
        argument == "tests" or argument.startswith("tests/") or "/tests/" in argument
        for argument in config.invocation_params.args
        if not argument.startswith("-")
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Bare pytest collects only the active capability manifests.

    Explicit pytest node selection remains available for focused foundation
    work, but never expands a release capability gate.
    """

    if _has_explicit_test_selection(config):
        return None
    try:
        relative = collection_path.resolve().relative_to(Path(__file__).resolve().parents[1])
    except ValueError:
        return None
    relative_name = relative.as_posix()
    if not relative_name.startswith("tests/") or not collection_path.name.startswith("test_"):
        return None
    return relative_name not in _ACTIVE_TEST_FILES


@pytest.fixture(autouse=True)
def hermetic_test_environment(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Remove host proxy leakage and reject accidental external connections.

    A test that intentionally exercises the network must opt in with
    ``@pytest.mark.network`` and is excluded from the default suite by also
    carrying an integration, e2e, or live marker.
    """

    for name in _PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    if request.node.get_closest_marker("live") is not None:
        return

    original_connect = socket.socket.connect

    if request.node.get_closest_marker("network") is not None:

        def guarded_connect(sock: socket.socket, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                host = str(address[0]).strip().lower()
                if host != "localhost":
                    try:
                        is_loopback = ipaddress.ip_address(host).is_loopback
                    except ValueError:
                        is_loopback = False
                    if not is_loopback:
                        pytest.fail(f"provider-free network tests are loopback-only: {address!r}")
            return original_connect(sock, address)

        monkeypatch.setattr(socket.socket, "connect", guarded_connect)
        return

    def guarded_connect(sock: socket.socket, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            pytest.fail(f"network access is disabled in the default test suite: {address!r}")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
