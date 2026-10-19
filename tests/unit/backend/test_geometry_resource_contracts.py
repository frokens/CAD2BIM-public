from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceListQuery,
    GeometryResourcePage,
    GeometryResourceRecord,
    GeometryResourceScope,
    GeometryResourceScopeKind,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    GeometryResourceVersionRecord,
    geometry_resource_content_digest,
)
from CAD2BIM_backend.ports.geometry_resources import GeometryResourceRepository
from CAD2BIM_backend.infrastructure.postgres.repository_geometry_resource import (
    PostgresGeometryResourceRepositoryMixin,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
)


NOW = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _scope(
    scope_kind: GeometryResourceScopeKind = GeometryResourceScopeKind.TASK,
) -> GeometryResourceScope:
    if scope_kind is GeometryResourceScopeKind.SYSTEM:
        return GeometryResourceScope(scope_kind=scope_kind)
    if scope_kind is GeometryResourceScopeKind.TENANT:
        return GeometryResourceScope(scope_kind=scope_kind, tenant_id="tenant-1")
    return GeometryResourceScope(
        scope_kind=scope_kind,
        tenant_id="tenant-1",
        task_id="task-1",
    )


def _resource(
    resource_kind: GeometryResourceKind = GeometryResourceKind.MAPPED_MODEL,
    *,
    resource_id: str = "geometry-resource-1",
    created_at: datetime = NOW,
) -> GeometryResourceRecord:
    return GeometryResourceRecord(
        resource_id=resource_id,
        resource_kind=resource_kind,
        owner_scope=_scope(),
        display_name="  Regulator body  ",
        created_at=created_at,
    )


def _version(
    *,
    resource_id: str = "geometry-resource-1",
    content_digest: str = SHA_A,
) -> GeometryResourceVersionRecord:
    manifest = {"format": "occt_brep_v1", "unit": "METRE"}
    return GeometryResourceVersionRecord(
        resource_version_id="geometry-version-1",
        resource_id=resource_id,
        version_no=1,
        origin_kind="agent_build",
        content_digest=content_digest,
        manifest_digest=sha256_digest(manifest),
        manifest=manifest,
        lineage={"source_artifact_version_id": "artifact-version-1"},
        created_at=NOW,
    )


def _asset(role: GeometryResourceAssetRole) -> GeometryResourceVersionAssetRecord:
    manifest = {"format": "occt_brep_v1", "unit": "METRE"}
    manifest_content = canonical_json(manifest).encode("utf-8")
    is_manifest = role is GeometryResourceAssetRole.MANIFEST
    return GeometryResourceVersionAssetRecord(
        resource_version_id="geometry-version-1",
        asset_role=role,
        asset_format="json"
        if role is not GeometryResourceAssetRole.CANONICAL_GEOMETRY
        else "occt_brep_v1",
        filename=f"{role.value}.bin",
        content_type="application/octet-stream",
        size_bytes=len(manifest_content) if is_manifest else 10,
        sha256=sha256_digest(manifest) if is_manifest else SHA_B,
        created_at=NOW,
    )


def _bundle(resource_kind: GeometryResourceKind) -> GeometryResourceVersionBundle:
    if resource_kind is GeometryResourceKind.MAPPED_MODEL:
        roles = (
            GeometryResourceAssetRole.CANONICAL_GEOMETRY,
            GeometryResourceAssetRole.MANIFEST,
        )
    else:
        roles = (
            GeometryResourceAssetRole.SOURCE_BUNDLE,
            GeometryResourceAssetRole.PARAMETER_SCHEMA,
        )
    resource = _resource(resource_kind)
    assets = tuple(_asset(role) for role in roles)
    version = _version()
    version = version.model_copy(
        update={
            "content_digest": geometry_resource_content_digest(
                resource_kind=resource.resource_kind,
                manifest_digest=version.manifest_digest,
                assets=assets,
            )
        }
    )
    return GeometryResourceVersionBundle(
        resource=resource,
        version=version,
        assets=assets,
    )


def test_repository_publication_identity_uses_asset_roles_not_tuple_order() -> None:
    publication = _bundle(GeometryResourceKind.MAPPED_MODEL)
    equivalent = publication.model_copy(update={"assets": tuple(reversed(publication.assets))})

    PostgresGeometryResourceRepositoryMixin._require_same_geometry_publication(
        publication,
        equivalent,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["latest", "versions", "roots", "programs"])
