"""Provider-visible usage text for the code capability family."""

CODE_RESULT_AVAILABLE_DESCRIPTION = (
    "Whether a Data QueryAttempt's temporary in-process result snapshot is currently "
    "available. Other owner types return null and expose durable results through "
    "result_alias."
)

CODE_LIST_DESCRIPTION = (
    "List persisted executable source only through its current authoritative owner. "
    "First-page mode requires one closed code-kind query and never mixes owner sequences. "
    "For pageable code kinds, page_size defaults to 20 and accepts only an integer from 1 "
    "through 50. A successful empty page has returned_count=0 and codes=[]; an error or "
    "missing structured result is not an empty page. ProductFamily program discovery may return rejections "
    "with family_version_alias, version_no, code=PRODUCT_FAMILY_CONTRACT_INVALID, field_path and reason. "
    "Rejected records cannot be executed or read as source; codes and returned_count describe only valid items. "
    "An empty valid page may still have rejections and next_cursor; continue with that cursor, which advances "
    "past the last scanned row. Technical failures are tool errors, not contract rejections. "
    "Cursor mode accepts only the unchanged "
    "opaque cursor with direct_session_alias and mode, and does not accept the first-page "
    "query again. Supported current owners are "
    "Data Transform, temporary SQL/Python Data QueryAttempts, server CAD measurement, "
    "Geometry authoring Operations, immutable Geometry Resource program versions, "
    "Geometry QueryAttempts, IFC QueryAttempts, ProductFamily authoring Operations, "
    "immutable ProductFamilyVersion realization programs, and current Geometry/Family drafts. "
    "geometry_draft and product_family_draft require draft_ref from Write or Info and return "
    "that draft's current source directory without pagination. Their locators freeze the current "
    "snapshot; if the draft changes, list again before reading. Fixed-asset drafts may have no source. "
    "geometry_resource_program lists published executable modeling_function versions. "
    "For editable fixed mapped_model build source, use geometry_draft. Authoring Operation "
    "source snapshots are read-only records and are not repair candidates. An empty "
    "geometry_resource_program page does not mean mapped-model build source was lost. "
    "Status filtering is available only for terminal Operation/Attempt kinds. Select the "
    "matching code_kind for an Attempt, Operation, ResourceVersion, or ProductFamilyVersion. "
    "Each result card contains an owner_alias for that owner and a separately issued "
    "code_locator; only code_locator is accepted by code_read. For a published Family, select "
    "code_kind=product_family_realization_program and match owner_alias to the exact Family version; "
    "use that card's locator and member directory rather than deriving a path from Family Info. "
    "Source discovery is a separate read and does not replace the Family's parameter or interface contract. "
    "Results also contain exact "
    "SHA facts, safe repair navigation, "
    "and bundle member directories where applicable. Reuse the listed member paths and "
    "SHA values to identify already-read exact versions; unchanged owner/version and "
    "member SHA do not require another full source read. result_available applies only to "
    "temporary Data Query snapshots; other owners return null and expose durable results "
    "through result_alias. repairable and repair_entry identify the current repair route and required "
    "owner/operation inputs. If repairable is false, do not invent a repair route; use the returned "
    "repair_unavailable_reason and the owning tool's current contract. "
    "Code cards and source reads never contain or recover authoring validation scenarios, samples, "
    "per-case results, or validation reports. Write edits a draft without cases; saved Preview "
    "Operation reports supply reusable cases and execution evidence. "
    "It does not scan naked Artifacts, "
    "create a Code resource, merge equal SHA values, or mutate owner facts. "
    'First-page example: {"direct_session_alias":"direct_001","mode":"first",'
    '"query":{"code_kind":"geometry_resource_program","page_size":20}}. '
    'Cursor example: {"direct_session_alias":"direct_001","mode":"cursor",'
    '"cursor":"<returned_next_cursor>"}. Copy the returned cursor rather than inventing one.'
)

CODE_READ_DESCRIPTION = (
    "Read exact persisted UTF-8 code selected by code_list. Copy code_locator and "
    "source_form from one returned card; owner_alias, Attempt aliases, Operation aliases, "
    "and result aliases are not code locators. The opaque locator navigates "
    "only: every call re-resolves and reauthorizes the original Operation, Package, "
    "ResourceVersion, ProductFamilyVersion, or QueryAttempt and rechecks its exact SHA "
    "and owner binding. ProductFamily authoring attempts never fall back to a published "
    "version asset, and published realization programs never fall back to an Operation "
    "Artifact. "
    "Reading source never reconstructs a complete business bundle or prior authoring test set and never "
    "authorizes reuse of an earlier validation outcome. "
    "single_text "
    "accepts no source_path; bundle requires one server-listed text member. view supports "
    "full, exact 1-based lines, or case-sensitive literal search. For several known members "
    "or owners, use direct_session_alias, mode=batch and reads containing 1-8 complete "
    "single_text/bundle selections (each with its own view, without session alias). "
    "Batch results preserve input order in reads, reauthorize every selection, and return "
    "all selections or an error, never a partial batch. Identical selections are rejected. "
    "The serialized batch response is bounded to 262144 UTF-8 bytes; on "
    "CODE_BATCH_BUDGET_EXCEEDED use fewer members or bounded lines/search views. "
    "CODE_SOURCE_READ_INVALID preserves the source-view rejection reason: full is limited "
    "to 65536 UTF-8 bytes, so read larger persisted source using lines/search with the same "
    "exact locator; do not resubmit its transform. Unknown owner failures remain "
    "CODE_OWNER_READ_FAILED and must not be treated as an expected view-budget rejection. "
    "Do not mix top-level single-read fields with mode=batch. Returned source_form determines "
    "the digest scope: source_sha256 is the selected text digest for single_text and the whole "
    "bundle digest for bundle; member_sha256 is the selected bundle member digest. Use the "
    "digest required by the target repair or revision contract. This tool accepts no "
    "ArtifactVersion ID, database ID, Storage path, host path, write, patch, or execution. "
    'Single-text example: {"direct_session_alias":"direct_001","source_form":"single_text",'
    '"code_locator":"<returned_code_locator>","view":{"view_mode":"full"}}. '
    'Bundle example: {"direct_session_alias":"direct_001","source_form":"bundle",'
    '"code_locator":"<returned_code_locator>","source_path":"main.py",'
    '"view":{"view_mode":"lines","start_line":1,"line_count":20}}. '
    'Batch example: {"direct_session_alias":"direct_001","mode":"batch","reads":['
    '{"source_form":"single_text","code_locator":"<returned_code_locator>",'
    '"view":{"view_mode":"search","text":"build","context_lines":2,"max_matches":5}}]}. '
    "Copy actual locators and bundle member paths from code_list."
)
