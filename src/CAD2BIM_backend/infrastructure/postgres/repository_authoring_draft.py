"""Shared root-CAS and terminal Operation transaction for editable drafts.

Called by the existing Geometry and Family repository mixins. Snapshot content
is deliberately opaque here and remains owned by the domain Applications.
"""

import logging

from sqlalchemy import and_, bindparam, cast, func, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from CAD2BIM_backend.domain.authoring_draft import (
    AuthoringDraftCommitResult, AuthoringDraftRecord, AuthoringPublicationCommitRejected,
    AuthoringPublicationPending, AuthoringPublicationResources,
    AuthoringDraftChanged,
)
from CAD2BIM_backend.domain.control_plane import ConflictError, IdempotencyConflictError
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef, CatalogResourceKind, CriticalOperationKind, CriticalOperationRecord,
    CriticalOperationStatus,
)
from CAD2BIM_backend.domain.identity import TenantActorKind
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind
from CAD2BIM_backend.ports.identity import IdentityAccessDenied
from CAD2BIM_backend.infrastructure.postgres.models import (
    ArtifactModel, ArtifactVersionModel, CriticalOperationModel,
    GeometryResourceModel, ProductFamilyModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import (
    PostgresDataRuntimeRepositoryMixin as Operations, append_catalog_entry, next_catalog_revision,
    _current_operation_contract_predicate,
)
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import require_system_curator
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    current_tenant_execution_context, set_tenant_execution_context,
)

logger = logging.getLogger(__name__)


def root_model(root_kind):
    if root_kind == "geometry":
        return GeometryResourceModel, GeometryResourceModel.resource_id
    if root_kind == "family":
        return ProductFamilyModel, ProductFamilyModel.family_id
    raise ValueError("unsupported authoring root kind")


def require_context(scope):
    if current_tenant_execution_context() != scope.identity or scope.identity.task_id is None:
        raise IdentityAccessDenied("draft access requires the active Task identity")


def private_root_predicate(model, scope):
    task = and_(model.owner_scope == "TASK", model.owner_tenant_id == scope.identity.tenant_id,
                model.owner_task_id == scope.identity.task_id)
    tenant = and_(model.owner_scope == "TENANT", model.owner_tenant_id == scope.identity.tenant_id)
    if scope.identity.actor_kind is TenantActorKind.SYSTEM_CURATOR:
        return or_(task, tenant, model.owner_scope == "SYSTEM")
    return or_(task, tenant)


async def require_root_owner(session, scope, owner):
    if owner.scope_kind is ResourceOwnerScopeKind.TASK:
        if owner.tenant_id == scope.identity.tenant_id and owner.task_id == scope.identity.task_id:
            return
    elif owner.scope_kind is ResourceOwnerScopeKind.TENANT:
        if owner.tenant_id == scope.identity.tenant_id:
            return
    elif owner.scope_kind is ResourceOwnerScopeKind.SYSTEM:
        if scope.identity.actor_kind is TenantActorKind.SYSTEM_CURATOR:
            await require_system_curator(session)
            return
    raise IdentityAccessDenied("only the resource owner can edit or publish its draft")


def owner_scope(model):
    return ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind[model.owner_scope],
                              tenant_id=model.owner_tenant_id, task_id=model.owner_task_id)


async def snapshot_models(session, scope, version_id, *, root=None):
    statement = select(ArtifactModel, ArtifactVersionModel).join(ArtifactVersionModel,
        ArtifactModel.artifact_id == ArtifactVersionModel.artifact_id).where(
            ArtifactVersionModel.version_id == version_id,
            ArtifactVersionModel.tenant_id == scope.identity.tenant_id,
            ArtifactVersionModel.content_type == "application/vnd.cad2bim.authoring-draft+json",
            ArtifactModel.kind == "authoring_draft",
            ArtifactModel.status.in_(("ready", "available")),
    )
    if root is not None and root.owner_scope == "TENANT":
        if root.owner_tenant_id != scope.identity.tenant_id or root.draft_artifact_version_id != version_id:
            raise IdentityAccessDenied("the exact snapshot is outside the authorized TENANT root")
    else:
        statement = statement.where(ArtifactVersionModel.owner_id == scope.owner_id,
            ArtifactVersionModel.task_id == scope.identity.task_id)
    pair = (await session.execute(statement)).one_or_none()
    if pair is None:
        raise IdentityAccessDenied("the exact draft snapshot is not available in this scope")
    return pair


async def read_snapshot(session, scope, version_id, *, root=None):
    _, artifact = await snapshot_models(session, scope, version_id, root=root)
    return ArtifactVersionRef(artifact_id=artifact.artifact_id, version_id=artifact.version_id, sha256=artifact.sha256)


