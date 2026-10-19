"""Provider-visible usage text shared across Direct capability families."""

from CAD2BIM_backend.domain.runtime_execution import RuntimeExecutionPolicy, DEFAULT_MAX_WAITING_REQUESTS

IFC_INSTALLATION_EVIDENCE_GUIDANCE = (
    "Input conditions define a feasible domain. A finite_segment permits an interface station "
    "within its endpoints; it neither selects that station nor assigns the whole interval to material. "
    "Geometry supplies numerical interfaces; Family transforms yield the selected world positions, "
    "axes and profiles. Constraint evaluation compares these outputs with declared internal relations "
    "and this invocation's external conditions. Completed evaluation may be PASS or FAIL. Construction "
    "and commit affect only requested products. For installation, establish source associations, read "
    "affected CURRENT objects and compare the required relationships; explicitly coordinate pending "
    "inputs or edit saved objects as dependencies require, then recheck affected saved relationships. "
    "Preserve units, world coordinates, direction, section and source paths. Reports remain historical "
    "after edits, especially placement. Named numerical interfaces, physical Body end faces and actual "
    "IfcPort/connection relations respectively describe solved references, saved material and network topology. "
    "Use supplied source-relation/path tables to identify peers before geometric candidate search; join "
    "source keys to the actual Apply GUID mapping. Persist source relations, permitted windows, solved "
    "interfaces, current observations and path plans with their own evidence identities. Distinguish "
    "candidates, confirmed relations, ambiguity and unchecked sources. Include internal occupancy and "
    "all sides of shared devices; refresh only facts affected by a later edit."
)

COMMON_TYPES_RESOURCE_URI = "cad2bim://contracts/common/types"
COMMON_USAGE_RESOURCE_URI = "cad2bim://contracts/common/usage"
COMMON_PREVIEW_TYPES_RESOURCE_URI = "cad2bim://contracts/common/preview_types"
COMMON_AUTHORING_RESOURCE_URI = "cad2bim://contracts/common/authoring"
COMMON_PREVIEW_RESOURCE_URI = "cad2bim://contracts/common/preview"
COMMON_TYPES_RESOURCE_DESCRIPTION = (
    "Shared authoring/program Schema types. Resolve referenced definitions with the calling "
    "document's root constraints; reuse a complete, still-applicable session copy."
)
COMMON_PREVIEW_TYPES_RESOURCE_DESCRIPTION = (
    "Shared Preview case and rendering input Schema types; some definitions reference common/types. "
    "Read only the pages named by the chosen contract's $ref; keep its root constraints."
)
COMMON_TYPES_NAVIGATION = (
    " Resolve external $ref using the named registered resource; reuse an already-read applicable copy."
)
COMMON_USAGE_RESOURCE_DESCRIPTION = (
    "Reference/Scope recovery, usage writing and support boundaries. Sections are labels within "
    "this resource, not separately readable pages."
)
COMMON_AUTHORING_RESOURCE_DESCRIPTION = (
    "Draft/Preview/Publish workflow: representative case selection, execution reuse and formal "
    "publication. Domain-specific request examples live in the two Preview tutorial resources."
)
COMMON_PREVIEW_RESOURCE_DESCRIPTION = (
    "Observation guide: cameras, interface overlays, material sections, reference layers and result "
    "interpretation. Read when selecting views or assessing geometry; not needed just to save source."
)
REFERENCE_USAGE_NAVIGATION = (
    " Copy server-issued references unchanged from current-scope list/info/write results; "
    "use the reference kind required by the selected draft, published or inspection branch. Reference kinds, Scope, restart and invalid-reference "
    "recovery: cad2bim://contracts/common/usage#references."
)
VERSION_USAGE_INSTRUCTIONS_NAVIGATION = (
    " Write draft-owned usage_instructions using cad2bim://contracts/common/usage#version_usage; "
    "read it when authoring or changing the text, or reuse a complete still-applicable session copy. "
)
PRODUCT_SUPPORT_SCOPE_NAVIGATION = (
    " Before setting or revising parameter bounds, read "
    "cad2bim://contracts/common/usage#support_scope for evidence, support and unverified-scope rules. "
)

