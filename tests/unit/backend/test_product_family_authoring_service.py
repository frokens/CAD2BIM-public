from __future__ import annotations
from CAD2BIM_backend.infrastructure.source_text.source_patch_adapter import TextSourcePatchAdapter


import hashlib


import json


from datetime import UTC, datetime


from types import SimpleNamespace
from tests.unit.backend.test_system_family_publication_service import _family


import pytest


from CAD2BIM_backend.application.artifact_service import ArtifactDownload


from CAD2BIM_backend.application.product_family_asset_service import (
    ProductFamilyAssetService,
)


from CAD2BIM_backend.application.product_family_authoring_service import (
    ProductFamilyAuthoringApplicationError,
    ProductFamilyAuthoringService,
)


from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputService


from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest


from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyCatalogNotFoundError


from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ExecutionMode


from CAD2BIM_backend.domain.data_runtime import AuthoringExecutionClaim, AuthoringExecutionState, CatalogResourceKind, CriticalOperationRecord, CriticalOperationStatus, DataCatalogEntry, PublishedCriticalOperation


from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterface


from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceInput,
    GeometryResourceKind,
    GeometryResourceRecord,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    GeometryResourceVersionRecord,
    geometry_resource_content_digest,
)


from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometryFormat, GeometryBounds, GeometrySummary, GeometryTopologySummary


from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext


from CAD2BIM_backend.domain.invocation import InvocationScope


from CAD2BIM_backend.domain.product_family import ProductFamilyVersionAssetRole


from CAD2BIM_backend.domain.product_family_authoring import ProductFamilyAuthoringCommitResult


from CAD2BIM_backend.domain.product_realization import product_geometry_binding_digest


from CAD2BIM_backend.domain.resource_scope import (
    ResourceOwnerScope,
    ResourceOwnerScopeKind,
)


from CAD2BIM_backend.domain.system_family_publication import (
    SystemFamilyPublicationCommand,
    SystemGeometryCopyDraft,
)


from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import (
    SourceTextGeometryAuthoringSource,
)


from CAD2BIM_backend.ports.storage import StoredObject, VerifiedStorageRead


pytestmark = pytest.mark.asyncio


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


BREP = b"canonical-product-body-brep"


def _scope() -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-authoring",
            principal_id="principal-authoring",
            task_id="task-authoring",
            actor_kind=TenantActorKind.USER,
            command_id="command-authoring",
        ),
        owner_id="tenant-authoring",
        run_id="run-authoring",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=None,
    )


def _summary() -> GeometrySummary:
    return GeometrySummary(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        bounds=GeometryBounds(
            minimum_m=(0.0, 0.0, 0.0),
            maximum_m=(1.0, 2.0, 3.0),
        ),
        topology=GeometryTopologySummary(
            vertex_count=8,
            edge_count=12,
            wire_count=6,
            face_count=6,
            shell_count=1,
            solid_count=1,
            triangle_count=0,
            boundary_edge_count=0,
        ),
        surface_area_m2=22.0,
        volume_m3=6.0,
    )


def _interfaces() -> tuple[GeometryModelInterface, ...]:
    return (
        GeometryModelInterface(interface_key="datum", local_frame={}),
        GeometryModelInterface(
            interface_key="outlet",
            local_frame={"translation_m": (1.0, 2.0, 3.0)},
            profile={"kind": "rectangle", "width_m": 0.2, "height_m": 0.1},
        ),
    )


