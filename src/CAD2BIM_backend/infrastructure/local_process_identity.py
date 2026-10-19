"""Proof of a local process lifetime for fenced, recoverable execution."""

from __future__ import annotations

import hashlib
import os
import re
import socket
from uuid import uuid4

import psutil


def _host_identity() -> str:
    return hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:16]


def current_local_executor_owner(kind: str) -> str:
    """Return a claim owner bound to this process birth, including PID reuse."""
    if kind not in {"transform", "authoring", "ifc"}:
        raise ValueError("unsupported local executor kind")
    pid = os.getpid()
    birth_us = round(psutil.Process(pid).create_time() * 1_000_000)
    return f"{kind}:{_host_identity()}:{pid}:{birth_us}:{uuid4().hex[:16]}"


def local_executor_stopped(owner: str, *, kind: str) -> bool:
    """Return true only when a correctly identified local process has ended."""
    if kind not in {"transform", "authoring", "ifc"}:
        raise ValueError("unsupported local executor kind")
    pattern = re.compile(
        rf"^{re.escape(kind)}:([0-9a-f]{{16}}):([1-9][0-9]*):"
        r"([1-9][0-9]*):([0-9a-f]{16})$"
    )
    match = pattern.fullmatch(owner)
    if match is None or match.group(1) != _host_identity():
        return False
    pid = int(match.group(2))
    birth_us = int(match.group(3))
    try:
        current_birth_us = round(psutil.Process(pid).create_time() * 1_000_000)
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, OSError):
        return False
    return current_birth_us != birth_us


def local_guard_process_group_stopped(pid: int, birth_us: int) -> bool:
    """Prove the registered guardian's entire process group is absent.

    An inaccessible or reused group is unknown and therefore remains owned.
    The birth value is validated at registration and retained for audit; the
    group absence check also covers a guardian that exited before its child.
    """
    if pid <= 0 or birth_us <= 0:
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


__all__ = [
    "current_local_executor_owner", "local_executor_stopped",
    "local_guard_process_group_stopped",
]
