from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_interface import (
    CircularInterfaceProfile,
    GeometryModelInterface,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometryBounds,
    GeometrySummary,
    GeometryTopologySummary,
)
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.domain.product_family import ProductFamilyContract
from CAD2BIM_backend.domain.product_realization import (
    ProductGeometryInstanceReceipt,
    ProductRealizationComponentPlacement,
    ProductRealizationFragmentPlacement,
    ProductRealizationGeometryExecution,
    ProductRealizationExternalConstraint,
    ProductRealizationInvocation,
    ProductRealizationOutputNeed,
    ProductRealizationRequest,
    ProductRealizationResult,
    ResolvedMappedGeometryBinding,
    ResolvedModelingFunctionBinding,
    SolvedProductRealizationProposal,
    finalize_product_realization,
    validate_product_realization_proposal,
    validate_product_realization_result_integrity,
)


def _summary() -> GeometrySummary:
    return GeometrySummary(
        format=CanonicalGeometryFormat.OCCT_BREP_V1,
        bounds=GeometryBounds(minimum_m=(0, 0, 0), maximum_m=(1, 1, 1)),
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
        surface_area_m2=6,
        volume_m3=1,
    )


def _mapped_binding(
    binding_key: str = "body",
    *,
    interfaces: tuple[GeometryModelInterface, ...] = (),
) -> ResolvedMappedGeometryBinding:
    payload = f"brep-{binding_key}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    return ResolvedMappedGeometryBinding(
        binding_key=binding_key,
        geometry_content_digest=digest,
        geometry=CanonicalGeometry(
            format=CanonicalGeometryFormat.OCCT_BREP_V1,
            content_type=CanonicalGeometryFormat.OCCT_BREP_V1.content_type,
            payload=payload,
            sha256=digest,
            summary=_summary(),
        ),
        interfaces=interfaces,
    )


def _single_family(*, fragment_mode: str = "fixed") -> ProductFamilyContract:
    return ProductFamilyContract.model_validate(
        {
            "product_structure_kind": "single_product",
            "components": [{"component_key": "body", "ifc_class": "IfcValve"}],
            "geometry_fragments": [
                {
                    "fragment_key": "shape",
                    "owner_component_key": "body",
                    "binding_options": [{"binding_key": "body", "parameter_sources": {"overrides": {}}}],
                    "local_transform_mode": fragment_mode,
                }
            ],
        }
    )


def _request(
    family: ProductFamilyContract,
    bindings,
    *,
    invocation: ProductRealizationInvocation | None = None,
) -> ProductRealizationRequest:
    source = b"bounded numeric realization source"
    return ProductRealizationRequest(
        family_contract=family,
        family_contract_digest=sha256_digest(family),
        source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        geometry_bindings=bindings,
        invocation=invocation
        or ProductRealizationInvocation(
            product_origin=(0, 0, 0),
            length_unit=IfcLengthUnit(),
        ),
    )


def _proposal(
    *,
    component_transform: RigidTransform3D | None = None,
    fragment_transform: RigidTransform3D | None = None,
    geometry_parameters: dict | None = None,
    uniform_scale: float = 1.0,
    internal_parameters: dict | None = None,
) -> SolvedProductRealizationProposal:
    return SolvedProductRealizationProposal(
        internal_parameters=internal_parameters or {},
        components=(ProductRealizationComponentPlacement(
            component_key="body",
            local_transform=component_transform,
        ),),
        fragments=(
            ProductRealizationFragmentPlacement(
                fragment_key="shape",
                binding_key="body",
                geometry_parameters=geometry_parameters or {},
                local_transform=fragment_transform,
                uniform_scale=uniform_scale,
            ),
        ),
    )


def _receipt(
    binding: ResolvedMappedGeometryBinding,
    *,
    fragment_key: str = "shape",
    parameters: dict | None = None,
    uniform_scale: float = 1.0,
) -> ProductGeometryInstanceReceipt:
    return ProductGeometryInstanceReceipt(
        fragment_key=fragment_key,
        binding_key=binding.binding_key,
        normalized_parameters=parameters or {},
        geometry_sha256=binding.geometry.sha256,
        geometry_summary=binding.geometry.summary,
        interfaces=binding.interfaces,
        uniform_scale=uniform_scale,
    )