def _geometry(
    *,
    content: bytes = BREP,
    asset_format: str = "occt_brep_v1",
    interfaces: tuple[GeometryModelInterface, ...] | None = None,
    version_id: str = "geometry-body-version-1",
    resource_id: str = "geometry-body",
    geometry_inputs: tuple[GeometryResourceInput, ...] = (),
) -> tuple[GeometryResourceVersionBundle, dict]:
    resource = GeometryResourceRecord(
        resource_id=resource_id,
        resource_kind=GeometryResourceKind.MAPPED_MODEL,
        owner_scope=ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind.SYSTEM),
        display_name="System product body",
        created_at=NOW,
    )
    manifest: dict = {
        "schema": "geometry_resource_manifest_v1",
        "geometry_summary": _summary().model_dump(mode="json"),
    }
    interfaces = interfaces or (GeometryModelInterface(interface_key="datum", local_frame={}),)
    manifest["interfaces"] = [item.model_dump(mode="json") for item in interfaces]
    if geometry_inputs:
        manifest["geometry_inputs"] = [item.model_dump(mode="json") for item in geometry_inputs]
    manifest_content = canonical_json(manifest).encode("utf-8")
    contents = {
        GeometryResourceAssetRole.CANONICAL_GEOMETRY: content,
        GeometryResourceAssetRole.MANIFEST: manifest_content,
    }
    specs = {
        GeometryResourceAssetRole.CANONICAL_GEOMETRY: (
            asset_format,
            "body.brep",
            "application/vnd.opencascade.brep",
        ),
        GeometryResourceAssetRole.MANIFEST: (
            "json",
            "manifest.json",
            "application/json",
        ),
    }
    assets = tuple(
        GeometryResourceVersionAssetRecord(
            resource_version_id=version_id,
            asset_role=role,
            asset_format=specs[role][0],
            filename=specs[role][1],
            content_type=specs[role][2],
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            created_at=NOW,
        )
        for role, payload in contents.items()
    )
    manifest_digest = sha256_digest(manifest)
    version = GeometryResourceVersionRecord(
        resource_version_id=version_id,
        resource_id=resource.resource_id,
        version_no=2 if version_id != "geometry-body-version-1" else 1,
        origin_kind="test_fixture",
        content_digest=geometry_resource_content_digest(
            resource_kind=resource.resource_kind,
            manifest_digest=manifest_digest,
            assets=assets,
        ),
        manifest_digest=manifest_digest,
        manifest=manifest,
        created_at=NOW,
    )
    return (
        GeometryResourceVersionBundle(
            resource=resource,
            version=version,
            assets=assets,
        ),
        contents,
    )


def _function_geometry(
    *,
    schema_content: bytes | None = None,
    source_format: str = "source_bundle_v1",
) -> tuple[GeometryResourceVersionBundle, dict]:
    source = (
        SourceTextGeometryAuthoringSource()
        .pack({"main.py": b"def build(parameters):\n    return object()\n"})
        .content
    )
    parameter_schema = schema_content or canonical_json(
        {
            "additionalProperties": False,
            "properties": {},
            "required": [],
            "type": "object",
        }
    ).encode("utf-8")
    resource = GeometryResourceRecord(
        resource_id="geometry-function",
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        owner_scope=ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind.SYSTEM),
        display_name="System product function",
        created_at=NOW,
    )
    version_id = "geometry-function-version-1"
    manifest = {"schema": "geometry_resource_manifest_v1"}
    contents = {
        GeometryResourceAssetRole.SOURCE_BUNDLE: source,
        GeometryResourceAssetRole.PARAMETER_SCHEMA: parameter_schema,
    }
    specs = {
        GeometryResourceAssetRole.SOURCE_BUNDLE: (
            source_format,
            "source.zip",
            "application/zip",
        ),
        GeometryResourceAssetRole.PARAMETER_SCHEMA: (
            "json_schema",
            "parameters.json",
            "application/schema+json",
        ),
    }
    assets = tuple(
        GeometryResourceVersionAssetRecord(
            resource_version_id=version_id,
            asset_role=role,
            asset_format=specs[role][0],
            filename=specs[role][1],
            content_type=specs[role][2],
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            created_at=NOW,
        )
        for role, payload in contents.items()
    )
    manifest_digest = sha256_digest(manifest)
    return (
        GeometryResourceVersionBundle(
            resource=resource,
            version=GeometryResourceVersionRecord(
                resource_version_id=version_id,
                resource_id=resource.resource_id,
                version_no=1,
                origin_kind="test_fixture",
                content_digest=geometry_resource_content_digest(
                    resource_kind=resource.resource_kind,
                    manifest_digest=manifest_digest,
                    assets=assets,
                ),
                manifest_digest=manifest_digest,
                manifest=manifest,
                created_at=NOW,
            ),
            assets=assets,
        ),
        contents,
    )


