"""Test-only second process that finishes one already selected IFC revision."""

from __future__ import annotations

import asyncio
import json
import os

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity, PreparedIfcRevision
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    bind_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.ports.ifc_revision_runtime import InspectExactIfcRevisionRequest
from tests.process_support.ifc_revision_runtime import build_in_process_ifc_stack


async def main() -> None:
    scope = IfcRunIdentity.model_validate_json(os.environ["P3_IFC_SCOPE"])
    context = TenantExecutionContext.model_validate_json(os.environ["P3_IFC_CONTEXT"])
    prepared = PreparedIfcRevision.model_validate_json(os.environ["P3_IFC_PREPARED"])
    engine = create_async_engine(os.environ["P3_IFC_DATABASE_URL"], pool_pre_ping=True)
    repository = PostgresControlPlaneRepository(async_sessionmaker(
        engine, class_=TenantAwareAsyncSession, expire_on_commit=False,
    ))
    stack = build_in_process_ifc_stack(
        repository, LocalStorage(os.environ["P3_IFC_STORAGE_ROOT"]),
    )
    mutations = 0
    inspections = 0
    execute = stack.runtime.execute

    async def inspect_only(request, *, source=None):
        nonlocal mutations, inspections
        if not isinstance(request, InspectExactIfcRevisionRequest):
            mutations += 1
            raise AssertionError("selected IFC candidate restarted mutation")
        inspections += 1
        return await execute(request, source=source)

    stack.runtime.execute = inspect_only
    try:
        with bind_tenant_execution_context(context):
            claim = await repository.claim_execution(
                scope, operation_id=prepared.operation_id,
                request_digest=prepared.request_digest,
            )
            try:
                resumed = await stack.writer.resume_selected_revision(scope, prepared, claim)
                assert resumed is not None
                committed, selected = resumed
                print(json.dumps({
                    "operation_alias": committed.operation_alias,
                    "revision_id": committed.exact.revision_id,
                    "sha256": committed.exact.artifact.sha256,
                    "project_name": selected.project_result.name if selected.project_result else None,
                    "mutations": mutations,
                    "inspections": inspections,
                    "generation": claim.generation,
                }))
            finally:
                await repository.retire_execution(scope, claim)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
