"""Seed reviewed package-owned SYSTEM geometry resources into the durable catalog."""

from __future__ import annotations

import argparse
import asyncio
import json

from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
from CAD2BIM_backend.application.system_geometry_seed_service import (
    SystemGeometrySeedResult,
    SystemGeometrySeedService,
)
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.system_geometry_seed import (
    system_geometry_seed_manifest_digest,
)
from CAD2BIM_backend.infrastructure.geometry_seed.loader import (
    load_production_system_geometry_seed,
)
from CAD2BIM_backend.infrastructure.postgres.database import create_database
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import require_system_curator
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle
from CAD2BIM_backend.ports.geometry_runtime import GeometryRuntimePort


async def _require_trusted_system_curator(database) -> None:
    async with database.engine.connect() as connection:
        await require_system_curator(connection)


async def run_production_system_geometry_seed(
    *,
    settings: Settings | None = None,
    geometry_runtime: GeometryRuntimePort | None = None,
) -> SystemGeometrySeedResult:
    """Run the fixed package manifest; callers cannot select a path or manifest."""

    resolved_settings = settings or Settings.load()
    loaded = load_production_system_geometry_seed(
        max_source_bytes=resolved_settings.max_upload_bytes
    )
    runtime = geometry_runtime
    owns_runtime = False
    if loaded.manifest.resources and runtime is None:
        from CAD2BIM_backend.infrastructure.geometry_runtime import (
            OcpCadQueryGeometryRuntime,
        )

        runtime = OcpCadQueryGeometryRuntime()
        owns_runtime = True
    database = create_database(resolved_settings)
    try:
        await database.verify_ready()
        await _require_trusted_system_curator(database)
        if runtime is not None:
            await runtime.verify_ready()
        repository = PostgresControlPlaneRepository(database.session_factory)
        storage = LocalStorage(
            resolved_settings.storage_root,
            max_size_bytes=resolved_settings.max_upload_bytes,
        )
        await storage.verify_ready()
        service = SystemGeometrySeedService(
            repository,
            storage,
            GeometryResourceService(repository, storage),
            runtime,
            pack_source_bundle,
        )
        manifest_digest = system_geometry_seed_manifest_digest(loaded.manifest)
        context = TenantExecutionContext(
            tenant_id="system",
            principal_id="system-geometry-seed",
            task_id=None,
            actor_kind=TenantActorKind.SYSTEM_CURATOR,
            command_id=f"system-geometry-seed-{manifest_digest[:24]}",
        )
        with bind_tenant_execution_context(context):
            return await service.seed(
                context,
                manifest=loaded.manifest,
                sources=loaded.sources,
            )
    finally:
        try:
            if owns_runtime and runtime is not None:
                await runtime.close()
        finally:
            await database.dispose()


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    result = asyncio.run(run_production_system_geometry_seed())
    loaded = load_production_system_geometry_seed()
    print(
        json.dumps(
            {
                "manifest_digest": system_geometry_seed_manifest_digest(loaded.manifest),
                "published_count": len(result.published),
                "replayed_count": len(result.replayed),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


__all__ = ["cli", "run_production_system_geometry_seed"]


if __name__ == "__main__":
    cli()
