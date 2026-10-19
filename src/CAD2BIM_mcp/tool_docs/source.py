"""Provider-visible usage text for the source capability family."""

FULL_SOURCE_VIEW_VIEW_MODE_DESCRIPTION = (
    "Return the complete exact source when it fits the response limit."
)

LINES_SOURCE_VIEW_VIEW_MODE_DESCRIPTION = (
    "Return one exact 1-based line range from the complete source."
)

LINES_SOURCE_VIEW_START_LINE_DESCRIPTION = (
    "First exact source line to return, using 1-based numbering."
)

LINES_SOURCE_VIEW_LINE_COUNT_DESCRIPTION = (
    "Number of exact lines requested; minimum 1 and maximum 200."
)

SEARCH_SOURCE_VIEW_VIEW_MODE_DESCRIPTION = (
    "Search the exact source for a case-sensitive literal substring."
)

SEARCH_SOURCE_VIEW_TEXT_DESCRIPTION = (
    "Case-sensitive literal text of at most 65,536 UTF-8 bytes; regex and fuzzy "
    "search are unsupported."
)

SEARCH_SOURCE_VIEW_CONTEXT_LINES_DESCRIPTION = (
    "Complete context lines before and after each match; maximum 20."
)

SEARCH_SOURCE_VIEW_MAX_MATCHES_DESCRIPTION = (
    "Maximum literal matches returned; default 20 and maximum 50."
)
