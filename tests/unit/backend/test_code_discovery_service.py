from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.application.code_discovery_service import (
    CodeDiscoveryApplicationError,
    CodeDiscoveryService,
    CodeKind,
    CodeListQuery,
    CodeReadSelection,
)
from CAD2BIM_backend.application.data_runtime_service import DataRuntimeApplicationError
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.data_runtime import (
    CriticalOperationErrorPhase,
    CriticalOperationKind,
    CriticalOperationStatus,
)
from CAD2BIM_backend.domain.geometry_query import GeometryQueryAttemptStatus
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.ifc_query import IfcQueryAttemptStatus
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_family import ProductFamilyVersionAssetRole
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind
from CAD2BIM_backend.domain.text_source import TextSourceFullResult, TextSourceView
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import (
    SourceTextGeometryAuthoringSource,
)
from CAD2BIM_backend.infrastructure.source_text.source_bundle import pack_source_bundle


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
SHA = "a" * 64
CODE = "def measure(ctx):\n    return []\n"
CODE_SHA = hashlib.sha256(CODE.encode()).hexdigest()
FAMILY_SOURCE = pack_source_bundle(
    {
        "main.py": b"from layout import solve\n\ndef realize(ctx):\n    return solve(ctx)\n",
        "layout.py": b"def solve(ctx):\n    return {'status': 'SOLVED'}\n",
    }
)
FAMILY_SOURCE_SHA = hashlib.sha256(FAMILY_SOURCE).hexdigest()


def _scope(*, run_id: str = "run-code", task_id: str = "task-code") -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-code",
            principal_id="principal-code",
            task_id=task_id,
            actor_kind=TenantActorKind.USER,
            command_id="command-code",
        ),
        owner_id="tenant-code",
        run_id=run_id,
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=10,
    )


def _codec() -> CursorCodec:
    return CursorCodec(base64.urlsafe_b64encode(b"c" * 32).rstrip(b"=").decode())


def _measurement_operation():
    operation = SimpleNamespace(
        operation_id="operation-measurement",
        operation_kind=CriticalOperationKind.DATA_TRANSFORM,
        tool_name="cad_compute_measurements",
        code_artifact=SimpleNamespace(sha256=CODE_SHA),
        status=CriticalOperationStatus.SUCCEEDED,
        error_code=None,
        error_message=None,
        result_package_id="package-measurement",
        repair_parent_operation_id=None,
        started_at=NOW,
        finished_at=NOW,
    )
    return SimpleNamespace(
        operation=operation,
        catalog_entry=SimpleNamespace(alias="op_004", ordinal=4),
    )


def _services():
    operation = _measurement_operation()
    data_runtime = SimpleNamespace(
        list_terminal_operation_catalog=AsyncMock(
            return_value=SimpleNamespace(
                operations=(operation,),
                result_packages={},
                complete=True,
                next_before_sequence=None,
            )
        ),
        get_transformation_code=AsyncMock(
            return_value=SimpleNamespace(
                engine="cad_measurement",
                language="python",
                code=CODE,
            )
        ),
    )
    geometry_queries = SimpleNamespace(
        list_terminal_geometry_query_attempts=AsyncMock(return_value=()),
        attempt_alias=lambda _scope, attempt: f"cur.{attempt.attempt_id}",
        attempt_alias_for_visible_id=AsyncMock(
            side_effect=lambda _scope, attempt_id: f"cur.{attempt_id}"
        ),
        resolve_attempt_alias=AsyncMock(return_value="geometry-attempt"),
        read_source=AsyncMock(
            return_value=TextSourceFullResult(
                sha256=SHA,
                size_bytes=24,
                total_lines=1,
                text="def query(ctx): return []\n",
            )
        ),
    )
    ifc_queries = SimpleNamespace(
        list_terminal_ifc_query_attempts=AsyncMock(return_value=()),
        attempt_alias=lambda _scope, attempt: f"cur.{attempt.attempt_id}",
        attempt_alias_for_visible_id=AsyncMock(
            side_effect=lambda _scope, attempt_id: f"cur.{attempt_id}"
        ),
        resolve_attempt_alias=AsyncMock(return_value="ifc-attempt"),
        read_source=AsyncMock(
            return_value=TextSourceFullResult(
                sha256=SHA,
                size_bytes=24,
                total_lines=1,
                text="def query(ctx): return []\n",
            )
        ),
    )
    return data_runtime, geometry_queries, ifc_queries


def _service() -> tuple[CodeDiscoveryService, object, object, object]:
    data_runtime, geometry_queries, ifc_queries = _services()
    service = CodeDiscoveryService(
        data_runtime,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        geometry_queries,
        ifc_queries,
        SimpleNamespace(),
        _codec(),
    )
    return service, data_runtime, geometry_queries, ifc_queries


async def test_lists_server_owned_measurement_without_creating_a_code_identity() -> None:
    service, data_runtime, _geometry, _ifc = _service()

    page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.CAD_MEASUREMENT_IMPLEMENTATION),
        None,
    )

    assert page.returned_count == 1
    card = page.codes[0]
    assert card.owner_alias == "op_004"
    assert card.source_sha256 == CODE_SHA
    assert card.ownership == "server"
    assert card.repairable is False
    assert card.result_alias is None
    assert card.code_locator.startswith("cur.")
    data_runtime.list_terminal_operation_catalog.assert_awaited_once_with(
        _scope(),
        tool_names=("cad_compute_measurements",),
        statuses=(CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED),
        before_sequence=None,
        page_size=20,
    )