async def draft_record(session, scope, root_kind, root):
    model, key = root_model(root_kind)
    return AuthoringDraftRecord(root_kind=root_kind, root_id=getattr(root, key.key),
        owner_scope=owner_scope(root), generation=root.draft_generation,
        snapshot=await read_snapshot(session, scope, root.draft_artifact_version_id, root=root))


async def get_draft_snapshot(repository, scope, *, draft):
    """Authorize one current root snapshot before revealing its storage origin."""
    from .repository_artifact import PostgresArtifactRepositoryMixin as Artifacts
    require_context(scope)
    model, key = root_model(draft.root_kind)
    async with repository._sessions() as session:
        await set_tenant_execution_context(session, scope.identity)
        root = await session.scalar(select(model).where(key == draft.root_id, private_root_predicate(model, scope)))
        if (root is None or owner_scope(root) != draft.owner_scope or root.draft_generation != draft.generation
            or root.draft_artifact_version_id != draft.snapshot.version_id):
            raise ConflictError("the authorized draft snapshot changed")
        artifact, version = await snapshot_models(session, scope, draft.snapshot.version_id, root=root)
        if (version.artifact_id != draft.snapshot.artifact_id or version.sha256 != draft.snapshot.sha256):
            raise ConflictError("the authorized draft differs from its exact Artifact")
        return Artifacts._artifact_record_from_models(artifact, version)


async def get_draft(repository, scope, *, root_kind, root_id):
    require_context(scope)
    model, key = root_model(root_kind)
    async with repository._sessions() as session:
        await set_tenant_execution_context(session, scope.identity)
        if scope.identity.actor_kind is TenantActorKind.SYSTEM_CURATOR:
            await require_system_curator(session)
        root = await session.scalar(select(model).where(key == root_id,
            private_root_predicate(model, scope), model.draft_artifact_version_id.is_not(None)))
        return None if root is None else await draft_record(session, scope, root_kind, root)


async def list_drafts(repository, scope, *, root_kind, after_root_id=None, limit=50):
    require_context(scope)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("draft page size must be between 1 and 100")
    model, key = root_model(root_kind)
    async with repository._sessions() as session:
        await set_tenant_execution_context(session, scope.identity)
        if scope.identity.actor_kind is TenantActorKind.SYSTEM_CURATOR:
            await require_system_curator(session)
        statement = select(model).where(private_root_predicate(model, scope),
                                         model.draft_artifact_version_id.is_not(None))
        if after_root_id is not None:
            statement = statement.where(key > after_root_id)
        roots = (await session.scalars(statement.order_by(key).limit(limit))).all()
        return tuple([await draft_record(session, scope, root_kind, root) for root in roots])


async def find_operation(session, scope, operation):
    identities = [CriticalOperationModel.operation_id == operation.operation_id,
                  CriticalOperationModel.idempotency_key == operation.idempotency_key]
    if operation.operation_kind is not CriticalOperationKind.AUTHORING_PUBLISH:
        identities.append(and_(CriticalOperationModel.tool_name == operation.tool_name,
                               CriticalOperationModel.request_fingerprint == operation.request_fingerprint))
    model = await session.scalar(select(CriticalOperationModel).where(
        CriticalOperationModel.owner_id == scope.owner_id,
        CriticalOperationModel.tenant_id == scope.identity.tenant_id,
        CriticalOperationModel.task_id == scope.identity.task_id,
        CriticalOperationModel.run_id == scope.run_id,
        or_(*identities),
    ))
    if model is not None and (
        model.operation_kind != operation.operation_kind.value
        or model.tool_name != operation.tool_name
        or model.request_fingerprint != operation.request_fingerprint
        or model.code_artifact_version_id != (operation.code_artifact.version_id if operation.code_artifact else None)
        or operation_request_detail(model.operation_detail_json) != operation_request_detail(operation.detail.model_dump(mode="json"))
    ):
        raise IdempotencyConflictError("authoring request identity was reused with different exact inputs")
    return model


def operation_request_detail(detail):
    # Publication output identities may reuse an earlier Run's exact versions.
    # Request fingerprint and selected snapshots retain admission identity.
    if detail.get("detail_kind") == "authoring_publish":
        return {key: value for key, value in detail.items() if key not in {"geometry_versions", "family_version"}}
    return detail


async def publish_operation(repository, session, operation):
    session.add(Operations._operation_model(operation))
    await session.flush()
    entry = await append_catalog_entry(session, owner_id=operation.owner_id,
        tenant_id=operation.tenant_id, task_id=operation.task_id, run_id=operation.run_id,
        kind=CatalogResourceKind.OPERATION,
        catalog_revision=await next_catalog_revision(session, operation.run_id),
        operation_id=operation.operation_id)
    await session.flush()
    from CAD2BIM_backend.domain.data_runtime import PublishedCriticalOperation
    return PublishedCriticalOperation(operation=operation, catalog_entry=Operations._catalog_record(entry))


