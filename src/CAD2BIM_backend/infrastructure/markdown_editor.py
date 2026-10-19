"""Thin binding to Deep Agents' exact string replacement function."""

from __future__ import annotations


class MarkdownEditError(ValueError):
    """The approved exact edit did not match its current text."""


class DeepAgentsMarkdownEditor:
    def replace(
        self,
        content: str,
        *,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> tuple[str, int]:
        if not old_string:
            raise MarkdownEditError("old_string must not be empty")
        from deepagents.backends.utils import perform_string_replacement

        result = perform_string_replacement(content, old_string, new_string, replace_all)
        if isinstance(result, str):
            raise MarkdownEditError(result)
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], str)
            or not isinstance(result[1], int)
            or result[1] < 1
        ):
            raise RuntimeError("Deep Agents returned an invalid markdown edit result")
        return result


__all__ = ["DeepAgentsMarkdownEditor", "MarkdownEditError"]
