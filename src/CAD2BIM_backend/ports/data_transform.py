"""Arrow boundary for the short-lived M2 DuckDB worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pyarrow as pa

from CAD2BIM_backend.ports.arrow_inputs import ArrowInput
from CAD2BIM_backend.domain.data_transform import (
    DataTransformWorkerReceipt,
    DataTransformWorkerRequest,
    PythonArrowWorkerRequest,
)
from CAD2BIM_backend.domain.data_query_python import PythonArrowQueryWorkerRequest


DUCKDB_LOGICAL_TYPE_METADATA_KEY = b"cad2bim.duckdb_logical_type"


# Compatibility name for existing Data Transform consumers. The DTO itself is
# owned by the entry-neutral Arrow boundary above.
ArrowTransformInput = ArrowInput


@dataclass(frozen=True, slots=True)
class ArrowTransformOutput:
    receipt: DataTransformWorkerReceipt
    schema: pa.Schema
    batches: tuple[pa.RecordBatch, ...]
    payload_name: str | None = None


class DataTransformWorkerPort(Protocol):
    async def execute(
        self,
        request: DataTransformWorkerRequest,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]: ...


class PythonArrowDataTransformWorkerPort(Protocol):
    async def execute(
        self,
        request: PythonArrowWorkerRequest,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]: ...


class IfcPythonArrowDataTransformWorkerPort(Protocol):
    async def execute_transform(
        self,
        request: PythonArrowWorkerRequest,
        inputs: tuple[ArrowInput, ...] = (),
        *,
        ifc_bytes: bytes,
        expected_ifc_sha256: str,
        deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]: ...


class PythonArrowQueryWorkerPort(Protocol):
    async def execute_query(
        self,
        request: PythonArrowQueryWorkerRequest,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> ArrowTransformOutput: ...


__all__ = [
    "ArrowTransformInput",
    "ArrowTransformOutput",
    "DataTransformWorkerPort",
    "DUCKDB_LOGICAL_TYPE_METADATA_KEY",
    "IfcPythonArrowDataTransformWorkerPort",
    "PythonArrowDataTransformWorkerPort",
    "PythonArrowQueryWorkerPort",
]