async def test_geometry_discovery_excludes_retired_format_before_pagination(monkeypatch, entry):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from CAD2BIM_backend.domain.geometry_catalog import GeometryResourceVersionListQuery
    from CAD2BIM_backend.infrastructure.postgres import repository_geometry_resource as module

    statements = []

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def scalar(self, statement):
            statements.append(statement)
            return None
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(all=lambda: [])
        async def scalars(self, statement):
            statements.append(statement)
            return SimpleNamespace(all=lambda: [])

    repository = object.__new__(PostgresGeometryResourceRepositoryMixin)
    repository._sessions = Session
    repository._require_geometry_context = lambda _: None
    monkeypatch.setattr(module, "set_tenant_execution_context", AsyncMock())
    context = object()
    if entry == "latest":
        await repository.get_latest_geometry_resource_version(context, resource_id="root")
    elif entry == "versions":
        await repository.list_geometry_resource_versions(context, query=GeometryResourceVersionListQuery(
            resource_id="root", before_version_no=4, page_size=2))
    elif entry == "roots":
        await repository.list_geometry_resources(context, query=GeometryResourceListQuery(page_size=2))
    else:
        await repository.list_executable_program_versions(context, before_created_at=None,
            before_resource_version_id=None, limit=2)
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "validation_profile" in compiled.params.values()
    assert "NOT" in sql and "manifest_json ?" in sql
    assert sql.index("manifest_json ?") < sql.index("LIMIT")
    assert "validation_summary_json" not in sql and "validation_report" not in compiled.params.values()


def test_exact_geometry_rejects_retired_contract():
    from types import SimpleNamespace
    from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogReferenceError

    with pytest.raises(GeometryCatalogReferenceError, match="reference is unavailable"):
        PostgresGeometryResourceRepositoryMixin._geometry_version_record(SimpleNamespace(
            manifest_json={"validation_profile": "retired"},
        ))


def test_resource_kind_is_closed_to_two_consumption_semantics() -> None:
    assert {kind.value for kind in GeometryResourceKind} == {
        "mapped_model",
        "modeling_function",
    }

    with pytest.raises(ValidationError, match="Input should be"):
        _resource().model_validate(
            {
                **_resource().model_dump(mode="json"),
                "resource_kind": "generated_model",
            }
        )


@pytest.mark.parametrize(
    ("scope_kind", "tenant_id", "task_id"),
    (
        ("system", "tenant-1", None),
        ("system", None, "task-1"),
        ("tenant", None, None),
        ("tenant", "tenant-1", "task-1"),
        ("task", None, "task-1"),
        ("task", "tenant-1", None),
    ),
)
def test_owner_scope_rejects_inconsistent_owner_fields(
    scope_kind: str,
    tenant_id: str | None,
    task_id: str | None,
) -> None:
    with pytest.raises(ValidationError):
        GeometryResourceScope(
            scope_kind=scope_kind,
            tenant_id=tenant_id,
            task_id=task_id,
        )


def test_owner_scope_has_one_canonical_storage_scope_identity() -> None:
    assert _scope(GeometryResourceScopeKind.SYSTEM).scope_id == "system"
    assert _scope(GeometryResourceScopeKind.TENANT).scope_id == "tenant-1"
    assert _scope(GeometryResourceScopeKind.TASK).scope_id == "task-1"


def test_records_are_closed_frozen_and_normalize_display_name() -> None:
    resource = _resource()
    assert resource.display_name == "Regulator body"

    with pytest.raises(ValidationError, match="Instance is frozen"):
        resource.display_name = "changed"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GeometryResourceRecord.model_validate(
            {**resource.model_dump(mode="json"), "operation_alias": "op_001"}
        )


def test_version_requires_canonical_manifest_digest_and_bounded_origin_identifier() -> None:
    version = _version()
    assert version.manifest_digest == sha256_digest(version.manifest)

    payload = version.model_dump(mode="json")
    with pytest.raises(ValidationError, match="manifest_digest"):
        GeometryResourceVersionRecord.model_validate({**payload, "manifest_digest": SHA_B})
    with pytest.raises(ValidationError):
        GeometryResourceVersionRecord.model_validate({**payload, "origin_kind": "Agent Build"})


