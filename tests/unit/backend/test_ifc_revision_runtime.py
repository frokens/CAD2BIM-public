from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock
from datetime import datetime, timezone
from pathlib import Path

import pytest

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    IfcLengthUnit,
    IfcProjectUnitChange,
)
from CAD2BIM_backend.domain.ifc_model import InitializeIfcModelCommand
from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectBusinessError,
    IfcProjectPatch,
    UpdateIfcProjectCommand,
)
from CAD2BIM_backend.domain.ifc_spatial_structure import (
    ApplyIfcSpatialStructureCommand,
    CreateIfcSpatialOperation,
    MAX_IFC_SPATIAL_NODES,
    IfcProjectParentReference,
    IfcProjectWorldPlacement,
    IfcSpatialStructureListQuery,
)
from CAD2BIM_backend.infrastructure.ifc_revision_runtime import (
    IfcOpenShellRevisionRuntime,
)
from CAD2BIM_backend.infrastructure.ifc_revision_runtime.protocol import (
    MAX_IFC_REVISION_CONTROL_BYTES,
    MAX_IFC_REVISION_RESULT_BYTES,
)
from CAD2BIM_backend.infrastructure.worker_runtime import (
    MAX_CONTROL_BYTES,
    WorkerLaunchSpec,
    WorkerProcessBudget,
    WorkerProcessObservations,
    WorkerSupervisorError,
    WorkerTerminal,
    read_json_frame,
    write_json_frame,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import (
    ApplyIfcSpatialStructureRevisionRequest,
    ConfigureIfcGeometryBasisRevisionRequest,
    GeneratedIfcRevision,
    IfcProjectRevisionEffect,
    IfcRevisionRuntimeError,
    IfcSpatialRevisionEffect,
    InitializeIfcRevisionRequest,
    InspectedIfcRevision,
    InspectExactIfcRevisionRequest,
    UpdateIfcProjectRevisionRequest,
)


def test_candidate_capacity_error_preserves_actual_bytes_and_terminal_stage(monkeypatch):
    from CAD2BIM_backend.infrastructure.ifc_revision_runtime import child
    monkeypatch.setattr(child, "MAX_IFC_REVISION_INPUT_BYTES", 8)
    with pytest.raises(IfcRevisionRuntimeError) as caught:
        child._write_candidate(b"candidate")
    terminal_stream = io.BytesIO()
    with pytest.raises(SystemExit):
        child._fail(terminal_stream, caught.value, execution_started=True)
    terminal_stream.seek(0)
    terminal = read_json_frame(terminal_stream, limit=child.MAX_STATUS_BYTES)
    assert terminal["code"] == "IFC_REVISION_CANDIDATE_TOO_LARGE"
    assert terminal["phase"] == "candidate_save" and terminal["retryable"] is False
    assert terminal["diagnostic"] == {"actual_bytes": 9, "limit_bytes": 8}
    assert "9 bytes" in terminal["message"] and "8 bytes" in terminal["message"]


def test_exact_model_inspection_does_not_load_unused_geometry_engines(tmp_path):
    from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec

    content = IfcOpenShellModelCodec().create_revision1(
        project_global_id=PROJECT_GLOBAL_ID, project_name=PROJECT_NAME,
        header_timestamp=HEADER_TIMESTAMP,
    ).content
    source = tmp_path / "inspect.ifc"
    source.write_bytes(content)
    # A fresh interpreter observes the real import closure, independently of
    # geometry modules imported by other tests in this process.
    program = """
import hashlib, json, os, sys
sys.path.insert(0, sys.argv[1])
from CAD2BIM_backend.infrastructure.ifc_revision_runtime import child
with open(sys.argv[2], "rb") as source:
    content = source.read()
    source.seek(0)
    result, _ = child._execute({"kind": "inspect_exact",
        "source": {"fd": os.dup(source.fileno()), "size_bytes": len(content),
                   "sha256": hashlib.sha256(content).hexdigest()}})
print(json.dumps({"inspection": result["inspection"], "loaded": list(sys.modules)}))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program,
         str(Path(__file__).resolve().parents[3] / "src"), str(source)],
        check=True, capture_output=True, text=True, timeout=20,
    )
    result = json.loads(completed.stdout)
    assert result["inspection"]["project_global_id"] == PROJECT_GLOBAL_ID
    assert result["inspection"]["sha256"] == hashlib.sha256(content).hexdigest()
    unused = {
        "cadquery", "ifcopenshell.geom", "jsonschema",
        "CAD2BIM_backend.infrastructure.ifc.product_compilation",
        "CAD2BIM_backend.infrastructure.ifc.project",
        "CAD2BIM_backend.infrastructure.ifc.spatial_structure",
    }
    assert not unused.intersection(result["loaded"])


@pytest.mark.parametrize("failure", ("digest", "native"))
def test_exact_source_descriptor_closes_on_read_or_native_failure(tmp_path, monkeypatch, failure):
    from CAD2BIM_backend.domain.ifc_model import IfcModelValidationError
    from CAD2BIM_backend.infrastructure.ifc_revision_runtime import child

    content = b"ISO-10303-21;"
    source = tmp_path / "source.ifc"
    source.write_bytes(content)
    descriptor = os.open(source, os.O_RDONLY)
    source.unlink()
    def fail_native(*args, **kwargs):
        raise IfcModelValidationError("native failure")
    monkeypatch.setattr(child.IfcOpenShellModelCodec, "private_copy_from_source_fd", fail_native)
    control = {"kind": "inspect_exact", "source": {
        "fd": descriptor, "size_bytes": len(content),
        "sha256": "wrong" if failure == "digest" else hashlib.sha256(content).hexdigest(),
    }}
    with pytest.raises(ValueError if failure == "digest" else IfcModelValidationError):
        child._execute(control)
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.asyncio
async def test_product_stream_preserves_required_null_summary_and_cached_source(tmp_path):
    from types import SimpleNamespace
    import cadquery as cq
    from CAD2BIM_backend.domain.ifc_product_compilation import CompileIfcProductsCommand, IfcProductCompilationBlock
    from CAD2BIM_backend.infrastructure.geometry_runtime.codec import reopen_geometry
    from CAD2BIM_backend.ports.ifc_revision_runtime import CompileIfcProductsRevisionRequest
    from tests.unit.backend.test_ifc_product_compilation import canonical, family, item

    geometry = canonical(cq.Workplane("XY").box(0.2, 0.1, 0.1).val())
    _, summary = reopen_geometry(geometry)
    geometry = geometry.model_copy(update={"summary": summary})
    blocks = tuple(IfcProductCompilationBlock(items=(item(family(), value, key=key),))
                   for key, value in (("p1", geometry), ("p2", None)))

    async def produce():
        for block in blocks:
            yield block

    request = CompileIfcProductsRevisionRequest(
        command=CompileIfcProductsCommand(family_contract=family()), blocks=produce())
    read_fd, write_fd = os.pipe()
    received = []

    def consume():
        with os.fdopen(read_fd, "rb") as stream:
            for index in range(2):
                frame = read_json_frame(stream, limit=64 * 1024**2)
                assert frame["index"] == index and frame["status"] == "block"
                received.append(IfcProductCompilationBlock.model_validate(frame["block"]))
            assert read_json_frame(stream, limit=64 * 1024**2) == {"status": "end", "count": 2}
        return SimpleNamespace(returncode=0)

    runtime = _runtime(tmp_path)
    try:
        await runtime._run_product_stream(asyncio.to_thread(consume), request, write_fd)
    finally:
        await runtime.close()
    source1, source2 = (block.items[0].fragment_sources[0] for block in received)
    assert source1.geometry.summary.topology.triangle_count is None
    assert source1.geometry.payload == geometry.payload and source1.source_key == source2.source_key
    assert source2.geometry is None


PROJECT_GLOBAL_ID = "1xvJu1wbjMywJMjRBpLw$1"
PROJECT_NAME = "Revision Runtime Test"
HEADER_TIMESTAMP = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("prior_execution", [False, True])
async def test_capacity_failure_preserves_previous_invocation_work(tmp_path, monkeypatch, prior_execution):
    from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_INVOCATION_EXECUTION
    from CAD2BIM_backend.infrastructure.worker_runtime import WorkerCapacityBusy

    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        runtime._capacity, "acquire_host",
        AsyncMock(side_effect=WorkerCapacityBusy("full", reason="queue_full")),
    )
    async with runtime.invocation_scope(IFC_REVISION_INVOCATION_EXECUTION) as invocation:
        invocation.execution_started = prior_execution
        with pytest.raises(IfcRevisionRuntimeError) as caught:
            await runtime.execute(InitializeIfcRevisionRequest(
                command=InitializeIfcModelCommand(project_name=PROJECT_NAME),
                project_global_id=PROJECT_GLOBAL_ID, header_timestamp=HEADER_TIMESTAMP,
            ))
    assert caught.value.execution_started is prior_execution
    assert caught.value.diagnostic["execution_started"] is prior_execution
    assert caught.value.diagnostic["current_stage_started"] is False
    assert caught.value.diagnostic["reason"] == "queue_full"


class _BytesSource:
    def __init__(self, content: bytes, *, sha256: str | None = None) -> None:
        self.content = content
        self.size_bytes = len(content)
        self.sha256 = sha256 or hashlib.sha256(content).hexdigest()
        self.iteration_count = 0

    async def iter_chunks(self, *, chunk_size: int = 1024 * 1024):  # type: ignore[no-untyped-def]
        self.iteration_count += 1
        for offset in range(0, len(self.content), min(chunk_size, 17)):
            yield self.content[offset : offset + min(chunk_size, 17)]


def _runtime(
    tmp_path: Path,
    *,
    supervisor: object | None = None,
) -> IfcOpenShellRevisionRuntime:
    return IfcOpenShellRevisionRuntime(
        python_executable=os.environ.get(
            "CAD2BIM_TEST_IFC_REVISION_PYTHON_EXECUTABLE",
            sys.executable,
        ),
        capacity_root=tmp_path / "capacity",
        supervisor=supervisor,  # type: ignore[arg-type]
    )


async def _read_candidate(result: GeneratedIfcRevision) -> bytes:
    return b"".join([chunk async for chunk in result.candidate.iter_chunks(chunk_size=23)])


def _placement() -> IfcProjectWorldPlacement:
    return IfcProjectWorldPlacement(
        origin=(0.0, 0.0, 0.0),
        axis=(0.0, 0.0, 1.0),
        ref_direction=(1.0, 0.0, 0.0),
    )


def test_closed_request_union_rejects_partial_revision1_expectation() -> None:
    with pytest.raises(ValueError, match="provided together"):
        InspectExactIfcRevisionRequest(expected_project_global_id=PROJECT_GLOBAL_ID)
    with pytest.raises(ValueError, match="timezone-aware"):
        InitializeIfcRevisionRequest(
            command=InitializeIfcModelCommand(project_name=PROJECT_NAME),
            project_global_id=PROJECT_GLOBAL_ID,
            header_timestamp=datetime(2026, 9, 4),
        )


@pytest.mark.asyncio
async def test_runtime_executes_all_closed_commands_and_candidate_is_repeatable(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    try:
        initialized = await runtime.execute(
            InitializeIfcRevisionRequest(
                command=InitializeIfcModelCommand(project_name=PROJECT_NAME),
                project_global_id=PROJECT_GLOBAL_ID,
                header_timestamp=HEADER_TIMESTAMP,
            )
        )
        assert isinstance(initialized, GeneratedIfcRevision)
        revision1 = await _read_candidate(initialized)
        assert revision1 == await _read_candidate(initialized)
        assert hashlib.sha256(revision1).hexdigest() == initialized.candidate.sha256

        inspected = await runtime.execute(
            InspectExactIfcRevisionRequest(
                expected_project_global_id=PROJECT_GLOBAL_ID,
                expected_project_name=PROJECT_NAME,
            ),
            source=_BytesSource(revision1),
        )
        assert isinstance(inspected, InspectedIfcRevision)
        assert inspected.inspection == initialized.inspection
        await initialized.candidate.aclose()

        geometry = await runtime.execute(
            ConfigureIfcGeometryBasisRevisionRequest(
                command=ConfigureIfcGeometryBasisCommand(
                    project_unit_change=IfcProjectUnitChange(
                        length_unit=IfcLengthUnit(prefix="MILLI")
                    )
                )
            ),
            source=_BytesSource(revision1),
        )
        assert isinstance(geometry, GeneratedIfcRevision)
        geometry_bytes = await _read_candidate(geometry)
        assert geometry.inspection.unit_assignment_count == 1
        assert geometry.effect is None
        await geometry.candidate.aclose()

        project = await runtime.execute(
            UpdateIfcProjectRevisionRequest(
                command=UpdateIfcProjectCommand(
                    patch=IfcProjectPatch(name="Updated Runtime Project", phase="Design")
                )
            ),
            source=_BytesSource(geometry_bytes),
        )
        assert isinstance(project, GeneratedIfcRevision)
        assert isinstance(project.effect, IfcProjectRevisionEffect)
        assert project.effect.project.name == "Updated Runtime Project"
        project_bytes = await _read_candidate(project)
        await project.candidate.aclose()

        spatial = await runtime.execute(
            ApplyIfcSpatialStructureRevisionRequest(
                command=ApplyIfcSpatialStructureCommand(
                    operations=(
                        CreateIfcSpatialOperation(
                            client_ref="site",
                            spatial_class="site",
                            parent=IfcProjectParentReference(),
                            project_world_placement=_placement(),
                            name="Runtime Site",
                            composition_type="ELEMENT",
                        ),
                    )
                )
            ),
            source=_BytesSource(project_bytes),
        )
        assert isinstance(spatial, GeneratedIfcRevision)
        assert isinstance(spatial.effect, IfcSpatialRevisionEffect)
        assert spatial.effect.created[0].client_ref == "site"
        assert spatial.effect.spatial_summary[0].name == "Runtime Site"
        assert spatial.effect.affected_global_ids == (
            spatial.effect.spatial_summary[0].global_id,
        )
        spatial_bytes = await _read_candidate(spatial)
        contexts = await runtime.execute(
            InspectExactIfcRevisionRequest(projection="contexts"), source=_BytesSource(spatial_bytes),
        )
        assert isinstance(contexts, InspectedIfcRevision)
        assert contexts.inspection.sha256 == hashlib.sha256(spatial_bytes).hexdigest()
        assert contexts.contexts is not None
        assert contexts.spatial_page is None
        page = await runtime.execute(
            InspectExactIfcRevisionRequest(projection="spatial", spatial_query=IfcSpatialStructureListQuery(page_size=1)),
            source=_BytesSource(spatial_bytes),
        )
        assert isinstance(page, InspectedIfcRevision)
        assert page.inspection == contexts.inspection
        assert page.contexts is None
        assert page.spatial_page is not None
        assert page.spatial_page.returned_count == 1
        assert page.spatial_page.items[0].name == "Runtime Site"
        await spatial.candidate.aclose()
        assert spatial.candidate.closed is True
        with pytest.raises(IfcRevisionRuntimeError) as closed:
            await _read_candidate(spatial)
        assert closed.value.code == "IFC_REVISION_CANDIDATE_CLOSED"
    finally:
        await runtime.close()


def test_ifc_protocol_budgets_cover_the_closed_spatial_domain_envelope() -> None:
    command = ApplyIfcSpatialStructureCommand(
        operations=tuple(
            CreateIfcSpatialOperation(
                client_ref=f"site_{index}",
                spatial_class="site",
                parent=IfcProjectParentReference(),
                project_world_placement=_placement(),
                name=f"Site {index}",
                description="界" * 4096,
                object_type="类" * 255,
                long_name="名" * 255,
                composition_type="ELEMENT",
            )
            for index in range(256)
        )
    )
    frame = io.BytesIO()
    write_json_frame(
        frame,
        {
            "kind": "apply_spatial_structure",
            "source": {"fd": 1, "size_bytes": 1, "sha256": "0" * 64},
            "command": command.model_dump(mode="json", exclude_none=True),
        },
        limit=MAX_IFC_REVISION_CONTROL_BYTES,
    )
    assert frame.tell() > MAX_CONTROL_BYTES
    # An affected summary can contain four bounded public text values plus
    # placement, relationship and blocker metadata.  Reserve 24 KiB per node
    # for the current 4,096-node closed catalog envelope.
    assert MAX_IFC_REVISION_RESULT_BYTES >= MAX_IFC_SPATIAL_NODES * 24 * 1024


@pytest.mark.asyncio
async def test_runtime_rejects_tampered_source_before_worker_execution(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    source = _BytesSource(b"ISO-10303-21;", sha256="0" * 64)
    try:
        with pytest.raises(IfcRevisionRuntimeError) as rejected:
            await runtime.execute(InspectExactIfcRevisionRequest(), source=source)
    finally:
        await runtime.close()
    assert source.iteration_count == 1
    assert rejected.value.code == "IFC_REVISION_SOURCE_INVALID"
    assert rejected.value.phase == "input_validation"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_business_failure_preserves_code_and_does_not_return_candidate(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    initialized: GeneratedIfcRevision | None = None
    try:
        result = await runtime.execute(
            InitializeIfcRevisionRequest(
                command=InitializeIfcModelCommand(project_name=PROJECT_NAME),
                project_global_id=PROJECT_GLOBAL_ID,
                header_timestamp=HEADER_TIMESTAMP,
            )
        )
        assert isinstance(result, GeneratedIfcRevision)
        initialized = result
        revision1 = await _read_candidate(result)
        with pytest.raises(IfcRevisionRuntimeError) as rejected:
            await runtime.execute(
                UpdateIfcProjectRevisionRequest(
                    command=UpdateIfcProjectCommand(
                        patch=IfcProjectPatch(name=PROJECT_NAME)
                    )
                ),
                source=_BytesSource(revision1),
            )
    finally:
        if initialized is not None:
            await initialized.candidate.aclose()
        await runtime.close()
    assert rejected.value.code == "IFC_NO_CHANGES"
    assert isinstance(rejected.value, IfcProjectBusinessError)
    assert rejected.value.phase == "business"
    assert rejected.value.execution_started is True
    assert rejected.value.retryable is False


@pytest.mark.asyncio
async def test_verify_ready_uses_real_child_and_close_fails_closed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    await runtime.verify_ready()
    await runtime.close()
    with pytest.raises(IfcRevisionRuntimeError) as closed:
        await runtime.verify_ready()
    assert closed.value.code == "IFC_REVISION_SERVICE_UNAVAILABLE"
    assert closed.value.retryable is True


class _FailingSupervisor:
    def __init__(self, code: str, *, execution_started: bool) -> None:
        self.code = code
        self.execution_started = execution_started
        self.scratch: Path | None = None
        self.source_name_absent = False
        self.control: dict[str, object] | None = None

    async def run(
        self,
        spec: WorkerLaunchSpec,
        *,
        budget: WorkerProcessBudget,
        deadline: float,
        on_guard_registered=None,
    ) -> WorkerTerminal:
        del budget, deadline
        self.scratch = spec.scratch
        assert spec.scratch is not None
        self.source_name_absent = not (spec.scratch / "source.ifc").exists()
        descriptor = int(spec.env["CAD2BIM_IFC_REVISION_CONTROL_FD"])
        with os.fdopen(os.dup(descriptor), "rb", buffering=0) as stream:
            self.control = read_json_frame(
                stream,
                limit=MAX_IFC_REVISION_CONTROL_BYTES,
            )
        source_fd = self.control["source"]["fd"]
        assert source_fd in spec.pass_fds
        assert f'(allow file-read-metadata (literal "/dev/fd/{source_fd}"))' in spec.command[2]
        raise WorkerSupervisorError(
            self.code,
            "simulated bounded worker failure",
            execution_started=self.execution_started,
        )


class _CrashedSupervisor:
    def __init__(self) -> None:
        self.scratch: Path | None = None

    async def run(
        self,
        spec: WorkerLaunchSpec,
        *,
        budget: WorkerProcessBudget,
        deadline: float,
        on_guard_registered=None,
    ) -> WorkerTerminal:
        del budget, deadline
        self.scratch = spec.scratch
        return WorkerTerminal(
            returncode=-11,
            status_payload=None,
            stdout=b"",
            stderr=b"",
            execution_started=True,
            observations=WorkerProcessObservations(),
        )


class _BlockingSupervisor:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.never = asyncio.Event()
        self.scratch: Path | None = None

    async def run(
        self,
        spec: WorkerLaunchSpec,
        *,
        budget: WorkerProcessBudget,
        deadline: float,
        on_guard_registered=None,
    ) -> WorkerTerminal:
        del budget, deadline
        self.scratch = spec.scratch
        self.entered.set()
        await self.never.wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supervisor_code", "expected_code", "execution_started"),
    [
        ("WORKER_TIMEOUT", "IFC_REVISION_TIMEOUT", True),
        ("WORKER_RESOURCE_LIMIT", "IFC_REVISION_RESOURCE_LIMIT", True),
    ],
)
async def test_supervisor_failures_are_typed_and_clean_private_scratch(
    tmp_path: Path,
    supervisor_code: str,
    expected_code: str,
    execution_started: bool,
) -> None:
    supervisor = _FailingSupervisor(
        supervisor_code,
        execution_started=execution_started,
    )
    runtime = _runtime(tmp_path, supervisor=supervisor)
    try:
        with pytest.raises(IfcRevisionRuntimeError) as rejected:
            await runtime.execute(
                InspectExactIfcRevisionRequest(),
                source=_BytesSource(b"ISO-10303-21;"),
            )
    finally:
        await runtime.close()
    assert rejected.value.code == expected_code
    assert rejected.value.execution_started is execution_started
    assert rejected.value.retryable is True
    assert supervisor.source_name_absent is True
    assert supervisor.scratch is not None and not supervisor.scratch.exists()
    assert supervisor.control is not None
    control_json = json.dumps(supervisor.control, sort_keys=True)
    assert not {
        "scope",
        "run_id",
        "operation",
        "artifact",
        "current",
        "storage_key",
        "provider_alias",
        "revision_id",
    }.intersection(control_json.lower().split('"'))


@pytest.mark.asyncio
async def test_native_crash_without_terminal_is_retryable_and_cleans_scratch(
    tmp_path: Path,
) -> None:
    supervisor = _CrashedSupervisor()
    runtime = _runtime(tmp_path, supervisor=supervisor)
    try:
        with pytest.raises(IfcRevisionRuntimeError) as rejected:
            await runtime.execute(
                InspectExactIfcRevisionRequest(),
                source=_BytesSource(b"ISO-10303-21;"),
            )
    finally:
        await runtime.close()
    assert rejected.value.code == "IFC_REVISION_WORKER_FAILED"
    assert rejected.value.execution_started is True
    assert rejected.value.retryable is True
    assert supervisor.scratch is not None and not supervisor.scratch.exists()


@pytest.mark.asyncio
async def test_cancellation_cleans_private_scratch(tmp_path: Path) -> None:
    supervisor = _BlockingSupervisor()
    runtime = _runtime(tmp_path, supervisor=supervisor)
    task = asyncio.create_task(
        runtime.execute(
            InspectExactIfcRevisionRequest(),
            source=_BytesSource(b"ISO-10303-21;"),
        )
    )
    entered = asyncio.create_task(supervisor.entered.wait())
    try:
        finished, _ = await asyncio.wait(
            (task, entered), timeout=10, return_when=asyncio.FIRST_COMPLETED
        )
        if task in finished:
            await task
            pytest.fail("IFC execution completed before entering the supervised worker")
        if entered not in finished:
            pytest.fail("IFC execution did not enter the supervised worker")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        entered.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(entered, task, return_exceptions=True)
        await runtime.close()
    assert supervisor.scratch is not None and not supervisor.scratch.exists()
