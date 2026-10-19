"""One current-contract vertical slice, using actual RLS, storage and geometry.

The caller supplies an isolated, migrated PostgreSQL. No model or native MCP
session is launched; the same Application owners used by both entries run here.
"""
import base64
import os
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.authoring_draft_service import AuthoringDraftError, AuthoringDraftService
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.data_runtime import AuthoringPreviewSelection, CriticalOperationKind, CriticalOperationStatus
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.preview_cases import GeometryPreviewCase, GeometryCaseExpectation, FamilyPreviewCase, FamilyCaseExpectation
from CAD2BIM_backend.domain.product_realization import ProductRealizationInvocation
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
from CAD2BIM_backend.domain.geometry_inspection import InspectionWholeSelection, InspectionResultCoordinates
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionModelObservation
from CAD2BIM_backend.ifc_application_resources import create_authoring_application_resources
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.infrastructure.postgres.models import (
    CriticalOperationModel, GeometryResourceVersionModel, ProductFamilyVersionModel, RunModel,
)
from CAD2BIM_backend.infrastructure.postgres.repository_product_family_authoring import (
    PostgresProductFamilyAuthoringRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.infrastructure.worker_runtime import HostWorkerCapacity
from tests.integration.backend.test_geometry_query_postgres import (
    _create_logins, _drop_logins, _open_context, _runtime, _runtime_url, _task_owner_and_run,
)
from tests.unit.backend.product_family_fixtures import current_family_contract

pytestmark = [pytest.mark.integration, pytest.mark.network]
DATABASE_URL = os.environ.get("CAD2BIM_TEST_DATABASE_URL", "").strip()


@pytest.mark.asyncio
async def test_targeted_write_edits_persist_complete_content_and_reject_partial_changes(tmp_path):
    """Exercise the changed save boundary with PostgreSQL/RLS, without rebuilding geometry."""
    from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringApplicationError
    if not DATABASE_URL:
        pytest.skip("isolated CAD2BIM_TEST_DATABASE_URL is required")
    logins = _create_logins(DATABASE_URL)
    runtime = _runtime(_runtime_url(DATABASE_URL, logins["mcp"]))
    admin = create_async_engine(DATABASE_URL)
    resources = None
    try:
        identity = await _open_context(runtime, subject=f"targeted-{uuid4().hex}", session_name="Targeted edits")
        owner, run = await _task_owner_and_run(admin, identity)
        scope = InvocationScope(identity=identity, owner_id=owner, run_id=run,
            execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
        storage = LocalStorage(tmp_path / "storage")
        resources = create_authoring_application_resources(repository=runtime.repository, storage=storage,
            artifacts=ArtifactService(runtime.repository, storage),
            cursor_codec=CursorCodec(base64.urlsafe_b64encode(b"e"*32).rstrip(b"=").decode()),
            capacity=HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path / "capacity"))
        with bind_tenant_execution_context(identity):
            original = {"mode":"create", "display_name":"Editable box",
                "usage_instructions":{"action":"set", "value":"Width is in metres. Original guidance."},
                "source":{"source_kind":"modeling_function",
                    "source_files":[{"path":"main.py", "source":"def build(ctx):\n    return None\n"},
                        {"path":"interfaces.py", "source":"def evaluate(ctx):\n    return None\n"}],
                    "parameter_schema":{"action":"set", "value":{"type":"object", "additionalProperties":False,
                        "required":["width"], "properties":{"width":{"type":"number", "default":0.1},
                            "obsolete":{"type":"number", "default":0.2}}}}}}
            first = await resources.geometry_authoring.save_draft_from_aliases(scope,
                declaration=original, request_identity="geometry-create")
            reference = resources.geometry_authoring.draft_reference(scope, first.draft)
            changed = await resources.geometry_authoring.save_draft_from_aliases(scope, request_identity="geometry-edit",
                declaration={"mode":"update", "draft_ref":reference, "expected_edit_token":1,
                    "usage_instructions":{"action":"edit", "edits":[{"old_text":"Original guidance.", "new_text":"Updated guidance."}]},
                    "parameter_schema":{"action":"edit", "set":{"height":{"schema":{"type":"number","default":0.3},"required":False}},
                        "remove":["obsolete"]}})
            current, content = await resources.geometry_authoring.read_draft(scope, draft_ref=reference)
            assert current.generation == changed.draft.generation == 2
            assert content.usage_instructions == "Width is in metres. Updated guidance."
            assert set(content.input_metadata.parameter_schema["properties"]) == {"width", "height"}
            assert {item.path: item.source for item in content.source_files} == {
                item["path"]: item["source"] for item in original["source"]["source_files"]}
            assert await resources.geometry_resources.get_latest_version(identity, resource_id=current.root_id) is None
            second = await resources.geometry_authoring.save_draft_from_aliases(scope,
                declaration={**original, "display_name":"Alternate box"}, request_identity="geometry-second")
            other_ref = resources.geometry_authoring.draft_reference(scope, second.draft)
            binding = {"binding_key":"body_geometry", "source":{"source_kind":"draft", "draft_ref":reference},
                "expected_resource_kind":"modeling_function", "geometry_purpose":"shape_fragment"}
            family_contract = current_family_contract(standard=True,
                parameter_sources={"width":{"source_kind":"fixed", "value":0.1}})
            family = await resources.family_authoring.save_draft_from_aliases(scope, request_identity="family-create", declaration={
                "mode":"create", "display_name":"Editable family", "purpose":"Bounded edit test",
                "usage_instructions":{"action":"set", "value":"Original family guidance."},
                "source":{"source_kind":"inline", "source_files":[{"path":"main.py", "source":"def realize(ctx):\n    return None\ndef standard_model(ctx):\n    return None\n"}],
                    "definition":{"action":"set", "value":{"family_contract":family_contract.model_dump(mode="json", by_alias=True),
                        "geometry_bindings":[binding]}}}})
            family_ref = resources.family_authoring.draft_reference(scope, family.draft)
            _, created_family = await resources.family_authoring.read_draft(scope, draft_ref=family_ref)
            assert {key: item.model_dump(mode="json") for key, item in
                created_family.definition.family_contract.geometry_fragments[0].binding_options[0].parameter_sources.overrides.items()} == {
                    "width":{"source_kind":"fixed", "value":0.1},
                    "height":{"source_kind":"fixed", "value":0.3}}
            edit = {"mode":"update", "draft_ref":family_ref, "expected_edit_token":1,
                "usage_instructions":{"action":"edit", "edits":[{"old_text":"Original", "new_text":"Revised"}]},
                "definition":{"action":"edit", "family_contract":{"geometry_fragments":{"set":[
                    family_contract.geometry_fragments[0].model_dump(mode="json", by_alias=True)]}},
                    "geometry_bindings":{"set":[{**binding,
                    "source":{"source_kind":"draft", "draft_ref":other_ref}}]}}}
            saved = await resources.family_authoring.save_draft_from_aliases(scope, declaration=edit, request_identity="family-edit")
            replay = await resources.family_authoring.save_draft_from_aliases(scope, declaration=edit, request_identity="family-edit")
            assert replay.draft == saved.draft
            before, result = await resources.family_authoring.read_draft(scope, draft_ref=family_ref)
            assert before.generation == 2 and result.usage_instructions == "Revised family guidance."
            assert result.definition.geometry_bindings[0].source.source_kind == "draft"
            assert result.definition.geometry_bindings[0].source.resource_id == second.draft.root_id
            assert {key: item.model_dump(mode="json") for key, item in
                result.definition.family_contract.geometry_fragments[0].binding_options[0].parameter_sources.overrides.items()} == {
                    "width":{"source_kind":"fixed", "value":0.1},
                    "obsolete":{"source_kind":"fixed", "value":0.2}}
            with pytest.raises(ProductFamilyAuthoringApplicationError):
                await resources.family_authoring.save_draft_from_aliases(scope, request_identity="stale-edit",
                    declaration={**edit, "display_name":"Must not save"})
            with pytest.raises(ProductFamilyAuthoringApplicationError) as invalid:
                await resources.family_authoring.save_draft_from_aliases(scope, request_identity="dangling-delete",
                    declaration={"mode":"update", "draft_ref":family_ref, "expected_edit_token":2,
                        "definition":{"action":"edit", "geometry_bindings":{"remove":["body_geometry"]}}})
            assert invalid.value.phase == "preflight"
            after, unchanged = await resources.family_authoring.read_draft(scope, draft_ref=family_ref)
            assert after == before and unchanged == result
    finally:
        if resources is not None:
            await resources.close()
        await runtime.close()
        await admin.dispose()
        _drop_logins(DATABASE_URL, logins)


@pytest.mark.asyncio
@pytest.mark.parametrize("geometry_first", [True, False], ids=["geometry-first", "family-first"])
async def test_draft_preview_publish_scope_and_original_operation_recovery(tmp_path, monkeypatch, geometry_first):
    if not DATABASE_URL:
        pytest.skip("isolated CAD2BIM_TEST_DATABASE_URL is required")
    logins = _create_logins(DATABASE_URL)
    runtime = _runtime(_runtime_url(DATABASE_URL, logins["mcp"]))
    admin = create_async_engine(DATABASE_URL)
    resources = None
    try:
        subject = f"draft-{uuid4().hex}"
        identity = await _open_context(runtime, subject=subject, session_name="Authoring draft slice")
        owner, run = await _task_owner_and_run(admin, identity)
        scope = InvocationScope(identity=identity, owner_id=owner, run_id=run,
            execution_mode=ExecutionMode.DIRECT_MCP, catalog_frontier=None)
        storage = LocalStorage(tmp_path / "storage")
        resources = create_authoring_application_resources(repository=runtime.repository, storage=storage,
            artifacts=ArtifactService(runtime.repository, storage),
            cursor_codec=CursorCodec(base64.urlsafe_b64encode(b"d"*32).rstrip(b"=").decode()),
            capacity=HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path / "capacity"))
        with bind_tenant_execution_context(identity):
            geometry = await resources.geometry_authoring.save_draft_from_aliases(scope, request_identity="save-geometry", declaration={
                "mode":"create", "display_name":"Bounded box", "usage_instructions":{"action":"set","value":"Lengths in metres."},
                "source":{"source_kind":"modeling_function", "source_files":[{"path":"main.py", "source":
                    "import cadquery as cq\ndef build(ctx):\n    return cq.Workplane('XY').box(ctx.param('width'), 0.1, 0.1)\n"},
                    {"path":"interfaces.py", "source":"def evaluate(ctx):\n    return None\n"}],
                    "parameter_schema":{"action":"set","value":{"type":"object","additionalProperties":False,"required":["width"],
                        "properties":{"width":{"type":"number","minimum":0.01,"maximum":1.0,"default":0.1}}}}}})
            geometry_ref = resources.geometry_authoring.draft_reference(scope, geometry.draft)
            assert await resources.geometry_resources.get_latest_version(identity, resource_id=geometry.draft.root_id) is None
            published_geometry = None
            if geometry_first:
                # A parametric definition needs no Preview or sample BREP to publish.
                published_geometry = await resources.geometry_authoring.publish_draft(scope,draft_ref=geometry_ref,
                    expected_edit_token=1,request_identity="publish-geometry")
                assert {asset.asset_role for asset in published_geometry.geometry_versions[0].assets} == {
                    GeometryResourceAssetRole.SOURCE_BUNDLE, GeometryResourceAssetRole.PARAMETER_SCHEMA}
            prepared = await resources.geometry_authoring.prepare_draft_preview(scope, draft_ref=geometry_ref)
            geometry_cases = (GeometryPreviewCase(case_key="nominal", parameters={"width":0.1}, expectation=GeometryCaseExpectation()),
                GeometryPreviewCase(case_key="outside", parameters={"width":2.0}, expectation=GeometryCaseExpectation(expected_status="REJECTED",reason_code="PARAMETERS_INVALID")))
            checked = await resources.geometry_preview.preview_cases(scope, **prepared, cases=geometry_cases,
                retain_for_inspection=True, request_identity="check-geometry")
            assert checked.report.checks_passed
            assert checked.report.cases[0].inspection_ref and checked.report.cases[1].inspection_ref is None
            loaded = await resources.geometry_inspection.read(scope, checked.report.cases[0].inspection_ref)
            assert loaded.geometry.summary.volume_m3 == pytest.approx(0.001)
            from unittest.mock import AsyncMock
            with monkeypatch.context() as frozen:
                for name in ("build_fixed", "validate_function", "evaluate_function_interfaces", "instantiate_function"):
                    frozen.setattr(resources.geometry_runtime,name,AsyncMock(side_effect=AssertionError("Saved observations must not rebuild")))
                observed = await resources.geometry_preview.preview_inspection(scope, checked.report.cases[0].inspection_ref,
                    InspectionWholeSelection(), InspectionResultCoordinates(), GeometryInspectionModelObservation())
                assert observed.preview is not None and not observed.diagnostics
                assert observed.preview.payload.startswith(b"\x89PNG\r\n\x1a\n")
                measured = await resources.geometry_query.fresh_inspection(scope, inspection_ref=checked.report.cases[0].inspection_ref,
                    selection=InspectionWholeSelection(),coordinate_space=InspectionResultCoordinates(),
                    source_code="def query(ctx):\n    return {'volume_m3':ctx.private_geometry().Volume()}\n",request_identity="measure-draft")
                assert measured.results[0]["volume_m3"] == pytest.approx(0.001)
            source = """def make(ctx):
    return {'status':'SOLVED','components':[{'component_key':'body'}], 'fragments':[{'fragment_key':'body_shape','binding_key':'body_geometry'}]}
def realize(ctx):
    return make(ctx)
def standard_model(ctx):
    return make(ctx)
"""
            family = await resources.family_authoring.save_draft_from_aliases(scope, request_identity="save-family", declaration={
                "mode":"create","display_name":"Bounded fitting","purpose":"Exercise the authoring lifecycle",
                "usage_instructions":{"action":"set","value":"Use the declared fixed dimensions."},"source":{"source_kind":"inline",
                    "source_files":[{"path":"main.py","source":source}],"definition":{"action":"set","value":{
                        "family_contract":current_family_contract(standard=True,
                            parameter_sources={"width":{"source_kind":"fixed","value":0.1}}).model_dump(mode="json",by_alias=True),
                        "geometry_bindings":[{"binding_key":"body_geometry","source":{"source_kind":"draft","draft_ref":geometry_ref},
                            "expected_resource_kind":"modeling_function","geometry_purpose":"shape_fragment"}]}}}})
            family_ref = resources.family_authoring.draft_reference(scope,family.draft)
            cases = tuple(FamilyPreviewCase(case_key=key, invocation=ProductRealizationInvocation(
                realization_kind=kind, length_unit={"name":"METRE"}, product_origin=origin), expectation=FamilyCaseExpectation(expected_status="SOLVED"))
                for key,kind,origin in (("nominal","standard_model",(0.,0.,0.)),("translated","standard_model",(2.,3.,4.)),("self","self_geometry",(0.,0.,0.))))
            prepared_family = await resources.family_authoring.prepare_draft_preview(scope,draft_ref=family_ref,
                invocations=tuple(item.invocation for item in cases))
            checked_family = await resources.realization_preview.preview_cases(scope, **prepared_family, cases=cases,
                nominal_scenario_key="nominal", retain_for_inspection=True, request_identity="check-family")
            assert checked_family.report.checks_passed
            examples = (AuthoringPreviewSelection(operation_alias=checked.operation_alias),
                        AuthoringPreviewSelection(operation_alias=checked_family.operation_alias))
            if not geometry_first:
                # Reject after Geometry and Family inserts, then prove the entire
                # publication transaction rolled back and its claim was released.
                original_insert = PostgresProductFamilyAuthoringRepositoryMixin._insert_product_family_bundle

                async def duplicate_family_insert(cls, session, bundle):
                    await original_insert(session, bundle)
                    return await original_insert(session, bundle)

                rejected_identity = "publish-family-integrity-rejected"
                with monkeypatch.context() as rejected:
                    rejected.setattr(PostgresProductFamilyAuthoringRepositoryMixin,
                        "_insert_product_family_bundle", classmethod(duplicate_family_insert))
                    with pytest.raises(AuthoringDraftError) as failed:
                        await resources.family_authoring.publish_draft(scope, draft_ref=family_ref,
                            expected_edit_token=1, preview_examples=examples,
                            request_identity=rejected_identity)
                assert failed.value.code == "AUTHORING_PUBLICATION_COMMIT_REJECTED"
                assert failed.value.phase == "commit_fence"
                assert failed.value.operation_alias is not None
                assert "INSERT" not in str(failed.value) and "product_family_versions" not in str(failed.value)
                rejected_operation_id = AuthoringDraftService.operation_identity(
                    scope, "ifc_authoring_publish", rejected_identity)
                rejected_operation = await runtime.repository.get_operation(scope, rejected_operation_id)
                assert rejected_operation is not None
                assert rejected_operation.operation.status is CriticalOperationStatus.FAILED
                assert rejected_operation.operation.error_code == failed.value.code
                assert rejected_operation.operation.error_phase.value == "commit_fence"
                assert rejected_operation.catalog_entry.alias == failed.value.operation_alias
                failed_catalog = await runtime.repository.list_operation_catalog(scope,
                    operation_kind=CriticalOperationKind.AUTHORING_PUBLISH,
                    tool_name="ifc_authoring_publish", outcome=CriticalOperationStatus.FAILED)
                assert any(entry.operation_id == rejected_operation_id
                    and entry.alias == failed.value.operation_alias for entry in failed_catalog.entries)
                async with admin.connect() as connection:
                    claimed = (await connection.execute(select(
                        CriticalOperationModel.authoring_execution_owner,
                        CriticalOperationModel.authoring_execution_token_sha256,
                    ).where(CriticalOperationModel.operation_id == rejected_operation_id))).one()
                    assert claimed == (None, None)
                    assert (await connection.execute(select(GeometryResourceVersionModel.resource_version_id)
                        .where(GeometryResourceVersionModel.resource_id == geometry.draft.root_id))).first() is None
                    assert (await connection.execute(select(ProductFamilyVersionModel.family_version_id)
                        .where(ProductFamilyVersionModel.family_id == family.draft.root_id))).first() is None
                with pytest.raises(AuthoringDraftError) as replay_failure:
                    await resources.family_authoring.publish_draft(scope, draft_ref=family_ref,
                        expected_edit_token=1, preview_examples=examples,
                        request_identity=rejected_identity)
                assert replay_failure.value.operation_alias == failed.value.operation_alias
                # A different identity with identical business inputs is allowed
                # after the definite failure, but an unresolved admission blocks
                # both this Run and another Run of the same Task.
                held_identity = "publish-family"
                with monkeypatch.context() as unclaimed:
                    unclaimed.setattr(runtime.repository, "claim_authoring_execution",
                        AsyncMock(return_value=None))
                    with pytest.raises(AuthoringDraftError) as pending:
                        await resources.family_authoring.publish_draft(scope, draft_ref=family_ref,
                            expected_edit_token=1, preview_examples=examples,
                            request_identity=held_identity)
                assert pending.value.code == "PUBLICATION_RECONCILIATION_REQUIRED"
                held_id = AuthoringDraftService.operation_identity(scope, "ifc_authoring_publish", held_identity)
                held = await runtime.repository.get_authoring_execution_state(scope, held_id,
                    operation_kind=CriticalOperationKind.AUTHORING_PUBLISH)
                assert held is not None and held.operation.status is CriticalOperationStatus.PREPARED
                assert held.execution_owner is None
                with monkeypatch.context() as blocked:
                    for name in ("build_fixed", "validate_function", "evaluate_function_interfaces", "instantiate_function"):
                        blocked.setattr(resources.geometry_runtime, name,
                            AsyncMock(side_effect=AssertionError("blocked publication must not build Geometry")))
                    for name in ("realize_geometry", "precheck_batch", "instantiate_batch"):
                        blocked.setattr(resources.realization_runtime, name,
                            AsyncMock(side_effect=AssertionError("blocked publication must not realize Family")))
                    with pytest.raises(AuthoringDraftError) as concurrent:
                        await resources.family_authoring.publish_draft(scope, draft_ref=family_ref,
                            expected_edit_token=1, preview_examples=examples,
                            request_identity="publish-before-reconcile")
                assert concurrent.value.code == "PUBLICATION_RECONCILIATION_REQUIRED"
                concurrent_id = AuthoringDraftService.operation_identity(
                    scope, "ifc_authoring_publish", "publish-before-reconcile")
                assert await runtime.repository.get_authoring_execution_state(scope, concurrent_id,
                    operation_kind=CriticalOperationKind.AUTHORING_PUBLISH) is None
                async with admin.begin() as connection:
                    prior = dict((await connection.execute(select(RunModel.__table__)
                        .where(RunModel.run_id == run))).mappings().one())
                    prior.update(run_id=f"run-{uuid4().hex}", idempotency_key=f"parallel-{uuid4().hex}",
                        status="completed")
                    await connection.execute(insert(RunModel).values(**prior))
                competing_scope = scope.model_copy(update={"run_id": prior["run_id"]})
                competing_id = AuthoringDraftService.operation_identity(
                    competing_scope, "ifc_authoring_publish", "publish-other-run")
                proposal = held.operation.model_copy(update={"operation_id": competing_id,
                    "run_id": competing_scope.run_id,
                    "idempotency_key": "authoring-publish:publish-other-run"})
                existing = await runtime.repository.begin_authoring_publication(competing_scope,
                    operation=proposal, expected_drafts=(family.draft, geometry.draft))
                assert existing.operation_id == held_id and existing.run_id == scope.run_id
                assert await runtime.repository.get_authoring_execution_state(competing_scope, competing_id,
                    operation_kind=CriticalOperationKind.AUTHORING_PUBLISH) is None
            result = await resources.family_authoring.publish_draft(scope,draft_ref=family_ref,expected_edit_token=1,
                preview_examples=examples,request_identity="publish-family")
            assert result.family_version.version.version_no == 1 and len(result.geometry_versions) == 1
            if not geometry_first:
                assert result.operation.operation.operation_id == held_id
                with pytest.raises(AuthoringDraftError) as still_failed:
                    await resources.family_authoring.publish_draft(scope, draft_ref=family_ref,
                        expected_edit_token=1, preview_examples=examples,
                        request_identity=rejected_identity)
                assert still_failed.value.code == failed.value.code
                assert still_failed.value.operation_alias == failed.value.operation_alias
            if not geometry_first:
                published_geometry = await resources.geometry_authoring.publish_draft(scope,draft_ref=geometry_ref,
                    expected_edit_token=1,
                    request_identity="publish-geometry-after-family")
                assert published_geometry.family_version is None
                assert published_geometry.operation.operation.detail.root_kind == "geometry"
                assert published_geometry.operation.operation.operation_id != result.operation.operation.operation_id
            assert result.geometry_versions == published_geometry.geometry_versions
            assert result.family_version.geometry_bindings[0].geometry_resource_version_id == result.geometry_versions[0].version.resource_version_id
            with monkeypatch.context() as no_rebuild:
                for name in ("build_fixed", "validate_function", "evaluate_function_interfaces", "instantiate_function"):
                    no_rebuild.setattr(resources.geometry_runtime, name,
                        AsyncMock(side_effect=AssertionError("exact publication must reuse Geometry")))
                for name in ("realize_geometry", "precheck_batch", "instantiate_batch"):
                    no_rebuild.setattr(resources.realization_runtime, name,
                        AsyncMock(side_effect=AssertionError("exact publication must reuse Family")))
                repeated = await resources.family_authoring.publish_draft(scope,draft_ref=family_ref,
                    expected_edit_token=1, preview_examples=examples, request_identity="publish-again")
            assert repeated.family_version == result.family_version and repeated.geometry_versions == result.geometry_versions
            assert repeated.operation.operation.operation_id != result.operation.operation.operation_id
            replayed = await resources.family_authoring.publish_draft(scope,draft_ref=family_ref,expected_edit_token=1,
                preview_examples=examples,request_identity="publish-family")
            assert replayed.operation == result.operation
            assert (await resources.geometry_inspection.read(scope, checked.report.cases[0].inspection_ref)).geometry.sha256 == loaded.geometry.sha256
            recovered = await runtime.repository.get_operation(scope, result.operation.operation.operation_id)
            assert recovered == result.operation
            # A later Run of this Task receives its own receipt, without needing
            # the old Run's aliases or inserting another immutable version.
            async with admin.begin() as connection:
                prior = dict((await connection.execute(select(RunModel.__table__).where(RunModel.run_id == run))).mappings().one())
                prior.update(run_id=f"run-{uuid4().hex}", idempotency_key=f"later-{uuid4().hex}", status="completed")
                await connection.execute(insert(RunModel).values(**prior))
            later_scope = scope.model_copy(update={"run_id":prior["run_id"]})
            later = await resources.family_authoring.publish_draft(later_scope,draft_ref=family_ref,
                expected_edit_token=1,request_identity="publish-later-run")
            assert later.family_version == result.family_version and later.geometry_versions == result.geometry_versions
            assert later.operation.operation.run_id == later_scope.run_id
            assert later.operation.operation.operation_id != result.operation.operation.operation_id
            assert await runtime.repository.get_operation(later_scope,later.operation.operation.operation_id) == later.operation
            original = await runtime.repository.get_operation_by_locator(later_scope,
                origin_run_id=scope.run_id, operation_id=result.operation.operation.operation_id)
            assert original.operation.operation_id == result.operation.operation.operation_id
            for kind, version_id in (("family", result.family_version.version.family_version_id),
                                     ("geometry", result.geometry_versions[0].version.resource_version_id)):
                origin = await runtime.repository.get_authoring_publication_for_version(later_scope,
                    root_kind=kind, version_id=version_id)
                assert origin is not None and origin.operation.status.value == "succeeded"
            family_alias = resources.family_catalog.version_ref(identity, result.family_version)
            if geometry_first:
                # Observe and query the formal asset, with all construction paths disabled.
                # A new Run needs neither the author's Preview alias nor a copied inspection.
                with monkeypatch.context() as frozen:
                    for name in ("build_fixed", "validate_function", "evaluate_function_interfaces", "instantiate_function"):
                        frozen.setattr(resources.geometry_runtime, name, AsyncMock(side_effect=AssertionError("Formal asset read must not rebuild")))
                    frozen.setattr(resources.realization_runtime, "realize", AsyncMock(side_effect=AssertionError("Formal asset read must not solve")))
                    for name in ("realize_geometry", "precheck_batch", "instantiate_batch"):
                        frozen.setattr(resources.realization_runtime, name, AsyncMock(side_effect=AssertionError("Formal asset read must not build")))
                    observed = await resources.geometry_preview.preview_family_standard_model(
                        later_scope, family_alias, GeometryInspectionModelObservation())
                    assert observed.preview.payload.startswith(b"\x89PNG\r\n\x1a\n")
                    measured = await resources.geometry_query.fresh_family_standard_model(
                        later_scope, family_version_alias=family_alias,
                        source_code="def query(ctx):\n    return [{'volume_m3':ctx.private_geometry().Volume()}, {'source':ctx.source()}]\n",
                        request_identity="measure-formal", page_size=1)
                    assert measured.family_version_id == result.family_version.version.family_version_id
                    assert measured.results[0]["volume_m3"] == pytest.approx(0.001)
                    paged = await resources.geometry_query.continue_page(later_scope, measured.next_cursor)
                    assert paged.family_version_id == measured.family_version_id
                    assert paged.results[0]["source"]["unit"] == "METRE"
                for key, body, fail in (("fixed", "return cq.Workplane('XY').box(0.2,0.1,0.1)", False),
                                         ("broken", "raise ValueError('deliberate build failure')", True)):
                    fixed = await resources.geometry_authoring.save_draft_from_aliases(scope,
                        request_identity=f"save-{key}", declaration={"mode":"create",
                            "display_name":key, "usage_instructions":{"action":"set","value":"A fixed local solid in metres."},
                            "source":{"source_kind":"mapped_model", "source_files":[{
                                "path":"main.py", "source":f"import cadquery as cq\ndef build(ctx):\n    {body}\n"}]}})
                    fixed_ref = resources.geometry_authoring.draft_reference(scope, fixed.draft)
                    if fail:
                        with pytest.raises(AuthoringDraftError) as failure:
                            await resources.geometry_authoring.publish_draft(scope,draft_ref=fixed_ref,
                                expected_edit_token=1,request_identity="publish-broken")
                        assert failure.value.operation_alias is not None
                        assert await resources.geometry_resources.get_latest_version(identity,resource_id=fixed.draft.root_id) is None
                        with pytest.raises(AuthoringDraftError) as replay:
                            await resources.geometry_authoring.publish_draft(scope,draft_ref=fixed_ref,
                                expected_edit_token=1,request_identity="publish-broken")
                        assert replay.value.operation_alias == failure.value.operation_alias
                    else:
                        formal = await resources.geometry_authoring.publish_draft(scope,draft_ref=fixed_ref,
                            expected_edit_token=1,request_identity="publish-fixed")
                        assert formal.geometry_versions[0].version.manifest["geometry_summary"]["volume_m3"] == pytest.approx(0.002)
        other = await _open_context(runtime, subject=f"draft-other-{uuid4().hex}", session_name="Other authoring scope")
        other_owner,other_run = await _task_owner_and_run(admin,other)
        other_scope = InvocationScope(identity=other,owner_id=other_owner,run_id=other_run,execution_mode=ExecutionMode.DIRECT_MCP,catalog_frontier=None)
        with bind_tenant_execution_context(other), pytest.raises(AuthoringDraftError):
            await resources.family_authoring.read_draft(other_scope,draft_ref=family_ref)
        if geometry_first:
            from CAD2BIM_backend.application.geometry_query_service import GeometryQueryApplicationError
            with bind_tenant_execution_context(other), pytest.raises(GeometryQueryApplicationError, match="reference"):
                await resources.geometry_query.fresh_family_standard_model(other_scope,
                    family_version_alias=family_alias,source_code="def query(ctx):\n    return []\n",
                    request_identity="other-formal")
        sibling = await _open_context(runtime,subject=subject,session_name="Same owner, different Task")
        sibling_owner,sibling_run = await _task_owner_and_run(admin,sibling)
        sibling_scope = scope.model_copy(update={"identity":sibling,"owner_id":sibling_owner,"run_id":sibling_run})
        with bind_tenant_execution_context(sibling):
            assert await runtime.repository.get_geometry_draft(sibling_scope,resource_id=geometry.draft.root_id) is None
            assert await runtime.repository.get_product_family_draft(sibling_scope,family_id=family.draft.root_id) is None
    finally:
        if resources is not None:
            await resources.close()
        await runtime.close()
        await admin.dispose()
        _drop_logins(DATABASE_URL,logins)
