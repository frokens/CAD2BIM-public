from __future__ import annotations

import socket

import pytest


pytestmark = [pytest.mark.integration, pytest.mark.network]


def test_provider_free_network_marker_installs_loopback_guard() -> None:
    assert socket.socket.connect.__name__ == "guarded_connect"


def test_provider_free_network_marker_allows_loopback() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.socket() as client:
            client.connect(listener.getsockname())
            accepted, _address = listener.accept()
            accepted.close()


def test_provider_free_network_marker_rejects_external_destination() -> None:
    with socket.socket() as client:
        client.settimeout(0.01)
        with pytest.raises(pytest.fail.Exception, match="loopback-only"):
            client.connect(("203.0.113.1", 443))
