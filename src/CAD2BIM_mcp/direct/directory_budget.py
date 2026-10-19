"""Shared response-budget trimming for descending keyset directories."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar


class _JsonModel(Protocol):
    def model_dump_json(self) -> str: ...


T = TypeVar("T")
R = TypeVar("R", bound=_JsonModel)


DIRECTORY_RESPONSE_BYTES = 128 * 1024


def bounded_keyset_directory(
    items: Sequence[T],
    *,
    complete: bool,
    next_before_sequence: int | None,
    sequence_of: Callable[[T], int],
    build_result: Callable[[tuple[T, ...], bool, int | None], R],
    item_label: str,
) -> R:
    """Trim one already-keyset-paged directory without changing its fact source."""

    selected: list[T] = []
    for item in items:
        candidate = build_result(
            tuple([*selected, item]),
            False,
            sequence_of(item),
        )
        if len(candidate.model_dump_json().encode("utf-8")) > DIRECTORY_RESPONSE_BYTES:
            break
        selected.append(item)
    if not selected and items:
        raise ValueError(f"one {item_label} directory card exceeds the response budget")
    truncated = len(selected) < len(items)
    return build_result(
        tuple(selected),
        complete and not truncated,
        (
            None
            if complete and not truncated
            else (sequence_of(selected[-1]) if truncated else next_before_sequence)
        ),
    )


__all__ = ["DIRECTORY_RESPONSE_BYTES", "bounded_keyset_directory"]