def test_content_digest_binds_kind_manifest_and_sorted_exact_asset_metadata() -> None:
    mapped = _bundle(GeometryResourceKind.MAPPED_MODEL)
    digest = geometry_resource_content_digest(
        resource_kind=mapped.resource.resource_kind,
        manifest_digest=mapped.version.manifest_digest,
        assets=tuple(reversed(mapped.assets)),
    )
    assert digest == mapped.version.content_digest

    changed_asset = mapped.assets[0].model_copy(update={"size_bytes": 11})
    changed_digest = geometry_resource_content_digest(
        resource_kind=mapped.resource.resource_kind,
        manifest_digest=mapped.version.manifest_digest,
        assets=(changed_asset, *mapped.assets[1:]),
    )
    assert changed_digest != digest

    with pytest.raises(ValidationError, match="content_digest"):
        GeometryResourceVersionBundle(
            resource=mapped.resource,
            version=mapped.version,
            assets=(changed_asset, *mapped.assets[1:]),
        )


def test_mapped_manifest_asset_must_match_canonical_version_manifest() -> None:
    mapped = _bundle(GeometryResourceKind.MAPPED_MODEL)
    changed_manifest_asset = next(
        asset for asset in mapped.assets if asset.asset_role is GeometryResourceAssetRole.MANIFEST
    ).model_copy(update={"sha256": SHA_B})
    assets = tuple(
        changed_manifest_asset if asset.asset_role is GeometryResourceAssetRole.MANIFEST else asset
        for asset in mapped.assets
    )
    version = mapped.version.model_copy(
        update={
            "content_digest": geometry_resource_content_digest(
                resource_kind=mapped.resource.resource_kind,
                manifest_digest=mapped.version.manifest_digest,
                assets=assets,
            )
        }
    )

    with pytest.raises(ValidationError, match="manifest asset"):
        GeometryResourceVersionBundle(
            resource=mapped.resource,
            version=version,
            assets=assets,
        )


@pytest.mark.parametrize(
    "resource_kind",
    (GeometryResourceKind.MAPPED_MODEL, GeometryResourceKind.MODELING_FUNCTION),
)
def test_exact_bundle_accepts_each_kind_required_asset_set(
    resource_kind: GeometryResourceKind,
) -> None:
    bundle = _bundle(resource_kind)
    assert bundle.resource.resource_kind is resource_kind
    assert bundle.version.version_no == 1


def test_exact_bundle_rejects_missing_duplicate_and_cross_kind_assets() -> None:
    mapped = _bundle(GeometryResourceKind.MAPPED_MODEL)

    with pytest.raises(ValidationError, match="missing required assets: manifest"):
        GeometryResourceVersionBundle(
            resource=mapped.resource,
            version=mapped.version,
            assets=tuple(
                asset
                for asset in mapped.assets
                if asset.asset_role is not GeometryResourceAssetRole.MANIFEST
            ),
        )
    with pytest.raises(ValidationError, match="asset roles must be unique"):
        GeometryResourceVersionBundle(
            resource=mapped.resource,
            version=mapped.version,
            assets=(*mapped.assets, mapped.assets[0]),
        )
    with pytest.raises(ValidationError, match="incompatible with resource kind"):
        GeometryResourceVersionBundle(
            resource=mapped.resource,
            version=mapped.version,
            assets=(*mapped.assets, _asset(GeometryResourceAssetRole.PARAMETER_SCHEMA)),
        )

    function = _bundle(GeometryResourceKind.MODELING_FUNCTION)
    with pytest.raises(ValidationError, match="incompatible with resource kind"):
        GeometryResourceVersionBundle(
            resource=function.resource,
            version=function.version,
            assets=(
                *function.assets,
                _asset(GeometryResourceAssetRole.CANONICAL_GEOMETRY),
            ),
        )


def test_exact_bundle_rejects_cross_root_and_cross_version_bindings() -> None:
    bundle = _bundle(GeometryResourceKind.MAPPED_MODEL)

    with pytest.raises(ValidationError, match="different resource root"):
        GeometryResourceVersionBundle(
            resource=bundle.resource,
            version=bundle.version.model_copy(update={"resource_id": "geometry-resource-other"}),
            assets=bundle.assets,
        )
    wrong_asset = _asset(GeometryResourceAssetRole.MANIFEST).model_copy(
        update={"resource_version_id": "geometry-version-other"}
    )
    with pytest.raises(ValidationError, match="different exact version"):
        GeometryResourceVersionBundle(
            resource=bundle.resource,
            version=bundle.version,
            assets=(bundle.assets[0], wrong_asset),
        )