class _Artifacts:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.by_key: dict[str, tuple[ArtifactRecord, bytes]] = {}
        self.by_version: dict[tuple[str, str], tuple[ArtifactRecord, bytes]] = {}

    async def upload_bytes(self, **values) -> ArtifactRecord:
        self.events.append("artifact:source")
        key = values["idempotency_key"]
        content = values["content"]
        existing = self.by_key.get(key)
        if existing is not None:
            assert existing[1] == content
            return existing[0]
        suffix = hashlib.sha256(key.encode()).hexdigest()[:24]
        record = ArtifactRecord(
            artifact_id=f"artifact_{suffix}",
            version_id=f"artifact_version_{suffix}",
            owner_id=values["owner_id"],
            task_id=values["task_id"],
            kind=values["kind"],
            filename=values["filename"],
            content_type=values["content_type"],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            idempotency_key=key,
            created_at=NOW,
            run_id=values["run_id"],
            version_metadata=values["version_metadata"],
        )
        self.by_key[key] = (record, content)
        self.by_version[(record.artifact_id, record.version_id)] = (record, content)
        return record

    async def download_version(
        self,
        owner_id: str,
        artifact_id: str,
        version_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactDownload:
        record, content = self.by_version[(artifact_id, version_id)]
        assert record.owner_id == owner_id
        assert record.run_id == run_id
        self.events.append("artifact:read-exact")
        return ArtifactDownload(artifact=record, content=content)


class _Storage:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.objects: dict[object, tuple[StoredObject, bytes]] = {}
        self.read_keys: list[object] = []

    async def write(self, key, *, filename, content_type, chunks):
        self.events.append(f"storage:{key.asset_role}")
        content = b"".join([chunk async for chunk in chunks])
        stored = StoredObject(
            key=key,
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=NOW,
        )
        existing = self.objects.get(key)
        if existing is not None:
            assert existing == (stored, content)
            return existing[0]
        self.objects[key] = (stored, content)
        return stored

    async def stat(self, key):
        return self.objects[key][0]

    async def read(self, key):
        self.read_keys.append(key)
        return self.objects[key][1]

    async def read_verified(self, key):
        self.read_keys.append(key)
        stored, content = self.objects[key]
        return VerifiedStorageRead(
            stored=stored,
            content=content,
            verified_sha256=hashlib.sha256(content).hexdigest(),
        )


class _GeometryResources:

    def __init__(
        self,
        bundle: GeometryResourceVersionBundle,
        contents: dict,
        events: list[str],
    ) -> None:
        self.bundle: GeometryResourceVersionBundle | None = bundle
        self.contents = contents
        self.events = events
        self.get_calls = 0
        self.contexts: list[TenantExecutionContext] = []

    async def get_exact_version(self, context, *, resource_id, resource_version_id):
        self.events.append("geometry:reauthorize")
        self.get_calls += 1
        self.contexts.append(context)
        if self.bundle is None:
            return None
        if (resource_id != self.bundle.resource.resource_id
                or resource_version_id != self.bundle.version.resource_version_id):
            return None
        return self.bundle

    async def read_exact_asset(
        self,
        context,
        *,
        resource_id,
        resource_version_id,
        asset_role,
    ):
        self.events.append(f"geometry:read:{asset_role.value}")
        self.contexts.append(context)
        if self.bundle is None:
            return None
        assert resource_id == self.bundle.resource.resource_id
        assert resource_version_id == self.bundle.version.resource_version_id
        asset = next(item for item in self.bundle.assets if item.asset_role is asset_role)
        return asset, self.contents[asset_role]


class _Catalog:
    def __init__(self, repository: _Repository, events: list[str]) -> None:
        self.repository = repository
        self.events = events

    def version_ref(self, context, bundle):
        return bundle.version.family_version_id

    async def version_ref_for_visible_id(self, context, family_version_id):
        if family_version_id not in self.repository.bundles:
            raise ProductFamilyCatalogNotFoundError("The exact ProductFamily version is not visible")
        return family_version_id

    async def resolve_exact_version(self, context, reference):
        self.events.append("catalog:read-exact")
        return self.repository.bundles[reference]


class _Repository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.states: dict[str, CriticalOperationRecord] = {}
        self.published: dict[str, PublishedCriticalOperation] = {}
        self.bundles: dict[str, object] = {}
        self.commit_response_loss = False
        self.commit_failures: list[Exception] = []
        self.prepare_failures_remaining = 0
        self.state_read_failures_remaining = 0
        self.prepare_state_read_failures_remaining = 0
        self.commit_state_read_failures_remaining = 0
        self.published_read_failures_remaining = 0
        self.state_read_calls = 0
        self.marked_indeterminate = 0
        self.reopened = 0
        self.publish_terminal_during_indeterminate = False
        self.execution_owners: dict[str, str] = {}
        self.execution_generations: dict[str, int] = {}
        self.claim_denied = False
        self.system_publication_allowed = True
        self.claim_calls = 0

    async def authorize_system_family_publication(self, scope):
        self.events.append("system:authorize")
        if not self.system_publication_allowed:
            raise PermissionError("SYSTEM publication is not authorized")

    async def get_latest_product_family_version(self, context, *, family_id):
        return max(
            (bundle for bundle in self.bundles.values() if bundle.family.family_id == family_id),
            key=lambda bundle: bundle.version.version_no,
            default=None,
        )

    async def get_latest_product_family_version_id(self, context, *, family_id):
        bundle = max((bundle for bundle in self.bundles.values() if bundle.family.family_id == family_id),
            key=lambda bundle: bundle.version.version_no, default=None)
        return None if bundle is None else bundle.version.family_version_id

    async def get_product_family_authoring_execution_state(self, scope, operation_id):
        operation = self.states.get(operation_id)
        return None if operation is None else AuthoringExecutionState(
            operation=operation,
            execution_owner=self.execution_owners.get(operation_id),
            execution_generation=self.execution_generations.get(operation_id, 0),
        )

    async def claim_product_family_authoring_execution(self, scope, operation_id):
        self.claim_calls += 1
        if self.claim_denied or operation_id in self.execution_owners:
            return None
        generation = self.execution_generations.get(operation_id, 0) + 1
        self.execution_generations[operation_id] = generation
        self.execution_owners[operation_id] = "test-owner"
        return AuthoringExecutionClaim(
            operation_id=operation_id, owner="test-owner", generation=generation,
            token="t" * 32,
        )

    async def reclaim_product_family_authoring_execution(
        self, scope, operation_id, *, expected_owner, expected_generation,
    ):
        return None

    async def prepare_product_family_authoring_operation(self, scope, operation):
        self.events.append("repository:prepare")
        if self.prepare_failures_remaining:
            self.prepare_failures_remaining -= 1
            if self.prepare_state_read_failures_remaining:
                self.prepare_state_read_failures_remaining -= 1
                self.state_read_failures_remaining = 1
            raise RuntimeError("simulated preparation failure")
        existing = self.states.get(operation.operation_id)
        if existing is not None:
            assert existing.request_fingerprint == operation.request_fingerprint
            assert existing.code_artifact == operation.code_artifact
            return existing
        self.states[operation.operation_id] = operation
        return operation

    async def complete_product_family_authoring_operation(self, scope, operation, *, execution_claim):
        self.events.append(f"repository:complete:{operation.status.value}")
        existing = self.states[operation.operation_id]
        if existing.status is not CriticalOperationStatus.PREPARED:
            return self.published[operation.operation_id]
        self.states[operation.operation_id] = operation
        self.execution_owners.pop(operation.operation_id, None)
        return self._publish(operation)

    async def mark_product_family_authoring_indeterminate(self, scope, operation, *, execution_claim):
        self.events.append("repository:indeterminate")
        self.marked_indeterminate += 1
        self.states[operation.operation_id] = operation
        self.execution_owners.pop(operation.operation_id, None)
        if self.publish_terminal_during_indeterminate:
            failed = CriticalOperationRecord.model_validate(
                operation.model_copy(
                    update={
                        "status": CriticalOperationStatus.FAILED,
                        "error_phase": "execution",
                    }
                )
            )
            self._publish(failed)
        return operation

    async def reopen_product_family_authoring_operation(self, scope, operation, *, execution_claim):
        self.events.append("repository:reopen")
        self.reopened += 1
        self.states[operation.operation_id] = operation
        return operation

    async def commit_product_family_authoring_success(self, scope, publication, *, execution_claim):
        self.events.append("repository:commit")
        if self.commit_failures:
            if self.commit_state_read_failures_remaining:
                self.commit_state_read_failures_remaining -= 1
                self.state_read_failures_remaining = 1
            raise self.commit_failures.pop(0)
        existing = self.states.get(publication.operation.operation_id)
        if existing is not None and existing.status is CriticalOperationStatus.SUCCEEDED:
            bundle = self.bundles[existing.result_product_family_version_id]
            return ProductFamilyAuthoringCommitResult(
                operation=self.published[existing.operation_id],
                family_version=bundle,
            )
        version_no = 1 + sum(
            bundle.family.family_id == publication.family.family_id
            for bundle in self.bundles.values()
        )
        bundle = publication.to_bundle(version_no=version_no)
        self.bundles[bundle.version.family_version_id] = bundle
        self.states[publication.operation.operation_id] = publication.operation
        self.execution_owners.pop(publication.operation.operation_id, None)
        published = self._publish(publication.operation)
        result = ProductFamilyAuthoringCommitResult(
            operation=published,
            family_version=bundle,
        )
        if self.commit_response_loss:
            self.commit_response_loss = False
            raise RuntimeError("simulated commit response loss")
        return result

    async def get_product_family_authoring_operation_state(
        self,
        scope,
        *,
        operation_id,
    ):
        self.state_read_calls += 1
        if self.state_read_failures_remaining:
            self.state_read_failures_remaining -= 1
            raise RuntimeError("simulated state read failure")
        return self.states.get(operation_id)

    async def get_product_family_authoring_operation(self, scope, *, operation_id):
        if self.published_read_failures_remaining:
            self.published_read_failures_remaining -= 1
            raise RuntimeError("simulated published Operation read failure")
        return self.published.get(operation_id)

    def _publish(self, operation: CriticalOperationRecord) -> PublishedCriticalOperation:
        existing = self.published.get(operation.operation_id)
        if existing is not None:
            return existing
        ordinal = len(self.published) + 1
        published = PublishedCriticalOperation(
            operation=operation,
            catalog_entry=DataCatalogEntry(
                catalog_entry_id=f"catalog-{ordinal}",
                owner_id=operation.owner_id,
                tenant_id=operation.tenant_id,
                task_id=operation.task_id,
                run_id=operation.run_id,
                resource_kind=CatalogResourceKind.OPERATION,
                alias=f"op_{ordinal:03d}",
                ordinal=ordinal,
                catalog_revision=ordinal,
                operation_id=operation.operation_id,
                created_at=operation.finished_at or operation.started_at,
            ),
        )
        self.published[operation.operation_id] = published
        return published


def _service(
    geometry_data: tuple[GeometryResourceVersionBundle, dict] | None = None,
):
    events: list[str] = []
    geometry, geometry_contents = geometry_data or _geometry()
    repository = _Repository(events)
    artifacts = _Artifacts(events)
    storage = _Storage(events)
    source_bundles = SourceTextGeometryAuthoringSource()
    family_assets = ProductFamilyAssetService(storage, source_bundles)
    geometry_resources = _GeometryResources(geometry, geometry_contents, events)
    runtime = SimpleNamespace(realize_calls=0)
    realization_inputs = ProductRealizationInputService(
        family_assets,
        geometry_resources,
        runtime=runtime,
        family_repository=repository,
    )
    catalog = _Catalog(repository, events)
    service = ProductFamilyAuthoringService(
        repository,
        artifacts,
        catalog,
        family_assets,
        realization_inputs,
        source_bundles,
        source_patch=TextSourcePatchAdapter(),
        clock=lambda: NOW,
    )
    return (
        service,
        repository,
        artifacts,
        storage,
        geometry_resources,
        runtime,
        events,
        geometry,
    )


async def _system_copy_fixture(*, with_transitive_dependency: bool = False, contract_updates=None):
    fixed, _ = _geometry(
        resource_id="geometry-fixed-input", version_id="geometry-fixed-input-version",
    )
    fixed = GeometryResourceVersionBundle.model_validate({
        **fixed.model_dump(),
    })
    fixed_target = GeometryResourceVersionBundle.model_validate({
        "resource": fixed.resource.model_copy(update={"resource_id": "system-copy-fixed-input"}),
        "version": fixed.version.model_copy(update={
            "resource_id": "system-copy-fixed-input",
            "resource_version_id": "system-copy-fixed-input-version", "version_no": 1,
        }),
        "assets": tuple(asset.model_copy(update={
            "resource_version_id": "system-copy-fixed-input-version",
        }) for asset in fixed.assets),
    })
    fixed_sha256 = next(asset.sha256 for asset in fixed.assets
                        if asset.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY)
    source_inputs = (GeometryResourceInput(
        input_key="fixed_body", resource_id=fixed.resource.resource_id,
        resource_version_id=fixed.version.resource_version_id,
        content_digest=fixed.version.content_digest, canonical_geometry_sha256=fixed_sha256,
    ),) if with_transitive_dependency else ()
    target_inputs = (source_inputs[0].model_copy(update={
        "resource_id": fixed_target.resource.resource_id,
        "resource_version_id": fixed_target.version.resource_version_id,
    }),) if with_transitive_dependency else ()
    geometry, contents = _geometry(geometry_inputs=source_inputs)
    geometry = GeometryResourceVersionBundle.model_validate({
        **geometry.model_dump(),
        "resource": geometry.resource.model_copy(update={
            "owner_scope": ResourceOwnerScope(
                scope_kind=ResourceOwnerScopeKind.TASK,
                tenant_id=_scope().identity.tenant_id,
                task_id=_scope().identity.task_id,
            ),
        }),
    })
    authoring, repository, artifacts, storage, resources, runtime, events, _ = _service((geometry, contents))
    source, source_assets = _family(geometry, "source-family", contract_updates=contract_updates)
    source = source.model_copy(update={"family": source.family.model_copy(update={"owner_scope": geometry.resource.owner_scope})})
    repository.bundles[source.version.family_version_id] = source
    for asset, body in zip(source.assets, source_assets, strict=True):
        await authoring._family_assets.write_exact_asset(source.family, asset, body)
    source_result = SimpleNamespace(family_version=source)
    source_assets = tuple([
        await authoring._family_assets.read_exact_asset(source, asset)
        for asset in source.assets
    ])
    target, _ = _geometry(
        resource_id="system-copy-geometry", version_id="system-copy-geometry-version",
        geometry_inputs=target_inputs,
    )
    target = GeometryResourceVersionBundle.model_validate({
        **target.model_dump(),
        "version": target.version.model_copy(update={
            "version_no": 1,
        }),
    })
    copied = SystemGeometryCopyDraft(
        source_resource_id=geometry.resource.resource_id,
        source_resource_version_id=geometry.version.resource_version_id,
        source_content_digest=geometry.version.content_digest,
        source_owner_scope=geometry.resource.owner_scope,
        target_version=target,
    )
    geometry_copies = (SystemGeometryCopyDraft(
        source_resource_id=fixed.resource.resource_id,
        source_resource_version_id=fixed.version.resource_version_id,
        source_content_digest=fixed.version.content_digest,
        source_owner_scope=fixed.resource.owner_scope, target_version=fixed_target,
    ), copied) if with_transitive_dependency else (copied,)
    scope = _scope().model_copy(update={
        "identity": _scope().identity.model_copy(update={"task_id": "management-task", "actor_kind": TenantActorKind.SYSTEM_CURATOR}),
        "run_id": "management-run",
    })
    inputs = ProductRealizationInputService(
        authoring._family_assets, resources, runtime=None, family_repository=repository,
    )
    service = ProductFamilyAuthoringService(
        repository, artifacts, authoring._family_catalog, authoring._family_assets,
        inputs, authoring._source_bundles, source_patch=TextSourcePatchAdapter(), clock=lambda: NOW,
    )
    command = SystemFamilyPublicationCommand(
        request_id="publish-family",
        source_family_id=source.family.family_id,
        source_family_version_id=source.version.family_version_id,
    )
    resources.bundle = None  # Prepared Geometry bytes are not yet published in the DB.
    events.clear()
    return {
        "service": service, "authoring": authoring, "repository": repository,
        "artifacts": artifacts, "storage": storage, "resources": resources,
        "runtime": runtime, "events": events, "scope": scope, "command": command,
        "source_result": source_result, "target": target,
        "prepare": {
            "source": source, "source_assets": source_assets,
            "source_kind": "selected_library", "source_reference": "selected-family:body",
            "geometry_copies": geometry_copies,
        },
    }


@pytest.mark.parametrize(("with_transitive_dependency", "standard_parameters"), [
    (False, {}), (True, {}), pytest.param(False, {"diameter": 0.2}, id="explicit_standard_parameters"),
])
async def test_system_copy_preserves_source_and_standard_assets_without_runtime(with_transitive_dependency, standard_parameters):
    contract_updates = {"public_parameters": [{"parameter_kind": "number", "parameter_key": "diameter",
        "unit": "metre", "minimum": 0.01, "maximum": 1}], "standard_model": {"public_parameters": standard_parameters}} if standard_parameters else None
    case = await _system_copy_fixture(with_transitive_dependency=with_transitive_dependency, contract_updates=contract_updates)
    service, source = case["service"], case["prepare"]["source"]
    before_calls = case["runtime"].realize_calls
    reads_before = case["resources"].get_calls
    prepared = await service.prepare_system_copy(case["scope"], case["command"], **case["prepare"])

    assert service._realization_inputs._runtime is None
    assert prepared.status is CriticalOperationStatus.PREPARED
    assert prepared.detail.mode == "system_copy"
    from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import _operation_detail_json
    persisted = _operation_detail_json(prepared)
    assert persisted["version_draft"]["family_contract"]["standard_model"] == (
        {"public_parameters": standard_parameters} if standard_parameters else {})
    assert case["resources"].get_calls == reads_before
    assert case["events"][0] == "system:authorize"
    assert len(case["repository"].bundles) == 1
    assert prepared.code_artifact.sha256 == source.assets[0].sha256
    assert prepared.task_id == case["scope"].identity.task_id
    assert prepared.base_product_family_version_id is None
    assert prepared.detail.version_draft.parent_family_version_id is None
    assert prepared.detail.system_copy.geometry_copies == case["prepare"]["geometry_copies"]
    if with_transitive_dependency:
        fixed, direct = prepared.detail.system_copy.geometry_copies
        declared = direct.target_version.version.manifest["geometry_inputs"][0]
        assert declared["resource_version_id"] == fixed.target_version.version.resource_version_id
        assert len(prepared.detail.version_draft.geometry_bindings) == 1
    case["resources"].bundle = case["target"]
    claim = await service.claim_system_copy(case["scope"], prepared)
    result = await service.commit_system_copy(case["scope"], prepared, execution_claim=claim)

    published = result.family_version
    assert published.family.owner_scope == case["command"].target_owner
    assert published.version.version_no == 1
    assert published.family.family_id != source.family.family_id
    assert published.version.family_version_id != source.version.family_version_id
    assert published.version.family_contract == source.version.family_contract
    assert published.version.family_contract.standard_model.public_parameters == standard_parameters
    assert (published.version.display_name, published.version.purpose, published.version.usage_instructions) == (
        source.version.display_name, source.version.purpose, source.version.usage_instructions,
    )
    assert case["repository"].bundles[source.version.family_version_id] == source
    source_content = dict(zip((asset.asset_role for asset in source.assets), case["prepare"]["source_assets"], strict=True))
    for asset in published.assets:
        content = await service._family_assets.read_exact_asset(published, asset)
        if asset.asset_role is ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST:
            manifest = json.loads(content)
            assert manifest["family_version_id"] == published.version.family_version_id
            assert manifest["binding_digest"] == product_geometry_binding_digest(
                (binding.binding_key, binding.expected_resource_kind, binding.geometry_content_digest)
                for binding in published.geometry_bindings
            )
            assert content != source_content[asset.asset_role]
        else:
            assert content == source_content[asset.asset_role]
    assert published.geometry_bindings[0].geometry_resource_version_id == case["target"].version.resource_version_id
    assert published.version.content_digest != source.version.content_digest
    assert result.operation.operation.detail.system_copy.geometry_copies == case["prepare"]["geometry_copies"]
    assert case["runtime"].realize_calls == before_calls
    assert not any(event.startswith("runtime:") for event in case["events"])


@pytest.mark.parametrize("commit_response_loss", [False, True])
async def test_system_copy_recovers_saved_plan_after_source_endpoint_is_gone(commit_response_loss):
    case = await _system_copy_fixture()
    service, repository = case["service"], case["repository"]
    prepared = await service.prepare_system_copy(case["scope"], case["command"], **case["prepare"])
    source_result = case["source_result"]
    repository.bundles.pop(source_result.family_version.version.family_version_id)
    for key in tuple(case["storage"].objects):
        if key.family_version_id == source_result.family_version.version.family_version_id:
            del case["storage"].objects[key]
    case["resources"].bundle = case["target"]
    restored = ProductFamilyAuthoringService(
        repository, case["artifacts"], service._family_catalog, service._family_assets,
        service._realization_inputs, service._source_bundles, source_patch=TextSourcePatchAdapter(), clock=lambda: NOW,
    )
    original = await restored.find_system_copy(case["scope"], case["command"])
    assert original == prepared
    # An original request ignores fresh source preparation; it resumes saved facts.
    assert await restored.prepare_system_copy(
        case["scope"], case["command"], source=None, source_assets=(),
        source_kind="selected_library", source_reference="offline", geometry_copies=(),
    ) == prepared
    claim = await restored.claim_system_copy(case["scope"], original)
    repository.commit_response_loss = commit_response_loss
    result = await restored.commit_system_copy(case["scope"], original, execution_claim=claim)
    claims_after_commit = repository.claim_calls
    replay = await restored.commit_system_copy(case["scope"], original)
    assert replay == result
    assert repository.claim_calls == claims_after_commit
    assert len(repository.bundles) == 1
    assert case["events"].count("repository:commit") == 1
    assert case["runtime"].realize_calls == 0


async def test_system_copy_authorizes_before_source_or_storage_and_conflicts_before_preparation():
    case = await _system_copy_fixture()
    service, repository = case["service"], case["repository"]
    artifacts_before = len(case["artifacts"].by_key)
    storage_before = dict(case["storage"].objects)
    repository.system_publication_allowed = False
    with pytest.raises(PermissionError):
        await service.prepare_system_copy(case["scope"], case["command"], **case["prepare"])
    assert case["events"] == ["system:authorize"]
    assert len(case["artifacts"].by_key) == artifacts_before
    assert case["storage"].objects == storage_before
    repository.system_publication_allowed = True
    prepared = await service.prepare_system_copy(case["scope"], case["command"], **case["prepare"])
    artifacts_before = len(case["artifacts"].by_key)
    reads_before = len(case["storage"].read_keys)
    for change in (
        {"source_family_version_id": "a-different-source-version"},
        {"source_family_id": "a-different-source-family"},
        {"target_owner": ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind.TENANT, tenant_id="other")},
    ):
        with pytest.raises(ProductFamilyAuthoringApplicationError) as conflict:
            await service.find_system_copy(case["scope"], case["command"].model_copy(update=change))
        assert conflict.value.code == "PRODUCT_FAMILY_IDEMPOTENCY_CONFLICT"
    assert len(case["artifacts"].by_key) == artifacts_before
    assert len(case["storage"].read_keys) == reads_before
    assert await service.find_system_copy(case["scope"], case["command"]) == prepared


