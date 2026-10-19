"""Provider-visible usage text for the ifc official docs capability family."""

IFC_SEARCH_OFFICIAL_DOCS_DESCRIPTION = (
    "Search the maintained buildingSMART IFC 4.3.2 DEV documentation, not a frozen RELEASE. Submit a natural-"
    "language question or exact Ifc concept; English Ifc names usually search best. "
    "The tool returns only small title, official URL, and official snippet cards in the "
    "adapter's deterministic order (query in title, exact title terms, then title, heading and body term hits), "
    "with default 3 and maximum 5 results. Matching is case-insensitive and not the website's fuzzy ranking. "
    "The official publisher's prebuilt search index is read from the fixed buildingSMART/Standards-builds "
    "repository on raw.githubusercontent.com and cached in memory for at most 5 minutes per client; "
    "there is no persistent local index. It does not fetch full result "
    "pages, use RAG, summarize with a model, or make a project modeling decision. There "
    "is no cursor, pagination, Direct session, Package, or Operation, and the hard "
    "timeout is 45 seconds. The search download is bounded by 32 MiB and the complete "
    "response by 32 KiB. Temporary network/429/5xx failures use retryable "
    "IFC_DOCS_UNAVAILABLE; an off-allowlist redirect or changed/invalid publication uses "
    "non-retryable IFC_DOCS_RESPONSE_INVALID. Select the useful card yourself and call "
    "ifc_read_official_doc with its URL; search again with a narrower concept when the "
    "cards do not answer the question. Minimal call: query='IfcValve', max_results=3."
)

IFC_READ_OFFICIAL_DOC_DESCRIPTION = (
    "Read one buildingSMART IFC 4.3.2 documentation page selected from "
    "ifc_search_official_docs. The URL must be an allowed official result URL; this is "
    "not an arbitrary web reader. Allowed pages are standards.buildingsmart.org/IFC/DEV/IFC4_3/HTML/ "
    "and /IFC/RELEASE/IFC4_3/HTML/. Legacy ifc43-docs links map to DEV, following the official migration. "
    "DEV is maintained documentation, not a frozen formal release; use an explicit RELEASE page when required. "
    "Bytes come from the matching fixed buildingSMART/Standards-builds publisher path on raw.githubusercontent.com. "
    "Optional focus text selects relevant original blocks "
    "while preserving their document order. The response returns title, canonical "
    "official URL, bounded original page text, and truncated; it is never an LLM "
    "summary. There is no cursor or pagination and the hard timeout is 45 seconds. Call "
    "search/read again for another page when one page is insufficient. One page download "
    "is bounded by 4 MiB and the complete response by 32 KiB. Temporary network/429/5xx "
    "failures use retryable IFC_DOCS_UNAVAILABLE; an invalid or off-allowlist page uses "
    "non-retryable IFC_DOCS_RESPONSE_INVALID. Minimal call: pass one URL returned by "
    "search and optionally focus='ports and connections'. Official text is "
    "reference material, not a project-specific IFC modeling decision."
)

IFC_SEARCH_OFFICIAL_DOCS_QUERY_DESCRIPTION = (
    "Natural-language question or exact IFC concept to search."
)

IFC_SEARCH_OFFICIAL_DOCS_MAX_RESULTS_DESCRIPTION = (
    "Official result cards requested; default 3 and maximum 5."
)

IFC_READ_OFFICIAL_DOC_URL_DESCRIPTION = (
    "Exact official buildingSMART IFC page URL returned by search."
)

IFC_READ_OFFICIAL_DOC_FOCUS_DESCRIPTION = (
    "Optional terms for selecting relevant original text blocks; omit to read "
    "the page from its beginning within the response budget."
)