async def test_operation_navigation_issues_a_readable_locator_without_rediscovery():
    service, runtime, _geometry, _ifc = _service()
    published = _measurement_operation()
    published.operation.tool_name = "data_derive"
    published.operation.run_id = _scope().run_id
    runtime.get_transformation_code.return_value = SimpleNamespace(engine="python_arrow", language="python", code=CODE)
    locator = service.data_operation_locator(_scope(), published)
    assert locator.startswith("cur.")
    runtime.list_terminal_operation_catalog.assert_not_awaited()
    source = await service.read_code(_scope(), locator, None, TextSourceView(view_mode="full"))
    assert source.view.text == CODE
    assert runtime.get_transformation_code.await_args.args[1].operation_alias == "op_004"
    assert service.data_operation_locator(_scope(run_id="different-run"), published) is None
    with pytest.raises(CodeDiscoveryApplicationError) as caught:
        await service.read_code(_scope(run_id="different-run"), locator, None, TextSourceView(view_mode="full"))
    assert caught.value.code == "CODE_LOCATOR_INVALID"


async def test_geometry_authoring_list_filters_non_code_operations_before_pagination() -> None:
    service, data_runtime, _geometry, _ifc = _service()
    data_runtime.list_terminal_operation_catalog.return_value = SimpleNamespace(
        operations=(),
        result_packages={},
        complete=True,
        next_before_sequence=None,
    )

    page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.GEOMETRY_AUTHORING_ATTEMPT),
        None,
    )

    assert page.codes == ()
    data_runtime.list_terminal_operation_catalog.assert_awaited_once_with(
        _scope(),
        tool_names=("ifc_geometry_resource_write",),
        statuses=(CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED),
        before_sequence=None,
        page_size=20,
        require_code_artifact=True,
    )


async def test_geometry_authoring_code_uses_the_current_source_owner():
    service, _runtime, _geometry, _ifc = _service()
    source = SimpleNamespace(
        bundle_sha256=SHA,
        members=(SimpleNamespace(source_path="main.py", sha256=SHA, size_bytes=20, text_available=True),),
    )
    service._geometry_authoring = SimpleNamespace(read_source=AsyncMock(return_value=source))
    service._geometry_resources = SimpleNamespace(get_exact_version=AsyncMock(return_value=None))
    operation = SimpleNamespace(
        operation_id="geometry-authoring", code_artifact=SimpleNamespace(sha256=SHA),
        finished_at=NOW, started_at=NOW, status=CriticalOperationStatus.SUCCEEDED,
        error_code=None, error_message=None, repair_parent_operation_id=None,
        result_geometry_resource_version_id="geometry-result", target_geometry_resource_id="geometry-root",
    )
    published = SimpleNamespace(
        operation=operation,
        catalog_entry=SimpleNamespace(alias="op_geometry", ordinal=1),
    )
    card = await service._authoring_operation_card(_scope(), published)
    assert card.repairable is False
    assert card.repair_entry is None
    assert card.repair_unavailable_reason == "owner_scope_read_only"
    service._geometry_resources.get_exact_version.assert_awaited_once()


async def test_code_read_reauthorizes_owner_and_rejects_cross_run_locator() -> None:
    service, data_runtime, _geometry, _ifc = _service()
    page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.CAD_MEASUREMENT_IMPLEMENTATION),
        None,
    )
    locator = page.codes[0].code_locator

    result = await service.read_code(_scope(), locator, None, TextSourceView(view_mode="full"))

    assert result.source_sha256 == CODE_SHA
    assert result.view.text == CODE
    reference = data_runtime.get_transformation_code.await_args.args[1]
    assert reference.operation_alias == "op_004"
    data_runtime.get_transformation_code.reset_mock()
    with pytest.raises(CodeDiscoveryApplicationError) as invalid_scope:
        await service.read_code(
            _scope(run_id="run-other"),
            locator,
            None,
            TextSourceView(view_mode="full"),
        )
    assert invalid_scope.value.code == "CODE_LOCATOR_INVALID"
    assert "run code_list again" in str(invalid_scope.value)
    assert "copy code_locator and source_form unchanged" in str(invalid_scope.value)
    assert "listed source_path for bundle code" in str(invalid_scope.value)
    data_runtime.get_transformation_code.assert_not_awaited()

    with pytest.raises(CodeDiscoveryApplicationError) as invalid_token:
        await service.read_code(
            _scope(),
            "cur.invalid",
            None,
            TextSourceView(view_mode="full"),
        )
    assert invalid_token.value.code == "CODE_LOCATOR_INVALID"
    assert str(invalid_token.value) == str(invalid_scope.value)
    data_runtime.get_transformation_code.assert_not_awaited()


async def test_code_batch_reauthorizes_each_owner_and_returns_no_partial_result() -> None:
    service, data_runtime, _geometry, _ifc = _service()
    page = await service.list_codes(
        _scope(), CodeListQuery(code_kind=CodeKind.CAD_MEASUREMENT_IMPLEMENTATION), None
    )
    first = CodeReadSelection(
        code_locator=page.codes[0].code_locator, view=TextSourceView(view_mode="full")
    )
    second = first.model_copy(
        update={"view": TextSourceView(view_mode="lines", start_line=1, line_count=1)}
    )
    batch = await service.read_code_batch(_scope(), (first, second))
    assert batch.reads[0].view.text == CODE
    assert batch.reads[1].source_sha256 == CODE_SHA
    assert data_runtime.get_transformation_code.await_count == 2
    with pytest.raises(CodeDiscoveryApplicationError) as duplicate:
        await service.read_code_batch(_scope(), (first, first))
    assert duplicate.value.code == "CODE_BATCH_INVALID"
    assert data_runtime.get_transformation_code.await_count == 2
    with pytest.raises(CodeDiscoveryApplicationError) as invalid:
        await service.read_code_batch(
            _scope(),
            (
                first,
                first.model_copy(update={"code_locator": "cur.invalid"}),
            ),
        )
    assert "reads[1]" in str(invalid.value)
    with pytest.raises(CodeDiscoveryApplicationError):
        await service.read_code_batch(_scope(run_id="other"), (first,))