async def begin_publication(repository, scope, *, operation, expected_drafts):
    operation = CriticalOperationRecord.model_validate(operation.model_dump(mode="python"))
    if operation.operation_kind is not CriticalOperationKind.AUTHORING_PUBLISH or operation.status is not CriticalOperationStatus.PREPARED:
        raise ValueError("publication admission requires its PREPARED Operation")
    require_context(scope)
    Operations._require_record_scope(scope, operation)
    selected = {(d.root_kind, d.root_id): (d.generation, d.snapshot) for d in operation.detail.expected_drafts}
    if selected != {(d.root_kind, d.root_id): (d.generation, d.snapshot) for d in expected_drafts}:
        raise ValueError("publication admission differs from its exact draft closure")
    try:
        async with repository._sessions() as session, session.begin():
            await set_tenant_execution_context(session, scope.identity)
            await Operations._require_data_run(session, scope)
            # Admission shares the exact root lock order used by publication and
            # draft writes. A second Run must see the first PREPARED publication
            # before it can admit another request for the same target root.
            for item in sorted(expected_drafts, key=lambda value: (value.root_id, value.root_kind)):
                await require_root_owner(session, scope, item.owner_scope)
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:root_id, 0))"),
                                      {"root_id": item.root_id})
            existing = await find_operation(session, scope, operation)
            if existing is not None:
                return await Operations._operation_record(session, existing)
            for item in expected_drafts:
                if item.owner_scope.scope_kind is ResourceOwnerScopeKind.TENANT and await session.scalar(
                    select(func.cad2bim_authoring_publication_fence(item.root_kind, item.root_id))
                ):
                    raise AuthoringPublicationPending(
                        "Another publication has frozen this resource; reconcile its original action before publishing again")
            target = (CriticalOperationModel.target_geometry_resource_id if operation.detail.root_kind == "geometry"
                else CriticalOperationModel.target_product_family_id)
            active = await session.scalar(select(CriticalOperationModel).where(
                CriticalOperationModel.owner_id == scope.owner_id,
                CriticalOperationModel.tenant_id == scope.identity.tenant_id,
                CriticalOperationModel.task_id == scope.identity.task_id,
                CriticalOperationModel.operation_kind == CriticalOperationKind.AUTHORING_PUBLISH.value,
                CriticalOperationModel.tool_name == "ifc_authoring_publish",
                CriticalOperationModel.status == CriticalOperationStatus.PREPARED.value,
                target == operation.detail.root_id,
                _current_operation_contract_predicate(),
            ).order_by(CriticalOperationModel.started_at.desc()).limit(1))
            if active is not None:
                return await Operations._operation_record(session, active)
            await load_current_draft_roots(session, scope, expected_drafts)
            session.add(Operations._operation_model(operation))
            await session.flush()
            return operation
    except IntegrityError:
        async with repository._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            existing = await find_operation(session, scope, operation)
            if existing is not None:
                return await Operations._operation_record(session, existing)
        raise


async def finish_publication(repository, session, scope, operation, execution_claim):
    model = await Operations._authoring_model(session, scope, operation.operation_id,
        operation_kind=CriticalOperationKind.AUTHORING_PUBLISH, lock=True)
    if model is None:
        raise ConflictError("original publication admission is unavailable")
    await find_operation(session, scope, operation)
    if model.status in {"succeeded", "failed"}:
        stored = await Operations._operation_record(session, model)
        if stored != operation:
            raise IdempotencyConflictError("publication terminal replay changed")
        return await Operations._require_published_operation(repository, session, model)
    Operations.require_authoring_claim(model, execution_claim)
    model.status = operation.status.value
    model.operation_detail_json = operation.detail.model_dump(mode="json")
    model.result_geometry_resource_version_id = operation.result_geometry_resource_version_id
    model.result_product_family_version_id = operation.result_product_family_version_id
    model.base_product_family_version_id = operation.base_product_family_version_id
    model.error_code, model.error_message = operation.error_code, operation.error_message
    model.error_phase = None if operation.error_phase is None else operation.error_phase.value
    model.finished_at = operation.finished_at
    model.authoring_execution_owner = None
    model.authoring_execution_token_sha256 = None
    entry = await append_catalog_entry(session, owner_id=operation.owner_id,
        tenant_id=operation.tenant_id, task_id=operation.task_id, run_id=operation.run_id,
        kind=CatalogResourceKind.OPERATION,
        catalog_revision=await next_catalog_revision(session, operation.run_id),
        operation_id=operation.operation_id)
    await session.flush()
    from CAD2BIM_backend.domain.data_runtime import PublishedCriticalOperation
    return PublishedCriticalOperation(operation=operation, catalog_entry=Operations._catalog_record(entry))


