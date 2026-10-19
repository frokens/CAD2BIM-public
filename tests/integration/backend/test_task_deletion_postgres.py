"""Task deletion over real API/Runner roles and fixture-owned database objects."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from alembic import command
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from CAD2BIM_backend.api import create_task_api
from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.chat_projection import ChatProjectionService
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.domain.control_plane import ExecutionMode, RunStatus, TaskStatus
from CAD2BIM_backend.domain.data_runtime import CriticalOperationRecord, CriticalOperationStatus
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogListQuery
from CAD2BIM_backend.domain.identity import AuthenticatedSubject, TenantActorKind
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScopeKind
from CAD2BIM_backend.infrastructure.postgres.models import CriticalOperationModel, RunModel, TaskModel
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from tests.integration.backend.test_component_authoring_postgres import _geometry, _resources, _BOX_SOURCE
from tests.integration.backend.test_geometry_resource_migrations import _isolated_database
from tests.integration.backend.test_geometry_resource_postgres import (
    _create_runtime, _create_test_runtime_logins, _drop_test_runtime_logins, _runtime_database_url,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]


@pytest.mark.asyncio
async def test_authenticated_task_delete_fences_execution_and_preserves_tenant_library(tmp_path):
    with _isolated_database("task_deletion") as (config, scoped_engine):
        command.upgrade(config, "head")
        admin_url = scoped_engine.url.render_as_string(hide_password=False)
        logins = _create_test_runtime_logins(admin_url)
        api = _create_runtime(_runtime_database_url(admin_url, logins["api"]))
        runner = _create_runtime(_runtime_database_url(admin_url, logins["runner"]))
        admin = create_async_engine(admin_url)
        storage = LocalStorage(tmp_path / "storage")
        resources = _resources(runner.repository, storage, tmp_path)
        protocol = ExecutionProtocolBinding(bundle_id="component", module="component", stage_key="component")
        service = TaskRunService(repository=api.repository, identity_directory=api.identity_directory,
            execution_protocol_factory=lambda _module, _stage: protocol,
            bind_identity=bind_tenant_execution_context)
        now = datetime.now(UTC)
        subjects = {token: AuthenticatedSubject(issuer="https://task-delete.example.test",
            subject=f"{token}-{uuid4().hex}", audience="cad2bim-api", authenticated_at=now,
            token_expires_at=now + timedelta(minutes=5)) for token in ("owner", "foreign")}
        artifacts = ArtifactService(api.repository, storage)
        graph_runtime = SimpleNamespace(get_checkpoint_state=AsyncMock(side_effect=AssertionError("deleted chat read checkpoint")))
        projection = ChatProjectionService(task_runs=service, repository=api.repository,
            graph_runtime=graph_runtime, bind_identity=bind_tenant_execution_context,
            feedback_repository=api.repository, artifact_service=artifacts,
            delta_bridge=None, checkpoint_observer=None, callback_factory=lambda: None)
        app = create_task_api(authenticator=SimpleNamespace(authenticate=lambda token: subjects[token]),
            service=service, artifact_service=artifacts,
            bind_identity=bind_tenant_execution_context, chat_projection=projection)
        headers = {"authorization": "Bearer owner"}

        async def new_task(client, title):
            task_response = await client.post("/tasks", json={"title": title, "metadata": {"task_type": "component"}})
            assert task_response.status_code == 201, task_response.text
            task_id = task_response.json()["task_id"]
            run_response = await client.post(f"/tasks/{task_id}/runs", json={"module": "component",
                "stage_key": "component", "idempotency_key": uuid4().hex,
                "content": [{"type": "text", "text": "Temporary fixture task; do not invoke a model."}]})
            assert run_response.status_code == 201, run_response.text
            identity, _ = await service.authorized_task_context(subjects["owner"], task_id)
            return task_id, run_response.json()["run_id"], identity

        try:
            await api.database.verify_ready()
            async with api.engine.connect() as connection:
                assert (await connection.execute(text(
                    "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
                ))).one() == (False, False)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url="http://task-delete.test", headers=headers) as client:
                source_task, source_run, source_identity = await new_task(client, "Temporary published source")
                claimed = await runner.repository.claim_next_run("task-delete-source", lease_seconds=120)
                assert claimed.run_id == source_run
                identity = source_identity.model_copy(update={"actor_kind": TenantActorKind.RUNNER})
                source_scope = InvocationScope(identity=identity, owner_id=identity.tenant_id,
                    run_id=source_run, execution_mode=ExecutionMode.GRAPH, catalog_frontier=None)
                with bind_tenant_execution_context(identity):
                    draft = await resources.geometry_authoring.save_draft_from_aliases(source_scope,
                        declaration=_geometry(name="Temporary retained tenant geometry"), request_identity="retained-draft")
                    assert draft.draft.owner_scope.scope_kind is ResourceOwnerScopeKind.TENANT
                    reference = resources.geometry_authoring.draft_reference(source_scope, draft.draft)
                    published = await resources.geometry_authoring.publish_draft(source_scope, draft_ref=reference,
                        expected_edit_token=1, request_identity="retained-publication")
                    assert len(published.geometry_versions) == 1
                    await runner.repository.pause_claimed(source_run, "task-delete-source", claimed.lease_token)
                artifact = draft.draft.snapshot
                content_path = f"/tasks/{source_task}/artifacts/{artifact.artifact_id}/versions/{artifact.version_id}/content"
                before_content = await client.get(content_path)
                assert before_content.status_code == 200 and before_content.content
                foreign = await client.delete(f"/tasks/{source_task}", headers={"authorization": "Bearer foreign"})
                assert foreign.status_code == 404
                assert (await client.get(f"/tasks/{source_task}")).status_code == 200
                # Exercise the reverse lifecycle lock order without blocking or
                # changing a fixture Task while another Run transaction owns it.
                async with admin.begin() as connection:
                    await connection.execute(select(RunModel).where(RunModel.run_id == source_run).with_for_update())
                    busy = await client.delete(f"/tasks/{source_task}")
                    assert busy.status_code == 409 and "changing" in busy.json()["detail"]
                for _ in range(2):
                    deleted = await client.delete(f"/tasks/{source_task}")
                    assert deleted.status_code == 204 and deleted.content == b""
                for path in (f"/tasks/{source_task}", f"/tasks/{source_task}/runs/{source_run}",
                    f"/tasks/{source_task}/runs/{source_run}/snapshot", content_path):
                    assert (await client.get(path)).status_code == 404
                assert (await client.post(f"/tasks/{source_task}/runs/{source_run}/resume",
                    json={"client_request_id": "deleted-continue"})).status_code == 404
                assert all(task["task_id"] != source_task for task in (await client.get("/tasks")).json()["tasks"])
                graph_runtime.get_checkpoint_state.assert_not_awaited()
                async with admin.connect() as connection:
                    assert (await connection.execute(select(TaskModel.status).where(
                        TaskModel.task_id == source_task))).scalar_one() == TaskStatus.ARCHIVED.value

                reader_task, reader_run, reader_identity = await new_task(client, "Temporary library reader")
                reader = reader_identity.model_copy(update={"actor_kind": TenantActorKind.RUNNER})
                reader_scope = InvocationScope(identity=reader, owner_id=reader.tenant_id,
                    run_id=reader_run, execution_mode=ExecutionMode.GRAPH, catalog_frontier=None)
                with bind_tenant_execution_context(reader):
                    retained = await runner.repository.get_geometry_draft(reader_scope, resource_id=draft.draft.root_id)
                    assert retained == draft.draft
                    reopened = resources.geometry_authoring.draft_reference(reader_scope, retained)
                    _, definition = await resources.geometry_authoring.read_draft(reader_scope, draft_ref=reopened)
                    assert definition.source_files[0].source == _BOX_SOURCE
                    catalog = await resources.geometry_catalog.list(reader, GeometryCatalogListQuery(), None)
                    assert len(catalog.resources) == 1
                    source = await resources.geometry_source.read_source(reader,
                        catalog.resources[0].latest_version.resource_version_ref, "main.py", None)
                    assert source.view.text == _BOX_SOURCE
                    assert await runner.repository.get_run_with_execution_identity(reader.tenant_id, source_run) is None
                assert (await client.delete(f"/tasks/{reader_task}")).status_code == 204

                active_task, active_run, active_identity = await new_task(client, "Temporary running task")
                claimed = await runner.repository.claim_next_run("task-delete-active", lease_seconds=120)
                assert claimed.run_id == active_run
                running = await client.delete(f"/tasks/{active_task}")
                assert running.status_code == 409 and "Pause" in running.json()["detail"]
                assert not (await client.get(f"/tasks/{active_task}/runs/{active_run}")).json()["pause_requested"]
                assert (await client.post(f"/tasks/{active_task}/runs/{active_run}/pause",
                    json={"client_request_id": "explicit-delete-pause"})).status_code == 200
                assert (await client.delete(f"/tasks/{active_task}")).status_code == 409
                with bind_tenant_execution_context(active_identity.model_copy(update={"actor_kind": TenantActorKind.RUNNER})):
                    await runner.repository.pause_claimed(active_run, "task-delete-active", claimed.lease_token)
                assert (await client.delete(f"/tasks/{active_task}")).status_code == 204
                async with admin.connect() as connection:
                    assert (await connection.execute(select(RunModel.status).where(
                        RunModel.run_id == active_run))).scalar_one() == RunStatus.CANCELLED.value

                pending_task, pending_run, pending_identity = await new_task(client, "Temporary unresolved write")
                pending = CriticalOperationRecord(operation_id=f"operation_{uuid4().hex}",
                    owner_id=pending_identity.tenant_id, tenant_id=pending_identity.tenant_id,
                    task_id=pending_task, run_id=pending_run, tool_name="cad_import_source",
                    source_filename="temporary.dxf",
                    idempotency_key="temporary-unresolved", request_fingerprint="b" * 64,
                    status=CriticalOperationStatus.PREPARED, started_at=datetime.now(UTC))
                model = runner.repository._operation_model(pending)
                async with async_sessionmaker(admin)() as session, session.begin():
                    session.add(model)
                unresolved = await client.delete(f"/tasks/{pending_task}")
                assert unresolved.status_code == 409 and "unresolved write" in unresolved.json()["detail"]
                assert (await client.get(f"/tasks/{pending_task}")).status_code == 200
                async with admin.connect() as connection:
                    assert (await connection.execute(select(CriticalOperationModel.status).where(
                        CriticalOperationModel.operation_id == pending.operation_id))).scalar_one() == "prepared"
                    assert (await connection.execute(select(RunModel.status).where(
                        RunModel.run_id == pending_run))).scalar_one() == RunStatus.QUEUED.value
        finally:
            await resources.close()
            await api.close()
            await runner.close()
            await admin.dispose()
            _drop_test_runtime_logins(admin_url, logins)