async def test_code_batch_enforces_aggregate_response_budget(monkeypatch) -> None:
    service, _data_runtime, _geometry, _ifc = _service()
    page = await service.list_codes(
        _scope(), CodeListQuery(code_kind=CodeKind.CAD_MEASUREMENT_IMPLEMENTATION), None
    )
    monkeypatch.setattr(
        "CAD2BIM_backend.application.code_discovery_service.MAX_CODE_READ_BATCH_BYTES", 1
    )
    with pytest.raises(CodeDiscoveryApplicationError) as caught:
        await service.read_code_batch(
            _scope(),
            (
                CodeReadSelection(
                    code_locator=page.codes[0].code_locator, view=TextSourceView(view_mode="full")
                ),
            ),
        )
    assert caught.value.code == "CODE_BATCH_BUDGET_EXCEEDED"


async def test_package_code_locator_reauthorizes_exact_package_owner() -> None:
    service, data_runtime, _geometry, _ifc = _service()
    data_runtime.get_transformation_code.return_value = SimpleNamespace(
        engine="python",
        language="python",
        code=CODE,
    )
    locator = service._locator(
        _scope(),
        CodeKind.DATA_TRANSFORM_ATTEMPT,
        "data_transform_package",
        package_alias="data_012",
    )

    result = await service.read_code(_scope(), locator, None, TextSourceView(view_mode="full"))

    reference = data_runtime.get_transformation_code.await_args.args[1]
    assert reference.package_alias == "data_012"
    assert result.code_kind is CodeKind.DATA_TRANSFORM_ATTEMPT
    with pytest.raises(CodeDiscoveryApplicationError) as cross_run:
        await service.read_code(
            _scope(run_id="run-other"),
            locator,
            None,
            TextSourceView(view_mode="full"),
        )
    assert cross_run.value.code == "CODE_LOCATOR_INVALID"


async def test_geometry_query_locator_preserves_task_bound_owner_semantics() -> None:
    service, _data_runtime, geometry, _ifc = _service()
    attempt = SimpleNamespace(
        attempt_id="geometry-attempt",
        source_artifact=SimpleNamespace(sha256=SHA),
        status=GeometryQueryAttemptStatus.SUCCEEDED,
        diagnostics=(),
        repair_parent_attempt_id=None,
        created_at=NOW,
        finished_at=NOW,
    )
    geometry.list_terminal_geometry_query_attempts.return_value = (attempt,)
    page = await service.list_codes(
        _scope(run_id="run-parent"),
        CodeListQuery(code_kind=CodeKind.GEOMETRY_QUERY_ATTEMPT),
        None,
    )

    result = await service.read_code(
        _scope(run_id="run-child"),
        page.codes[0].code_locator,
        None,
        TextSourceView(view_mode="full"),
    )

    assert result.code_kind is CodeKind.GEOMETRY_QUERY_ATTEMPT
    geometry.resolve_attempt_alias.assert_awaited_once()
    geometry.read_source.assert_awaited_once()


async def test_list_requires_one_first_page_query_or_one_cursor() -> None:
    service, _data_runtime, _geometry, _ifc = _service()

    with pytest.raises(CodeDiscoveryApplicationError, match="exactly one"):
        await service.list_codes(_scope(), None, None)
    with pytest.raises(CodeDiscoveryApplicationError, match="exactly one"):
        await service.list_codes(
            _scope(),
            CodeListQuery(code_kind=CodeKind.DATA_TRANSFORM_ATTEMPT),
            "cur.duplicate",
        )


async def test_published_program_query_rejects_meaningless_status_filter() -> None:
    with pytest.raises(ValueError, match="do not accept"):
        CodeListQuery(
            code_kind=CodeKind.GEOMETRY_RESOURCE_PROGRAM,
            statuses=("succeeded",),
        )


async def test_locator_rejects_a_signed_code_kind_owner_kind_mismatch() -> None:
    service, _data_runtime, geometry, _ifc = _service()
    locator = service._locator(
        _scope(),
        CodeKind.IFC_QUERY_ATTEMPT,
        "geometry_query_attempt",
        attempt_alias="cur.geometry-attempt",
    )

    with pytest.raises(CodeDiscoveryApplicationError) as caught:
        await service.read_code(_scope(), locator, None, TextSourceView(view_mode="full"))

    assert caught.value.code == "CODE_LOCATOR_INVALID"
    geometry.resolve_attempt_alias.assert_not_awaited()


async def test_locator_rejects_owner_data_that_changed_to_another_code_kind() -> None:
    service, data_runtime, _geometry, _ifc = _service()
    locator = service._locator(
        _scope(),
        CodeKind.CAD_MEASUREMENT_IMPLEMENTATION,
        "data_transform_operation",
        operation_alias="op_004",
    )
    data_runtime.get_transformation_code.return_value = SimpleNamespace(
        engine="python",
        language="python",
        code=CODE,
    )

    with pytest.raises(CodeDiscoveryApplicationError) as caught:
        await service.read_code(_scope(), locator, None, TextSourceView(view_mode="full"))

    assert caught.value.code == "CODE_OWNER_INTEGRITY_ERROR"
    assert str(caught.value) == "The code locator no longer matches its exact owner"


