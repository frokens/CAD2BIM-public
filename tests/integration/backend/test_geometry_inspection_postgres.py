"""One focused real Worker/Operation/Artifact/Query slice on an isolated database."""

from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
from uuid import uuid4

import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.data_runtime_service import DirectScopeResolver, DirectSessionService
from CAD2BIM_backend.application.geometry_query_service import GeometryQueryApplicationError
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.domain.geometry_inspection import (
    GeometrySectionObservation, GeometrySectionPlane, InspectionFragmentLocalCoordinates,
    InspectionFragmentSelection, InspectionResultCoordinates, InspectionWholeSelection,
)
from CAD2BIM_backend.domain.geometry_inspection_asset import GeometryInspectionModelObservation
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyContract,
)
from CAD2BIM_backend.domain.product_family_authoring import (
    FamilyDraftInlineCreateCommand, ProductFamilyAuthoringSourceFile,
)
from CAD2BIM_backend.domain.product_realization import ProductRealizationInvocation
from CAD2BIM_backend.domain.text_source import SourceEdit, TextSourceView
from CAD2BIM_backend.ifc_application_resources import create_authoring_application_resources
from CAD2BIM_backend.infrastructure.data_query import CursorCodec
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.infrastructure.worker_runtime.capacity import HostWorkerCapacity
from tests.integration.backend.test_geometry_query_postgres import (
    _create_logins, _drop_logins, _runtime, _runtime_url,
)


pytestmark = [pytest.mark.integration, pytest.mark.network, pytest.mark.asyncio]
TEST_DATABASE_URL = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
AREA = math.pi * (0.04**2 - 0.036**2)
RING = "import cadquery as cq\n\ndef build(ctx):\n    return cq.Workplane('XY').circle(0.04).circle(0.036).extrude(1.0)\n"