AUTHORING_DRAFT_REF_DESCRIPTION = (
    "Current editable draft reference returned by Write or draft Info; copy unchanged. "
    "It is neither a published version nor a retained geometry inspection."
)
AUTHORING_EXPECTED_EDIT_TOKEN_DESCRIPTION = (
    "Positive integer edit token from the latest Write or draft Info. Optimistic concurrency check, "
    "not a published version number or SHA. On conflict read the current draft before updating."
)
AUTHORING_EDIT_TOKEN_DESCRIPTION = "Current positive integer edit token; copy to expected_edit_token for the next update or publication."
AUTHORING_USAGE_INSTRUCTIONS_DESCRIPTION = (
    "Author-maintained usage: action=set with value replaces the text; action=edit with edits changes literal fragments. "
    "Omit on update or published-source creation to inherit; explicit null is invalid. "
    "Check inherited text after changing parameters, interfaces or supported behavior. "
    "Writing guidance: cad2bim://contracts/common/usage#version_usage."
)
AUTHORING_PREVIEW_EXAMPLES_DESCRIPTION = (
    "Optional saved Preview Operation aliases and case_keys; empty case_keys selects all report cases. "
    "These examples provide observation/input navigation, not formal model bytes or a passing-test "
    "requirement. Sources must match this implementation and remain accessible. Omit when unnecessary."
)


