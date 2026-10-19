"""Editable Task Geometry content and exact read/Preview inputs."""

from __future__ import annotations


import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftService, validate_draft_source
from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftRecord, AuthoringDraftCommitResult
from CAD2BIM_backend.domain.authoring_draft_content import GeometryDraftContent, GeometryPublishedBase
from CAD2BIM_backend.domain.geometry_authoring import (
    GeometryDraftWriteCommand, GeometryAuthoringSourceFile,
    GeometryParameterSchemaEditError, resolve_geometry_parameter_schema,
)
from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
from CAD2BIM_backend.application.text_source_view import TextSourceViewPolicy, TextSourceViewService
from CAD2BIM_backend.domain.canonical import canonical_json, stable_id
from CAD2BIM_backend.domain.data_runtime import PublishedCriticalOperation
from CAD2BIM_backend.domain.geometry_authoring import GeometryAuthoringInputMetadata, GeometryAuthoringSourceReadResult
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole, GeometryResourceInput, GeometryResourceKind, GeometryResourceRecord, GeometryResourceScope, GeometryResourceScopeKind
from CAD2BIM_backend.domain.geometry_runtime import (
    GeometryBuildFixedRequest, MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS,
    MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES, MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.text_source import TextSourceView
from CAD2BIM_backend.domain.version_usage import (
    MAX_VERSION_USAGE_INSTRUCTIONS_UTF8_BYTES, validate_version_usage_instructions,
)
from CAD2BIM_backend.ports.geometry_authoring import GeometryAuthoringRepository, GeometryAuthoringSourceError, GeometryAuthoringSourcePort
from CAD2BIM_backend.ports.source_patch import SourcePatchPort, SourcePatchPortError


_MAX_DIAGNOSTIC_BYTES = 2_000


class GeometryAuthoringApplicationError(RuntimeError):
    """Bounded authoring failure suitable for deterministic entry projection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: Literal[
            "read",
            "preflight",
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


class GeometryAuthoringService:
    """Save editable Geometry content and resolve exact read/Preview inputs."""

    def __init__(
        self,
        repository: GeometryAuthoringRepository,
        resources: GeometryResourceService,
        source_bundles: GeometryAuthoringSourcePort,
        *,
        source_patch: SourcePatchPort,
        clock: Callable[[], datetime] | None = None,
        text_views: TextSourceViewService | None = None,
        drafts: AuthoringDraftService | None = None,
        catalog=None,
    ) -> None:
        self._repository = repository
        self._drafts = drafts
        self._catalog = catalog
        self._resources = resources
        self._source_bundles = source_bundles
        self._source_patch = source_patch
        self._clock = clock or (lambda: datetime.now(UTC))
        self._text_views = text_views or TextSourceViewService(
            TextSourceViewPolicy(
                maximum_source_bytes=source_bundles.maximum_text_member_bytes,
                maximum_full_bytes=64 * 1024,
                maximum_projected_bytes=96 * 1024,
            )
        )

    def draft_reference(self, scope, draft):
        return self._drafts.reference(scope, draft)

    async def request_rejection_status(self, scope, *, request_identity, error) -> Literal["not_started", "unknown"]:
        definite = self._drafts.fixed_input_rejection(error) or (
            isinstance(error, GeometryAuthoringApplicationError) and (
                error.fixed_input_rejection or error.code in {"GEOMETRY_DRAFT_CONFLICT", "DRAFT_UNCHANGED"}
            )
        )
        return await self._drafts.request_rejection_status(scope, tool_name="ifc_geometry_resource_write",
            request_identity=request_identity, definite_rejection=definite)

    async def operation_result(self, scope, published):
        from CAD2BIM_backend.domain.data_runtime import AuthoringDraftWriteOperationDetail, CriticalOperationKind
        if isinstance(published.operation.detail, AuthoringDraftWriteOperationDetail):
            return self._drafts.operation_result(scope, published)
        if published.operation.operation_kind is CriticalOperationKind.AUTHORING_PUBLISH:
            return await self._publication.operation_result(scope, published)
        return None

    async def publish_draft(self, scope, **request):
        return await self._publication.publish(scope, root_kind="geometry", **request)

    async def read_draft(self, scope, *, draft_ref):
        root_id = self._drafts.resolve_reference(scope, draft_ref, root_kind="geometry")
        draft = await self._repository.get_geometry_draft(scope, resource_id=root_id)
        if draft is None:
            raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_NOT_AVAILABLE", "No editable Geometry draft is visible")
        return draft, await self._drafts.read(scope, draft, GeometryDraftContent)

    async def draft_info(self, scope, *, draft_ref):
        from CAD2BIM_backend.domain.authoring_draft_content import GeometryDraftInfo
        from CAD2BIM_backend.domain.geometry_authoring import GeometryDraftInputBinding
        draft, content = await self.read_draft(scope, draft_ref=draft_ref)
        inputs = []
        for item in content.input_metadata.geometry_inputs:
            bundle = await self._resources.get_exact_version(scope.identity, resource_id=item.resource_id,
                resource_version_id=item.resource_version_id)
            if bundle is None:
                raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_UNAVAILABLE", "A draft input is no longer visible")
            inputs.append(GeometryDraftInputBinding(input_key=item.input_key,
                resource_version_alias=self._catalog.version_ref(scope.identity, bundle)))
        return GeometryDraftInfo(draft_ref=draft_ref, edit_token=draft.generation,
            **await self._drafts.save_navigation(scope, draft),
            display_name=content.display_name, usage_instructions=content.usage_instructions,
            resource_kind=content.input_metadata.resource_kind.value,
            parameter_schema_json=canonical_json(content.input_metadata.parameter_schema) if content.input_metadata.parameter_schema is not None else None,
            geometry_inputs=tuple(inputs), interfaces=content.interfaces,
            source_files=tuple(item.path for item in content.source_files))

    async def publication_navigation(self, scope, *, root_kind, version_id):
        return await self._drafts.publication_navigation(scope, root_kind=root_kind, version_id=version_id)

    async def save_draft_from_aliases(self, scope, *, declaration, request_identity):
        try:
            command = TypeAdapter(GeometryDraftWriteCommand).validate_python(declaration)
        except ValidationError as exc:
            raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID", "The Geometry draft request is invalid",
                diagnostic={"errors": [{"field": ".".join(map(str, item["loc"])), "reason": item["msg"]}
                    for item in exc.errors(include_input=False)][:8]}, fixed_input_rejection=True) from None
        raw = command.model_dump(mode="json", exclude_unset=True, by_alias=True)
        operation_id, fingerprint, replay = await self._drafts.replay(
            scope, root_kind="geometry", declaration=raw, request_identity=request_identity)
        owner_scope = await self._drafts.resource_owner(scope)
        if replay is not None:
            detail = replay.operation.detail
            return AuthoringDraftCommitResult(operation=replay, draft=AuthoringDraftRecord(
                root_kind="geometry", root_id=detail.root_id, owner_scope=owner_scope,
                generation=detail.result_generation, snapshot=replay.operation.code_artifact))
        expected = None
        root_id = stable_id("geometry_resource", {"task_id": scope.identity.task_id, "operation_id": operation_id})
        if command.mode == "update":
            previous, content = await self.read_draft(scope, draft_ref=command.draft_ref)
            await self._drafts.require_write_owner(scope, previous.owner_scope)
            owner_scope = previous.owner_scope
            previous_content = content
            if previous.generation != command.expected_edit_token:
                raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_CONFLICT", "The draft changed; read it before editing")
            root_id, expected = previous.root_id, previous.generation
            values = content.model_dump(mode="python")
            if "interfaces" in command.model_fields_set and content.input_metadata.resource_kind is GeometryResourceKind.MODELING_FUNCTION:
                raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID",
                    "Function interfaces are emitted by interfaces.py evaluate(ctx); omit interfaces from Write",
                    diagnostic={"field": "interfaces"}, fixed_input_rejection=True)
            for key in ("display_name", "interfaces"):
                if key in command.model_fields_set:
                    values[key] = getattr(command, key)
            if "usage_instructions" in command.model_fields_set:
                values["usage_instructions"] = self._draft_usage(content.usage_instructions, command.usage_instructions)
            metadata = content.input_metadata.model_dump(mode="python")
            if "geometry_inputs" in command.model_fields_set:
                metadata["geometry_inputs"] = await self._draft_geometry_inputs(scope, command.geometry_inputs)
            if "parameter_schema" in command.model_fields_set:
                if content.input_metadata.resource_kind is not GeometryResourceKind.MODELING_FUNCTION:
                    raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID",
                        "Only modeling_function drafts accept parameter_schema",
                        diagnostic={"field": "parameter_schema"}, fixed_input_rejection=True)
                try:
                    metadata["parameter_schema"] = resolve_geometry_parameter_schema(
                        content.input_metadata.parameter_schema, command.parameter_schema)
                except ValueError as exc:
                    diagnostic = {"field": "parameter_schema", "reason": str(exc)}
                    if isinstance(exc, GeometryParameterSchemaEditError):
                        diagnostic["parameter_key"] = exc.parameter_key
                    raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID", str(exc),
                        diagnostic=diagnostic, fixed_input_rejection=True) from exc
            values["input_metadata"] = metadata
            if "source_changes" in command.model_fields_set:
                try:
                    original = self._source_bundles.pack({f.path: f.source.encode("utf-8") for f in content.source_files})
                    changed = self._source_bundles.mutate(original.content, original.sha256, command.source_changes,
                        maximum_members=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS,
                        maximum_member_bytes=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES,
                        maximum_total_bytes=MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES)
                    values["source_files"] = self._draft_source_files(changed.content)
                except GeometryAuthoringSourceError as exc:
                    raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID", str(exc),
                        diagnostic={"field": "source_changes", "failure": exc.failure.value}, fixed_input_rejection=True) from exc
            content = self._draft_content(values)
            if content == previous_content:
                raise GeometryAuthoringApplicationError("DRAFT_UNCHANGED", "The draft content is unchanged")
        elif command.source.source_kind == "published":
            bundle = await self._catalog.resolve_exact_version(scope.identity, command.source.resource_version_alias)
            content = await self._draft_from_published(scope, bundle,
                display_name=command.display_name, usage_instructions=command.usage_instructions)
            if bundle.resource.owner_scope == owner_scope:
                root_id = bundle.resource.resource_id
        else:
            source = command.source
            schema_change = getattr(source, "parameter_schema", None)
            content = self._draft_content(dict(display_name=command.display_name,
                usage_instructions=self._draft_usage(None, command.usage_instructions), source_files=source.source_files,
                input_metadata=GeometryAuthoringInputMetadata(
                    resource_kind=GeometryResourceKind(source.source_kind),
                    geometry_inputs=await self._draft_geometry_inputs(scope, source.geometry_inputs),
                    parameter_schema=schema_change.value if schema_change else None),
                interfaces=getattr(source, "interfaces", ())))
        if content.source_files:
            members = {f.path: f.source.encode("utf-8") for f in content.source_files}
            packed_source = self._source_bundles.pack(members)
            validate_draft_source(members, entry_functions=("build",))
            if content.input_metadata.resource_kind is GeometryResourceKind.MODELING_FUNCTION:
                try:
                    self._source_bundles.validate_function_source(packed_source.content)
                except GeometryAuthoringSourceError as exc:
                    raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID", str(exc),
                        diagnostic={"field": "source_files", "failure": exc.failure.value}) from exc
        await self._require_draft_dependencies(scope, owner_scope, content)
        resource = GeometryResourceRecord(resource_id=root_id,
            resource_kind=content.input_metadata.resource_kind, owner_scope=owner_scope,
            display_name=content.display_name, created_at=self._now())
        existing = await self._resources.get_resource(scope.identity, resource_id=root_id)
        if existing is not None:
            resource = existing
        snapshot, operation = await self._drafts.save(scope, root_kind="geometry", root_id=root_id,
            content=content, expected_generation=expected, operation_id=operation_id,
            fingerprint=fingerprint, request_identity=request_identity)
        return await self._repository.commit_geometry_draft(scope, resource=resource,
            snapshot=snapshot, expected_generation=expected, operation=operation)

    def _draft_content(self, values):
        try:
            return GeometryDraftContent.model_validate(values)
        except ValidationError as exc:
            raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID",
                "The complete Geometry draft is invalid",
                diagnostic={"errors": [{"field": ".".join(map(str, error["loc"])),
                    "reason": error["msg"]} for error in exc.errors(include_input=False)][:8]}) from exc

    def _draft_usage(self, current, change):
        try:
            if change is not None and change.action == "set":
                value = change.value
            else:
                validate_version_usage_instructions(current, required=True)
                value = current if change is None else self._source_patch.apply_text(current, change.edits,
                    maximum_source_bytes=MAX_VERSION_USAGE_INSTRUCTIONS_UTF8_BYTES)
            return validate_version_usage_instructions(value, required=True)
        except SourcePatchPortError as exc:
            raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID", str(exc),
                diagnostic={"field": "usage_instructions", "failure": exc.failure,
                    "edit_index": exc.edit_index}, fixed_input_rejection=True) from exc
        except ValueError as exc:
            raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID",
                f"usage_instructions: {exc}; provide action=set with valid complete usage",
                diagnostic={"field": "usage_instructions", "reason": str(exc)}, fixed_input_rejection=True) from exc

    def _draft_source_files(self, source_bundle):
        inspected = self._source_bundles.inspect(source_bundle)
        return tuple(GeometryAuthoringSourceFile(path=member.source_path, source=inspected.read_text(member.source_path))
                     for member in inspected.members if member.source_path.endswith(".py"))

    async def prepare_draft_preview(self, scope, *, draft_ref, parameters=()):
        from CAD2BIM_backend.domain.preview_cases import PreviewSourceSnapshot
        draft, content = await self.read_draft(scope, draft_ref=draft_ref)
        prepared = await self.prepare_draft_content(scope, draft=draft, content=content)
        source = PreviewSourceSnapshot(source_kind="geometry_sample", source_type="draft",
            source_id=draft.root_id, source_ref=draft_ref, snapshot_id=draft.snapshot.version_id,
            content_digest=content.execution_digest, title=content.display_name, draft_artifact=draft.snapshot)
        dependencies = []
        for item in content.input_metadata.geometry_inputs:
            bundle = await self._resources.get_exact_version(scope.identity, resource_id=item.resource_id,
                resource_version_id=item.resource_version_id)
            if bundle is None or bundle.version.content_digest != item.content_digest:
                raise GeometryAuthoringApplicationError("GEOMETRY_INPUT_CHANGED", "Exact Geometry dependency is not available")
            reference = self._catalog._reference(scope.identity, reference_kind="resource_version",
                resource_id=item.resource_id, resource_version_id=item.resource_version_id)
            dependencies.append(PreviewSourceSnapshot(source_kind="geometry_sample", source_type="published",
                source_id=item.resource_id, source_ref=reference, snapshot_id=item.resource_version_id,
                content_digest=item.content_digest, title=bundle.resource.display_name))
        return {**prepared, "source": source, "dependencies": tuple(dependencies)}

    async def prepare_draft_content(self, scope, *, draft, content):
        await self._require_draft_dependencies(scope, draft.owner_scope, content)
        """Prepare exact frozen Geometry bytes for either Preview or publication."""
        from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, CanonicalGeometryFormat, GeometrySummary
        if content.source_files:
            packed = self._source_bundles.pack({f.path: f.source.encode("utf-8") for f in content.source_files})
            if content.input_metadata.resource_kind is GeometryResourceKind.MODELING_FUNCTION:
                self._source_bundles.validate_function_source(packed.content)
            is_function = content.input_metadata.resource_kind is GeometryResourceKind.MODELING_FUNCTION
            request = GeometryBuildFixedRequest(source_bundle=packed.content,
                geometry_inputs=() if is_function else await self._resources.load_build_inputs(scope.identity, content.input_metadata.geometry_inputs))
            async def load_materials(source):
                return source.model_copy(update={"geometry_inputs": await self._resources.load_build_inputs(
                    scope.identity, content.input_metadata.geometry_inputs)})
        else:
            base = content.published_base
            bundle = await self._resources.get_exact_version(scope.identity, resource_id=base.resource_id,
                resource_version_id=base.resource_version_id)
            pair = await self._resources.read_exact_asset(scope.identity, resource_id=base.resource_id,
                resource_version_id=base.resource_version_id, asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY)
            if bundle is None or pair is None:
                raise GeometryAuthoringApplicationError("GEOMETRY_INPUT_CHANGED", "Fixed draft base is not available")
            asset, payload = pair
            request = CanonicalGeometry(format=CanonicalGeometryFormat(asset.asset_format), content_type=asset.content_type,
                payload=payload, sha256=asset.sha256, summary=GeometrySummary.model_validate(bundle.version.manifest["geometry_summary"]))
        return {"build_request": request,
            **({"material_loader": load_materials} if content.input_metadata.resource_kind is GeometryResourceKind.MODELING_FUNCTION else {}),
            "parameter_schema": content.input_metadata.parameter_schema, "interfaces": content.interfaces}

    async def _require_draft_dependencies(self, scope, owner, content):
        for item in content.input_metadata.geometry_inputs:
            bundle = await self._resources.get_exact_version(scope.identity, resource_id=item.resource_id,
                resource_version_id=item.resource_version_id)
            if bundle is None or bundle.version.content_digest != item.content_digest:
                raise GeometryAuthoringApplicationError("GEOMETRY_INPUT_CHANGED", "Exact Geometry dependency is not available")
            if not owner.permits_dependency(bundle.resource.owner_scope):
                raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_SCOPE_INVALID",
                    "Geometry inputs cannot have a narrower owner scope than the draft")
        if not content.source_files and content.published_base is not None:
            base = content.published_base
            bundle = await self._resources.get_exact_version(scope.identity, resource_id=base.resource_id,
                resource_version_id=base.resource_version_id)
            if bundle is None or not owner.permits_dependency(bundle.resource.owner_scope):
                raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_SCOPE_INVALID",
                    "An exact fixed-model base must be available within the draft owner scope")

    async def _draft_geometry_inputs(self, scope, bindings):
        inputs = []
        for item in bindings:
            bundle = await self._catalog.resolve_exact_version(scope.identity, item.resource_version_alias)
            canonical = next((a for a in bundle.assets if a.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY), None)
            if bundle.resource.resource_kind is not GeometryResourceKind.MAPPED_MODEL or canonical is None:
                raise GeometryAuthoringApplicationError("GEOMETRY_DRAFT_INPUT_INVALID", "Geometry input requires a published fixed model")
            inputs.append(GeometryResourceInput(input_key=item.input_key,
                resource_id=bundle.resource.resource_id, resource_version_id=bundle.version.resource_version_id,
                content_digest=bundle.version.content_digest, canonical_geometry_sha256=canonical.sha256))
        return tuple(inputs)

    async def _draft_from_published(self, scope, bundle, *, display_name, usage_instructions):
        async def read(role):
            return await self._resources.read_exact_asset(scope.identity,
                resource_id=bundle.resource.resource_id, resource_version_id=bundle.version.resource_version_id,
                asset_role=role)
        source = await read(GeometryResourceAssetRole.SOURCE_BUNDLE)
        schema = await read(GeometryResourceAssetRole.PARAMETER_SCHEMA)
        return self._draft_content(dict(display_name=display_name if display_name is not None else bundle.resource.display_name,
            usage_instructions=self._draft_usage(bundle.version.manifest.get("usage_instructions"), usage_instructions),
            source_files=self._draft_source_files(source[1]) if source else (),
            input_metadata=GeometryAuthoringInputMetadata(resource_kind=bundle.resource.resource_kind,
                geometry_inputs=bundle.version.manifest.get("geometry_inputs", ()),
                parameter_schema=json.loads(schema[1]) if schema else None),
            interfaces=bundle.version.manifest.get("interfaces", ()) if bundle.resource.resource_kind is GeometryResourceKind.MAPPED_MODEL else (),
            published_base=GeometryPublishedBase(resource_id=bundle.resource.resource_id,
                resource_version_id=bundle.version.resource_version_id)))


    async def get_operation(
        self,
        scope: InvocationScope,
        *,
        operation_id: str,
    ) -> PublishedCriticalOperation:
        published = await self._repository.get_geometry_authoring_operation(
            scope,
            operation_id=operation_id,
        )
        if published is None:
            raise GeometryAuthoringApplicationError(
                "GEOMETRY_AUTHORING_OPERATION_NOT_FOUND",
                "The terminal geometry authoring Operation was not found",
                phase="read",
            )
        return published


    async def read_source(self, scope, *, operation_id, source_path, view):
        """Read the exact saved draft content associated with a Write receipt."""
        published = await self.get_operation(scope, operation_id=operation_id)
        content = await self._drafts.read_snapshot(scope, published.operation.code_artifact, GeometryDraftContent)
        if not content.source_files:
            raise GeometryAuthoringApplicationError("GEOMETRY_SOURCE_UNAVAILABLE",
                "This fixed asset has no editable source bundle", phase="read")
        packed = self._source_bundles.pack({f.path: f.source.encode("utf-8") for f in content.source_files})
        inspected = self._source_bundles.inspect(packed.content)
        if source_path is None and view is not None:
            raise GeometryAuthoringApplicationError("GEOMETRY_SOURCE_READ_INVALID",
                "source_path is required for a source view", phase="read")
        projected = (self._text_views.view(inspected.read_text(source_path),
            view or TextSourceView(view_mode="full")) if source_path is not None else None)
        return GeometryAuthoringSourceReadResult(operation_id=operation_id,
            bundle_sha256=packed.sha256, members=inspected.members, selected_path=source_path, view=projected)


    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("geometry authoring clock must return an aware datetime")
        return value


def _bounded_message(message: str) -> str:
    normalized = message.strip() or "Geometry authoring failed"
    encoded = normalized.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_DIAGNOSTIC_BYTES:
        return normalized
    suffix = "... [truncated]"
    budget = _MAX_DIAGNOSTIC_BYTES - len(suffix.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix


__all__ = ["GeometryAuthoringApplicationError", "GeometryAuthoringService"]