async def test_actual_sample_is_retained_once_and_queried_after_restart(tmp_path: Path, monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    logins = _create_logins(TEST_DATABASE_URL)
    database = _runtime(_runtime_url(TEST_DATABASE_URL, logins["mcp"]))
    graph = restarted = None
    evidence = Path(os.getenv("CAD2BIM_INSPECTION_EVIDENCE_ROOT", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    try:
        suffix = uuid4().hex
        sessions = DirectSessionService(database.repository, database.identity, f"inspection-{suffix}", bind_tenant_execution_context)
        resolver = DirectScopeResolver(database.repository, database.identity, f"inspection-{suffix}", bind_tenant_execution_context)
        opened = await sessions.open_direct_session(f"Geometry inspection {suffix}")
        scope = await resolver.resolve(opened.binding.direct_session_alias, command_id=f"inspection-{suffix}")
        storage = LocalStorage(tmp_path / "storage", max_size_bytes=128 * 1024 * 1024)
        artifacts = ArtifactService(database.repository, storage)
        codec = CursorCodec(base64.urlsafe_b64encode(b"i" * 32).rstrip(b"=").decode())
        capacity = HostWorkerCapacity(max_concurrent_workers=1, capacity_root=tmp_path / "capacity")
        def compose():
            return create_authoring_application_resources(repository=database.repository, storage=storage,
                artifacts=artifacts, cursor_codec=codec, capacity=capacity)
        graph = compose()
        with bind_tenant_execution_context(scope.identity):
            await storage.verify_ready()
            await graph.geometry_runtime.verify_ready()
            written = await graph.geometry_authoring.save_draft_from_aliases(scope,
                request_identity=f"sample-write-{suffix}", declaration={"mode":"create",
                "display_name":"Inspection hollow sample", "usage_instructions":{"action":"set","value":"Build a hollow tube in metres."},
                "source":{"source_kind":"modeling_function", "source_files":[{"path":"main.py",
                    "source":RING.replace("extrude(1.0)", "extrude(ctx.param('length_m'))")},
                    {"path":"interfaces.py", "source":"def evaluate(ctx):\n    pass\n"}],
                    "parameter_schema":{"action":"set","value":{"type":"object", "properties":{"length_m":{"type":"number","exclusiveMinimum":0}},
                        "required":["length_m"], "additionalProperties":False}}}})
            source_ref = graph.geometry_authoring.draft_reference(scope, written.draft)
            prepared = await graph.geometry_authoring.prepare_draft_preview(scope, draft_ref=source_ref)
            calls = []
            original_build = graph.geometry_preview._geometry_runtime.instantiate_function
            async def record_build(*args, **kwargs):
                result = await original_build(*args, **kwargs)
                calls.append(result)
                return result
            monkeypatch.setattr(graph.geometry_preview._geometry_runtime, "instantiate_function", record_build)
            datum = RigidTransform3D(translation_m=(5,7,1), rotation=((0,0,1),(0,1,0),(-1,0,0)))
            # The ordinary sample has no persistent Operation or reusable model.
            before_ordinary = await database.repository.list_operation_catalog(scope, page_size=50)
            ordinary = await graph.geometry_preview.preview_loaded(scope, **prepared, parameters={"length_m":1.0}, datum_transform=datum)
            assert ordinary.images[0][1].payload.startswith(b"\x89PNG")
            before = await database.repository.list_operation_catalog(scope, page_size=50)
            assert len(before.entries) == len(before_ordinary.entries)
            saved_complete = database.repository.complete_geometry_inspection_operation
            response_lost = False
            async def complete_then_lose(*args, **kwargs):
                nonlocal response_lost
                result = await saved_complete(*args, **kwargs)
                if not response_lost:
                    response_lost = True
                    raise TimeoutError("test: response lost after the real terminal commit")
                return result
            monkeypatch.setattr(database.repository, "complete_geometry_inspection_operation", complete_then_lose)
            retained = await graph.geometry_preview.preview_loaded(scope, **prepared, parameters={"length_m":1.0}, datum_transform=datum,
                retain_for_inspection=True, request_identity=f"sample-retain-{suffix}")
            monkeypatch.setattr(database.repository, "complete_geometry_inspection_operation", saved_complete)
            inspection_ref = retained.report.cases[0].inspection_ref
            assert retained.images[0][1].payload.startswith(b"\x89PNG") and retained.operation_alias and response_lost
            assert len(calls) == 2
            original = await graph.geometry_inspection.read(scope, inspection_ref)
            assert original.geometries[("fragment","sample")].payload == calls[-1].sample_geometries[0].payload
            after = await database.repository.list_operation_catalog(scope, page_size=50)
            assert len(after.entries) == len(before.entries) + 1
            entry = next(item for item in after.entries if item.alias == retained.operation_alias)
            operation = await database.repository.get_operation(scope, entry.operation_id)
            listed = await graph.geometry_inspection.operation_result(scope, operation)
            details = await graph.geometry_inspection.operation_result(scope, operation, include_request=True)
            assert listed.result_kind == details.result_kind == "case_report"
            assert json.loads(details.request_json)["cases"][0]["parameters"] == {"length_m":1.0}
            (evidence / "sample-preview.png").write_bytes(retained.images[0][1].payload)
            await graph.close()
            restarted = compose()
            async def no_rebuild(*_args, **_kwargs):
                pytest.fail("retained inspection re-executed the modeling function")
            monkeypatch.setattr(restarted.geometry_preview._geometry_runtime, "evaluate_function_interfaces", no_rebuild)
            monkeypatch.setattr(restarted.geometry_preview._geometry_runtime, "instantiate_function", no_rebuild)
            recovered = await restarted.geometry_preview.preview_loaded(scope, **prepared, parameters={"length_m":1.0}, datum_transform=datum,
                retain_for_inspection=True, request_identity=f"sample-retain-{suffix}")
            assert recovered.report.cases[0].inspection_ref == inspection_ref and recovered.operation_alias == retained.operation_alias
            observation = GeometrySectionObservation(plane=GeometrySectionPlane(origin_m=(5.5,7,1), normal=(1,0,0)), offsets_m=(-0.001,0,0.001))
            section = await restarted.geometry_preview.preview_inspection(scope, inspection_ref=inspection_ref,
                selection=InspectionWholeSelection(), coordinates=InspectionResultCoordinates(), observation=observation)
            assert all(m.status == "computed" and m.material_area_sum_m2 == pytest.approx(AREA, rel=1e-7) for m in section.section.measurements)
            assert len(section.section.measurements[1].objects[0].material_regions[0].boundaries) == 2
            (evidence / "sample-sections.json").write_text(section.section.model_dump_json(indent=2, exclude={"images"}))
            for img in section.section.images:
                (evidence / f"sample-section-{img.section_index}.png").write_bytes(img.preview.payload)
            plane = {"origin_m":[5.5,7,1],"normal":[1,0,0]}
            query = "def query(ctx):\n    return ctx.sections(" + repr(plane) + ", offsets_m=(-0.001,0.0,0.001))['measurements']\n"
            page = await restarted.geometry_query.fresh_inspection(scope, inspection_ref=inspection_ref,
                selection=InspectionWholeSelection(), coordinate_space=InspectionResultCoordinates(), source_code=query,
                request_identity=f"section-query-{suffix}", page_size=2)
            assert page.total_count == 3 and page.returned_count == 2 and page.next_cursor
            following = await restarted.geometry_query.continue_page(scope, page.next_cursor)
            assert following.returned_count == 1 and following.results[0]["material_area_sum_m2"] == pytest.approx(AREA)
            attempt_id = await restarted.geometry_query.resolve_attempt_alias(scope, page.attempt_alias)
            source = await restarted.geometry_query.read_source(scope, attempt_id, TextSourceView(view_mode="full"))
            assert source.sha256 == page.source_sha256
            with pytest.raises(GeometryQueryApplicationError) as failure:
                await restarted.geometry_query.fresh_inspection(scope, inspection_ref=inspection_ref,
                    selection=InspectionFragmentSelection(fragment_key="sample"),
                    coordinate_space=InspectionFragmentLocalCoordinates(fragment_key="sample"),
                    source_code="def query(ctx):\n    raise ValueError('repair-me')\n", request_identity=f"failed-query-{suffix}")
            parent_id = await restarted.geometry_query.resolve_attempt_alias(scope, failure.value.attempt_alias)
            repaired = await restarted.geometry_query.repair(scope, parent_id,
                edits=(SourceEdit("raise ValueError('repair-me')", "return [ctx.source(), ctx.fragments()]"),),
                request_identity=f"repair-query-{suffix}")
            assert repaired.inspection_ref == inspection_ref and repaired.selection.fragment_key == "sample"
            assert repaired.coordinate_space.coordinate_space == "fragment_local"
    finally:
        for resources in (graph,restarted):
            if resources is not None:
                await resources.close()
        await database.close()
        _drop_logins(TEST_DATABASE_URL, logins)


def _family_retention_command(binding, request_identity):
    contract = ProductFamilyContract.model_validate({"product_structure_kind":"single_product",
        "standard_model":{},
        "components":[{"component_key":"body","ifc_class":"IfcValve"}], "geometry_fragments":[
            {"fragment_key":key,"owner_component_key":"body",
             "fixed_local_transform":{"translation_m":[0,0,2 if key=="b" else 0]},
             "binding_options":[{"binding_key":"tube","parameter_sources":{"overrides": {}},
                 "uniform_scale_source":{"source_kind":"fixed","value":2.0 if key=="b" else 1.0}}]}
            for key in ("a","b")]})
    source = "def realize(ctx):\n    return {'status':'SOLVED','components':[{'component_key':'body'}],'fragments':[{'fragment_key':'a','binding_key':'tube'},{'fragment_key':'b','binding_key':'tube','local_transform':{'translation_m':[0,0,2]},'uniform_scale':2.0}]}\n\ndef standard_model(ctx):\n    return realize(ctx)\n"
    return FamilyDraftInlineCreateCommand(mode="create",
        display_name="Inspection two fragment family", purpose="Exercise actual scaled fragment retention.",
        usage_instructions={"action":"set","value":"Place both declared tube fragments at the supplied product origin."},
        source={"source_kind":"inline", "definition":{"action":"set","value":{"family_contract":contract,"geometry_bindings":[binding]}},
            "source_files":[ProductFamilyAuthoringSourceFile(path="main.py", source=source)]})


async def test_actual_family_retains_same_execution_scaled_local_fragments(tmp_path: Path, monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    invocation = ProductRealizationInvocation(product_origin=(5,7,1), length_unit=IfcLengthUnit())
    # Validate the entire command before creating roles, resources or Workers.
    _family_retention_command({"binding_key":"tube", "source":{"source_kind":"draft","draft_ref":"fixture-draft"},
        "expected_resource_kind":"mapped_model","geometry_purpose":"product_component"}, "fixture-preflight")
    logins = _create_logins(TEST_DATABASE_URL)
    database = _runtime(_runtime_url(TEST_DATABASE_URL, logins["mcp"]))
    graph = None
    evidence = Path(os.getenv("CAD2BIM_INSPECTION_EVIDENCE_ROOT", str(tmp_path)))
    evidence.mkdir(parents=True,exist_ok=True)
    try:
        suffix = uuid4().hex
        sessions = DirectSessionService(database.repository,database.identity,f"inspection-family-{suffix}",bind_tenant_execution_context)
        resolver = DirectScopeResolver(database.repository,database.identity,f"inspection-family-{suffix}",bind_tenant_execution_context)
        opened = await sessions.open_direct_session(f"Inspection Family {suffix}")
        scope = await resolver.resolve(opened.binding.direct_session_alias,command_id=f"family-{suffix}")
        storage = LocalStorage(tmp_path / "storage",max_size_bytes=128*1024*1024)
        graph = create_authoring_application_resources(repository=database.repository,storage=storage,
            artifacts=ArtifactService(database.repository,storage),cursor_codec=CursorCodec(base64.urlsafe_b64encode(b"i"*32).rstrip(b"=").decode()),
            capacity=HostWorkerCapacity(max_concurrent_workers=1,capacity_root=tmp_path / "capacity"))
        with bind_tenant_execution_context(scope.identity):
            mapped = await graph.geometry_authoring.save_draft_from_aliases(scope, request_identity=f"mapped-write-{suffix}",
                declaration={"mode":"create", "display_name":"Inspection mapped tube",
                    "usage_instructions":{"action":"set","value":"A one metre hollow tube in local metres."},
                    "source":{"source_kind":"mapped_model","source_files":[{"path":"main.py","source":RING}]}})
            mapped_ref = graph.geometry_authoring.draft_reference(scope, mapped.draft)
            command = _family_retention_command({"binding_key":"tube", "source":{"source_kind":"draft","draft_ref":mapped_ref},
                "expected_resource_kind":"mapped_model","geometry_purpose":"product_component"}, f"family-write-{suffix}")
            family = await graph.family_authoring.save_draft_from_aliases(scope,
                declaration=command.model_dump(mode="json",by_alias=True), request_identity=f"family-write-{suffix}")
            family_ref = graph.family_authoring.draft_reference(scope, family.draft)
            prepared_family = await graph.family_authoring.prepare_draft_preview(scope, draft_ref=family_ref, invocations=(invocation,))
            prepared_family.pop("requests")
            executions = []
            actual_build = graph.realization_preview._runtime.instantiate_batch
            async def tracked_build(*args, **kwargs):
                async for execution in actual_build(*args, **kwargs):
                    executions.append(execution)
                    yield execution
            monkeypatch.setattr(graph.realization_preview._runtime, "instantiate_batch", tracked_build)
            result = await graph.realization_preview.preview_loaded(scope, **prepared_family,
                retain_for_inspection=True,request_identity=f"family-retain-{suffix}")
            assert result.inspection_ref and result.operation_alias and result.preview is not None
            loaded = await graph.geometry_inspection.read(scope,result.inspection_ref)
            assert len(executions) == 1 and loaded.geometry.payload == executions[0].geometry.payload
            assert loaded.manifest.realization == executions[0].realization
            assert {m.object_key for m in loaded.manifest.geometries if m.role=="fragment"} == {"a","b"}
            for key, geometry in executions[0].instance_geometries.items():
                assert loaded.geometries[("fragment",key)].payload == geometry.payload
            async def forbidden_rebuild(*_args, **_kwargs):
                raise AssertionError("Saved Family inspection must not execute the original program")
            monkeypatch.setattr(graph.realization_preview._runtime, "realize_batch", forbidden_rebuild)
            monkeypatch.setattr(graph.realization_preview._runtime, "instantiate_batch", forbidden_rebuild)
            scaled = await graph.geometry_preview.preview_inspection(scope,inspection_ref=result.inspection_ref,
                selection=InspectionFragmentSelection(fragment_key="b"),coordinates=InspectionFragmentLocalCoordinates(fragment_key="b"),
                observation=GeometrySectionObservation(plane=GeometrySectionPlane(origin_m=(0,0,1),normal=(0,0,1))))
            assert scaled.section.measurements[0].material_area_sum_m2 == pytest.approx(AREA*4,rel=1e-7)
            (evidence / "family-fragment-sections.json").write_text(scaled.section.model_dump_json(indent=2,exclude={"images"}))
            for img in scaled.section.images:
                (evidence / f"family-fragment-section-{img.section_index}.png").write_bytes(img.preview.payload)
    finally:
        if graph is not None:
            await graph.close()
        await database.close()
        _drop_logins(TEST_DATABASE_URL,logins)
