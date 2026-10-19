"""Direct Data Catalog provider response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, StrictBool, field_validator, model_validator

from CAD2BIM_backend.domain.data_query import (
    DataDescribePackageResult as BackendDataDescribePackageResult,
    JsonPayloadSummary,
    PackageSummary,
    ParquetPayloadSummary,
    PayloadSchema,
)

from CAD2BIM_mcp.direct.contract_base import (
    _MAX_MARKDOWN_BYTES,
    DirectContract,
    NonNegativeInt64,
    PositiveInt64,
    SectionTitle,
    _non_blank_no_nul,
    _utc,
)


class ParquetDataPayloadCard(DirectContract):
    payload_name: str = Field(min_length=1, max_length=120)
    format: Literal["parquet"] = "parquet"
    row_count: NonNegativeInt64


class JsonDataPayloadCard(DirectContract):
    payload_name: str = Field(min_length=1, max_length=120)
    format: Literal["json"] = "json"
    size_bytes: NonNegativeInt64
    json_kind: Literal["document", "records"]


DataPayloadCard: TypeAlias = ParquetDataPayloadCard | JsonDataPayloadCard


class DirectJsonPayloadSummary(DirectContract):
    payload_name: str = Field(min_length=1, max_length=120)
    format: Literal["json"] = "json"
    size_bytes: PositiveInt64
    json_kind: Literal["document", "records"]


DirectPayloadSummary: TypeAlias = Annotated[
    ParquetPayloadSummary | DirectJsonPayloadSummary,
    Field(discriminator="format"),
]


class DirectDataDescribePackageResult(DirectContract):
    package: PackageSummary
    producing_operation_alias: str | None = Field(default=None, pattern=r"^op_[0-9]{3,}$", max_length=32)
    payloads: tuple[DirectPayloadSummary, ...]
    payload_schema: PayloadSchema | None = None

    @model_validator(mode="after")
    def validate_payloads(self) -> "DirectDataDescribePackageResult":
        BackendDataDescribePackageResult(
            package=self.package,
            producing_operation_alias=self.producing_operation_alias,
            payloads=tuple(
                JsonPayloadSummary(
                    payload_name=item.payload_name,
                    size_bytes=item.size_bytes,
                    json_kind=item.json_kind,
                )
                if isinstance(item, DirectJsonPayloadSummary)
                else item
                for item in self.payloads
            ),
            payload_schema=self.payload_schema,
        )
        return self


class _CatalogPackageCard(DirectContract):
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)
    producing_operation_alias: str | None = Field(default=None, pattern=r"^op_[0-9]{3,}$", max_length=32)
    sequence: PositiveInt64
    name: str = Field(min_length=1, max_length=255)
    created_at: datetime
    description_title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=1_000)
    section_titles: tuple[SectionTitle, ...] = Field(min_length=1, max_length=16)
    payloads: tuple[DataPayloadCard, ...] = Field(min_length=1, max_length=16)

    _created_at = field_validator("created_at")(_utc)
    _name = field_validator("name")(_non_blank_no_nul)
    _description_title = field_validator("description_title")(_non_blank_no_nul)
    _summary = field_validator("summary")(_non_blank_no_nul)

    @field_validator("section_titles")
    @classmethod
    def validate_sections(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("section_titles must be unique")
        for item in value:
            if len(item) > 160:
                raise ValueError("section title exceeds 160 characters")
            _non_blank_no_nul(item)
        return value

    @field_validator("payloads")
    @classmethod
    def validate_payloads(cls, value: tuple[DataPayloadCard, ...]) -> tuple[DataPayloadCard, ...]:
        names = [item.payload_name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("payload_name values must be unique")
        return value


class SourceL0CatalogPackageCard(_CatalogPackageCard):
    package_kind: Literal["source_l0"]
    source_filename: str = Field(min_length=1, max_length=255)

    _source_filename = field_validator("source_filename")(_non_blank_no_nul)


class DerivedCatalogPackageCard(_CatalogPackageCard):
    package_kind: Literal["derived"]
    producing_operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)


CatalogPackageCard: TypeAlias = SourceL0CatalogPackageCard | DerivedCatalogPackageCard


class DirectPackageListResult(DirectContract):
    packages: tuple[CatalogPackageCard, ...] = Field(max_length=50)
    complete: StrictBool
    next_before_sequence: PositiveInt64 | None

    @model_validator(mode="after")
    def validate_page(self) -> DirectPackageListResult:
        sequences = [item.sequence for item in self.packages]
        aliases = [item.package_alias for item in self.packages]
        if len(set(sequences)) != len(sequences) or len(set(aliases)) != len(aliases):
            raise ValueError("Package page aliases and sequences must be unique")
        if any(left <= right for left, right in zip(sequences, sequences[1:])):
            raise ValueError("Package page sequences must be strictly descending")
        if self.complete:
            if self.next_before_sequence is not None:
                raise ValueError("complete Package page must not have a continuation")
        else:
            if not sequences or self.next_before_sequence != min(sequences):
                raise ValueError("incomplete Package page must continue from its minimum sequence")
        return self


class PackageDescriptionResult(DirectContract):
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$")
    package_name: str = Field(min_length=1, max_length=255)
    markdown: str = Field(min_length=1, max_length=_MAX_MARKDOWN_BYTES)

    @field_validator("markdown")
    @classmethod
    def validate_markdown(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _MAX_MARKDOWN_BYTES:
            raise ValueError("markdown exceeds 65,536 UTF-8 bytes")
        return value


class DataPackagePrimaryResult(DirectContract):
    result_kind: Literal["data_package"]
    package_alias: str = Field(pattern=r"^data_[0-9]{3,}$", max_length=32)
    package_name: str = Field(min_length=1, max_length=255)

    _package_name = field_validator("package_name")(_non_blank_no_nul)