async def test_published_geometry_program_is_navigation_only_not_repairable() -> None:
    data_runtime, geometry_queries, ifc_queries = _services()
    source = SimpleNamespace(
        bundle_sha256=SHA,
        members=(
            SimpleNamespace(
                source_path="main.py",
                size_bytes=24,
                sha256=SHA,
                text_available=True,
            ),
        ),
        selected_path=None,
        view=None,
    )
    bundle = SimpleNamespace(
        resource=SimpleNamespace(
            owner_scope=SimpleNamespace(scope_kind=SimpleNamespace(value="task"))
        ),
        version=SimpleNamespace(
            resource_version_id="version-program",
            version_no=3,
            created_at=NOW,
        ),
        assets=(
            SimpleNamespace(
                asset_role=GeometryResourceAssetRole.SOURCE_BUNDLE,
                sha256=SHA,
            ),
        ),
    )
    geometry_resources = SimpleNamespace(
        list_executable_program_versions=AsyncMock(return_value=(bundle,))
    )
    geometry_catalog = SimpleNamespace(version_ref=lambda _identity, _bundle: "gres_v1.program")
    geometry_sources = SimpleNamespace(read_source=AsyncMock(return_value=source))
    service = CodeDiscoveryService(
        data_runtime,
        geometry_resources,
        geometry_catalog,
        geometry_sources,
        geometry_queries,
        ifc_queries,
        SimpleNamespace(),
        _codec(),
    )

    page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.GEOMETRY_RESOURCE_PROGRAM),
        None,
    )

    assert page.codes[0].repairable is False
    assert page.codes[0].repair_entry is None


async def test_query_repair_cards_reauthorize_and_project_exact_parent_aliases() -> None:
    service, _data_runtime, geometry, ifc = _service()
    geometry_attempt = SimpleNamespace(
        attempt_id="geometry-child",
        source_artifact=SimpleNamespace(sha256=SHA),
        status=GeometryQueryAttemptStatus.FAILED,
        diagnostics=(),
        repair_parent_attempt_id="geometry-parent",
        created_at=NOW,
        finished_at=NOW,
    )
    ifc_attempt = SimpleNamespace(
        attempt_id="ifc-child",
        source_artifact=SimpleNamespace(sha256=SHA),
        status=IfcQueryAttemptStatus.FAILED,
        diagnostics=(),
        repair_parent_attempt_id="ifc-parent",
        created_at=NOW,
        finished_at=NOW,
    )
    geometry.list_terminal_geometry_query_attempts.return_value = (geometry_attempt,)
    ifc.list_terminal_ifc_query_attempts.return_value = (ifc_attempt,)

    geometry_page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.GEOMETRY_QUERY_ATTEMPT),
        None,
    )
    ifc_page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.IFC_QUERY_ATTEMPT),
        None,
    )

    assert geometry_page.codes[0].parent_owner_alias == "cur.geometry-parent"
    assert ifc_page.codes[0].parent_owner_alias == "cur.ifc-parent"
    geometry.attempt_alias_for_visible_id.assert_awaited_once_with(_scope(), "geometry-parent")
    ifc.attempt_alias_for_visible_id.assert_awaited_once_with(_scope(), "ifc-parent")


async def test_missing_query_repair_parent_is_reported_as_owner_integrity_failure() -> None:
    service, _data_runtime, geometry, _ifc = _service()
    geometry.list_terminal_geometry_query_attempts.return_value = (
        SimpleNamespace(
            attempt_id="geometry-child",
            source_artifact=SimpleNamespace(sha256=SHA),
            status=GeometryQueryAttemptStatus.FAILED,
            diagnostics=(),
            repair_parent_attempt_id="geometry-missing",
            created_at=NOW,
            finished_at=NOW,
        ),
    )
    geometry.attempt_alias_for_visible_id.side_effect = RuntimeError("hidden repository detail")

    with pytest.raises(CodeDiscoveryApplicationError) as caught:
        await service.list_codes(
            _scope(),
            CodeListQuery(code_kind=CodeKind.GEOMETRY_QUERY_ATTEMPT),
            None,
        )

    assert caught.value.code == "CODE_OWNER_INTEGRITY_ERROR"
    assert str(caught.value) == ("A repair geometry query Attempt lost its exact parent binding")
    assert isinstance(caught.value.__cause__, RuntimeError)


async def test_unknown_owner_failure_is_masked_and_retains_exception_cause() -> None:
    service, data_runtime, _geometry, _ifc = _service()
    data_runtime.list_terminal_operation_catalog.side_effect = RuntimeError(
        "secret database connection details"
    )

    with pytest.raises(CodeDiscoveryApplicationError) as caught:
        await service.list_codes(
            _scope(),
            CodeListQuery(code_kind=CodeKind.DATA_TRANSFORM_ATTEMPT),
            None,
        )

    assert caught.value.code == "CODE_OWNER_READ_FAILED"
    assert str(caught.value) == "The code owner could not be read"
    assert isinstance(caught.value.__cause__, RuntimeError)


async def test_large_transform_full_rejection_preserves_lines_recovery() -> None:
    service, data_runtime, _geometry, _ifc = _service()
    source = "# 计量 source line\n" * 4000
    assert 65_536 < len(source.encode("utf-8")) <= 131_072
    data_runtime.get_transformation_code.return_value = SimpleNamespace(
        engine="python_arrow", language="python", code=source,
    )
    locator = service._locator(
        _scope(), CodeKind.DATA_TRANSFORM_ATTEMPT, "data_transform_operation",
        operation_alias="op_012",
    )
    with pytest.raises(CodeDiscoveryApplicationError) as rejected:
        await service.read_code(_scope(), locator, None, TextSourceView(view_mode="full"))
    assert rejected.value.code == "CODE_SOURCE_READ_INVALID"
    assert str(rejected.value) == "source is too large for full view; use lines or search"
    reconstructed = []
    for start in range(1, len(source.splitlines()) + 1, 200):
        result = await service.read_code(
            _scope(), locator, None,
            TextSourceView(view_mode="lines", start_line=start, line_count=200),
        )
        reconstructed.extend(line.text for line in result.view.lines)
    assert "".join(reconstructed) == source


