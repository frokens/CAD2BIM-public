from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import pytest
from fastmcp import Client

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.cad_measurement_service import (
    CadMeasurementApplicationError,
    CadMeasurementService,
)
from CAD2BIM_backend.application import cad_measurement_service as measurement_module
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.data_query_service import DataQueryService
from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
from CAD2BIM_backend.domain.cad_measurement import (
    CAD_MEASUREMENT_PAYLOAD_NAMES,
    CadAnalyzeToleranceCandidatesCommand,
    CadMeasurementErrorCode,
)
from CAD2BIM_backend.domain.data_runtime import (
    CatalogResourceKind,
    CriticalOperationStatus,
    PackageMemberKind,
    ValidatedSource,
)
from CAD2BIM_backend.domain.data_transform import OperationCodeReference
from CAD2BIM_backend.infrastructure.cad_measurement.supervised_worker import (
    CadMeasurementWorkerAnalysisResult,
    CadMeasurementWorkerError,
)
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import (
    InMemoryIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.memory.state import MemoryState
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.server import create_direct_mcp
from tests.integration.backend.test_m3_multi_cad_service import (
    FIXTURE_ROOT,
    _command,
    _InProcessMeasurementWorker,
)


pytestmark = pytest.mark.integration


class _AnalyzerWorker:
    def __init__(self) -> None:
        self.command = None

    async def analyze(self, tables, command, *, deadline):
        del tables, deadline
        self.command = command
        return CadMeasurementWorkerAnalysisResult(
            started_at=datetime.now(timezone.utc),
            payload={
                "coverage": {
                    "declared_path_layer_count": len(command.path_layers),
                    "declared_point_group_count": len(command.point_groups),
                    "selected_path_entity_count": 1,
                    "selected_point_entity_count": 0,
                    "atomic_edge_count": 1,
                    "endpoint_count": 2,
                    "ignored_non_path_entity_count": 0,
                    "ignored_non_point_entity_count": 0,
                    "unsupported_geometry_count": 0,
                    "nonzero_bulge_count": 0,
                    "nonfinite_coordinate_count": 0,
                    "zero_length_edge_count": 0,
                    "compute_blocked": False,
                },
                "distributions": [],
                "generated_probes": [],
                "trial_results": [],
                "diagnostic_samples": [],
                "suggested_starting_trial": None,
                "suggestion_reason": "no_explicit_trials",
            },
        )


class _FailingWorker:
    def __init__(self, *, started: bool) -> None:
        self.started = started

    async def compute(self, tables, command, *, deadline):
        del tables, command, deadline
        started_at = datetime.now(timezone.utc) if self.started else None
        raise CadMeasurementWorkerError(
            (
                CadMeasurementErrorCode.CAD_ZERO_LENGTH_EDGE.value
                if self.started
                else CadMeasurementErrorCode.CAPACITY_BUSY.value
            ),
            "deterministic lifecycle failure",
            phase="execution" if self.started else "preflight",
            execution_started=self.started,
            retryable=not self.started,
            started_at=started_at,
        )


class _CancellingWorker:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def compute(self, tables, command, *, deadline):
        del tables, command, deadline
        started_at = datetime.now(timezone.utc)
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            setattr(
                exc,
                "worker_failure",
                CadMeasurementWorkerError(
                    CadMeasurementErrorCode.TIMEOUT.value,
                    "cancelled",
                    phase="execution",
                    execution_started=True,
                    started_at=started_at,
                ),
            )
            raise


class _RepositoryProxy:
    def __init__(self, repository, *, persist_then_raise: bool) -> None:
        self._repository = repository
        self._persist_then_raise = persist_then_raise

    def __getattr__(self, name):
        return getattr(self._repository, name)

    async def publish_package_result(self, scope, package, members, operation):
        if self._persist_then_raise:
            await self._repository.publish_package_result(scope, package, members, operation)
        raise RuntimeError("simulated publication response loss")


class _BlockingPublicationRepository:
    def __init__(self, repository) -> None:
        self._repository = repository
        self.entered = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self._repository, name)

    async def publish_package_result(self, scope, package, members, operation):
        del scope, package, members, operation
        self.entered.set()
        await asyncio.Event().wait()


