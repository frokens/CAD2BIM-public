"""Editable Family content, exact reads and authorized SYSTEM publication copies."""

from __future__ import annotations


import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal, NoReturn

from pydantic import ValidationError, TypeAdapter, RootModel, JsonValue
from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftService, validate_draft_source
from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftRecord, AuthoringDraftCommitResult
from CAD2BIM_backend.domain.authoring_draft_content import FamilyDraftContent, FamilyPublishedBase, SavedFamilyDraftDefinition
from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftWriteCommand, FamilyDraftDefinition, ProductFamilyAuthoringSourceFile

from CAD2BIM_backend.application.artifact_service import ArtifactDownload, ArtifactService
from CAD2BIM_backend.application.exact_artifact_reader import (
    ExactArtifactReadError,
    ExactArtifactReader,
)
from CAD2BIM_backend.application.product_family_asset_service import (
    ProductFamilyAssetIntegrityError,
    ProductFamilyAssetService,
)
from CAD2BIM_backend.application.product_family_catalog_service import (
    ProductFamilyCatalogService,
)
from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputError, ProductRealizationInputService
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest, stable_id
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ConflictError
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef,
    AuthoringExecutionClaim,
    CriticalOperationErrorPhase,
    CriticalOperationInitiator,
    CriticalOperationKind,
    CriticalOperationRecord,
    CriticalOperationStatus,
    ProductFamilyWriteOperationDetail,
    PublishedCriticalOperation,
)
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceVersionBundle,
)
from CAD2BIM_backend.domain.geometry_interface import resolve_fixed_geometry_interfaces
from CAD2BIM_backend.domain.identity import stable_internal_identity_id
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.product_family import ProductFamilyGeometryBindingRecord, ProductFamilyRecord, ProductFamilyVersionAssetRecord, ProductFamilyVersionAssetRole, ProductFamilyVersionBundle
from CAD2BIM_backend.domain.product_family_authoring import ProductFamilyAuthoringCommitResult, ProductFamilyAuthoringPublication, ProductFamilyAuthoringVersionDraft, ResolvedProductFamilyGeometryBindingInput
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyCatalogIntegrityError,
    ProductFamilyCatalogReferenceError,
    ProductFamilyCatalogNotFoundError,
)
from CAD2BIM_backend.domain.product_realization import MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES, MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS, MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES, ProductRealizationGeometryExecution, ProductFamilyStandardModelManifest, product_geometry_binding_digest
from CAD2BIM_backend.domain.resource_scope import (
    ResourceOwnerScope,
    ResourceOwnerScopeKind,
)
from CAD2BIM_backend.domain.system_family_publication import (
    SystemFamilyCopyPlan,
    SystemFamilyPublicationCommand,
    SystemGeometryCopyDraft,
)
from CAD2BIM_backend.domain.version_usage import MAX_VERSION_USAGE_INSTRUCTIONS_UTF8_BYTES
from CAD2BIM_backend.ports.geometry_authoring import GeometryAuthoringSourceError, GeometryAuthoringSourcePort
from CAD2BIM_backend.ports.product_family_authoring import (
    ProductFamilyAuthoringCommitFenceError,
    ProductFamilyAuthoringRepository,
)
from CAD2BIM_backend.ports.storage import StorageError
from CAD2BIM_backend.ports.source_patch import SourcePatchPort, SourcePatchPortError


_WRITE_TOOL = "ifc_product_family_write"
_SOURCE_CONTENT_TYPE = "application/zip"
_SOURCE_FILENAME = "product_family_source.zip"
_SOURCE_KIND = "product_family_authoring_source"
_MAX_TECHNICAL_ATTEMPTS = 3
_MAX_DIAGNOSTIC_BYTES = 2_000