async def finalize_publication_failure(repository, scope, *, operation, execution_claim):
    operation = CriticalOperationRecord.model_validate(operation.model_dump(mode="python"))
    if operation.operation_kind is not CriticalOperationKind.AUTHORING_PUBLISH or operation.status is not CriticalOperationStatus.FAILED:
        raise ValueError("publication failure completion requires its FAILED Operation")
    require_context(scope)
    Operations._require_record_scope(scope, operation)
    async with repository._sessions() as session, session.begin():
        await set_tenant_execution_context(session, scope.identity)
        await Operations._require_data_run(session, scope)
        return await finish_publication(repository, session, scope, operation, execution_claim)


async def draft_replay(repository, session, scope, operation, owner):
    model = await find_operation(session, scope, operation)
    if model is None:
        return None
    stored = await Operations._operation_record(session, model)
    published = await Operations._published_operation(repository, session, model, run_id=scope.run_id)
    if stored.status is not CriticalOperationStatus.SUCCEEDED or published is None:
        raise ConflictError("the original draft write has no committed terminal result")
    # An old successful write still returns its own immutable snapshot after edits.
    return AuthoringDraftCommitResult(operation=published, draft=AuthoringDraftRecord(
        root_kind=stored.detail.root_kind, root_id=stored.detail.root_id, owner_scope=owner,
        generation=stored.detail.result_generation, snapshot=stored.code_artifact))


def require_draft_write(operation, root_kind, root_id, snapshot, expected_generation):
    if expected_generation is not None and (type(expected_generation) is not int or expected_generation < 1):
        raise ValueError("expected draft generation must be a positive integer or None")
    detail = operation.detail
    if (operation.status is not CriticalOperationStatus.SUCCEEDED
        or operation.operation_kind.value != ("geometry_resource_write" if root_kind == "geometry" else "product_family_write")
        or detail.detail_kind != "authoring_draft_write"
        or detail.root_kind != root_kind or detail.root_id != root_id
        or detail.expected_generation != expected_generation
        or detail.result_generation != (expected_generation or 0) + 1
        or operation.code_artifact != snapshot
        or operation.result_geometry_resource_version_id is not None
        or operation.result_product_family_version_id is not None):
        raise ValueError("draft commit requires its exact successful Write Operation")


async def commit_draft(repository, scope, *, root_kind, root_record, snapshot,
                       expected_generation, operation, new_root, require_same_root):
    require_context(scope)
    model, key = root_model(root_kind)
    root_id = getattr(root_record, key.key)
    require_draft_write(operation, root_kind, root_id, snapshot, expected_generation)
    Operations._require_record_scope(scope, operation)
    try:
        async with repository._sessions() as session, session.begin():
            await set_tenant_execution_context(session, scope.identity)
            await Operations._require_data_run(session, scope)
            await require_root_owner(session, scope, root_record.owner_scope)
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:root_id, 0))"),
                                  {"root_id": root_id})
            replay = await draft_replay(repository, session, scope, operation, root_record.owner_scope)
            if replay is not None:
                return replay
            if await read_snapshot(session, scope, snapshot.version_id) != snapshot:
                raise ConflictError("the draft snapshot differs from the saved exact Artifact")
            root = await session.scalar(select(model).where(key == root_id).with_for_update())
            if root is None:
                if expected_generation is not None:
                    raise ConflictError("the draft root no longer exists")
                root = new_root(root_record)
                root.draft_generation = 0
                session.add(root)
            else:
                require_same_root(root, root_record)
            if root.draft_generation != (expected_generation or 0):
                raise AuthoringDraftChanged("the draft changed; read its current edit token before saving")
            root.draft_artifact_version_id = snapshot.version_id
            root.draft_generation += 1
            await session.flush()
            published = await publish_operation(repository, session, operation)
            return AuthoringDraftCommitResult(operation=published,
                draft=await draft_record(session, scope, root_kind, root))
    except SQLAlchemyError:
        # Commit response loss is reconciled from the original immutable Operation,
        # never by assuming that the current root still points at this snapshot.
        async with repository._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            replay = await draft_replay(repository, session, scope, operation, root_record.owner_scope)
            if replay is not None:
                return replay
        raise


