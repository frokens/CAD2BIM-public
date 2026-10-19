"""Unit interpretation belongs to the entry loader, before the Runtime boundary."""

import pytest
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

from CAD2BIM_backend.application.product_realization_input_service import (
    ProductRealizationInputError,
    ProductRealizationInputService,
    normalize_product_realization_invocation,
    normalize_product_realization_validation_scenario,
)
from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogIntegrityError
from CAD2BIM_backend.domain.product_family import ProductFamilyContract
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationInvocation, ProductRealizationRequest, ProductRealizationValidationScenario,
)
from tests.unit.backend.test_product_realization_preview_service import _request, _scope
from tests.unit.backend.test_product_family_catalog_service import _family_bundle


def test_same_name_sources_expand_once_override_and_never_fallback():
    from tests.unit.backend.product_family_fixtures import current_family_document
    document = current_family_document(public_parameters=[{"parameter_kind": "number", "parameter_key": "width", "minimum": 0, "maximum": 1}],
        solved_parameters=[{"parameter_kind": "number", "parameter_key": "width_solved", "minimum": 0, "maximum": 1}])
    option = document["geometry_fragments"][0]["binding_options"][0]
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "width": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.2},
        "fixed": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.3}}}
    option["parameter_sources"] = {"default_source": "public_parameter", "overrides": {"fixed": {"source_kind": "fixed", "value": 0.4}}}
    normalized = ProductRealizationInputService.normalize_contract_defaults(ProductFamilyContract.model_validate(document), {"body_geometry": schema})
    sources = normalized.geometry_fragments[0].binding_options[0].parameter_sources
    assert sources.default_source is None
    assert sources.overrides["width"].parameter_key == "width" and sources.overrides["fixed"].value == 0.4
    assert ProductRealizationInputService.normalize_contract_defaults(normalized, {"body_geometry": schema}) == normalized
    option["parameter_sources"]["default_source"] = "solved_parameter"
    with pytest.raises(ProductRealizationInputError, match="no same-name solved_parameter.*width"):
        ProductRealizationInputService.normalize_contract_defaults(ProductFamilyContract.model_validate(document), {"body_geometry": schema})
    option["parameter_sources"] = {"overrides": {"fixed": {"source_kind": "fixed", "value": 2}}}
    with pytest.raises(ValueError, match="fixed"):
        ProductRealizationInputService.normalize_contract_defaults(ProductFamilyContract.model_validate(document), {"body_geometry": schema})
    option["parameter_sources"] = {"overrides": {"extra": {"source_kind": "fixed", "value": 1}}}
    with pytest.raises(ProductRealizationInputError, match="unknown Geometry"):
        ProductRealizationInputService.normalize_contract_defaults(ProductFamilyContract.model_validate(document), {"body_geometry": schema})


async def _published_loader(geometry_data=None):
    from tests.unit.backend.test_product_family_authoring_service import _service, _scope as authoring_scope
    from tests.unit.backend.test_system_family_publication_service import _family

    service, _, _, storage, resources, _, _, geometry = _service(geometry_data)
    family, contents = _family(geometry, "published-loader-family")
    for asset, body in zip(family.assets, contents, strict=True):
        await service._family_assets.write_exact_asset(family.family, asset, body)
    return service._realization_inputs, authoring_scope(), family, storage, resources, contents


@pytest.mark.asyncio
async def test_published_loader_reads_exact_source_and_reauthorizes_interfaces_in_scope():
    from tests.unit.backend.test_product_family_authoring_service import _geometry, _interfaces, BREP
    from CAD2BIM_backend.domain.product_realization import ResolvedMappedGeometryBinding

    service, scope, family, storage, resources, contents = await _published_loader(_geometry(interfaces=_interfaces()))
    request = await service.load_published_request(scope, family_version=family,
        invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}))
    assert request.source_bundle == contents[0]
    assert isinstance(request.geometry_bindings[0], ResolvedMappedGeometryBinding)
    assert request.geometry_bindings[0].geometry.payload == BREP
    assert request.geometry_bindings[0].interfaces == _interfaces()
    assert resources.contexts and all(value == scope.identity for value in resources.contexts)
    assert any(key.asset_role == "realization_source_bundle" for key in storage.read_keys)


