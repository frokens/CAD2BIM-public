"""Publish an explicitly selected current library through Backend curator authority."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from pathlib import Path

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.identity import ConfiguredLocalSubject, TenantActorKind, stable_internal_identity_id
from CAD2BIM_backend.domain.system_family_publication import SystemFamilyPublicationCommand
from CAD2BIM_backend.ifc_application_resources import create_system_family_publication_service
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.postgres.database import create_database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import PostgresIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import require_system_curator
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.infrastructure.storage import LocalStorage


async def run_selected_system_publication(
    *, publication_name: str, request_prefix: str,
    selector_path: Path | None = None, library_path: Path | None = None,
    family_key: str | None = None, resume_request_id: str | None = None,
    settings: Settings | None = None,
) -> tuple[dict[str, object], ...]:
    settings = settings or Settings.load()
    if settings.system_publication_subject is None:
        raise ValueError("CAD2BIM_SYSTEM_PUBLICATION_SUBJECT is required for the management identity")
    if resume_request_id is None and (selector_path is None or library_path is None):
        raise ValueError("a new publication requires the explicit source selector and current library")
    database = create_database(settings)
    try:
        await database.verify_ready()
        async with database.engine.connect() as connection:
            await require_system_curator(connection)
        repository = PostgresControlPlaneRepository(database.session_factory)
        identities = PostgresIdentityDirectory(database.session_factory)
        context = await identities.resolve_configured_local_subject(
            ConfiguredLocalSubject(subject=settings.system_publication_subject),
            command_id=stable_internal_identity_id("publication_", "system-library-command", publication_name),
        )
        context = context.model_copy(update={"actor_kind": TenantActorKind.SYSTEM_CURATOR})
        scope = await repository.open_system_family_publication_scope(context, publication_name)
        storage = LocalStorage(settings.storage_root, max_size_bytes=settings.max_upload_bytes)
        await storage.verify_ready()
        source = None
        if resume_request_id is not None:
            operation_id = stable_internal_identity_id(
                "op_", "system-family-publication-operation", scope.run_id, resume_request_id,
            )
            with bind_tenant_execution_context(scope.identity):
                await repository.authorize_system_family_publication(scope)
                saved = await repository.get_product_family_authoring_operation_state(scope, operation_id=operation_id)
            if saved is None or saved.detail.mode != "system_copy":
                raise ValueError("the exact management publication request was not found")
            plan = saved.detail.system_copy
            commands = (SystemFamilyPublicationCommand(
                request_id=resume_request_id, source_family_id=plan.source_family_id,
                source_family_version_id=plan.source_family_version_id,
                target_owner=plan.target_owner,
            ),)
        else:
            from CAD2BIM_backend.infrastructure.system_library.selected_source import SelectedLibrarySystemFamilySource
            source = SelectedLibrarySystemFamilySource(selector_path=selector_path, library_path=library_path)
            commands = source.commands(request_prefix=request_prefix)
            if family_key is not None:
                commands = tuple(command for command in commands if command.request_id == f"{request_prefix}:{family_key}")
                if not commands:
                    raise ValueError("family_key is not in the authorized current selection")
        service = create_system_family_publication_service(
            repository=repository, storage=storage, artifacts=ArtifactService(repository, storage),
            cursor_codec=CursorCodec(secrets.token_urlsafe(32)), source=source, bind_identity=bind_tenant_execution_context,
        )
        results = []
        for command in commands:
            committed = await service.publish(scope, command)
            results.append({
                "request_id": command.request_id, "operation_id": committed.operation.operation.operation_id,
                "family_id": committed.family_version.family.family_id,
                "family_version_id": committed.family_version.version.family_version_id,
                "geometry_mapping": [
                    {"source_resource_version_id": item.source_resource_version_id,
                     "resource_id": item.target_version.resource.resource_id,
                     "resource_version_id": item.target_version.version.resource_version_id,
                     "content_digest": item.target_version.version.content_digest}
                    for item in committed.operation.operation.detail.system_copy.geometry_copies
                ],
            })
        return tuple(results)
    finally:
        await database.dispose()


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication-name", required=True)
    parser.add_argument("--request-prefix", default="system-library")
    parser.add_argument("--selector", type=Path)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--family-key")
    parser.add_argument("--resume-request-id")
    args = parser.parse_args(argv)
    result = asyncio.run(run_selected_system_publication(
        publication_name=args.publication_name, request_prefix=args.request_prefix,
        selector_path=args.selector, library_path=args.library,
        family_key=args.family_key, resume_request_id=args.resume_request_id,
    ))
    print(json.dumps({"publications": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
