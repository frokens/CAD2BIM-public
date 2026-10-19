"""Durable reference index uses real restricted roles and the existing repository."""
from __future__ import annotations

import asyncio
import base64
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.application.catalog_scope import catalog_scope_digest
from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec
from CAD2BIM_backend.infrastructure.postgres.models import OpaqueReferenceIndexModel
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context, set_tenant_execution_context,
)
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError
from tests.integration.backend.test_geometry_resource_postgres import (
    _create_runtime, _create_test_runtime_logins, _drop_test_runtime_logins,
    _open_direct_context, _runtime_database_url,
)

pytestmark = [pytest.mark.integration, pytest.mark.network, pytest.mark.asyncio]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()


async def test_reference_index_is_stable_concurrent_immutable_and_scope_bound():
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    roles = _create_test_runtime_logins(TEST_DATABASE_URL)
    runtime_url = _runtime_database_url(TEST_DATABASE_URL, roles["mcp"])
    runtime = _create_runtime(runtime_url)
    restarted = None
    admin = create_async_engine(TEST_DATABASE_URL)
    codec = CursorCodec(base64.urlsafe_b64encode(b"i" * 32).rstrip(b"=").decode())
    subject = f"reference-index-{uuid4().hex}"
    try:
        await runtime.database.verify_ready()
        scope = await _open_direct_context(runtime, subject=subject, session_name="reference-one")
        other_task = await _open_direct_context(runtime, subject=subject, session_name="reference-two")
        other_user = await _open_direct_context(runtime, subject=subject + "-other", session_name="reference-other")
        service = OpaqueReferenceService(runtime.repository, codec)
        old = codec.encode({"domain": "geometry_catalog_reference",
                            "reference_kind": "resource_version", "exact": "version-one"})
        newer = codec.encode({"domain": "geometry_catalog_reference",
                              "reference_kind": "resource_version", "exact": "version-two"})
        with bind_tenant_execution_context(scope):
            handles = await asyncio.gather(*(service.shorten(scope, old, ("gv",)) for _ in range(8)))
            assert len(set(handles)) == 1 and len(handles[0]) == 25
            handle = handles[0]
            new_handle = await service.shorten(scope, newer, ("gv",))
            assert new_handle != handle
            assert await service.expand(scope, old, ("gv",)) == old
        await runtime.close()
        restarted = _create_runtime(runtime_url)
        service = OpaqueReferenceService(restarted.repository, codec)
        with bind_tenant_execution_context(scope):
            assert await service.expand(scope, handle, ("gv",)) == old
            assert await service.shorten(scope, old, ("gv",)) == handle
            assert await service.expand(scope, new_handle, ("gv",)) == newer
            with pytest.raises(ReferenceIndexError):
                await service.expand(scope, handle, ("fv",))
            with pytest.raises(ReferenceIndexError):
                await service.expand(scope, handle[:-1] + ("A" if handle[-1] != "A" else "B"), ("gv",))
        for foreign in (other_task, other_user,
                        scope.model_copy(update={"principal_id": other_user.principal_id}),
                        scope.model_copy(update={"tenant_id": other_user.tenant_id})):
            with bind_tenant_execution_context(foreign):
                with pytest.raises(ReferenceIndexError, match="current scope"):
                    await service.expand(foreign, handle, ("gv",))
                # Deliberately bypass the digest/filter helper: RLS must itself hide it.
                async with restarted.sessions() as session:
                    await set_tenant_execution_context(session, foreign)
                    assert await session.scalar(select(OpaqueReferenceIndexModel.handle).where(
                        OpaqueReferenceIndexModel.handle == handle)) is None
        with bind_tenant_execution_context(scope):
            async with restarted.sessions() as session:
                await set_tenant_execution_context(session, scope)
                with pytest.raises(DBAPIError, match="permission denied"):
                    await session.execute(update(OpaqueReferenceIndexModel).where(
                        OpaqueReferenceIndexModel.handle == handle).values(sealed_reference=newer))
        # Even a migration administrator cannot retarget an existing binding.
        async with admin.begin() as connection:
            with pytest.raises(DBAPIError, match="immutable"):
                await connection.execute(update(OpaqueReferenceIndexModel).where(
                    OpaqueReferenceIndexModel.handle == handle).values(sealed_reference=newer))
        async with admin.connect() as connection:
            row = (await connection.execute(text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'opaque_reference_index'"))).one()
            assert row == (True, True)
            count = await connection.scalar(text(
                "SELECT count(*) FROM opaque_reference_index WHERE scope_digest=:scope"
            ), {"scope": catalog_scope_digest(scope)})
            assert count == 2
    finally:
        await runtime.close()
        if restarted:
            await restarted.close()
        await admin.dispose()
        _drop_test_runtime_logins(TEST_DATABASE_URL, roles)


async def test_runner_reference_translation_survives_run_change_and_reconnect():
    """Task navigation survives a new Run; credentials and domain locators do not move."""
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    from types import SimpleNamespace

    from alembic import command
    from fastapi import FastAPI
    import httpx

    from CAD2BIM_backend.domain.control_plane import StartRunRequest
    from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
    from CAD2BIM_backend.domain.identity import TenantActorKind
    from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant
    from CAD2BIM_backend.infrastructure.postgres.database import EXPECTED_SCHEMA_REVISION
    from CAD2BIM_backend.infrastructure.postgres.models import RunModel
    from CAD2BIM_backend.infrastructure.runner_domain_tokens import RunnerDomainTokenCodec
    from CAD2BIM_backend.internal_domain_api import create_internal_domain_router
    from tests.integration.backend.test_geometry_resource_migrations import _isolated_database

    # A fresh schema keeps the real claim function away from unrelated test Runs.
    with _isolated_database("runner_references") as (config, scoped_engine):
        command.upgrade(config, "20261002_0107")
        with scoped_engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261002_0107"
            assert not connection.scalar(text(
                "SELECT has_table_privilege('cad2bim_runner_runtime','opaque_reference_index','SELECT')"
            ))
        command.upgrade(config, "head")
        admin_url = scoped_engine.url.render_as_string(hide_password=False)
        roles = _create_test_runtime_logins(admin_url)
        mcp = _create_runtime(_runtime_database_url(admin_url, roles["mcp"]))
        runner_url = _runtime_database_url(admin_url, roles["runner"])
        runner = _create_runtime(runner_url)
        admin = create_async_engine(admin_url)
        codec = CursorCodec(base64.urlsafe_b64encode(b"r" * 32).rstrip(b"=").decode())
        token_codec = RunnerDomainTokenCodec(b"runner-reference-test-secret-value")
        protocol = ExecutionProtocolBinding(bundle_id="component", module="component", stage_key="component")
        runner_id = f"references-{uuid4().hex}"

        def application(runtime, references):
            app = FastAPI()
            # Only the reference route is invoked; unrelated business owners stay unused.
            app.include_router(create_internal_domain_router(
                codec=token_codec, runner_repository=runtime.repository,
                bind_identity=bind_tenant_execution_context, execution_protocol=protocol,
                data=SimpleNamespace(runtime=None, query=None, query_sql=None, query_python=None,
                    json=None, transform=None, code_discovery=None, references=references),
                ifc=None,
                authoring=SimpleNamespace(geometry_inspection=SimpleNamespace(operation_result=None),
                    family_catalog=SimpleNamespace(version_ref_for_visible_id=None)),
            ))
            return app

        async def claim(context):
            identity = context.model_copy(update={"actor_kind": TenantActorKind.RUNNER})
            with bind_tenant_execution_context(identity):
                task = await runner.repository.get_task(identity.tenant_id, identity.task_id)
                run = await runner.repository.create_run(task.owner_id, identity.task_id,
                    StartRunRequest(module="component", stage_key="component", idempotency_key=uuid4().hex),
                    execution_protocol_factory=lambda: protocol)
                claimed = await runner.repository.claim_next_run(runner_id, lease_seconds=120)
            assert claimed is not None and claimed.run_id == run.run_id
            grant = RunnerCheckpointGrant.from_claim(claimed, runner_id=runner_id)
            token = token_codec.issue(grant, action_id=f"call:{uuid4().hex}", tool_name="operation_info")
            return grant, {"Authorization": f"Bearer {token}"}

        async def translate(app, headers, direction, value, kind="or"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                return await client.post("/internal/domain/references/translate", headers=headers,
                    json={"direction": direction, "items": [{"value": value, "kinds": [kind]}]})

        try:
            await runner.database.verify_ready()
            async with runner.engine.connect() as connection:
                assert (await connection.execute(text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                ))).one() == (False, False)
                assert (await connection.execute(text(
                    "SELECT has_table_privilege(current_user,'opaque_reference_index','SELECT'), "
                    "has_table_privilege(current_user,'opaque_reference_index','INSERT'), "
                    "has_table_privilege(current_user,'opaque_reference_index','UPDATE'), "
                    "has_table_privilege(current_user,'opaque_reference_index','DELETE')"
                ))).one() == (True, True, False, False)
                assert (await connection.execute(text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
                    "WHERE oid=to_regclass('opaque_reference_index')"
                ))).one() == (True, True)
            subject = f"runner-reference-{uuid4().hex}"
            contexts = [await _open_direct_context(mcp, subject=who, session_name=name)
                for who, name in ((subject, "one"), (subject, "another-task"), (subject + "-foreign", "foreign-tenant"))]
            assert contexts[0].tenant_id == contexts[1].tenant_id
            assert contexts[0].tenant_id != contexts[2].tenant_id
            # Retire only the three fixture-created Direct Runs before Graph creation.
            async with admin.begin() as connection:
                await connection.execute(update(RunModel).where(
                    RunModel.task_id.in_([context.task_id for context in contexts])
                ).values(status="completed"))

            references = OpaqueReferenceService(runner.repository, codec)
            app = application(runner, references)
            first, first_headers = await claim(contexts[0])
            operation_id = f"operation_{uuid4().hex}"
            sealed = references.operation_reference(first.scope.identity,
                origin_run_id=first.scope.run_id, operation_id=operation_id)
            result = await translate(app, first_headers, "shorten", sealed)
            assert result.status_code == 200, result.text
            handle = result.json()["values"][0]
            assert handle.startswith("or_") and len(handle) == 25
            with bind_tenant_execution_context(first.scope.identity):
                await runner.repository.complete(first.scope.run_id, runner_id, first.lease_token)

            second, headers = await claim(contexts[0])
            assert first.scope.run_id != second.scope.run_id
            assert catalog_scope_digest(first.scope.identity) == catalog_scope_digest(second.scope.identity)
            assert (await translate(app, first_headers, "expand", handle)).status_code == 403
            result = await translate(app, headers, "shorten", sealed)
            assert result.status_code == 200 and result.json() == {"values": [handle]}
            assert (await translate(app, headers, "expand", handle)).json() == {"values": [sealed]}
            assert references.operation_locator(second.scope.identity, sealed) == (first.scope.run_id, operation_id)

            await runner.close()
            runner = _create_runtime(runner_url)
            references = OpaqueReferenceService(runner.repository, codec)
            app = application(runner, references)
            result = await translate(app, headers, "expand", handle)
            assert result.status_code == 200 and result.json() == {"values": [sealed]}
            assert (await translate(app, headers, "shorten", sealed)).json() == {"values": [handle]}
            wrong_kind = await translate(app, headers, "expand", handle, "gv")
            assert wrong_kind.status_code == 409 and wrong_kind.json()["detail"]["code"] == "REFERENCE_INVALID"
            for context in contexts[1:]:
                foreign, foreign_headers = await claim(context)
                rejected = await translate(app, foreign_headers, "expand", handle)
                assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "REFERENCE_INVALID"
                with bind_tenant_execution_context(foreign.scope.identity):
                    with pytest.raises(ReferenceIndexError, match="unavailable in this scope"):
                        references.operation_locator(foreign.scope.identity, sealed)
                    # No repository digest predicate here: the database must itself hide it.
                    async with runner.sessions() as session:
                        await set_tenant_execution_context(session, foreign.scope.identity)
                        assert await session.scalar(select(OpaqueReferenceIndexModel.handle).where(
                            OpaqueReferenceIndexModel.handle == handle)) is None
                    await runner.repository.complete(foreign.scope.run_id, runner_id, foreign.lease_token)
            with bind_tenant_execution_context(second.scope.identity):
                async with runner.sessions() as session:
                    row = (await session.execute(select(OpaqueReferenceIndexModel).where(
                        OpaqueReferenceIndexModel.handle == handle))).scalar_one()
                    assert row.sealed_reference == sealed
                await runner.repository.complete(second.scope.run_id, runner_id, second.lease_token)
            async with admin.connect() as connection:
                assert await connection.scalar(text(
                    "SELECT count(*) FROM opaque_reference_index WHERE scope_digest=:scope"
                ), {"scope": catalog_scope_digest(second.scope.identity)}) == 1
            # Only the 0108 reference grant is reversible. Later resource
            # deletion intentionally forbids restoring retired online roots.
            with _isolated_database("reference_grant_reversal") as (grant_config, grant_engine):
                command.upgrade(grant_config, "20261002_0108")
                command.downgrade(grant_config, "20261002_0107")
                with grant_engine.connect() as connection:
                    assert not connection.scalar(text(
                        "SELECT has_table_privilege('cad2bim_runner_runtime','opaque_reference_index','SELECT')"
                    ))
                    assert connection.scalar(text(
                        "SELECT has_table_privilege('cad2bim_mcp_runtime','opaque_reference_index','SELECT')"
                    ))
                command.upgrade(grant_config, "head")
            with scoped_engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == EXPECTED_SCHEMA_REVISION
                assert connection.scalar(text("SELECT count(*) FROM opaque_reference_index")) == 1
        finally:
            await mcp.close()
            await runner.close()
            await admin.dispose()
            _drop_test_runtime_logins(admin_url, roles)