async def _runtime(tmp_path: Path, *, worker=None, repository=None):
    repository = repository or InMemoryControlPlaneRepository(MemoryState())
    identities = InMemoryIdentityDirectory()
    artifacts = ArtifactService(repository, LocalStorage(tmp_path / "storage"))
    runtime = DataRuntimeService(repository, artifacts)
    sessions = DirectSessionService(repository, identities, "m3-lifecycle-user")
    resolver = DirectScopeResolver(repository, identities, "m3-lifecycle-user")
    opened = await sessions.open_direct_session("M3 lifecycle")
    scope = await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id="command_m3_lifecycle",
    )
    source_path = FIXTURE_ROOT / "minimal_line.dxf"
    imported = await runtime.import_cad_source(
        scope,
        ValidatedSource(
            filename=source_path.name,
            media_type="application/dxf",
            content=source_path.read_bytes(),
            source_identity="test-fixture:m3-lifecycle/minimal_line.dxf",
        ),
    )
    service = CadMeasurementService(
        repository,
        artifacts,
        ExactPayloadResolver(repository, artifacts),
        worker=worker or _InProcessMeasurementWorker(),
    )
    return repository, runtime, scope, imported, service


async def _catalog_counts(repository, scope) -> tuple[int, int]:
    entries = await repository.list_catalog(scope)
    return (
        sum(item.resource_kind is CatalogResourceKind.PACKAGE for item in entries),
        sum(item.resource_kind is CatalogResourceKind.OPERATION for item in entries),
    )


async def test_analyzer_returns_bounded_facts_without_package_or_operation(
    tmp_path: Path,
) -> None:
    worker = _AnalyzerWorker()
    repository, _runtime_service, scope, imported, service = await _runtime(tmp_path, worker=worker)
    before = await _catalog_counts(repository, scope)
    result = await service.analyze(
        scope,
        CadAnalyzeToleranceCandidatesCommand(
            package_alias=imported.published.package.catalog_entry.alias,
            source_unit_to_m=1.0,
            path_layers=("0",),
            point_groups=(),
            trials=(),
        ),
    )

    assert result.document_key
    assert result.package_alias == imported.published.package.catalog_entry.alias
    assert await _catalog_counts(repository, scope) == before
    assert worker.command.path_layers == ("0",)


