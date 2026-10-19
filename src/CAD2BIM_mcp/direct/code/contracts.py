"""Closed Provider contracts for owner-backed code discovery and reads."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, model_validator

from CAD2BIM_backend.application.code_discovery_service import (
    CodeMember,
    CodeKind,
    CodeListQuery,
    CodeReadSelection,
    CodeTerminalStatus,
    MAX_CODE_READ_BATCH_ITEMS,
    MAX_CODE_READ_BATCH_BYTES,
)
from CAD2BIM_mcp.direct.contract_base import DirectContract, NonNegativeInt64
from CAD2BIM_mcp.direct.ifc.product_family.contracts import IfcProductFamilyRejection
from CAD2BIM_mcp.direct.source_view import FullSourceView, LinesSourceView, SearchSourceView, to_text_source_view
from CAD2BIM_mcp.tool_docs import code as _tool_docs


_DIRECT_ALIAS = r"^direct_[0-9]{3,}$"
_TOKEN = r"^cur\..+$"


class _CodeListInput(DirectContract):
    direct_session_alias: str = Field(pattern=_DIRECT_ALIAS, max_length=32)


class DataTransformCodeQuery(DirectContract):
    code_kind: Literal["data_transform_attempt"]
    statuses: tuple[CodeTerminalStatus, ...] = Field(default=(), max_length=2)
    page_size: int = Field(default=20, strict=True, ge=1, le=50)


class DataQuerySqlAttemptCodeQuery(DirectContract):
    code_kind: Literal["data_query_sql_attempt"]
    statuses: tuple[CodeTerminalStatus, ...] = Field(default=(), max_length=2)
    page_size: int = Field(default=20, strict=True, ge=1, le=50)


class DataQueryPythonAttemptCodeQuery(DirectContract):
    code_kind: Literal["data_query_python_attempt"]
    statuses: tuple[CodeTerminalStatus, ...] = Field(default=(), max_length=2)
    page_size: int = Field(default=20, strict=True, ge=1, le=50)


class CadMeasurementCodeQuery(DirectContract):
    code_kind: Literal["cad_measurement_implementation"]
    statuses: tuple[CodeTerminalStatus, ...] = Field(default=(), max_length=2)
    page_size: int = Field(default=20, strict=True, ge=1, le=50)


class GeometryAuthoringCodeQuery(DirectContract):
    code_kind: Literal["geometry_authoring_attempt"]
    statuses: tuple[CodeTerminalStatus, ...] = Field(default=(), max_length=2)
    page_size: int = Field(default=20, strict=True, ge=1, le=50)


class GeometryResourceProgramQuery(DirectContract):
    code_kind: Literal["geometry_resource_program"]
    page_size: int = Field(default=20, strict=True, ge=1, le=50)


class GeometryQueryAttemptCodeQuery(DirectContract):
    code_kind: Literal["geometry_query_attempt"]
    statuses: tuple[CodeTerminalStatus, ...] = Field(default=(), max_length=2)
    page_size: int = Field(default=20, strict=True, ge=1, le=50)


class IfcQueryAttemptCodeQuery(DirectContract):
    code_kind: Literal["ifc_query_attempt"]
    statuses: tuple[CodeTerminalStatus, ...] = Field(default=(), max_length=2)
    page_size: int = Field(default=20, strict=True, ge=1, le=50)


class ProductFamilyAuthoringAttemptCodeQuery(DirectContract):
    code_kind: Literal["product_family_authoring_attempt"]
    statuses: tuple[CodeTerminalStatus, ...] = Field(default=(), max_length=2)
    page_size: int = Field(default=20, strict=True, ge=1, le=50)


class ProductFamilyRealizationProgramCodeQuery(DirectContract):
    code_kind: Literal["product_family_realization_program"]
    page_size: int = Field(default=20, strict=True, ge=1, le=50)


class GeometryDraftCodeQuery(DirectContract):
    code_kind: Literal["geometry_draft"]
    draft_ref: str = Field(min_length=1, max_length=16_384)


class FamilyDraftCodeQuery(DirectContract):
    code_kind: Literal["product_family_draft"]
    draft_ref: str = Field(min_length=1, max_length=16_384)


CodeFirstPageQuery: TypeAlias = Annotated[
    DataTransformCodeQuery
    | CadMeasurementCodeQuery
    | DataQuerySqlAttemptCodeQuery
    | DataQueryPythonAttemptCodeQuery
    | GeometryAuthoringCodeQuery
    | GeometryResourceProgramQuery
    | GeometryQueryAttemptCodeQuery
    | IfcQueryAttemptCodeQuery
    | ProductFamilyAuthoringAttemptCodeQuery
    | ProductFamilyRealizationProgramCodeQuery
    | GeometryDraftCodeQuery
    | FamilyDraftCodeQuery,
    Field(discriminator="code_kind"),
]


class CodeListFirstPageFields(DirectContract):
    mode: Literal["first"]
    query: CodeFirstPageQuery


class CodeListCursorFields(DirectContract):
    mode: Literal["cursor"]
    cursor: str = Field(pattern=_TOKEN, max_length=16_384)


class CodeListFirstPageInput(CodeListFirstPageFields, _CodeListInput):
    pass


class CodeListCursorInput(CodeListCursorFields, _CodeListInput):
    pass


DirectCodeListInput: TypeAlias = Annotated[
    CodeListFirstPageInput | CodeListCursorInput,
    Field(discriminator="mode"),
]


class DirectCodeCard(DirectContract):
    code_locator: str = Field(min_length=1, max_length=16_384)
    code_kind: CodeKind
    language: Literal["sql", "python"]
    source_form: Literal["single_text", "bundle"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    members: tuple[CodeMember, ...] = Field(default=(), max_length=64)
    owner_type: Literal[
        "authoring_draft",
        "critical_operation",
        "geometry_resource_version",
        "data_query_attempt",
        "geometry_query_attempt",
        "ifc_query_attempt",
        "product_family_version",
    ]
    owner_alias: str = Field(min_length=1, max_length=16_384)
    status: Literal["succeeded", "failed", "published", "draft"]
    diagnostic_code: str | None = Field(default=None, min_length=1, max_length=120)
    diagnostic_message: str | None = Field(default=None, min_length=1, max_length=2_000)
    parent_owner_alias: str | None = Field(default=None, min_length=1, max_length=16_384)
    ownership: Literal["agent", "server"]
    repairable: bool
    repair_entry: str | None = Field(default=None, min_length=1, max_length=120)
    repair_unavailable_reason: (
        Literal[
            "operation_succeeded",
            "owner_scope_read_only",
            "server_recovery_required",
        ]
        | None
    ) = None
    result_alias: str | None = Field(default=None, min_length=1, max_length=16_384)
    result_code_locator: str | None = Field(default=None, min_length=1, max_length=16_384)
    result_available: bool | None = Field(
        default=None,
        description=_tool_docs.CODE_RESULT_AVAILABLE_DESCRIPTION,
    )


class DirectCodeListResult(DirectContract):
    code_kind: CodeKind
    codes: tuple[DirectCodeCard, ...] = Field(max_length=50)
    rejections: tuple[IfcProductFamilyRejection, ...] = ()
    returned_count: int = Field(ge=0, le=50)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def validate_page(self) -> "DirectCodeListResult":
        if self.returned_count != len(self.codes):
            raise ValueError("returned_count must equal codes length")
        if any(item.code_kind is not self.code_kind for item in self.codes):
            raise ValueError("one code page cannot mix code kinds")
        return self


SourceViewInput: TypeAlias = Annotated[
    FullSourceView | LinesSourceView | SearchSourceView,
    Field(discriminator="view_mode"),
]


class SingleTextCodeReadSelection(DirectContract):
    source_form: Literal["single_text"]
    code_locator: str = Field(pattern=_TOKEN, max_length=16_384)
    view: SourceViewInput


class BundleCodeReadSelection(DirectContract):
    source_form: Literal["bundle"]
    code_locator: str = Field(pattern=_TOKEN, max_length=16_384)
    source_path: str = Field(min_length=1, max_length=255)
    view: SourceViewInput


CodeReadSelectionInput: TypeAlias = Annotated[
    SingleTextCodeReadSelection | BundleCodeReadSelection,
    Field(discriminator="source_form"),
]


class SingleTextCodeReadInput(SingleTextCodeReadSelection, _CodeListInput):
    pass


class BundleCodeReadInput(BundleCodeReadSelection, _CodeListInput):
    pass


class BatchCodeReadFields(DirectContract):
    mode: Literal["batch"]
    reads: tuple[CodeReadSelectionInput, ...] = Field(
        min_length=1, max_length=MAX_CODE_READ_BATCH_ITEMS
    )


class BatchCodeReadInput(BatchCodeReadFields, _CodeListInput):
    pass


DirectCodeReadInput: TypeAlias = SingleTextCodeReadInput | BundleCodeReadInput | BatchCodeReadInput


class DirectCodeReadResult(DirectContract):
    code_locator: str = Field(pattern=_TOKEN, max_length=16_384)
    code_kind: CodeKind
    language: Literal["sql", "python"]
    source_form: Literal["single_text", "bundle"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_path: str | None = Field(default=None, min_length=1, max_length=255)
    member_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    view_mode: Literal["full", "lines", "search"]
    total_lines: NonNegativeInt64
    text: str = Field(max_length=98_304)
    returned_start_line: int | None = Field(default=None, strict=True, ge=1)
    returned_line_count: NonNegativeInt64
    match_count: NonNegativeInt64
    truncated: bool


class DirectCodeReadBatchResult(DirectContract):
    mode: Literal["batch"] = "batch"
    reads: tuple[DirectCodeReadResult, ...] = Field(
        min_length=1, max_length=MAX_CODE_READ_BATCH_ITEMS
    )

    @model_validator(mode="after")
    def validate_response_budget(self) -> "DirectCodeReadBatchResult":
        if len(self.model_dump_json().encode("utf-8")) > MAX_CODE_READ_BATCH_BYTES:
            raise ValueError("code read batch exceeds its 262144-byte response budget")
        return self


DirectCodeReadResponse: TypeAlias = DirectCodeReadResult | DirectCodeReadBatchResult


CODE_LIST_INPUT_ADAPTER = TypeAdapter(DirectCodeListInput)
CODE_READ_INPUT_ADAPTER = TypeAdapter(DirectCodeReadInput)


def validate_code_list_input(value: object) -> DirectCodeListInput:
    return CODE_LIST_INPUT_ADAPTER.validate_python(value)


def validate_code_read_input(value: object) -> DirectCodeReadInput:
    return CODE_READ_INPUT_ADAPTER.validate_python(value)


def to_code_list_query(value: CodeFirstPageQuery) -> CodeListQuery:
    payload = value.model_dump(mode="json")
    return CodeListQuery.model_validate(payload)


def to_code_read_selection(value: CodeReadSelectionInput) -> CodeReadSelection:
    return CodeReadSelection(
        code_locator=value.code_locator,
        source_path=value.source_path if isinstance(value, BundleCodeReadSelection) else None,
        view=to_text_source_view(value.view),
    )


__all__ = [
    "BatchCodeReadFields",
    "BatchCodeReadInput",
    "BundleCodeReadSelection",
    "BundleCodeReadInput",
    "CODE_LIST_INPUT_ADAPTER",
    "CODE_READ_INPUT_ADAPTER",
    "CodeListCursorInput",
    "CodeListCursorFields",
    "CodeListFirstPageInput",
    "CodeListFirstPageFields",
    "DirectCodeListInput",
    "DirectCodeCard",
    "DirectCodeListResult",
    "DirectCodeReadInput",
    "DirectCodeReadBatchResult",
    "DirectCodeReadResponse",
    "DirectCodeReadResult",
    "SingleTextCodeReadInput",
    "validate_code_list_input",
    "validate_code_read_input",
    "to_code_read_selection",
]