def test_invocation_requires_explicit_finite_origin_and_unit_and_runtime_requires_metre() -> None:
    with pytest.raises(ValidationError, match="product_origin"):
        ProductRealizationInvocation.model_validate({"length_unit": {}})
    with pytest.raises(ValidationError, match="length_unit"):
        ProductRealizationInvocation.model_validate({"product_origin": [0, 0, 0]})
    with pytest.raises(ValidationError, match="finite"):
        ProductRealizationInvocation(
            product_origin=(0, float("inf"), 0),
            length_unit=IfcLengthUnit(),
        )
    with pytest.raises(ValidationError, match="not booleans"):
        ProductRealizationInvocation(
            product_origin=(True, 0, 0),
            length_unit=IfcLengthUnit(),
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProductRealizationInvocation.model_validate(
            {"product_origin": [0, 0, 0], "length_unit": {}, "datum_transform": {}}
        )

    family = _single_family()
    binding = _mapped_binding()
    with pytest.raises(ValidationError, match="normalized to METRE"):
        _request(
            family,
            (binding,),
            invocation=ProductRealizationInvocation(
                product_origin=(1000, 0, 0),
                length_unit=IfcLengthUnit(prefix="MILLI"),
            ),
        )


def test_preflight_closes_fixed_identity_and_backend_derives_root_from_product_origin() -> None:
    family = _single_family()
    binding = _mapped_binding()
    request = _request(
        family,
        (binding,),
        invocation=ProductRealizationInvocation(
            product_origin=(2, 3, 4),
            length_unit=IfcLengthUnit(),
        ),
    )
    normalized = validate_product_realization_proposal(request, _proposal())
    assert isinstance(normalized, SolvedProductRealizationProposal)
    assert normalized.components[0].local_transform == RigidTransform3D()
    assert normalized.fragments[0].local_transform == RigidTransform3D()

    result = finalize_product_realization(request, normalized, (_receipt(binding),))
    assert result.component_project_world_transforms[0].project_world_transform == (
        RigidTransform3D(translation_m=(2, 3, 4))
    )
    assert result.frame_check_status == "PASS"
    validate_product_realization_result_integrity(
        request,
        result,
        output_need=ProductRealizationOutputNeed.PREVIEW_WORLD,
    )


def test_program_transform_must_be_explicit_while_fixed_transform_must_match_contract() -> None:
    binding = _mapped_binding()
    program_request = _request(_single_family(fragment_mode="program"), (binding,))
    with pytest.raises(ValueError, match="program fragment.*local_transform"):
        validate_product_realization_proposal(program_request, _proposal())

    transform = RigidTransform3D(translation_m=(1, 0, 0))
    normalized = validate_product_realization_proposal(
        program_request,
        _proposal(fragment_transform=transform),
    )
    assert normalized.fragments[0].local_transform == transform

    fixed_request = _request(_single_family(), (binding,))
    with pytest.raises(ValueError) as component_failure:
        validate_product_realization_proposal(
            fixed_request,
            _proposal(component_transform=transform),
        )
    assert str(component_failure.value) == (
        "Product realization proposal conflicts with a fixed component placement; "
        "reason=FIXED_COMPONENT_TRANSFORM_MISMATCH; "
        "definition_path=family_contract.components.0.fixed_local_transform; "
        "proposal_path=proposal.components.0.local_transform; component_key=body"
    )

    with pytest.raises(ValueError) as fragment_failure:
        validate_product_realization_proposal(
            fixed_request,
            _proposal(fragment_transform=transform),
        )
    assert str(fragment_failure.value) == (
        "Product realization proposal conflicts with a fixed fragment placement; "
        "reason=FIXED_FRAGMENT_TRANSFORM_MISMATCH; "
        "definition_path=family_contract.geometry_fragments.0.fixed_local_transform; "
        "proposal_path=proposal.fragments.0.local_transform; fragment_key=shape"
    )


@pytest.mark.parametrize(
    ("member", "change"),
    (
        ("component", "missing"),
        ("component", "duplicate"),
        ("component", "unknown"),
        ("fragment", "missing"),
        ("fragment", "duplicate"),
        ("fragment", "unknown"),
    ),
)
def test_preflight_rejects_incomplete_duplicate_or_unknown_solution_members(
    member: str,
    change: str,
) -> None:
    family = ProductFamilyContract.model_validate(
        {
            "product_structure_kind": "semantic_assembly",
            "components": [
                {"component_key": "root", "ifc_class": "IfcElementAssembly"},
                {
                    "component_key": "body",
                    "ifc_class": "IfcValve",
                    "parent_component_key": "root",
                },
            ],
            "geometry_fragments": [
                {
                    "fragment_key": "left",
                    "owner_component_key": "body",
                    "binding_options": [{"binding_key": "body", "parameter_sources": {"overrides": {}}}],
                },
                {
                    "fragment_key": "right",
                    "owner_component_key": "body",
                    "binding_options": [{"binding_key": "body", "parameter_sources": {"overrides": {}}}],
                },
            ],
        }
    )
    request = _request(family, (_mapped_binding(),))
    components = [
        ProductRealizationComponentPlacement(component_key="root"),
        ProductRealizationComponentPlacement(component_key="body"),
    ]
    fragments = [
        ProductRealizationFragmentPlacement(fragment_key="left", binding_key="body", geometry_parameters={}),
        ProductRealizationFragmentPlacement(fragment_key="right", binding_key="body", geometry_parameters={}),
    ]
    values = components if member == "component" else fragments
    if change == "missing":
        values.pop()
    elif change == "duplicate":
        values[-1] = values[0]
    elif member == "component":
        values[-1] = ProductRealizationComponentPlacement(component_key="unknown")
    else:
        values[-1] = ProductRealizationFragmentPlacement(
            fragment_key="unknown",
            binding_key="body",
            geometry_parameters={},
        )
    proposal = SolvedProductRealizationProposal(
        components=tuple(components),
        fragments=tuple(fragments),
    )
    with pytest.raises(ValueError, match=f"{member}s do not match"):
        validate_product_realization_proposal(request, proposal)


def _function_case() -> ProductRealizationRequest:
    family = ProductFamilyContract.model_validate(
        {
            "product_structure_kind": "single_product",
            "components": [{"component_key": "body", "ifc_class": "IfcValve"}],
            "public_parameters": [
                {
                    "parameter_kind": "number",
                    "parameter_key": "width",
                    "unit": "metre",
                    "minimum": 0.1,
                    "maximum": 1.0,
                }
            ],
            "solved_parameters": [
                {
                    "parameter_kind": "number",
                    "parameter_key": "cut",
                    "unit": "metre",
                    "minimum": 0.1,
                    "maximum": 1.0,
                }
            ],
            "geometry_fragments": [
                {
                    "fragment_key": "shape",
                    "owner_component_key": "body",
                    "binding_options": [{
                        "binding_key": "body",
                        "parameter_sources": {"overrides": {
                            "fixed": {"source_kind": "fixed", "value": 3.0},
                            "width": {
                                "source_kind": "public_parameter",
                                "parameter_key": "width",
                            },
                            "cut": {
                                "source_kind": "solved_parameter",
                                "parameter_key": "cut",
                            },
                        }},
                    }],
                }
            ],
        }
    )
    source = b"geometry function source"
    binding = ResolvedModelingFunctionBinding(
        binding_key="body",
        geometry_content_digest="a" * 64,
        source_bundle=source,
        source_bundle_sha256=hashlib.sha256(source).hexdigest(),
        parameter_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["width", "cut"],
            "properties": {
                "fixed": {"type": "number", "default": 3.0},
                "width": {"type": "number", "minimum": 0.1, "maximum": 1.0},
                "cut": {"type": "number", "minimum": 0.1, "maximum": 1.0},
            },
        },
    )
    return _request(
        family,
        (binding,),
        invocation=ProductRealizationInvocation(
            public_parameters={"width": 0.5},
            product_origin=(0, 0, 0),
            length_unit=IfcLengthUnit(),
        ),
    )