@pytest.mark.asyncio
async def test_published_loader_reads_exact_function_source_and_schema():
    from tests.unit.backend.test_product_family_authoring_service import _function_geometry
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
    from CAD2BIM_backend.domain.product_realization import ResolvedModelingFunctionBinding

    geometry = _function_geometry()
    service, scope, family, _, _, _ = await _published_loader(geometry)
    request = await service.load_published_request(scope, family_version=family,
        invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}))
    binding = request.geometry_bindings[0]
    assert isinstance(binding, ResolvedModelingFunctionBinding)
    assert binding.source_bundle == geometry[1][GeometryResourceAssetRole.SOURCE_BUNDLE]
    assert binding.parameter_schema == {"additionalProperties": False, "properties": {}, "required": [], "type": "object"}


@pytest.mark.asyncio
async def test_numeric_closure_reads_only_exact_source_and_fixed_interfaces(monkeypatch):
    from unittest.mock import AsyncMock
    from tests.unit.backend.test_product_family_authoring_service import _geometry, _interfaces
    service, scope, family, storage, _, _ = await _published_loader(_geometry(interfaces=_interfaces()))
    forbidden_verification = AsyncMock(side_effect=AssertionError("numeric preparation read Family standard geometry"))
    forbidden_geometry = AsyncMock(side_effect=AssertionError("numeric preparation read mapped BREP"))
    monkeypatch.setattr(service._family_assets, "verify_bundle", forbidden_verification)
    monkeypatch.setattr(service, "_required_geometry_asset_pair", forbidden_geometry)
    before = len(storage.read_keys)
    prepared = await service.prepare_published_closure(scope, family_version=family)
    invocation = ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
    normalized = prepared.normalize_invocation(invocation)
    requests = prepared.requests((normalized, normalized))
    assert requests[0].geometry_bindings[0].geometry is None
    assert requests[0].geometry_bindings[0].interfaces == _interfaces()
    assert requests[0] == requests[1] and requests[0] is not requests[1]
    assert {key.asset_role for key in storage.read_keys[before:]} == {"realization_source_bundle"}
    forbidden_verification.assert_not_called()
    forbidden_geometry.assert_not_called()


@pytest.mark.asyncio
async def test_selected_mapped_format_is_rejected_before_any_heavy_read():
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole
    from tests.unit.backend.test_product_realization_preview_service import _execution
    from CAD2BIM_backend.domain.product_realization import ProductRealizationStatus
    request = _request()
    result = _execution(ProductRealizationStatus.SOLVED).realization.model_copy(update={"execution_stage": "interface_constraints_evaluated"})
    binding = SimpleNamespace(binding_key="body_geometry", geometry_resource_version_id="selected")
    bundle = SimpleNamespace(version=SimpleNamespace(resource_version_id="selected"), assets=(
        SimpleNamespace(asset_role=GeometryResourceAssetRole.CANONICAL_GEOMETRY, asset_format="step"),))
    family = SimpleNamespace(version=SimpleNamespace(family_contract=request.family_contract), geometry_bindings=(binding,))
    service = ProductRealizationInputService(AsyncMock(), AsyncMock(), runtime=None, family_repository=AsyncMock())
    service.reauthorize_geometry_bindings = AsyncMock(return_value=(bundle,))
    service._required_geometry_asset_pair = AsyncMock(side_effect=AssertionError("heavy read before format check"))
    with pytest.raises(ProductRealizationInputError) as error:
        await service.load_geometry_materials(_scope(), family_version=family, requests=(request,), prechecks=(result,))
    assert error.value.code == "GEOMETRY_FORMAT_UNSUPPORTED"
    service._required_geometry_asset_pair.assert_not_awaited()


