"""Build formal assets from the exact admitted Geometry and Family definitions.

Storage keeps its existing domain owners. The repository commits the complete
Geometry/Family closure and its one terminal Operation in one transaction.
"""
from datetime import UTC, datetime
import asyncio
import hashlib
from typing import Literal

from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError
from CAD2BIM_backend.domain.authoring_draft import (
    AuthoringPublicationCommitResult, AuthoringPublicationCommitRejected,
    AuthoringPublicationPending,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest, stable_id
from CAD2BIM_backend.domain.control_plane import ExecutionMode, ConflictError
from CAD2BIM_backend.domain.data_runtime import (
    AuthoringDraftSelection, AuthoringPublishOperationDetail, AuthoringPublishedGeometryReference,
    CriticalOperationRecord, CriticalOperationKind, CriticalOperationStatus, CriticalOperationInitiator,
    CriticalOperationErrorPhase, AuthoringPreviewExampleReference,
)
from CAD2BIM_backend.domain.geometry_authoring import GeometryAuthoringVersionDraft, GeometryAuthoringSuccessPublication
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole, GeometryResourceKind, GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle, geometry_resource_content_digest,
)
from CAD2BIM_backend.domain.product_family_authoring import (
    ProductFamilyAuthoringVersionDraft, ProductFamilyAuthoringPublication, ResolvedProductFamilyGeometryBindingInput,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationOutputNeed, ProductRealizationInvocation,
    ResolvedModelingFunctionBinding,
    product_realization_binding_digest,
)
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, GeometryFixedBuildResult
from CAD2BIM_backend.domain.geometry_interface import resolve_fixed_geometry_interfaces
from CAD2BIM_backend.domain.runtime_execution import GEOMETRY_AUTHORING_EXECUTION, PRODUCT_AUTHORING_EXECUTION
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit


class AuthoringPublicationService:
    def __init__(self, repository, drafts, geometry, family, inspections, *, geometry_runtime, realization_runtime):
        self._repository, self._drafts = repository, drafts
        self._geometry, self._family, self._inspections = geometry, family, inspections
        self._geometry_runtime, self._realization_runtime = geometry_runtime, realization_runtime

    async def request_rejection_status(self, scope, *, request_identity, error) -> Literal["not_started", "unknown"]:
        definite = self._drafts.fixed_input_rejection(error) or (
            isinstance(error, AuthoringDraftError) and error.code == "DRAFT_CHANGED")
        return await self._drafts.request_rejection_status(scope, tool_name="ifc_authoring_publish",
            request_identity=request_identity, definite_rejection=definite)

    async def operation_result(self, scope, published):
        """Reauthorize exact formal results of the original publication Operation."""
        operation = published.operation
        if (operation.owner_id != scope.owner_id or operation.tenant_id != scope.identity.tenant_id
            or operation.task_id != scope.identity.task_id
            or operation.operation_kind is not CriticalOperationKind.AUTHORING_PUBLISH
            or operation.status is not CriticalOperationStatus.SUCCEEDED):
            raise AuthoringDraftError("PUBLICATION_RESULT_UNAVAILABLE", "Publication is outside this scope")
        geometries = []
        for reference in operation.detail.geometry_versions:
            bundle = await self._geometry._resources.get_exact_version(scope.identity,
                resource_id=reference.resource_id, resource_version_id=reference.resource_version_id)
            if bundle is None:
                raise AuthoringDraftError("PUBLICATION_RESULT_UNAVAILABLE", "An exact published Geometry is unavailable")
            geometries.append({"resource_version_alias": self._geometry._catalog.version_ref(scope.identity, bundle)})
        family = None
        if operation.result_product_family_version_id is not None:
            bundle = await self._repository.get_product_family_version_by_id(scope.identity,
                family_version_id=operation.result_product_family_version_id)
            if bundle is None:
                raise AuthoringDraftError("PUBLICATION_RESULT_UNAVAILABLE", "The exact published Family is unavailable")
            family = self._family._family_catalog.version_ref(scope.identity, bundle)
        return {"root_kind": operation.detail.root_kind, "geometry_versions": tuple(geometries),
            "family_version_alias": family}

    async def publish(self, scope, *, root_kind, draft_ref, expected_edit_token, request_identity, preview_examples=()):
        declaration = {"root_kind": root_kind, "draft_ref": draft_ref, "expected_edit_token": expected_edit_token,
            "preview_examples": [item.model_dump(mode="json") for item in preview_examples]}
        fingerprint = sha256_digest(declaration)
        operation_id = self._drafts.operation_identity(scope, "ifc_authoring_publish", request_identity)
        state = await self._repository.get_authoring_execution_state(scope, operation_id,
            operation_kind=CriticalOperationKind.AUTHORING_PUBLISH)
        if state is not None:
            if state.operation.request_fingerprint != fingerprint:
                raise AuthoringDraftError("AUTHORING_IDEMPOTENCY_CONFLICT", "The original publication call has different inputs")
            terminal = await self._terminal_result(scope, state.operation)
            if terminal is not None:
                return terminal
        recovered_claim = await self._claim(scope, state.operation) if state is not None else None
        try:
            owner = self._geometry if root_kind == "geometry" else self._family
            target, target_content = await owner.read_draft(scope, draft_ref=draft_ref)
            await self._drafts.require_write_owner(scope, target.owner_scope)
            if target.generation != expected_edit_token:
                raise AuthoringDraftError("DRAFT_CHANGED", "Draft changed; reread before publishing")
            expected_drafts, geometry_drafts, published_bindings = [target], {}, {}
            if root_kind == "geometry":
                geometry_drafts[target.root_id] = (target, target_content)
            else:
                definition = await self._family._project_draft_definition(scope, target_content.definition, target.owner_scope)
                for binding in definition.geometry_bindings:
                    if binding.source.source_kind == "draft":
                        root_id = self._drafts.resolve_reference(scope, binding.source.draft_ref, root_kind="geometry")
                        if root_id not in geometry_drafts:
                            draft, content = await self._geometry.read_draft(scope, draft_ref=binding.source.draft_ref)
                            if draft.owner_scope != target.owner_scope:
                                raise AuthoringDraftError("PUBLICATION_SCOPE_INVALID", "A Family cannot publish another owner's draft")
                            geometry_drafts[root_id] = (draft, content)
                            expected_drafts.append(draft)
                        published_bindings[binding.binding_key] = root_id
                    else:
                        published_bindings[binding.binding_key] = await self._geometry._catalog.resolve_exact_version(
                            scope.identity, binding.source.resource_version_alias)
            selections = tuple(AuthoringDraftSelection(root_kind=d.root_kind, root_id=d.root_id,
                generation=d.generation, snapshot=d.snapshot) for d in expected_drafts)
            if state is not None and state.operation.detail.expected_drafts != selections:
                raise AuthoringDraftError("DRAFT_CHANGED", "The admitted publication closure changed; use a new request after rereading")
            examples = (state.operation.detail.preview_examples if state is not None else
                await self._preview_examples(scope, preview_examples, target, target_content, geometry_drafts, published_bindings))
        except (Exception, asyncio.CancelledError) as exc:
            if state is not None:
                await self._fail(scope, state.operation, recovered_claim, exc, CriticalOperationErrorPhase.COMMIT_FENCE)
            raise
        created = datetime.now(UTC)
        operation = state.operation if state is not None else CriticalOperationRecord(
            operation_id=operation_id, owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id, run_id=scope.run_id,
            operation_kind=CriticalOperationKind.AUTHORING_PUBLISH, tool_name="ifc_authoring_publish",
            initiator=(CriticalOperationInitiator.DIRECT_CLIENT if scope.execution_mode is ExecutionMode.DIRECT_MCP else CriticalOperationInitiator.RUNNER),
            intent_summary="Publish exact definitions and construct their required formal assets.",
            idempotency_key=f"authoring-publish:{request_identity}", request_fingerprint=fingerprint,
            status=CriticalOperationStatus.PREPARED, started_at=created,
            target_geometry_resource_id=target.root_id if root_kind == "geometry" else None,
            target_product_family_id=target.root_id if root_kind == "family" else None,
            detail=AuthoringPublishOperationDetail(root_kind=root_kind, root_id=target.root_id,
                expected_drafts=selections, preview_examples=examples))
        if state is None:
            try:
                operation = await self._repository.begin_authoring_publication(scope, operation=operation,
                    expected_drafts=tuple(expected_drafts))
            except AuthoringPublicationPending as exc:
                raise AuthoringDraftError(exc.code, str(exc)) from exc
            if operation.operation_id != operation_id or operation.run_id != scope.run_id:
                raise AuthoringDraftError("PUBLICATION_RECONCILIATION_REQUIRED",
                    "Another publication for this draft is active or uncertain; check its original Operation before starting a new request.")
        terminal = await self._terminal_result(scope, operation)
        if terminal is not None:
            return terminal
        claim = recovered_claim or await self._claim(scope, operation)
        committing = False
        phase = CriticalOperationErrorPhase.EXECUTION
        try:
            existing = await self._repository.find_current_authoring_publication(scope,
                root_kind=root_kind, root_id=target.root_id, expected_drafts=tuple(expected_drafts))
            if existing is not None:
                geometries = existing.geometry_versions
                family_version = existing.family_version
                success = self._success(operation, geometries, family_version)
                committing = True
                return await self._repository.commit_authoring_publication(scope, operation=success,
                    expected_drafts=tuple(expected_drafts), execution_claim=claim)
            candidates, by_root = [], {}
            for draft, content in geometry_drafts.values():
                published = await self._repository.find_published_geometry_draft(scope, draft=draft)
                if published is not None:
                    by_root[draft.root_id] = published
                    continue
                candidate = await self._geometry_candidate(scope, draft, content, operation_id, created)
                candidates.append(candidate)
                by_root[draft.root_id] = GeometryResourceVersionBundle(resource=candidate[0],
                    version=candidate[1].to_record(version_no=1), assets=candidate[2])
            family_candidate = None
            if root_kind == "family":
                family_candidate = await self._family_candidate(scope, target, target_content,
                    published_bindings, by_root, candidates, operation_id, created)
            version = family_candidate[1] if family_candidate else None
            success = self._success(operation, tuple(by_root.values()), version)
            geometry_publications = tuple(GeometryAuthoringSuccessPublication(operation=success,
                resource=r, version=v, assets=assets) for r,v,assets,_ in candidates)
            phase = CriticalOperationErrorPhase.STORAGE_VERIFICATION
            for publication, candidate in zip(geometry_publications, candidates, strict=True):
                await self._geometry._resources.store_candidate_assets(scope.identity,
                    publication.to_bundle(version_no=1), candidate[3])
            family_publication = None
            if family_candidate:
                family, version, specs = family_candidate
                assets = tuple([await self._family._family_assets.write_exact_asset(family, asset, body)
                    for asset,body in specs])
                family_publication = ProductFamilyAuthoringPublication(operation=success, family=family,
                    version=version, geometry_bindings=version.geometry_bindings, assets=assets)
            committing = True
            return await self._repository.commit_authoring_publication(scope, operation=success,
                geometry_publications=geometry_publications, family_publication=family_publication,
                expected_drafts=tuple(expected_drafts), execution_claim=claim)
        except (Exception, asyncio.CancelledError) as exc:
            if committing and not isinstance(exc, (ConflictError, AuthoringPublicationCommitRejected)):
                # A lost commit response is never turned into a contradictory failure.
                try:
                    recovered = await self._repository.get_operation(scope, operation.operation_id)
                except Exception:
                    recovered = None
                if recovered is not None:
                    result = await self._terminal_result(scope, recovered.operation)
                    if result is not None:
                        return result
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise AuthoringDraftError("PUBLICATION_RECONCILIATION_REQUIRED",
                    "Publication commit outcome is uncertain; retain the original request and check its Operation before retrying.",
                    phase=CriticalOperationErrorPhase.COMMIT_FENCE.value) from exc
            await self._fail(scope, operation, claim, exc,
                CriticalOperationErrorPhase.COMMIT_FENCE if committing else phase)

    async def _fail(self, scope, operation, claim, exc, phase):
        code = ("AUTHORING_CONFLICT" if isinstance(exc, ConflictError)
            else "AUTHORING_CANCELLED" if isinstance(exc, asyncio.CancelledError)
            else "AUTHORING_TIMEOUT" if isinstance(exc, TimeoutError)
            else getattr(exc, "code", "AUTHORING_PUBLICATION_FAILED"))
        failed = CriticalOperationRecord.model_validate({**operation.model_dump(mode="python"),
            "status": CriticalOperationStatus.FAILED, "finished_at": datetime.now(UTC),
            "error_code": str(code), "error_message": str(exc)[:2000] or str(code), "error_phase": phase})
        saved = await asyncio.shield(self._repository.finalize_authoring_publication_operation(
            scope, operation=failed, execution_claim=claim))
        if isinstance(exc, asyncio.CancelledError):
            raise exc
        if saved.operation != failed:
            raise AuthoringDraftError("PUBLICATION_RECONCILIATION_REQUIRED",
                "The original publication failure could not be confirmed; retain its original request.",
                phase=phase.value) from exc
        raise AuthoringDraftError(str(code), failed.error_message,
            operation_alias=saved.catalog_entry.alias, phase=phase.value, operation_status="failed") from exc

    async def _claim(self, scope, operation):
        state = await self._repository.get_authoring_execution_state(scope, operation.operation_id,
            operation_kind=CriticalOperationKind.AUTHORING_PUBLISH)
        if state is None or state.operation.request_fingerprint != operation.request_fingerprint:
            raise AuthoringDraftError("PUBLICATION_RECONCILIATION_REQUIRED", "The admitted publication is unavailable")
        if state.execution_owner is None:
            claim = await self._repository.claim_authoring_execution(scope, operation.operation_id,
                operation_kind=CriticalOperationKind.AUTHORING_PUBLISH)
        else:
            claim = await self._repository.reclaim_authoring_execution(scope, operation.operation_id,
                operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
                expected_owner=state.execution_owner, expected_generation=state.execution_generation)
        if claim is None:
            raise AuthoringDraftError("PUBLICATION_RECONCILIATION_REQUIRED", "The original publication is active or uncertain")
        return claim

    async def _terminal_result(self, scope, operation):
        if operation.status is CriticalOperationStatus.FAILED:
            saved_failure = await self._repository.get_operation(scope, operation.operation_id)
            confirmed = saved_failure is not None and saved_failure.operation == operation
            raise AuthoringDraftError(operation.error_code, operation.error_message,
                operation_alias=saved_failure.catalog_entry.alias if confirmed else None,
                phase=operation.error_phase.value if operation.error_phase is not None else "execution",
                operation_status="failed" if confirmed else None)
        if operation.status is not CriticalOperationStatus.SUCCEEDED:
            return None
        published = await self._repository.get_operation(scope, operation.operation_id)
        geometries = tuple([await self._geometry._resources.get_exact_version(scope.identity,
            resource_id=ref.resource_id, resource_version_id=ref.resource_version_id)
            for ref in operation.detail.geometry_versions])
        family = (await self._repository.get_product_family_version_by_id(scope.identity,
            family_version_id=operation.result_product_family_version_id)
            if operation.result_product_family_version_id else None)
        if published is None or any(item is None for item in geometries) or (
            operation.detail.root_kind == "family" and family is None):
            raise AuthoringDraftError("PUBLICATION_RESULT_UNAVAILABLE", "The saved publication closure is not visible")
        return AuthoringPublicationCommitResult(operation=published, geometry_versions=geometries, family_version=family)

    @staticmethod
    def _success(operation, geometries, family_version):
        detail = operation.detail.model_copy(update={
            "geometry_versions": tuple(AuthoringPublishedGeometryReference(resource_id=b.resource.resource_id,
                resource_version_id=b.version.resource_version_id) for b in geometries),
            "family_version": family_version})
        root_geometry = next((b for b in geometries if b.resource.resource_id == detail.root_id), None)
        return CriticalOperationRecord.model_validate({**operation.model_dump(mode="python"),
            "status": CriticalOperationStatus.SUCCEEDED, "finished_at": datetime.now(UTC), "detail": detail,
            "result_geometry_resource_version_id": root_geometry.version.resource_version_id if root_geometry else None,
            "result_product_family_version_id": family_version.family_version_id if family_version else None,
            "base_product_family_version_id": family_version.parent_family_version_id if family_version else None})

    async def _preview_examples(self, scope, selections, target, content, geometry_drafts, published_bindings):
        examples = []
        closure = [(target, content), *geometry_drafts.values()]
        for selection in selections:
            published = await self._inspections._report_operation(scope, selection.operation_alias)
            report = await self._inspections.read_case_report(scope, selection.operation_alias)
            keys = set(selection.case_keys) or {item.case.case_key for item in report.cases}
            if not keys <= {item.case.case_key for item in report.cases}:
                raise AuthoringDraftError("PREVIEW_CASE_MISSING", "A selected saved case does not exist")
            if not any(self._matches(source, draft, contents)
                for source in (report.source, *report.dependencies) for draft,contents in closure):
                raise AuthoringDraftError("PREVIEW_SOURCE_MISMATCH", "The example does not describe this exact definition")
            if self._matches(report.source, target, content) and target.root_kind == "family":
                for value in published_bindings.values():
                    if isinstance(value, str):
                        dependency, dependency_content = geometry_drafts[value]
                        matches = any(self._matches(source, dependency, dependency_content) for source in report.dependencies)
                    else:
                        matches = any(source.source_type == "published"
                            and source.source_id == value.resource.resource_id
                            and source.snapshot_id == value.version.resource_version_id
                            and source.content_digest == value.version.content_digest for source in report.dependencies)
                    if not matches:
                        raise AuthoringDraftError("PREVIEW_DEPENDENCY_MISMATCH", "The example used a different exact Geometry dependency")
            examples.append(AuthoringPreviewExampleReference(origin_run_id=published.operation.run_id,
                operation_id=published.operation.operation_id,
                case_keys=tuple(item.case.case_key for item in report.cases if item.case.case_key in keys)))
        return tuple(examples)

    @staticmethod
    def _matches(source, draft, content):
        return (source.source_type == "draft" and source.source_id == draft.root_id
            and source.content_digest == content.execution_digest)

    async def _geometry_candidate(self, scope, draft, content, operation_id, created):
        resource = await self._geometry._resources.get_resource(scope.identity, resource_id=draft.root_id)
        if resource is None:
            raise AuthoringDraftError("DRAFT_NOT_AVAILABLE", "Geometry root is no longer visible")
        version_id = stable_id("geometry_resource_version", {"operation_id": operation_id, "resource_id": draft.root_id})
        metadata = content.input_metadata
        prepared = await self._geometry.prepare_draft_content(scope, draft=draft, content=content)
        manifest = {"resource_kind": metadata.resource_kind.value,
            "source_format": "python_source_bundle_v1",
            "unit": "METRE", "local_frame": {"handedness": "right", "origin": [0.,0.,0.],
                "x_axis": [1.,0.,0.], "y_axis": [0.,1.,0.], "z_axis": [0.,0.,1.]},
            "usage_instructions": content.usage_instructions}
        specs = []
        if content.source_files:
            packed = self._geometry._source_bundles.pack({f.path:f.source.encode("utf-8") for f in content.source_files})
            manifest["source_bundle_sha256"] = packed.sha256
            specs.append((GeometryResourceAssetRole.SOURCE_BUNDLE, "source_bundle_v1", "source.zip", "application/zip", packed.content))
        if metadata.geometry_inputs:
            manifest["geometry_inputs"] = [item.model_dump(mode="json") for item in metadata.geometry_inputs]
        if metadata.resource_kind is GeometryResourceKind.MAPPED_MODEL:
            geometry = prepared["build_request"]
            interfaces = prepared["interfaces"]
            async with self._geometry_runtime.invocation_scope(GEOMETRY_AUTHORING_EXECUTION), self._geometry_runtime.execution_scope(GEOMETRY_AUTHORING_EXECUTION):
                if isinstance(geometry, CanonicalGeometry):
                    summary = await self._geometry_runtime.reopen(geometry)
                    geometry = geometry.model_copy(update={"summary": summary})
                else:
                    built = GeometryFixedBuildResult.model_validate(await self._geometry_runtime.build_fixed(geometry))
                    if built.source_bundle_sha256 != hashlib.sha256(geometry.source_bundle).hexdigest():
                        raise ValueError("Fixed Geometry build changed its exact source")
                    geometry, interfaces = built.geometry, resolve_fixed_geometry_interfaces(interfaces, built.interfaces)
            manifest.update(geometry_format=geometry.format.value,
                asset_content_type=geometry.content_type.split(";")[0], runtime_content_type=geometry.content_type,
                geometry_summary=geometry.summary.model_dump(mode="json"))
            manifest.update(canonical_geometry_sha256=geometry.sha256, build_parameters_sha256=sha256_digest({}),
                interfaces=[item.model_dump(mode="json") for item in interfaces])
            specs.extend(((GeometryResourceAssetRole.CANONICAL_GEOMETRY, geometry.format.value, "geometry.brep", geometry.content_type, geometry.payload),
                (GeometryResourceAssetRole.MANIFEST, "json", "manifest.json", "application/json", canonical_json(manifest).encode("utf-8"))))
        else:
            schema = canonical_json(metadata.parameter_schema).encode("utf-8")
            manifest.update(parameter_schema_sha256=hashlib.sha256(schema).hexdigest())
            specs.append((GeometryResourceAssetRole.PARAMETER_SCHEMA, "json_schema", "parameters.schema.json", "application/schema+json", schema))
        assets = tuple(GeometryResourceVersionAssetRecord(resource_version_id=version_id, asset_role=role,
            asset_format=fmt, filename=name, content_type=mime, size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
            created_at=created) for role,fmt,name,mime,body in specs)
        manifest_digest = sha256_digest(manifest)
        version = GeometryAuthoringVersionDraft(resource_version_id=version_id, resource_id=draft.root_id,
            origin_kind="agent_generated", manifest=manifest, manifest_digest=manifest_digest,
            content_digest=geometry_resource_content_digest(resource_kind=metadata.resource_kind, manifest_digest=manifest_digest, assets=assets),
            lineage={"operation_id": operation_id, "draft_snapshot": draft.snapshot.model_dump(mode="json")},
            created_at=created)
        return resource, version, assets, tuple(spec[-1] for spec in specs)

    async def _family_candidate(self, scope, draft, content, published_bindings, new_geometry,
                                candidates, operation_id, created):
        from CAD2BIM_backend.application.product_family_authoring_service import _version_assets
        version_id = stable_id("product_family_version", {"operation_id": operation_id, "family_id": draft.root_id})
        bindings, runtime_bindings, schemas = [], [], {}
        candidate_bytes = {v.resource_version_id: {a.asset_role: body for a,body in zip(assets,bodies,strict=True)}
            for _,v,assets,bodies in candidates}
        for binding in content.definition.geometry_bindings:
            value = published_bindings[binding.binding_key]
            bundle = new_geometry[value] if isinstance(value, str) else value
            resolved = ResolvedProductFamilyGeometryBindingInput(binding_key=binding.binding_key,
                geometry_resource_id=bundle.resource.resource_id, geometry_resource_version_id=bundle.version.resource_version_id,
                geometry_content_digest=bundle.version.content_digest, geometry_owner_scope=bundle.resource.owner_scope,
                expected_resource_kind=binding.expected_resource_kind, geometry_purpose=binding.geometry_purpose)
            bindings.append(resolved)
            runtime = (await self._family._realization_inputs._load_runtime_bindings(
                scope, (resolved,), (bundle,), candidate_assets=candidate_bytes, numeric_only=True))[0]
            runtime_bindings.append(runtime)
            if isinstance(runtime, ResolvedModelingFunctionBinding):
                schemas[binding.binding_key] = runtime.parameter_schema
        inputs = self._family._realization_inputs
        contract = inputs.normalize_standard_definition(
            inputs.normalize_contract_defaults(content.definition.family_contract, schemas))
        parent_id = await self._repository.get_latest_product_family_version_id(scope.identity, family_id=draft.root_id)
        version = ProductFamilyAuthoringVersionDraft(family_id=draft.root_id, family_version_id=version_id,
            parent_family_version_id=parent_id,
            display_name=content.display_name, purpose=content.purpose, usage_instructions=content.usage_instructions,
            family_contract=contract, contract_digest=sha256_digest(contract), geometry_bindings=tuple(bindings), created_at=created)
        packed = self._family._source_bundles.pack({f.path:f.source.encode("utf-8") for f in content.source_files})
        execution = None
        if contract.standard_model is not None:
            invocation = ProductRealizationInvocation(realization_kind="standard_model",
                public_parameters=contract.standard_model.public_parameters,
                product_origin=(0., 0., 0.), length_unit=IfcLengthUnit(name="METRE"))
            request = inputs.prepare_definition_requests(family_contract=contract, source_bundle=packed.content,
                geometry_bindings=tuple(runtime_bindings), invocations=(invocation,))[0]
            async with self._realization_runtime.invocation_scope(PRODUCT_AUTHORING_EXECUTION), self._realization_runtime.execution_scope(PRODUCT_AUTHORING_EXECUTION):
                checked = [result async for result in self._realization_runtime.precheck_batch((request,))]
                if len(checked) != 1 or checked[0].frame_check_status != "PASS":
                    raise AuthoringDraftError("STANDARD_MODEL_BUILD_FAILED", "The standard construction failed its numeric interface check")
                selected = {fragment.binding_key for fragment in checked[0].proposal.fragments}
                actual = {item.binding_key: item for item in request.geometry_bindings}
                for resolved in bindings:
                    if resolved.binding_key in selected:
                        value = published_bindings[resolved.binding_key]
                        bundle = new_geometry[value] if isinstance(value, str) else value
                        actual[resolved.binding_key] = (await inputs._load_runtime_bindings(scope, (resolved,), (bundle,),
                            candidate_assets=candidate_bytes))[0]
                loaded = request.model_copy(update={"geometry_bindings": tuple(actual[item.binding_key] for item in request.geometry_bindings)})
                outputs = [item async for item in self._realization_runtime.instantiate_batch((loaded,), checked,
                    output_need=ProductRealizationOutputNeed.PREVIEW_WORLD)]
                if len(outputs) != 1:
                    raise AuthoringDraftError("STANDARD_MODEL_BUILD_FAILED", "The standard construction returned no exact geometry")
                execution = outputs[0]
            result = execution.realization
            if (execution.geometry is None
                or result.binding_digest != product_realization_binding_digest(request)
                or result.source_bundle_sha256 != request.source_bundle_sha256
                or result.normalized_public_parameters != request.invocation.public_parameters
                or result.frame_check_status == "FAIL"):
                raise AuthoringDraftError("STANDARD_MODEL_BUILD_FAILED", "The standard construction did not produce valid exact geometry")
        specs = _version_assets(family_version_id=version_id, source_bundle=packed.content, draft=version,
            standard_execution=execution, created_at=created)
        family = await self._repository.get_product_family_authoring_root(scope, family_id=draft.root_id)
        return family, version, specs