class ProductFamilyAuthoringApplicationError(RuntimeError):
    """Bounded failure suitable for a future Provider or Runner projection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: Literal[
            "preflight",
            "code_validation",
            "execution",
            "output_validation",
            "storage_verification",
            "commit_fence",
            "reconcile",
        ] = "preflight",
        retryable: bool = False,
        operation_alias: str | None = None,
        diagnostic: dict[str, object] | None = None,
        fixed_input_rejection: bool = False,
    ) -> None:
        super().__init__(_bounded_message(message))
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.operation_alias = operation_alias
        self.diagnostic = diagnostic
        self.fixed_input_rejection = fixed_input_rejection


class ProductFamilyAuthoringService:
    """Save Family drafts and preserve the distinct exact SYSTEM copy path."""

    def __init__(
        self,
        repository: ProductFamilyAuthoringRepository,
        artifacts: ArtifactService,
        family_catalog: ProductFamilyCatalogService,
        family_assets: ProductFamilyAssetService,
        realization_inputs: ProductRealizationInputService,
        source_bundles: GeometryAuthoringSourcePort,
        *,
        source_patch: SourcePatchPort,
        clock: Callable[[], datetime] | None = None,
        drafts: AuthoringDraftService | None = None,
        geometry_authoring=None,
    ) -> None:
        self._repository = repository
        self._drafts = drafts
        self._geometry_authoring = geometry_authoring
        self._artifacts = artifacts
        self._exact_artifacts = ExactArtifactReader(artifacts)
        self._family_catalog = family_catalog
        self._family_assets = family_assets
        self._realization_inputs = realization_inputs
        self._source_bundles = source_bundles
        self._source_patch = source_patch
        self._clock = clock or (lambda: datetime.now(UTC))

    def draft_reference(self, scope, draft):
        return self._drafts.reference(scope, draft)

    async def request_rejection_status(self, scope, *, request_identity, error) -> Literal["not_started", "unknown"]:
        definite = self._drafts.fixed_input_rejection(error) or (
            isinstance(error, ProductFamilyAuthoringApplicationError) and (
                error.fixed_input_rejection or error.code == "DRAFT_CHANGED"
                or (error.code == "PRODUCT_FAMILY_INPUT_INVALID" and (error.diagnostic or {}).get("failure")
                    in {"stale_source", "match_not_found", "match_not_unique", "overlap", "budget_exceeded"})
            )
        )
        return await self._drafts.request_rejection_status(scope, tool_name=_WRITE_TOOL,
            request_identity=request_identity, definite_rejection=definite)

    async def operation_result(self, scope, published):
        from CAD2BIM_backend.domain.data_runtime import AuthoringDraftWriteOperationDetail, CriticalOperationKind
        if isinstance(published.operation.detail, AuthoringDraftWriteOperationDetail):
            if published.operation.code_artifact is None:
                raise ProductFamilyAuthoringApplicationError("FAMILY_DRAFT_NOT_AVAILABLE", "Original Family draft snapshot is unavailable")
            await self.read_draft_snapshot(scope, published.operation.code_artifact)
            return self._drafts.operation_result(scope, published)
        if published.operation.operation_kind is CriticalOperationKind.AUTHORING_PUBLISH:
            return await self._publication.operation_result(scope, published)
        return None

    async def publish_draft(self, scope, **request):
        return await self._publication.publish(scope, root_kind="family", **request)

    async def read_draft(self, scope, *, draft_ref):
        root_id = self._drafts.resolve_reference(scope, draft_ref, root_kind="family")
        draft = await self._repository.get_product_family_draft(scope, family_id=root_id)
        if draft is None:
            raise ProductFamilyAuthoringApplicationError("FAMILY_DRAFT_NOT_AVAILABLE", "No editable Family draft is visible")
        return draft, await self.read_draft_snapshot(scope, draft.snapshot, draft=draft)

    async def read_draft_snapshot(self, scope, snapshot, *, draft=None):
        raw = await self._drafts.read_snapshot(scope, snapshot, RootModel[dict[str, JsonValue]], draft=draft)
        try:
            return FamilyDraftContent.model_validate(raw.root)
        except ValueError as exc:
            raise ProductFamilyAuthoringApplicationError(
                "FAMILY_DRAFT_NOT_AVAILABLE", "This exact Family draft has an invalid contract"
            ) from exc

    async def draft_info(self, scope, *, draft_ref):
        from CAD2BIM_backend.domain.authoring_draft_content import FamilyDraftInfo
        draft, content = await self.read_draft(scope, draft_ref=draft_ref)
        definition = await self._project_draft_definition(scope, content.definition, draft.owner_scope)
        return FamilyDraftInfo(draft_ref=draft_ref, edit_token=draft.generation,
            **await self._drafts.save_navigation(scope, draft),
            display_name=content.display_name, purpose=content.purpose, usage_instructions=content.usage_instructions,
            contract_json=canonical_json(content.definition.family_contract),
            geometry_bindings=definition.geometry_bindings, source_files=tuple(item.path for item in content.source_files))

    async def save_draft_from_aliases(self, scope, *, declaration, request_identity):
        try:
            command = TypeAdapter(FamilyDraftWriteCommand).validate_python(declaration)
        except ValidationError as exc:
            raise _draft_input_validation_error(exc, declaration=declaration) from None
        raw = command.model_dump(mode="json", exclude_unset=True)
        operation_id, fingerprint, replay = await self._drafts.replay(
            scope, root_kind="family", declaration=raw, request_identity=request_identity)
        owner = await self._drafts.resource_owner(scope)
        if replay is not None:
            detail = replay.operation.detail
            await self.read_draft_snapshot(scope, replay.operation.code_artifact)
            return AuthoringDraftCommitResult(operation=replay, draft=AuthoringDraftRecord(
                root_kind="family", root_id=detail.root_id, owner_scope=owner,
                generation=detail.result_generation, snapshot=replay.operation.code_artifact))
        expected = None
        previous_content = None
        root_id = stable_id("product_family", {"task_id": scope.identity.task_id, "operation_id": operation_id})
        if command.mode == "update":
            previous, content = await self.read_draft(scope, draft_ref=command.draft_ref)
            await self._drafts.require_write_owner(scope, previous.owner_scope)
            owner = previous.owner_scope
            previous_content = content
            if previous.generation != command.expected_edit_token:
                raise ProductFamilyAuthoringApplicationError("DRAFT_CHANGED", "Family draft changed; reread before editing")
            root_id, expected = previous.root_id, previous.generation
            values = content.model_dump(mode="python")
            for field in ("display_name", "purpose"):
                if field in command.model_fields_set:
                    values[field] = getattr(command, field)
            if "usage_instructions" in command.model_fields_set:
                change = command.usage_instructions
                if change.action == "set":
                    values["usage_instructions"] = change.value
                else:
                    try:
                        values["usage_instructions"] = self._source_patch.apply_text(
                            content.usage_instructions, change.edits,
                            maximum_source_bytes=MAX_VERSION_USAGE_INSTRUCTIONS_UTF8_BYTES)
                    except SourcePatchPortError as exc:
                        raise ProductFamilyAuthoringApplicationError(
                            "PRODUCT_FAMILY_INPUT_INVALID", str(exc),
                            diagnostic={"field": "usage_instructions", "edit_index": exc.edit_index, "failure": exc.failure}) from None
            if "definition" in command.model_fields_set:
                projected = (None if command.definition.action == "set" else
                    await self._project_draft_definition(scope, content.definition, owner))
                try:
                    values["definition"] = (command.definition.value if command.definition.action == "set"
                        else command.definition.merge(projected))
                except ValidationError as exc:
                    raise _draft_input_validation_error(exc, field="definition", fixed_input_rejection=True) from None
                except ValueError as exc:
                    raise ProductFamilyAuthoringApplicationError("PRODUCT_FAMILY_INPUT_INVALID", str(exc),
                        diagnostic={"field": "definition"}) from None
            if "source_changes" in command.model_fields_set:
                try:
                    packed = self._source_bundles.pack({f.path: f.source.encode("utf-8") for f in content.source_files})
                    changed = self._source_bundles.mutate(packed.content, packed.sha256, command.source_changes,
                        maximum_members=MAX_PRODUCT_REALIZATION_SOURCE_MEMBERS,
                        maximum_member_bytes=MAX_PRODUCT_REALIZATION_SOURCE_MEMBER_BYTES,
                        maximum_total_bytes=MAX_PRODUCT_REALIZATION_SOURCE_TOTAL_BYTES)
                    values["source_files"] = self._draft_source_files(changed.content)
                except GeometryAuthoringSourceError as exc:
                    raise ProductFamilyAuthoringApplicationError("PRODUCT_FAMILY_INPUT_INVALID", str(exc),
                        diagnostic={"field": "source_changes", "failure": exc.failure.value}) from exc
        elif command.source.source_kind == "published":
            bundle = await self._family_catalog.resolve_exact_version(scope.identity, command.source.family_version_alias)
            source = next(a for a in bundle.assets if a.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE)
            source_bytes = await self._family_assets.read_exact_asset(bundle, source)
            bindings = []
            for binding in bundle.geometry_bindings:
                geometry = await self._geometry_authoring._resources.get_exact_version(scope.identity,
                    resource_id=binding.geometry_resource_id, resource_version_id=binding.geometry_resource_version_id)
                alias = self._geometry_authoring._catalog.version_ref(scope.identity, geometry)
                bindings.append({"binding_key": binding.binding_key, "source": {"source_kind": "published", "resource_version_alias": alias},
                    "expected_resource_kind": binding.expected_resource_kind, "geometry_purpose": binding.geometry_purpose})
            values = {
                "display_name": command.display_name if "display_name" in command.model_fields_set else bundle.version.display_name,
                "purpose": command.purpose if "purpose" in command.model_fields_set else bundle.version.purpose,
                "usage_instructions": command.usage_instructions.value if "usage_instructions" in command.model_fields_set else bundle.version.usage_instructions,
                "source_files": self._draft_source_files(source_bytes),
                "definition": FamilyDraftDefinition(family_contract=bundle.version.family_contract, geometry_bindings=bindings),
                "published_base": FamilyPublishedBase(family_id=bundle.family.family_id, family_version_id=bundle.version.family_version_id)}
            if bundle.family.owner_scope == owner:
                root_id = bundle.family.family_id
        else:
            values = {"display_name": command.display_name, "purpose": command.purpose,
                "usage_instructions": command.usage_instructions.value, "source_files": command.source.source_files,
                "definition": command.source.definition.value}
        if command.mode != "update" or "definition" in command.model_fields_set:
            values["definition"] = await self._validate_draft_definition(scope, values["definition"], owner=owner)
        content = _validate_draft_content(values)
        members = {f.path: f.source.encode("utf-8") for f in content.source_files}
        self._source_bundles.pack(members)
        validate_draft_source(members, entry_functions=("realize", "standard_model"))
        if content == previous_content:
            raise ProductFamilyAuthoringApplicationError("DRAFT_UNCHANGED", "The draft content is unchanged")
        family = ProductFamilyRecord(family_id=root_id, owner_scope=owner, created_at=self._now())
        existing = await self._repository.get_product_family_authoring_root(scope, family_id=root_id)
        if existing is not None:
            family = existing
        snapshot, operation = await self._drafts.save(scope, root_kind="family", root_id=root_id,
            content=content, expected_generation=expected, operation_id=operation_id,
            fingerprint=fingerprint, request_identity=request_identity)
        try:
            return await self._repository.commit_product_family_draft(scope, family=family,
                snapshot=snapshot, expected_generation=expected, operation=operation)
        except ConflictError:
            raise ProductFamilyAuthoringApplicationError(
                "DRAFT_CHANGED", "Family draft changed; reread before editing", fixed_input_rejection=True) from None

    def _draft_source_files(self, source_bundle):
        inspected = self._source_bundles.inspect(source_bundle)
        return tuple(ProductFamilyAuthoringSourceFile(path=member.source_path, source=inspected.read_text(member.source_path))
            for member in inspected.members if member.source_path.endswith(".py"))

    async def _validate_draft_definition(self, scope, definition, *, owner):
        try:
            self._realization_inputs.normalize_standard_definition(definition.family_contract)
        except ProductRealizationInputError as exc:
            raise ProductFamilyAuthoringApplicationError("PRODUCT_FAMILY_INPUT_INVALID", str(exc),
                diagnostic={"field": "definition.family_contract.standard_model"}) from None
        except (ValueError, ValidationError) as exc:
            raise ProductFamilyAuthoringApplicationError("PRODUCT_FAMILY_INPUT_INVALID", str(exc),
                diagnostic={"field": "definition.family_contract.standard_model"}) from None
        required = {option.binding_key for fragment in definition.family_contract.geometry_fragments for option in fragment.binding_options}
        actual = {binding.binding_key for binding in definition.geometry_bindings}
        if required != actual:
            missing, undeclared = sorted(required - actual), sorted(actual - required)
            raise ProductFamilyAuthoringApplicationError("BINDINGS_INVALID",
                f"Family bindings must cover every declared binding option exactly; missing binding keys: {missing}; undeclared binding keys: {undeclared}",
                diagnostic={"field": "definition.geometry_bindings", "missing_binding_keys": missing, "undeclared_binding_keys": undeclared})
        schemas, saved_bindings = {}, []
        for binding in definition.geometry_bindings:
            if binding.source.source_kind == "draft":
                draft, content = await self._geometry_authoring.read_draft(scope, draft_ref=binding.source.draft_ref)
                if draft.owner_scope != owner:
                    raise ProductFamilyAuthoringApplicationError("BINDINGS_INVALID",
                        "Family draft bindings must use the same editable resource owner")
                source = {"source_kind": "draft", "resource_id": draft.root_id}
                kind = content.input_metadata.resource_kind
                if kind is GeometryResourceKind.MODELING_FUNCTION:
                    schemas[binding.binding_key] = content.input_metadata.parameter_schema
            else:
                bundle = await self._geometry_authoring._catalog.resolve_exact_version(scope.identity, binding.source.resource_version_alias)
                if not owner.permits_dependency(bundle.resource.owner_scope):
                    raise ProductFamilyAuthoringApplicationError("BINDINGS_INVALID",
                        "A published Geometry binding cannot have a narrower owner scope than the Family")
                source = {"source_kind": "published", "resource_id": bundle.resource.resource_id,
                    "resource_version_id": bundle.version.resource_version_id}
                kind = bundle.resource.resource_kind
                if kind is GeometryResourceKind.MODELING_FUNCTION:
                    _, body = await self._geometry_authoring._resources.read_exact_asset(scope.identity,
                        resource_id=bundle.resource.resource_id, resource_version_id=bundle.version.resource_version_id,
                        asset_role=GeometryResourceAssetRole.PARAMETER_SCHEMA)
                    import json
                    schemas[binding.binding_key] = json.loads(body)
            if kind is not binding.expected_resource_kind:
                raise ProductFamilyAuthoringApplicationError("BINDINGS_INVALID", "Family binding kind differs from the exact Geometry",
                    diagnostic={"field": "definition.geometry_bindings", "binding_key": binding.binding_key})
            saved_bindings.append({**binding.model_dump(mode="python"), "source": source})
        try:
            contract = self._realization_inputs.normalize_contract_defaults(definition.family_contract, schemas)
        except (ProductRealizationInputError, ValueError) as exc:
            raise ProductFamilyAuthoringApplicationError("PRODUCT_FAMILY_INPUT_INVALID", str(exc),
                diagnostic={"field": "definition.family_contract.geometry_fragments"}) from None
        return SavedFamilyDraftDefinition(family_contract=contract, geometry_bindings=saved_bindings)

    async def _project_draft_definition(self, scope, definition, owner):
        """Issue current-scope references from the one saved exact definition."""
        bindings = []
        for binding in definition.geometry_bindings:
            if binding.source.source_kind == "draft":
                draft = await self._geometry_authoring._repository.get_geometry_draft(
                    scope, resource_id=binding.source.resource_id)
                if draft is None or draft.owner_scope != owner:
                    raise ProductFamilyAuthoringApplicationError("BINDINGS_INVALID", "The saved Geometry draft is unavailable")
                source = {"source_kind": "draft", "draft_ref": self._drafts.reference(scope, draft)}
            else:
                bundle = await self._geometry_authoring._resources.get_exact_version(scope.identity,
                    resource_id=binding.source.resource_id, resource_version_id=binding.source.resource_version_id)
                if bundle is None or not owner.permits_dependency(bundle.resource.owner_scope):
                    raise ProductFamilyAuthoringApplicationError("BINDINGS_INVALID", "The saved exact Geometry binding is unavailable")
                source = {"source_kind": "published",
                    "resource_version_alias": self._geometry_authoring._catalog.version_ref(scope.identity, bundle)}
            bindings.append({**binding.model_dump(mode="python"), "source": source})
        return FamilyDraftDefinition(family_contract=definition.family_contract, geometry_bindings=bindings)

    async def prepare_draft_preview(self, scope, *, draft_ref, invocations):
        from CAD2BIM_backend.domain.preview_cases import PreviewSourceSnapshot
        from CAD2BIM_backend.domain.product_realization import ResolvedMappedGeometryBinding, ResolvedModelingFunctionBinding
        from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry
        from CAD2BIM_backend.application.product_realization_input_service import normalize_product_realization_invocation
        from CAD2BIM_backend.domain.product_family_authoring_contracts import ProductFamilyInvocationSemanticError
        draft, content = await self.read_draft(scope, draft_ref=draft_ref)
        source = PreviewSourceSnapshot(source_kind="family_realization", source_type="draft",
            source_id=draft.root_id, source_ref=draft_ref, snapshot_id=draft.snapshot.version_id,
            content_digest=content.execution_digest, title=content.display_name, draft_artifact=draft.snapshot)
        runtime_bindings, bindings, dependencies, schemas = [], [], [], {}
        deferred_mapped = {}
        function_materials, published_exact = {}, {}
        resolved_drafts = {}
        definition = await self._project_draft_definition(scope, content.definition, draft.owner_scope)
        for binding in definition.geometry_bindings:
            if binding.source.source_kind == "draft":
                reference = binding.source.draft_ref
                root_id = self._drafts.resolve_reference(scope, reference, root_kind="geometry")
                if root_id not in resolved_drafts:
                    prepared = await self._geometry_authoring.prepare_draft_preview(scope, draft_ref=reference)
                    resolved_drafts[root_id] = prepared
                    dependencies.append(prepared["source"])
                    dependencies.extend(prepared["dependencies"])
                prepared = resolved_drafts[root_id]
                geometry_source, build = prepared["source"], prepared["build_request"]
                if binding.expected_resource_kind is GeometryResourceKind.MODELING_FUNCTION:
                    schema = prepared["parameter_schema"]
                    if schema is None:
                        raise ProductFamilyAuthoringApplicationError("BINDINGS_INVALID", "Function binding needs its actual parameter contract")
                    schemas[binding.binding_key] = schema
                    runtime = ResolvedModelingFunctionBinding(binding_key=binding.binding_key,
                        geometry_content_digest=geometry_source.content_digest, source_bundle=build.source_bundle,
                        source_bundle_sha256=hashlib.sha256(build.source_bundle).hexdigest(),
                        parameter_schema=schema)
                    function_materials[binding.binding_key] = (prepared["material_loader"], build)
                else:
                    if not isinstance(build, CanonicalGeometry):
                        deferred_mapped[len(runtime_bindings)] = (binding.binding_key, geometry_source, prepared)
                        runtime = None
                    else:
                        runtime = ResolvedMappedGeometryBinding(binding_key=binding.binding_key,
                            geometry_content_digest=geometry_source.content_digest, geometry=build,
                            interfaces=prepared["interfaces"])
                record = ProductFamilyGeometryBindingRecord(family_version_id=draft.snapshot.version_id,
                    binding_key=binding.binding_key, geometry_resource_id=geometry_source.source_id,
                    geometry_resource_version_id=geometry_source.snapshot_id, geometry_content_digest=geometry_source.content_digest,
                    expected_resource_kind=binding.expected_resource_kind, geometry_purpose=binding.geometry_purpose)
            else:
                bundle = await self._geometry_authoring._catalog.resolve_exact_version(scope.identity, binding.source.resource_version_alias)
                record = ProductFamilyGeometryBindingRecord(family_version_id=draft.snapshot.version_id,
                    binding_key=binding.binding_key, geometry_resource_id=bundle.resource.resource_id,
                    geometry_resource_version_id=bundle.version.resource_version_id,
                    geometry_content_digest=bundle.version.content_digest, expected_resource_kind=binding.expected_resource_kind,
                    geometry_purpose=binding.geometry_purpose)
                runtime = (await self._realization_inputs._load_runtime_bindings(scope, (record,), (bundle,), numeric_only=True))[0]
                published_exact[binding.binding_key] = bundle
                if isinstance(runtime, ResolvedModelingFunctionBinding):
                    schemas[binding.binding_key] = runtime.parameter_schema
                dependencies.append(PreviewSourceSnapshot(source_kind="geometry_sample", source_type="published",
                    source_id=bundle.resource.resource_id, source_ref=binding.source.resource_version_alias,
                    snapshot_id=bundle.version.resource_version_id, content_digest=bundle.version.content_digest,
                    title=bundle.resource.display_name))
            runtime_bindings.append(runtime)
            bindings.append(record)
        contract = self._realization_inputs.normalize_contract_defaults(content.definition.family_contract, schemas)
        packed = self._source_bundles.pack({f.path: f.source.encode("utf-8") for f in content.source_files})
        normalized_invocations = []
        for index, invocation in enumerate(invocations):
            try:
                normalized_invocations.append(normalize_product_realization_invocation(contract, invocation))
            except ProductFamilyInvocationSemanticError as exc:
                raise ProductFamilyAuthoringApplicationError(
                    "PRODUCT_FAMILY_INPUT_INVALID", str(exc), phase="preflight",
                    diagnostic={"item_index": index, "field": exc.invalid_path,
                                "reason": exc.reason, "correction": exc.correction},
                ) from exc
        invocations = tuple(normalized_invocations)
        async def load_requests(geometry_runtime):
            # Called only after Preview admission/replay, using the exact bytes
            # frozen above. It never reloads a mutable draft.
            actual = list(runtime_bindings)
            for index, (key, geometry_source, prepared) in deferred_mapped.items():
                build = prepared["build_request"]
                if not isinstance(build, CanonicalGeometry):
                    result = await geometry_runtime.build_fixed(build)
                    if result.source_bundle_sha256 != hashlib.sha256(build.source_bundle).hexdigest():
                        raise ValueError("Deferred Geometry build changed its frozen source")
                    build = result.geometry
                    prepared["build_request"], prepared["interfaces"] = build, resolve_fixed_geometry_interfaces(
                        prepared["interfaces"], result.interfaces)
                actual[index] = ResolvedMappedGeometryBinding(binding_key=key,
                    geometry_content_digest=geometry_source.content_digest, geometry=build, interfaces=prepared["interfaces"])
            return self._realization_inputs.prepare_definition_requests(family_contract=contract,
                source_bundle=packed.content, geometry_bindings=tuple(actual), invocations=invocations)
        material_cache = {}
        async def load_materials(requests, prechecks):
            selected = {fragment.binding_key for result in prechecks for fragment in result.proposal.fragments}
            for binding in requests[0].geometry_bindings:
                key = binding.binding_key
                if key not in selected or key in material_cache:
                    continue
                if key in function_materials:
                    loader, build = function_materials[key]
                    loaded = await loader(build)
                    material_cache[key] = binding.model_copy(update={"geometry_inputs": loaded.geometry_inputs})
                elif key in published_exact:
                    bundle = published_exact[key]
                    record = next(item for item in bindings if item.binding_key == key)
                    if isinstance(binding, ResolvedMappedGeometryBinding):
                        material_cache[key] = (await self._realization_inputs._load_runtime_bindings(scope, (record,), (bundle,)))[0]
                    else:
                        from CAD2BIM_backend.domain.geometry_resource import geometry_resource_inputs
                        inputs = geometry_resource_inputs(bundle.version.manifest)
                        material_cache[key] = binding.model_copy(update={"geometry_inputs":
                            await self._geometry_authoring._resources.load_build_inputs(scope.identity, inputs) if inputs else ()})
                else:
                    material_cache[key] = binding
            return tuple(request.model_copy(update={"geometry_bindings": tuple(material_cache.get(item.binding_key, item)
                for item in request.geometry_bindings)}) for request in requests)
        unique_dependencies = {(d.source_kind, d.source_id, d.snapshot_id): d for d in dependencies}
        return {"source": source, "requests": None, "request_loader": load_requests,
            "family_contract": contract, "invocations": invocations, "geometry_bindings": tuple(bindings),
            "material_loader": load_materials,
            "dependencies": tuple(unique_dependencies.values())}

    async def find_system_copy(
        self, scope: InvocationScope, command: SystemFamilyPublicationCommand,
    ) -> CriticalOperationRecord | None:
        """Authorize and recover a management request before touching its source."""
        await self._repository.authorize_system_family_publication(scope)
        _require_task_scope(scope)
        operation_id = stable_internal_identity_id(
            "op_", "system-family-publication-operation", scope.run_id, command.request_id,
        )
        operation = await self._get_operation_state_with_retries(scope, operation_id)
        if operation is None:
            return None
        fingerprint = sha256_digest({
            "mode": "system_copy", "command": command.model_dump(mode="json"),
        })
        if operation.request_fingerprint != fingerprint:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_IDEMPOTENCY_CONFLICT",
                "The SYSTEM publication request was reused with different arguments",
            )
        detail = operation.detail
        if not isinstance(detail, ProductFamilyWriteOperationDetail) or detail.mode != "system_copy":
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_OPERATION_INTEGRITY_FAILED",
                "The original SYSTEM publication lost its saved copy plan",
                phase="reconcile", retryable=True,
            )
        plan = detail.system_copy
        if plan is None or (
            plan.source_family_id != command.source_family_id
            or plan.source_family_version_id != command.source_family_version_id
            or plan.target_owner != command.target_owner
        ):
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_OPERATION_INTEGRITY_FAILED",
                "The saved SYSTEM copy plan disagrees with its original request",
                phase="reconcile", retryable=True,
            )
        return operation

    async def prepare_system_copy(
        self,
        scope: InvocationScope,
        command: SystemFamilyPublicationCommand,
        *,
        source: ProductFamilyVersionBundle,
        source_assets: tuple[bytes, ...],
        source_kind: Literal["authorized_catalog", "selected_library"],
        source_reference: str,
        geometry_copies: tuple[SystemGeometryCopyDraft, ...],
    ) -> CriticalOperationRecord:
        """Freeze a complete copy on the original Operation before Geometry publication."""
        original = await self.find_system_copy(scope, command)
        if original is not None:
            return original
        operation_id = stable_internal_identity_id(
            "op_", "system-family-publication-operation", scope.run_id, command.request_id,
        )
        family_id = stable_internal_identity_id(
            "pf_", "system-family-publication-root", scope.run_id, command.request_id,
        )
        version_id = stable_internal_identity_id(
            "fv_", "system-family-publication-version", scope.run_id, command.request_id,
        )
        try:
            source = ProductFamilyVersionBundle.model_validate(source.model_dump())
            if (
                source.family.family_id != command.source_family_id
                or source.version.family_version_id != command.source_family_version_id
                or source.family.owner_scope.scope_kind is not ResourceOwnerScopeKind.TASK
                or source.version.family_contract.standard_model is None
                or family_id == source.family.family_id
                or version_id == source.version.family_version_id
            ):
                raise ValueError("source is not the requested successful current TASK version")
            if len(source.assets) != len(source_assets):
                raise ValueError("source asset bytes are incomplete")
            content_by_role = {}
            for asset, content in zip(source.assets, source_assets, strict=True):
                if (not isinstance(content, bytes) or len(content) != asset.size_bytes
                    or hashlib.sha256(content).hexdigest() != asset.sha256):
                    raise ProductFamilyAssetIntegrityError("source Family asset bytes are not exact")
                content_by_role[asset.asset_role] = content
            required_roles = {
                ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE,
                ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY,
                ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST,
            }
            if set(content_by_role) != required_roles:
                raise ValueError("source must contain only complete formal Family assets")
            source_content = content_by_role[ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE]
            inspected = self._source_bundles.inspect(source_content)
            if not any(item.source_path == "main.py" and item.text_available for item in inspected.members):
                raise ValueError("source bundle requires exact UTF-8 main.py")
            manifest = self._family_assets._parse_standard_model_manifest(
                source, content_by_role[ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST],
            )
            geometry_copies = tuple(
                SystemGeometryCopyDraft.model_validate(item.model_dump())
                for item in geometry_copies
            )
            copies = {
                item.source_resource_version_id: item for item in geometry_copies
            }
            required_versions = {
                item.geometry_resource_version_id for item in source.geometry_bindings
            }
            if len(copies) != len(geometry_copies) or not required_versions.issubset(copies):
                raise ValueError("Geometry copy mapping must cover every exact source binding")
            bindings = []
            for binding in source.geometry_bindings:
                copied = copies[binding.geometry_resource_version_id]
                owner = copied.source_owner_scope
                if (
                    copied.source_resource_id != binding.geometry_resource_id
                    or copied.source_content_digest != binding.geometry_content_digest
                    or copied.target_version.resource.owner_scope != command.target_owner
                    or (owner.scope_kind is not ResourceOwnerScopeKind.SYSTEM and (
                        owner.tenant_id != source.family.owner_scope.tenant_id
                        or (owner.scope_kind is ResourceOwnerScopeKind.TASK
                            and owner.task_id != source.family.owner_scope.task_id)
                    ))
                ):
                    raise ValueError("Geometry copy does not preserve authorized source facts")
                bindings.append(ResolvedProductFamilyGeometryBindingInput.from_version_bundle(
                    binding_key=binding.binding_key,
                    expected_resource_kind=binding.expected_resource_kind,
                    geometry_purpose=binding.geometry_purpose,
                    geometry_version=copied.target_version,
                ))
            bindings = tuple(bindings)
            manifest = ProductFamilyStandardModelManifest.model_validate({
                **manifest.model_dump(mode="json", by_alias=True),
                "family_version_id": version_id,
                "binding_digest": product_geometry_binding_digest(
                    (item.binding_key, item.expected_resource_kind, item.geometry_content_digest)
                    for item in bindings
                ),
            })
            content_by_role[ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST] = (
                canonical_json(manifest.model_dump(mode="json", by_alias=True)).encode("utf-8")
            )
            assets_and_content = tuple(
                (
                    ProductFamilyVersionAssetRecord.model_validate({
                        **asset.model_dump(),
                        "family_version_id": version_id,
                        "size_bytes": len(content_by_role[asset.asset_role]),
                        "sha256": hashlib.sha256(content_by_role[asset.asset_role]).hexdigest(),
                        "created_at": self._now(),
                    }),
                    content_by_role[asset.asset_role],
                )
                for asset in source.assets
            )
            # Validate the complete recovery metadata before any Family write.
            plan = SystemFamilyCopyPlan(
                source_family_id=source.family.family_id,
                source_family_version_id=source.version.family_version_id,
                source_content_digest=source.version.content_digest,
                source_owner_scope=source.family.owner_scope,
                source_kind=source_kind,
                source_reference=source_reference,
                target_owner=command.target_owner,
                geometry_copies=geometry_copies,
                family_assets=tuple(item[0] for item in assets_and_content),
            )
            draft = ProductFamilyAuthoringVersionDraft(
                family_version_id=version_id,
                family_id=family_id,
                display_name=source.version.display_name,
                purpose=source.version.purpose,
                usage_instructions=source.version.usage_instructions,
                family_contract=source.version.family_contract,
                contract_digest=source.version.contract_digest,
                program_entry=source.version.program_entry,
                geometry_bindings=bindings,
                created_at=self._now(),
            )
        except ProductFamilyAssetIntegrityError as exc:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_STORAGE_INTEGRITY_FAILED", str(exc),
                phase="storage_verification",
            ) from exc
        except (ValidationError, ValueError, TypeError, GeometryAuthoringSourceError) as exc:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_SYSTEM_COPY_INVALID",
                "SYSTEM copy requires a successful current exact source, complete assets and its exact Geometry mapping",
            ) from exc
        source_artifact = await self._upload_source_artifact_with_retries(
            scope, operation_id=operation_id, mode="system_copy", content=source_content,
        )
        source_ref = _artifact_ref(source_artifact)
        downloaded = await self._read_exact_artifact(scope, source_ref)
        if downloaded.content != source_content or source_ref.sha256 != hashlib.sha256(source_content).hexdigest():
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_SOURCE_INTEGRITY_FAILED",
                "The saved SYSTEM copy source Artifact is not exact",
                phase="storage_verification",
            )
        family = ProductFamilyRecord(
            family_id=family_id, owner_scope=plan.target_owner,
            created_at=source_artifact.created_at,
        )
        assets = await self._store_assets_with_retries(family, assets_and_content)
        plan = SystemFamilyCopyPlan.model_validate({
            **plan.model_dump(), "family_assets": assets,
        })
        draft = draft.model_copy(update={"created_at": source_artifact.created_at})
        operation = CriticalOperationRecord(
            operation_id=operation_id,
            owner_id=scope.owner_id,
            tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id or "",
            run_id=scope.run_id,
            operation_kind=CriticalOperationKind.PRODUCT_FAMILY_WRITE,
            initiator=CriticalOperationInitiator.DIRECT_CLIENT,
            tool_name=_WRITE_TOOL,
            intent_summary="Copy one successful exact ProductFamily version to SYSTEM.",
            idempotency_key=f"system-family-publication:{command.request_id}",
            request_fingerprint=sha256_digest({
                "mode": "system_copy", "command": command.model_dump(mode="json"),
            }),
            status=CriticalOperationStatus.PREPARED,
            code_artifact=source_ref,
            detail=ProductFamilyWriteOperationDetail(
                mode="system_copy", version_draft=draft, system_copy=plan,
            ),
            target_product_family_id=family_id,
            started_at=source_artifact.created_at,
        )
        return await self._prepare_or_reconcile(scope, operation)

    async def claim_system_copy(
        self, scope: InvocationScope, operation: CriticalOperationRecord,
    ) -> AuthoringExecutionClaim:
        await self._repository.authorize_system_family_publication(scope)
        saved = await self._saved_system_copy(scope, operation)
        return await self._claim_execution(scope, saved)

    async def commit_system_copy(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        *,
        execution_claim: AuthoringExecutionClaim | None = None,
    ) -> ProductFamilyAuthoringCommitResult:
        """Commit saved copy facts without an authoring Runtime or a live source endpoint."""
        await self._repository.authorize_system_family_publication(scope)
        saved = await self._saved_system_copy(scope, operation)
        if saved.status is CriticalOperationStatus.SUCCEEDED:
            return await self._resolve_success(scope, saved)
        if saved.status is CriticalOperationStatus.FAILED:
            await self._raise_persisted_failure(scope, saved)
        if execution_claim is None:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RECONCILIATION_REQUIRED",
                "Prepared SYSTEM copy requires its existing execution claim",
                phase="reconcile", retryable=True,
            )
        if saved.status is CriticalOperationStatus.INDETERMINATE:
            saved = await self._reopen_indeterminate(scope, saved, execution_claim)
        detail = saved.detail
        plan = detail.system_copy
        draft = detail.version_draft
        try:
            succeeded = CriticalOperationRecord.model_validate(saved.model_copy(update={
                "status": CriticalOperationStatus.SUCCEEDED,
                "result_product_family_version_id": draft.family_version_id,
                "finished_at": self._now(),
            }))
            publication = ProductFamilyAuthoringPublication(
                operation=succeeded,
                family=ProductFamilyRecord(
                    family_id=draft.family_id, owner_scope=plan.target_owner,
                    created_at=draft.created_at,
                ),
                version=draft,
                geometry_bindings=draft.geometry_bindings,
                assets=plan.family_assets,
            )
            candidate = publication.to_bundle(version_no=1)
            await self._verify_family_assets_with_retries(candidate)
            source_asset = next(asset for asset in candidate.assets
                                if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE)
            source_content = await self._read_family_asset_with_retries(candidate, source_asset)
            exact_source = await self._read_exact_artifact(scope, saved.code_artifact)
            if exact_source.content != source_content or source_asset.sha256 != saved.code_artifact.sha256:
                raise ProductFamilyAssetIntegrityError("SYSTEM copy source and saved Artifact disagree")
            exact_geometry = await self._reauthorize_geometry_bindings(scope, draft.geometry_bindings)
            expected = {item.target_version.version.resource_version_id: item.target_version
                        for item in plan.geometry_copies}
            if any(bundle != expected[bundle.version.resource_version_id] for bundle in exact_geometry):
                raise ProductFamilyAuthoringApplicationError(
                    "PRODUCT_FAMILY_GEOMETRY_CHANGED",
                    "A saved SYSTEM Geometry copy changed before Family commit",
                )
        except ProductFamilyAssetIntegrityError as exc:
            await self._fail_prepared(scope, saved, ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_STORAGE_INTEGRITY_FAILED", str(exc), phase="storage_verification",
            ), execution_claim=execution_claim)
        except ProductFamilyAuthoringApplicationError as error:
            if error.retryable:
                await self._mark_indeterminate(scope, saved, error, execution_claim)
            await self._fail_prepared(scope, saved, error, execution_claim=execution_claim)
        except (ValidationError, TypeError, ValueError):
            await self._fail_prepared(scope, saved, ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RESULT_INTEGRITY_FAILED",
                "The saved SYSTEM copy facts cannot form an exact publication",
                phase="storage_verification",
            ), execution_claim=execution_claim)
        return await self._commit_with_reconcile(scope, saved, publication, execution_claim)

    async def _saved_system_copy(
        self, scope: InvocationScope, supplied: CriticalOperationRecord,
    ) -> CriticalOperationRecord:
        saved = await self._get_operation_state_with_retries(scope, supplied.operation_id)
        if saved is None or (
            saved.request_fingerprint != supplied.request_fingerprint
            or saved.detail != supplied.detail
            or saved.code_artifact != supplied.code_artifact
            or saved.target_product_family_id != supplied.target_product_family_id
            or not isinstance(saved.detail, ProductFamilyWriteOperationDetail)
            or saved.detail.mode != "system_copy"
            or saved.detail.system_copy is None
        ):
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_OPERATION_INTEGRITY_FAILED",
                "SYSTEM copy must use the original saved Operation and immutable plan",
                phase="reconcile", retryable=True,
            )
        return saved


    async def get_operation(
        self,
        scope: InvocationScope,
        *,
        operation_id: str,
    ) -> PublishedCriticalOperation:
        published = await self._repository.get_product_family_authoring_operation(
            scope,
            operation_id=operation_id,
        )
        if published is None:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_OPERATION_NOT_FOUND",
                "The terminal ProductFamily authoring Operation was not found",
            )
        return published


    async def _claim_execution(
        self, scope: InvocationScope, operation: CriticalOperationRecord,
    ) -> AuthoringExecutionClaim:
        state = await self._repository.get_product_family_authoring_execution_state(
            scope, operation.operation_id,
        )
        if state is None or state.operation.request_fingerprint != operation.request_fingerprint:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RECONCILIATION_REQUIRED",
                "The admitted ProductFamily Operation changed before execution",
                phase="reconcile", retryable=True,
            )
        if state.execution_owner is None:
            claim = await self._repository.claim_product_family_authoring_execution(
                scope, operation.operation_id,
            )
        else:
            claim = await self._repository.reclaim_product_family_authoring_execution(
                scope, operation.operation_id,
                expected_owner=state.execution_owner,
                expected_generation=state.execution_generation,
            )
        if claim is None:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RECONCILIATION_REQUIRED",
                "The original ProductFamily execution is active or its outcome is uncertain",
                phase="reconcile", retryable=True,
            )
        return claim

    async def _get_operation_state_with_retries(
        self,
        scope: InvocationScope,
        operation_id: str,
    ) -> CriticalOperationRecord | None:
        last_error: Exception | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                return await self._repository.get_product_family_authoring_operation_state(
                    scope,
                    operation_id=operation_id,
                )
            except ProductFamilyCatalogReferenceError as exc:
                raise ProductFamilyAuthoringApplicationError(
                    exc.code,
                    str(exc),
                    phase="preflight",
                    retryable=False,
                ) from exc
            except Exception as exc:
                last_error = exc
        raise ProductFamilyAuthoringApplicationError(
            "PRODUCT_FAMILY_SERVICE_UNAVAILABLE",
            "ProductFamily Operation state exhausted bounded database retries",
            phase="reconcile",
            retryable=True,
        ) from last_error


    async def _upload_source_artifact_with_retries(
        self,
        scope: InvocationScope,
        *,
        operation_id: str,
        mode: Literal["create", "repair_attempt", "revise_version", "system_copy"],
        content: bytes,
    ) -> ArtifactRecord:
        last_error: Exception | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                return await self._artifacts.upload_bytes(
                    owner_id=scope.owner_id,
                    task_id=scope.identity.task_id or "",
                    filename=_SOURCE_FILENAME,
                    content_type=_SOURCE_CONTENT_TYPE,
                    content=content,
                    idempotency_key=f"product-family-authoring-source:{operation_id}",
                    kind=_SOURCE_KIND,
                    run_id=scope.run_id,
                    version_metadata={
                        "mode": mode,
                        "program_entry": "main.py:realize",
                    },
                )
            except ConflictError:
                raise
            except Exception as exc:
                last_error = exc
        raise ProductFamilyAuthoringApplicationError(
            "PRODUCT_FAMILY_STORAGE_UNAVAILABLE",
            "ProductFamily source Artifact exhausted bounded publication retries",
            phase="reconcile",
            retryable=True,
        ) from last_error


    async def _reauthorize_geometry_bindings(
        self,
        scope: InvocationScope,
        bindings: Sequence[
            ResolvedProductFamilyGeometryBindingInput | ProductFamilyGeometryBindingRecord
        ],
    ) -> tuple[GeometryResourceVersionBundle, ...]:
        try:
            return await self._realization_inputs.reauthorize_geometry_bindings(
                scope,
                bindings,
            )
        except ProductRealizationInputError as exc:
            raise _authoring_input_error(exc) from exc


    async def _store_assets_with_retries(
        self,
        family: ProductFamilyRecord,
        assets_and_content: Sequence[tuple[ProductFamilyVersionAssetRecord, bytes]],
    ) -> tuple[ProductFamilyVersionAssetRecord, ...]:
        stored_assets: list[ProductFamilyVersionAssetRecord] = []
        for asset, content in assets_and_content:
            last_error: Exception | None = None
            for _ in range(_MAX_TECHNICAL_ATTEMPTS):
                try:
                    stored_assets.append(
                        await self._family_assets.write_exact_asset(family, asset, content)
                    )
                    break
                except ProductFamilyAssetIntegrityError:
                    raise
                except (StorageError, OSError) as exc:
                    last_error = exc
                except Exception as exc:
                    last_error = exc
            else:
                raise ProductFamilyAuthoringApplicationError(
                    "PRODUCT_FAMILY_STORAGE_UNAVAILABLE",
                    "ProductFamily asset storage exhausted bounded technical retries",
                    phase="storage_verification",
                    retryable=True,
                ) from last_error
        return tuple(stored_assets)

    async def _read_family_asset_with_retries(
        self,
        bundle: ProductFamilyVersionBundle,
        asset: ProductFamilyVersionAssetRecord,
    ) -> bytes:
        last_error: Exception | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                return await self._family_assets.read_exact_asset(bundle, asset)
            except ProductFamilyAssetIntegrityError:
                raise
            except (StorageError, OSError) as exc:
                last_error = exc
        raise ProductFamilyAuthoringApplicationError(
            "PRODUCT_FAMILY_STORAGE_UNAVAILABLE",
            "ProductFamily asset storage exhausted bounded read retries",
            phase="reconcile",
            retryable=True,
        ) from last_error

    async def _verify_family_assets_with_retries(
        self,
        bundle: ProductFamilyVersionBundle,
    ) -> None:
        last_error: Exception | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                await self._family_assets.verify_bundle(bundle)
                return
            except ProductFamilyAssetIntegrityError:
                raise
            except (StorageError, OSError) as exc:
                last_error = exc
        raise ProductFamilyAuthoringApplicationError(
            "PRODUCT_FAMILY_STORAGE_UNAVAILABLE",
            "ProductFamily asset storage exhausted bounded verification retries",
            phase="reconcile",
            retryable=True,
        ) from last_error

    async def _prepare_or_reconcile(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
    ) -> CriticalOperationRecord:
        last_error: Exception | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                return await self._repository.prepare_product_family_authoring_operation(
                    scope,
                    operation,
                )
            except ConflictError:
                raise
            except Exception as exc:
                last_error = exc
                try:
                    recovered = await self._repository.get_product_family_authoring_operation_state(
                        scope,
                        operation_id=operation.operation_id,
                    )
                except Exception as reconcile_error:
                    last_error = reconcile_error
                    continue
                if recovered is not None:
                    return recovered
        raise ProductFamilyAuthoringApplicationError(
            "PRODUCT_FAMILY_SERVICE_UNAVAILABLE",
            "The source was saved but its ProductFamily Operation could not be prepared",
            phase="reconcile",
            retryable=True,
        ) from last_error

    async def _commit_with_reconcile(
        self,
        scope: InvocationScope,
        prepared: CriticalOperationRecord,
        publication: ProductFamilyAuthoringPublication,
        execution_claim: AuthoringExecutionClaim,
    ) -> ProductFamilyAuthoringCommitResult:
        last_error: Exception | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                committed = await self._repository.commit_product_family_authoring_success(
                    scope,
                    publication,
                    execution_claim=execution_claim,
                )
                return await self._verify_commit(scope, committed)
            except ProductFamilyAuthoringCommitFenceError as exc:
                raise ProductFamilyAuthoringApplicationError(
                    "PRODUCT_FAMILY_RECONCILIATION_REQUIRED",
                    "The ProductFamily commit failed its ownership or database integrity checks",
                    phase="reconcile", retryable=True,
                ) from exc
            except Exception as exc:
                last_error = exc
                try:
                    recovered = await self._repository.get_product_family_authoring_operation_state(
                        scope,
                        operation_id=prepared.operation_id,
                    )
                except Exception as reconcile_error:
                    last_error = reconcile_error
                    continue
                if recovered is not None:
                    if recovered.status is CriticalOperationStatus.SUCCEEDED:
                        try:
                            return await self._resolve_success(scope, recovered)
                        except ProductFamilyAuthoringApplicationError as resolve_error:
                            if not resolve_error.retryable:
                                raise
                            last_error = resolve_error
                            continue
                        except Exception as resolve_error:
                            last_error = resolve_error
                            continue
                    if recovered.status is CriticalOperationStatus.FAILED:
                        await self._raise_persisted_failure(scope, recovered)
                    if recovered.status is CriticalOperationStatus.INDETERMINATE:
                        raise ProductFamilyAuthoringApplicationError(
                            "PRODUCT_FAMILY_RECONCILIATION_REQUIRED",
                            "The ProductFamily commit is indeterminate",
                            phase="reconcile",
                            retryable=True,
                        ) from exc
        error = ProductFamilyAuthoringApplicationError(
            "PRODUCT_FAMILY_COMMIT_UNCERTAIN",
            "The ProductFamily commit exhausted bounded reconciliation",
            phase="reconcile",
            retryable=True,
        )
        await self._mark_indeterminate(scope, prepared, error, execution_claim)
        raise error from last_error

    async def _verify_commit(
        self,
        scope: InvocationScope,
        committed: ProductFamilyAuthoringCommitResult,
    ) -> ProductFamilyAuthoringCommitResult:
        operation = committed.operation.operation
        bundle = committed.family_version
        if (
            operation.status is not CriticalOperationStatus.SUCCEEDED
            or operation.target_product_family_id != bundle.family.family_id
            or operation.result_product_family_version_id != bundle.version.family_version_id
        ):
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RESULT_INTEGRITY_FAILED",
                "The committed ProductFamily Operation and exact version disagree",
                phase="reconcile",
                retryable=True,
            )
        exact = await self._read_family_version_by_id(
            scope,
            bundle.version.family_version_id,
        )
        if exact != bundle:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RESULT_INTEGRITY_FAILED",
                "The committed ProductFamily version failed exact database readback",
                phase="reconcile",
                retryable=True,
            )
        await self._verify_family_assets_with_retries(exact)
        source_asset = next(
            asset
            for asset in exact.assets
            if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE
        )
        version_source = await self._read_family_asset_with_retries(exact, source_asset)
        if operation.code_artifact is None:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RESULT_INTEGRITY_FAILED",
                "The successful ProductFamily Operation lost its code Artifact",
                phase="reconcile",
                retryable=True,
            )
        operation_source = await self._read_exact_artifact(scope, operation.code_artifact)
        if (
            operation_source.content != version_source
            or source_asset.sha256 != operation.code_artifact.sha256
        ):
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RESULT_INTEGRITY_FAILED",
                "The Operation source and ProductFamilyVersion source are not exact",
                phase="reconcile",
                retryable=True,
            )
        return committed

    async def _resolve_success(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
    ) -> ProductFamilyAuthoringCommitResult:
        published = await self._repository.get_product_family_authoring_operation(
            scope,
            operation_id=operation.operation_id,
        )
        version_id = operation.result_product_family_version_id
        if published is None or version_id is None:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RESULT_INTEGRITY_FAILED",
                "The successful ProductFamily Operation lost its exact result binding",
                phase="reconcile",
                retryable=True,
            )
        bundle = await self._read_family_version_by_id(scope, version_id)
        return await self._verify_commit(
            scope,
            ProductFamilyAuthoringCommitResult(
                operation=published,
                family_version=bundle,
            ),
        )

    async def _reopen_indeterminate(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
        execution_claim: AuthoringExecutionClaim,
    ) -> CriticalOperationRecord:
        detail = operation.detail
        if not isinstance(detail, ProductFamilyWriteOperationDetail):
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RECONCILIATION_REQUIRED",
                "The indeterminate Operation lost its exact version draft",
                phase="reconcile",
                retryable=True,
            )
        try:
            await self._read_family_version_by_id(scope, detail.version_draft.family_version_id)
        except ProductFamilyAuthoringApplicationError as exc:
            if exc.code != "PRODUCT_FAMILY_VERSION_NOT_FOUND":
                raise
        else:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RECONCILIATION_REQUIRED",
                "The indeterminate Operation has contradictory ready-version evidence",
                phase="reconcile",
                retryable=True,
            )
        prepared = CriticalOperationRecord.model_validate(
            operation.model_copy(
                update={
                    "status": CriticalOperationStatus.PREPARED,
                    "error_code": None,
                    "error_message": None,
                    "error_phase": None,
                    "finished_at": None,
                }
            )
        )
        reopened = await self._repository.reopen_product_family_authoring_operation(
            scope,
            prepared,
            execution_claim=execution_claim,
        )
        if reopened.status is not CriticalOperationStatus.PREPARED:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RECONCILIATION_REQUIRED",
                "The indeterminate ProductFamily Operation could not be safely reopened",
                phase="reconcile",
                retryable=True,
            )
        return reopened

    async def _fail_prepared(
        self,
        scope: InvocationScope,
        prepared: CriticalOperationRecord,
        error: ProductFamilyAuthoringApplicationError,
        *, execution_claim: AuthoringExecutionClaim,
    ) -> NoReturn:
        terminal = CriticalOperationRecord.model_validate(
            prepared.model_copy(
                update={
                    "status": CriticalOperationStatus.FAILED,
                    "error_code": error.code,
                    "error_message": _persisted_error_message(
                        CriticalOperationStatus.FAILED
                    ),
                    "error_phase": _persisted_phase(error.phase),
                    "finished_at": self._now(),
                }
            )
        )
        last_error: Exception | None = None
        published: PublishedCriticalOperation | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                published = await self._repository.complete_product_family_authoring_operation(
                    scope,
                    terminal,
                    execution_claim=execution_claim,
                )
                break
            except Exception as exc:
                last_error = exc
                try:
                    published = await self._repository.get_product_family_authoring_operation(
                        scope,
                        operation_id=prepared.operation_id,
                    )
                except Exception:
                    published = None
                if published is not None:
                    break
        if published is None:
            await self._mark_indeterminate(
                scope,
                prepared,
                ProductFamilyAuthoringApplicationError(
                    "PRODUCT_FAMILY_FAILURE_PUBLICATION_UNCERTAIN",
                    "The failed ProductFamily Operation could not be reconciled",
                    phase="reconcile",
                    retryable=True,
                ),
                execution_claim,
            )
            raise AssertionError("unreachable") from last_error
        if published.operation.status is CriticalOperationStatus.SUCCEEDED:
            raise ProductFamilyAuthoringApplicationError(
                "PRODUCT_FAMILY_RESULT_RECONCILIATION_REQUIRED",
                "The ProductFamily Operation succeeded while failure publication lost its response",
                phase="reconcile",
                retryable=True,
                operation_alias=published.catalog_entry.alias,
            )
        raise ProductFamilyAuthoringApplicationError(
            error.code,
            str(error),
            phase=error.phase,
            retryable=error.retryable,
            operation_alias=published.catalog_entry.alias,
            diagnostic=error.diagnostic,
        )

    async def _mark_indeterminate(
        self,
        scope: InvocationScope,
        prepared: CriticalOperationRecord,
        error: ProductFamilyAuthoringApplicationError,
        execution_claim: AuthoringExecutionClaim,
    ) -> NoReturn:
        terminal = CriticalOperationRecord.model_validate(
            prepared.model_copy(
                update={
                    "status": CriticalOperationStatus.INDETERMINATE,
                    "error_code": error.code,
                    "error_message": _persisted_error_message(
                        CriticalOperationStatus.INDETERMINATE
                    ),
                    "error_phase": CriticalOperationErrorPhase.RECONCILE,
                    "finished_at": self._now(),
                }
            )
        )
        last_error: Exception | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                recorded = await self._repository.mark_product_family_authoring_indeterminate(
                    scope,
                    terminal,
                    execution_claim=execution_claim,
                )
                if recorded.status is CriticalOperationStatus.INDETERMINATE:
                    raise await self._with_published_operation_alias(
                        scope,
                        prepared.operation_id,
                        error,
                    )
            except ProductFamilyAuthoringApplicationError:
                raise
            except Exception as exc:
                last_error = exc
                try:
                    recorded = await self._repository.get_product_family_authoring_operation_state(
                        scope,
                        operation_id=prepared.operation_id,
                    )
                except Exception:
                    recorded = None
                if (
                    recorded is not None
                    and recorded.status is CriticalOperationStatus.INDETERMINATE
                ):
                    raise await self._with_published_operation_alias(
                        scope,
                        prepared.operation_id,
                        error,
                    ) from exc
        uncertain = ProductFamilyAuthoringApplicationError(
            "PRODUCT_FAMILY_RECONCILIATION_UNCERTAIN",
            "The ProductFamily Operation could not confirm its system pause",
            phase="reconcile",
            retryable=True,
        )
        raise await self._with_published_operation_alias(
            scope,
            prepared.operation_id,
            uncertain,
        ) from last_error

    async def _with_published_operation_alias(
        self,
        scope: InvocationScope,
        operation_id: str,
        error: ProductFamilyAuthoringApplicationError,
    ) -> ProductFamilyAuthoringApplicationError:
        """Attach only an already-published safe alias; INDETERMINATE has none."""

        try:
            published = await self._repository.get_product_family_authoring_operation(
                scope,
                operation_id=operation_id,
            )
        except Exception:
            published = None
        if published is None:
            return error
        return ProductFamilyAuthoringApplicationError(
            error.code,
            str(error),
            phase=error.phase,
            retryable=error.retryable,
            operation_alias=published.catalog_entry.alias,
            diagnostic=error.diagnostic,
        )

    async def _raise_persisted_failure(
        self,
        scope: InvocationScope,
        operation: CriticalOperationRecord,
    ) -> NoReturn:
        published = await self._repository.get_product_family_authoring_operation(
            scope,
            operation_id=operation.operation_id,
        )
        raise ProductFamilyAuthoringApplicationError(
            operation.error_code or "PRODUCT_FAMILY_AUTHORING_FAILED",
            operation.error_message or "The ProductFamily authoring Operation failed",
            phase=_application_phase(operation.error_phase),
            operation_alias=(published.catalog_entry.alias if published is not None else None),
        )

    async def _read_exact_artifact(
        self,
        scope: InvocationScope,
        reference: ArtifactVersionRef,
    ) -> ArtifactDownload:
        last_error: ExactArtifactReadError | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                return await self._exact_artifacts.read(
                    scope,
                    reference,
                    expected_content_type=_SOURCE_CONTENT_TYPE,
                )
            except ExactArtifactReadError as exc:
                if "failed integrity validation" in str(exc):
                    raise ProductFamilyAuthoringApplicationError(
                        "PRODUCT_FAMILY_SOURCE_INTEGRITY_FAILED",
                        "The exact ProductFamily source Artifact failed integrity validation",
                        phase="storage_verification",
                    ) from exc
                last_error = exc
        raise ProductFamilyAuthoringApplicationError(
            "PRODUCT_FAMILY_STORAGE_UNAVAILABLE",
            "The exact ProductFamily source Artifact exhausted bounded read retries",
            phase="reconcile",
            retryable=True,
        ) from last_error


    async def _read_family_version_by_id(
        self,
        scope: InvocationScope,
        family_version_id: str,
    ) -> ProductFamilyVersionBundle:
        last_error: Exception | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                reference = await self._family_catalog.version_ref_for_visible_id(
                    scope.identity,
                    family_version_id,
                )
                return await self._family_catalog.resolve_exact_version(
                    scope.identity,
                    reference,
                )
            except ProductFamilyCatalogNotFoundError as exc:
                raise ProductFamilyAuthoringApplicationError(
                    "PRODUCT_FAMILY_VERSION_NOT_FOUND",
                    "The exact ProductFamily version is not visible",
                ) from exc
            except ProductFamilyCatalogIntegrityError as exc:
                raise ProductFamilyAuthoringApplicationError(
                    "PRODUCT_FAMILY_RESULT_INTEGRITY_FAILED",
                    str(exc),
                    phase="reconcile",
                ) from exc
            except Exception as exc:
                last_error = exc
        raise ProductFamilyAuthoringApplicationError(
            "PRODUCT_FAMILY_SERVICE_UNAVAILABLE",
            "Exact ProductFamily readback exhausted bounded database or storage retries",
            phase="reconcile",
            retryable=True,
        ) from last_error


    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("ProductFamily authoring clock must return an aware datetime")
        return value


def _artifact_ref(record) -> ArtifactVersionRef:
    return ArtifactVersionRef(
        artifact_id=record.artifact_id,
        version_id=record.version_id,
        sha256=record.sha256,
    )


def _version_assets(
    *,
    family_version_id: str,
    source_bundle: bytes,
    draft: ProductFamilyAuthoringVersionDraft,
    standard_execution: ProductRealizationGeometryExecution | None,
    created_at: datetime,
) -> tuple[tuple[ProductFamilyVersionAssetRecord, bytes], ...]:
    specs = (
        (
            ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE,
            "source_bundle_v1",
            "realization_source.zip",
            _SOURCE_CONTENT_TYPE,
            source_bundle,
        ),
    )
    if draft.family_contract.standard_model is not None:
        if standard_execution is None or standard_execution.geometry is None:
            raise ValueError("standard model publication requires actual canonical geometry")
        geometry = standard_execution.geometry
        manifest = ProductFamilyStandardModelManifest(
            family_version_id=family_version_id,
            contract_digest=draft.contract_digest,
            source_bundle_sha256=hashlib.sha256(source_bundle).hexdigest(),
            binding_digest=standard_execution.realization.binding_digest,
            geometry_sha256=geometry.sha256,
            geometry_summary=geometry.summary,
        )
        specs += (
            (ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY, "occt_brep_v1",
             "standard_model.brep", "application/vnd.opencascade.brep", geometry.payload),
            (ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST, "json",
             "standard_model.json", "application/json",
             canonical_json(manifest.model_dump(mode="json", by_alias=True)).encode("utf-8")),
        )
    return tuple(
        (
            ProductFamilyVersionAssetRecord(
                family_version_id=family_version_id,
                asset_role=role,
                asset_format=asset_format,
                filename=filename,
                content_type=content_type,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                created_at=created_at,
            ),
            content,
        )
        for role, asset_format, filename, content_type, content in specs
    )


def _persisted_phase(phase: str) -> CriticalOperationErrorPhase:
    return {
        "execution": CriticalOperationErrorPhase.EXECUTION,
        "output_validation": CriticalOperationErrorPhase.OUTPUT_VALIDATION,
        "storage_verification": CriticalOperationErrorPhase.STORAGE_VERIFICATION,
        "commit_fence": CriticalOperationErrorPhase.COMMIT_FENCE,
    }.get(phase, CriticalOperationErrorPhase.CODE_VALIDATION)


def _application_phase(
    phase: CriticalOperationErrorPhase | None,
) -> Literal[
    "code_validation",
    "execution",
    "output_validation",
    "storage_verification",
    "commit_fence",
]:
    return {
        CriticalOperationErrorPhase.EXECUTION: "execution",
        CriticalOperationErrorPhase.OUTPUT_VALIDATION: "output_validation",
        CriticalOperationErrorPhase.STORAGE_VERIFICATION: "storage_verification",
        CriticalOperationErrorPhase.COMMIT_FENCE: "commit_fence",
    }.get(phase, "code_validation")


def _draft_input_validation_error(error: ValidationError, *, field: str = "", declaration=None,
    fixed_input_rejection: bool = False) -> ProductFamilyAuthoringApplicationError:
    issues = error.errors(include_url=False, include_input=False)
    issue = issues[0]
    location = issue["loc"]
    if isinstance(declaration, dict):
        branch = "FamilyDraftUpdateCommand" if declaration.get("mode") == "update" else (
            "FamilyDraftPublishedCreateCommand" if isinstance(declaration.get("source"), dict)
            and declaration["source"].get("source_kind") == "published" else "FamilyDraftInlineCreateCommand")
        selected = next((item for item in issues if item["loc"] and (
            item["loc"][0] == branch or str(item["loc"][0]).endswith(f", {branch}]"))), None)
        if selected is not None:
            issue, location = selected, selected["loc"][1:]
    path = ".".join(str(part) for part in location)
    path = ".".join(part for part in (field, path) if part)
    message = issue["msg"]
    if "usage_instructions" in path:
        message += "; provide usage_instructions with action=set to supply valid usage"
    return ProductFamilyAuthoringApplicationError(
        "PRODUCT_FAMILY_INPUT_INVALID", f"{path or 'request'}: {message}",
        diagnostic={"field": path or "request"}, fixed_input_rejection=declaration is not None or fixed_input_rejection)


def _validate_draft_content(values) -> FamilyDraftContent:
    try:
        return FamilyDraftContent.model_validate(values)
    except ValidationError as exc:
        raise _draft_input_validation_error(exc) from None


def _authoring_input_error(
    error: ProductRealizationInputError,
) -> ProductFamilyAuthoringApplicationError:
    code = {
        "GEOMETRY_SERVICE_UNAVAILABLE": "PRODUCT_FAMILY_SERVICE_UNAVAILABLE",
        "GEOMETRY_NOT_FOUND": "PRODUCT_FAMILY_GEOMETRY_NOT_FOUND",
        "GEOMETRY_CHANGED": "PRODUCT_FAMILY_GEOMETRY_CHANGED",
        "STORAGE_UNAVAILABLE": "PRODUCT_FAMILY_STORAGE_UNAVAILABLE",
        "PUBLISHED_SOURCE_INTEGRITY_FAILED": ("PRODUCT_FAMILY_STORAGE_INTEGRITY_FAILED"),
        "INPUT_INVALID": "PRODUCT_FAMILY_OUTPUT_INVALID",
    }.get(error.code, "PRODUCT_FAMILY_OUTPUT_INVALID")
    return ProductFamilyAuthoringApplicationError(
        code,
        str(error),
        phase=error.phase,
        retryable=error.retryable,
    )


def _require_task_scope(scope: InvocationScope) -> None:
    if not scope.identity.task_id:
        raise ProductFamilyAuthoringApplicationError(
            "PRODUCT_FAMILY_TASK_REQUIRED",
            "ProductFamily authoring requires a TASK-scoped invocation",
        )


def _bounded_message(message: str) -> str:
    normalized = message.strip() or "ProductFamily authoring failed"
    encoded = normalized.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_DIAGNOSTIC_BYTES:
        return normalized
    suffix = "... [truncated]"
    budget = _MAX_DIAGNOSTIC_BYTES - len(suffix.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix


def _persisted_error_message(status: CriticalOperationStatus) -> str:
    if status is CriticalOperationStatus.INDETERMINATE:
        return "ProductFamily authoring outcome requires reconciliation"
    return "ProductFamily authoring did not complete successfully"


__all__ = ["ProductFamilyAuthoringApplicationError", "ProductFamilyAuthoringService"]