def test_function_parameters_are_complete_normalized_and_match_fixed_public_solved_sources() -> None:
    request = _function_case()
    parameters = {"fixed": 3.0, "width": 0.5, "cut": 0.75}
    proposal = _proposal(
        geometry_parameters=parameters,
        internal_parameters={"cut": 0.75},
    )
    assert validate_product_realization_proposal(request, proposal).fragments[0].geometry_parameters == parameters
    from CAD2BIM_backend.domain.product_realization import SolvedProductRealizationAuthorProposal
    author_data = proposal.model_dump(mode="python")
    author_data["fragments"][0].pop("geometry_parameters")
    canonical = validate_product_realization_proposal(request, SolvedProductRealizationAuthorProposal.model_validate(author_data))
    assert canonical.fragments[0].geometry_parameters == parameters
    with pytest.raises(ValidationError, match="Extra inputs"):
        SolvedProductRealizationAuthorProposal.model_validate(proposal.model_dump(mode="python"))
    corrupted = canonical.model_copy(update={"fragments": (canonical.fragments[0].model_copy(
        update={"geometry_parameters": parameters | {"width": 0.6}}),)})
    with pytest.raises(ValueError, match="GEOMETRY_PARAMETER_SOURCE_MISMATCH"):
        finalize_product_realization(request, corrupted, (), output_need=ProductRealizationOutputNeed.SOLVE_ONLY)

    with pytest.raises(ValueError, match="every exact Geometry parameter"):
        validate_product_realization_proposal(
            request,
            _proposal(
                geometry_parameters={"width": 0.5, "cut": 0.75},
                internal_parameters={"cut": 0.75},
            ),
        )
    with pytest.raises(ValueError, match="missing required keys; affected_count=1; paths=cut"):
        validate_product_realization_proposal(
            request,
            _proposal(geometry_parameters=parameters),
        )
    with pytest.raises(ValueError, match="uniform_scale must be 1"):
        validate_product_realization_proposal(
            request,
            _proposal(
                geometry_parameters=parameters,
                uniform_scale=2.0,
                internal_parameters={"cut": 0.75},
            ),
        )


