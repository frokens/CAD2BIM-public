"""Provider-visible usage text for the operation capability family."""

from CAD2BIM_mcp.tool_docs.preview import SAVED_PREVIEW_OBSERVATION_USAGE

OPERATION_LIST_DESCRIPTION = (
    "Default mode=catalog lists terminal source_ingestion, data_transform, and ifc_model_write Operations, plus "
    "geometry_resource_write, product_family_write, authoring_publish and geometry_inspection Operations, visible in this Direct "
    "scope. Each selection card exposes stable sequence, kind, outcome, deterministic "
    "intent, time, operation_alias/operation_ref and a failed operation's error code. Use operation_get "
    "to select cases and obtain their saved geometry references for observation or Query without rebuilding. "
    "List reads terminal ledger summaries without parsing executable Family contracts or loading saved models. First page: omit "
    "before_sequence. Continuation: submit next_before_sequence unchanged; ordering is "
    "fixed by descending sequence. Optional operation_kind, tool_name and outcome filter before "
    "pagination; catalog mode has no cursor, OFFSET, page number or custom sort. "
    "For product provenance, filter tool_name=ifc_products_apply before paging. Intent text may be identical across Families; "
    "use each needed operation_get detail once, then its original input Package description/rows or exact Family info to verify the business Family. "
    "After a product write response is lost, mode=ifc_products_recovery with required tool_name=ifc_products_apply or ifc_products_edit discovers that tool's original records in this Run, "
    "including prepared and indeterminate without a Catalog alias. Supply tool_name, page_size and optional unchanged next_cursor as cursor; "
    "do not mix before_sequence or catalog filters. Each item gives operation_ref, known outcome and call times; no sequence is invented. "
    "Use operation_get mode=operation_reference to compare original Family/input. Prepared does not prove the worker is active, "
    "and an empty page or read failure does not prove zero commit. This is a read; only the original product tool's mode=recover may continue its saved action. "
    "Default page_size is 20, maximum 50. The response is capped at "
    "128 KiB and may return fewer cards with a continuation. Use operation_get for bounded "
    "detail and code_list/code_read for exact executable source. Hard timeout is 10 seconds. Results "
    "are execution ledger facts, not a Job queue or business decision. Geometry and ProductFamily "
    "authoring summaries can retain facts about retired contracts; that does not confer execution or resource recovery eligibility. "
    "operation_get checks the exact current source before recovering draft tokens, formal versions or saved Preview evidence. "
    "Recovery and reference rules: cad2bim://contracts/common/usage#references."
)