def require_publication_inputs(operation, geometry_publications, family_publication, expected_drafts):
    if operation.operation_kind.value != "authoring_publish" or operation.status is not CriticalOperationStatus.SUCCEEDED:
        raise ValueError("publication requires its synchronous successful Operation")
    detail = operation.detail
    selected = {(item.root_kind, item.root_id): (item.generation, item.snapshot)
                for item in detail.expected_drafts}
    expected = {(item.root_kind, item.root_id): (item.generation, item.snapshot)
                for item in expected_drafts}
    if len(expected) != len(expected_drafts) or selected != expected:
        raise ValueError("publication draft closure differs from the original Operation")
    outputs = {(item.resource.resource_id, item.version.resource_version_id) for item in geometry_publications}
    if (len(outputs) != len(geometry_publications)
        or not outputs <= {(item.resource_id, item.resource_version_id) for item in detail.geometry_versions}
        or ((geometry_publications or family_publication is not None) and (family_publication is None) != (detail.family_version is None))
        or (family_publication is not None and family_publication.version != detail.family_version)
        or any(item.operation != operation for item in geometry_publications)
        or (family_publication is not None and family_publication.operation != operation)):
        raise ValueError("publication results differ from the exact admitted closure")


async def publication_replay(repository, session, scope, operation, geometry_publications, family_publication):
    from CAD2BIM_backend.domain.authoring_draft import AuthoringPublicationCommitResult
    from .repository_geometry_resource import PostgresGeometryResourceRepositoryMixin as Geometry
    from .repository_product_family import PostgresProductFamilyRepositoryMixin as FamilyRead
    from .repository_product_family_authoring import PostgresProductFamilyAuthoringRepositoryMixin as Family

    model = await find_operation(session, scope, operation)
    if model is None:
        return None
    if model.status == "prepared":
        return None
    if model.status != "succeeded":
        raise ConflictError("publication has no committed successful result")
    published = await Operations._published_operation(repository, session, model, run_id=scope.run_id)
    if published is None:
        raise ConflictError("publication has no original Catalog result")
    geometries = []
    incoming_by_id = {item.version.resource_version_id: item for item in geometry_publications}
    for reference in published.operation.detail.geometry_versions:
        actual = await Geometry._load_geometry_bundle(session, resource_id=reference.resource_id,
                                                      resource_version_id=reference.resource_version_id)
        if actual is None:
            raise ConflictError("publication lost an exact Geometry result")
        incoming = incoming_by_id.get(reference.resource_version_id)
        if incoming is not None:
            Geometry._require_same_geometry_publication(actual, incoming.to_bundle(version_no=actual.version.version_no))
        geometries.append(actual)
    family = None
    if published.operation.detail.family_version is not None:
        family = await FamilyRead._load_product_family_bundle(session,
            family_id=published.operation.detail.family_version.family_id,
            family_version_id=published.operation.detail.family_version.family_version_id)
        if family is None:
            raise ConflictError("publication lost its exact Family result")
        if family_publication is not None and family_publication.version.family_version_id == family.version.family_version_id:
            Family._require_same_publication(family, family_publication.to_bundle(version_no=family.version.version_no))
    return AuthoringPublicationCommitResult(operation=published, geometry_versions=tuple(geometries), family_version=family)


