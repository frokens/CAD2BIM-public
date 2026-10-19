"""Test-only IFC claim host used to kill the real owner around the guard gate."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    TenantAwareAsyncSession,
    bind_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.worker_runtime.supervisor import (
    WorkerLaunchSpec,
    WorkerProcessBudget,
    WorkerProcessSupervisor,
)


async def main() -> None:
    phase = Path(os.environ["P2_IFC_HOST_PHASE"])
    mode = os.environ["P2_IFC_HOST_MODE"]
    scope = IfcRunIdentity.model_validate_json(os.environ["P2_IFC_HOST_SCOPE"])
    context = TenantExecutionContext.model_validate_json(os.environ["P2_IFC_HOST_CONTEXT"])
    engine = create_async_engine(os.environ["P2_IFC_HOST_DATABASE_URL"], pool_pre_ping=True)
    repository = PostgresControlPlaneRepository(async_sessionmaker(
        engine, class_=TenantAwareAsyncSession, expire_on_commit=False,
    ))
    try:
        with bind_tenant_execution_context(context):
            claim = await repository.claim_execution(
                scope,
                operation_id=os.environ["P2_IFC_HOST_OPERATION_ID"],
                request_digest=os.environ["P2_IFC_HOST_DIGEST"],
            )
            (phase / "claim.json").write_text(claim.model_dump_json())

            async def register(pid: int, birth_us: int) -> None:
                (phase / "guard.json").write_text(json.dumps({
                    "pid": pid, "birth_us": birth_us,
                }))
                if mode == "before":
                    await asyncio.Event().wait()
                await repository.associate_execution_guard(
                    scope, claim, guardian_pid=pid, guardian_birth_us=birth_us,
                )
                (phase / "registered").touch()

            await WorkerProcessSupervisor().run(
                WorkerLaunchSpec(
                    command=(
                        sys.executable, "-I", "-c",
                        "import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text('started');time.sleep(60)",
                        str(phase / "child-started"),
                    ),
                    cwd=phase, env={}, scratch=phase,
                ),
                budget=WorkerProcessBudget(
                    wall_seconds=60, cpu_seconds=2, rss_bytes=256 * 1024**2,
                    scratch_bytes=1024 * 1024, threads=4, open_file_descriptors=32,
                    stdout_bytes=1024, stderr_bytes=1024, cleanup_seconds=1,
                ),
                deadline=time.monotonic() + 60,
                on_guard_registered=register,
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
