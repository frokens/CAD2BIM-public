from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pyarrow as pa
import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactService, ArtifactStream
from CAD2BIM_backend.application.code_discovery_service import CodeDiscoveryService
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.data_query_service import DataQueryService
from CAD2BIM_backend.application.data_transform_service import (
    DataTransformApplicationError,
    DataTransformService,
    _request_fingerprint,
)
from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.data_runtime import (
    CatalogResourceKind,
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationStatus,
    DataPackageKind,
    DataTransformOperationDetail,
    PackageMemberKind,
    ValidatedSource,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.data_query import DataDescribePackageQuery
from CAD2BIM_backend.domain.data_transform import (
    BooleanListParameter,
    BooleanParameter,
    DataRepairTransformCommand,
    DataTransformCommand,
    DataTransformWorkerError,
    DataTransformWorkerPolicy,
    DataTransformWorkerReceipt,
    DuckDbSqlExecution,
    InlineColumn,
    InlineIntegerValue,
    InlineRow,
    InlineStringValue,
    InlineTable,
    IntegerListParameter,
    IntegerParameter,
    NumberListParameter,
    NumberParameter,
    OutputPackageDeclaration,
    ParquetOutputDeclaration,
    JsonOutputDeclaration,
    PythonArrowExecution,
    PythonArrowWorkerPolicy,
    StringListParameter,
    StringParameter,
    TransformInputBinding,
)
from CAD2BIM_backend.domain.text_source import SourceEdit as TransformSourceEdit, TextSourceView
from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory import (
    data_runtime_repository as memory_data_runtime_repository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import (
    InMemoryIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.memory.state import MemoryState
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.infrastructure.data_transform.duckdb_worker import (
    SubprocessDuckDBDataTransformWorker,
)
from CAD2BIM_backend.infrastructure.data_transform.python_arrow_worker import (
    SubprocessPythonArrowDataTransformWorker,
)
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.ports.data_transform import (
    ArrowTransformInput,
    ArrowTransformOutput,
)
from CAD2BIM_mcp.direct.transform import application as direct_transform_application
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.code.contracts import CodeListFirstPageInput, SingleTextCodeReadInput
from CAD2BIM_mcp.direct.errors import DirectApplicationError


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "cad"

DESCRIPTION = """# Selected CAD facts
## Summary
Publishes deterministic candidate rows from exact bound CAD facts.
## Intended use
Support later data investigation.
## Contents
One selected_rows Parquet payload.
## Method
Apply one parameterized DuckDB SELECT to the declared inputs.
## Scope and completeness
The output covers every row selected by this exact request.
## Known limitations
The rows are facts and candidates, not approved IFC decisions.
"""


def _named_worker_output(
    table: pa.Table,
    payload_name: str,
) -> ArrowTransformOutput:
    return ArrowTransformOutput(
        receipt=DataTransformWorkerReceipt(
            row_count=table.num_rows,
            arrow_nbytes=table.nbytes,
            execution_ms=1,
        ),
        schema=table.schema,
        batches=tuple(table.to_batches()),
        payload_name=payload_name,
    )


def _worker_output(
    table: pa.Table,
    payload_name: str = "selected_rows",
) -> tuple[ArrowTransformOutput, ...]:
    return (_named_worker_output(table, payload_name),)


class FakeWorkerPort:
    def __init__(
        self,
        output: pa.Table | None = None,
        *,
        failure: DataTransformWorkerError | None = None,
    ) -> None:
        self.output = output or pa.table(
            {
                "candidate_key": pa.array(["candidate-1", "candidate-2"]),
                "source": pa.array(["entities", "geometry"]),
            }
        )
        self.failure = failure
        self.requests = []
        self.input_tables: list[dict[str, pa.Table]] = []
        self.input_descriptors: list[dict[str, tuple[str, str | None]]] = []

    async def execute(
        self,
        request,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        self.requests.append(request)
        self.input_descriptors.append(
            {item.input_name: (item.input_format, item.json_kind) for item in inputs}
        )
        self.input_tables.append({item.input_name: item.reader.read_all() for item in inputs})
        if self.failure is not None:
            raise self.failure
        return _worker_output(self.output)


class MultiOutputWorkerPort(FakeWorkerPort):
    def __init__(self, outputs: tuple[tuple[str, pa.Table], ...]) -> None:
        super().__init__()
        self.outputs = outputs

    async def execute(
        self,
        request,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        self.requests.append(request)
        self.input_tables.append({item.input_name: item.reader.read_all() for item in inputs})
        return tuple(
            _named_worker_output(table, payload_name) for payload_name, table in self.outputs
        )


class BlockingWorkerPort(FakeWorkerPort):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        request,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        self.requests.append(request)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            raise DataTransformWorkerError(
                "TRANSFORM_TIMEOUT",
                "The fake Worker was cancelled after execution started",
                phase="execution",
                execution_started=True,
            ) from None
        return _worker_output(self.output)


class CleanupBlockingWorkerPort(BlockingWorkerPort):
    def __init__(self, cleanup_seconds: float) -> None:
        super().__init__()
        self.cleanup_seconds = cleanup_seconds

    async def execute(
        self,
        request,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        self.requests.append(request)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(self.cleanup_seconds)
            raise DataTransformWorkerError(
                "TRANSFORM_TIMEOUT",
                "The fake Worker was cancelled after execution started",
                phase="execution",
                execution_started=True,
            ) from None
        return _worker_output(self.output)


class RacingWorkerPort(FakeWorkerPort):
    """One identical call fails while the other publishes success first."""

    def __init__(self) -> None:
        super().__init__()
        self.both_started = asyncio.Event()
        self.call_count = 0

    async def execute(
        self,
        request,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        ordinal = self.call_count
        self.call_count += 1
        self.requests.append(request)
        if self.call_count == 2:
            self.both_started.set()
        await self.both_started.wait()
        if ordinal == 0:
            raise DataTransformWorkerError(
                "TRANSFORM_EXECUTION_FAILED",
                "The losing concurrent execution failed",
                phase="execution",
                execution_started=True,
            )
        return _worker_output(self.output)


class DistinctFailureWorkerPort(FakeWorkerPort):
    def __init__(self) -> None:
        super().__init__()
        self.both_started = asyncio.Event()
        self.call_count = 0

    async def execute(
        self,
        request,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        ordinal = self.call_count
        self.call_count += 1
        if self.call_count == 2:
            self.both_started.set()
        await self.both_started.wait()
        code = "TRANSFORM_EXECUTION_FAILED" if ordinal == 0 else "TRANSFORM_RESOURCE_LIMIT"
        raise DataTransformWorkerError(
            code,
            f"concurrent failure {ordinal}",
            phase="execution",
            execution_started=True,
        )


class VolatileWorkerPort(FakeWorkerPort):
    """Return different valid bytes for each identical request execution."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def execute(
        self,
        request,
        inputs: tuple[ArrowTransformInput, ...],
        *, deadline: float | None = None,
    ) -> tuple[ArrowTransformOutput, ...]:
        self.requests.append(request)
        self.call_count += 1
        return _worker_output(
            pa.table(
                {
                    "candidate_key": [f"candidate-{self.call_count}"],
                    "source": ["volatile"],
                }
            )
        )


@dataclass(frozen=True, slots=True)
class TransformRuntime:
    state: MemoryState
    repository: InMemoryControlPlaneRepository
    artifacts: ArtifactService
    application: DataRuntimeService
    service: DataTransformService
    facade: DirectApplicationFacade
    scope: InvocationScope
    direct_session_alias: str
    source_package_alias: str


async def _transform(
    runtime: TransformRuntime,
    *args,
    request_identity: str | None = None,
    **kwargs,
):
    return await runtime.service.transform(
        *args,
        request_identity=request_identity or f"test-transform:{uuid4().hex}",
        **kwargs,
    )


async def _repair_transform(
    runtime: TransformRuntime,
    *args,
    request_identity: str | None = None,
    request_payload: dict[str, object] | None = None,
    **kwargs,
):
    command = kwargs.get("command") or args[3]
    assert isinstance(command, DataRepairTransformCommand)
    declared = request_payload or {
        "mode": "repair",
        "request": {
            "source_kind": "exact_operation",
            "parent_operation_id": command.parent_operation_id,
            **command.model_dump(mode="json", exclude={"parent_operation_id"}, exclude_none=True),
        },
    }
    return await runtime.service.repair_transform(
        *args,
        request_identity=request_identity or f"test-repair:{uuid4().hex}",
        request_payload=declared,
        **kwargs,
    )


async def _runtime(
    tmp_path: Path,
    worker: FakeWorkerPort,
    *,
    python_worker: FakeWorkerPort | None = None,
) -> TransformRuntime:
    state = MemoryState()
    repository = InMemoryControlPlaneRepository(state)
    identities = InMemoryIdentityDirectory()
    artifacts = ArtifactService(repository, LocalStorage(tmp_path / "storage"))
    sessions = DirectSessionService(repository, identities, "m2a-application-test-user")
    resolver = DirectScopeResolver(repository, identities, "m2a-application-test-user")
    opened = await sessions.open_direct_session("M2a application tests")
    scope = await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id="command_m2a_application_test",
    )

    source_path = FIXTURE_ROOT / "minimal_line.dxf"

    application = DataRuntimeService(repository, artifacts)
    imported = await application.import_cad_source(
        scope,
        ValidatedSource(
            filename=source_path.name,
            media_type="application/dxf",
            content=source_path.read_bytes(),
            source_identity=f"test-fixture:{source_path.name}",
        ),
    )
    service = DataTransformService(
        repository,
        artifacts,
        ExactPayloadResolver(repository, artifacts),
        worker,
        python_worker=python_worker,
        source_patch=TextSourcePatchAdapter(),
    )
    code_discovery = CodeDiscoveryService(
        application,
        None,
        None,
        None,
        None,
        None,
        None,
        CursorCodec("A" * 43),
    )
    facade = DirectApplicationFacade(
        application,
        sessions,
        resolver,
        transform_service=service,
        code_discovery_service=code_discovery,
    )
    return TransformRuntime(
        state=state,
        repository=repository,
        artifacts=artifacts,
        application=application,
        service=service,
        facade=facade,
        scope=scope,
        direct_session_alias=opened.binding.direct_session_alias,
        source_package_alias=imported.published.package.catalog_entry.alias,
    )


def _command(
    package_alias: str,
    *,
    inputs: tuple[TransformInputBinding, ...] | None = None,
) -> DataTransformCommand:
    return DataTransformCommand(
        inputs=inputs
        or (
            TransformInputBinding(
                input_name="entities",
                package_alias=package_alias,
                payload_name="entities",
            ),
            TransformInputBinding(
                input_name="geometry",
                package_alias=package_alias,
                payload_name="geometry_elements",
            ),
        ),
        parameters=(
            StringParameter(name="target_layer", kind="string", value="0"),
            IntegerParameter(name="minimum_count", kind="integer", value=1),
            NumberParameter(name="snap_distance", kind="number", value=1.25),
            BooleanParameter(name="keep_unresolved", kind="boolean", value=True),
            StringListParameter(
                name="allowed_types",
                kind="string_list",
                value=("LINE", "POINT"),
            ),
            IntegerListParameter(
                name="group_codes",
                kind="integer_list",
                value=(10, 20),
            ),
            NumberListParameter(
                name="weights",
                kind="number_list",
                value=(0.5, 1.5),
            ),
            BooleanListParameter(
                name="flags",
                kind="boolean_list",
                value=(True, False),
            ),
        ),
        execution=DuckDbSqlExecution(
            engine="duckdb_sql",
            code=(
                "SELECT e.entity_key AS candidate_key, 'entities' AS source "
                "FROM entities AS e "
                "CROSS JOIN (SELECT count(*) FROM geometry) AS g "
                "WHERE e.layer = $target_layer"
            ),
        ),
        outputs=(
            ParquetOutputDeclaration(
                payload_name="selected_rows",
                format="parquet",
                description="Rows selected from exact CAD facts.",
                logical_key_fields=("candidate_key",),
            ),
        ),
        output_package=OutputPackageDeclaration(
            name="selected_cad_facts_v1",
            description_markdown=DESCRIPTION,
        ),
    )


def _python_command(package_alias: str) -> DataTransformCommand:
    base = _command(
        package_alias,
        inputs=(
            TransformInputBinding(
                input_name="entities",
                package_alias=package_alias,
                payload_name="entities",
            ),
        ),
    )
    return base.model_copy(
        update={
            "parameters": (),
            "execution": PythonArrowExecution(
                engine="python_arrow",
                code=(
                    "def transform(ctx):\n"
                    "    table = ctx.table('entities').select(['entity_key'])\n"
                    "    ctx.write_table('selected_rows', "
                    "table.rename_columns(['candidate_key']))\n"
                ),
            ),
        }
    )


def _python_worker(
    tmp_path: Path,
    *,
    total_call_seconds: float = 5.0,
) -> SubprocessPythonArrowDataTransformWorker:
    return SubprocessPythonArrowDataTransformWorker(
        policy=PythonArrowWorkerPolicy(
            total_call_seconds=total_call_seconds,
            execution_seconds=total_call_seconds - 0.5,
            capacity_wait_seconds=.1,
            terminal_seconds=.1,
            wall_seconds=4.0,
            cleanup_seconds=0.2,
        ),
        python_executable=os.environ.get(
            "CAD2BIM_TEST_DATA_PYTHON_WORKER_EXECUTABLE",
            sys.executable,
        ),
        capacity_root=tmp_path / "python-capacity",
    )


def _multi_output_command(
    package_alias: str,
    output_names: tuple[str, ...] = ("products", "ports"),
) -> DataTransformCommand:
    base = _command(package_alias)
    declarations = tuple(
        ParquetOutputDeclaration(
            payload_name=name,
            format="parquet",
            description=f"Declared {name} rows.",
            logical_key_fields=(f"{name[:-1] if name.endswith('s') else name}_key",),
        )
        for name in output_names
    )
    return base.model_copy(
        update={
            "execution": DuckDbSqlExecution(
                engine="duckdb_sql",
                code="CREATE TEMP TABLE products AS SELECT 1 AS product_key;",
            ),
            "outputs": declarations,
        }
    )


def _expected_request_fingerprint(
    scope,
    command,
    resolved,
) -> str:
    value = {
        "run_id": scope.run_id,
        "inputs": [
            {
                "input_name": item.request_name,
                "package_id": item.package_id,
                "member_id": item.payload.member.member_id,
                "artifact": item.payload.member.artifact.model_dump(mode="json"),
            }
            for item in resolved
        ],
        "parameters": [item.model_dump(mode="json") for item in command.parameters],
        "inline_tables": [item.model_dump(mode="json") for item in command.inline_tables],
        "execution": command.execution.model_dump(mode="json"),
        "outputs": [item.model_dump(mode="json") for item in command.outputs],
        "output_package": command.output_package.model_dump(mode="json"),
    }
    return sha256_digest(value)


async def _catalog_counts(runtime: TransformRuntime) -> tuple[int, int]:
    packages = await runtime.repository.list_catalog(runtime.scope, CatalogResourceKind.PACKAGE)
    operations = await runtime.repository.list_catalog(runtime.scope, CatalogResourceKind.OPERATION)
    return len(packages), len(operations)


async def _latest_operation(runtime: TransformRuntime):
    entries = await runtime.repository.list_catalog(runtime.scope, CatalogResourceKind.OPERATION)
    entry = max(entries, key=lambda item: item.ordinal)
    operation = await runtime.repository.get_operation(runtime.scope, entry.operation_id or "")
    assert operation is not None
    return operation


async def _input_members(runtime: TransformRuntime):
    entries = await runtime.repository.list_catalog(runtime.scope, CatalogResourceKind.PACKAGE)
    source_entry = next(item for item in entries if item.alias == runtime.source_package_alias)
    package = await runtime.repository.get_package(runtime.scope, source_entry.package_id or "")
    assert package is not None
    names = {
        "payloads/entities/part-00000.parquet",
        "payloads/geometry_elements/part-00000.parquet",
    }
    return tuple(member for member in package.members if member.member_name in names)


async def _visible_packages(runtime: TransformRuntime):
    entries = await runtime.repository.list_catalog(runtime.scope, CatalogResourceKind.PACKAGE)
    result = {}
    for entry in entries:
        package = await runtime.repository.get_package(runtime.scope, entry.package_id or "")
        assert package is not None
        result[entry.alias] = package
    return result


async def test_distinct_transform_actions_publish_distinct_packages_and_same_action_replays(
    tmp_path: Path,
) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)

    first = await _transform(runtime,
        runtime.scope, command, request_identity="test-transform-action-one"
    )
    replayed = await _transform(runtime,
        runtime.scope, command, request_identity="test-transform-action-one"
    )
    second = await _transform(runtime,
        runtime.scope, command, request_identity="test-transform-action-two"
    )

    assert replayed.published.operation.operation.operation_id == first.published.operation.operation.operation_id
    assert replayed.published.package.package.package_id == first.published.package.package.package_id
    assert second.published.operation.operation.operation_id != first.published.operation.operation.operation_id
    assert second.published.package.package.package_id != first.published.package.package.package_id
    assert len(worker.requests) == 2


async def test_concurrent_same_action_uses_one_worker_and_one_exact_result(
    tmp_path: Path,
) -> None:
    class GatedWorker(FakeWorkerPort):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, request, inputs, *, deadline=None):
            self.started.set()
            await self.release.wait()
            return await super().execute(request, inputs, deadline=deadline)

    worker = GatedWorker()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    request_identity = "test-concurrent-same-transform"
    first_task = asyncio.create_task(
        _transform(
            runtime, runtime.scope, command, request_identity=request_identity
        )
    )
    await worker.started.wait()
    second_task = asyncio.create_task(
        _transform(
            runtime, runtime.scope, command, request_identity=request_identity
        )
    )
    worker.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first.published.operation.operation.operation_id == second.published.operation.operation.operation_id
    assert first.published.package.package.package_id == second.published.package.package.package_id
    assert len(worker.requests) == 1


async def test_transform_action_conflict_precedes_current_alias_resolution(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path, FakeWorkerPort())
    first_command = _command(runtime.source_package_alias)
    await _transform(runtime,
        runtime.scope, first_command, request_identity="test-transform-conflict"
    )
    changed = first_command.model_copy(
        update={
            "inputs": (
                first_command.inputs[0].model_copy(update={"package_alias": "data_999"}),
            )
        }
    )

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime,
            runtime.scope, changed, request_identity="test-transform-conflict"
        )

    assert captured.value.code == "TRANSFORM_REQUEST_IDENTITY_CONFLICT"


async def test_transform_action_replay_reads_its_exact_package_past_old_frontier(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path, FakeWorkerPort())
    command = _command(runtime.source_package_alias)
    result = await _transform(runtime,
        runtime.scope, command, request_identity="test-transform-old-frontier"
    )
    old_scope = runtime.scope.model_copy(update={"catalog_frontier": 1})

    replayed = await _transform(runtime,
        old_scope, command, request_identity="test-transform-old-frontier"
    )

    assert replayed.published.package.package.package_id == result.published.package.package.package_id


@pytest.mark.parametrize("diagnostic", (None, {
    "exception_type": "RuntimeError",
    "message": "Instance with GlobalId 'missing' not found",
    "function": "transform",
    "line": 5,
    "source_line": "product = model.by_guid(guid)",
}))
async def test_failed_transform_replay_preserves_exact_error_past_old_frontier(
    tmp_path: Path,
    diagnostic: dict | None,
) -> None:
    worker = FakeWorkerPort(
        failure=DataTransformWorkerError(
            "TRANSFORM_EXECUTION_FAILED",
            "The SQL failed after execution started",
            phase="execution",
            execution_started=True,
            diagnostic=diagnostic,
        )
    )
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    request_identity = "test-failed-transform-old-frontier"

    with pytest.raises(DataTransformApplicationError) as first:
        await _transform(
            runtime, runtime.scope, command, request_identity=request_identity
        )
    old_scope = runtime.scope.model_copy(update={"catalog_frontier": 1})
    with pytest.raises(DataTransformApplicationError) as replay:
        await _transform(
            runtime, old_scope, command, request_identity=request_identity
        )

    assert (
        replay.value.code,
        replay.value.phase,
        replay.value.retryable,
        replay.value.operation_alias,
    ) == (
        first.value.code,
        first.value.phase,
        first.value.retryable,
        first.value.operation_alias,
    )
    assert len(worker.requests) == 1
    assert str(replay.value) == str(first.value)
    operation = await _latest_operation(runtime)
    assert operation.operation.error_message == str(first.value)
    if diagnostic is not None:
        assert "RuntimeError" in str(first.value)
        assert "GlobalId 'missing' not found" in str(first.value)
        assert '"line":5' in str(first.value)


@pytest.mark.parametrize("source_padding", (0, 4000))
async def test_admitted_unclaimed_transform_resumes_from_exact_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_padding: int,
) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    if source_padding:
        source = command.execution.code + "-- capacity padding\n" * source_padding
        assert 65_536 < len(source.encode("utf-8")) < 131_072
        command = command.model_copy(
            update={"execution": DuckDbSqlExecution(engine="duckdb_sql", code=source)}
        )
    original_claim = runtime.repository.claim_data_transform_execution

    async def no_claim(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime.repository, "claim_data_transform_execution", no_claim)
    with pytest.raises(DataTransformApplicationError):
        await _transform(runtime,
            runtime.scope,
            command,
            request_identity="test-transform-unclaimed",
            deadline=monotonic() + 1.5,
        )
    monkeypatch.setattr(
        runtime.repository, "claim_data_transform_execution", original_claim
    )

    resumed = await _transform(runtime,
        runtime.scope,
        command,
        request_identity="test-transform-unclaimed",
    )

    assert resumed.published.operation.operation.status is CriticalOperationStatus.SUCCEEDED
    assert len(worker.requests) == 1
    assert worker.requests[0].code == command.execution.code


async def _admitted_unclaimed_transform(
    runtime: TransformRuntime,
    command: DataTransformCommand,
    request_identity: str,
    monkeypatch: pytest.MonkeyPatch,
):
    original_claim = runtime.repository.claim_data_transform_execution

    async def no_claim(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime.repository, "claim_data_transform_execution", no_claim)
    try:
        with pytest.raises(DataTransformApplicationError):
            await _transform(
                runtime,
                runtime.scope,
                command,
                request_identity=request_identity,
                deadline=monotonic() + 0.3,
            )
    finally:
        monkeypatch.setattr(
            runtime.repository, "claim_data_transform_execution", original_claim
        )
    state = await runtime.repository.get_data_transform_operation_by_identity(
        runtime.scope, request_identity
    )
    assert state is not None
    assert state.operation.status is CriticalOperationStatus.PREPARED
    assert state.execution_owner is None
    return state


def _mark_transform_expired(runtime: TransformRuntime, state):
    expired = state.model_copy(
        update={"deadline_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )
    runtime.state.data_transform_operations[state.operation.operation_id] = expired
    return expired


async def test_expired_unclaimed_transform_publishes_original_timeout_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    request_identity = "test-expired-unclaimed-transform"
    state = await _admitted_unclaimed_transform(
        runtime, command, request_identity, monkeypatch
    )
    _mark_transform_expired(runtime, state)

    with pytest.raises(DataTransformApplicationError) as failure:
        await _transform(
            runtime, runtime.scope, command, request_identity=request_identity
        )

    assert failure.value.code == "TRANSFORM_TIMEOUT"
    assert failure.value.phase == "execution"
    assert failure.value.operation_alias == "op_002"
    terminal = await runtime.repository.get_data_transform_operation_by_identity(
        runtime.scope, request_identity
    )
    assert terminal is not None
    assert terminal.operation.operation_id == state.operation.operation_id
    assert terminal.operation.status is CriticalOperationStatus.FAILED
    assert worker.requests == []


async def test_expired_stopped_executor_without_candidate_publishes_timeout_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    request_identity = "test-expired-stopped-transform"
    state = await _admitted_unclaimed_transform(
        runtime, command, request_identity, monkeypatch
    )
    claim = await runtime.repository.claim_data_transform_execution(
        runtime.scope,
        state.operation.operation_id,
        owner="test-stopped-executor",
        lease_seconds=30.0,
    )
    assert claim is not None
    _mark_transform_expired(
        runtime,
        await runtime.repository.get_data_transform_operation_by_identity(
            runtime.scope, request_identity
        ),
    )
    monkeypatch.setattr(
        memory_data_runtime_repository,
        "data_transform_executor_stopped",
        lambda _owner: True,
    )

    with pytest.raises(DataTransformApplicationError) as failure:
        await _transform(
            runtime, runtime.scope, command, request_identity=request_identity
        )

    assert failure.value.code == "TRANSFORM_TIMEOUT"
    assert failure.value.operation_alias == "op_002"
    terminal = await runtime.repository.get_data_transform_operation_by_identity(
        runtime.scope, request_identity
    )
    assert terminal is not None
    assert terminal.operation.operation_id == state.operation.operation_id
    assert terminal.operation.status is CriticalOperationStatus.FAILED
    assert worker.requests == []


async def test_expired_healthy_executor_without_candidate_remains_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    request_identity = "test-expired-healthy-transform"
    state = await _admitted_unclaimed_transform(
        runtime, command, request_identity, monkeypatch
    )
    claim = await runtime.repository.claim_data_transform_execution(
        runtime.scope,
        state.operation.operation_id,
        owner="test-healthy-executor",
        lease_seconds=30.0,
    )
    assert claim is not None
    _mark_transform_expired(
        runtime,
        await runtime.repository.get_data_transform_operation_by_identity(
            runtime.scope, request_identity
        ),
    )
    monkeypatch.setattr(
        memory_data_runtime_repository,
        "data_transform_executor_stopped",
        lambda _owner: False,
    )

    with pytest.raises(DataTransformApplicationError) as uncertain:
        await runtime.service.resume_by_identity(
            runtime.scope,
            request_identity,
            state.request_payload,
            deadline=monotonic() + 0.3,
        )

    assert uncertain.value.code == "TRANSFORM_RESULT_UNCERTAIN"
    current = await runtime.repository.get_data_transform_operation_by_identity(
        runtime.scope, request_identity
    )
    assert current is not None
    assert current.operation.status is CriticalOperationStatus.PREPARED
    assert await _catalog_counts(runtime) == (1, 1)
    assert worker.requests == []


async def test_waiter_timeout_does_not_cancel_admitted_transform(
    tmp_path: Path,
) -> None:
    class GatedWorker(FakeWorkerPort):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, request, inputs, *, deadline=None):
            self.started.set()
            await self.release.wait()
            return await super().execute(request, inputs, deadline=deadline)

    worker = GatedWorker()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    waiter = asyncio.create_task(
        _transform(runtime,
            runtime.scope,
            command,
            request_identity="test-transform-disconnected-waiter",
            deadline=monotonic() + 0.5,
        )
    )
    await worker.started.wait()
    with pytest.raises(DataTransformApplicationError):
        await waiter

    worker.release.set()
    resumed = await _transform(runtime,
        runtime.scope,
        command,
        request_identity="test-transform-disconnected-waiter",
    )

    assert resumed.published.operation.operation.status is CriticalOperationStatus.SUCCEEDED
    assert len(worker.requests) == 1


async def _stage_unpublished_candidate(
    runtime: TransformRuntime,
    command: DataTransformCommand,
    request_identity: str,
    monkeypatch: pytest.MonkeyPatch,
):
    original_publish = runtime.repository.publish_package_result

    async def lost_publish_response(*_args, **_kwargs):
        raise RuntimeError("candidate was staged before publication response was lost")

    async def no_terminal_yet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime.repository, "publish_package_result", lost_publish_response)
    monkeypatch.setattr(runtime.service, "_try_reconcile", no_terminal_yet)
    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(
            runtime,
            runtime.scope,
            command,
            request_identity=request_identity,
        )
    assert captured.value.code == "TRANSFORM_PUBLICATION_UNCERTAIN"
    monkeypatch.setattr(runtime.repository, "publish_package_result", original_publish)
    state = await runtime.repository.get_data_transform_operation_by_identity(
        runtime.scope, request_identity
    )
    assert state is not None and state.candidate is not None
    return state


async def test_stopped_executor_publishes_original_complete_candidate_without_worker_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    request_identity = "test-recover-staged-candidate"
    state = await _stage_unpublished_candidate(
        runtime, command, request_identity, monkeypatch
    )
    monkeypatch.setattr(
        memory_data_runtime_repository,
        "data_transform_executor_stopped",
        lambda _owner: True,
    )

    recovered = await _transform(
        runtime,
        runtime.scope,
        command,
        request_identity=request_identity,
    )

    assert recovered.published.operation.operation.operation_id == state.operation.operation_id
    assert recovered.published.package.package.package_id == state.candidate.package.package_id
    assert len(worker.requests) == 1


async def test_healthy_executor_candidate_is_not_reclaimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    request_identity = "test-healthy-candidate-owner"
    state = await _stage_unpublished_candidate(
        runtime, command, request_identity, monkeypatch
    )
    monkeypatch.setattr(
        memory_data_runtime_repository,
        "data_transform_executor_stopped",
        lambda _owner: False,
    )

    with pytest.raises(DataTransformApplicationError):
        await runtime.service.resume_by_identity(
            runtime.scope,
            request_identity,
            state.request_payload,
            deadline=monotonic() + 0.2,
        )

    current = await runtime.repository.get_data_transform_operation_by_identity(
        runtime.scope, request_identity
    )
    assert current is not None
    assert current.execution_generation == state.execution_generation
    assert current.candidate == state.candidate
    assert len(worker.requests) == 1


async def test_candidate_recovery_rejects_changed_artifact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(tmp_path, FakeWorkerPort())
    command = _command(runtime.source_package_alias)
    request_identity = "test-candidate-byte-integrity"
    state = await _stage_unpublished_candidate(
        runtime, command, request_identity, monkeypatch
    )
    monkeypatch.setattr(
        memory_data_runtime_repository,
        "data_transform_executor_stopped",
        lambda _owner: True,
    )
    target = state.candidate.members[0].artifact
    original_stream = runtime.artifacts.stream_version

    async def changed_stream(owner_id, artifact_id, version_id):
        stream = await original_stream(owner_id, artifact_id, version_id)
        if (artifact_id, version_id) != (target.artifact_id, target.version_id):
            return stream

        async def wrong_bytes():
            yield b"x" * stream.artifact.size_bytes

        return ArtifactStream(artifact=stream.artifact, chunks=wrong_bytes())

    monkeypatch.setattr(runtime.artifacts, "stream_version", changed_stream)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(
            runtime,
            runtime.scope,
            command,
            request_identity=request_identity,
        )

    assert captured.value.code == "OPERATION_INTEGRITY_ERROR"
    assert await _catalog_counts(runtime) == (1, 1)


async def test_success_publishes_four_member_derived_package_and_replays_exact_result(
    tmp_path: Path,
) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    resolved = await runtime.service._resolve_inputs(
        runtime.scope,
        command,
        monotonic() + 60,
    )
    content_fingerprint = _expected_request_fingerprint(
        runtime.scope,
        command,
        resolved,
    )

    request_identity = "test-complete-transform"
    result = await _transform(
        runtime, runtime.scope, command, request_identity=request_identity
    )
    artifact_count = len(runtime.state.artifacts)
    catalog_counts = await _catalog_counts(runtime)
    replayed = await _transform(
        runtime, runtime.scope, command, request_identity=request_identity
    )
    provider_result = await runtime.facade.transform_data(
        runtime.direct_session_alias,
        command,
        request_identity=request_identity,
    )
    package_directory = await runtime.facade.list_data_packages(
        runtime.direct_session_alias,
        None,
        20,
    )
    operation_directory = await runtime.facade.list_operations(
        runtime.direct_session_alias,
        None,
        20,
    )
    provider_operation = await runtime.facade.get_operation(
        runtime.direct_session_alias,
        "op_002",
    )
    provider_codes = await runtime.facade.list_code(
        CodeListFirstPageInput(
            direct_session_alias=runtime.direct_session_alias,
            mode="first",
            query={"code_kind": "data_transform_attempt", "page_size": 20},
        )
    )
    provider_code_card = next(
        item for item in provider_codes.codes if item.result_alias == "data_002"
    )
    assert provider_code_card.result_code_locator is not None
    provider_code = await runtime.facade.read_code(
        SingleTextCodeReadInput(
            direct_session_alias=runtime.direct_session_alias,
            source_form="single_text",
            code_locator=provider_code_card.result_code_locator,
            view={"view_mode": "full"},
        )
    )

    assert result.published.package.catalog_entry.alias == "data_002"
    assert result.published.operation.catalog_entry.alias == "op_002"
    assert result.published.package.package.package_kind is DataPackageKind.DERIVED
    assert len(result.published.package.members) == 4
    assert {
        (member.member_name, member.member_kind) for member in result.published.package.members
    } == {
        ("code/transformation.sql", PackageMemberKind.CODE),
        ("description.md", PackageMemberKind.DESCRIPTION),
        ("manifest.json", PackageMemberKind.MANIFEST),
        ("payloads/selected_rows/part-00000.parquet", PackageMemberKind.PAYLOAD),
    }
    assert result.output_members[0].row_count == 2
    assert result.output_members[0].logical_key_fields == ("candidate_key",)
    assert provider_result.operation_alias == "op_002"
    assert provider_result.package_alias == "data_002"
    assert provider_result.output.payload_name == "selected_rows"
    assert provider_result.output.row_count == 2
    assert package_directory.packages[0].package_kind == "derived"
    assert package_directory.packages[0].producing_operation_alias == "op_002"
    assert operation_directory.operations[0].operation_alias == "op_002"
    assert provider_operation.detail.detail_kind == "data_transform"
    assert [item.package_alias for item in provider_operation.detail.inputs] == [
        "data_001",
        "data_001",
    ]
    assert provider_code_card.owner_alias == "op_002"
    assert provider_code.text == command.execution.code

    operation = result.published.operation.operation
    assert operation.request_fingerprint == sha256_digest(
        {
            "run_id": runtime.scope.run_id,
            "request_identity": request_identity,
            "content_fingerprint": content_fingerprint,
        }
    )
    code_member = next(
        member
        for member in result.published.package.members
        if member.member_kind is PackageMemberKind.CODE
    )
    assert operation.operation_kind is CriticalOperationKind.DATA_TRANSFORM
    assert operation.status is CriticalOperationStatus.SUCCEEDED
    assert operation.code_artifact == code_member.artifact
    assert (
        len(
            [
                artifact
                for artifact in runtime.state.artifacts.values()
                if artifact.kind == "data_transform_code"
            ]
        )
        == 1
    )

    detail = operation.detail
    assert detail is not None
    assert [item.input_name for item in detail.inputs] == ["entities", "geometry"]
    assert len({item.member_id for item in detail.inputs}) == 2
    assert [item.kind.value for item in detail.parameters] == [
        "string",
        "integer",
        "number",
        "boolean",
        "string_list",
        "integer_list",
        "number_list",
        "boolean_list",
    ]
    assert set(worker.input_tables[0]) == {"entities", "geometry"}
    assert worker.input_tables[0]["entities"].num_rows == 1
    assert worker.input_tables[0]["geometry"].num_rows == 2
    assert [item.value for item in worker.requests[0].parameters] == [
        "0",
        1,
        1.25,
        True,
        ("LINE", "POINT"),
        (10, 20),
        (0.5, 1.5),
        (True, False),
    ]

    assert replayed == result
    assert len(worker.requests) == 1
    assert len(runtime.state.artifacts) == artifact_count
    assert await _catalog_counts(runtime) == catalog_counts == (2, 2)


async def test_current_fingerprint_tracks_execution_and_inline_inputs(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path, FakeWorkerPort())
    for command in (
        _command(runtime.source_package_alias),
        _python_command(runtime.source_package_alias),
    ):
        resolved = await runtime.service._resolve_inputs(
            runtime.scope, command, monotonic() + 60
        )
        current = _request_fingerprint(runtime.scope, command, resolved)
        assert current == _expected_request_fingerprint(
            runtime.scope, command, resolved
        )
        changed_execution = command.model_copy(
            update={"execution": command.execution.model_copy(update={"code": "changed code"})}
        )
        assert current != _request_fingerprint(runtime.scope, changed_execution, resolved)
        inline_table = InlineTable(
            input_name="layer_map",
            columns=(InlineColumn(name="layer", type="string", nullable=False),),
            rows=(InlineRow(values=(InlineStringValue(kind="string", value="0"),)),),
        )
        with_inline = command.model_copy(update={"inline_tables": (inline_table,)})
        inline_fingerprint = _request_fingerprint(runtime.scope, with_inline, resolved)
        assert current != inline_fingerprint
        changed_inline = with_inline.model_copy(
            update={
                "inline_tables": (
                    inline_table.model_copy(
                        update={
                            "rows": (
                                InlineRow(values=(InlineStringValue(kind="string", value="1"),)),
                            )
                        }
                    ),
                )
            }
        )
        assert inline_fingerprint != _request_fingerprint(runtime.scope, changed_inline, resolved)

    json_command = _command(runtime.source_package_alias).model_copy(
        update={
            "outputs": (
                JsonOutputDeclaration(
                    payload_name="summary",
                    format="json",
                    description="Summary of the exact source.",
                    json_kind="document",
                    schema_json='{"type":"object"}',
                ),
            ),
        }
    )
    resolved = await runtime.service._resolve_inputs(
        runtime.scope, json_command, monotonic() + 60
    )
    assert _request_fingerprint(runtime.scope, json_command, resolved) == (
        _expected_request_fingerprint(
            runtime.scope,
            json_command,
            resolved,
        )
    )


async def test_multi_output_publishes_one_atomic_package_and_replays_declared_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = MultiOutputWorkerPort(
        (
            ("products", pa.table({"product_key": ["p-1", "p-2"], "kind": ["a", "b"]})),
            ("ports", pa.table({"port_key": ["port-1"], "product_key": ["p-1"]})),
        )
    )
    runtime = await _runtime(tmp_path, worker)
    command = _multi_output_command(runtime.source_package_alias)
    resolved = await runtime.service._resolve_inputs(
        runtime.scope,
        command,
        monotonic() + 60,
    )
    content_fingerprint = _expected_request_fingerprint(
        runtime.scope,
        command,
        resolved,
    )
    original_publish = runtime.repository.publish_package_result
    publication_member_counts: list[int] = []

    async def capture_one_publication(scope, package, members, operation, **kwargs):
        publication_member_counts.append(len(members))
        return await original_publish(scope, package, members, operation, **kwargs)

    monkeypatch.setattr(
        runtime.repository,
        "publish_package_result",
        capture_one_publication,
    )

    request_identity = "test-multi-output-transform"
    result = await _transform(
        runtime, runtime.scope, command, request_identity=request_identity
    )
    artifact_count = len(runtime.state.artifacts)
    replayed = await _transform(
        runtime, runtime.scope, command, request_identity=request_identity
    )

    assert [item.logical_payload_name for item in result.output_members] == [
        "products",
        "ports",
    ]
    assert [item.row_count for item in result.output_members] == [2, 1]
    assert result.published.operation.operation.request_fingerprint == sha256_digest(
        {
            "run_id": runtime.scope.run_id,
            "request_identity": request_identity,
            "content_fingerprint": content_fingerprint,
        }
    )
    assert len(result.published.package.members) == 5
    assert result == replayed
    assert len(worker.requests) == 1
    assert publication_member_counts == [5]
    assert len(runtime.state.artifacts) == artifact_count
    assert await _catalog_counts(runtime) == (2, 2)

    package = result.published.package
    code_member = next(
        item for item in package.members if item.member_kind is PackageMemberKind.CODE
    )
    manifest_member = next(
        item for item in package.members if item.member_kind is PackageMemberKind.MANIFEST
    )
    assert result.published.operation.operation.code_artifact == code_member.artifact
    manifest = json.loads(
        (
            await runtime.artifacts.download(
                runtime.scope.owner_id,
                manifest_member.artifact.artifact_id,
            )
        ).content
    )
    assert set(manifest["payloads"]) == {"products", "ports"}
    assert manifest["payloads"]["products"]["row_count"] == 2
    assert manifest["payloads"]["ports"]["row_count"] == 1


async def test_single_output_multi_statement_keeps_current_action_fingerprint(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path, FakeWorkerPort())
    base = _command(runtime.source_package_alias)
    command = base.model_copy(
        update={
            "execution": DuckDbSqlExecution(
                engine="duckdb_sql",
                code=(
                    "CREATE TEMP VIEW staged AS SELECT * FROM entities; "
                    "CREATE TEMP VIEW selected_rows AS "
                    "SELECT entity_key AS candidate_key, 'entities' AS source FROM staged;"
                ),
            )
        }
    )
    resolved = await runtime.service._resolve_inputs(
        runtime.scope,
        command,
        monotonic() + 60,
    )
    expected = _expected_request_fingerprint(
        runtime.scope,
        command,
        resolved,
    )

    request_identity = "test-multi-statement-transform"
    result = await _transform(
        runtime, runtime.scope, command, request_identity=request_identity
    )

    assert result.published.operation.operation.request_fingerprint == sha256_digest(
        {
            "run_id": runtime.scope.run_id,
            "request_identity": request_identity,
            "content_fingerprint": expected,
        }
    )


async def test_second_invalid_output_publishes_failed_operation_and_no_partial_package(
    tmp_path: Path,
) -> None:
    worker = MultiOutputWorkerPort(
        (
            ("products", pa.table({"product_key": ["p-1"]})),
            ("ports", pa.table({"port_key": ["duplicate", "duplicate"]})),
        )
    )
    runtime = await _runtime(tmp_path, worker)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime,
            runtime.scope,
            _multi_output_command(runtime.source_package_alias),
        )

    assert captured.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert captured.value.operation_alias == "op_002"
    assert await _catalog_counts(runtime) == (1, 2)
    operation = await _latest_operation(runtime)
    assert operation.operation.status is CriticalOperationStatus.FAILED
    assert operation.operation.code_artifact is not None
    assert not any(
        package.package_kind is DataPackageKind.DERIVED
        for package in runtime.state.data_packages.values()
    )
    assert not any(
        artifact.idempotency_key.startswith("data-transform-generated:")
        for artifact in runtime.state.artifacts.values()
    )


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [
        ("_OUTPUT_ROW_LIMIT", 2),
        ("_OUTPUT_ARROW_BYTES", 1),
        ("_OUTPUT_AGGREGATE_PARQUET_BYTES", 1),
    ],
)
async def test_multi_output_aggregate_budget_failure_has_no_partial_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, limit_name, limit_value)
    worker = MultiOutputWorkerPort(
        (
            ("products", pa.table({"product_key": ["p-1", "p-2"]})),
            ("ports", pa.table({"port_key": ["port-1"]})),
        )
    )
    runtime = await _runtime(tmp_path, worker)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime,
            runtime.scope,
            _multi_output_command(runtime.source_package_alias),
        )

    assert captured.value.code == "TRANSFORM_OUTPUT_BUDGET_EXCEEDED"
    assert captured.value.operation_alias == "op_002"
    assert await _catalog_counts(runtime) == (1, 2)
    assert not any(
        package.package_kind is DataPackageKind.DERIVED
        for package in runtime.state.data_packages.values()
    )


async def test_second_payload_upload_failure_keeps_partial_private_and_new_action_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = MultiOutputWorkerPort(
        (
            ("products", pa.table({"product_key": ["p-1"]})),
            ("ports", pa.table({"port_key": ["port-1"]})),
        )
    )
    runtime = await _runtime(tmp_path, worker)
    command = _multi_output_command(runtime.source_package_alias)
    original_upload = runtime.service._upload_path
    upload_calls = 0

    async def fail_second_payload_upload(*args, **kwargs):
        nonlocal upload_calls
        upload_calls += 1
        if upload_calls == 2:
            raise DataTransformApplicationError(
                "TRANSFORM_SERVICE_UNAVAILABLE",
                "The second Payload Artifact could not be stored",
                phase="publication",
                retryable=True,
            )
        return await original_upload(*args, **kwargs)

    monkeypatch.setattr(runtime.service, "_upload_path", fail_second_payload_upload)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, command)

    assert captured.value.code == "TRANSFORM_SERVICE_UNAVAILABLE"
    assert captured.value.operation_alias is None
    assert await _catalog_counts(runtime) == (1, 1)
    assert not any(
        package.package_kind is DataPackageKind.DERIVED
        for package in runtime.state.data_packages.values()
    )
    orphan_payloads = {
        artifact.artifact_id
        for artifact in runtime.state.artifacts.values()
        if artifact.idempotency_key.startswith("data-transform-generated:")
    }
    assert len(orphan_payloads) == 1

    monkeypatch.setattr(runtime.service, "_upload_path", original_upload)
    result = await _transform(runtime, runtime.scope, command)

    published_payloads = {member.artifact.artifact_id for member in result.output_members}
    assert orphan_payloads.isdisjoint(published_payloads)
    assert [item.logical_payload_name for item in result.output_members] == [
        "products",
        "ports",
    ]
    assert await _catalog_counts(runtime) == (2, 2)


async def test_new_action_does_not_claim_previous_unpublished_candidate_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(tmp_path, FakeWorkerPort())
    original_publish = runtime.repository.publish_package_result
    publish_calls = 0

    async def fail_first_publish(scope, package, members, operation, **kwargs):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 1:
            raise RuntimeError("publication response lost before commit")
        return await original_publish(scope, package, members, operation, **kwargs)

    monkeypatch.setattr(runtime.repository, "publish_package_result", fail_first_publish)
    command = _command(runtime.source_package_alias)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, command)
    assert captured.value.code == "TRANSFORM_PUBLICATION_UNCERTAIN"

    orphaned = {
        artifact.artifact_id: artifact.sha256
        for artifact in runtime.state.artifacts.values()
        if artifact.idempotency_key.startswith("data-transform-generated:")
    }
    assert len(orphaned) == 2

    result = await _transform(runtime, runtime.scope, command)
    generated_after_retry = {
        artifact.artifact_id: artifact.sha256
        for artifact in runtime.state.artifacts.values()
        if artifact.idempotency_key.startswith("data-transform-generated:")
    }
    published_generated_refs = {
        member.artifact.artifact_id
        for member in result.published.package.members
        if member.member_kind in {PackageMemberKind.PAYLOAD, PackageMemberKind.MANIFEST}
    }

    assert set(orphaned).issubset(generated_after_retry)
    assert published_generated_refs.isdisjoint(orphaned)
    assert len(generated_after_retry) == 4
    assert await _catalog_counts(runtime) == (2, 2)


async def test_retry_with_different_volatile_output_uses_new_generated_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(tmp_path, VolatileWorkerPort())
    original_publish = runtime.repository.publish_package_result
    publish_calls = 0

    async def fail_first_publish(scope, package, members, operation, **kwargs):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 1:
            raise RuntimeError("publication response lost before commit")
        return await original_publish(scope, package, members, operation, **kwargs)

    monkeypatch.setattr(runtime.repository, "publish_package_result", fail_first_publish)
    command = _command(runtime.source_package_alias)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, command)
    assert captured.value.code == "TRANSFORM_PUBLICATION_UNCERTAIN"
    first_generated_ids = {
        artifact.artifact_id
        for artifact in runtime.state.artifacts.values()
        if artifact.idempotency_key.startswith("data-transform-generated:")
    }
    assert len(first_generated_ids) == 2

    result = await _transform(runtime, runtime.scope, command)
    all_generated_ids = {
        artifact.artifact_id
        for artifact in runtime.state.artifacts.values()
        if artifact.idempotency_key.startswith("data-transform-generated:")
    }
    published_generated_refs = {
        member.artifact.artifact_id
        for member in result.published.package.members
        if member.member_kind in {PackageMemberKind.PAYLOAD, PackageMemberKind.MANIFEST}
    }

    assert len(all_generated_ids) == 4
    assert published_generated_refs.isdisjoint(first_generated_ids)
    assert len(published_generated_refs) == 2
    assert await _catalog_counts(runtime) == (2, 2)


async def test_repeated_package_alias_uses_direct_catalog_lookup_and_one_package_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    calls = {"alias": 0, "package": 0}
    original_alias_lookup = runtime.repository.get_catalog_entry_by_alias
    original_package_lookup = runtime.repository.get_package

    async def tracked_alias_lookup(scope, resource_kind, alias):
        calls["alias"] += 1
        return await original_alias_lookup(scope, resource_kind, alias)

    async def tracked_package_lookup(scope, package_id):
        calls["package"] += 1
        return await original_package_lookup(scope, package_id)

    async def forbidden_catalog_scan(*_args, **_kwargs):
        raise AssertionError("exact alias resolution must not scan the whole Catalog")

    monkeypatch.setattr(runtime.repository, "get_catalog_entry_by_alias", tracked_alias_lookup)
    monkeypatch.setattr(runtime.repository, "get_package", tracked_package_lookup)
    monkeypatch.setattr(runtime.repository, "list_catalog", forbidden_catalog_scan)

    result = await _transform(runtime,
        runtime.scope,
        _command(runtime.source_package_alias),
    )

    assert result.published.package.catalog_entry.alias == "data_002"
    assert calls == {"alias": 1, "package": 1}
    assert len(worker.requests) == 1


async def test_unresolved_payload_is_preflight_failure_without_operation_or_artifact(
    tmp_path: Path,
) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(
        runtime.source_package_alias,
        inputs=(
            TransformInputBinding(
                input_name="missing",
                package_alias=runtime.source_package_alias,
                payload_name="not_a_payload",
            ),
        ),
    )
    artifact_count = len(runtime.state.artifacts)
    catalog_counts = await _catalog_counts(runtime)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, command)

    assert captured.value.code == "PAYLOAD_NOT_FOUND"
    assert captured.value.phase == "preflight"
    assert captured.value.operation_alias is None
    assert len(worker.requests) == 0
    assert len(runtime.state.artifacts) == artifact_count
    assert await _catalog_counts(runtime) == catalog_counts == (1, 1)


async def test_input_parquet_metadata_budget_is_cumulative_and_precedes_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    members = await _input_members(runtime)
    sizes = [runtime.state.artifacts[member.artifact.artifact_id].size_bytes for member in members]
    assert len(sizes) == 2 and min(sizes) > 0
    monkeypatch.setattr(service_module, "_INPUT_PARQUET_BYTES", sum(sizes) - 1)

    async def unexpected_stream(*_args, **_kwargs):
        raise AssertionError("metadata preflight must reject before streaming bytes")

    monkeypatch.setattr(runtime.artifacts, "stream_version", unexpected_stream)
    before = set(Path(tempfile.gettempdir()).glob("cad2bim-exact-payload-*.parquet"))
    artifact_count = len(runtime.state.artifacts)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_INPUT_BUDGET_EXCEEDED"
    assert captured.value.phase == "preflight"
    assert captured.value.operation_alias is None
    assert worker.requests == []
    assert len(runtime.state.artifacts) == artifact_count + 2  # admitted source and description
    assert set(Path(tempfile.gettempdir()).glob("cad2bim-exact-payload-*.parquet")) == before


async def test_input_parquet_stream_budget_defends_metadata_and_removes_all_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    members = await _input_members(runtime)
    total_size = sum(
        runtime.state.artifacts[member.artifact.artifact_id].size_bytes for member in members
    )
    monkeypatch.setattr(service_module, "_INPUT_PARQUET_BYTES", total_size - 1)
    original_get_version_metadata = runtime.artifacts.get_version_metadata
    original_stream_version = runtime.artifacts.stream_version
    exact_records = {}
    exact_contents = {}
    for member in members:
        stream = await original_stream_version(
            member.owner_id,
            member.artifact.artifact_id,
            member.artifact.version_id,
        )
        exact_records[member.artifact.artifact_id] = stream.artifact
        exact_contents[member.artifact.artifact_id] = b"".join(
            [chunk async for chunk in stream.chunks]
        )

    async def underreported_metadata(owner_id: str, artifact_id: str, version_id: str):
        artifact = exact_records.get(artifact_id)
        if artifact is None:
            artifact = await original_get_version_metadata(owner_id, artifact_id, version_id)
        return artifact.model_copy(update={"size_bytes": artifact.size_bytes - 1})

    async def oversized_stream(owner_id: str, artifact_id: str, version_id: str):
        artifact = exact_records[artifact_id]

        async def chunks():
            yield exact_contents[artifact_id]

        return ArtifactStream(
            artifact=artifact.model_copy(update={"size_bytes": artifact.size_bytes - 1}),
            chunks=chunks(),
        )

    monkeypatch.setattr(runtime.artifacts, "get_version_metadata", underreported_metadata)
    monkeypatch.setattr(runtime.artifacts, "stream_version", oversized_stream)
    before = set(Path(tempfile.gettempdir()).glob("cad2bim-exact-payload-*.parquet"))
    artifact_count = len(runtime.state.artifacts)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_INPUT_BUDGET_EXCEEDED"
    assert captured.value.phase == "preflight"
    assert captured.value.operation_alias is None
    assert worker.requests == []
    assert len(runtime.state.artifacts) == artifact_count + 2  # admitted source and description
    assert set(Path(tempfile.gettempdir()).glob("cad2bim-exact-payload-*.parquet")) == before


async def test_worker_not_started_after_admission_records_failed_operation(
    tmp_path: Path,
) -> None:
    worker = FakeWorkerPort(
        failure=DataTransformWorkerError(
            "TRANSFORM_CAPACITY_BUSY",
            "Worker capacity is busy",
            phase="preflight",
            execution_started=False,
        )
    )
    runtime = await _runtime(tmp_path, worker)
    catalog_counts = await _catalog_counts(runtime)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_CAPACITY_BUSY"
    assert captured.value.phase == "execution"
    assert captured.value.retryable is False
    assert captured.value.operation_alias == "op_002"
    assert len(worker.requests) == 1
    assert await _catalog_counts(runtime) == (1, catalog_counts[1] + 1)


async def test_execution_started_failure_publishes_failed_operation_and_code_only(
    tmp_path: Path,
) -> None:
    worker = FakeWorkerPort(
        failure=DataTransformWorkerError(
            "TRANSFORM_EXECUTION_FAILED",
            "The SQL failed after execution started",
            phase="execution",
            execution_started=True,
        )
    )
    runtime = await _runtime(tmp_path, worker)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_EXECUTION_FAILED"
    assert captured.value.phase == "execution"
    assert captured.value.operation_alias == "op_002"
    assert await _catalog_counts(runtime) == (1, 2)
    operation = await _latest_operation(runtime)
    assert operation.operation.status is CriticalOperationStatus.FAILED
    assert operation.operation.error_phase is CriticalOperationErrorPhase.EXECUTION
    assert operation.operation.result_package_id is None
    assert operation.operation.code_artifact is not None
    code_artifact = runtime.state.artifacts[operation.operation.code_artifact.artifact_id]
    assert code_artifact.kind == "data_transform_code"
    assert (
        len(
            [
                artifact
                for artifact in runtime.state.artifacts.values()
                if artifact.kind == "data_transform_code"
            ]
        )
        == 1
    )


async def test_multibyte_worker_error_is_utf8_safely_bounded_before_persistence(
    tmp_path: Path,
) -> None:
    worker = FakeWorkerPort(
        failure=DataTransformWorkerError(
            "TRANSFORM_EXECUTION_FAILED",
            "错" * 1_000,
            phase="execution",
            execution_started=True,
        )
    )
    runtime = await _runtime(tmp_path, worker)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    operation = await _latest_operation(runtime)
    persisted = operation.operation.error_message
    assert persisted is not None
    assert persisted == "错" * 666
    assert str(captured.value) == persisted
    assert len(persisted.encode("utf-8")) == 1_998
    assert "\ufffd" not in persisted


@pytest.mark.parametrize(
    ("table", "message"),
    (
        (pa.table({"not_the_key": ["row-1"]}), "absent"),
        (pa.table({"candidate_key": pa.array([None], type=pa.string())}), "null"),
        (pa.table({"candidate_key": ["same", "same"]}), "unique"),
    ),
)
async def test_invalid_logical_key_records_output_validation_failure_without_package(
    tmp_path: Path,
    table: pa.Table,
    message: str,
) -> None:
    worker = FakeWorkerPort(table)
    runtime = await _runtime(tmp_path, worker)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"
    assert message in str(captured.value)
    assert await _catalog_counts(runtime) == (1, 2)
    operation = await _latest_operation(runtime)
    assert operation.operation.status is CriticalOperationStatus.FAILED
    assert operation.operation.error_phase is CriticalOperationErrorPhase.OUTPUT_VALIDATION
    assert operation.operation.code_artifact is not None


async def test_output_budget_uses_small_arrow_fixture_and_records_failed_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_OUTPUT_ROW_LIMIT", 1)
    worker = FakeWorkerPort(pa.table({"candidate_key": ["row-1", "row-2"]}))
    runtime = await _runtime(tmp_path, worker)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_OUTPUT_BUDGET_EXCEEDED"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"
    assert await _catalog_counts(runtime) == (1, 2)
    operation = await _latest_operation(runtime)
    assert operation.operation.status is CriticalOperationStatus.FAILED
    assert operation.operation.error_phase is CriticalOperationErrorPhase.OUTPUT_VALIDATION
    assert operation.operation.code_artifact is not None


async def test_request_deadline_cancels_worker_and_records_terminal_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_TOTAL_TIMEOUT_SECONDS", 0.20)
    monkeypatch.setattr(service_module, "_WORKER_CLEANUP_RESERVE_SECONDS", 0.05)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.03)
    monkeypatch.setattr(service_module, "_PUBLICATION_RECONCILE_RESERVE_SECONDS", 0.01)
    worker = BlockingWorkerPort()
    runtime = await _runtime(tmp_path, worker)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_TIMEOUT"
    assert captured.value.phase == "execution"
    assert captured.value.operation_alias == "op_002"
    operation = await _latest_operation(runtime)
    assert operation.operation.status is CriticalOperationStatus.FAILED
    assert operation.operation.error_code == "TRANSFORM_TIMEOUT"
    assert operation.operation.code_artifact is not None


async def _assert_admitted_failure_replays(
    runtime: TransformRuntime,
    command: DataTransformCommand,
    request_identity: str,
    failure: DataTransformApplicationError,
) -> None:
    assert failure.phase == "execution"
    assert failure.retryable is False
    assert failure.operation_alias == "op_002"
    state = await runtime.repository.get_data_transform_operation_by_identity(
        runtime.scope, request_identity
    )
    assert state is not None
    operation = state.operation
    assert operation.status is CriticalOperationStatus.FAILED
    assert operation.error_code == failure.code
    assert operation.error_phase is CriticalOperationErrorPhase.EXECUTION
    assert operation.result_package_id is None
    assert operation.code_artifact is not None
    assert await _catalog_counts(runtime) == (1, 2)
    with pytest.raises(DataTransformApplicationError) as replayed:
        await runtime.service.transform(
            runtime.scope, command, request_identity=request_identity
        )
    assert (replayed.value.code, replayed.value.phase, replayed.value.operation_alias) == (
        failure.code, failure.phase, failure.operation_alias
    )
    assert replayed.value.retryable is False
    replay_state = await runtime.repository.get_data_transform_operation_by_identity(
        runtime.scope, request_identity
    )
    assert replay_state == state
    assert await _catalog_counts(runtime) == (1, 2)


async def test_slow_real_worker_staging_timeout_preserves_admitted_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_TOTAL_TIMEOUT_SECONDS", 0.30)
    monkeypatch.setattr(service_module, "_WORKER_CLEANUP_RESERVE_SECONDS", 0.05)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.03)
    worker = SubprocessDuckDBDataTransformWorker(
        policy=DataTransformWorkerPolicy(
            wall_seconds=5.0,
            cpu_seconds=10,
            cleanup_seconds=0.05,
            capacity_wait_seconds=.05,
            execution_seconds=.10,
            terminal_seconds=.03,
        )
    )
    original_staging = worker._stage_inputs
    entered = threading.Event()
    release = threading.Event()

    def slow_staging(root: Path, inputs: tuple[ArrowTransformInput, ...]):
        entered.set()
        release.wait(timeout=2.0)
        return original_staging(root, inputs)

    monkeypatch.setattr(worker, "_stage_inputs", slow_staging)
    runtime = await _runtime(tmp_path, worker)
    catalog_counts = await _catalog_counts(runtime)
    command = _command(runtime.source_package_alias)
    request_identity = "test_slow_real_worker_staging_timeout_preserves_admitted_failure"
    transforming = asyncio.create_task(
        _transform(runtime, runtime.scope, command, request_identity=request_identity)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1.0)
        with pytest.raises(DataTransformApplicationError) as captured:
            await asyncio.wait_for(transforming, timeout=0.5)
    finally:
        release.set()
        await worker.close()

    assert captured.value.code == "TRANSFORM_TIMEOUT"
    assert catalog_counts == (1, 1)
    await _assert_admitted_failure_replays(
        runtime, command, request_identity, captured.value
    )



async def test_python_capacity_failure_preserves_admitted_operation_without_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_PYTHON_TOTAL_TIMEOUT_SECONDS", 0.30)
    monkeypatch.setattr(service_module, "_PYTHON_WORKER_CLEANUP_RESERVE_SECONDS", 0.07)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.03)
    worker = _python_worker(tmp_path)
    await worker._capacity.acquire()
    runtime = await _runtime(tmp_path, FakeWorkerPort(), python_worker=worker)
    catalog_counts = await _catalog_counts(runtime)
    command = _python_command(runtime.source_package_alias)
    request_identity = "test_python_capacity_failure_preserves_admitted_operation_without_package"

    try:
        with pytest.raises(DataTransformApplicationError) as captured:
            await _transform(runtime,
                runtime.scope,
                command,
                request_identity=request_identity,
            )
    finally:
        worker._capacity.release()
        await worker.close()

    assert captured.value.code == "TRANSFORM_CAPACITY_BUSY"
    assert catalog_counts == (1, 1)
    await _assert_admitted_failure_replays(
        runtime, command, request_identity, captured.value
    )
    assert not any(
        package.package_kind is DataPackageKind.DERIVED
        for package in runtime.state.data_packages.values()
    )



async def test_slow_python_staging_timeout_preserves_failed_action_without_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_PYTHON_TOTAL_TIMEOUT_SECONDS", 0.35)
    monkeypatch.setattr(service_module, "_PYTHON_WORKER_CLEANUP_RESERVE_SECONDS", 0.08)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.03)
    worker = _python_worker(tmp_path)
    worker._policy = replace(
        worker._policy, total_call_seconds=.35, execution_seconds=.10,
        capacity_wait_seconds=.10, terminal_seconds=.03, cleanup_seconds=.08,
    )
    original_staging = worker._stage_inputs
    entered = threading.Event()
    release = threading.Event()
    scratch_roots: list[Path] = []

    def slow_staging(root: Path, inputs: tuple[ArrowTransformInput, ...]):
        scratch_roots.append(root)
        entered.set()
        release.wait(timeout=2.0)
        return original_staging(root, inputs)

    monkeypatch.setattr(worker, "_stage_inputs", slow_staging)
    runtime = await _runtime(tmp_path, FakeWorkerPort(), python_worker=worker)
    catalog_counts = await _catalog_counts(runtime)
    command = _python_command(runtime.source_package_alias)
    request_identity = "test_slow_python_staging_timeout_preserves_failed_action_without_package"
    transforming = asyncio.create_task(
        _transform(runtime,
            runtime.scope,
            command,
            request_identity=request_identity,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1.0)
        with pytest.raises(DataTransformApplicationError) as captured:
            await asyncio.wait_for(transforming, timeout=0.6)
    finally:
        release.set()
        await worker.close()

    assert captured.value.code == "TRANSFORM_TIMEOUT"
    assert catalog_counts == (1, 1)
    await _assert_admitted_failure_replays(
        runtime, command, request_identity, captured.value
    )
    assert not any(
        package.package_kind is DataPackageKind.DERIVED
        for package in runtime.state.data_packages.values()
    )
    assert scratch_roots
    assert all(not root.exists() for root in scratch_roots)



async def test_worker_cleanup_reserve_still_leaves_time_for_terminal_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_TOTAL_TIMEOUT_SECONDS", 0.35)
    monkeypatch.setattr(service_module, "_WORKER_CLEANUP_RESERVE_SECONDS", 0.07)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.09)
    monkeypatch.setattr(service_module, "_PUBLICATION_RECONCILE_RESERVE_SECONDS", 0.02)
    worker = CleanupBlockingWorkerPort(cleanup_seconds=0.05)
    runtime = await _runtime(tmp_path, worker)

    started = asyncio.get_running_loop().time()
    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.45
    assert captured.value.code == "TRANSFORM_TIMEOUT"
    assert captured.value.phase == "execution"
    assert captured.value.operation_alias == "op_002"
    operation = await _latest_operation(runtime)
    assert operation.operation.status is CriticalOperationStatus.FAILED


async def test_direct_scope_delay_consumes_the_same_transform_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(direct_transform_application, "_TRANSFORM_TIMEOUT_SECONDS", 0.35)
    monkeypatch.setattr(service_module, "_WORKER_CLEANUP_RESERVE_SECONDS", 0.05)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.09)
    monkeypatch.setattr(service_module, "_PUBLICATION_RECONCILE_RESERVE_SECONDS", 0.02)
    worker = CleanupBlockingWorkerPort(cleanup_seconds=0.03)
    runtime = await _runtime(tmp_path, worker)

    class DelayedResolver:
        async def resolve(self, _alias: str, *, command_id: str):
            assert command_id.startswith("command_direct_data_transform_")
            await asyncio.sleep(0.15)
            return runtime.scope

    facade = DirectApplicationFacade(
        runtime.application,
        object(),
        DelayedResolver(),
        transform_service=runtime.service,
    )
    started = asyncio.get_running_loop().time()

    with pytest.raises(DirectApplicationError) as captured:
        await facade.transform_data(
            runtime.direct_session_alias,
            _command(runtime.source_package_alias),
            request_identity="test-delayed-scope",
        )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.45
    assert captured.value.code == "TRANSFORM_PUBLICATION_UNCERTAIN"
    assert captured.value.phase == "publication"
    assert captured.value.retryable is True
    worker.release.set()
    replay = await facade.transform_data(
        runtime.direct_session_alias,
        _command(runtime.source_package_alias),
        request_identity="test-delayed-scope",
    )
    assert replay.operation_alias == "op_002"
    assert len(worker.requests) == 1


async def test_output_validation_deadline_leaves_time_for_terminal_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_TOTAL_TIMEOUT_SECONDS", 0.30)
    monkeypatch.setattr(service_module, "_WORKER_CLEANUP_RESERVE_SECONDS", 0.03)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.08)
    monkeypatch.setattr(service_module, "_PUBLICATION_RECONCILE_RESERVE_SECONDS", 0.02)
    original_validation = service_module._validated_arrow_table
    validation_started = threading.Event()
    release_validation = threading.Event()

    def slow_cpu_validation(*args, **kwargs):
        validation_started.set()
        release_validation.wait(timeout=1.0)
        return original_validation(*args, **kwargs)

    monkeypatch.setattr(service_module, "_validated_arrow_table", slow_cpu_validation)
    runtime = await _runtime(tmp_path, FakeWorkerPort())

    started = asyncio.get_running_loop().time()
    task = asyncio.create_task(
        _transform(runtime, runtime.scope, _command(runtime.source_package_alias))
    )
    try:
        assert await asyncio.to_thread(validation_started.wait, 0.20)
        with pytest.raises(DataTransformApplicationError) as captured:
            await task
    finally:
        release_validation.set()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.30
    assert captured.value.code == "TRANSFORM_TIMEOUT"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"
    operation = await _latest_operation(runtime)
    assert operation.operation.status is CriticalOperationStatus.FAILED
    assert await _catalog_counts(runtime) == (1, 2)
    assert not any(
        package.package_kind is DataPackageKind.DERIVED
        for package in runtime.state.data_packages.values()
    )


async def test_parquet_write_deadline_transfers_cleanup_without_partial_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_TOTAL_TIMEOUT_SECONDS", 0.30)
    monkeypatch.setattr(service_module, "_WORKER_CLEANUP_RESERVE_SECONDS", 0.03)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.08)
    monkeypatch.setattr(service_module, "_PUBLICATION_RECONCILE_RESERVE_SECONDS", 0.02)
    original_write = service_module.pq.write_table
    write_started = threading.Event()
    release_write = threading.Event()
    written_paths: list[Path] = []

    def slow_parquet_write(table, where, *args, **kwargs):
        written_paths.append(Path(where))
        write_started.set()
        release_write.wait(timeout=1.0)
        return original_write(table, where, *args, **kwargs)

    monkeypatch.setattr(service_module.pq, "write_table", slow_parquet_write)
    runtime = await _runtime(tmp_path, FakeWorkerPort())

    started = asyncio.get_running_loop().time()
    task = asyncio.create_task(
        _transform(runtime, runtime.scope, _command(runtime.source_package_alias))
    )
    cleanups = ()
    try:
        assert await asyncio.to_thread(write_started.wait, 0.20)
        with pytest.raises(DataTransformApplicationError) as captured:
            await task
        cleanups = tuple(service_module._OUTPUT_VALIDATION_CLEANUPS)
    finally:
        release_write.set()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.30
    assert captured.value.code == "TRANSFORM_TIMEOUT"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"
    operation = await _latest_operation(runtime)
    assert operation.operation.status is CriticalOperationStatus.FAILED
    assert operation.operation.code_artifact is not None
    assert await _catalog_counts(runtime) == (1, 2)
    assert not any(
        package.package_kind is DataPackageKind.DERIVED
        for package in runtime.state.data_packages.values()
    )

    assert cleanups
    await asyncio.wait_for(asyncio.gather(*cleanups), timeout=1.0)
    assert written_paths
    assert all(not path.exists() for path in written_paths)


async def test_caller_cancellation_after_worker_start_preserves_admitted_execution(
    tmp_path: Path,
) -> None:
    worker = BlockingWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    request_identity = "test-cancelled-worker-waiter"
    task = asyncio.create_task(
        _transform(runtime, runtime.scope, command, request_identity=request_identity)
    )
    await worker.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    worker.release.set()
    replay = await _transform(
        runtime, runtime.scope, command, request_identity=request_identity
    )
    assert replay.published.operation.operation.status is CriticalOperationStatus.SUCCEEDED
    assert len(worker.requests) == 1


async def test_caller_cancellation_during_output_validation_preserves_admitted_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    original_validation = service_module._validated_arrow_table
    validation_started = threading.Event()
    release_validation = threading.Event()

    def slow_validation(*args, **kwargs):
        validation_started.set()
        release_validation.wait(timeout=1.0)
        return original_validation(*args, **kwargs)

    monkeypatch.setattr(service_module, "_validated_arrow_table", slow_validation)
    runtime = await _runtime(tmp_path, FakeWorkerPort())
    command = _command(runtime.source_package_alias)
    request_identity = "test-cancelled-validation-waiter"
    task = asyncio.create_task(
        _transform(runtime, runtime.scope, command, request_identity=request_identity)
    )
    try:
        assert await asyncio.to_thread(validation_started.wait, 0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_validation.set()

    replay = await _transform(
        runtime, runtime.scope, command, request_identity=request_identity
    )
    assert replay.published.operation.operation.status is CriticalOperationStatus.SUCCEEDED
    assert len(runtime.service._worker.requests) == 1


async def test_caller_cancellation_during_failed_operation_commit_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorkerPort(
        failure=DataTransformWorkerError(
            "TRANSFORM_EXECUTION_FAILED",
            "execution failed before cancellation",
            phase="execution",
            execution_started=True,
        )
    )
    runtime = await _runtime(tmp_path, worker)
    commit_started = asyncio.Event()

    async def hanging_failed_commit(*args, **kwargs):
        commit_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.repository, "record_failed_operation", hanging_failed_commit)
    catalog_counts = await _catalog_counts(runtime)
    task = asyncio.create_task(
        _transform(runtime, runtime.scope, _command(runtime.source_package_alias))
    )
    await commit_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _catalog_counts(runtime) == catalog_counts == (1, 1)
    assert not any(
        package.package_kind is DataPackageKind.DERIVED
        for package in runtime.state.data_packages.values()
    )


async def test_concurrent_distinct_actions_keep_success_and_failure_separate(
    tmp_path: Path,
) -> None:
    worker = RacingWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)

    first, second = await asyncio.gather(
        _transform(runtime, runtime.scope, command),
        _transform(runtime, runtime.scope, command),
        return_exceptions=True,
    )

    failures = [item for item in (first, second) if isinstance(item, Exception)]
    successes = [item for item in (first, second) if not isinstance(item, Exception)]
    assert len(failures) == len(successes) == 1
    assert isinstance(failures[0], DataTransformApplicationError)
    assert failures[0].code == "TRANSFORM_EXECUTION_FAILED"
    assert successes[0].published.operation.operation.status is CriticalOperationStatus.SUCCEEDED
    assert len(worker.requests) == 2
    assert await _catalog_counts(runtime) == (2, 3)


async def test_concurrent_distinct_actions_keep_failure_codes_separate(
    tmp_path: Path,
) -> None:
    worker = DistinctFailureWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    command = _command(runtime.source_package_alias)
    results = await asyncio.gather(
        _transform(runtime, runtime.scope, command),
        _transform(runtime, runtime.scope, command),
        return_exceptions=True,
    )

    assert all(isinstance(item, DataTransformApplicationError) for item in results)
    assert {item.code for item in results} == {  # type: ignore[union-attr]
        "TRANSFORM_EXECUTION_FAILED",
        "TRANSFORM_RESOURCE_LIMIT",
    }
    assert {item.operation_alias for item in results} == {"op_002", "op_003"}  # type: ignore[union-attr]
    assert await _catalog_counts(runtime) == (1, 3)


async def test_hanging_publication_artifact_upload_obeys_request_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_TOTAL_TIMEOUT_SECONDS", 0.20)
    monkeypatch.setattr(service_module, "_WORKER_CLEANUP_RESERVE_SECONDS", 0.03)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.03)
    monkeypatch.setattr(service_module, "_PUBLICATION_RECONCILE_RESERVE_SECONDS", 0.03)
    runtime = await _runtime(tmp_path, FakeWorkerPort())

    async def hanging_upload(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.service, "_upload_path", hanging_upload)
    started = asyncio.get_running_loop().time()
    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.35
    assert captured.value.code == "TRANSFORM_SERVICE_UNAVAILABLE"
    assert captured.value.phase == "publication"
    assert captured.value.retryable is True


async def test_hanging_package_commit_reconciles_within_request_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_TOTAL_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(service_module, "_WORKER_CLEANUP_RESERVE_SECONDS", 0.03)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.03)
    monkeypatch.setattr(service_module, "_PUBLICATION_RECONCILE_RESERVE_SECONDS", 0.05)
    runtime = await _runtime(tmp_path, FakeWorkerPort())

    async def hanging_publish(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.repository, "publish_package_result", hanging_publish)
    started = asyncio.get_running_loop().time()
    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.40
    assert captured.value.code == "TRANSFORM_PUBLICATION_UNCERTAIN"
    assert captured.value.phase == "publication"
    assert captured.value.retryable is True


async def test_caller_cancellation_during_package_commit_reconciles_then_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _runtime(tmp_path, FakeWorkerPort())
    commit_started = asyncio.Event()
    catalog_counts = await _catalog_counts(runtime)

    async def hanging_publish(*args, **kwargs):
        commit_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.repository, "publish_package_result", hanging_publish)
    task = asyncio.create_task(
        _transform(runtime, runtime.scope, _command(runtime.source_package_alias))
    )
    await commit_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _catalog_counts(runtime) == catalog_counts == (1, 1)
    assert not any(
        package.package_kind is DataPackageKind.DERIVED
        for package in runtime.state.data_packages.values()
    )


async def test_hanging_publication_reconcile_is_bounded_by_same_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    monkeypatch.setattr(service_module, "_TOTAL_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(service_module, "_WORKER_CLEANUP_RESERVE_SECONDS", 0.03)
    monkeypatch.setattr(service_module, "_TERMINAL_PUBLICATION_RESERVE_SECONDS", 0.03)
    monkeypatch.setattr(service_module, "_PUBLICATION_RECONCILE_RESERVE_SECONDS", 0.05)
    runtime = await _runtime(tmp_path, FakeWorkerPort())
    replay_calls = 0
    original_get_state = runtime.repository.get_data_transform_operation_by_identity

    async def fail_publish(*args, **kwargs):
        raise RuntimeError("database response lost")

    async def first_replay_then_hang(*args, **kwargs):
        nonlocal replay_calls
        replay_calls += 1
        if replay_calls == 1:
            return await original_get_state(*args, **kwargs)
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.repository, "publish_package_result", fail_publish)
    monkeypatch.setattr(
        runtime.repository, "get_data_transform_operation_by_identity", first_replay_then_hang
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.40
    assert captured.value.code == "TRANSFORM_PUBLICATION_UNCERTAIN"
    assert replay_calls == 2


async def test_hanging_successful_replay_package_read_obeys_request_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    runtime = await _runtime(tmp_path, FakeWorkerPort())
    command = _command(runtime.source_package_alias)
    request_identity = "test-hanging-exact-replay"
    await _transform(
        runtime, runtime.scope, command, request_identity=request_identity
    )
    monkeypatch.setattr(service_module, "_TOTAL_TIMEOUT_SECONDS", 0.10)
    result_read_calls = 0

    async def hanging_result_read(*args, **kwargs):
        nonlocal result_read_calls
        result_read_calls += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.repository, "get_result_for_operation", hanging_result_read)
    started = asyncio.get_running_loop().time()
    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(
            runtime, runtime.scope, command, request_identity=request_identity
        )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.25
    assert captured.value.code == "TRANSFORM_TIMEOUT"
    assert captured.value.phase == "publication"
    assert result_read_calls == 1


async def test_invalid_arrow_field_name_records_output_validation_failure(
    tmp_path: Path,
) -> None:
    worker = FakeWorkerPort(pa.table({"x" * 256: ["row-1"]}))
    runtime = await _runtime(tmp_path, worker)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"
    operation = await _latest_operation(runtime)
    assert operation.operation.status is CriticalOperationStatus.FAILED


async def test_real_worker_invalid_arrow_field_name_is_rejected_before_publication(
    tmp_path: Path,
) -> None:
    worker = SubprocessDuckDBDataTransformWorker()
    runtime = await _runtime(tmp_path, worker)  # type: ignore[arg-type]
    long_name = "x" * 256
    base = _command(
        runtime.source_package_alias,
        inputs=(
            TransformInputBinding(
                input_name="entities",
                package_alias=runtime.source_package_alias,
                payload_name="entities",
            ),
        ),
    )
    command = base.model_copy(
        update={
            "parameters": (),
            "execution": DuckDbSqlExecution(
                engine="duckdb_sql",
                code=(f'SELECT entity_key AS candidate_key, 1 AS "{long_name}" FROM entities'),
            ),
        }
    )

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, command)

    assert captured.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"


async def test_wide_legitimate_arrow_type_publishes_and_queries_exact_schema(
    tmp_path: Path,
) -> None:
    children = [pa.array([index], type=pa.int64()) for index in range(40)]
    struct = pa.StructArray.from_arrays(
        children,
        names=[f"descriptive_nested_field_{index:02d}" for index in range(40)],
    )
    table = pa.table({"candidate_key": ["row-1"], "wide": struct})
    assert len(str(table.schema.field("wide").type)) > 255
    worker = FakeWorkerPort(table)
    runtime = await _runtime(tmp_path, worker)

    result = await _transform(runtime,
        runtime.scope,
        _command(runtime.source_package_alias),
    )
    query_service = DataQueryService(
        runtime.repository,
        runtime.artifacts,
        CursorCodec("A" * 43),
    )
    described = await query_service.describe_package(
        runtime.scope,
        DataDescribePackageQuery(
            package_alias=result.published.package.catalog_entry.alias,
            payload_name="selected_rows",
        ),
    )

    persisted_wide = next(
        column for column in result.output_members[0].arrow_schema if column.name == "wide"
    )
    queried_wide = next(
        column for column in described.payload_schema.columns if column.name == "wide"
    )
    assert persisted_wide.type == str(table.schema.field("wide").type)
    assert queried_wide.type == persisted_wide.type


async def test_list_struct_schema_uses_the_serialized_parquet_child_name(
    tmp_path: Path,
) -> None:
    sections = pa.array(
        [[{"section_key": "section-1", "window_status": "pending"}]],
        type=pa.list_(
            pa.struct(
                [
                    pa.field("section_key", pa.string(), nullable=False),
                    pa.field("window_status", pa.string(), nullable=False),
                ]
            )
        ),
    )
    table = pa.table({"candidate_key": ["row-1"], "planned_pipe_sections": sections})
    assert "list<item:" in str(table.schema.field("planned_pipe_sections").type)
    runtime = await _runtime(tmp_path, FakeWorkerPort(table))

    result = await runtime.service.transform(
        runtime.scope,
        _command(runtime.source_package_alias),
        request_identity="serialized-parquet-footer-schema",
    )
    member = result.output_members[0]
    persisted_type = next(
        column.type for column in member.arrow_schema
        if column.name == "planned_pipe_sections"
    )
    manifest_member = next(
        item for item in result.published.package.members
        if item.member_kind is PackageMemberKind.MANIFEST
    )
    manifest = json.loads(
        (
            await runtime.artifacts.download_version(
                runtime.scope.owner_id,
                manifest_member.artifact.artifact_id,
                manifest_member.artifact.version_id,
                run_id=runtime.scope.run_id,
            )
        ).content
    )
    described = await DataQueryService(
        runtime.repository,
        runtime.artifacts,
        CursorCodec("A" * 43),
    ).describe_package(
        runtime.scope,
        DataDescribePackageQuery(
            package_alias=result.published.package.catalog_entry.alias,
            payload_name="selected_rows",
        ),
    )
    queried_type = next(
        column.type for column in described.payload_schema.columns
        if column.name == "planned_pipe_sections"
    )

    assert "list<element:" in persisted_type
    assert queried_type == persisted_type
    assert next(
        column["type"] for column in manifest["payloads"]["selected_rows"]["arrow_schema"]
        if column["name"] == "planned_pipe_sections"
    ) == persisted_type


async def test_arrow_type_over_utf8_budget_is_output_validation_failure(
    tmp_path: Path,
) -> None:
    field_count = 4_000
    children = [pa.array([index], type=pa.int64()) for index in range(field_count)]
    struct = pa.StructArray.from_arrays(
        children,
        names=[f"nested_{index:05d}" for index in range(field_count)],
    )
    table = pa.table({"candidate_key": ["row-1"], "wide": struct})
    assert len(str(table.schema.field("wide").type).encode("utf-8")) > 65_536
    runtime = await _runtime(tmp_path, FakeWorkerPort(table))

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"


async def test_non_parquet_serializable_arrow_type_is_stable_output_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    def reject_parquet(*args, **kwargs) -> None:
        raise pa.ArrowNotImplementedError("unsupported test type")

    worker = FakeWorkerPort(pa.table({"candidate_key": ["row-1"]}))
    runtime = await _runtime(tmp_path, worker)
    monkeypatch.setattr(service_module.pq, "write_table", reject_parquet)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"


async def test_output_scratch_creation_oserror_records_resource_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    runtime = await _runtime(tmp_path, FakeWorkerPort())

    def fail_mkstemp(*args, **kwargs):
        raise OSError("scratch is unavailable")

    monkeypatch.setattr(service_module, "tempfile", SimpleNamespace(mkstemp=fail_mkstemp))
    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_RESOURCE_LIMIT"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"
    operation = await _latest_operation(runtime)
    assert operation.operation.status is CriticalOperationStatus.FAILED


async def test_output_scratch_write_oserror_records_resource_limit_and_cleans_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from CAD2BIM_backend.application import data_transform_service as service_module

    runtime = await _runtime(tmp_path, FakeWorkerPort())
    scratch_path = tmp_path / "controlled-transform-output.parquet"

    def controlled_mkstemp(*args, **kwargs):
        descriptor = os.open(scratch_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return descriptor, str(scratch_path)

    def fail_write(*args, **kwargs):
        raise OSError("scratch write failed")

    monkeypatch.setattr(
        service_module,
        "tempfile",
        SimpleNamespace(mkstemp=controlled_mkstemp),
    )
    monkeypatch.setattr(service_module.pq, "write_table", fail_write)
    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))

    assert captured.value.code == "TRANSFORM_RESOURCE_LIMIT"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"
    assert not scratch_path.exists()


async def test_python_engine_routes_to_dedicated_worker_and_publishes_python_code(
    tmp_path: Path,
) -> None:
    sql_worker = FakeWorkerPort()
    python_worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, sql_worker, python_worker=python_worker)
    command = DataTransformCommand(
        inputs=(
            TransformInputBinding(
                input_name="entities",
                package_alias=runtime.source_package_alias,
                payload_name="entities",
            ),
        ),
        inline_tables=(
            InlineTable(
                input_name="layer_map",
                columns=(
                    InlineColumn(name="layer", type="string", nullable=False),
                    InlineColumn(name="priority", type="integer", nullable=False),
                ),
                rows=(
                    InlineRow(
                        values=(
                            InlineStringValue(kind="string", value="0"),
                            InlineIntegerValue(kind="integer", value=1),
                        )
                    ),
                ),
            ),
        ),
        execution=PythonArrowExecution(
            engine="python_arrow",
            code=(
                "def transform(ctx):\n"
                "    source = ctx.table('entities')\n"
                "    ctx.write_table('selected_rows', source.select(['entity_key']))\n"
            ),
        ),
        outputs=(
            ParquetOutputDeclaration(
                payload_name="selected_rows",
                format="parquet",
                description="Selected entity rows.",
                logical_key_fields=("candidate_key",),
            ),
        ),
        output_package=OutputPackageDeclaration(
            name="python_selected_rows_v1",
            description_markdown=DESCRIPTION,
        ),
    )

    result = await _transform(runtime, runtime.scope, command)

    assert sql_worker.requests == []
    assert len(python_worker.requests) == 1
    assert set(python_worker.input_tables[0]) == {"entities", "layer_map"}
    assert python_worker.input_descriptors[0]["entities"] == ("parquet", None)
    assert python_worker.input_descriptors[0]["layer_map"] == ("inline", None)
    assert python_worker.input_tables[0]["layer_map"].to_pylist() == [{"layer": "0", "priority": 1}]
    code = next(
        item
        for item in result.published.package.members
        if item.member_kind is PackageMemberKind.CODE
    )
    assert code.member_name == "code/transformation.py"
    assert code.media_type == "text/x-python"
    detail = result.published.operation.operation.detail
    assert detail is not None
    assert detail.engine == "python_arrow"
    assert detail.inline_tables == command.inline_tables


async def test_json_output_publishes_exact_payload_schema_and_replays_atomically(
    tmp_path: Path,
) -> None:
    value = {"metrics": {"candidate_count": 2}, "status": "ready"}
    worker = FakeWorkerPort(pa.table({"json_value": [json.dumps(value)]}))
    runtime = await _runtime(tmp_path, worker)
    schema_json = json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {
                "metrics": {
                    "type": "object",
                    "properties": {"candidate_count": {"type": "integer"}},
                    "required": ["candidate_count"],
                    "additionalProperties": False,
                }
            },
            "type": "object",
            "properties": {
                "metrics": {"$ref": "#/$defs/metrics"},
                "status": {"type": "string"},
            },
            "required": ["metrics", "status"],
            "additionalProperties": False,
        },
        separators=(",", ":"),
    )
    command = DataTransformCommand(
        inputs=(
            TransformInputBinding(
                input_name="entities",
                package_alias=runtime.source_package_alias,
                payload_name="entities",
            ),
        ),
        execution=DuckDbSqlExecution(
            engine="duckdb_sql",
            code="SELECT to_json(struct_pack(candidate_count := count(*))) AS json_value FROM entities",
        ),
        outputs=(
            JsonOutputDeclaration(
                payload_name="selected_rows",
                format="json",
                description="Bounded transform summary.",
                json_kind="document",
                schema_json=schema_json,
            ),
        ),
        output_package=OutputPackageDeclaration(
            name="json_summary_v1",
            description_markdown=DESCRIPTION,
        ),
    )

    request_identity = "test-json-output-transform"
    result = await _transform(
        runtime, runtime.scope, command, request_identity=request_identity
    )
    artifact_count = len(runtime.state.artifacts)
    replayed = await _transform(
        runtime, runtime.scope, command, request_identity=request_identity
    )

    assert result == replayed
    assert len(runtime.state.artifacts) == artifact_count
    members = {item.member_name: item for item in result.published.package.members}
    assert set(members) == {
        "payloads/selected_rows.json",
        "schemas/selected_rows.schema.json",
        "code/transformation.sql",
        "description.md",
        "manifest.json",
    }
    payload = members["payloads/selected_rows.json"]
    schema = members["schemas/selected_rows.schema.json"]
    assert payload.json_kind is not None and payload.json_kind.value == "document"
    expected_payload = b'{"metrics":{"candidate_count":2},"status":"ready"}'
    assert payload.logical_byte_count == len(expected_payload)
    payload_stream = await runtime.artifacts.stream_version(
        payload.owner_id,
        payload.artifact.artifact_id,
        payload.artifact.version_id,
    )
    schema_stream = await runtime.artifacts.stream_version(
        schema.owner_id,
        schema.artifact.artifact_id,
        schema.artifact.version_id,
    )
    assert b"".join([chunk async for chunk in payload_stream.chunks]) == expected_payload
    assert b"".join([chunk async for chunk in schema_stream.chunks]) == schema_json.encode()

    repaired = await _repair_transform(
        runtime, runtime.scope, result.published.operation, await _visible_packages(runtime),
        DataRepairTransformCommand(
            parent_operation_id=result.published.operation.operation.operation_id,
            output_package={"name": "revised_json_summary"},
        ),
        request_identity="test-json-output-metadata-repair",
    )
    assert repaired.published.package.package.package_id != result.published.package.package.package_id
    assert repaired.output_members[0].json_kind.value == "document"

    consumer_worker = FakeWorkerPort()
    consumer = DataTransformService(
        runtime.repository,
        runtime.artifacts,
        ExactPayloadResolver(runtime.repository, runtime.artifacts),
        consumer_worker,
    )
    consumer_command = DataTransformCommand(
        inputs=(
            TransformInputBinding(
                input_name="summary",
                package_alias=result.published.package.catalog_entry.alias,
                payload_name="selected_rows",
            ),
        ),
        execution=DuckDbSqlExecution(
            engine="duckdb_sql",
            code="SELECT json_value FROM summary",
        ),
        outputs=(
            ParquetOutputDeclaration(
                payload_name="selected_rows",
                format="parquet",
                description="Rows selected after exact JSON binding.",
                logical_key_fields=("candidate_key",),
            ),
        ),
        output_package=OutputPackageDeclaration(
            name="json_input_binding_v1",
            description_markdown=DESCRIPTION,
        ),
    )
    await consumer.transform(
        runtime.scope,
        consumer_command,
        request_identity="test-json-consumer-transform",
    )
    assert consumer_worker.input_descriptors[0]["summary"] == ("json", "document")
    assert consumer_worker.input_tables[0]["summary"].to_pylist() == [
        {"json_value": expected_payload.decode()}
    ]


async def test_remote_json_schema_reference_fails_preflight_without_operation(
    tmp_path: Path,
) -> None:
    worker = FakeWorkerPort(pa.table({"json_value": ["{}"]}))
    runtime = await _runtime(tmp_path, worker)
    command = DataTransformCommand(
        inputs=(
            TransformInputBinding(
                input_name="entities",
                package_alias=runtime.source_package_alias,
                payload_name="entities",
            ),
        ),
        execution=DuckDbSqlExecution(engine="duckdb_sql", code="SELECT '{}' AS json_value"),
        outputs=(
            JsonOutputDeclaration(
                payload_name="selected_rows",
                format="json",
                description="Invalid remote schema test.",
                json_kind="document",
                schema_json='{"$ref":"https://example.invalid/schema.json"}',
            ),
        ),
        output_package=OutputPackageDeclaration(
            name="invalid_json_schema_v1",
            description_markdown=DESCRIPTION,
        ),
    )
    before = await _catalog_counts(runtime)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, command)

    assert captured.value.code == "JSON_SCHEMA_INVALID"
    assert captured.value.phase == "preflight"
    assert worker.requests == []
    assert await _catalog_counts(runtime) == before


async def test_invalid_second_json_output_records_failure_without_partial_package(
    tmp_path: Path,
) -> None:
    worker = MultiOutputWorkerPort(
        (
            ("products", pa.table({"product_key": ["p-1"]})),
            ("summary", pa.table({"json_value": ["[]"]})),
        )
    )
    runtime = await _runtime(tmp_path, worker)
    base = _command(runtime.source_package_alias)
    command = base.model_copy(
        update={
            "execution": DuckDbSqlExecution(
                engine="duckdb_sql",
                code="CREATE TEMP TABLE products AS SELECT 'p-1' AS product_key;",
            ),
            "outputs": (
                ParquetOutputDeclaration(
                    payload_name="products",
                    format="parquet",
                    description="Valid products before invalid JSON.",
                    logical_key_fields=("product_key",),
                ),
                JsonOutputDeclaration(
                    payload_name="summary",
                    format="json",
                    description="Invalid JSON instance.",
                    json_kind="document",
                    schema_json='{"type":"object"}',
                ),
            ),
        }
    )

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, command)

    assert captured.value.code == "JSON_INSTANCE_INVALID"
    assert captured.value.phase == "output_validation"
    assert captured.value.operation_alias == "op_002"
    assert await _catalog_counts(runtime) == (1, 2)


async def test_code_validation_failure_persists_exact_source_description_and_no_package(
    tmp_path: Path,
) -> None:
    python_worker = FakeWorkerPort(
        failure=DataTransformWorkerError(
            "TRANSFORM_PYTHON_INVALID",
            '{"exception_type":"SyntaxError","line":2,"column":58}',
            phase="preflight",
            execution_started=False,
        )
    )
    runtime = await _runtime(tmp_path, FakeWorkerPort(), python_worker=python_worker)
    command = _python_command(runtime.source_package_alias).model_copy(
        update={
            "execution": PythonArrowExecution(
                engine="python_arrow",
                code=(
                    "def transform(ctx):\n"
                    "    ctx.write_table('selected_rows', ctx.table('entities')\n"
                ),
            )
        }
    )

    with pytest.raises(DataTransformApplicationError) as captured:
        await _transform(runtime, runtime.scope, command)

    assert captured.value.code == "TRANSFORM_PYTHON_INVALID"
    assert captured.value.phase == "code_validation"
    assert captured.value.operation_alias == "op_002"
    assert str(captured.value) == '{"exception_type":"SyntaxError","line":2,"column":58}'
    assert await _catalog_counts(runtime) == (1, 2)
    operation = await _latest_operation(runtime)
    record = operation.operation
    assert record.status is CriticalOperationStatus.FAILED
    assert record.error_phase is CriticalOperationErrorPhase.CODE_VALIDATION
    assert record.result_package_id is None
    assert record.code_artifact is not None
    assert record.description_artifact is not None

    source_stream = await runtime.artifacts.stream_version(
        runtime.scope.owner_id,
        record.code_artifact.artifact_id,
        record.code_artifact.version_id,
    )
    description_stream = await runtime.artifacts.stream_version(
        runtime.scope.owner_id,
        record.description_artifact.artifact_id,
        record.description_artifact.version_id,
    )
    assert b"".join([chunk async for chunk in source_stream.chunks]) == (
        command.execution.code.encode("utf-8")
    )
    assert b"".join([chunk async for chunk in description_stream.chunks]) == (
        DESCRIPTION.encode("utf-8")
    )


async def test_failed_transform_can_be_exactly_patched_replayed_and_published(
    tmp_path: Path,
) -> None:
    bad_literal = "ctx.write_table('selected_rows', ctx.table('entities')"
    good_literal = "ctx.write_table('selected_rows', ctx.table('entities'))"
    python_worker = FakeWorkerPort(
        failure=DataTransformWorkerError(
            "TRANSFORM_PYTHON_INVALID",
            '{"exception_type":"SyntaxError","line":2,"column":58}',
            phase="preflight",
            execution_started=False,
        )
    )
    runtime = await _runtime(tmp_path, FakeWorkerPort(), python_worker=python_worker)
    command = _python_command(runtime.source_package_alias).model_copy(
        update={
            "execution": PythonArrowExecution(
                engine="python_arrow",
                code=f"def transform(ctx):\n    {bad_literal}\n",
            )
        }
    )
    with pytest.raises(DataTransformApplicationError):
        await _transform(runtime, runtime.scope, command)
    parent = await _latest_operation(runtime)
    parent_record = parent.operation
    assert parent_record.code_artifact is not None
    assert parent_record.description_artifact is not None

    python_worker.failure = None
    repair = DataRepairTransformCommand(
        parent_operation_id=parent_record.operation_id,
        edits=(TransformSourceEdit(old_text=bad_literal, new_text=good_literal),),
    )
    request_identity = "test-exact-repair-replay"
    result = await _repair_transform(runtime,
        runtime.scope,
        parent,
        await _visible_packages(runtime),
        repair,
        request_identity=request_identity,
    )
    repaired_id = result.published.operation.operation.operation_id
    replayed = await _repair_transform(runtime,
        runtime.scope,
        parent,
        await _visible_packages(runtime),
        repair,
        request_identity=request_identity,
    )

    operation = runtime.repository._state.critical_operations[repaired_id]
    assert operation.status is CriticalOperationStatus.SUCCEEDED
    assert operation.tool_name == "data_derive"
    assert operation.repair_parent_operation_id == parent_record.operation_id
    assert operation.description_artifact == parent_record.description_artifact
    assert operation.code_artifact is not None
    assert operation.code_artifact.sha256 != parent_record.code_artifact.sha256
    assert replayed.published.operation.catalog_entry.alias == (
        result.published.operation.catalog_entry.alias
    )
    assert (
        replayed.published.package.catalog_entry.alias
        == result.published.package.catalog_entry.alias
    )
    assert len(python_worker.requests) == 2
    assert await _catalog_counts(runtime) == (2, 3)


async def test_succeeded_transform_can_be_revised_as_sibling_with_original_inputs(
    tmp_path: Path,
) -> None:
    python_worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, FakeWorkerPort(), python_worker=python_worker)
    command = _python_command(runtime.source_package_alias)
    original = await _transform(runtime, runtime.scope, command)
    parent = original.published.operation
    parent_record = parent.operation
    parent_detail = parent_record.detail
    assert isinstance(parent_detail, DataTransformOperationDetail)
    assert parent_record.code_artifact is not None
    assert parent_record.result_package_id is not None

    repair_command = DataRepairTransformCommand(
        parent_operation_id=parent_record.operation_id,
        edits=(
            TransformSourceEdit(
                old_text="def transform(ctx):",
                new_text="def transform(ctx):\n    # revised sibling attempt",
            ),
        ),
    )
    measurement_parent = parent.model_copy(
        update={
            "operation": parent_record.model_copy(update={"tool_name": "cad_compute_measurements"})
        }
    )
    with pytest.raises(DataTransformApplicationError) as invalid_parent:
        await _repair_transform(runtime,
            runtime.scope,
            measurement_parent,
            await _visible_packages(runtime),
            repair_command,
        )
    assert invalid_parent.value.code == "TRANSFORM_REPAIR_PARENT_INVALID"

    revised = await _repair_transform(runtime,
        runtime.scope,
        parent,
        await _visible_packages(runtime),
        repair_command,
    )

    revision = revised.published.operation.operation
    revision_detail = revision.detail
    assert isinstance(revision_detail, DataTransformOperationDetail)
    assert revision.status is CriticalOperationStatus.SUCCEEDED
    assert revision.tool_name == "data_derive"
    assert revision.repair_parent_operation_id == parent_record.operation_id
    assert revision_detail.inputs == parent_detail.inputs
    assert revision.result_package_id != parent_record.result_package_id
    assert all(
        binding.package_id != parent_record.result_package_id for binding in revision_detail.inputs
    )
    assert (
        await runtime.repository.get_package(
            runtime.scope,
            parent_record.result_package_id,
        )
        is not None
    )
    assert len(python_worker.requests) == 2
    assert await _catalog_counts(runtime) == (3, 3)


async def test_metadata_only_repair_reexecutes_with_new_description_and_exact_resume(tmp_path: Path) -> None:
    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, worker)
    original = await _transform(runtime, runtime.scope, _command(runtime.source_package_alias))
    parent = original.published.operation
    change = DataRepairTransformCommand.model_validate({
        "parent_operation_id": parent.operation.operation_id,
        "output_package": {"description_markdown": {"action": "edit", "edits": [{"old_text": DESCRIPTION.splitlines()[0], "new_text": "# Reviewed entities"}]}},
    })
    revised = await _repair_transform(runtime, runtime.scope, parent, await _visible_packages(runtime), change, request_identity="metadata-unit")
    record = revised.published.operation.operation
    assert record.description_artifact != parent.operation.description_artifact
    assert record.code_artifact.sha256 == parent.operation.code_artifact.sha256
    assert revised.published.package.package.package_id != original.published.package.package.package_id
    assert len(worker.requests) == 2
    state = await runtime.repository.get_data_transform_operation_by_identity(runtime.scope, "metadata-unit")
    assert isinstance(state.resolved_command["output_package"]["description_markdown"], str)
    assert isinstance(state.resolved_command["outputs"], list)
    old = await runtime.application.get_transform_input(runtime.scope, parent.catalog_entry.alias, input_kind="package_description", view=TextSourceView(view_mode="full"))
    new = await runtime.application.get_transform_input(runtime.scope, revised.published.operation.catalog_entry.alias, input_kind="package_description", view=TextSourceView(view_mode="search", text="Reviewed", context_lines=0))
    assert old.source.text == DESCRIPTION
    assert new.source.total_match_count == 1
    replay = await _repair_transform(runtime, runtime.scope, parent, await _visible_packages(runtime), change, request_identity="metadata-unit")
    assert replay.published.operation.operation.operation_id == record.operation_id
    assert len(worker.requests) == 2
    with pytest.raises(DataTransformApplicationError, match="must change"):
        await _repair_transform(runtime, runtime.scope, parent, await _visible_packages(runtime), DataRepairTransformCommand.model_validate({
            "parent_operation_id": parent.operation.operation_id, "output_package": {"name": original.published.package.package.name},
        }))


async def test_failed_transform_schema_edit_reads_saved_text_and_runs_validated_schema(tmp_path: Path) -> None:
    worker = FakeWorkerPort(pa.table({"json_value": ['{"count":1.5}']}), failure=DataTransformWorkerError("TRANSFORM_EXECUTION_FAILED", "original execution failed", phase="execution", execution_started=True))
    runtime = await _runtime(tmp_path, worker)
    schema = '{"type":"object","properties":{"count":{"type":"integer"}},"required":["count"]}'
    command = _command(runtime.source_package_alias).model_copy(update={"outputs": (JsonOutputDeclaration(payload_name="selected_rows", format="json", description="Summary", json_kind="document", schema_json=schema),)})
    with pytest.raises(DataTransformApplicationError):
        await _transform(runtime, runtime.scope, command)
    parent = await _latest_operation(runtime)
    saved = await runtime.application.get_transform_input(runtime.scope, parent.catalog_entry.alias, input_kind="output_schema", payload_name="selected_rows", view=TextSourceView(view_mode="full"))
    assert saved.source.text == schema
    worker.failure = None
    changes = DataRepairTransformCommand.model_validate({
        "parent_operation_id": parent.operation.operation_id,
        "outputs": {"action": "edit", "set": [{"payload_name": "selected_rows", "format": "json", "description": "Summary", "json_kind": "document", "schema_json": json.dumps({"action": "edit", "edits": [{"old_text": '"integer"', "new_text": '"number"'}]})}]},
    })
    revised = await _repair_transform(runtime, runtime.scope, parent, await _visible_packages(runtime), changes)
    assert revised.published.operation.operation.detail.outputs[0].schema_json == schema.replace('"integer"', '"number"')
    assert revised.published.operation.operation.description_artifact == parent.operation.description_artifact
    assert len(worker.requests) == 2
    before = await _catalog_counts(runtime)
    with pytest.raises(DataTransformApplicationError):
        await _repair_transform(runtime, runtime.scope, parent, await _visible_packages(runtime), DataRepairTransformCommand.model_validate({
            "parent_operation_id": parent.operation.operation_id, "outputs": {"action": "edit", "remove": ["selected_rows"]},
        }))
    assert await _catalog_counts(runtime) == before


@pytest.mark.parametrize("input_kind", ["package_description", "output_schema"])
async def test_transform_input_near_field_limit_requires_bounded_view_for_json_framing(tmp_path: Path, input_kind: str) -> None:
    from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
    worker = FakeWorkerPort(failure=DataTransformWorkerError("TRANSFORM_EXECUTION_FAILED", "Saved failed input", phase="execution", execution_started=True))
    runtime = await _runtime(tmp_path, worker)
    payload = _command(runtime.source_package_alias).model_dump(mode="python")
    if input_kind == "package_description":
        source = DESCRIPTION + ('"x"\n' * ((65_000 - len(DESCRIPTION.encode("utf-8"))) // 4))
        payload["output_package"]["description_markdown"] = source
        payload_name = None
    else:
        source = '{\n' + ('\n' * 64_950) + '"type":"object"\n}'
        payload["outputs"] = [{"payload_name": "selected_rows", "format": "json", "description": "Summary", "json_kind": "document", "schema_json": source}]
        payload_name = "selected_rows"
    with pytest.raises(DataTransformApplicationError):
        await _transform(runtime, runtime.scope, DataTransformCommand.model_validate(payload))
    parent = await _latest_operation(runtime)
    with pytest.raises(DataRuntimeApplicationError) as captured:
        await runtime.application.get_transform_input(runtime.scope, parent.catalog_entry.alias, input_kind=input_kind, payload_name=payload_name, view=TextSourceView(view_mode="full"))
    assert captured.value.code == "OPERATION_GET_RESPONSE_TOO_LARGE"
    selected = await runtime.application.get_transform_input(runtime.scope, parent.catalog_entry.alias, input_kind=input_kind, payload_name=payload_name, view=TextSourceView(view_mode="lines", start_line=1, line_count=2))
    assert selected.source.size_bytes == len(source.encode("utf-8"))
    assert [line.text for line in selected.source.lines] == source.splitlines(keepends=True)[:2]


async def test_repair_patch_precondition_failure_does_not_create_operation(
    tmp_path: Path,
) -> None:
    python_worker = FakeWorkerPort(
        failure=DataTransformWorkerError(
            "TRANSFORM_PYTHON_INVALID",
            "invalid source",
            phase="preflight",
            execution_started=False,
        )
    )
    runtime = await _runtime(tmp_path, FakeWorkerPort(), python_worker=python_worker)
    command = _python_command(runtime.source_package_alias)
    with pytest.raises(DataTransformApplicationError):
        await _transform(runtime, runtime.scope, command)
    parent = await _latest_operation(runtime)
    before = await _catalog_counts(runtime)

    with pytest.raises(DataTransformApplicationError) as captured:
        await _repair_transform(runtime,
            runtime.scope,
            parent,
            await _visible_packages(runtime),
            DataRepairTransformCommand(
                parent_operation_id=parent.operation.operation_id,
                edits=(TransformSourceEdit(old_text="no such literal", new_text="transform"),),
            ),
        )

    assert captured.value.code == "TRANSFORM_REPAIR_MATCH_NOT_FOUND"
    assert captured.value.operation_alias is None
    assert await _catalog_counts(runtime) == before


async def test_large_transform_source_saved_read_in_sections_and_repaired(tmp_path: Path) -> None:
    from CAD2BIM_backend.application.text_source_view import TextSourceViewService, TextSourceViewError
    from CAD2BIM_backend.domain.data_transform import OperationCodeReference
    from CAD2BIM_backend.domain.text_source import TextSourceView

    worker = FakeWorkerPort()
    runtime = await _runtime(tmp_path, FakeWorkerPort(), python_worker=worker)
    base = _python_command(runtime.source_package_alias)
    source = base.execution.code + "# capacity padding\n" * 4000
    assert 65_536 < len(source.encode("utf-8")) < 131_072
    command = base.model_copy(update={"execution": PythonArrowExecution(engine="python_arrow", code=source)})
    result = await runtime.service.transform(
        runtime.scope, command, request_identity="large-transform-source"
    )
    parent = result.published.operation
    exact = await runtime.application.get_transformation_code(runtime.scope, OperationCodeReference(
        reference_kind="operation", operation_alias=parent.catalog_entry.alias))
    assert exact.code == source
    view = TextSourceViewService()
    with pytest.raises(TextSourceViewError, match="use lines or search"):
        view.view(exact.code, TextSourceView(view_mode="full"))
    selected = view.view(exact.code, TextSourceView(view_mode="lines", start_line=1, line_count=3))
    assert [line.text for line in selected.lines] == source.splitlines(keepends=True)[:3]
    repair = DataRepairTransformCommand(
        parent_operation_id=parent.operation.operation_id,
        edits=(TransformSourceEdit(old_text="def transform(ctx):", new_text="def transform(ctx):  # repaired"),),
    )
    repaired = await _repair_transform(
        runtime, runtime.scope, parent, await _visible_packages(runtime), repair
    )
    current = await runtime.application.get_transformation_code(runtime.scope, OperationCodeReference(
        reference_kind="operation", operation_alias=repaired.published.operation.catalog_entry.alias))
    assert current.code == source.replace("def transform(ctx):", "def transform(ctx):  # repaired", 1)
    assert worker.requests[-1].code == current.code
    assert parent.operation.code_artifact != repaired.published.operation.operation.code_artifact


async def test_repair_failure_then_success_forms_immutable_parent_chain(
    tmp_path: Path,
) -> None:
    bad = "ctx.write_table('selected_rows', ctx.table('entities')"
    still_bad = "ctx.write_table('selected_rows', ctx.table('entities'))]"
    good = "ctx.write_table('selected_rows', ctx.table('entities'))"
    python_worker = FakeWorkerPort(
        failure=DataTransformWorkerError(
            "TRANSFORM_PYTHON_INVALID",
            "SyntaxError at repaired line",
            phase="preflight",
            execution_started=False,
        )
    )
    runtime = await _runtime(tmp_path, FakeWorkerPort(), python_worker=python_worker)
    original_command = _python_command(runtime.source_package_alias).model_copy(
        update={
            "execution": PythonArrowExecution(
                engine="python_arrow",
                code=f"def transform(ctx):\n    {bad}\n",
            )
        }
    )
    with pytest.raises(DataTransformApplicationError):
        await _transform(runtime, runtime.scope, original_command)
    original = await _latest_operation(runtime)
    assert original.operation.code_artifact is not None

    with pytest.raises(DataTransformApplicationError) as first_repair_error:
        await _repair_transform(runtime,
            runtime.scope,
            original,
            await _visible_packages(runtime),
            DataRepairTransformCommand(
                parent_operation_id=original.operation.operation_id,
                edits=(TransformSourceEdit(old_text=bad, new_text=still_bad),),
            ),
        )
    assert first_repair_error.value.operation_alias == "op_003"
    failed_repair = await _latest_operation(runtime)
    assert failed_repair.operation.tool_name == "data_derive"
    assert failed_repair.operation.repair_parent_operation_id == original.operation.operation_id
    assert failed_repair.operation.code_artifact is not None

    python_worker.failure = None
    success = await _repair_transform(runtime,
        runtime.scope,
        failed_repair,
        await _visible_packages(runtime),
        DataRepairTransformCommand(
            parent_operation_id=failed_repair.operation.operation_id,
            edits=(TransformSourceEdit(old_text=still_bad, new_text=good),),
        ),
    )
    final = success.published.operation.operation
    assert final.tool_name == "data_derive"
    assert final.repair_parent_operation_id == failed_repair.operation.operation_id
    assert final.status is CriticalOperationStatus.SUCCEEDED
    assert original.operation.status is CriticalOperationStatus.FAILED
    assert failed_repair.operation.status is CriticalOperationStatus.FAILED
    assert await _catalog_counts(runtime) == (2, 4)