async def current_publication(repository, session, scope, *, root_kind, root_id, expected_drafts, same_run):
    from CAD2BIM_backend.domain.data_runtime import AuthoringPublishedGeometryReference
    from .repository_geometry_resource import PostgresGeometryResourceRepositoryMixin as Geometry
    from .repository_product_family import PostgresProductFamilyRepositoryMixin as Family

    selected = [{"root_kind": item.root_kind, "root_id": item.root_id,
                 "generation": item.generation, "snapshot": item.snapshot.model_dump(mode="json")}
                for item in expected_drafts]
    target = next(item for item in expected_drafts if item.root_kind == root_kind and item.root_id == root_id)
    if target.owner_scope.scope_kind is ResourceOwnerScopeKind.TENANT:
        result = await session.scalar(text(
            "SELECT cad2bim_authoring_publication_result(:root_kind, :root_id, :selected)"
        ).bindparams(bindparam("selected", type_=JSONB)),
            {"root_kind": root_kind, "root_id": root_id, "selected": selected})
        if result is None:
            return None
        references = tuple(AuthoringPublishedGeometryReference.model_validate(item)
            for item in result["geometry_versions"])
        family_reference = result["family_version"]
        geometries = tuple([await Geometry._load_geometry_bundle(session, resource_id=item.resource_id,
            resource_version_id=item.resource_version_id) for item in references])
        family = (await Family._load_product_family_bundle(session, family_id=family_reference["family_id"],
            family_version_id=family_reference["family_version_id"]) if family_reference is not None else None)
        if any(item is None for item in geometries) or (family_reference is not None and family is None):
            raise ConflictError("current publication is missing an exact formal result")
        family_detail = await formal_family_draft(session, family) if family is not None else None
        return AuthoringPublicationResources(geometry_versions=geometries, family_version=family_detail)
    selected_json = cast(CriticalOperationModel.operation_detail_json["expected_drafts"], JSONB)
    statement = select(CriticalOperationModel).where(
        CriticalOperationModel.owner_id == scope.owner_id,
        CriticalOperationModel.tenant_id == scope.identity.tenant_id,
        CriticalOperationModel.task_id == scope.identity.task_id,
        CriticalOperationModel.operation_kind == "authoring_publish",
        CriticalOperationModel.status == "succeeded",
        _current_operation_contract_predicate(),
        CriticalOperationModel.operation_detail_json["root_kind"].as_string() == root_kind,
        CriticalOperationModel.operation_detail_json["root_id"].as_string() == root_id,
        selected_json.contains(selected), selected_json.contained_by(selected))
    if same_run:
        statement = statement.where(CriticalOperationModel.run_id == scope.run_id)
    model = await session.scalar(statement.order_by(
        (CriticalOperationModel.run_id == scope.run_id).desc(), CriticalOperationModel.started_at.desc()).limit(1))
    if model is None:
        return None
    published = await Operations._published_operation(repository, session, model, run_id=model.run_id)
    if published is None:
        raise ConflictError("current publication has no original Catalog result")
    geometries = tuple([await Geometry._load_geometry_bundle(session, resource_id=item.resource_id,
        resource_version_id=item.resource_version_id) for item in published.operation.detail.geometry_versions])
    family_detail = published.operation.detail.family_version
    family = (await Family._load_product_family_bundle(session, family_id=family_detail.family_id,
        family_version_id=family_detail.family_version_id) if family_detail is not None else None)
    if any(item is None for item in geometries) or (family_detail is not None and family is None):
        raise ConflictError("current publication is missing an exact formal result")
    return AuthoringPublicationResources(geometry_versions=geometries, family_version=family_detail)


async def formal_family_draft(session, bundle):
    """Project a current formal Family to the existing receipt definition."""
    from CAD2BIM_backend.domain.product_family_authoring_contracts import (
        ProductFamilyAuthoringVersionDraft, ResolvedProductFamilyGeometryBindingInput,
    )
    from .repository_geometry_resource import PostgresGeometryResourceRepositoryMixin as Geometry

    bindings = []
    for binding in bundle.geometry_bindings:
        geometry = await Geometry._load_geometry_bundle(session, resource_id=binding.geometry_resource_id,
            resource_version_id=binding.geometry_resource_version_id)
        if (geometry is None or geometry.version.content_digest != binding.geometry_content_digest
            or not bundle.family.owner_scope.permits_dependency(geometry.resource.owner_scope)):
            raise ConflictError("the reused formal Family binding is unavailable")
        bindings.append(ResolvedProductFamilyGeometryBindingInput.from_version_bundle(
            binding_key=binding.binding_key, expected_resource_kind=binding.expected_resource_kind,
            geometry_purpose=binding.geometry_purpose, geometry_version=geometry))
    return ProductFamilyAuthoringVersionDraft(
        **bundle.version.model_dump(mode="python", exclude={"version_no", "content_digest"}),
        geometry_bindings=tuple(bindings))


async def find_current_publication(repository, scope, *, root_kind, root_id, expected_drafts):
    require_context(scope)
    async with repository._sessions() as session, session.begin():
        await set_tenant_execution_context(session, scope.identity)
        for item in expected_drafts:
            await require_root_owner(session, scope, item.owner_scope)
        await load_current_draft_roots(session, scope, expected_drafts)
        return await current_publication(repository, session, scope, root_kind=root_kind,
            root_id=root_id, expected_drafts=expected_drafts, same_run=False)


