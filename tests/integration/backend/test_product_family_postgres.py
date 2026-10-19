from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.backend.product_family_fixtures import (
    current_family_contract,
    current_realization_source,
)
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from CAD2BIM_backend.application.product_family_asset_service import (
    ProductFamilyAssetService,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    geometry_resource_content_digest,
    GeometryResourceKind,
    GeometryResourceScope,
    GeometryResourceScopeKind,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometryFormat,
    GeometryBounds,
    GeometrySummary,
    GeometryTopologySummary,
)
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyGeometryBindingRecord,
    ProductFamilyGeometryPurpose,
    ProductFamilyListQuery,
    ProductFamilyRecord,
    ProductFamilyVersionAssetRecord,
    ProductFamilyVersionAssetRole,
    ProductFamilyVersionBundle,
    ProductFamilyVersionListQuery,
    ProductFamilyVersionRecord,
    product_family_content_digest,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductFamilyStandardModelManifest,
)
from CAD2BIM_backend.infrastructure.postgres.models import (
    ProductFamilyGeometryBindingModel,
    ProductFamilyModel,
    ProductFamilyVersionAssetModel,
    ProductFamilyVersionModel,
)
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
)
from CAD2BIM_backend.infrastructure.source_text.geometry_authoring import (
    SourceTextGeometryAuthoringSource,
)
from CAD2BIM_backend.infrastructure.source_text.source_bundle import (
    inspect_source_bundle,
    pack_source_bundle,
)
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from tests.integration.backend.test_geometry_resource_postgres import (
    _Runtime,
    _RuntimeLogin,
    _cleanup_geometry_rows,
    _create_runtime,
    _create_test_runtime_logins,
    _drop_test_runtime_logins,
    _insert_admin_bundle as _insert_admin_geometry_bundle,
    _mapped_bundle,
    _open_direct_context,
    _runtime_database_url,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
_PRODUCT_FAMILY_TABLES = (
    "product_families",
    "product_family_versions",
    "product_family_geometry_bindings",
    "product_family_version_assets",
)


_STANDARD_GEOMETRY = b"BREP:family-persistence"


def _family_source_bundle(family_version_id: str) -> bytes:
    return pack_source_bundle(
        {
            "main.py": (
                f"# exact source for {family_version_id}\ndef realize(ctx):\n    return None\n"
            ).encode("utf-8")
        }
    )


def _current_family_source_bundle(family_version_id: str) -> bytes:
    current_source = inspect_source_bundle(current_realization_source(binding_key="body"))
    return pack_source_bundle({
        "main.py": f"# exact source for {family_version_id}\n".encode()
        + current_source.read("main.py")
    })


def _standard_manifest_bytes(
    *,
    family_version_id: str,
    contract_digest: str,
    source_sha256: str,
    binding: ProductFamilyGeometryBindingRecord,
) -> bytes:
    manifest = ProductFamilyStandardModelManifest(
        family_version_id=family_version_id,
        contract_digest=contract_digest,
        source_bundle_sha256=source_sha256,
        binding_digest=sha256_digest(
            [{
                "binding_key": binding.binding_key,
                "resource_kind": binding.expected_resource_kind.value,
                "geometry_content_digest": binding.geometry_content_digest,
            }]
        ),

        geometry_sha256=hashlib.sha256(_STANDARD_GEOMETRY).hexdigest(),
        geometry_summary=GeometrySummary(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            bounds=GeometryBounds(minimum_m=(0.0, 0.0, 0.0), maximum_m=(1.0, 1.0, 1.0)),
            topology=GeometryTopologySummary(
                vertex_count=8,
                edge_count=12,
                wire_count=6,
                face_count=6,
                shell_count=1,
                solid_count=1,
                triangle_count=None,
                boundary_edge_count=0,
            ),
            surface_area_m2=6.0,
            volume_m3=1.0,
        ),
    )
    return canonical_json(manifest.model_dump(mode="json", by_alias=True)).encode()


async def _store_family_assets(
    storage: LocalStorage,
    bundle: ProductFamilyVersionBundle,
    *,
    content_by_role: dict[ProductFamilyVersionAssetRole, bytes] | None = None,
) -> None:
    if content_by_role is None:
        content_by_role = {
            ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE:
                _family_source_bundle(bundle.version.family_version_id),
            ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY: b"BREP:family-persistence",
            ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST: b'{"fixture":"v4"}',
        }
    for asset in bundle.assets:
        stored = await storage.write_bytes(
            ProductFamilyAssetService.storage_key(bundle, asset),
            content_by_role[asset.asset_role],
            filename=asset.filename,
            content_type=asset.content_type,
        )
        assert stored.size_bytes == asset.size_bytes
        assert stored.sha256 == asset.sha256


def _family_bundle(
    *,
    family: ProductFamilyRecord,
    family_version_id: str,
    version_no: int,
    geometry_resource_id: str,
    geometry_resource_version_id: str,
    geometry_content_digest: str,
    created_at: datetime,
    parent_family_version_id: str | None = None,
    formal: bool = False,
) -> ProductFamilyVersionBundle:
    family_contract = current_family_contract(binding_key="body", standard=formal)
    usage_instructions = (
        "Use the declared body geometry and interface datum." if formal else None
    )
    binding = ProductFamilyGeometryBindingRecord(
        family_version_id=family_version_id,
        binding_key="body",
        geometry_resource_id=geometry_resource_id,
        geometry_resource_version_id=geometry_resource_version_id,
        geometry_content_digest=geometry_content_digest,
        expected_resource_kind=GeometryResourceKind.MAPPED_MODEL,
        geometry_purpose=ProductFamilyGeometryPurpose.PRODUCT_COMPONENT,
    )
    source = _family_source_bundle(family_version_id)
    contract_digest = sha256_digest(family_contract)
    asset = ProductFamilyVersionAssetRecord(
        family_version_id=family_version_id,
        asset_role=ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE,
        asset_format="source_bundle_v1",
        filename="source.zip",
        content_type="application/zip",
        size_bytes=len(source),
        sha256=hashlib.sha256(source).hexdigest(),
        created_at=created_at,
    )
    assets = [asset]
    if formal:
        for role, content, asset_format, filename, content_type in (
            (ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY,
             b"BREP:family-persistence", "occt_brep_v1", "standard.brep",
             "application/vnd.opencascade.brep"),
            (ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST,
             b'{"fixture":"v4"}', "json", "standard.json", "application/json"),
        ):
            assets.append(ProductFamilyVersionAssetRecord(
                family_version_id=family_version_id,
                asset_role=role,
                asset_format=asset_format,
                filename=filename,
                content_type=content_type,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                created_at=created_at,
            ))
    version = ProductFamilyVersionRecord(
        family_version_id=family_version_id,
        family_id=family.family_id,
        version_no=version_no,
        parent_family_version_id=parent_family_version_id,
        display_name=f"Family {family.family_id} v{version_no}",
        purpose=f"PostgreSQL integration fixture version {version_no}",
        family_contract=family_contract,
        contract_digest=contract_digest,
        content_digest=product_family_content_digest(
            contract_digest=contract_digest,
            program_entry="main.py:realize",
            geometry_bindings=(binding,),
            assets=tuple(assets),
            usage_instructions=usage_instructions,
        ),
        usage_instructions=usage_instructions,
        program_entry="main.py:realize",
        created_at=created_at,
    )
    return ProductFamilyVersionBundle(
        family=family,
        version=version,
        geometry_bindings=(binding,),
        assets=tuple(assets),
    )


def _current_repository_asset_contents(
    bundle: ProductFamilyVersionBundle,
) -> dict[ProductFamilyVersionAssetRole, bytes]:
    source = _current_family_source_bundle(bundle.version.family_version_id)
    return {
        ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE: source,
        ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY: _STANDARD_GEOMETRY,
        ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST: _standard_manifest_bytes(
            family_version_id=bundle.version.family_version_id,
            contract_digest=bundle.version.contract_digest,
            source_sha256=hashlib.sha256(source).hexdigest(),
            binding=bundle.geometry_bindings[0],
        ),
    }


def _current_repository_family_bundle(
    bundle: ProductFamilyVersionBundle,
) -> ProductFamilyVersionBundle:
    content_by_role = _current_repository_asset_contents(bundle)
    assets = tuple(
        asset.model_copy(update={
            "size_bytes": len(content_by_role[asset.asset_role]),
            "sha256": hashlib.sha256(content_by_role[asset.asset_role]).hexdigest(),
        })
        for asset in bundle.assets
    )
    version = bundle.version.model_copy(update={
        "content_digest": product_family_content_digest(
            contract_digest=bundle.version.contract_digest,
            program_entry=bundle.version.program_entry,
            geometry_bindings=bundle.geometry_bindings,
            assets=assets,
            usage_instructions=bundle.version.usage_instructions,
        ),
    })
    return ProductFamilyVersionBundle(
        family=bundle.family,
        version=version,
        geometry_bindings=bundle.geometry_bindings,
        assets=assets,
    )


async def _insert_admin_family_bundle(
    admin_engine: AsyncEngine,
    bundle: ProductFamilyVersionBundle,
    *,
    insert_root: bool,
) -> None:
    scope = bundle.family.owner_scope
    async with admin_engine.begin() as connection:
        if insert_root:
            await connection.execute(
                insert(ProductFamilyModel).values(
                    family_id=bundle.family.family_id,
                    owner_scope=scope.scope_kind.name,
                    owner_tenant_id=scope.tenant_id,
                    owner_task_id=scope.task_id,
                    created_at=bundle.family.created_at,
                )
            )
        await connection.execute(
            insert(ProductFamilyVersionModel).values(
                family_version_id=bundle.version.family_version_id,
                family_id=bundle.version.family_id,
                version_no=bundle.version.version_no,
                parent_family_version_id=bundle.version.parent_family_version_id,
                display_name=bundle.version.display_name,
                purpose=bundle.version.purpose,
                usage_instructions=bundle.version.usage_instructions,
                family_contract_json=bundle.version.family_contract.model_dump(mode="json"),
                contract_digest=bundle.version.contract_digest,
                content_digest=bundle.version.content_digest,
                program_entry=bundle.version.program_entry,
                created_at=bundle.version.created_at,
            )
        )
        await connection.execute(
            insert(ProductFamilyGeometryBindingModel),
            [
                {
                    "family_version_id": binding.family_version_id,
                    "binding_key": binding.binding_key,
                    "geometry_resource_id": binding.geometry_resource_id,
                    "geometry_resource_version_id": binding.geometry_resource_version_id,
                    "geometry_content_digest": binding.geometry_content_digest,
                    "expected_resource_kind": binding.expected_resource_kind.value,
                    "geometry_purpose": binding.geometry_purpose.value,
                }
                for binding in bundle.geometry_bindings
            ],
        )
        await connection.execute(
            insert(ProductFamilyVersionAssetModel),
            [
                {
                    "family_version_id": asset.family_version_id,
                    "asset_role": asset.asset_role.value,
                    "asset_format": asset.asset_format,
                    "filename": asset.filename,
                    "content_type": asset.content_type,
                    "size_bytes": asset.size_bytes,
                    "sha256": asset.sha256,
                    "created_at": asset.created_at,
                }
                for asset in bundle.assets
            ],
        )


@pytest.mark.asyncio
async def test_current_v4_family_version_persists_and_reads_exact_options(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    suffix = uuid4().hex
    admin = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    storage = LocalStorage(tmp_path / "storage")
    curator = None
    logins: dict[str, _RuntimeLogin] = {}
    family_id = f"v4-family-{suffix}"
    geometry_id = f"v4-geometry-{suffix}"
    try:
        logins = _create_test_runtime_logins(TEST_DATABASE_URL)
        curator = _create_runtime(_runtime_database_url(TEST_DATABASE_URL, logins["curator"]))
        await curator.database.verify_ready()
        created_at = datetime.now(UTC)
        system_scope = GeometryResourceScope(scope_kind=GeometryResourceScopeKind.SYSTEM)
        geometry = await _mapped_bundle(
            storage=storage,
            resource_id=geometry_id,
            resource_version_id=f"{geometry_id}-v1",
            version_no=1,
            scope=system_scope,
            created_at=created_at,
            salt=suffix,
        )
        await _insert_admin_geometry_bundle(admin, geometry)
        family = ProductFamilyRecord(
            family_id=family_id, owner_scope=system_scope, created_at=created_at,
        )
        bundle = _family_bundle(
            family=family,
            family_version_id=f"{family_id}-v1",
            version_no=1,
            geometry_resource_id=geometry_id,
            geometry_resource_version_id=geometry.version.resource_version_id,
            geometry_content_digest=geometry.version.content_digest,
            created_at=created_at,
            formal=True,
        )
        await _store_family_assets(storage, bundle)
        await _insert_admin_family_bundle(admin, bundle, insert_root=True)

        context = TenantExecutionContext(
            tenant_id="system",
            principal_id="system-curator",
            task_id=None,
            actor_kind=TenantActorKind.SYSTEM_CURATOR,
            command_id=f"v4-family-read-{suffix}",
        )
        with bind_tenant_execution_context(context):
            loaded = await curator.repository.get_exact_product_family_version(
                context,
                family_id=family_id,
                family_version_id=bundle.version.family_version_id,
            )
        assert loaded == bundle
        assert loaded.version.family_contract.geometry_fragments[0].binding_options[0].binding_key == "body"
        incomplete_geometry = await _mapped_bundle(
            storage=storage, resource_id=f"{geometry_id}-incomplete",
            resource_version_id=f"{geometry_id}-incomplete-v1", version_no=1,
            scope=system_scope, created_at=created_at, salt=suffix,
        )
        geometry_assets = tuple(asset for asset in incomplete_geometry.assets
                                if asset.asset_role is not GeometryResourceAssetRole.MANIFEST)
        incomplete_geometry = incomplete_geometry.model_copy(update={
            "assets": geometry_assets,
            "version": incomplete_geometry.version.model_copy(update={
                "content_digest": geometry_resource_content_digest(
                    resource_kind=incomplete_geometry.resource.resource_kind,
                    manifest_digest=incomplete_geometry.version.manifest_digest,
                    assets=geometry_assets,
                ),
            }),
        })
        with pytest.raises(DBAPIError, match="Geometry formal assets are incomplete"):
            await _insert_admin_geometry_bundle(admin, incomplete_geometry)
        incomplete_family = _family_bundle(
            family=family.model_copy(update={"family_id": f"{family_id}-incomplete"}),
            family_version_id=f"{family_id}-incomplete-v1", version_no=1,
            geometry_resource_id=geometry_id,
            geometry_resource_version_id=geometry.version.resource_version_id,
            geometry_content_digest=geometry.version.content_digest,
            created_at=created_at, formal=True,
        )
        family_assets = tuple(asset for asset in incomplete_family.assets
                              if asset.asset_role is not ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY)
        incomplete_family = incomplete_family.model_copy(update={
            "assets": family_assets,
            "version": incomplete_family.version.model_copy(update={
                "content_digest": product_family_content_digest(
                    contract_digest=incomplete_family.version.contract_digest,
                    program_entry=incomplete_family.version.program_entry,
                    geometry_bindings=incomplete_family.geometry_bindings,
                    assets=family_assets,
                ),
            }),
        })
        with pytest.raises(DBAPIError, match="standard model assets are incomplete"):
            await _insert_admin_family_bundle(admin, incomplete_family, insert_root=True)
    finally:
        if curator is not None:
            await curator.close()
        await _cleanup_family_rows(admin, {family_id, f"{family_id}-incomplete"})
        await _cleanup_geometry_rows(admin, {geometry_id, f"{geometry_id}-incomplete"})
        _drop_test_runtime_logins(TEST_DATABASE_URL, logins)
        await admin.dispose()


async def _family_row_counts(
    admin_engine: AsyncEngine,
    family_ids: set[str],
) -> tuple[int, int, int, int]:
    async with admin_engine.connect() as connection:
        version_ids = select(ProductFamilyVersionModel.family_version_id).where(
            ProductFamilyVersionModel.family_id.in_(family_ids)
        )
        values = (
            await connection.scalar(
                select(func.count())
                .select_from(ProductFamilyModel)
                .where(ProductFamilyModel.family_id.in_(family_ids))
            ),
            await connection.scalar(
                select(func.count())
                .select_from(ProductFamilyVersionModel)
                .where(ProductFamilyVersionModel.family_id.in_(family_ids))
            ),
            await connection.scalar(
                select(func.count())
                .select_from(ProductFamilyGeometryBindingModel)
                .where(ProductFamilyGeometryBindingModel.family_version_id.in_(version_ids))
            ),
            await connection.scalar(
                select(func.count())
                .select_from(ProductFamilyVersionAssetModel)
                .where(ProductFamilyVersionAssetModel.family_version_id.in_(version_ids))
            ),
        )
    return tuple(int(value or 0) for value in values)  # type: ignore[return-value]


async def _listed_family_ids(
    runtime: _Runtime,
    context: TenantExecutionContext,
    controlled_ids: set[str],
) -> set[str]:
    found: set[str] = set()
    query = ProductFamilyListQuery(page_size=1)
    for _ in range(len(controlled_ids) + 3):
        with bind_tenant_execution_context(context):
            page = await runtime.repository.list_product_families(context, query=query)
        found.update(
            entry.family.family_id
            for entry in page.entries
            if entry.family.family_id in controlled_ids
        )
        if page.complete:
            break
        assert page.next_before_created_at is not None
        assert page.next_before_family_id is not None
        query = ProductFamilyListQuery(
            before_created_at=page.next_before_created_at,
            before_family_id=page.next_before_family_id,
            page_size=1,
        )
    return found


async def _cleanup_family_rows(admin_engine: AsyncEngine, family_ids: set[str]) -> None:
    if not family_ids:
        return
    async with admin_engine.begin() as connection:
        version_ids = tuple(
            (
                await connection.scalars(
                    select(ProductFamilyVersionModel.family_version_id).where(
                        ProductFamilyVersionModel.family_id.in_(family_ids)
                    )
                )
            ).all()
        )
        if version_ids:
            await connection.execute(
                delete(ProductFamilyGeometryBindingModel).where(
                    ProductFamilyGeometryBindingModel.family_version_id.in_(version_ids)
                )
            )
            await connection.execute(
                delete(ProductFamilyVersionAssetModel).where(
                    ProductFamilyVersionAssetModel.family_version_id.in_(version_ids)
                )
            )
            await connection.execute(
                delete(ProductFamilyVersionModel).where(
                    ProductFamilyVersionModel.family_version_id.in_(version_ids)
                )
            )
        await connection.execute(
            delete(ProductFamilyModel).where(ProductFamilyModel.family_id.in_(family_ids))
        )


@pytest.mark.asyncio
async def test_product_family_repository_is_ready_exact_restart_safe_and_rls_scoped(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    runtime_logins: dict[str, _RuntimeLogin] = {}
    runtimes: dict[str, _Runtime] = {}
    restarted: _Runtime | None = None
    admin_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    storage = LocalStorage(tmp_path / "storage")
    prefix = f"b305-{uuid4().hex}"
    family_ids = {
        f"{prefix}-task-one",
        f"{prefix}-task-two",
        f"{prefix}-tenant",
        f"{prefix}-system",
        f"{prefix}-other",
        f"{prefix}-incomplete",
    }
    geometry_ids = {f"geometry-{family_id}" for family_id in family_ids}
    try:
        runtime_logins = _create_test_runtime_logins(TEST_DATABASE_URL)
        runtimes = {
            kind: _create_runtime(_runtime_database_url(TEST_DATABASE_URL, role))
            for kind, role in runtime_logins.items()
        }
        await runtimes["mcp"].database.verify_ready()

        subject = f"{prefix}-subject"
        task_one = await _open_direct_context(
            runtimes["mcp"], subject=subject, session_name=f"{prefix} task one"
        )
        task_two = await _open_direct_context(
            runtimes["mcp"], subject=subject, session_name=f"{prefix} task two"
        )
        other = await _open_direct_context(
            runtimes["mcp"], subject=f"{prefix}-other-subject", session_name=f"{prefix} other"
        )
        assert task_one.tenant_id == task_two.tenant_id
        assert task_one.task_id != task_two.task_id
        assert task_one.tenant_id != other.tenant_id

        scopes = {
            "task-one": GeometryResourceScope(
                scope_kind=GeometryResourceScopeKind.TASK,
                tenant_id=task_one.tenant_id,
                task_id=task_one.task_id,
            ),
            "task-two": GeometryResourceScope(
                scope_kind=GeometryResourceScopeKind.TASK,
                tenant_id=task_two.tenant_id,
                task_id=task_two.task_id,
            ),
            "tenant": GeometryResourceScope(
                scope_kind=GeometryResourceScopeKind.TENANT,
                tenant_id=task_one.tenant_id,
            ),
            "system": GeometryResourceScope(scope_kind=GeometryResourceScopeKind.SYSTEM),
            "other": GeometryResourceScope(
                scope_kind=GeometryResourceScopeKind.TASK,
                tenant_id=other.tenant_id,
                task_id=other.task_id,
            ),
        }
        base_time = datetime.now(UTC) + timedelta(hours=1)
        bundles: dict[str, ProductFamilyVersionBundle] = {}
        for offset, (name, scope) in enumerate(scopes.items()):
            family_id = f"{prefix}-{name}"
            geometry_id = f"geometry-{family_id}"
            geometry = await _mapped_bundle(
                storage=storage,
                resource_id=geometry_id,
                resource_version_id=f"{geometry_id}-v1",
                version_no=1,
                scope=scope,
                created_at=base_time + timedelta(seconds=offset),
                salt=name,
            )
            await _insert_admin_geometry_bundle(admin_engine, geometry)
            family = ProductFamilyRecord(
                family_id=family_id,
                owner_scope=scope,
                created_at=base_time + timedelta(seconds=offset),
            )
            bundle = _current_repository_family_bundle(
                _family_bundle(
                    family=family,
                    family_version_id=f"{family_id}-v1",
                    version_no=1,
                    geometry_resource_id=geometry.resource.resource_id,
                    geometry_resource_version_id=geometry.version.resource_version_id,
                    geometry_content_digest=geometry.version.content_digest,
                    created_at=base_time + timedelta(seconds=offset),
                    formal=True,
                )
            )
            bundles[name] = bundle
            await _store_family_assets(
                storage, bundle, content_by_role=_current_repository_asset_contents(bundle)
            )
            await _insert_admin_family_bundle(admin_engine, bundle, insert_root=True)

        task_one_v1 = bundles["task-one"]
        task_one_v2 = _current_repository_family_bundle(
            _family_bundle(
                family=task_one_v1.family,
                family_version_id=f"{task_one_v1.family.family_id}-v2",
                version_no=2,
                parent_family_version_id=task_one_v1.version.family_version_id,
                geometry_resource_id=task_one_v1.geometry_bindings[0].geometry_resource_id,
                geometry_resource_version_id=(
                    task_one_v1.geometry_bindings[0].geometry_resource_version_id
                ),
                geometry_content_digest=task_one_v1.geometry_bindings[0].geometry_content_digest,
                created_at=task_one_v1.version.created_at + timedelta(microseconds=1),
                formal=True,
            )
        )
        await _store_family_assets(
            storage, task_one_v2, content_by_role=_current_repository_asset_contents(task_one_v2)
        )
        await _insert_admin_family_bundle(admin_engine, task_one_v2, insert_root=False)

        incomplete_id = f"{prefix}-incomplete"
        async with admin_engine.begin() as connection:
            await connection.execute(
                insert(ProductFamilyModel).values(
                    family_id=incomplete_id,
                    owner_scope=GeometryResourceScopeKind.TASK.name,
                    owner_tenant_id=task_one.tenant_id,
                    owner_task_id=task_one.task_id,
                    created_at=base_time + timedelta(seconds=10),
                )
            )

        controlled = set(family_ids)
        assert await _listed_family_ids(runtimes["mcp"], task_one, controlled) == {
            bundles["task-one"].family.family_id,
            bundles["tenant"].family.family_id,
            bundles["system"].family.family_id,
        }
        assert await _listed_family_ids(runtimes["mcp"], task_two, controlled) == {
            bundles["task-two"].family.family_id,
            bundles["tenant"].family.family_id,
            bundles["system"].family.family_id,
        }
        assert await _listed_family_ids(runtimes["mcp"], other, controlled) == {
            bundles["other"].family.family_id,
            bundles["system"].family.family_id,
        }
        tenant_context = task_one.model_copy(
            update={"task_id": None, "command_id": f"{prefix}-tenant-view"}
        )
        assert await _listed_family_ids(runtimes["mcp"], tenant_context, controlled) == {
            bundles["tenant"].family.family_id,
            bundles["system"].family.family_id,
        }
        curator_context = TenantExecutionContext(
            tenant_id="system",
            principal_id="system-curator",
            task_id=None,
            actor_kind=TenantActorKind.SYSTEM_CURATOR,
            command_id=f"{prefix}-curator-view",
        )
        assert await _listed_family_ids(runtimes["curator"], curator_context, controlled) == {
            bundles["system"].family.family_id
        }

        with bind_tenant_execution_context(task_one):
            assert (
                await runtimes["mcp"].repository.get_product_family(
                    task_one, family_id=incomplete_id
                )
                is None
            )
            assert (
                await runtimes["mcp"].repository.get_product_family(
                    task_one, family_id=bundles["task-two"].family.family_id
                )
                is None
            )
            assert (
                await runtimes["mcp"].repository.get_product_family_version_by_id(
                    task_one,
                    family_version_id=bundles["other"].version.family_version_id,
                )
                is None
            )
            exact_v1 = await runtimes["mcp"].repository.get_exact_product_family_version(
                task_one,
                family_id=task_one_v1.family.family_id,
                family_version_id=task_one_v1.version.family_version_id,
            )
            visible_identity = await runtimes["mcp"].repository.get_product_family_version_identity(
                task_one, family_id=task_one_v1.family.family_id,
                family_version_id=task_one_v1.version.family_version_id,
            )
            assert await runtimes["mcp"].repository.get_product_family_version_identity(
                task_one, family_id=bundles["task-two"].family.family_id,
                family_version_id=bundles["task-two"].version.family_version_id,
            ) is None
            assert await runtimes["mcp"].repository.get_product_family_version_identity(
                task_one, family_id=task_one_v1.family.family_id,
                family_version_id=bundles["task-two"].version.family_version_id,
            ) is None
            exact_v2 = await runtimes["mcp"].repository.get_exact_product_family_version(
                task_one,
                family_id=task_one_v1.family.family_id,
                family_version_id=task_one_v2.version.family_version_id,
            )
            latest = await runtimes["mcp"].repository.get_latest_product_family_version(
                task_one, family_id=task_one_v1.family.family_id
            )
            first_page = await runtimes["mcp"].repository.list_product_family_versions(
                task_one,
                query=ProductFamilyVersionListQuery(
                    family_id=task_one_v1.family.family_id, page_size=1
                ),
            )
            second_page = await runtimes["mcp"].repository.list_product_family_versions(
                task_one,
                query=ProductFamilyVersionListQuery(
                    family_id=task_one_v1.family.family_id,
                    before_version_no=first_page.next_before_version_no,
                    page_size=1,
                ),
            )
        assert exact_v1 == task_one_v1
        assert visible_identity == task_one_v1.version.family_version_id
        assert exact_v2 == task_one_v2
        assert latest == task_one_v2
        assert tuple(item.family_version_id for item in first_page.versions) == (task_one_v2.version.family_version_id,)
        assert tuple(item.family_version_id for item in second_page.versions) == (task_one_v1.version.family_version_id,)
        assert first_page.versions[0].family == task_one_v2.family
        assert first_page.next_before_version_no == 2
        assert second_page.complete is True
        assert set(task_one_v1.family.model_dump()) == {"family_id", "owner_scope", "created_at"}
        assert exact_v1 is not None
        assert exact_v1.assets[0].sha256 != task_one_v2.assets[0].sha256
        source_bundles = SourceTextGeometryAuthoringSource()
        await ProductFamilyAssetService(storage, source_bundles).verify_bundle(exact_v1)
        reopened_storage = LocalStorage(tmp_path / "storage")
        exact_source = await ProductFamilyAssetService(
            reopened_storage,
            source_bundles,
        ).read_exact_asset(
            exact_v1,
            exact_v1.assets[0],
        )
        assert exact_source == _current_family_source_bundle(exact_v1.version.family_version_id)

        for runtime_kind, runtime in runtimes.items():
            async with runtime.engine.connect() as connection:
                for table in _PRODUCT_FAMILY_TABLES:
                    privileges = (
                        await connection.execute(
                            text(
                                "SELECT "
                                "has_table_privilege(current_user, :table, 'INSERT'), "
                                "has_table_privilege(current_user, :table, 'UPDATE'), "
                                "has_table_privilege(current_user, :table, 'DELETE')"
                            ),
                            {"table": table},
                        )
                    ).one()
                    assert privileges == (
                        runtime_kind in {"mcp", "runner", "curator"},
                        False,
                        False,
                    )

        async with admin_engine.connect() as connection:
            insert_policy_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE schemaname = current_schema() "
                    "AND tablename IN ("
                    "'product_families', 'product_family_versions', "
                    "'product_family_geometry_bindings', "
                    "'product_family_version_assets'"
                    ") AND cmd = 'INSERT'"
                )
            )
        # Runtime and SYSTEM-curator INSERT policies each cover all four tables.
        assert int(insert_policy_count or 0) == 2 * len(_PRODUCT_FAMILY_TABLES)

        await runtimes["mcp"].close()
        del runtimes["mcp"]
        restarted = _create_runtime(_runtime_database_url(TEST_DATABASE_URL, runtime_logins["mcp"]))
        await restarted.database.verify_ready()
        with bind_tenant_execution_context(task_one):
            assert (
                await restarted.repository.get_exact_product_family_version(
                    task_one,
                    family_id=task_one_v1.family.family_id,
                    family_version_id=task_one_v1.version.family_version_id,
                )
                == task_one_v1
            )
            assert (
                await restarted.repository.get_latest_product_family_version(
                    task_one, family_id=task_one_v1.family.family_id
                )
                == task_one_v2
            )
    finally:
        for runtime in runtimes.values():
            await runtime.close()
        if restarted is not None:
            await restarted.close()
        try:
            await _cleanup_family_rows(admin_engine, family_ids)
            await _cleanup_geometry_rows(admin_engine, geometry_ids)
        finally:
            await admin_engine.dispose()
            if runtime_logins:
                _drop_test_runtime_logins(TEST_DATABASE_URL, runtime_logins)


@pytest.mark.asyncio
async def test_product_family_database_guards_reject_incomplete_and_invalid_dependencies(
    tmp_path: Path,
) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")

    admin_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    storage = LocalStorage(tmp_path / "storage")
    prefix = f"b305-guard-{uuid4().hex}"
    family_ids = {
        f"{prefix}-incomplete",
        f"{prefix}-system-tenant",
        f"{prefix}-tenant-task",
        f"{prefix}-kind",
    }
    geometry_ids = {
        f"{prefix}-tenant-geometry",
        f"{prefix}-task-geometry",
    }
    try:
        tenant_id = f"{prefix}-tenant"
        task_id = f"{prefix}-task"
        # Reuse real control-plane identity rows so Geometry and ProductFamily scope FKs apply.
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO principals (principal_id, issuer, subject, status) "
                    "VALUES (:principal, 'test', :principal, 'active')"
                ),
                {"principal": f"{prefix}-principal"},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenants (tenant_id, kind, status) "
                    "VALUES (:tenant, 'personal', 'active')"
                ),
                {"tenant": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_principals (tenant_id, principal_id, access) "
                    "VALUES (:tenant, :principal, 'owner')"
                ),
                {"principal": f"{prefix}-principal", "tenant": tenant_id},
            )
            await connection.execute(
                text("INSERT INTO users (owner_id, tenant_id) VALUES (:principal, :tenant)"),
                {"principal": f"{prefix}-principal", "tenant": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO tasks ("
                    "task_id, owner_id, tenant_id, title, metadata_json, status, active_module"
                    ") VALUES ("
                    ":task, :principal, :tenant, 'B3.05 guard fixture', '{}'::jsonb, "
                    "'running', 'workflow'"
                    ")"
                ),
                {"principal": f"{prefix}-principal", "tenant": tenant_id, "task": task_id},
            )

        tenant_scope = GeometryResourceScope(
            scope_kind=GeometryResourceScopeKind.TENANT, tenant_id=tenant_id
        )
        task_scope = GeometryResourceScope(
            scope_kind=GeometryResourceScopeKind.TASK,
            tenant_id=tenant_id,
            task_id=task_id,
        )
        created_at = datetime.now(UTC)
        tenant_geometry = await _mapped_bundle(
            storage=storage,
            resource_id=f"{prefix}-tenant-geometry",
            resource_version_id=f"{prefix}-tenant-geometry-v1",
            version_no=1,
            scope=tenant_scope,
            created_at=created_at,
            salt="tenant",
        )
        task_geometry = await _mapped_bundle(
            storage=storage,
            resource_id=f"{prefix}-task-geometry",
            resource_version_id=f"{prefix}-task-geometry-v1",
            version_no=1,
            scope=task_scope,
            created_at=created_at,
            salt="task",
        )
        await _insert_admin_geometry_bundle(admin_engine, tenant_geometry)
        await _insert_admin_geometry_bundle(admin_engine, task_geometry)

        incomplete_family = ProductFamilyRecord(
            family_id=f"{prefix}-incomplete",
            owner_scope=task_scope,
            created_at=created_at,
        )
        incomplete = _family_bundle(
            family=incomplete_family,
            family_version_id=f"{prefix}-incomplete-v1",
            version_no=1,
            geometry_resource_id=task_geometry.resource.resource_id,
            geometry_resource_version_id=task_geometry.version.resource_version_id,
            geometry_content_digest=task_geometry.version.content_digest,
            created_at=created_at,
        )
        with pytest.raises(DBAPIError) as incomplete_error:
            async with admin_engine.begin() as connection:
                await connection.execute(
                    insert(ProductFamilyModel).values(
                        family_id=incomplete.family.family_id,
                        owner_scope=incomplete.family.owner_scope.scope_kind.name,
                        owner_tenant_id=incomplete.family.owner_scope.tenant_id,
                        owner_task_id=incomplete.family.owner_scope.task_id,
                    )
                )
                await connection.execute(
                    insert(ProductFamilyVersionModel).values(
                        family_version_id=incomplete.version.family_version_id,
                        family_id=incomplete.version.family_id,
                        version_no=1,
                        display_name=incomplete.version.display_name,
                        purpose=incomplete.version.purpose,
                        family_contract_json=incomplete.version.family_contract.model_dump(
                            mode="json"
                        ),
                            contract_digest=incomplete.version.contract_digest,
                            content_digest=incomplete.version.content_digest,
                            program_entry=incomplete.version.program_entry,
                    )
                )
        assert getattr(incomplete_error.value.orig, "sqlstate", None) == "23514"

        invalid_specs = (
            (
                "system-tenant",
                GeometryResourceScope(scope_kind=GeometryResourceScopeKind.SYSTEM),
                tenant_geometry,
            ),
            ("tenant-task", tenant_scope, task_geometry),
            ("kind", task_scope, task_geometry),
        )
        for name, family_scope, geometry in invalid_specs:
            family = ProductFamilyRecord(
                family_id=f"{prefix}-{name}", owner_scope=family_scope, created_at=created_at
            )
            bundle = _family_bundle(
                family=family,
                family_version_id=f"{prefix}-{name}-v1",
                version_no=1,
                geometry_resource_id=geometry.resource.resource_id,
                geometry_resource_version_id=geometry.version.resource_version_id,
                geometry_content_digest=geometry.version.content_digest,
                created_at=created_at,
            )
            if name == "kind":
                wrong = bundle.geometry_bindings[0].model_copy(
                    update={"expected_resource_kind": GeometryResourceKind.MODELING_FUNCTION}
                )
                version = bundle.version.model_copy(
                    update={
                        "content_digest": product_family_content_digest(
                            contract_digest=bundle.version.contract_digest,
                            program_entry=bundle.version.program_entry,
                            geometry_bindings=(wrong,),
                            assets=bundle.assets,
                        )
                    }
                )
                bundle = ProductFamilyVersionBundle(
                    family=family,
                    version=version,
                    geometry_bindings=(wrong,),
                    assets=bundle.assets,
                )
            with pytest.raises(DBAPIError) as dependency_error:
                await _insert_admin_family_bundle(admin_engine, bundle, insert_root=True)
            assert getattr(dependency_error.value.orig, "sqlstate", None) == "23514"

        assert await _family_row_counts(admin_engine, family_ids) == (0, 0, 0, 0)
    finally:
        try:
            await _cleanup_family_rows(admin_engine, family_ids)
            await _cleanup_geometry_rows(admin_engine, geometry_ids)
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM tasks WHERE task_id = :task"),
                    {"task": f"{prefix}-task"},
                )
                await connection.execute(
                    text("DELETE FROM users WHERE owner_id = :principal"),
                    {"principal": f"{prefix}-principal"},
                )
                await connection.execute(
                    text(
                        "DELETE FROM tenant_principals "
                        "WHERE tenant_id = :tenant AND principal_id = :principal"
                    ),
                    {
                        "principal": f"{prefix}-principal",
                        "tenant": f"{prefix}-tenant",
                    },
                )
                await connection.execute(
                    text("DELETE FROM tenants WHERE tenant_id = :tenant"),
                    {"tenant": f"{prefix}-tenant"},
                )
                await connection.execute(
                    text("DELETE FROM principals WHERE principal_id = :principal"),
                    {
                        "principal": f"{prefix}-principal",
                    },
                )
        finally:
            await admin_engine.dispose()
