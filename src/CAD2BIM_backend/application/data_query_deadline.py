"""One shared deadline split for temporary data-query execution and terminal facts."""

from __future__ import annotations


from CAD2BIM_backend.domain.runtime_execution import DATA_SQL_EXECUTION

DATA_QUERY_TERMINAL_PUBLICATION_RESERVE_SECONDS = (
    DATA_SQL_EXECUTION.terminal_seconds + DATA_SQL_EXECUTION.cleanup_seconds
)


def data_query_execution_deadline(whole_call_deadline: float) -> float:
    """Stop execution early enough for cleanup and durable terminal publication."""

    return whole_call_deadline - DATA_QUERY_TERMINAL_PUBLICATION_RESERVE_SECONDS


__all__ = [
    "DATA_QUERY_TERMINAL_PUBLICATION_RESERVE_SECONDS",
    "data_query_execution_deadline",
]