@pytest.mark.asyncio
async def test_numeric_closure_preserves_valid_rows_after_normal_input_rejection():
    from tests.unit.backend.test_product_family_authoring_service import _geometry
    service, scope, family, _, _, _ = await _published_loader(_geometry())
    prepared = await service.prepare_published_closure(scope, family_version=family)
    valid = ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"})
    invalid = valid.model_copy(update={"public_parameters": {"undeclared": 42}})
    with pytest.raises(ValueError):
        prepared.normalize_invocation(invalid)
    requests = prepared.requests((prepared.normalize_invocation(valid),))
    assert len(requests) == 1 and requests[0].invocation == valid


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["geometry", "source", "interface"])
async def test_published_loader_rejects_corruption_without_exposing_private_values(corrupt):
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceAssetRole

    service, scope, family, storage, resources, _ = await _published_loader()
    private = "/Users/private/source.py?token=secret"
    if corrupt == "source":
        key = next(key for key in storage.objects if key.asset_role == "realization_source_bundle")
        stored, _ = storage.objects[key]
        storage.objects[key] = (stored, b"changed-source")
    elif corrupt == "geometry":
        resources.contents[GeometryResourceAssetRole.CANONICAL_GEOMETRY] = b"changed-geometry"
    else:
        version = resources.bundle.version
        manifest = {**version.manifest, "interfaces": [{"interface_key": "outlet", "local_frame": {},
            "profile": {"kind": "circular", "diameter_m": private}}]}
        resources.bundle = resources.bundle.model_copy(update={"version": version.model_copy(update={"manifest": manifest})})
    with pytest.raises(ProductRealizationInputError) as caught:
        await service.load_published_request(scope, family_version=family,
            invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}))
    assert caught.value.code == ("PUBLISHED_SOURCE_INTEGRITY_FAILED" if corrupt == "source" else "INPUT_INVALID")
    assert private not in str(caught.value)
    if corrupt == "interface":
        assert "invalid_path=interfaces.0.profile.circular.diameter_m" in str(caught.value)
        assert "error_type=float_parsing" in str(caught.value)


@pytest.mark.parametrize("has_default", [True, False])
def test_family_geometry_defaults_are_frozen_exactly_and_never_invented(has_default):
    contract = ProductFamilyContract.model_validate({
        "product_structure_kind": "single_product",
        "components": [{"component_key": "body", "ifc_class": "IfcValve"}],
        "geometry_fragments": [{"fragment_key": "shape", "owner_component_key": "body",
            "binding_options": [{"binding_key": "body", "parameter_sources": {"overrides": {}}}]}]})
    definition = {"type": "object", "additionalProperties": False, "required": [],
        "properties": {"wall_m": {"type": "number", "minimum": 0, "maximum": 0.1, "default": 0.01}}}
    if has_default:
        definition["default"] = {}
    schemas = {"body": {"type": "object", "properties": {"section": definition}}}
    if not has_default:
        with pytest.raises(ProductRealizationInputError, match="explicit source.*section"):
            ProductRealizationInputService.normalize_contract_defaults(contract, schemas)
        return
    normalized = ProductRealizationInputService.normalize_contract_defaults(contract, schemas)
    assert contract.geometry_fragments[0].binding_options[0].parameter_sources.overrides == {}
    value = normalized.geometry_fragments[0].binding_options[0].parameter_sources.overrides["section"]
    assert value.source_kind == "fixed" and value.value == {"wall_m": 0.01}
    assert ProductRealizationInputService.normalize_contract_defaults(normalized, schemas) == normalized


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["mapped_format", "function_source_format", "schema_encoding"])
async def test_published_loader_rejects_exact_geometry_format_or_schema_drift(drift):
    from tests.unit.backend.test_product_family_authoring_service import _geometry, _function_geometry

    data = (_geometry(asset_format="json") if drift == "mapped_format" else
        _function_geometry(source_format="zip") if drift == "function_source_format" else
        _function_geometry(schema_content=b'{ "type": "object"}'))
    service, scope, family, _, _, _ = await _published_loader(data)
    with pytest.raises(ProductRealizationInputError) as caught:
        await service.load_published_request(scope, family_version=family,
            invocation=ProductRealizationInvocation(product_origin=(0, 0, 0), length_unit={"name": "METRE"}))
    assert caught.value.code == "INPUT_INVALID"
    assert caught.value.phase == "output_validation"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [GeometryCatalogIntegrityError("private asset"), RuntimeError("implementation bug")])
