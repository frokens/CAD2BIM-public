from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.ifc_geometry_basis import (
    IfcRepresentationContextCatalog,
)
from CAD2BIM_backend.domain.ifc_model import (
    IfcGeometryBasisConfigureResult,
    IfcModelInfo,
    IfcModelInitializeResult,
)
from CAD2BIM_backend.domain.ifc_project import (
    IfcProjectNoChangeError,
    IfcProjectSummary,
    IfcProjectUpdateResult,
)
from CAD2BIM_backend.domain.ifc_spatial_structure import IfcSpatialStructureListResult
from CAD2BIM_backend.internal_ifc_api import create_internal_ifc_router


_PATHS = {
    "/internal/domain/ifc/model/initialize",
    "/internal/domain/ifc/model/info",
    "/internal/domain/ifc/model/geometry-basis/configure",
    "/internal/domain/ifc/products/apply",
    "/internal/domain/ifc/model/representation-contexts/list",
    "/internal/domain/ifc/project/update",
    "/internal/domain/ifc/spatial-structure/list",
    "/internal/domain/ifc/spatial-structure/apply",
    "/internal/domain/ifc/query/python",
}


@pytest.mark.asyncio
async def test_ifc_router_paths_and_authorized_actions_reach_shared_core():
    access = SimpleNamespace(
        owner_id="tenant-ifc", task_id="task-ifc", run_id="run-ifc",
        action_id="action-ifc-1",
    )
    identity = TenantExecutionContext(
        tenant_id="tenant-ifc", principal_id="principal-ifc", task_id="task-ifc",
        actor_kind=TenantActorKind.RUNNER, command_id="action-ifc-1",
    )
    authorized_calls = []

    async def authorized(_request, *, tool_name):
        authorized_calls.append(tool_name)
        return access, identity

    info = IfcModelInfo(
        size_bytes=320,
        project={"global_id": "A" * 22, "name": "Plant"},
        geometry_basis_configured=False, geometry_basis=None,
        site_count=0, element_count=0,
    )
    project = IfcProjectSummary(global_id="A" * 22, name="Updated plant")
    ifc = SimpleNamespace(
        model=SimpleNamespace(
            initialize=AsyncMock(return_value=IfcModelInitializeResult(
                operation_alias="op_001", model=info,
            )),
            info=AsyncMock(return_value=info),
        ),
        geometry_basis=SimpleNamespace(
            configure=AsyncMock(return_value=IfcGeometryBasisConfigureResult(
                operation_alias="op_003", model=info,
            )),
            list_representation_contexts=AsyncMock(
                return_value=IfcRepresentationContextCatalog(),
            ),
        ),
        project=SimpleNamespace(update=AsyncMock(return_value=IfcProjectUpdateResult(
            operation_alias="op_002", project=project,
        ))),
        spatial_structure=SimpleNamespace(
            list=AsyncMock(return_value=IfcSpatialStructureListResult(
                items=(), returned_count=0,
            )),
            apply=AsyncMock(return_value=SimpleNamespace(
                model_dump=lambda **_: {"operation_alias": "op_004"},
            )),
        ),
        query=SimpleNamespace(
            fresh=AsyncMock(return_value=SimpleNamespace(
                attempt_alias="cur.fresh", parent_attempt_alias=None,
                source_sha256="c" * 64, results=(), returned_count=0,
                total_count=0, result_bytes=2, next_cursor=None,
            )),
            repair_from_alias=AsyncMock(return_value=SimpleNamespace(
                attempt_alias="cur.repaired", parent_attempt_alias="cur.parent",
                source_sha256="b" * 64, results=(), returned_count=0,
                total_count=0, result_bytes=2, next_cursor=None,
            )),
        ),
    )
    router = create_internal_ifc_router(
        authorized=authorized, bind_identity=lambda _identity: nullcontext(), ifc=ifc,
    )
    app = FastAPI()
    app.include_router(router, prefix="/internal/domain")
    assert {
        "/internal/domain" + route.path for route in router.routes
    } == _PATHS

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        initialized = await client.post(
            "/internal/domain/ifc/model/initialize", json={"project_name": "Plant"},
        )
        model_info = await client.post("/internal/domain/ifc/model/info")
        configured = await client.post(
            "/internal/domain/ifc/model/geometry-basis/configure",
            json={"command": {"project_unit_change": {"length_unit": {"name": "METRE"}}}},
        )
        contexts = await client.post(
            "/internal/domain/ifc/model/representation-contexts/list",
        )
        updated = await client.post(
            "/internal/domain/ifc/project/update",
            json={"command": {"patch": {"name": "Updated plant"}, "clear_fields": []}},
        )
        spatial = await client.post(
            "/internal/domain/ifc/spatial-structure/list", json={"query": {}},
        )
        applied = await client.post(
            "/internal/domain/ifc/spatial-structure/apply",
            json={"command": {"operations": [{
                "action": "create", "client_ref": "site_0", "spatial_class": "site",
                "parent": {"kind": "project"},
                "project_world_placement": {
                    "origin": [0, 0, 0], "axis": [0, 0, 1],
                    "ref_direction": [1, 0, 0],
                },
                "name": "Site", "composition_type": "ELEMENT",
            }]}},
        )
        fresh = await client.post(
            "/internal/domain/ifc/query/python",
            json={"mode": "fresh", "code": "result = []", "inputs": []},
        )
        repaired = await client.post(
            "/internal/domain/ifc/query/python",
            json={
                "mode": "repair", "parent_attempt_alias": "cur.parent",

                "edits": [{"old_text": "broken", "new_text": "fixed"}],
                "page_size": 10,
            },
        )
        mixed_query = await client.post(
            "/internal/domain/ifc/query/python",
            json={"mode": "cursor", "cursor": "cur.page", "code": "unexpected"},
        )
        missing_spatial_mode = await client.post(
            "/internal/domain/ifc/spatial-structure/list", json={},
        )

    assert all(response.status_code == 200 for response in (
        initialized, model_info, configured, contexts, updated,
        spatial, applied, fresh, repaired,
    ))
    assert initialized.json()["operation_alias"] == "op_001"
    assert initialized.json()["model"]["size_bytes"] == info.size_bytes
    assert model_info.json()["size_bytes"] == info.size_bytes
    assert configured.json()["model"]["size_bytes"] == info.size_bytes
    assert updated.json()["operation_alias"] == "op_002"
    assert configured.json()["operation_alias"] == "op_003"
    assert applied.json()["operation_alias"] == "op_004"
    assert repaired.json()["result_json"] == "[]"
    assert mixed_query.status_code == missing_spatial_mode.status_code == 422
    assert authorized_calls == [
        "ifc_model_initialize", "ifc_model_info",
        "ifc_model_configure_geometry_basis", "ifc_representation_context_list",
        "ifc_project_update", "ifc_spatial_structure_list",
        "ifc_spatial_structure_apply", "ifc_query_python",
        "ifc_query_python", "ifc_query_python",
    ]
    assert ifc.geometry_basis.configure.await_args.kwargs["idempotency_key"] == "action-ifc-1"
    assert ifc.project.update.await_args.kwargs["idempotency_key"] == "action-ifc-1"
    assert ifc.spatial_structure.apply.await_args.kwargs["idempotency_key"] == "action-ifc-1"
    assert ifc.query.repair_from_alias.await_args.args[1] == "cur.parent"
    assert ifc.query.repair_from_alias.await_args.args[3] == "action-ifc-1"