@pytest.mark.parametrize(
    ("parameter_name", "wrong_value", "source_kind", "source_parameter_key"),
    (
        ("fixed", 4.0, "fixed", None),
        ("width", 0.6, "public_parameter", "width"),
        ("cut", 0.8, "solved_parameter", "cut"),
    ),
)
def test_parameter_source_conflict_identifies_safe_paths_without_echoing_values(
    parameter_name: str,
    wrong_value: float,
    source_kind: str,
    source_parameter_key: str | None,
) -> None:
    parameters = {"fixed": 3.0, "width": 0.5, "cut": 0.75}
    with pytest.raises(ValueError) as provenance_failure:
        validate_product_realization_proposal(
            _function_case(),
            _proposal(
                geometry_parameters=parameters | {parameter_name: wrong_value},
                internal_parameters={"cut": 0.75},
            ),
        )
    message = str(provenance_failure.value)
    assert (
        "reason=GEOMETRY_PARAMETER_SOURCE_MISMATCH; "
        f"definition_path=family_contract.geometry_fragments.0.binding_options.0.parameter_sources.overrides.{parameter_name}; "
        f"proposal_path=proposal.fragments.0.geometry_parameters.{parameter_name}; "
        f"fragment_key=shape; parameter_name={parameter_name}; source_kind={source_kind}"
    ) in message
    if source_parameter_key is None:
        assert "source_parameter_key=" not in message
    else:
        assert message.endswith(f"source_parameter_key={source_parameter_key}")
    assert str(wrong_value) not in message


def test_mapped_fragment_requires_empty_parameters_and_uses_declared_positive_scale() -> None:
    with pytest.raises(ValidationError, match="not a boolean"):
        ProductRealizationFragmentPlacement(
            fragment_key="shape",
            binding_key="body",
            geometry_parameters={},
            uniform_scale=True,
        )
    with pytest.raises(ValidationError, match="not a boolean"):
        ProductGeometryInstanceReceipt(
            fragment_key="shape",
            binding_key="body",
            geometry_sha256="0" * 64,
            uniform_scale=True,
        )

    family_data = _single_family().model_dump(mode="json", by_alias=True)
    family_data["public_parameters"] = [
        {
            "parameter_kind": "number",
            "parameter_key": "scale",
            "unit": "dimensionless",
            "minimum": 0.1,
            "maximum": 2.0,
        }
    ]
    family_data["geometry_fragments"][0]["binding_options"][0]["uniform_scale_source"] = {
        "source_kind": "public_parameter",
        "parameter_key": "scale",
    }
    family = ProductFamilyContract.model_validate(family_data)
    binding = _mapped_binding()
    request = _request(
        family,
        (binding,),
        invocation=ProductRealizationInvocation(
            public_parameters={"scale": 1.5},
            product_origin=(0, 0, 0),
            length_unit=IfcLengthUnit(),
        ),
    )
    proposal = _proposal(uniform_scale=1.5)
    result = finalize_product_realization(
        request,
        proposal,
        (_receipt(binding, uniform_scale=1.5),),
    )
    assert result.instance_receipts[0].uniform_scale == 1.5
    with pytest.raises(ValueError) as scale_failure:
        validate_product_realization_proposal(
            request,
            _proposal(uniform_scale=1.25),
        )
    assert str(scale_failure.value) == (
        "Product realization proposal conflicts with the declared uniform scale source; "
        "reason=UNIFORM_SCALE_SOURCE_MISMATCH; "
        "definition_path=family_contract.geometry_fragments.0.binding_options.0.uniform_scale_source; "
        "proposal_path=proposal.fragments.0.uniform_scale; fragment_key=shape; "
        "parameter_name=uniform_scale; source_kind=public_parameter; "
        "source_parameter_key=scale"
    )
    with pytest.raises(ValueError, match="mapped fragment geometry_parameters must be empty"):
        validate_product_realization_proposal(
            request,
            _proposal(geometry_parameters={"scale": 1.5}, uniform_scale=1.5),
        )