OPERATION_GET_DESCRIPTION = (
    "Data Transform details contain only paged metadata, never inline rows or parameter values. "
    "Each inputs/outputs/parameters/inline_tables page gives total_count, items, complete and next_index. "
    "Continue using mode=transform_input, input_kind=metadata, section and start_index=next_index; item_count is 1-100. "
    "Use input_kind=inline_schema with input_name, start_column/column_count (1-32) for actual column types; "
    "inline_rows uses input_name, zero-based start_row, row_count (1-200) and optional exact select columns. "
    "Rows return total_rows, the actual range, complete and next_row. If one row is too large, select fewer columns "
    "or use inline_text with input_name, row_index, column_name, zero-based start_character and character_count (1-16384). "
    "Text intervals count Unicode characters and return total_characters, actual returned_characters and next_character. "
    "Use input_kind=parameter with parameter_name for one exact typed original value. All reads use the same immutable "
    "Operation, including failed operations with no output Package, and never create a Package/Operation or substitute output rows. "
    "Metadata and values remain bounded to 64 KiB; pages may return fewer items because of actual encoded size. "
    "Normal detail includes a CodeDiscovery-issued code_locator when available in this Run; pass it unchanged to code_read. "
    "For one saved Data Transform input, select mode=transform_input, operation_alias, "
    "input_kind=package_description or output_schema plus payload_name, and a view with "
    "view_mode=full/lines/search. This returns only the selected original text and its identity, "
    "including inputs of failed terminal transforms. The response remains capped at 64 KiB; "
    "if framing is too large, choose a smaller lines/search view. "
    "Read one Operation: mode=operation (default) uses a terminal operation_alias from operation_list in the current Run; "
    "mode=operation_reference uses the accessible operation_ref from Info or another result; "
    "mode=inspection uses inspection_ref to find its original saved case. "
    "For ifc_products_apply, detail includes its authorized exact family_version_ref and original input Package alias, "
    "payload_name and logical_key_fields. The Family reference and original input identify the batch; IFC class alone does not identify a business Family. "
    "mode=operation_reference also reads prepared/indeterminate original records without inventing a Catalog alias, sequence or report. "
    "For ifc_products_edit, detail gives the original input_binding, base_revision_no and optional bounded saved diagnostics; complete=false navigates the original report checks. Success requires original report and committed revision; failed primary_result may be null and never has committed_revision. Unsettled has no summary or terminal error. "
    "operation.primary_result.summary gives whole-batch input_count and committed_count; selected READ items and report matches are not batch totals. "
    "Retain an unchanged Operation's references, input binding, report alias and summary for later dependent reads instead of re-reading the same detail. "
    "Success and rejected/technical failure can both retain a report Package "
    "and truthful execution summary. Apply reports products/components/interfaces/checks; Edit reports "
    "products/changes/checks and interfaces only when observations exist. Use primary_result.report_tables "
    "for the actual saved table directory. Succeeded Apply and Edit expose their original committed IFC result "
    "through the existing operation_ref. "
    "This is Operation provenance navigation; IFC model_info/query CURRENT reads the current revision and does not "
    "select an old revision through this reference. Input/report Package aliases belong to the original Run. "
    "Trusted test preparation appears as ifc_trusted_baseline_load provenance, with baseline_loaded and "
    "its original committed revision summary on success; code_available=false. This source name is not a callable public tool. "
    "Data Transform detail likewise returns each resolved input's logical_key_fields; these are read-only saved "
    "member facts and are not new data_derive request fields. Returns "
    "the original Operation identity and terminal outcome plus bounded source_ingestion, data_transform, IFC model-write, "
    "geometry-resource, product-family or geometry_inspection recovery and result detail; "
    "a failed branch retains its stable error code/phase/message and terminal outcome for the original "
    "request. For authoring_publish, a confirmed failed original remains failed; after correcting its "
    "cause, a new call may keep the same draft and optional Preview links. Prepared or unknown results "
    "must be reconciled through the original Operation first. It "
    "does not include SQL or Package description Markdown by default. It never exposes Artifact identifiers, data rows, "
    "Worker logs, Jobs or recovery internals. Use code_list/code_read for persisted Data, "
    "Geometry, or ProductFamily executable source when "
    "code_available=true. Operation detail preserves source and publication/recovery facts, but it "
    "does not imply business acceptance. A retained single Preview detail pairs its exact source "
    "and original request_json with its inspection_ref or failure diagnostic. A saved batch report "
    "pairs each case input, outcome and available inspection_ref; an interrupted batch can retain "
    "completed case evidence without passing the batch. Temporary Preview and observations of an "
    "already saved reference create no new solidification Operation. Write saves implementation content, "
    "not runtime case evidence. Saved Preview reports provide bounded case summaries with content sizes and navigation: select case_key for one case, "
    "or omit it and page with next_after_case_key as after_case_key. Default page_size=1, maximum 4; actual encoded size may reduce the page without skipping cases. "
    "Inspection selection already identifies one case and takes no paging fields. Publish detail links "
    "formal versions and optional Preview examples; these associations do not supply the formal models. "
    "Read complete content through mode=preview_report and the returned operation_ref: content_kind=case_request or case_outcome requires case_key; dependencies, coverage_gaps, cross_case_diagnostics, placement_input and observation_input take no case_key. start_character (default 0) and character_count (default 4096, maximum 16384) select Unicode code points. Follow next_character until complete; concatenate text before parsing JSON, including null. Totals describe the public referenced content before slicing. Requests/results remain paired; inherited cases do not inherit PASS. Response limit "
    "is 64 KiB and hard timeout is 10 seconds. The "
    "record is an execution fact, not approval of its result. OPERATION_NOT_FOUND does not establish "
    "that a published resource is missing; current recovery rules are in "
    "cad2bim://contracts/common/usage#references. "
    + SAVED_PREVIEW_OBSERVATION_USAGE
)

OPERATION_LIST_BEFORE_SEQUENCE_DESCRIPTION = (
    "Optional Operation sequence boundary. Omit or use null on the first page; "
    "then submit next_before_sequence unchanged."
)

OPERATION_LIST_PAGE_SIZE_DESCRIPTION = "Operation cards requested; default 20 and maximum 50."

PRODUCT_APPLY_BATCH_SUMMARY_DESCRIPTION = (
    "Execution counts for the entire original input batch, not the products selected by a later READ. "
    "input_count is the original row count and committed_count is the batch's committed product count. "
    "Component rows and a selected READ's items/report matches do not add to these counts."
)

IFC_APPLY_RECOVERY_LIST_INPUT_CURSOR_DESCRIPTION = (
    "Copy next_cursor unchanged to continue original Apply discovery in this Run."
)

