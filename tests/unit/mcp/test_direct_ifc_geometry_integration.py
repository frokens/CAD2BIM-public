from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from CAD2BIM_backend.application.geometry_query_service import GeometryQueryApplicationError
from CAD2BIM_mcp.direct.application import DirectApplicationFacade
from CAD2BIM_mcp.direct.bootstrap import DirectRuntimeResources
from CAD2BIM_mcp.direct.errors import DirectApplicationError
from CAD2BIM_mcp.direct.ifc.geometry.contracts import (
    DirectIfcGeometrySinglePreviewInput,
    DirectIfcGeometryQueryFreshInput,
    IfcGeometryResourceVersionQueryTarget,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp


async def test_direct_server_registers_geometry_tools_without_preview_template() -> None:
    server = create_direct_mcp(Mock())

    tools = [tool.name for tool in await server.list_tools()]
    assert len(tools) == 41
    assert (
        sum(
            name.startswith("ifc_")
            and name not in {"ifc_search_official_docs", "ifc_read_official_doc"}
            for name in tools
        )
        == 21
    )
    for name in (
        "ifc_geometry_resource_list",
        "ifc_geometry_resource_info",
        "ifc_geometry_resource_source_read",
        "ifc_geometry_preview",
        "ifc_geometry_query_python",
        "ifc_geometry_resource_write",
        "ifc_product_family_list",
        "ifc_product_family_info",
        "ifc_product_family_write",
        "ifc_product_realization_preview",
        "ifc_products_apply",
        "ifc_products_read",
        "ifc_query_python",
    ):
        assert tools.count(name) == 1

    templates = await server.list_resource_templates()
    assert templates == []


async def test_geometry_query_uses_its_total_execution_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import CAD2BIM_mcp.direct.ifc.geometry.application as geometry_application

    assert geometry_application._GEOMETRY_QUERY_TIMEOUT_SECONDS == 900.0
    monkeypatch.setattr(geometry_application, "_GEOMETRY_QUERY_TIMEOUT_SECONDS", 0.001)
    scope = SimpleNamespace(identity=Mock(), owner_id="owner", run_id="run_001")
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=scope))
    catalog = SimpleNamespace(
        resolve_exact_version=AsyncMock(
            return_value=SimpleNamespace(
                resource=SimpleNamespace(resource_id="resource_internal"),
                version=SimpleNamespace(resource_version_id="version_internal"),
            )
        )
    )

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def block(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    query = SimpleNamespace(fresh=block)
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        geometry_catalog_service=catalog,
        geometry_query_service=query,
    )

    with pytest.raises(DirectApplicationError) as caught:
        await facade.query_ifc_geometry_python(
            DirectIfcGeometryQueryFreshInput(
                direct_session_alias="direct_001",
                mode="fresh",
                target=IfcGeometryResourceVersionQueryTarget(
                    target_kind="geometry_resource_version",
                    resource_version_alias="cur.version",
                ),
                code="def query(ctx):\n    return []\n",
            ),
            request_identity="mcp:test",
        )

    assert caught.value.code == "GEOMETRY_QUERY_TIMEOUT"
    assert "total deadline expired" in str(caught.value)
    assert started.is_set()
    assert cancelled.is_set()


async def test_geometry_query_projects_terminal_resource_limit_as_nonretryable() -> None:
    scope = SimpleNamespace(identity=Mock(), owner_id="owner", run_id="run_001")
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=scope))
    catalog = SimpleNamespace(
        resolve_exact_version=AsyncMock(
            return_value=SimpleNamespace(
                resource=SimpleNamespace(resource_id="resource_internal"),
                version=SimpleNamespace(resource_version_id="version_internal"),
            )
        )
    )
    query = SimpleNamespace(
        fresh=AsyncMock(
            side_effect=GeometryQueryApplicationError(
                "GEOMETRY_QUERY_RESOURCE_LIMIT",
                "The geometry query exceeded its supervised execution limit",
                phase="execution",
                retryable=False,
                attempt_alias="cur.attempt",
            )
        )
    )
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        geometry_catalog_service=catalog,
        geometry_query_service=query,
    )

    with pytest.raises(DirectApplicationError) as caught:
        await facade.query_ifc_geometry_python(
            DirectIfcGeometryQueryFreshInput(
                direct_session_alias="direct_001",
                mode="fresh",
                target=IfcGeometryResourceVersionQueryTarget(
                    target_kind="geometry_resource_version",
                    resource_version_alias="cur.version",
                ),
                code="def query(ctx):\n    return []\n",
            ),
            request_identity="mcp:resource-limit",
        )

    assert caught.value.code == "GEOMETRY_QUERY_RESOURCE_LIMIT"
    assert caught.value.phase == "execution"
    assert caught.value.retryable is False
    assert caught.value.diagnostic == {"attempt_alias": "cur.attempt"}
    query.fresh.assert_awaited_once()


