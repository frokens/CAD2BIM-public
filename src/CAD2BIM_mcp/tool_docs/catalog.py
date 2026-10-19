"""Provider-visible usage text for the catalog capability family."""

DATA_PACKAGE_LIST_DESCRIPTION = (
    "List only immutable source_l0 and derived Data Packages visible in one Direct "
    "session; Operations are discovered separately with operation_list. Cards contain "
    "the Package kind, compiled description title/summary/section directory, stable "
    "sequence and time, exact payload names, and producing_operation_alias for source navigation. "
    "L0 comes from backend processing of a user upload; its producer alias is null when no "
    "processing record is available in this scope. Derived Packages have an exact producing Operation. First page: omit "
    "before_sequence. Continuation: submit next_before_sequence unchanged; the query is "
    "strictly sequence < before_sequence. Default page_size is 20, maximum 50, ordering "
    "is fixed by descending sequence, and there is no cursor, OFFSET, page number, filter "
    "or custom sort. The response is capped at 128 KiB and may return fewer cards with a "
    "continuation. Hard timeout is 10 seconds. Cards are resource facts for selection, "
    "not business approval or transformation results. Discover each required stage in this session and read its "
    "description and payload schema before querying; a final Package does not stand in for its source or "
    "intermediate Packages. Aliases are scoped to this Direct session; a different database or subject does not "
    "inherit them. A supplied observation snapshot is not a live Package query. Report which Packages and "
    "payloads were actually read, queried or unavailable."
)

DATA_GET_PACKAGE_DESCRIPTION_DESCRIPTION = (
    "Read the exact description.md of one source_l0 or derived Data Package selected from "
    "data_package_list. Use it for the Package purpose, method, scope, completeness and "
    "known limitations; use data_describe_package for Payload names/schema and "
    "data_query_rows for facts. This unpaginated read does not return rows, SQL, Artifact "
    "identifiers or Operation detail. Hard timeout is 10 seconds. The Markdown documents "
    "the producer's declared scope and is not an approved business or IFC decision."
)

DATA_DESCRIBE_PACKAGE_DESCRIPTION = (
    "Describe one immutable source_l0 or derived Data Package selected from "
    "data_package_list. "
    "Returns producing_operation_alias and every payload name, row count, and logical key; "
    "the alias is null for L0 when its processing record was not recorded or is inaccessible. Set optional "
    "payload_name from that list to obtain its exact field schema. This call is not "
    "paginated and does not return samples, rows, code, expanded lineage, transformations, or "
    "arbitrary column statistics. Hard timeout is 10 seconds."
)

DATA_PACKAGE_LIST_BEFORE_SEQUENCE_DESCRIPTION = (
    "Optional Package sequence boundary. Omit or use null on the first page; "
    "then submit next_before_sequence unchanged."
)

DATA_PACKAGE_LIST_PAGE_SIZE_DESCRIPTION = "Package cards requested; default 20 and maximum 50."

DATA_GET_PACKAGE_DESCRIPTION_DIRECT_SESSION_ALIAS_DESCRIPTION = "Direct session alias."

DATA_GET_PACKAGE_DESCRIPTION_PACKAGE_ALIAS_DESCRIPTION = "Package alias from the catalog."

DATA_DESCRIBE_PACKAGE_PACKAGE_ALIAS_DESCRIPTION = (
    "Package alias discovered in the catalog after user upload and backend processing."
)

DATA_DESCRIBE_PACKAGE_PAYLOAD_NAME_DESCRIPTION = (
    "Optional payload name from the package card; omit to list payloads "
    "without returning a field schema."
)