async def test_geometry_reauthorization_does_not_retry_permanent_failure(error):
    geometry = AsyncMock()
    geometry.get_exact_version.side_effect = error
    service = ProductRealizationInputService(Mock(), geometry, runtime=Mock(), family_repository=Mock())
    binding = SimpleNamespace(geometry_resource_id="g", geometry_resource_version_id="v")
    expected = ProductRealizationInputError if isinstance(error, GeometryCatalogIntegrityError) else RuntimeError
    with pytest.raises(expected) as caught:
        await service.reauthorize_geometry_bindings(_scope(), [binding])
    geometry.get_exact_version.assert_awaited_once()
    if isinstance(error, GeometryCatalogIntegrityError):
        assert caught.value.code == "GEOMETRY_ASSET_INTEGRITY_FAILED"
        assert caught.value.phase == "storage_verification" and not caught.value.retryable


@pytest.mark.asyncio
async def test_geometry_reauthorization_retries_temporary_connection_failure():
    geometry = AsyncMock()
    geometry.get_exact_version.side_effect = [ConnectionError("disconnected"), None]
    service = ProductRealizationInputService(Mock(), geometry, runtime=Mock(), family_repository=Mock())
    binding = SimpleNamespace(geometry_resource_id="g", geometry_resource_version_id="v")
    with pytest.raises(ProductRealizationInputError) as caught:
        await service.reauthorize_geometry_bindings(_scope(), [binding])
    assert caught.value.code == "GEOMETRY_NOT_FOUND"
    assert geometry.get_exact_version.await_count == 2


def test_millimetre_origin_equals_metre_without_rescaling_declared_units():
    request = _request()
    contract = ProductFamilyContract.model_validate({
        **request.family_contract.model_dump(),
        "public_parameters": [
            {"parameter_kind": "number", "parameter_key": "length", "unit": "metre"},
            {"parameter_kind": "number", "parameter_key": "angle", "unit": "radian"},
            {"parameter_kind": "number", "parameter_key": "ratio", "unit": "dimensionless"},
        ],
        "interfaces": [{**request.family_contract.interfaces[0].model_dump(), "external_constraint": {
            "allowed_kinds": ["finite_segment"], "allowed_directions": ["same"],
            "maximum_position_tolerance_m": 0.01, "maximum_angular_tolerance_rad": 0.01,
        }}],
    })
    facts = {
        "public_parameters": {"length": 0.25, "angle": 1.5, "ratio": 2},
        "external_constraints": [{"interface_key": "outlet", "source_kind": "design_target", "condition": {
            "kind": "finite_segment", "start_m": [0.9, 2, 3], "end_m": [1.2, 2, 3],
            "outer_diameter_m": 0.2, "direction_relation": "same",
            "position_tolerance_m": 0.001, "angular_tolerance_rad": 0.005,
        }}],
    }
    metres = ProductRealizationInvocation(**facts, product_origin=(1, 2, 3), length_unit={"name": "METRE"})
    millimetres = ProductRealizationInvocation(**facts, product_origin=(1000, 2000, 3000),
                                             length_unit={"name": "METRE", "prefix": "MILLI"})
    normalized = normalize_product_realization_invocation(contract, millimetres)
    assert normalized == normalize_product_realization_invocation(contract, metres)
    assert normalized.product_origin == (1, 2, 3)
    assert normalized.length_unit.prefix is None
    assert normalized.public_parameters == metres.public_parameters
    assert normalized.external_constraints == metres.external_constraints
    assert normalize_product_realization_invocation(contract, normalized) == normalized
    scenario = ProductRealizationValidationScenario(**millimetres.model_dump(), scenario_key="millimetres", expected_status="SOLVED")
    saved = normalize_product_realization_validation_scenario(contract, scenario)
    assert saved.to_invocation() == normalized
    base = {**request.model_dump(), "family_contract": contract, "family_contract_digest": sha256_digest(contract)}
    a = ProductRealizationRequest.model_validate({**base, "invocation": saved.to_invocation()})
    b = ProductRealizationRequest.model_validate({**base, "invocation": metres})
    assert a == b