PRODUCT_APPLY_COMMITTED_REVISION_REVISION_NO_DESCRIPTION = (
    "Revision number in the original Operation's model, bound by operation_ref; "
    "this is the revision that action committed, not necessarily CURRENT."
)

PRODUCT_APPLY_COMMITTED_REVISION_SIZE_BYTES_DESCRIPTION = (
    "Exact persisted file byte count for that committed revision, read from its "
    "immutable Artifact metadata without reserializing the IFC."
)

IFC_APPLY_RECOVERY_CARD_DESCRIPTION = (
    "Original Operation metadata, including records not yet in the Catalog."
)

OPERATION_LIST_OPERATION_KIND_DESCRIPTION = (
    "Optional exact kind filter applied before keyset pagination. Omit to list all "
    "supported terminal Operation kinds."
)

OPERATION_GET_OPERATION_ALIAS_DESCRIPTION = (
    "Exact terminal Operation alias from current operation_list or a current write, retained Preview or Publish result."
)

GEOMETRY_INSPECTION_REQUEST_JSON_DESCRIPTION = (
    "The complete original exact retained Preview request, encoded as JSON."
)

SOURCE_INGESTION_SOURCE_DETAIL_SIZE_BYTES_DESCRIPTION = (
    "Exact original DXF byte size. null means this retained failed Source Ingestion has no "
    "recorded original byte-size fact; otherwise the value is a positive integer."
)

RUNNER_OPERATION_LIST_DESCRIPTION = (
    "List visible terminal Operations with optional operation_kind, tool_name and outcome filters. "
    "Continue using next_before_sequence as before_sequence; page_size is 1-50. Cards show "
    "saved outcomes and a small Preview case/model index. Use operation_get for details; "
    "saved inspection_ref supports observation or Query without rebuilding."
)

RUNNER_OPERATION_GET_DESCRIPTION = (
    "Data Transform details use paged inputs/outputs/parameters/inline_tables metadata and an exact code_locator, "
    "without inline rows or full parameter values. Continue each list with mode=transform_input, input_kind=metadata, "
    "section and start_index=next_index, item_count 1-100. Use inline_schema(input_name,start_column,column_count), "
    "inline_rows(input_name,start_row,row_count,select), parameter(parameter_name), or "
    "inline_text(input_name,row_index,column_name,start_character,character_count) for original saved values. "
    "Indices are zero-based physical rows/columns or Unicode characters, not logical key filters. Each read returns "
    "actual range/count, total and complete/next position; follow the returned position under the unchanged 64 KiB cap. "
    "Large rows require narrower columns or inline_text; these reads also work on failed transforms without output, "
    "and create no Package/Operation. code_read consumes the issued code_locator unchanged. "
    "For one saved Data Transform description or JSON Schema, use mode=transform_input with "
    "operation_alias, input_kind=package_description or output_schema plus payload_name, and "
    "view full/lines/search. This returns only the selected original text, including failed transforms; "
    "choose a smaller view if JSON framing exceeds the 64 KiB response budget. "
    "Read a visible terminal Operation: mode=operation (default) takes its current-Run operation_alias; "
    "mode=operation_reference takes operation_ref from Info or a result; mode=inspection takes "
    "inspection_ref and finds the original saved case. The first two modes accept case_key or "
    "after_case_key/page_size (1-4); inspection mode has no paging fields. Preview detail is a bounded summary; mode=preview_report reads the original case_request/case_outcome (with case_key), or dependencies/coverage_gaps/cross_case_diagnostics/placement_input/observation_input (without case_key). Use operation_ref and start_character/character_count, follow next_character, and concatenate Unicode text before parsing JSON. Totals and the 64 KiB cap include public reference projection and JSON escaping. Content pairs saved inputs "
    "with actual results, including partial failed cases. "
    "For saved ifc_products_apply, detail gives authorized exact Family reference and original input Package "
    "alias/payload/logical keys; succeeded and failed records can both retain their diagnostic report Package "
    "and execution summary. A succeeded card's existing operation_ref navigates that original exact IFC commit "
    "through operation_get; it does not select historical revisions in IFC CURRENT queries. Package aliases "
    "belong to the original Run. Data Transform input logical_key_fields are read-only resolved member facts. "
    "Publish returns formal versions and optional Preview example links, which do not supply formal models. Reads never rebuild. Missing, invisible "
    "or inconsistent records fail; responses over 64 KiB return OPERATION_GET_RESPONSE_TOO_LARGE. "
    + SAVED_PREVIEW_OBSERVATION_USAGE
)
