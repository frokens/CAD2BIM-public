from __future__ import annotations

import asyncio
import json

import pytest

from CAD2BIM_backend.infrastructure.chatgpt_auth import ChatGPTAuth, ChatGPTAuthError


class _Input:
    def __init__(self, output):
        self.output = output
        self.received = []

    def write(self, line):
        request = json.loads(line)
        self.received.append(request)
        result = "private-fixture-token" if request["command"] == "access-token" else {"status": "connected", "sharing": True}
        self.output.feed_data(json.dumps({"id": request["id"], "result": result}).encode() + b"\n")

    async def drain(self):
        pass


async def test_private_supplier_has_no_public_token_command_and_preserves_association(tmp_path):
    from types import SimpleNamespace

    output = asyncio.StreamReader()
    process = SimpleNamespace(returncode=None, stdin=_Input(output), stdout=output)
    auth = ChatGPTAuth(tmp_path)
    auth._process = process
    assert await auth.get_access_token() == "private-fixture-token"
    assert await auth.get_session() == {"status": "connected", "sharing": True}
    assert process.stdin.received == [{"id": 1, "command": "access-token"}, {"id": 2, "command": "status"}]
    assert "private-fixture-token" not in repr(auth)


async def test_cancelled_waiter_does_not_discard_refresh_reply_or_mix_next_request(tmp_path):
    auth = ChatGPTAuth(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    operations = []

    async def exchange(command):
        async with auth._lock:
            operations.append(command)
            if command == "access-token":
                started.set()
                await finish.wait()
            return command

    auth._exchange = exchange
    caller = asyncio.create_task(auth._request("access-token"))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    next_request = asyncio.create_task(auth._request("status"))
    await asyncio.sleep(0)
    assert operations == ["access-token"]
    finish.set()
    assert await next_request == "status"
    assert operations == ["access-token", "status"]


def test_official_auth_error_keeps_safe_code_and_retryability():
    failure = ChatGPTAuthError({"code": "sharing_not_enabled", "message": "Enable sharing", "retryable": False})
    assert failure.code == "sharing_not_enabled" and not failure.retryable