@pytest.mark.asyncio
async def test_failed_ifc_write_projects_persisted_operation_alias():
    access = SimpleNamespace(
        owner_id="tenant-ifc", task_id="task-ifc", run_id="run-ifc",
        action_id="action-ifc-1",
    )
    identity = TenantExecutionContext(
        tenant_id="tenant-ifc", principal_id="principal-ifc", task_id="task-ifc",
        actor_kind=TenantActorKind.RUNNER, command_id="action-ifc-1",
    )

    async def authorized(_request, *, tool_name):
        assert tool_name == "ifc_project_update"
        return access, identity

    failure = IfcProjectNoChangeError("Project metadata is unchanged")
    failure.operation_alias = "op_123"
    ifc = SimpleNamespace(project=SimpleNamespace(update=AsyncMock(side_effect=failure)))
    app = FastAPI()
    app.include_router(create_internal_ifc_router(
        authorized=authorized, bind_identity=lambda _identity: nullcontext(), ifc=ifc,
    ), prefix="/internal/domain")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/internal/domain/ifc/project/update",
            json={"command": {"patch": {"name": "Plant"}, "clear_fields": []}},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "IFC_NO_CHANGES", "message": "Project metadata is unchanged",
        "phase": "preflight", "retryable": False, "operation_alias": "op_123",
        "operation_status": "failed",
    }