async def test_geometry_preview_timeout_cancels_the_read_and_projects_its_own_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import CAD2BIM_mcp.direct.ifc.geometry.application as geometry_application

    monkeypatch.setattr(geometry_application, "_GEOMETRY_PREVIEW_TIMEOUT_SECONDS", 0.01)
    scope = SimpleNamespace(identity=Mock(), owner_id="owner", run_id="run_001")
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=scope))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def block(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    preview = SimpleNamespace(prepare_published_cases=AsyncMock(return_value={}), preview_loaded=AsyncMock(side_effect=block))
    facade = DirectApplicationFacade(
        Mock(),
        Mock(),
        resolver,
        lambda _identity: nullcontext(),
        geometry_preview_service=preview,
    )

    with pytest.raises(DirectApplicationError) as caught:
        await facade.preview_ifc_geometry(
            DirectIfcGeometrySinglePreviewInput(
                direct_session_alias="direct_001",
                mode="single",
                source={"source_kind": "published", "resource_version_alias": "cur.version"},
            )
        )

    assert started.is_set()
    assert cancelled.is_set()
    assert caught.value.code == "GEOMETRY_PREVIEW_TIMEOUT"
    assert caught.value.phase == "read"
    assert caught.value.retryable is False
    assert "total deadline expired" in str(caught.value)
    preview.preview_loaded.assert_awaited_once()


async def test_direct_runtime_checks_prerequisites_and_closes_unique_runtime_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import CAD2BIM_mcp.direct.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "verify_attested_environment", lambda *_args: None)
    geometry_runtime = SimpleNamespace(verify_ready=AsyncMock(), close=AsyncMock())
    geometry_codec = SimpleNamespace(close=AsyncMock())
    product_realization_runtime = SimpleNamespace(
        verify_ready=AsyncMock(),
        close=AsyncMock(),
    )
    ifc_query_worker = SimpleNamespace(verify_ready=AsyncMock(), close=AsyncMock())
    ifc_revision_runtime = SimpleNamespace(verify_ready=AsyncMock(), close=AsyncMock())
    database = SimpleNamespace(verify_ready=AsyncMock(), dispose=AsyncMock())
    storage = SimpleNamespace(verify_ready=AsyncMock())
    transform_worker = SimpleNamespace(verify_ready=AsyncMock(), close=AsyncMock())
    python_worker = SimpleNamespace(verify_ready=AsyncMock(), close=AsyncMock())
    snapshots = SimpleNamespace(close=AsyncMock())
    measurement = SimpleNamespace(close=AsyncMock())
    resources = DirectRuntimeResources(
        application=Mock(),
        query_service=Mock(),
        query_python_service=Mock(),
        json_service=Mock(),
        query_sql_service=Mock(),
        query_snapshots=snapshots,
        transform_service=Mock(),
        cad_measurement_service=measurement,
        ifc_model_service=Mock(),
        ifc_trusted_baseline_service=Mock(),
        ifc_geometry_basis_service=Mock(),
        ifc_project_service=Mock(),
        ifc_spatial_structure_service=Mock(),
        ifc_query_service=Mock(),
        ifc_revision_runtime=ifc_revision_runtime,
        ifc_write_coordinator=SimpleNamespace(close=AsyncMock()),
        geometry_resource_service=Mock(),
        geometry_catalog_service=Mock(),
        geometry_source_service=Mock(),
        geometry_preview_service=Mock(),
        geometry_inspection_service=Mock(),
        geometry_query_service=Mock(),
        geometry_authoring_service=Mock(),
        product_family_catalog_service=Mock(),
        product_family_authoring_service=Mock(),
        product_realization_input_service=Mock(),
        product_realization_preview_service=Mock(),
        product_realization_runtime=product_realization_runtime,
        code_discovery_service=Mock(),
        geometry_runtime=geometry_runtime,
        geometry_codec=geometry_codec,
        ifc_query_worker=ifc_query_worker,
        transform_worker=transform_worker,
        python_transform_worker=python_worker,
        sessions=Mock(),
        scope_resolver=Mock(),
        scope_binder=Mock(),
        database=database,
        storage=storage,
        python_worker_executable=Path(__file__),
        python_worker_attestation=Mock(),
    )

    await resources.verify_ready()
    await resources.close()

    database.verify_ready.assert_awaited_once_with()
    storage.verify_ready.assert_awaited_once_with()
    transform_worker.verify_ready.assert_not_awaited()
    python_worker.verify_ready.assert_not_awaited()
    geometry_runtime.verify_ready.assert_not_awaited()
    geometry_runtime.close.assert_awaited_once_with()
    product_realization_runtime.verify_ready.assert_not_awaited()
    product_realization_runtime.close.assert_awaited_once_with()
    ifc_query_worker.verify_ready.assert_not_awaited()
    ifc_query_worker.close.assert_awaited_once_with()
    ifc_revision_runtime.verify_ready.assert_not_awaited()
    ifc_revision_runtime.close.assert_awaited_once_with()
    database.dispose.assert_awaited_once_with()