AUTHORING_WORKFLOW_GUIDANCE = (
    "Write saves an editable draft after structural checks; it does not execute source or publish. "
    "Preview accepts drafts and published versions. single observes one input; cases compares finite "
    "named inputs against declared expectations. Rendering and retain are independent. retain=true "
    "saves the report and successful geometry under one Operation; false is temporary. Returned "
    "inspection_ref values support Geometry Preview images/sections and Query measurements without "
    "solving again, before or after publication. A draft edit does not alter a saved model. "
    "Normal returned references can be used directly. Use operation_list/get when a result was lost "
    "or interrupted; do not add a recovery call after every success."
)
AUTHORING_REUSE_GUIDANCE = (
    "from_operation inherits case inputs and expectations, clearing old reuse fields. To reuse a saved "
    "result without repeating its inputs, add {change_kind:'reuse',case_key:...} as an item in "
    "changes. Reuse applies only to the marked case: inherited cases not deleted or marked for reuse "
    "execute again. To reuse only one saved case, mark it for reuse and delete the other unneeded cases. "
    "Inline or replaced cases can use case.reuse "
    "to select a saved case or inspection. In either form, "
    "its source, inputs (including provenance), bindings and relevant dependencies must still match. "
    "Keep unchanged source identities stable; relabeling them is not a new geometric check. An incompatible reuse "
    "is rejected, not silently rebuilt. Reuse unchanged case definitions even when their results need "
    "rechecking. Display names, ordinary usage text or a changed edit token alone do not require a "
    "rebuild; keep inherited usage accurate after behavior changes. "
    "A retained single has expectation_check=not_checked. To check a requirement, submit cases "
    "with the same execution inputs, an expectation derived from that requirement and explicit reuse. "
    "This is optional checking, not a publication prerequisite. Do not choose an expectation just to pass. "
    "A completed business rejection can be reused and compared with an expectation, but has no model "
    "to query or render. Technical failures and unrun cases are not reusable execution results. "
    "Geometry and Family examples are in their respective Preview tutorial resources."
)
AUTHORING_PUBLICATION_GUIDANCE = (
    "Publish takes root_kind, draft_ref and the current expected_edit_token from Write/Info. It "
    "constructs fixed Geometry from its definition and publishes parametric Geometry as source plus "
    "parameter contract, without requiring a sample model. It never extracts formal assets from Preview. "
    "Optional preview_examples {operation_alias,case_keys} link matching saved instances for observation "
    "and input lookup; they do not determine the formal model or require passing expectations. "
    "Published versions are immutable; changed drafts need another Publish, while unchanged content "
    "reuses its version. Publication is not business acceptance or IFC installation. Recover an "
    "uncertain or interrupted call through its original Operation before repeating. A confirmed failed "
    "Publish stays failed under its original request identity; after fixing the actual cause, a new call "
    "may use the same draft, edit token and optional Preview links. Do not change inputs merely to bypass "
    "duplicate handling."
)
FAMILY_PUBLICATION_GUIDANCE = (
    "Family publication fixes all declared Geometry bindings, including unselected binding_options. "
    "An exact published binding stays fixed even if that Geometry has an edited draft. A draft "
    "binding freezes its current content: unchanged already-published content reuses that version; "
    "new fixed dependencies are built once and parametric dependencies are published as programs. "
    "When family_contract.standard_model is declared, Publish uses its product parameters and contract defaults at "
    "local origin zero in metres, without external conditions. Supply required values without defaults "
    "in that definition. Preview nominal_scenario_key selects a checking case, not formal product inputs. "
    "Without family_contract.standard_model no formal standard BREP is required. A required build failure publishes "
    "no partial closure. Preview coverage and shape checks remain separate business facts."
)
AUTHORING_PUBLISH_USAGE = (
    "Publish a Geometry or Family draft from its definition. Use current draft_ref/edit_token from "
    "Write/Info. Builds fixed Geometry and declared Family standard models; parametric Geometry "
    "publishes source and contract without a required sample. Family includes its draft Geometry dependencies. "
    "preview_examples optionally links matching saved instances; no Preview is required to publish. "
    "Published versions are immutable; subsequent draft changes require another publication. "
    "Preview and Query do not require publication. Recover uncertain outcomes through the original Operation. "
    "After a confirmed failed Publish, fix its cause before a new call; unchanged business inputs and "
    "optional Preview links do not need alteration. "
)
AUTHORING_PUBLISH_DESCRIPTION = AUTHORING_PUBLISH_USAGE + (
    "Definition, instance reuse and publication rules: cad2bim://contracts/common/authoring. "
    "Complete requests: cad2bim://contracts/ifc_geometry_preview/tutorial or "
    "cad2bim://contracts/ifc_product_realization_preview/tutorial."
)


def common_usage_document() -> dict[str, str]:
    """Reference handling and resource-authored usage, without execution tutorials."""
    return {
        "references": REFERENCE_USAGE_DESCRIPTION,
        "version_usage": VERSION_USAGE_INSTRUCTIONS_AUTHORING_GUIDE,
        "support_scope": PRODUCT_SUPPORT_SCOPE_PLANNING_DESCRIPTION,
    }


def common_authoring_document() -> dict[str, str]:
    from CAD2BIM_mcp.tool_docs.preview import FINITE_VALIDATION_GUIDANCE
    return {
        "workflow": AUTHORING_WORKFLOW_GUIDANCE,
        "case_selection": FINITE_VALIDATION_GUIDANCE,
        "reuse": AUTHORING_REUSE_GUIDANCE,
        "publication": AUTHORING_PUBLICATION_GUIDANCE,
        "family_publication": FAMILY_PUBLICATION_GUIDANCE,
    }


def common_preview_document() -> dict[str, str]:
    from CAD2BIM_mcp.tool_docs.preview import (
        GEOMETRY_PREVIEW_PROFILE_USAGE, GEOMETRY_PREVIEW_PROFILE_EXAMPLES,
        GEOMETRY_INSPECTION_COMPLETION_GUIDANCE, GEOMETRY_PREVIEW_REFERENCES_DESCRIPTION,
        PREVIEW_RESULT_GUIDANCE, SAVED_PREVIEW_OBSERVATION_USAGE,
    )
    return {
        "preview_profile": GEOMETRY_PREVIEW_PROFILE_USAGE + GEOMETRY_PREVIEW_PROFILE_EXAMPLES,
        "geometry_inspection": GEOMETRY_INSPECTION_COMPLETION_GUIDANCE + " " + SAVED_PREVIEW_OBSERVATION_USAGE,
        "reference_geometries": GEOMETRY_PREVIEW_REFERENCES_DESCRIPTION,
        "preview_results": PREVIEW_RESULT_GUIDANCE,
    }