def test_actual_interface_comes_from_receipt_and_scale_does_not_change_origin_or_placement() -> None:
    family = ProductFamilyContract.model_validate(
        {
            "product_structure_kind": "single_product",
            "components": [{"component_key": "body", "ifc_class": "IfcValve"}],
            "geometry_fragments": [
                {
                    "fragment_key": "shape",
                    "owner_component_key": "body",
                    "binding_options": [{
                        "binding_key": "body",
                        "parameter_sources": {"overrides": {}},
                        "uniform_scale_source": {"source_kind": "fixed", "value": 2.0},
                    }],
                    "local_transform_mode": "program",
                }
            ],
            "interfaces": [
                {
                    "interface_key": "outlet",
                    "owner_component_key": "body",
                    "interface_kind": "external",
                    "source": {
                        "source_kind": "model_interface",
                        "fragment_key": "shape",
                        "model_interface_key": "port",
                    },
                    "external_constraint": {
                        "allowed_kinds": ["fixed_point"],
                        "allowed_directions": ["none"],
                        "profile_required": True,
                        "maximum_position_tolerance_m": 1e-6,
                        "maximum_angular_tolerance_rad": 1e-6,
                    },
                }
            ],
        }
    )
    actual_interface = GeometryModelInterface(
        interface_key="port",
        local_frame=RigidTransform3D(translation_m=(2, 0, 0)),
        profile=CircularInterfaceProfile(diameter_m=0.4),
    )
    binding = _mapped_binding(
        interfaces=(
            GeometryModelInterface(
                interface_key="port",
                local_frame=RigidTransform3D(translation_m=(1, 0, 0)),
                profile=CircularInterfaceProfile(diameter_m=0.2),
            ),
        )
    )
    request = _request(
        family,
        (binding,),
        invocation=ProductRealizationInvocation.model_validate(
            {
                "product_origin": [10, 0, 0],
                "length_unit": {},
                "external_constraints": [
                    {
                        "interface_key": "outlet",
                        "source_kind": "design_target",
                        "condition": {
                            "kind": "fixed_point", "point_m": [15, 0, 0],
                            "direction_relation": "none",
                            "outer_diameter_m": 0.4,
                            "position_tolerance_m": 1e-6,
                            "angular_tolerance_rad": 1e-6,
                        },
                    }
                ],
            }
        ),
    )
    proposal = _proposal(
        fragment_transform=RigidTransform3D(translation_m=(3, 0, 0)),
        uniform_scale=2.0,
    )
    receipt = _receipt(binding, uniform_scale=2.0).model_copy(
        update={"interfaces": (actual_interface,)}
    )
    result = finalize_product_realization(request, proposal, (receipt,))
    assert result.interface_project_world_frames[0].project_world_frame.translation_m == (
        15.0,
        0.0,
        0.0,
    )
    assert result.external_condition_checks[0].evaluation.satisfied is True
    assert result.frame_check_status == "PASS"

    with pytest.raises(ValueError, match="no referenced model interface"):
        finalize_product_realization(
            request,
            proposal,
            (receipt.model_copy(update={"interfaces": ()}),),
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        SolvedProductRealizationProposal.model_validate(
            proposal.model_dump(mode="python")
            | {"interfaces": [{"interface_key": "outlet", "local_frame": {}}]}
        )


def test_standard_installation_inputs_require_explicit_direction_and_preserve_tolerances() -> None:
    fixed = ProductRealizationExternalConstraint.model_validate({
        "interface_key": "outlet", "condition": {
            "kind": "fixed_point", "point_m": [1, 2, 3], "axis": [0, 0, 2], "direction_relation": "opposed",
        },
    })
    assert fixed.source_kind == "design_target"
    assert fixed.condition.axis == (0, 0, 1)
    assert fixed.condition.direction_relation == "opposed"
    assert fixed.condition.outer_diameter_m is None
    assert fixed.condition.position_tolerance_m == fixed.condition.angular_tolerance_rad == 1e-6
    segment = {"kind": "finite_segment", "start_m": [0, 0, 0], "end_m": [0, 0, 2],
               "outer_diameter_m": 0.2, "direction_relation": "same"}
    for invalid in (
        {key: value for key, value in segment.items() if key != "direction_relation"},
        {**segment, "end_m": [0, 0, 0]},
        {**segment, "end_m": [0, 0, float("inf")]},
        {**segment, "start_m": [False, 0, 0]},
        {**segment, "outer_diameter_m": 0},
        {**segment, "allowed_interval_m": [0.1, 1.9]},
        {**segment, "real_pipe_id": "pipe-1"},
        {**segment, "reference_direction": [0, 0, 1]},
        {**segment, "direction_relation": "none", "reference_direction": [1, 0, 0]},
        {"position": {"position_kind": "point", "point_m": [0, 0, 0]}},
        {"kind": "infinite_axis", "point_m": [0, 0, 0], "axis": [0, 0, 1], "outer_diameter_m": 0.2},
        {"kind": "fixed_port", "point_m": [0, 0, 0], "axis": [0, 0, 1], "direction_relation": "opposed"},
        {"kind": "fixed_point", "point_m": [0, 0, 0], "axis": [0, 0, 1]},
        {"point_m": [0, 0, 0], "axis": [0, 0, 1], "direction_relation": "same"},
        {"kind": "fixed_point", "point_m": [0, 0, 0], "direction_relation": "same"},
        {"kind": "fixed_point", "point_m": [0, 0, 0], "direction_relation": "none", "axis": [0, 0, 1]},
        {"kind": "fixed_point", "point_m": [0, 0, 0], "direction_relation": "none", "reference_direction": [1, 0, 0]},
        {"kind": "fixed_point", "point_m": [0, 0, 0], "direction_relation": "opposed", "axis": [0, 0, 0]},
    ):
        with pytest.raises(ValidationError):
            ProductRealizationExternalConstraint.model_validate({"interface_key": "outlet", "condition": invalid})


@pytest.mark.parametrize("kind,point,opposed,diameter,expected", [
    ("finite_segment", (0, 0, 1), False, 0.2, True),
    ("finite_segment", (0, 0, 0), False, 0.2, True),
    ("finite_segment", (0, 0, 2), False, 0.2, True),
    ("finite_segment", (0.01, 0, 1), False, 0.2, False),
    ("finite_segment", (0, 0, 2.01), False, 0.2, False),
    ("finite_segment", (0, 0, 1), True, 0.2, False),
    ("finite_segment", (0, 0, 1), False, 0.3, False),
    ("fixed_point", (0, 0, 1), True, 0.2, True),
    ("fixed_point", (0, 0, 1.01), True, 0.2, False),
    ("infinite_axis", (0, 0, 100), False, 0.2, True),
])
def test_standard_installation_checks_use_same_build_position_direction_and_od(
    kind, point, opposed, diameter, expected,
) -> None:
    family = ProductFamilyContract.model_validate({
        **_single_family().model_dump(),
        "interfaces": [{
            "interface_key": "outlet", "owner_component_key": "body", "interface_kind": "external",
            "source": {"source_kind": "model_interface", "fragment_key": "shape", "model_interface_key": "port"},
            "external_constraint": {
                "allowed_kinds": ["finite_segment", "fixed_point", "infinite_axis"],
                "allowed_directions": ["same", "opposed"], "profile_required": True,
                "maximum_position_tolerance_m": 1e-6, "maximum_angular_tolerance_rad": 1e-6,
            },
        }],
    })
    condition = {"kind": kind, "outer_diameter_m": 0.2}
    if kind == "finite_segment":
        condition.update(start_m=[0, 0, 0], end_m=[0, 0, 2], direction_relation="same")
    else:
        condition.update(point_m=[0, 0, 1], axis=[0, 0, 1],
            direction_relation="opposed" if kind == "fixed_point" else "same")
    binding = _mapped_binding(interfaces=(GeometryModelInterface(
        interface_key="port", local_frame=RigidTransform3D(translation_m=(0, 0, 1)),
        profile=CircularInterfaceProfile(diameter_m=0.2),
    ),))
    invocation = ProductRealizationInvocation.model_validate({
        "product_origin": [0, 0, 0], "length_unit": {},
        "external_constraints": [{"interface_key": "outlet", "condition": condition}],
    })
    request = _request(family, (binding,), invocation=invocation)
    actual = GeometryModelInterface(
        interface_key="port",
        local_frame=RigidTransform3D(translation_m=point,
            rotation=((1, 0, 0), (0, -1, 0), (0, 0, -1)) if opposed else RigidTransform3D().rotation),
        profile=CircularInterfaceProfile(diameter_m=diameter),
    )
    receipt = _receipt(binding).model_copy(update={"interfaces": (actual,)})
    result = finalize_product_realization(request, _proposal(), (receipt,))
    check = result.external_condition_checks[0]
    assert check.interface_key == "outlet"
    assert check.evaluation.satisfied is expected
    assert result.frame_check_status == ("PASS" if expected else "FAIL")
    assert check.external_actual_connection_status == "NOT_CHECKED"
    if kind == "finite_segment":
        assert check.evaluation.condition.position.extent.minimum_m == 0
        assert check.evaluation.condition.position.extent.maximum_m == 2


@pytest.mark.parametrize("kind", ["fixed_point", "finite_segment", "infinite_axis"])
def test_od_can_only_be_omitted_without_a_profile_requirement(kind) -> None:
    data = {**_single_family().model_dump(), "interfaces": [{
        "interface_key": "attachment", "owner_component_key": "body", "interface_kind": "external",
        "source": {"source_kind": "model_interface", "fragment_key": "shape", "model_interface_key": "end"},
        "external_constraint": {
            "allowed_kinds": [kind], "allowed_directions": ["opposed"],
            "maximum_position_tolerance_m": 1e-6, "maximum_angular_tolerance_rad": 1e-6,
        },
    }]}
    condition = ({"kind": kind, "start_m": [0, 0, 0], "end_m": [0, 0, 1], "direction_relation": "opposed"}
        if kind == "finite_segment" else
        {"kind": kind, "point_m": [0, 0, 0], "axis": [0, 0, 1], "direction_relation": "opposed"})
    invocation = ProductRealizationInvocation.model_validate({
        "product_origin": [0, 0, 0], "length_unit": {}, "external_constraints": [{
            "interface_key": "attachment", "condition": condition,
        }],
    })
    binding = _mapped_binding()
    _request(ProductFamilyContract.model_validate(data), (binding,), invocation=invocation)
    data["interfaces"][0]["external_constraint"]["profile_required"] = True
    with pytest.raises(ValueError, match="external profile cannot be relaxed"):
        _request(ProductFamilyContract.model_validate(data), (binding,), invocation=invocation)
    data["interfaces"][0]["external_constraint"]["profile_required"] = False
    data["interfaces"][0]["connection_profile"] = {"outer_diameter_m": 0.2}
    with pytest.raises(ValueError, match="external profile cannot be relaxed"):
        _request(ProductFamilyContract.model_validate(data), (binding,), invocation=invocation)


@pytest.mark.parametrize("kind", ["fixed_point", "finite_segment", "infinite_axis"])
def test_position_only_targets_compare_supplied_profile_and_report_actual_binding(kind) -> None:
    from CAD2BIM_backend.application.preview_case_evaluation import evaluate_family_case
    from CAD2BIM_backend.domain.preview_cases import FamilyPreviewCase, FamilyCaseExpectation
    from CAD2BIM_mcp.direct.ifc.preview_cases import external_target_comparison

    data = {**_single_family().model_dump(), "interfaces": [{
        "interface_key": "outlet", "owner_component_key": "body", "interface_kind": "external",
        "source": {"source_kind": "model_interface", "fragment_key": "shape", "model_interface_key": "port"},
        "external_constraint": {"allowed_kinds": [kind], "allowed_directions": ["none"], "profile_required": False,
            "maximum_position_tolerance_m": 1e-6, "maximum_angular_tolerance_rad": 1e-6},
    }]}
    family = ProductFamilyContract.model_validate(data)
    condition = ({"kind": kind, "start_m": [0, 0, 0], "end_m": [0, 0, 2], "direction_relation": "none"}
        if kind == "finite_segment" else {"kind": kind, "point_m": [0, 0, 1], "direction_relation": "none"})
    if kind == "infinite_axis":
        condition["axis"] = [0, 0, 2]
    binding = _mapped_binding(interfaces=(GeometryModelInterface(interface_key="port", local_frame=RigidTransform3D()),))
    for requested_od, actual_od, offset, passed in (
        (None, None, 0, True), (None, None, 0.01, False),
        (0.2, 0.2, 0, True), (0.2, 0.3, 0, False), (0.2, None, 0, False),
    ):
        target = {**condition, **({} if requested_od is None else {"outer_diameter_m": requested_od})}
        invocation = ProductRealizationInvocation.model_validate({"product_origin": [0, 0, 0], "length_unit": {},
            "external_constraints": [{"interface_key": "outlet", "condition": target}]})
        actual = GeometryModelInterface(interface_key="port", local_frame=RigidTransform3D(translation_m=(offset, 0, 1)),
            profile=None if actual_od is None else CircularInterfaceProfile(diameter_m=actual_od))
        result = finalize_product_realization(_request(family, (binding,), invocation=invocation), _proposal(),
            (_receipt(binding).model_copy(update={"interfaces": (actual,)}),))
        check = result.external_condition_checks[0].evaluation
        assert check.satisfied is passed
        assert check.direction_residual_rad is check.roll_residual_rad is None
        assert check.profile_satisfied is (None if requested_od is None else passed)
        assert external_target_comparison(result) == ("PASS" if passed else "FAIL")
        case = FamilyPreviewCase(case_key="target", invocation=invocation,
            expectation=FamilyCaseExpectation(expected_status="SOLVED"))
        status, diagnostics = evaluate_family_case(case, result)
        assert status == ("pass" if passed else "fail")
        if not passed:
            message = diagnostics[0].message
            assert f"External target {kind}" in message and "interface=outlet; binding=shape/port" in message
            assert ("position: residual=" if offset else "profile: requested=") in message
def test_solve_only_has_no_actual_facts_and_integrity_check_does_not_refinalize() -> None:
    family = _single_family()
    binding = _mapped_binding()
    request = _request(family, (binding,))
    result = finalize_product_realization(
        request,
        _proposal(),
        (),
        output_need=ProductRealizationOutputNeed.SOLVE_ONLY,
    )
    assert result.instance_receipts == ()
    assert result.component_project_world_transforms == ()
    assert result.interface_project_world_frames == ()
    assert result.frame_check_status is None
    validate_product_realization_result_integrity(
        request,
        result,
        output_need=ProductRealizationOutputNeed.SOLVE_ONLY,
    )
    with pytest.raises(ValueError, match="solve-only"):
        finalize_product_realization(
            request,
            _proposal(),
            (_receipt(binding),),
            output_need=ProductRealizationOutputNeed.SOLVE_ONLY,
        )


# Private drawing-specific regression omitted from public distribution.


@pytest.mark.parametrize("field", ["normalized_public_parameters", "binding_digest", "source_bundle_sha256", "instance_receipts"])
def test_integrity_check_rejects_wrong_request_or_missing_fragment(field) -> None:
    family = _single_family()
    binding = _mapped_binding()
    request = _request(family, (binding,))
    result = finalize_product_realization(request, _proposal(), (_receipt(binding),))
    with pytest.raises(ValueError, match="identity|receipts"):
        validate_product_realization_result_integrity(
            request,
            result.model_copy(update={field: (() if field == "instance_receipts" else
                {"unexpected_parameter": 42.0} if field == "normalized_public_parameters" else "0" * 64)}),
            output_need=ProductRealizationOutputNeed.PREVIEW_WORLD,
        )
    with pytest.raises(ValidationError, match="instance_handle"):
        ProductGeometryInstanceReceipt.model_validate(
            _receipt(binding).model_dump() | {"instance_handle": "instance_0001"}
        )


def test_current_contract_rejects_old_flat_fragment_and_implicit_proposal_binding() -> None:
    family_data = _single_family().model_dump(mode="json", by_alias=True)
    fragment = family_data["geometry_fragments"][0]
    fragment.update(fragment.pop("binding_options")[0])
    with pytest.raises(ValidationError, match="binding_options"):
        ProductFamilyContract.model_validate(family_data)
    proposal_data = _proposal().model_dump(mode="python")
    proposal_data["fragments"][0].pop("binding_key")
    with pytest.raises(ValidationError, match="binding_key"):
        SolvedProductRealizationProposal.model_validate(proposal_data)


def test_geometry_execution_indexes_local_geometry_by_fragment_key() -> None:
    family = _single_family()
    binding = _mapped_binding()
    request = _request(family, (binding,))
    result = finalize_product_realization(
        request,
        _proposal(),
        (_receipt(binding),),
        output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
    )
    execution = ProductRealizationGeometryExecution(
        realization=result,
        output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
        instance_geometries={"shape": binding.geometry},
    )
    assert set(execution.instance_geometries or {}) == {"shape"}
    historical_summary = binding.geometry.summary.model_copy(update={
        "surface_area_m2": binding.geometry.summary.surface_area_m2 + 1e-10,
        "topology": binding.geometry.summary.topology.model_copy(update={"triangle_count": 12345}),
    })
    same_bytes = binding.geometry.model_copy(update={"summary": historical_summary})
    assert ProductRealizationGeometryExecution(
        realization=result,
        output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
        instance_geometries={"shape": same_bytes},
    ).instance_geometries["shape"].payload == binding.geometry.payload
    with pytest.raises(ValidationError, match="exact receipt"):
        ProductRealizationGeometryExecution(
            realization=result,
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
            instance_geometries={"shape": same_bytes.model_copy(update={"payload": b"corrupt"})},
        )
    with pytest.raises(ValidationError, match="fragment geometry"):
        ProductRealizationGeometryExecution(
            realization=result,
            output_need=ProductRealizationOutputNeed.LOCAL_FRAGMENTS,
            instance_geometries={"instance_0001": binding.geometry},
        )