@pytest.mark.parametrize("failure", [
    "stale_source", "missing_bytes", "corrupt_bytes",
    "missing_mapping", "private_target",
])
async def test_system_copy_rejects_inexact_source_and_mapping_before_family_writes(failure):
    case = await _system_copy_fixture()
    values = dict(case["prepare"])
    if failure == "stale_source":
        values["source"] = values["source"].model_copy(update={
            "version": values["source"].version.model_copy(update={"family_version_id": "changed-version"}),
        })
    elif failure == "missing_bytes":
        values["source_assets"] = values["source_assets"][:-1]
    elif failure == "corrupt_bytes":
        values["source_assets"] = (*values["source_assets"][:-1], b"corrupt manifest bytes")
    elif failure == "missing_mapping":
        values["geometry_copies"] = ()
    else:
        copied = values["geometry_copies"][0]
        values["geometry_copies"] = (copied.model_copy(update={
            "target_version": copied.target_version.model_copy(update={
                "resource": copied.target_version.resource.model_copy(update={
                    "owner_scope": case["prepare"]["source"].family.owner_scope,
                }),
            }),
        }),)
    artifacts_before = len(case["artifacts"].by_key)
    states_before = len(case["repository"].states)
    storage_before = dict(case["storage"].objects)
    with pytest.raises(ProductFamilyAuthoringApplicationError) as invalid:
        await case["service"].prepare_system_copy(case["scope"], case["command"], **values)
    assert invalid.value.code in {"PRODUCT_FAMILY_SYSTEM_COPY_INVALID", "PRODUCT_FAMILY_STORAGE_INTEGRITY_FAILED"}
    assert len(case["artifacts"].by_key) == artifacts_before
    assert len(case["repository"].states) == states_before
    assert case["storage"].objects == storage_before
    assert case["runtime"].realize_calls == 0


