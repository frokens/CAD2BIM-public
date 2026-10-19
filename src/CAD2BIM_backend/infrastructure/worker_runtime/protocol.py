"""Small bounded framed-JSON protocol shared by controlled workers."""

from __future__ import annotations

import json
import os
import struct
from typing import Any, BinaryIO


_HEADER = struct.Struct("!I")
MAX_CONTROL_BYTES = 256 * 1024
MAX_STATUS_BYTES = 32 * 1024


class WorkerProtocolError(RuntimeError):
    pass


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise WorkerProtocolError("worker protocol frame ended early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_json_frame(stream: BinaryIO, *, limit: int) -> dict[str, Any]:
    size = _HEADER.unpack(_read_exact(stream, _HEADER.size))[0]
    if not 1 <= size <= limit:
        raise WorkerProtocolError("worker protocol frame exceeds its byte limit")
    try:
        value = json.loads(_read_exact(stream, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError("worker protocol frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise WorkerProtocolError("worker protocol frame must contain an object")
    return value


def encode_json_frame(value: dict[str, Any], *, limit: int) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return frame_json_payload(payload, limit=limit)


def frame_json_payload(payload: bytes, *, limit: int) -> bytes:
    """Frame JSON already emitted by a trusted serializer, without parsing it again."""
    if not 1 <= len(payload) <= limit:
        raise WorkerProtocolError("worker protocol frame exceeds its byte limit")
    return _HEADER.pack(len(payload)) + payload


def write_json_frame(stream: BinaryIO, value: dict[str, Any], *, limit: int) -> None:
    write_all(stream, encode_json_frame(value, limit=limit))
    stream.flush()


def write_all(stream: BinaryIO, payload: bytes) -> None:
    """Raw inherited pipes may accept a short write even in blocking mode."""
    view = memoryview(payload)
    while view:
        count = stream.write(view[:65_536])
        if not isinstance(count, int) or count <= 0:
            raise WorkerProtocolError("worker protocol payload could not be written")
        view = view[count:]


def open_binary_fd(descriptor: int, mode: str) -> BinaryIO:
    """Own one inherited descriptor without accepting a filesystem path."""

    if mode not in {"rb", "wb"}:
        raise ValueError("worker descriptor mode must be rb or wb")
    return os.fdopen(descriptor, mode, buffering=0)


__all__ = [
    "MAX_CONTROL_BYTES",
    "MAX_STATUS_BYTES",
    "WorkerProtocolError",
    "encode_json_frame",
    "open_binary_fd",
    "read_json_frame",
    "write_json_frame",
]


class AsyncWorkerChannel:
    """Host side of owned pipes; one bounded frame/payload, no helper threads."""

    def __init__(self, read_fd: int, write_fd: int) -> None:
        self.read_fd, self.write_fd = read_fd, write_fd
        self.bytes_read = 0
        self.bytes_written = 0
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)

    async def _ready(self, descriptor: int, *, writing: bool = False) -> None:
        import asyncio
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        def wake():
            if not ready.done():
                ready.set_result(None)
        add = loop.add_writer if writing else loop.add_reader
        remove = loop.remove_writer if writing else loop.remove_reader
        add(descriptor, wake)
        try:
            await ready
        finally:
            remove(descriptor)

    async def read_exact(self, size: int) -> bytes:
        if not 0 <= size <= 64 * 1024 * 1024:
            raise WorkerProtocolError("worker channel payload exceeds its byte limit")
        result = bytearray()
        while len(result) < size:
            try:
                chunk = os.read(self.read_fd, min(65_536, size - len(result)))
            except BlockingIOError:
                await self._ready(self.read_fd)
                continue
            if not chunk:
                raise WorkerProtocolError("worker channel ended early")
            result.extend(chunk)
            self.bytes_read += len(chunk)
        return bytes(result)

    async def write(self, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            try:
                count = os.write(self.write_fd, view[:65_536])
            except BlockingIOError:
                await self._ready(self.write_fd, writing=True)
                continue
            if count == 0:
                raise WorkerProtocolError("worker channel cannot accept bytes")
            view = view[count:]
            self.bytes_written += count

    async def read_json(self, *, limit: int) -> dict[str, Any]:
        size = _HEADER.unpack(await self.read_exact(_HEADER.size))[0]
        if not 1 <= size <= limit:
            raise WorkerProtocolError("worker channel JSON frame exceeds its byte limit")
        try:
            value = json.loads(await self.read_exact(size))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerProtocolError("worker channel JSON frame is invalid") from exc
        if not isinstance(value, dict):
            raise WorkerProtocolError("worker channel JSON frame must be an object")
        return value

    async def write_json(self, value: dict[str, Any], *, limit: int) -> None:
        await self.write(encode_json_frame(value, limit=limit))

    async def require_eof(self) -> None:
        while True:
            try:
                trailing = os.read(self.read_fd, 1)
            except BlockingIOError:
                await self._ready(self.read_fd)
                continue
            if trailing:
                raise WorkerProtocolError("worker channel has trailing bytes")
            return