def test_runtime_rejects_unconverted_origin_context():
    request = _request()
    with pytest.raises(ValueError, match="normalized to METRE"):
        ProductRealizationRequest.model_validate({
            **request.model_dump(), "invocation": {
                "product_origin": [1000, 0, 0], "length_unit": {"name": "METRE", "prefix": "MILLI"},
            },
        })


def test_origin_conversion_rejects_overflow_instead_of_producing_nonfinite_runtime_input():
    invocation = ProductRealizationInvocation(product_origin=(1e308, 0, 0),
                                             length_unit={"name": "METRE", "prefix": "EXA"})
    with pytest.raises(ValueError, match="finite"):
        normalize_product_realization_invocation(_request().family_contract, invocation)




@pytest.mark.asyncio
async def test_batch_input_error_names_only_the_invalid_invocation():
    request = _request()
    runtime = Mock()
    service = ProductRealizationInputService(Mock(), Mock(), runtime=runtime, family_repository=AsyncMock())
    service._load_runtime_bindings = AsyncMock(return_value=request.geometry_bindings)
    invalid = ProductRealizationInvocation(
        product_origin=(0, 0, 0), length_unit={"name": "METRE"},
        public_parameters={"unknown": 1},
    )
    inputs = dict(
        family_contract=request.family_contract,
        family_contract_digest=request.family_contract_digest,
        bindings=(), source_bundle=request.source_bundle,
        source_bundle_sha256=request.source_bundle_sha256,
        exact_geometry=(),
    )

    with pytest.raises(ProductRealizationInputError) as failure:
        await service._load_requests(Mock(), invocations=(request.invocation, invalid), **inputs)
    assert failure.value.code == "INPUT_INVALID"
    assert failure.value.phase == "output_validation"
    assert failure.value.item_index == 1
    assert failure.value.public_message is not None
    runtime.realize_batch.assert_not_called()

    service._load_runtime_bindings = AsyncMock(side_effect=ValueError("invalid exact binding"))
    with pytest.raises(ProductRealizationInputError) as binding_failure:
        await service._load_requests(Mock(), invocations=(request.invocation,), **inputs)
    assert binding_failure.value.item_index is None


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_version", [1, 2])
async def test_published_execution_loads_the_selected_exact_family_without_latest_gate(selected_version):
    repository = AsyncMock()
    repository.get_latest_product_family_version.side_effect = AssertionError("exact execution queried latest")
    assets, geometry = AsyncMock(), AsyncMock()
    service = ProductRealizationInputService(
        assets, geometry, runtime=Mock(), family_repository=repository,
    )
    family = _family_bundle(version_no=selected_version)
    service._read_published_source = AsyncMock(return_value=b"source")
    service.reauthorize_geometry_bindings = AsyncMock(return_value=())
    service._load_requests = AsyncMock(return_value=(_request(),))
    result = await service.load_published_request(_scope(), family_version=family, invocation=_request().invocation)
    assert result == _request()
    repository.get_latest_product_family_version.assert_not_awaited()
    service._read_published_source.assert_awaited_once()
    assert service._read_published_source.await_args.args[0] == family


@pytest.mark.asyncio
async def test_current_family_page_preserves_exact_geometry_without_requiring_geometry_latest():
    family = _family_bundle(version_no=2)
    repository = AsyncMock()
    repository.get_latest_product_family_version.return_value = family
    assets = AsyncMock()
    service = ProductRealizationInputService(
        assets, AsyncMock(), runtime=Mock(),
        family_repository=repository,
    )
    service._read_published_source = AsyncMock(return_value=b"source")
    exact_geometry = (object(),)
    service.reauthorize_geometry_bindings = AsyncMock(return_value=exact_geometry)
    service._load_requests = AsyncMock(return_value=(_request(), _request()))
    requests = await service.load_published_requests(
        _scope(), family_version=family, invocations=(_request().invocation,) * 2,
    )
    assert len(requests) == 2
    repository.get_latest_product_family_version.assert_not_awaited()
    service.reauthorize_geometry_bindings.assert_awaited_once_with(_scope(), family.geometry_bindings)
    assert service._load_requests.await_args.kwargs["bindings"] == family.geometry_bindings
    assert service._load_requests.await_args.kwargs["exact_geometry"] == exact_geometry