async def test_known_owner_application_failure_preserves_stable_code_and_bounded_message() -> None:
    service, data_runtime, _geometry, _ifc = _service()
    data_runtime.list_terminal_operation_catalog.side_effect = DataRuntimeApplicationError(
        "DATA_SCOPE_REJECTED", "A" * 3_000
    )

    with pytest.raises(CodeDiscoveryApplicationError) as caught:
        await service.list_codes(
            _scope(),
            CodeListQuery(code_kind=CodeKind.DATA_TRANSFORM_ATTEMPT),
            None,
        )

    assert caught.value.code == "DATA_SCOPE_REJECTED"
    assert len(str(caught.value).encode("utf-8")) <= 2_000
    assert isinstance(caught.value.__cause__, DataRuntimeApplicationError)


async def test_bundle_owner_reads_reauthorize_at_owner_boundary_and_keep_scope_rules() -> None:
    scope = _scope()
    projected = TextSourceFullResult(
        sha256=SHA,
        size_bytes=24,
        total_lines=1,
        text="def build(ctx): return []\n",
    )
    source = SimpleNamespace(
        bundle_sha256=SHA,
        members=(
            SimpleNamespace(
                source_path="main.py",
                size_bytes=24,
                sha256=SHA,
                text_available=True,
            ),
        ),
        selected_path="main.py",
        view=projected,
    )
    data_runtime, geometry_queries, ifc_queries = _services()
    data_runtime.get_operation = AsyncMock(
        return_value=SimpleNamespace(
            operation=SimpleNamespace(
                operation=SimpleNamespace(
                    operation_id="operation-authoring",
                    operation_kind=CriticalOperationKind.GEOMETRY_RESOURCE_WRITE,
                )
            )
        )
    )
    geometry_sources = SimpleNamespace(read_source=AsyncMock(return_value=source))
    geometry_authoring = SimpleNamespace(read_source=AsyncMock(return_value=source))
    service = CodeDiscoveryService(
        data_runtime,
        SimpleNamespace(),
        SimpleNamespace(),
        geometry_sources,
        geometry_queries,
        ifc_queries,
        geometry_authoring,
        _codec(),
    )
    authoring_locator = service._locator(
        scope,
        CodeKind.GEOMETRY_AUTHORING_ATTEMPT,
        "geometry_authoring_operation",
        operation_alias="op_authoring",
    )
    program_locator = service._locator(
        scope,
        CodeKind.GEOMETRY_RESOURCE_PROGRAM,
        "geometry_resource_version",
        resource_version_ref="gres_v1.program",
    )

    authoring = await service.read_code(
        scope,
        authoring_locator,
        "main.py",
        TextSourceView(view_mode="full"),
    )
    program = await service.read_code(
        scope,
        program_locator,
        "main.py",
        TextSourceView(view_mode="full"),
    )

    assert authoring.code_kind is CodeKind.GEOMETRY_AUTHORING_ATTEMPT
    assert program.code_kind is CodeKind.GEOMETRY_RESOURCE_PROGRAM
    data_runtime.get_operation.assert_awaited_once_with(scope, "op_authoring")
    geometry_authoring.read_source.assert_awaited_once_with(
        scope,
        operation_id="operation-authoring",
        source_path="main.py",
        view=TextSourceView(view_mode="full"),
    )
    geometry_sources.read_source.assert_awaited_once_with(
        scope.identity,
        "gres_v1.program",
        "main.py",
        TextSourceView(view_mode="full"),
    )

    with pytest.raises(CodeDiscoveryApplicationError) as authoring_cross_run:
        await service.read_code(
            _scope(run_id="run-other"),
            authoring_locator,
            "main.py",
            TextSourceView(view_mode="full"),
        )
    assert authoring_cross_run.value.code == "CODE_LOCATOR_INVALID"

    # Published resources are intentionally Run-independent but remain Task-bound.
    await service.read_code(
        _scope(run_id="run-other"),
        program_locator,
        "main.py",
        TextSourceView(view_mode="full"),
    )
    with pytest.raises(CodeDiscoveryApplicationError) as program_cross_task:
        await service.read_code(
            _scope(task_id="task-other"),
            program_locator,
            "main.py",
            TextSourceView(view_mode="full"),
        )
    assert program_cross_task.value.code == "CODE_LOCATOR_INVALID"


async def test_ifc_query_owner_read_is_run_bound_and_reauthorized() -> None:
    service, _data_runtime, _geometry, ifc = _service()
    locator = service._locator(
        _scope(),
        CodeKind.IFC_QUERY_ATTEMPT,
        "ifc_query_attempt",
        attempt_alias="cur.ifc-attempt",
    )

    result = await service.read_code(_scope(), locator, None, TextSourceView(view_mode="full"))

    assert result.code_kind is CodeKind.IFC_QUERY_ATTEMPT
    ifc.resolve_attempt_alias.assert_awaited_once_with(_scope(), "cur.ifc-attempt")
    ifc.read_source.assert_awaited_once_with(
        _scope(), "ifc-attempt", TextSourceView(view_mode="full")
    )
    with pytest.raises(CodeDiscoveryApplicationError) as cross_run:
        await service.read_code(
            _scope(run_id="run-other"),
            locator,
            None,
            TextSourceView(view_mode="full"),
        )
    assert cross_run.value.code == "CODE_LOCATOR_INVALID"


