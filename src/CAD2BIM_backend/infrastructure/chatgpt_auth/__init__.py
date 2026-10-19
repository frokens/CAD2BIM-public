"""Private pipe to the official local SIWC credential owner; never a model proxy."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Self


class ChatGPTAuthError(RuntimeError):
    """Safe official authentication diagnostics without credential/request dumps."""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.code = detail.get("code", "auth_helper_failed")
        self.retryable = bool(detail.get("retryable", False))
        super().__init__(detail.get("message", "ChatGPT authentication failed"))


class ChatGPTAuth:
    """One application registration managed solely by the official encrypted store."""

    def __init__(self, storage_dir: Path | None = None) -> None:
        self.storage_dir = (storage_dir or Path.home() / ".config/cad2bim/component-demo").resolve()
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._sequence = 0

    async def _start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        root = Path(__file__).parent
        runtime_path = root / "node_modules/electron/path.txt"
        helper = root / "build/main.cjs"
        if not runtime_path.is_file() or not helper.is_file():
            raise ChatGPTAuthError({
                "code": "auth_helper_not_built",
                "message": "Install and build the local authentication helper first.",
            })
        # Use the prepared binary directly. Electron's Node CLI can lazily install
        # and print non-protocol output on the private credential pipe.
        executable = root / "node_modules/electron/dist" / runtime_path.read_text().strip()
        if not executable.is_file():
            raise ChatGPTAuthError({
                "code": "auth_helper_not_built", "message": "Prepare the Electron runtime first.",
            })
        self.storage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.storage_dir.chmod(0o700)
        environment = dict(os.environ)
        environment.pop("ELECTRON_RUN_AS_NODE", None)
        environment.pop("ELECTRON_ENABLE_LOGGING", None)
        if environment.get("HTTPS_PROXY") or environment.get("HTTP_PROXY"):
            environment["NODE_USE_ENV_PROXY"] = "1"
        environment["CAD2BIM_CHATGPT_STORAGE_DIR"] = str(self.storage_dir)
        self._process = await asyncio.create_subprocess_exec(
            str(executable), str(helper), cwd=root, env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _exchange(self, command: str) -> Any:
        async with self._lock:
            await self._start()
            process = self._process
            assert process is not None and process.stdin is not None and process.stdout is not None
            self._sequence += 1
            identity = self._sequence
            process.stdin.write(json.dumps({"id": identity, "command": command}).encode() + b"\n")
            await process.stdin.drain()
            # stdout is private protocol data, including the access token. Never log it.
            line = await process.stdout.readline()
            if not line:
                raise ChatGPTAuthError({"code": "auth_helper_stopped", "message": "The authentication helper stopped."})
            try:
                response = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ChatGPTAuthError({"code": "auth_helper_protocol", "message": "Invalid authentication reply."}) from error
            if detail := response.get("error"):
                raise ChatGPTAuthError(detail)
            if response.get("id") != identity:
                raise ChatGPTAuthError({"code": "auth_helper_protocol", "message": "Authentication reply identity mismatch."})
            return response["result"]

    async def _request(self, command: str) -> Any:
        # Cancellation of a model waiter must not discard an in-flight refresh rotation.
        return await asyncio.shield(self._exchange(command))

    async def get_access_token(self) -> str:
        result = await self._request("access-token")
        if not isinstance(result, str) or not result:
            raise ChatGPTAuthError({"code": "auth_helper_protocol", "message": "No usable access token was supplied."})
        return result

    async def get_session(self) -> dict[str, Any]:
        return await self._request("status")

    async def sign_in(self, *, new_profile: bool = False) -> dict[str, Any]:
        return await self._request("sign-in-new" if new_profile else "sign-in")

    async def close(self) -> None:
        if self._process is None:
            return
        process, self._process = self._process, None
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.terminate()
            await process.wait()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
