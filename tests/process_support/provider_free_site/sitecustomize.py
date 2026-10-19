"""Fail closed on non-loopback Python network access in provider-free process gates."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any


class ProviderFreeNetworkError(OSError):
    """Raised when a provider-free child attempts non-loopback network access."""


def _assert_loopback(address: Any) -> None:
    if not isinstance(address, tuple) or not address:
        return
    host = address[0]
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(str(host)).is_loopback:
            return
    except ValueError:
        pass
    raise ProviderFreeNetworkError(
        f"provider-free process rejected non-loopback network destination: {host!r}"
    )


if not getattr(socket, "_cad2bim_provider_free_guard", False):
    _original_connect = socket.socket.connect
    _original_connect_ex = socket.socket.connect_ex
    _original_bind = socket.socket.bind
    _original_sendto = socket.socket.sendto
    _original_getaddrinfo = socket.getaddrinfo
    _original_sendmsg = getattr(socket.socket, "sendmsg", None)

    def _connect(self: socket.socket, address: Any) -> Any:
        _assert_loopback(address)
        return _original_connect(self, address)

    def _connect_ex(self: socket.socket, address: Any) -> int:
        _assert_loopback(address)
        return _original_connect_ex(self, address)

    def _bind(self: socket.socket, address: Any) -> Any:
        _assert_loopback(address)
        return _original_bind(self, address)

    def _sendto(self: socket.socket, data: Any, *args: Any) -> int:
        if args:
            _assert_loopback(args[-1])
        return _original_sendto(self, data, *args)

    def _sendmsg(
        self: socket.socket,
        buffers: Any,
        ancdata: Any = (),
        flags: int = 0,
        address: Any = None,
    ) -> int:
        if address is not None:
            _assert_loopback(address)
        if _original_sendmsg is None:
            raise AttributeError("socket.sendmsg is unavailable")
        if address is None:
            return _original_sendmsg(self, buffers, ancdata, flags)
        return _original_sendmsg(self, buffers, ancdata, flags, address)

    def _getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host != "localhost":
            try:
                if not ipaddress.ip_address(str(host)).is_loopback:
                    raise ProviderFreeNetworkError(
                        "provider-free process rejected non-loopback network "
                        f"destination: {host!r}"
                    )
            except ValueError as error:
                raise ProviderFreeNetworkError(
                    "provider-free process rejected non-loopback network "
                    f"destination: {host!r}"
                ) from error
        return _original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.socket.bind = _bind
    socket.socket.sendto = _sendto
    if _original_sendmsg is not None:
        socket.socket.sendmsg = _sendmsg
    socket.getaddrinfo = _getaddrinfo
    socket._cad2bim_provider_free_guard = True