def _product_family_service(
    *,
    operation_status=CriticalOperationStatus.FAILED,
    error_phase=CriticalOperationErrorPhase.OUTPUT_VALIDATION,
    detail_kind="authoring_draft_write",
):
    data_runtime, geometry_queries, ifc_queries = _services()
    operation = SimpleNamespace(
        operation_id="operation-family",
        parent_owner_alias=None,
        operation_kind=CriticalOperationKind.PRODUCT_FAMILY_WRITE,
        detail_kind=detail_kind,
        tool_name="ifc_product_family_write",
        code_artifact=SimpleNamespace(
            artifact_id="artifact-family",
            version_id="artifact-family-version",
            sha256=FAMILY_SOURCE_SHA,
        ),
        status=operation_status,
        error_code="PRODUCT_FAMILY_PROGRAM_INVALID"
        if operation_status is CriticalOperationStatus.FAILED
        else None,
        error_message="program output is invalid"
        if operation_status is CriticalOperationStatus.FAILED
        else None,
        error_phase=(error_phase if operation_status is CriticalOperationStatus.FAILED else None),
        repair_parent_operation_id=None,
        result_product_family_version_id=(
            "family-version-1" if operation_status is CriticalOperationStatus.SUCCEEDED else None
        ),
        started_at=NOW,
        finished_at=NOW,
    )
    operation.catalog_entry = SimpleNamespace(alias="op_family", ordinal=11)
    data_runtime.list_product_family_operation_sources = AsyncMock(return_value=SimpleNamespace(
        sources=(operation,), complete=True, next_before_sequence=None,
    ))
    data_runtime.get_product_family_operation_source = AsyncMock(return_value=operation)
    source_bundles = SourceTextGeometryAuthoringSource()
    from CAD2BIM_backend.domain.authoring_draft_content import FamilyDraftContent
    from tests.unit.backend.test_product_realization_preview_service import _request
    source = source_bundles.inspect(FAMILY_SOURCE)
    draft_content = FamilyDraftContent(display_name="Family", purpose="Example", usage_instructions="Use declared inputs",
        source_files=tuple({"path": member.source_path, "source": source.read_text(member.source_path)} for member in source.members),
        definition={"family_contract": _request().family_contract, "geometry_bindings": [{
            "binding_key": "body_geometry", "source": {"source_kind": "published", "resource_id": "geometry-root", "resource_version_id": "geometry-version"},
            "expected_resource_kind": "mapped_model", "geometry_purpose": "shape_fragment"}]})
    exact_artifacts = SimpleNamespace(
        read=AsyncMock(return_value=SimpleNamespace(content=FAMILY_SOURCE))
    )
    family_authoring = SimpleNamespace(read_draft_snapshot=AsyncMock(return_value=draft_content))
    if detail_kind == "authoring_draft_write":
        operation.code_artifact.sha256 = hashlib.sha256(draft_content.model_dump_json().encode()).hexdigest()
    family_catalog = SimpleNamespace(
        program_source_ref_for_visible_id=AsyncMock(return_value="pfv_v1.exact"),
        list_program_sources=AsyncMock(return_value=SimpleNamespace(sources=(), rejections=(), complete=True)),
        resolve_program_source=AsyncMock(),
        program_source_ref=lambda _scope, _bundle: "pfv_v1.exact",
    )
    family_assets = SimpleNamespace(read_program_source=AsyncMock())
    service = CodeDiscoveryService(
        data_runtime,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        geometry_queries,
        ifc_queries,
        SimpleNamespace(),
        _codec(),
        product_family_catalog=family_catalog,
        product_family_assets=family_assets,
        exact_artifacts=exact_artifacts,
        source_bundles=source_bundles,
        product_family_authoring=family_authoring,
    )
    return service, data_runtime, family_catalog, family_assets, exact_artifacts, operation


async def test_product_family_saved_authoring_snapshot_is_run_bound_and_read_only() -> None:
    service, data_runtime, _catalog, family_assets, exact_artifacts, operation = (
        _product_family_service()
    )

    page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT),
        None,
    )

    card = page.codes[0]
    assert card.status == "failed"
    assert card.repairable is False
    assert card.repair_entry is None
    assert card.repair_unavailable_reason == "owner_scope_read_only"
    assert card.result_alias is None
    assert [member.source_path for member in card.members] == ["layout.py", "main.py"]
    data_runtime.list_product_family_operation_sources.assert_awaited_once_with(
        _scope(),
        statuses=(CriticalOperationStatus.SUCCEEDED, CriticalOperationStatus.FAILED),
        before_sequence=None,
        page_size=20,
    )
    service._product_family_authoring.read_draft_snapshot.assert_awaited_once_with(
        _scope(),
        operation.code_artifact,
    )
    exact_artifacts.read.assert_not_awaited()
    family_assets.read_program_source.assert_not_awaited()

    result = await service.read_code(
        _scope(),
        card.code_locator,
        "layout.py",
        TextSourceView(view_mode="full"),
    )
    assert result.code_kind is CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT
    assert (
        result.member_sha256
        == hashlib.sha256(b"def solve(ctx):\n    return {'status': 'SOLVED'}\n").hexdigest()
    )
    data_runtime.get_product_family_operation_source.assert_awaited_once_with(_scope(), "op_family")
    assert service._product_family_authoring.read_draft_snapshot.await_count == 2
    exact_artifacts.read.assert_not_awaited()

    with pytest.raises(CodeDiscoveryApplicationError) as cross_run:
        await service.read_code(
            _scope(run_id="run-other"),
            card.code_locator,
            "layout.py",
            TextSourceView(view_mode="full"),
        )
    assert cross_run.value.code == "CODE_LOCATOR_INVALID"

    service._product_family_authoring = None
    with pytest.raises(CodeDiscoveryApplicationError) as unavailable:
        await service.list_codes(_scope(), CodeListQuery(
            code_kind=CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT), None)
    assert unavailable.value.code == "CODE_OWNER_UNAVAILABLE"
    exact_artifacts.read.assert_not_awaited()