def test_asset_rejects_paths_and_all_records_require_aware_timestamps() -> None:
    with pytest.raises(ValidationError, match="must not contain a path"):
        _asset(GeometryResourceAssetRole.MANIFEST).model_copy(
            update={"filename": "nested/manifest.json"}
        ).model_validate(
            {
                **_asset(GeometryResourceAssetRole.MANIFEST).model_dump(mode="json"),
                "filename": "nested/manifest.json",
            }
        )

    with pytest.raises(ValidationError, match="timezone-aware"):
        GeometryResourceRecord.model_validate(
            {**_resource().model_dump(), "created_at": NOW.replace(tzinfo=None)}
        )


def test_list_query_requires_a_complete_bounded_keyset_cursor() -> None:
    query = GeometryResourceListQuery(
        resource_kinds=(GeometryResourceKind.MAPPED_MODEL,),
        page_size=100,
    )
    assert query.page_size == 100

    with pytest.raises(ValidationError, match="requires timestamp and resource ID"):
        GeometryResourceListQuery(before_created_at=NOW)
    with pytest.raises(ValidationError):
        GeometryResourceListQuery(page_size=101)
    with pytest.raises(ValidationError, match="filters must be unique"):
        GeometryResourceListQuery(
            resource_kinds=(
                GeometryResourceKind.MAPPED_MODEL,
                GeometryResourceKind.MAPPED_MODEL,
            )
        )


def test_page_requires_descending_rows_and_exact_continuation() -> None:
    newer = _resource(resource_id="resource-z", created_at=NOW)
    older = _resource(
        resource_id="resource-a",
        created_at=NOW - timedelta(seconds=1),
    )
    page = GeometryResourcePage(
        resources=(newer, older),
        complete=False,
        next_before_created_at=older.created_at,
        next_before_resource_id=older.resource_id,
    )
    assert not page.complete

    with pytest.raises(ValidationError, match="strictly descending"):
        GeometryResourcePage(resources=(older, newer), complete=True)
    with pytest.raises(ValidationError, match="must match its last row"):
        GeometryResourcePage(
            resources=(newer, older),
            complete=False,
            next_before_created_at=newer.created_at,
            next_before_resource_id=newer.resource_id,
        )


def test_repository_port_exposes_one_atomic_publication_and_exact_read_contract() -> None:
    assert set(GeometryResourceRepository.__dict__) >= {
        "require_geometry_resource_publication_allowed",
        "publish_geometry_resource_version",
        "get_geometry_resource",
        "get_exact_geometry_resource_version",
        "list_executable_program_versions",
        "list_geometry_resources",
    }


@pytest.mark.asyncio
async def test_program_version_repository_filters_in_sql_before_stable_keyset_limit() -> None:
    class _EmptyResult:
        @staticmethod
        def all() -> tuple[()]:
            return ()

    class _CapturingSession:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def __aenter__(self) -> "_CapturingSession":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: object, *_args: object, **_kwargs: object) -> _EmptyResult:
            self.statements.append(statement)
            return _EmptyResult()

    session = _CapturingSession()
    repository = PostgresGeometryResourceRepositoryMixin(lambda: session)  # type: ignore[arg-type]
    context = TenantExecutionContext(
        tenant_id="tenant-1",
        principal_id="principal-1",
        task_id="task-1",
        actor_kind=TenantActorKind.USER,
        command_id="command-1",
    )
    boundary = NOW - timedelta(seconds=1)

    with bind_tenant_execution_context(context):
        result = await repository.list_executable_program_versions(
            context,
            before_created_at=boundary,
            before_resource_version_id="geometry-version-boundary",
            limit=3,
        )

    assert result == ()
    statement = session.statements[-1]
    sql = str(
        statement.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "JOIN geometry_resources" in sql
    assert "geometry_resources.resource_kind = 'modeling_function'" in sql
    assert "geometry_resource_versions.manifest_json ->> 'source_format'" in sql
    assert "= 'python_source_bundle_v1'" in sql
    assert "geometry_resource_version_assets.asset_role = 'source_bundle'" in sql
    assert "geometry_resource_version_assets.asset_format = 'source_bundle_v1'" in sql
    assert "geometry_resource_version_assets.content_type = 'application/zip'" in sql
    assert "geometry_resource_versions.created_at <" in sql
    assert "geometry_resource_versions.resource_version_id < 'geometry-version-boundary'" in sql
    assert sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT 3")
    assert (
        "ORDER BY geometry_resource_versions.created_at DESC, "
        "geometry_resource_versions.resource_version_id DESC" in sql
    )
