"""Runner Data tool inputs. Run authority is carried only by the server credential."""

from __future__ import annotations

from CAD2BIM_backend.domain.data_transform import (
    TransformMetadataSelection, TransformInlineSchemaSelection, TransformInlineRowsSelection,
    TransformParameterSelection, TransformInlineTextSelection,
)

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from CAD2BIM_backend.domain.data_query_python import DataQueryPythonQuery
from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlQuery
from CAD2BIM_backend.domain.data_transform import (
    DataTransformCommand, DuckDbSqlExecution, InlineTable, OutputPackageDeclaration,
    PythonArrowExecution, TransformInputBinding, TransformOutputDeclaration,
    TransformParameter, TransformSourceEdit,
    OutputPackageInput, OutputPackageUpdate, TransformOutputsSet, TransformOutputsChange,
    resolve_data_transform_create,
)
from CAD2BIM_backend.domain.data_json import JsonReadQuery
from CAD2BIM_backend.domain.text_source import SourceEdit, TextEdit
from CAD2BIM_mcp.direct.code.contracts import (
    BatchCodeReadFields, BundleCodeReadSelection, CodeListCursorFields,
    CodeListFirstPageFields, SingleTextCodeReadSelection,
)
from CAD2BIM_mcp.direct.transform.common_inputs import (
    OperationAliasFields, OperationInspectionFields, OperationListFields,
    OperationReferenceFields, OperationTransformDescriptionFields, OperationTransformSchemaFields,
)
from CAD2BIM_mcp.direct.query.cad_inputs import DataRowsToolQuery
from CAD2BIM_mcp.direct.operation.inputs import IfcProductsRecoveryListInput
from CAD2BIM_mcp.direct.transform.repair_inputs import (
    FailedOperationRepairSource, SucceededPackageRepairSource,
)


class RunnerDataInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackageListInput(RunnerDataInput):
    before_sequence: int | None = Field(default=None, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


class DescribePackageInput(RunnerDataInput):
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)
    payload_name: str | None = Field(default=None, min_length=1, max_length=120)


class PackageDescriptionInput(RunnerDataInput):
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)


class RowsInput(RunnerDataInput):
    query: DataRowsToolQuery | None = None
    cursor: str | None = Field(default=None, pattern=r"^cur_.+$", max_length=16_384)

    @model_validator(mode="after")
    def one_branch(self) -> "RowsInput":
        if (self.query is None) == (self.cursor is None):
            raise ValueError("provide exactly one query or cursor")
        return self


class SqlFreshInput(RunnerDataInput):
    mode: Literal["fresh"]
    query: DataQuerySqlQuery


class SqlRepairInput(RunnerDataInput):
    mode: Literal["repair"]
    parent_attempt_alias: str = Field(min_length=1, max_length=16_384)
    edits: tuple[TransformSourceEdit, ...] = Field(min_length=1, max_length=16)
    page_size: int = Field(default=100, ge=1, le=200)


class SqlCursorInput(RunnerDataInput):
    mode: Literal["cursor"]
    cursor: str = Field(pattern=r"^cur_.+$", max_length=16_384)


SqlInput: TypeAlias = Annotated[
    SqlFreshInput | SqlRepairInput | SqlCursorInput, Field(discriminator="mode")
]


class PythonFreshInput(RunnerDataInput):
    mode: Literal["fresh"]
    query: DataQueryPythonQuery


class PythonRepairInput(RunnerDataInput):
    mode: Literal["repair"]
    parent_attempt_alias: str = Field(min_length=1, max_length=16_384)
    edits: tuple[TransformSourceEdit, ...] = Field(min_length=1, max_length=16)
    page_size: int = Field(default=100, ge=1, le=200)


class PythonCursorInput(RunnerDataInput):
    mode: Literal["cursor"]
    cursor: str = Field(pattern=r"^cur_.+$", max_length=16_384)


PythonInput: TypeAlias = Annotated[
    PythonFreshInput | PythonRepairInput | PythonCursorInput, Field(discriminator="mode")
]


class JsonInput(RunnerDataInput):
    query: JsonReadQuery | None = None
    cursor: str | None = Field(default=None, pattern=r"^cur_.+$", max_length=16_384)

    @model_validator(mode="after")
    def one_branch(self) -> "JsonInput":
        if (self.query is None) == (self.cursor is None):
            raise ValueError("provide exactly one query or cursor")
        return self


class _TransformCreateInput(RunnerDataInput):
    mode: Literal["create"]
    inputs: tuple[TransformInputBinding, ...] = Field(min_length=1, max_length=16)
    outputs: TransformOutputsSet
    output_package: OutputPackageInput
    parameters: tuple[TransformParameter, ...] = Field(default=(), max_length=32)
    inline_tables: tuple[InlineTable, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_domain_contract(self) -> "TransformCreateInput":
        resolve_data_transform_create(self.model_dump(mode="python", exclude={"mode"}))
        return self


class TransformSqlCreateInput(_TransformCreateInput):
    execution: DuckDbSqlExecution


class TransformPythonCreateInput(_TransformCreateInput):
    execution: PythonArrowExecution


class TransformIfcCreateInput(TransformPythonCreateInput):
    ifc_source: Literal["current"]
    inputs: tuple[TransformInputBinding, ...] = Field(default=(), max_length=16)


TransformCreateInput: TypeAlias = TransformSqlCreateInput | TransformPythonCreateInput | TransformIfcCreateInput


class TransformRepairInput(RunnerDataInput):
    mode: Literal["repair"]
    source: Annotated[
        FailedOperationRepairSource | SucceededPackageRepairSource,
        Field(discriminator="source_kind"),
    ]
    edits: tuple[SourceEdit, ...] | None = Field(default=None, min_length=1, max_length=16)
    outputs: TransformOutputsChange | None = None
    output_package: OutputPackageUpdate | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "TransformRepairInput":
        if self.edits is None and self.outputs is None and self.output_package is None:
            raise ValueError("repair requires edits, outputs or output_package")
        if self.edits is not None:
            TextEdit(action="edit", edits=self.edits)
        return self


TransformInput: TypeAlias = TransformCreateInput | TransformRepairInput


CodeListInput: TypeAlias = Annotated[
    CodeListFirstPageFields | CodeListCursorFields, Field(discriminator="mode"),
]
CodeReadInput: TypeAlias = SingleTextCodeReadSelection | BundleCodeReadSelection | BatchCodeReadFields
class OperationCatalogListInput(OperationListFields):
    mode: Literal["catalog"] = "catalog"


OperationListInput: TypeAlias = OperationCatalogListInput | IfcProductsRecoveryListInput
from CAD2BIM_backend.domain.preview_report_read import PreviewCaseTextSelection, PreviewReportTextSelection

OperationGetInput: TypeAlias = (PreviewCaseTextSelection | PreviewReportTextSelection | OperationAliasFields | OperationReferenceFields | OperationInspectionFields
    | OperationTransformDescriptionFields | OperationTransformSchemaFields
    | TransformMetadataSelection | TransformInlineSchemaSelection | TransformInlineRowsSelection
    | TransformParameterSelection | TransformInlineTextSelection)


__all__ = [
    "PackageListInput", "DescribePackageInput", "PackageDescriptionInput", "RowsInput",
    "SqlInput", "PythonInput", "JsonInput", "TransformInput", "CodeListInput",
    "CodeReadInput", "OperationListInput", "OperationGetInput",
]