async def test_current_system_copy_source_uses_exact_zip_without_draft_parsing() -> None:
    service, _runtime, _catalog, _assets, exact_artifacts, operation = _product_family_service(
        detail_kind="product_family_write",
        operation_status=CriticalOperationStatus.SUCCEEDED,
    )
    page = await service.list_codes(_scope(), CodeListQuery(
        code_kind=CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT), None)
    assert [member.source_path for member in page.codes[0].members] == ["layout.py", "main.py"]
    exact_artifacts.read.assert_awaited_once_with(
        _scope(), operation.code_artifact, expected_content_type="application/zip",
    )
    service._product_family_authoring.read_draft_snapshot.assert_not_awaited()


async def test_product_family_draft_write_does_not_claim_a_published_program() -> None:
    service, _runtime, catalog, _assets, _exact, _operation = _product_family_service(
        operation_status=CriticalOperationStatus.SUCCEEDED
    )

    page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT),
        None,
    )

    card = page.codes[0]
    assert card.status == "succeeded"
    assert card.repairable is False
    assert card.repair_entry is None
    assert card.repair_unavailable_reason == "owner_scope_read_only"
    assert card.result_alias is None
    assert card.result_code_locator is None
    catalog.program_source_ref_for_visible_id.assert_not_awaited()


async def test_product_family_server_failure_code_is_not_source_repairable() -> None:
    service, *_ = _product_family_service(
        error_phase=CriticalOperationErrorPhase.STORAGE_VERIFICATION
    )

    page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT),
        None,
    )

    card = page.codes[0]
    assert card.status == "failed"
    assert card.repairable is False
    assert card.repair_entry is None
    assert card.repair_unavailable_reason == "owner_scope_read_only"


@pytest.mark.parametrize("kind", [CodeKind.GEOMETRY_DRAFT, CodeKind.PRODUCT_FAMILY_DRAFT])
async def test_current_draft_source_is_task_scoped_and_stale_locator_never_reads_new_text(kind):
    service, *_ = _service()
    draft = SimpleNamespace(snapshot=SimpleNamespace(version_id="snapshot-1"))
    content = SimpleNamespace(source_files=(SimpleNamespace(path="main.py", source="def build(ctx):\n    return ctx\n"),))
    owner = SimpleNamespace(read_draft=AsyncMock(return_value=(draft, content)))
    service._geometry_authoring = service._product_family_authoring = owner
    service._source_bundles = SourceTextGeometryAuthoringSource()
    page = await service.list_codes(_scope(), CodeListQuery(code_kind=kind, draft_ref="cur.draft"), None)
    card = page.codes[0]
    assert card.owner_type == "authoring_draft" and card.status == "draft" and card.repairable
    result = await service.read_code(_scope(run_id="another-run"), card.code_locator, "main.py", TextSourceView(view_mode="full"))
    assert result.view.text == content.source_files[0].source
    draft.snapshot.version_id = "snapshot-2"
    with pytest.raises(CodeDiscoveryApplicationError) as stale:
        await service.read_code(_scope(), card.code_locator, "main.py", TextSourceView(view_mode="full"))
    assert stale.value.code == "CODE_DRAFT_CHANGED"


async def test_historical_product_family_authoring_operation_is_not_code_readable() -> None:
    service, data_runtime, _catalog, _assets, exact_artifacts, operation = (
        _product_family_service()
    )
    rejected = DataRuntimeApplicationError(
        "OPERATION_NOT_FOUND",
        "Family source Operation was not found in this invocation scope",
    )
    data_runtime.list_product_family_operation_sources.side_effect = rejected

    with pytest.raises(CodeDiscoveryApplicationError) as listed:
        await service.list_codes(
            _scope(),
            CodeListQuery(code_kind=CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT),
            None,
        )
    assert listed.value.code == "OPERATION_NOT_FOUND"
    exact_artifacts.read.assert_not_awaited()

    data_runtime.get_product_family_operation_source.side_effect = rejected
    locator = service._locator(
        _scope(),
        CodeKind.PRODUCT_FAMILY_AUTHORING_ATTEMPT,
        "product_family_authoring_operation",
        operation_alias="op_family",
    )
    with pytest.raises(CodeDiscoveryApplicationError) as read:
        await service.read_code(
            _scope(), locator, "main.py", TextSourceView(view_mode="full")
        )
    assert read.value.code == "OPERATION_NOT_FOUND"
    data_runtime.get_product_family_operation_source.assert_awaited_once_with(
        _scope(), "op_family"
    )
    exact_artifacts.read.assert_not_awaited()


async def test_product_family_published_program_is_cross_run_and_reauthorizes_version_owner() -> (
    None
):
    service, _runtime, catalog, family_assets, exact_artifacts, _operation = (
        _product_family_service()
    )
    asset = SimpleNamespace(
        asset_role=ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE,
        sha256=FAMILY_SOURCE_SHA,
    )
    bundle = SimpleNamespace(
        family=SimpleNamespace(
            owner_scope=ResourceOwnerScope(
                scope_kind=ResourceOwnerScopeKind.TASK,
                tenant_id="tenant-code",
                task_id="task-code",
            )
        ),
        family_version_id="family-version-1", version_no=1, created_at=NOW,
        source_asset=asset,
    )
    catalog.list_program_sources.return_value = SimpleNamespace(sources=(bundle,), rejections=(), complete=True)
    catalog.resolve_program_source.return_value = bundle
    family_assets.read_program_source.return_value = FAMILY_SOURCE

    page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM),
        None,
    )

    card = page.codes[0]
    assert card.status == "published"
    assert card.owner_type == "product_family_version"
    assert card.ownership == "agent"
    assert card.repairable is True
    assert card.repair_entry == "ifc_product_family_write"
    assert card.repair_unavailable_reason is None
    assert card.owner_alias == "pfv_v1.exact"
    family_assets.read_program_source.assert_awaited_once_with(bundle)
    exact_artifacts.read.assert_not_awaited()

    result = await service.read_code(
        _scope(run_id="run-other"),
        card.code_locator,
        "main.py",
        TextSourceView(view_mode="search", text="realize"),
    )
    assert result.code_kind is CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM
    catalog.resolve_program_source.assert_awaited_once_with(
        _scope(run_id="run-other").identity,
        "pfv_v1.exact",
    )
    assert family_assets.read_program_source.await_count == 2
    exact_artifacts.read.assert_not_awaited()

    with pytest.raises(CodeDiscoveryApplicationError) as cross_task:
        await service.read_code(
            _scope(task_id="task-other"),
            card.code_locator,
            "main.py",
            TextSourceView(view_mode="full"),
        )
    assert cross_task.value.code == "CODE_LOCATOR_INVALID"