def runtime_budget_description(policy: RuntimeExecutionPolicy, pool: str) -> str:
    """Describe the same policy used by Application and worker admission."""
    return (
        f" Admission uses the {pool} pool: up to {policy.queue_seconds:g}s queued, "
        f"{policy.execution_seconds:g}s for the execution chain, and {policy.total_seconds:g}s "
        f"total with {policy.terminal_seconds:g}s reserved for publication/terminal facts and "
        f"{policy.cleanup_seconds:g}s for cleanup. Queues are FIFO and bounded to {DEFAULT_MAX_WAITING_REQUESTS} waiting "
        "requests per pool. Queue rejection is not an input or geometry validation failure; "
        "diagnostics distinguish queue_full, queue_timeout, capacity_unavailable, and work "
        "already started. Follow retryable and confirm an uncertain write through its existing "
        "Operation before repeating it."
    )

REFERENCE_USAGE_DESCRIPTION = (
    " Reference fields for Geometry, ProductFamily, their source locators and Preview/Query "
    "targets return server-issued short typed handles (for example gv_ plus 22 opaque "
    "characters). Copy the entire returned value unchanged; do not construct, shorten or "
    "repair it. Root handles gr_/fr_ are for discovery; published branches require an exact gv_/fv_ "
    "version handle from Catalog or Publish. Write returns draft_ref and edit_token for draft "
    "Info, update and Preview; publication is not required for draft Preview. Use sc_ for code_read, "
    "gi_ for retained Geometry Inspection results, gq_ for Geometry Query attempts and or_ for "
    "accessible original Operations across Runs of the same Task. A handle "
    "keeps the same scoped identity after restart but does not grant access outside the current "
    "scope; a new immutable version has a new handle. On an invalid reference, discard that "
    "value and copy a newly discovered current-scope reference from its catalog or Operation "
    "result; do not retry the known-invalid spelling. Page cursors keep their "
    "separate unchanged opaque format. "
    "Operation recovery accepts current-contract records selected from operation_list or returned by "
    "current tools. Historical execution-style Write aliases are outside this recovery surface; "
    "For a saved products Apply, operation_get pairs the exact Family reference and original input Package "
    "payload/logical keys with its retained report and execution summary, including a failed report. Only a "
    "succeeded Apply commits products. Its existing operation_ref identifies that Operation's exact IFC commit; "
    "it does not select a historical IFC revision in CURRENT model or query tools. "
    "OPERATION_NOT_FOUND alone does not mean their published Geometry or Family is unavailable. "
    "Find an existing published implementation through its current catalog, then read its exact Info "
    "and source; do not enumerate old operation aliases to reconstruct validation cases."
    " OUTPUT_REFERENCE_PROJECTION_FAILED with phase=output_validation means the tool already "
    "returned a result but its response references could not be prepared. Preserve the original "
    "request and inspect the returned operation_alias before any new write; the error does not "
    "mean the business write failed."
)

MULTIMODAL_RESULT_GUIDE_DESCRIPTION = (
    "Result reading guide: core fields, outcomes and next actions, not a full output Schema "
    "or an execution result. Read only when the actual response needs explanation. "
    "Shared execution, retention and image semantics are in cad2bim://contracts/common/preview."
)
MULTIMODAL_RESULT_USAGE_DESCRIPTION = (
    " Result JSON is the first TextContent block; inline images and resource links "
    "follow when available. No structuredContent mirror is returned. The JSON result "
    "can normally be read directly; optional field guidance is at "
    "cad2bim://contracts/{tool_name}/result."
)