async def test_compute_success_is_exact_atomic_idempotent_and_code_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _InProcessMeasurementWorker()
    repository, runtime, scope, imported, service = await _runtime(tmp_path, worker=worker)
    command = _command(
        imported.published.package.catalog_entry.alias,
        "0",
        "M3 lifecycle measurements",
    )
    first = await service.compute(scope, command)
    # A completed request replays its persisted result after server code changes;
    # replay must not need to reconstruct the current implementation audit bundle.
    def unavailable_current_implementation() -> bytes:
        raise AssertionError("replay must not inspect the current implementation")

    monkeypatch.setattr(
        measurement_module, "_implementation_bundle_bytes", unavailable_current_implementation
    )
    second = await service.compute(scope, command)

    assert second.published == first.published
    members = first.published.package.members
    assert len(members) == 13
    assert tuple(item.logical_payload_name for item in first.output_members) == (
        CAD_MEASUREMENT_PAYLOAD_NAMES
    )
    assert {
        item.member_kind for item in members if item.member_kind is not PackageMemberKind.PAYLOAD
    } == {
        PackageMemberKind.CODE,
        PackageMemberKind.DESCRIPTION,
        PackageMemberKind.MANIFEST,
    }
    operation = first.published.operation
    assert operation.operation.status is CriticalOperationStatus.SUCCEEDED
    assert operation.operation.tool_name == "cad_compute_measurements"
    code = await runtime.get_transformation_code(
        scope,
        OperationCodeReference(
            reference_kind="operation",
            operation_alias=operation.catalog_entry.alias,
        ),
    )
    assert (code.engine, code.language, code.media_type) == (
        "cad_measurement",
        "python",
        "text/x-python",
    )
    from CAD2BIM_backend.infrastructure import cad_measurement
    source_directory = Path(cad_measurement.__file__).parent
    snapshot = {"__name__": "stored_measurement_sources"}
    exec(code.code, snapshot)
    decoded = snapshot["sources"]()
    assert decoded
    for name, content in decoded.items():
        assert content == (source_directory / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("started", [False, True])
async def test_compute_failure_operation_depends_only_on_worker_started_fact(
    tmp_path: Path,
    started: bool,
) -> None:
    worker = _FailingWorker(started=started)
    repository, _runtime_service, scope, imported, service = await _runtime(tmp_path, worker=worker)
    before = await _catalog_counts(repository, scope)
    with pytest.raises(CadMeasurementApplicationError) as captured:
        await service.compute(
            scope,
            _command(
                imported.published.package.catalog_entry.alias,
                "0",
                f"M3 failure started {started}",
            ),
        )
    after = await _catalog_counts(repository, scope)

    assert after[0] == before[0]
    assert after[1] == before[1] + int(started)
    assert captured.value.retryable is (not started)
    if started:
        assert captured.value.operation_alias is not None


async def test_publication_response_loss_reconciles_but_unknown_outcome_never_records_failed(
    tmp_path: Path,
) -> None:
    base = InMemoryControlPlaneRepository(MemoryState())
    persisted_proxy = _RepositoryProxy(base, persist_then_raise=True)
    repository, _runtime_service, scope, imported, service = await _runtime(
        tmp_path / "persisted",
        repository=persisted_proxy,
    )
    result = await service.compute(
        scope,
        _command(
            imported.published.package.catalog_entry.alias,
            "0",
            "M3 reconciled publication",
        ),
    )
    assert result.published.operation.operation.status is CriticalOperationStatus.SUCCEEDED

    empty_base = InMemoryControlPlaneRepository(MemoryState())
    uncertain_proxy = _RepositoryProxy(empty_base, persist_then_raise=False)
    repository, _runtime_service, scope, imported, service = await _runtime(
        tmp_path / "uncertain",
        repository=uncertain_proxy,
    )
    with pytest.raises(CadMeasurementApplicationError) as captured:
        await service.compute(
            scope,
            _command(
                imported.published.package.catalog_entry.alias,
                "0",
                "M3 uncertain publication",
            ),
            deadline=monotonic() + 1.0,
        )
    assert captured.value.code == CadMeasurementErrorCode.PUBLICATION_UNCERTAIN.value
    assert (await _catalog_counts(repository, scope))[1] == 1  # source import only


async def test_artifact_staging_failure_records_failed_operation_without_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _runtime_service, scope, imported, service = await _runtime(tmp_path)
    before = await _catalog_counts(repository, scope)

    async def fail_first_payload(*args, **kwargs):
        del args, kwargs
        raise CadMeasurementApplicationError(
            CadMeasurementErrorCode.SERVICE_UNAVAILABLE.value,
            "simulated payload staging failure",
            phase="publication",
            retryable=True,
        )

    monkeypatch.setattr(service, "_upload_path", fail_first_payload)
    with pytest.raises(CadMeasurementApplicationError) as captured:
        await service.compute(
            scope,
            _command(
                imported.published.package.catalog_entry.alias,
                "0",
                "M3 deterministic staging failure",
            ),
        )

    assert captured.value.code == CadMeasurementErrorCode.SERVICE_UNAVAILABLE.value
    assert captured.value.code != CadMeasurementErrorCode.PUBLICATION_UNCERTAIN.value
    assert captured.value.retryable is False
    assert captured.value.operation_alias is not None
    after = await _catalog_counts(repository, scope)
    assert after == (before[0], before[1] + 1)


async def test_artifact_staging_cancellation_records_failure_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _runtime_service, scope, imported, service = await _runtime(tmp_path)
    before = await _catalog_counts(repository, scope)
    entered = asyncio.Event()

    async def block_first_payload(*args, **kwargs):
        del args, kwargs
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_upload_path", block_first_payload)
    task = asyncio.create_task(
        service.compute(
            scope,
            _command(
                imported.published.package.catalog_entry.alias,
                "0",
                "M3 cancelled artifact staging",
            ),
        )
    )
    await asyncio.wait_for(entered.wait(), 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    after = await _catalog_counts(repository, scope)
    assert after == (before[0], before[1] + 1)


async def test_started_cancellation_propagates_after_recording_terminal_failure(
    tmp_path: Path,
) -> None:
    worker = _CancellingWorker()
    repository, _runtime_service, scope, imported, service = await _runtime(tmp_path, worker=worker)
    before = await _catalog_counts(repository, scope)
    task = asyncio.create_task(
        service.compute(
            scope,
            _command(
                imported.published.package.catalog_entry.alias,
                "0",
                "M3 cancelled execution",
            ),
        )
    )
    await asyncio.wait_for(worker.entered.wait(), 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await _catalog_counts(repository, scope))[1] == before[1] + 1


async def test_publication_cancellation_propagates_without_failed_operation(
    tmp_path: Path,
) -> None:
    base = InMemoryControlPlaneRepository(MemoryState())
    proxy = _BlockingPublicationRepository(base)
    repository, _runtime_service, scope, imported, service = await _runtime(
        tmp_path,
        repository=proxy,
    )
    before = await _catalog_counts(repository, scope)
    task = asyncio.create_task(
        service.compute(
            scope,
            _command(
                imported.published.package.catalog_entry.alias,
                "0",
                "M3 cancelled publication",
            ),
            deadline=monotonic() + 0.3,
        )
    )
    await asyncio.wait_for(proxy.entered.wait(), 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _catalog_counts(repository, scope) == before


async def test_input_arrow_budget_fails_prestart_without_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = replace(
        measurement_module.CAD_MEASUREMENT_COMPUTE_BUDGET,
        input_arrow_bytes=1,
    )
    monkeypatch.setattr(measurement_module, "CAD_MEASUREMENT_COMPUTE_BUDGET", budget)
    repository, _runtime_service, scope, imported, service = await _runtime(tmp_path)
    before = await _catalog_counts(repository, scope)
    with pytest.raises(CadMeasurementApplicationError) as captured:
        await service.compute(
            scope,
            _command(
                imported.published.package.catalog_entry.alias,
                "0",
                "M3 input budget failure",
            ),
        )
    assert captured.value.code == (
        CadMeasurementErrorCode.CAD_MEASUREMENT_INPUT_BUDGET_EXCEEDED.value
    )
    assert await _catalog_counts(repository, scope) == before


async def test_compute_rejects_derived_input_before_starting_worker(tmp_path: Path) -> None:
    repository, _runtime_service, scope, imported, service = await _runtime(tmp_path)
    result = await service.compute(
        scope,
        _command(
            imported.published.package.catalog_entry.alias,
            "0",
            "M3 source result",
        ),
    )
    before = await _catalog_counts(repository, scope)
    with pytest.raises(CadMeasurementApplicationError) as captured:
        await service.compute(
            scope,
            _command(
                result.published.package.catalog_entry.alias,
                "0",
                "M3 invalid derived input",
            ),
        )
    assert captured.value.code == CadMeasurementErrorCode.PACKAGE_KIND_MISMATCH.value
    assert await _catalog_counts(repository, scope) == before


async def test_fastmcp_direct_flow_runs_real_worker_and_publishes_exact_measurements(
    tmp_path: Path,
) -> None:
    repository = InMemoryControlPlaneRepository(MemoryState())
    identities = InMemoryIdentityDirectory()
    artifacts = ArtifactService(repository, LocalStorage(tmp_path / "storage"))
    runtime = DataRuntimeService(repository, artifacts)
    sessions = DirectSessionService(repository, identities, "m3-direct-user")
    resolver = DirectScopeResolver(repository, identities, "m3-direct-user")
    payloads = ExactPayloadResolver(repository, artifacts)
    measurement = CadMeasurementService(repository, artifacts, payloads)
    query = DataQueryService(
        repository,
        artifacts,
        CursorCodec("bTJiLXN0ZGlvLWN1cnNvci1rZXktMzItYnl0ZXMtbWluaW11bQ"),
    )
    server = create_direct_mcp(
        DirectApplicationFacade(
            runtime,
            sessions,
            resolver,
            query_service=query,
            cad_measurement_service=measurement,
        ),
    )

    async with Client(server) as client:
        assert "cad_import_source" not in {tool.name for tool in await client.list_tools()}
        opened = (
            await client.call_tool(
                "runtime_open_direct_session",
                {"session_name": "M3 real Direct flow"},
            )
        ).structured_content
        assert opened is not None
        direct_alias = opened["direct_session_alias"]
        # User upload is prepared by the backend before Agent discovery.
        source = FIXTURE_ROOT / "minimal_line.dxf"
        upload_scope = await resolver.resolve(direct_alias, command_id="upload-fixture")
        imported = await runtime.import_cad_source(upload_scope, ValidatedSource(
            filename=source.name, media_type="application/dxf",
            content=source.read_bytes(), source_identity=str(source), adapter_metadata={},
        ))
        source_alias = imported.published.package.catalog_entry.alias
        catalog = (
            await client.call_tool(
                "data_package_list",
                {"direct_session_alias": direct_alias},
            )
        ).structured_content
        assert catalog is not None and catalog["packages"][0]["package_alias"] == source_alias
        entities = (
            await client.call_tool(
                "cad_query_entities",
                {
                    "direct_session_alias": direct_alias,
                    "query": {
                        "mode": "count_by",
                        "count_by": "layer",
                        "package_alias": source_alias,
                    },
                },
            )
        ).structured_content
        assert entities is not None
        analysis = (
            await client.call_tool(
                "cad_analyze_tolerance_candidates",
                {
                    "direct_session_alias": direct_alias,
                    "package_alias": source_alias,
                    "source_unit_to_m": 1.0,
                    "path_layers": ["0"],
                    "point_groups": [],
                    "trials": [],
                },
                timeout=30,
            )
        ).structured_content
        assert analysis is not None and analysis["package_alias"] == source_alias
        computed = (
            await client.call_tool(
                "cad_compute_measurements",
                {
                    "direct_session_alias": direct_alias,
                    "package_alias": source_alias,
                    "source_unit_to_m": 1.0,
                    "path_layers": ["0"],
                    "point_scopes": [],
                    "tolerance_spec": {
                        "endpoint_quantization_m": 0.0001,
                        "endpoint_snap_candidate_m": 0.01,
                        "endpoint_on_path_candidate_m": 0.01,
                        "host_endpoint_guard_m": 0.01,
                        "point_target_uniqueness_margin_m": 0.005,
                        "straight_turn_tolerance_deg": 0.5,
                    },
                    "output_package": {
                        "name": "M3 Direct measurements",
                        "description_markdown": _command(
                            source_alias, "0", "M3 Direct measurements"
                        ).output_package.description_markdown,
                    },
                },
                timeout=30,
            )
        ).structured_content
        assert computed is not None
        assert tuple(item["payload_name"] for item in computed["outputs"]) == (
            CAD_MEASUREMENT_PAYLOAD_NAMES
        )
        described = (
            await client.call_tool(
                "data_describe_package",
                {
                    "direct_session_alias": direct_alias,
                    "package_alias": computed["package_alias"],
                },
            )
        ).structured_content
        assert described is not None
        described_names = tuple(item["payload_name"] for item in described["payloads"])
        assert len(described_names) == 10
        assert set(described_names) == set(CAD_MEASUREMENT_PAYLOAD_NAMES)