async def published_geometry_draft(repository, session, scope, *, draft):
    """Find one exact Geometry output, even when its original publication root was Family."""
    from .repository_geometry_resource import PostgresGeometryResourceRepositoryMixin as Geometry

    if draft.root_kind != "geometry":
        raise ValueError("Geometry draft lookup requires a Geometry root")
    if draft.owner_scope.scope_kind is ResourceOwnerScopeKind.TENANT:
        reference = await session.scalar(text(
            "SELECT cad2bim_authoring_geometry_draft_result(:root_id, :generation, :snapshot)"
        ).bindparams(bindparam("snapshot", type_=JSONB)),
            {"root_id": draft.root_id, "generation": draft.generation,
             "snapshot": draft.snapshot.model_dump(mode="json")})
        if reference is None:
            return None
        geometry = await Geometry._load_geometry_bundle(session, resource_id=reference["resource_id"],
            resource_version_id=reference["resource_version_id"])
        if geometry is None:
            raise ConflictError("published Geometry draft lost its exact formal result")
        return geometry
    selected = [{"root_kind": "geometry", "root_id": draft.root_id,
        "generation": draft.generation, "snapshot": draft.snapshot.model_dump(mode="json")}]
    statement = select(CriticalOperationModel).where(
        CriticalOperationModel.owner_id == scope.owner_id,
        CriticalOperationModel.tenant_id == scope.identity.tenant_id,
        CriticalOperationModel.task_id == scope.identity.task_id,
        CriticalOperationModel.operation_kind == "authoring_publish",
        CriticalOperationModel.status == "succeeded",
        cast(CriticalOperationModel.operation_detail_json["expected_drafts"], JSONB).contains(selected),
        _current_operation_contract_predicate(),
        cast(CriticalOperationModel.operation_detail_json["geometry_versions"], JSONB).contains([
            {"resource_id": draft.root_id}]))
    model = await session.scalar(statement.order_by(
        (CriticalOperationModel.run_id == scope.run_id).desc(), CriticalOperationModel.started_at.desc()).limit(1))
    if model is None:
        return None
    original = await Operations._published_operation(repository, session, model, run_id=model.run_id)
    if original is None:
        raise ConflictError("published Geometry draft has no original Catalog result")
    reference = next(item for item in original.operation.detail.geometry_versions if item.resource_id == draft.root_id)
    geometry = await Geometry._load_geometry_bundle(session, resource_id=reference.resource_id,
        resource_version_id=reference.resource_version_id)
    if geometry is None:
        raise ConflictError("published Geometry draft lost its exact formal result")
    return geometry


async def find_published_geometry_draft(repository, scope, *, draft):
    require_context(scope)
    async with repository._sessions() as session, session.begin():
        await set_tenant_execution_context(session, scope.identity)
        await require_root_owner(session, scope, draft.owner_scope)
        await load_current_draft_roots(session, scope, (draft,))
        return await published_geometry_draft(repository, session, scope, draft=draft)


async def load_current_draft_roots(session, scope, expected_drafts):
    roots = {}
    for item in sorted(expected_drafts, key=lambda value: (value.root_id, value.root_kind)):
        model, key = root_model(item.root_kind)
        root = await session.scalar(select(model).where(key == item.root_id,
            private_root_predicate(model, scope)).with_for_update())
        if root is None or owner_scope(root) != item.owner_scope:
            raise IdentityAccessDenied("publication draft is outside its editable owner scope")
        if (root.draft_generation != item.generation
            or root.draft_artifact_version_id != item.snapshot.version_id
            or await read_snapshot(session, scope, item.snapshot.version_id, root=root) != item.snapshot):
            raise ConflictError("a draft changed after publication input selection")
        roots[(item.root_kind, item.root_id)] = root
    return roots


def reuse_publication_results(operation, previous):
    from CAD2BIM_backend.domain.data_runtime import AuthoringPublishedGeometryReference
    geometries = tuple(AuthoringPublishedGeometryReference(resource_id=item.resource.resource_id,
        resource_version_id=item.version.resource_version_id) for item in previous.geometry_versions)
    root_geometry = next((item for item in geometries if item.resource_id == operation.detail.root_id), None)
    family = previous.family_version
    values = operation.model_dump(mode="python")
    values.update(result_geometry_resource_version_id=root_geometry.resource_version_id if root_geometry else None,
        result_product_family_version_id=family.family_version_id if family else None,
        base_product_family_version_id=family.parent_family_version_id if family else None)
    values["detail"] = operation.detail.model_copy(update={
        "geometry_versions": geometries, "family_version": family})
    return type(operation).model_validate(values)