@pytest.mark.parametrize("version_id", [
    "grsv_5f83660abba68a02ab56504b4bf3d1a4c74e83c10dbd23a4e01ead0e687d84ef",
    "grsv_d0445cda0fac41ef9875a0acc27b51b37ec0fa5a559c3c53c65900208f9bfb89",
    "grsv_bcdccb696c7527b8655ad3186efd9017de58a4749d49df3ba51a13415117d668",
    "grsv_33de27ba960f9dd0ba2ed25caa2f30341a1c630b454f81e90ad7ceea206e8b63",
    "grsv_225603a650fbc008feea1f6bf9e265dfb5863d4bf582666e47a580766710a69a",
], ids=["pipe", "service_saddle", "connector", "curved_two_port", "rigid_two_port"])
def test_ifc_route_uses_exact_system_version_not_author_name_or_copy_label(version_id):
    from CAD2BIM_backend.application.system_geometry_ifc_routes import (
        system_geometry_ifc_route, system_geometry_source_reuse_mode,
    )
    from CAD2BIM_backend.domain.geometry_resource import GeometryResourceScopeKind
    from CAD2BIM_backend.domain.ifc_geometry_construction import IfcGeometryRepresentationRoute

    def bundle(version_id):
        return SimpleNamespace(
            resource=SimpleNamespace(display_name="PIPE", owner_scope=SimpleNamespace(
                scope_kind=GeometryResourceScopeKind.SYSTEM)),
            version=SimpleNamespace(resource_version_id=version_id, origin_kind="system_copy"),
        )

    selected = bundle(version_id)
    assert system_geometry_ifc_route(selected) is IfcGeometryRepresentationRoute.PARAMETRIC
    selected.resource.display_name = "arbitrary author name"
    assert system_geometry_ifc_route(selected) is IfcGeometryRepresentationRoute.PARAMETRIC
    assert system_geometry_ifc_route(bundle("author_same_name_and_copy_tag")) is IfcGeometryRepresentationRoute.TFS
    expected_reuse = "rigid_two_port" if version_id == "grsv_225603a650fbc008feea1f6bf9e265dfb5863d4bf582666e47a580766710a69a" else "exact"
    assert system_geometry_source_reuse_mode(selected) == expected_reuse
    assert system_geometry_source_reuse_mode(bundle("author_same_name_and_copy_tag")) == "exact"
    selected.resource.owner_scope.scope_kind = "task"
    if expected_reuse == "rigid_two_port":
        with pytest.raises(GeometryCatalogIntegrityError, match="ownership"):
            system_geometry_source_reuse_mode(selected)


@pytest.mark.parametrize("version_id", [
    "grsv_5f83660abba68a02ab56504b4bf3d1a4c74e83c10dbd23a4e01ead0e687d84ef",
    "grsv_d0445cda0fac41ef9875a0acc27b51b37ec0fa5a559c3c53c65900208f9bfb89",
    "grsv_bcdccb696c7527b8655ad3186efd9017de58a4749d49df3ba51a13415117d668",
], ids=["pipe", "service_saddle", "connector"])
def test_known_system_version_with_inconsistent_owner_is_technical_error(version_id):
    from CAD2BIM_backend.application.system_geometry_ifc_routes import system_geometry_ifc_route

    inconsistent = SimpleNamespace(
        resource=SimpleNamespace(owner_scope=SimpleNamespace(scope_kind="task")),
        version=SimpleNamespace(resource_version_id=version_id),
    )
    with pytest.raises(GeometryCatalogIntegrityError, match="ownership"):
        system_geometry_ifc_route(inconsistent)
