"""Wait for an already owned settlement task without transferring cancellation."""

from __future__ import annotations

import asyncio


async def drain_shielded_task(task: asyncio.Task[object]) -> BaseException | None:
    """Observe completion despite repeated caller cancellation; caller owns its budget."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    try:
        task.result()
    except BaseException as cause:
        return cause
    return None


__all__ = ["drain_shielded_task"]