async def test_system_copy_requires_original_claim_and_rejects_replaced_saved_plan():
    case = await _system_copy_fixture()
    service, repository = case["service"], case["repository"]
    prepared = await service.prepare_system_copy(case["scope"], case["command"], **case["prepare"])
    with pytest.raises(ProductFamilyAuthoringApplicationError) as absent_claim:
        await service.commit_system_copy(case["scope"], prepared)
    assert absent_claim.value.code == "PRODUCT_FAMILY_RECONCILIATION_REQUIRED"
    replaced = prepared.model_copy(update={
        "detail": prepared.detail.model_copy(update={
            "system_copy": prepared.detail.system_copy.model_copy(update={"source_reference": "replaced"}),
        }),
    })
    with pytest.raises(ProductFamilyAuthoringApplicationError) as replaced_plan:
        await service.claim_system_copy(case["scope"], replaced)
    assert replaced_plan.value.code == "PRODUCT_FAMILY_OPERATION_INTEGRITY_FAILED"
    repository.claim_denied = True
    with pytest.raises(ProductFamilyAuthoringApplicationError) as fenced:
        await service.claim_system_copy(case["scope"], prepared)
    assert fenced.value.code == "PRODUCT_FAMILY_RECONCILIATION_REQUIRED"
    assert len(repository.bundles) == 1
    assert case["events"].count("repository:commit") == 0
    assert case["runtime"].realize_calls == 0


async def test_system_copy_rechecks_saved_assets_before_commit():
    case = await _system_copy_fixture()
    service, repository = case["service"], case["repository"]
    prepared = await service.prepare_system_copy(case["scope"], case["command"], **case["prepare"])
    claim = await service.claim_system_copy(case["scope"], prepared)
    case["resources"].bundle = case["target"]
    target_source = next(asset for asset in prepared.detail.system_copy.family_assets
                         if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE)
    family = case["prepare"]["source"].family.model_copy(update={
        "family_id": prepared.target_product_family_id,
        "owner_scope": prepared.detail.system_copy.target_owner,
    })
    key = service._family_assets.candidate_storage_key(family, target_source)
    stored, content = case["storage"].objects[key]
    case["storage"].objects[key] = (stored, content + b"changed")
    with pytest.raises(ProductFamilyAuthoringApplicationError) as corrupt:
        await service.commit_system_copy(case["scope"], prepared, execution_claim=claim)
    assert corrupt.value.code == "PRODUCT_FAMILY_STORAGE_INTEGRITY_FAILED"
    failed = repository.states[prepared.operation_id]
    assert failed.status is CriticalOperationStatus.FAILED
    assert prepared.detail.version_draft.family_version_id not in repository.bundles