PRODUCT_SUPPORT_SCOPE_PLANNING_DESCRIPTION = (
    "The Schema is authoritative for typed bounds. usage_instructions records coupled conditions, exact "
    "dependency assumptions, and declared support versus verified cases and unknown inputs. Examples "
    "alone do not define the supported range; preferred dimensions are not necessary feasibility "
    "conditions. A bounded search miss or numerical capacity limit does not prove infeasibility."
    " Keep hard constraints, design preferences and solved variables distinct. SOLVED does not itself "
    "verify reasonable spatial occupation. When compactness is required, assess it within actual shape, "
    "port, wall/channel, transition and stated engineering limits; a finite segment is an allowed "
    "connection window, not a length to consume."
)

VERSION_USAGE_INSTRUCTIONS_AUTHORING_GUIDE = (
    "usage_instructions is model-owned Markdown, editable in a draft and frozen on publication; read it "
    "through draft or exact version Info. Include caller-relevant assumptions, local anchors, interface "
    "meanings, support limits and allowed representations, their declared triggers and evidence limits. "
    "Distinguish hard constraints, design preferences and solved variables; describe any requested "
    "compactness objective, engineering limits and verified scope. "
    "Keep Geometry-local and Family-assembly conditions with their respective owner; do not repeat Schema "
    "fields, bindings, source algorithms, tool workflows or development history. Omission on update or "
    "published-source creation inherits existing valid text, never regenerates it. Inline creation or "
    "missing inherited usage requires action=set,value with the full text. Update accepts "
    "action=set,value or action=edit,edits: old_text must match once in the original, edits cannot "
    "overlap, empty new_text deletes, and keeping the anchor adds adjacent text. Review inherited usage "
    "when behavior or dependencies change. Geometric interfaces are not IFC distribution ports; placement "
    "does not inspect or modify external objects or enable an unavailable Apply. The body must be "
    "nonblank UTF-8 without NUL, at most 8192 Unicode characters and 32768 UTF-8 bytes; Markdown newlines "
    "are allowed and text is never truncated."
)

VERSION_USAGE_INSTRUCTIONS_CREATE_DESCRIPTION = (
    "Required model-owned Markdown describing inputs, use and limits; frozen on publication. Preserved exactly; "
    "nonblank UTF-8 without NUL, at most 8192 characters and 32768 bytes."
)

VERSION_USAGE_INSTRUCTIONS_MODIFY_DESCRIPTION = (
    "Optional action=set,value or action=edit,edits; omission inherits the current draft. The resulting "
    "draft must have nonblank usage instructions. A usage-only update is valid; a total "
    "no-op is not. Describe this model's inputs, use and limits within 8192 characters/32768 UTF-8 bytes."
)

VERSION_USAGE_INSTRUCTIONS_INFO_DESCRIPTION = (
    "Complete usage instructions for this exact immutable version. Read with its machine contract and "
    "exact dependencies for input meanings, units, coordinates, interfaces and applicable limitations. "
    "The machine contract remains authoritative for typed bounds; do not infer missing inputs or operations."
)

DIRECT_SESSION_ALIAS_DESCRIPTION = (
    "Alias returned by runtime_open_direct_session for this Direct scope."
)
DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION = "Alias returned by runtime_open_direct_session."
DIRECT_SESSION_TARGET_RUN_ALIAS_DESCRIPTION = (
    "Alias returned by runtime_open_direct_session for the target Run."
)
DIRECT_SESSION_TASK_SCOPE_ALIAS_DESCRIPTION = (
    "Alias returned by runtime_open_direct_session for this Direct TASK scope."
)
READ_BATCH_DESCRIPTION = (
    " Multiple independent READ calls may be submitted together. Do not submit a WRITE in the "
    "same batch, and wait for the returned result before making a dependent call."
)