async def commit_publication(repository, scope, *, operation, geometry_publications,
                             family_publication, expected_drafts, execution_claim):
    from CAD2BIM_backend.domain.authoring_draft import AuthoringPublicationCommitResult
    from CAD2BIM_backend.infrastructure.postgres.models import GeometryResourceVersionModel, ProductFamilyVersionModel
    from .repository_geometry_resource import PostgresGeometryResourceRepositoryMixin as Geometry
    from .repository_product_family_authoring import PostgresProductFamilyAuthoringRepositoryMixin as Family

    require_context(scope)
    require_publication_inputs(operation, geometry_publications, family_publication, expected_drafts)
    Operations._require_record_scope(scope, operation)
    try:
        async with repository._sessions() as session, session.begin():
            await set_tenant_execution_context(session, scope.identity)
            await Operations._require_data_run(session, scope)
            admitted = await Operations._authoring_model(session, scope, operation.operation_id,
                operation_kind=CriticalOperationKind.AUTHORING_PUBLISH, lock=True)
            if admitted is None:
                raise ConflictError("publication requires its original admission")
            if admitted.status == "prepared":
                Operations.require_authoring_claim(admitted, execution_claim)
            # Consistent root ordering spans the complete G/F closure. Acquire all
            # roots before checking any CAS, and retain every lock until commit.
            for item in sorted(expected_drafts, key=lambda value: (value.root_id, value.root_kind)):
                await require_root_owner(session, scope, item.owner_scope)
                await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:root_id, 0))"),
                                      {"root_id": item.root_id})
            replay = await publication_replay(repository, session, scope, operation,
                                               geometry_publications, family_publication)
            if replay is not None:
                return replay
            roots = await load_current_draft_roots(session, scope, expected_drafts)
            current = await current_publication(repository, session, scope, root_kind=operation.detail.root_kind,
                root_id=operation.detail.root_id, expected_drafts=expected_drafts, same_run=False)
            if current is not None:
                operation = reuse_publication_results(operation, current)
                published = await finish_publication(repository, session, scope, operation, execution_claim)
                from .repository_product_family import PostgresProductFamilyRepositoryMixin as FamilyRead
                family = (await FamilyRead._load_product_family_bundle(session,
                    family_id=current.family_version.family_id, family_version_id=current.family_version.family_version_id)
                    if current.family_version is not None else None)
                return AuthoringPublicationCommitResult(operation=published,
                    geometry_versions=current.geometry_versions, family_version=family)
            if not geometry_publications and family_publication is None and not (
                operation.detail.root_kind == "geometry" and len(expected_drafts) == 1
                and len(operation.detail.geometry_versions) == 1 and operation.detail.family_version is None
            ):
                raise ConflictError("publication receipt requires an existing exact formal closure")
            proposed_ids = {item.version.resource_version_id for item in geometry_publications}
            for reference in operation.detail.geometry_versions:
                selected = tuple(item for item in expected_drafts
                    if item.root_kind == "geometry" and item.root_id == reference.resource_id)
                prior = await published_geometry_draft(repository, session, scope, draft=selected[0]) if selected else None
                if reference.resource_version_id in proposed_ids:
                    if prior is not None:
                        raise ConflictError(
                            "Geometry draft was published while preparing this closure; "
                            "confirm its published version, then submit a new publication request. "
                            "The unchanged draft may be reused"
                        )
                    continue
                if prior is None or prior.version.resource_version_id != reference.resource_version_id:
                    raise ConflictError("reused Geometry does not match its exact published draft")
            await Operations._require_operation_artifacts(session, operation)
            geometries = []
            # Applications pass dependency-first Geometry proposals. The existing
            # exact-input projection checks already inserted or published inputs.
            for proposal in geometry_publications:
                Geometry._require_same_geometry_root(roots[("geometry", proposal.resource.resource_id)], proposal.resource)
                maximum = await session.scalar(select(func.max(GeometryResourceVersionModel.version_no)).where(
                    GeometryResourceVersionModel.resource_id == proposal.resource.resource_id))
                bundle = proposal.to_bundle(version_no=int(maximum or 0) + 1)
                geometries.append(await Geometry._insert_geometry_bundle(session, bundle))
            family = None
            if family_publication is not None:
                root = roots[("family", family_publication.family.family_id)]
                Family._require_same_family_root(root, family_publication.family)
                maximum = await session.scalar(select(func.max(ProductFamilyVersionModel.version_no)).where(
                    ProductFamilyVersionModel.family_id == family_publication.family.family_id))
                family = await Family._insert_product_family_bundle(session,
                    family_publication.to_bundle(version_no=int(maximum or 0) + 1))
            published = await finish_publication(repository, session, scope, operation, execution_claim)
            geometries = tuple([await Geometry._load_geometry_bundle(session, resource_id=item.resource_id,
                resource_version_id=item.resource_version_id) for item in operation.detail.geometry_versions])
            if any(item is None for item in geometries):
                raise ConflictError("publication lost part of its exact Geometry closure")
            return AuthoringPublicationCommitResult(operation=published,
                geometry_versions=geometries, family_version=family)
    except IntegrityError as exc:
        logger.exception("Authoring publication transaction rejected by database integrity check")
        raise AuthoringPublicationCommitRejected(
            "Publication was rejected by an integrity check; the draft remains available."
        ) from exc
    except SQLAlchemyError:
        async with repository._sessions() as session:
            await set_tenant_execution_context(session, scope.identity)
            replay = await publication_replay(repository, session, scope, operation,
                                               geometry_publications, family_publication)
            if replay is not None:
                return replay
        raise