async def test_historical_product_family_program_is_not_listed_or_code_readable() -> None:
    service, _runtime, catalog, family_assets, exact_artifacts, _operation = (
        _product_family_service()
    )
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogReferenceError

    rejected = ProductFamilyCatalogReferenceError(
        "product family source does not use the current contract"
    )
    catalog.list_program_sources.side_effect = rejected
    catalog.resolve_program_source.side_effect = rejected

    with pytest.raises(CodeDiscoveryApplicationError) as listed:
        await service.list_codes(
            _scope(),
            CodeListQuery(code_kind=CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM),
            None,
        )
    assert listed.value.code == "PRODUCT_FAMILY_REFERENCE_INVALID"
    family_assets.read_program_source.assert_not_awaited()

    locator = service._locator(
        _scope(),
        CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM,
        "product_family_version",
        family_version_ref="pfv_old.exact",
    )
    with pytest.raises(CodeDiscoveryApplicationError) as read:
        await service.read_code(
            _scope(), locator, "main.py", TextSourceView(view_mode="full")
        )
    assert read.value.code == "PRODUCT_FAMILY_REFERENCE_INVALID"
    catalog.resolve_program_source.assert_awaited_once_with(
        _scope().identity, "pfv_old.exact"
    )
    family_assets.read_program_source.assert_not_awaited()
    exact_artifacts.read.assert_not_awaited()


@pytest.mark.parametrize(
    "owner_scope",
    (
        ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind.SYSTEM),
        ResourceOwnerScope(
            scope_kind=ResourceOwnerScopeKind.TENANT,
            tenant_id="tenant-code",
        ),
        ResourceOwnerScope(
            scope_kind=ResourceOwnerScopeKind.TASK,
            tenant_id="tenant-code",
            task_id="task-other",
        ),
    ),
    ids=("system", "tenant", "other-task"),
)
async def test_product_family_visible_non_current_task_program_is_read_only(
    owner_scope: ResourceOwnerScope,
) -> None:
    service, _runtime, catalog, family_assets, _exact_artifacts, _operation = (
        _product_family_service()
    )
    asset = SimpleNamespace(
        asset_role=ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE,
        sha256=FAMILY_SOURCE_SHA,
    )
    bundle = SimpleNamespace(
        family=SimpleNamespace(owner_scope=owner_scope),
        family_version_id="family-version-shared", version_no=3, created_at=NOW,
        source_asset=asset,
    )
    catalog.list_program_sources.return_value = SimpleNamespace(sources=(bundle,), rejections=(), complete=True)
    family_assets.read_program_source.return_value = FAMILY_SOURCE

    page = await service.list_codes(
        _scope(),
        CodeListQuery(code_kind=CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM),
        None,
    )

    card = page.codes[0]
    assert card.ownership == "server"
    assert card.repairable is False
    assert card.repair_entry is None
    assert card.repair_unavailable_reason == "owner_scope_read_only"


async def test_product_family_published_program_rejects_status_filter() -> None:
    with pytest.raises(ValueError, match="do not accept"):
        CodeListQuery(
            code_kind=CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM,
            statuses=("failed",),
        )


@pytest.mark.asyncio
async def test_family_source_all_rejected_page_keeps_diagnostics_and_scan_cursor() -> None:
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogRejection
    service, _, catalog, assets, _, _ = _product_family_service()
    rejection = ProductFamilyCatalogRejection(
        family_version_ref="pfv_v1.exact", version_no=1,
        field_path="family_contract.interfaces.0.allowed_kinds", reason="Current constraint kind is invalid",
    )
    catalog.list_program_sources.side_effect = (
        SimpleNamespace(sources=(), rejections=(rejection,), complete=False,
            next_before_created_at=NOW, next_before_family_version_id="rejected-version"),
        SimpleNamespace(sources=(), rejections=(), complete=True),
    )
    first = await service.list_codes(_scope(), CodeListQuery(
        code_kind=CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM, page_size=1), None)
    assert first.codes == () and first.returned_count == 0 and first.next_cursor
    assert first.rejections == (rejection,)
    assets.read_program_source.assert_not_awaited()
    second = await service.list_codes(_scope(), None, first.next_cursor)
    assert second.next_cursor is None and second.rejections == ()
    kwargs = catalog.list_program_sources.await_args.kwargs
    assert kwargs["before_family_version_id"] == "rejected-version" and kwargs["limit"] == 1


@pytest.mark.asyncio
async def test_family_source_storage_failure_stays_technical_not_a_rejection() -> None:
    service, _, catalog, assets, _, _ = _product_family_service()
    catalog.list_program_sources.return_value = SimpleNamespace(
        sources=(SimpleNamespace(source_asset=SimpleNamespace(sha256=FAMILY_SOURCE_SHA)),),
        rejections=(), complete=True,
    )
    assets.read_program_source.side_effect = OSError("storage unavailable")
    with pytest.raises(CodeDiscoveryApplicationError) as caught:
        await service.list_codes(_scope(), CodeListQuery(code_kind=CodeKind.PRODUCT_FAMILY_REALIZATION_PROGRAM), None)
    assert caught.value.code == "CODE_OWNER_READ_FAILED"
