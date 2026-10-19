from __future__ import annotations

import math

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.canonical import sha256_digest
from CAD2BIM_backend.domain.geometry_resource import GeometryResourceKind
from CAD2BIM_backend.domain.product_family import (
    PRODUCT_FAMILY_PROGRAM_ENTRY,
    ProductFamilyContract,
    ProductFamilyGeometryBindingRecord,
    ProductFamilyGeometryPurpose,
    ProductFamilyListEntry,
    ProductFamilyListQuery,
    ProductFamilyRecord,
    ProductFamilyStructureKind,
    ProductFamilyVersionAssetRecord,
    ProductFamilyVersionAssetRole,
    ProductFamilyVersionBundle,
    ProductFamilyVersionRecord,
    product_family_content_digest,
)
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind
from CAD2BIM_backend.domain.product_family_catalog import (
    ProductFamilyDiscoveryPage as ProductFamilyPage, ProductFamilyMetadataPage,
)
from tests.unit.backend.product_family_fixtures import current_family_document


NOW = datetime(2026, 9, 4, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


def test_standard_structure_preserves_all_members_and_constraints():
    from CAD2BIM_backend.domain.product_family import standard_product_family_structure

    data = _model_interface_family_data()
    data["standard_model"] = {}
    original = ProductFamilyContract.model_validate(data)
    before = original.model_dump_json()
    view = standard_product_family_structure(original)
    assert view.components == original.components
    assert view.geometry_fragments == original.geometry_fragments
    assert view.interfaces == original.interfaces
    assert view.constraints == original.constraints
    assert original.model_dump_json() == before
    for forbidden in ({"component_keys": ["root", "left"]}, {"fragment_keys": ["left_shape"]},
                      {"interface_keys": []}, {"installation_frame": {}}):
        with pytest.raises(ValidationError, match="Extra inputs"):
            ProductFamilyContract.model_validate({**data, "standard_model": {**data["standard_model"], **forbidden}})

def _single_contract_data() -> dict[str, object]:
    return current_family_document(binding_key="body", interfaces=[])

def _family(*, family_id: str = "family-1") -> ProductFamilyRecord:
    return ProductFamilyRecord(
        family_id=family_id,
        owner_scope=ResourceOwnerScope(
            scope_kind=ResourceOwnerScopeKind.TASK,
            tenant_id="tenant-1",
            task_id="task-1",
        ),
        created_at=NOW,
    )


def _binding(
    *,
    family_version_id: str = "family-version-1",
    binding_key: str = "body",
    digest: str = SHA_A,
) -> ProductFamilyGeometryBindingRecord:
    return ProductFamilyGeometryBindingRecord(
        family_version_id=family_version_id,
        binding_key=binding_key,
        geometry_resource_id=f"geometry-{binding_key}",
        geometry_resource_version_id=f"geometry-{binding_key}-version-1",
        geometry_content_digest=digest,
        expected_resource_kind=GeometryResourceKind.MAPPED_MODEL,
        geometry_purpose=ProductFamilyGeometryPurpose.PRODUCT_COMPONENT,
    )


def _asset(
    *,
    family_version_id: str = "family-version-1",
    role: ProductFamilyVersionAssetRole = (ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE),
    digest: str = SHA_B,
) -> ProductFamilyVersionAssetRecord:
    is_source = role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE
    return ProductFamilyVersionAssetRecord(
        family_version_id=family_version_id,
        asset_role=role,
        asset_format="source_bundle_v1" if is_source else "json",
        filename="source.zip" if is_source else "validation.json",
        content_type="application/zip" if is_source else "application/json",
        size_bytes=128,
        sha256=digest,
        created_at=NOW,
    )


def _bundle(
    *,
    family: ProductFamilyRecord | None = None,
    family_version_id: str = "family-version-1",
    version_no: int = 1,
    parent_family_version_id: str | None = None,
    bindings: tuple[ProductFamilyGeometryBindingRecord, ...] | None = None,
    assets: tuple[ProductFamilyVersionAssetRecord, ...] | None = None,
) -> ProductFamilyVersionBundle:
    family = family or _family()
    bindings = bindings or (_binding(family_version_id=family_version_id),)
    assets = assets or (_asset(family_version_id=family_version_id),)
    contract = ProductFamilyContract.model_validate(_single_contract_data())
    contract_digest = sha256_digest(contract)
    content_digest = product_family_content_digest(
        contract_digest=contract_digest,
        program_entry=PRODUCT_FAMILY_PROGRAM_ENTRY,
        geometry_bindings=bindings,
        assets=assets,
    )
    version = ProductFamilyVersionRecord(
        family_version_id=family_version_id,
        family_id=family.family_id,
        version_no=version_no,
        parent_family_version_id=parent_family_version_id,
        display_name="  Regulator assembly  ",
        purpose="  Connect two gas pipe interfaces  ",
        family_contract=contract,
        contract_digest=contract_digest,
        content_digest=content_digest,
        created_at=NOW + timedelta(seconds=version_no),
    )
    return ProductFamilyVersionBundle(
        family=family,
        version=version,
        geometry_bindings=bindings,
        assets=assets,
    )


def test_product_family_scope_and_records_are_closed_frozen_and_normalized() -> None:
    assert ResourceOwnerScope(scope_kind="system").scope_id == "system"
    assert ResourceOwnerScope(scope_kind="tenant", tenant_id="tenant-1").scope_id == ("tenant-1")
    assert (
        ResourceOwnerScope(scope_kind="task", tenant_id="tenant-1", task_id="task-1").scope_id
        == "task-1"
    )

    with pytest.raises(ValidationError):
        ResourceOwnerScope(scope_kind="system", tenant_id="tenant-1")
    with pytest.raises(ValidationError):
        ResourceOwnerScope(scope_kind="tenant", tenant_id="tenant-1", task_id="task-1")

    bundle = _bundle()
    assert "schema" not in bundle.version.family_contract.model_dump()
    assert "contract_schema" not in bundle.version.family_contract.model_dump()
    assert bundle.version.display_name == "Regulator assembly"
    assert bundle.version.purpose == "Connect two gas pipe interfaces"
    assert bundle.version.product_structure_kind is ProductFamilyStructureKind.SINGLE_PRODUCT
    assert not any(name.startswith("current") for name in ProductFamilyRecord.model_fields)
    assert not any(name.startswith("current") for name in ProductFamilyVersionRecord.model_fields)
    with pytest.raises(ValidationError, match="frozen"):
        bundle.version.version_no = 2
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProductFamilyRecord(
            **_family().model_dump(),
            latest_family_version_id="family-version-1",
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("duplicate_component", "component keys must be unique"),
        ("duplicate_fragment", "fragment keys must be unique"),
        ("duplicate_interface", "interface keys must be unique"),
        ("two_roots", "exactly one root"),
        ("missing_parent", "parent reference is missing"),
        ("cycle", "contains a cycle"),
        ("wrong_root", "physical product family root requires geometry"),
        ("missing_fragment_owner", "fragment owner reference is missing"),
        ("missing_interface_owner", "interface owner reference is missing"),
    ],
)
def test_family_contract_rejects_ambiguous_or_dangling_topology(change, message) -> None:
    data = _model_interface_family_data()
    if change == "duplicate_component":
        data["components"].append(dict(data["components"][1]))
    elif change == "duplicate_fragment":
        data["geometry_fragments"].append(dict(data["geometry_fragments"][0]))
    elif change == "duplicate_interface":
        data["interfaces"].append(dict(data["interfaces"][0]))
    elif change == "two_roots":
        data["components"][1].pop("parent_component_key")
    elif change == "missing_parent":
        data["components"][1]["parent_component_key"] = "missing"
    elif change == "cycle":
        data["components"][1]["parent_component_key"] = "right"
        data["components"][2]["parent_component_key"] = "left"
    elif change == "wrong_root":
        data["components"][0]["ifc_class"] = "IfcValve"
    elif change == "missing_fragment_owner":
        data["geometry_fragments"][0]["owner_component_key"] = "missing"
        data["interfaces"] = []
        data["constraints"] = []
    else:
        data["interfaces"][0]["owner_component_key"] = "missing"
    with pytest.raises(ValidationError, match=message):
        ProductFamilyContract.model_validate(data)

def test_family_contract_rejects_open_fields_and_unsupported_ifc_classes() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProductFamilyContract.model_validate(
            _single_contract_data() | {"unversioned_extension": True}
        )
    contract = _single_contract_data()
    contract["components"] = [{"component_key": "body", "ifc_class": "IfcBuildingElementProxy"}]
    with pytest.raises(ValidationError, match="IfcBuildingElementProxy"):
        ProductFamilyContract.model_validate(contract)


def _model_interface_family_data() -> dict[str, object]:
    return current_family_document(
        fragment_key="left_shape", binding_key="left",
        product_structure_kind="semantic_assembly",
        components=[
            {"component_key": "root", "ifc_class": "IfcElementAssembly"},
            {"component_key": "left", "ifc_class": "IfcValve", "parent_component_key": "root"},
            {"component_key": "right", "ifc_class": "IfcPipeFitting", "parent_component_key": "root"},
        ],
        geometry_fragments=[
            {"fragment_key": "left_shape", "owner_component_key": "left",
             "binding_options": [{"binding_key": "left", "parameter_sources": {"overrides": {}}}]},
            {"fragment_key": "right_shape", "owner_component_key": "right",
             "binding_options": [{"binding_key": "right", "parameter_sources": {"overrides": {}}}]},
        ],
        interfaces=[
            {"interface_key": "left_end", "owner_component_key": "left", "interface_kind": "internal",
             "source": {"source_kind": "model_interface", "fragment_key": "left_shape", "model_interface_key": "outlet"},
             "connection_profile": {"profile_kind": "circular", "outer_diameter_m": 0.2}},
            {"interface_key": "right_end", "owner_component_key": "right", "interface_kind": "internal",
             "source": {"source_kind": "model_interface", "fragment_key": "right_shape", "model_interface_key": "inlet"}},
        ],
        constraints=[{
            "constraint_kind": "interface_coincidence", "constraint_key": "join",
            "interface_a_key": "left_end", "interface_b_key": "right_end",
            "position_tolerance_m": 0.001, "angular_tolerance_rad": 0.01,
            "frame_relation": {"direction_relation": "opposed", "orientation_requirement": "axis_only"},
        }],
    )

def test_family_interface_sources_are_closed_and_keep_requirements_separate() -> None:
    contract = ProductFamilyContract.model_validate(_model_interface_family_data())
    source = contract.interfaces[0].source
    assert source is not None
    assert source.source_kind == "model_interface"
    assert contract.interfaces[0].connection_profile is not None
    serialized = contract.model_dump(mode="json")
    relation = serialized["constraints"][0]["frame_relation"]
    assert relation == {
        "direction_relation": "opposed",
        "orientation_requirement": "axis_only",
    }

    for forbidden in (
        {
            "declared_frame": {
                "origin_m": (0, 0, 0),
                "axis": (0, 0, 1),
                "reference_direction": (1, 0, 0),
            }
        },
        {"adapter_binding_key": "adapter"},
    ):
        payload = _model_interface_family_data()
        payload["interfaces"][0].update(forbidden)
        with pytest.raises(ValidationError, match="Extra inputs"):
            ProductFamilyContract.model_validate(payload)


@pytest.mark.parametrize("moving_kind", ("component", "fragment"))
def test_program_relative_pose_cannot_also_declare_a_fixed_transform(moving_kind):
    data = _model_interface_family_data()
    moving = data["components"][2] if moving_kind == "component" else data["geometry_fragments"][1]
    moving.update(local_transform_mode="program", fixed_local_transform={})
    with pytest.raises(ValidationError, match="cannot also declare a fixed transform"):
        ProductFamilyContract.model_validate(data)


def test_program_relative_pose_is_explicit_and_root_is_fixed_identity():
    from CAD2BIM_backend.domain.canonical import canonical_json
    data = _model_interface_family_data()
    family = ProductFamilyContract.model_validate(data)
    serialized = family.model_dump(mode="json")
    assert all("local_transform_mode" not in row for row in serialized["components"] + serialized["geometry_fragments"])
    for row in data["components"] + data["geometry_fragments"]:
        row["local_transform_mode"] = "fixed"
    assert canonical_json(ProductFamilyContract.model_validate(data)) == canonical_json(family)
    data["components"][2]["local_transform_mode"] = "program"
    assert ProductFamilyContract.model_validate(data).components[2].local_transform_mode == "program"
    data["components"][0]["local_transform_mode"] = "program"
    with pytest.raises(ValidationError, match="root component requires a fixed"):
        ProductFamilyContract.model_validate(data)
    data["components"][0]["local_transform_mode"] = "fixed"
    data["components"][0]["fixed_local_transform"] = {"translation_m": [1, 0, 0]}
    with pytest.raises(ValidationError, match="root component local transform must be identity"):
        ProductFamilyContract.model_validate(data)


def test_semantic_assembly_allows_physical_root_with_geometry_and_mixed_children():
    data = _model_interface_family_data()
    data["components"] = data["components"][1:]
    data["components"][0].pop("parent_component_key")
    data["components"][1]["parent_component_key"] = "left"
    family = ProductFamilyContract.model_validate(data)
    assert family.components[0].ifc_class == "IfcValve"
    assert family.components[1].ifc_class == "IfcPipeFitting"
    assert family.product_structure_kind == "semantic_assembly"


def test_model_interface_source_requires_exact_fragment_and_owner_closure() -> None:
    missing = _model_interface_family_data()
    missing["interfaces"][0]["source"]["fragment_key"] = "missing"
    with pytest.raises(ValidationError, match="fragment reference is missing"):
        ProductFamilyContract.model_validate(missing)

    wrong_owner = _model_interface_family_data()
    wrong_owner["interfaces"][0]["source"]["fragment_key"] = "right_shape"
    with pytest.raises(ValidationError, match="owner disagrees"):
        ProductFamilyContract.model_validate(wrong_owner)


def test_interface_sources_and_constraint_relations_are_required() -> None:
    for field in ("source", "frame_relation"):
        data = _model_interface_family_data()
        (data["interfaces"][0] if field == "source" else data["constraints"][0]).pop(field)
        with pytest.raises(ValidationError, match=field):
            ProductFamilyContract.model_validate(data)
    data = _model_interface_family_data()
    data["constraints"][0]["interface_b_key"] = "missing"
    with pytest.raises(ValidationError, match="constraint interface reference is missing"):
        ProductFamilyContract.model_validate(data)
    data = _model_interface_family_data()
    data["constraints"][0]["constraint_kind"] = "no_collision"
    with pytest.raises(ValidationError, match="interface_coincidence"):
        ProductFamilyContract.model_validate(data)

def test_family_contract_rejects_a_provider_unreadable_total_json_size() -> None:
    component_keys = [f"c{index:03d}_" + "x" * 59 for index in range(127)]
    root_key = "root"
    components = [{"component_key": root_key, "ifc_class": "IfcElementAssembly"}]
    components.extend(
        {
            "component_key": key,
            "ifc_class": "IfcPipeFitting",
            "parent_component_key": root_key,
        }
        for key in component_keys
    )
    fragments = []
    for index in range(256):
        fragments.append(
            {
                "fragment_key": f"f{index:03d}_" + "x" * 59,
                "owner_component_key": component_keys[index % len(component_keys)],
                "binding_options": [{
                    "binding_key": f"b{index:03d}_" + "x" * 59,
                    "parameter_sources": {"overrides": {}},
                }],
            }
        )
    interfaces = [
        {
            "interface_key": f"i{index:03d}_" + "x" * 59,
            "owner_component_key": component_keys[0],
            "interface_kind": "external",
            "source": {"source_kind": "model_interface", "fragment_key": fragments[0]["fragment_key"], "model_interface_key": "end"},
        }
        for index in range(64)
    ]

    with pytest.raises(ValidationError, match="canonical JSON byte budget"):
        ProductFamilyContract.model_validate(
            {
                "product_structure_kind": "semantic_assembly",
                "components": components,
                "geometry_fragments": fragments,
                "interfaces": interfaces,
            }
        )


def test_version_requires_canonical_contract_fixed_entry_and_exact_content_digest() -> None:
    bundle = _bundle()
    assert bundle.version.contract_digest == sha256_digest(bundle.version.family_contract)
    assert bundle.version.program_entry == "main.py:realize"

    with pytest.raises(ValidationError, match="contract_digest"):
        ProductFamilyVersionRecord.model_validate(
            bundle.version.model_dump() | {"contract_digest": "0" * 64}
        )
    with pytest.raises(ValidationError, match="main.py:realize"):
        ProductFamilyVersionRecord.model_validate(
            bundle.version.model_dump() | {"program_entry": "solver.py:solve"}
        )
    with pytest.raises(ValidationError, match="product_structure_kind"):
        ProductFamilyVersionRecord.model_validate(
            bundle.version.model_dump()
            | {
                "family_contract": {
                    "product_structure_kind": "unknown",
                    "components": [{"component_key": "body", "ifc_class": "IfcValve"}],
                    "geometry_fragments": [
                        {
                            "fragment_key": "body_shape",
                            "owner_component_key": "body",
                            "binding_options": [{"binding_key": "body", "parameter_sources": {"overrides": {}}}],
                        }
                    ],
                    "interfaces": [],
                },
                "contract_digest": sha256_digest(
                    {
                        "product_structure_kind": "unknown",
                        "components": [{"component_key": "body", "ifc_class": "IfcValve"}],
                        "geometry_fragments": [
                            {
                                "fragment_key": "body_shape",
                                "owner_component_key": "body",
                                "binding_options": [{"binding_key": "body", "parameter_sources": {"overrides": {}}}],
                            }
                        ],
                        "interfaces": [],
                    }
                ),
            }
        )

    with pytest.raises(ValidationError, match="content_digest"):
        ProductFamilyVersionBundle(
            family=bundle.family,
            version=bundle.version.model_copy(update={"content_digest": "0" * 64}),
            geometry_bindings=bundle.geometry_bindings,
            assets=bundle.assets,
        )



def test_content_digest_is_order_independent_and_binds_exact_dependencies_and_assets() -> None:
    bindings = (
        _binding(binding_key="z_adapter", digest=SHA_A),
        _binding(binding_key="body", digest=SHA_B),
    )
    assets = (
        _asset(role=ProductFamilyVersionAssetRole.VALIDATION_REPORT, digest=SHA_A),
        _asset(role=ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE, digest=SHA_B),
    )
    contract_digest = sha256_digest(
        {
            "product_structure_kind": "semantic_assembly",
        }
    )
    digest = product_family_content_digest(
        contract_digest=contract_digest,
        program_entry=PRODUCT_FAMILY_PROGRAM_ENTRY,
        geometry_bindings=bindings,
        assets=assets,
    )
    reordered = product_family_content_digest(
        contract_digest=contract_digest,
        program_entry=PRODUCT_FAMILY_PROGRAM_ENTRY,
        geometry_bindings=tuple(reversed(bindings)),
        assets=tuple(reversed(assets)),
    )
    assert reordered == digest

    changed = product_family_content_digest(
        contract_digest=contract_digest,
        program_entry=PRODUCT_FAMILY_PROGRAM_ENTRY,
        geometry_bindings=(
            bindings[0].model_copy(update={"geometry_content_digest": "0" * 64}),
            bindings[1],
        ),
        assets=assets,
    )
    assert changed != digest

    changed_asset = product_family_content_digest(
        contract_digest=contract_digest,
        program_entry=PRODUCT_FAMILY_PROGRAM_ENTRY,
        geometry_bindings=bindings,
        assets=(assets[0].model_copy(update={"sha256": "0" * 64}), assets[1]),
    )
    assert changed_asset != digest


def test_ready_bundle_requires_one_exact_source_asset_and_consistent_version_owners() -> None:
    bundle = _bundle()
    with pytest.raises(ValidationError, match="different root"):
        ProductFamilyVersionBundle(
            family=_family(family_id="other-family"),
            version=bundle.version,
            geometry_bindings=bundle.geometry_bindings,
            assets=bundle.assets,
        )
    with pytest.raises(ValidationError, match="another version"):
        ProductFamilyVersionBundle(
            family=bundle.family,
            version=bundle.version,
            geometry_bindings=(_binding(family_version_id="other-version"),),
            assets=bundle.assets,
        )
    with pytest.raises(ValidationError, match="binding keys must be unique"):
        ProductFamilyVersionBundle(
            family=bundle.family,
            version=bundle.version,
            geometry_bindings=(bundle.geometry_bindings[0], bundle.geometry_bindings[0]),
            assets=bundle.assets,
        )
    with pytest.raises(ValidationError, match="exactly one realization source bundle"):
        ProductFamilyVersionBundle(
            family=bundle.family,
            version=bundle.version,
            geometry_bindings=bundle.geometry_bindings,
            assets=(_asset(role=ProductFamilyVersionAssetRole.VALIDATION_REPORT),),
        )
    with pytest.raises(ValidationError, match="canonical source_bundle_v1 ZIP"):
        bad_source = bundle.assets[0].model_copy(update={"asset_format": "zip"})
        ProductFamilyVersionBundle(
            family=bundle.family,
            version=bundle.version,
            geometry_bindings=bundle.geometry_bindings,
            assets=(bad_source,),
        )


def test_ready_bundle_closes_contract_to_exact_geometry_bindings() -> None:
    with pytest.raises(ValidationError, match="missing geometry binding"):
        _bundle(bindings=(_binding(binding_key="other"),))

    with pytest.raises(ValidationError, match="unused geometry binding"):
        _bundle(bindings=(_binding(), _binding(binding_key="other")))

    # Historical purpose values have no current enum member and must be rejected.
    with pytest.raises(ValidationError, match="geometry_purpose"):
        ProductFamilyGeometryBindingRecord.model_validate(
            {**_binding().model_dump(), "geometry_purpose": "interface_adapter"}
        )
    incompatible = _binding().model_copy(update={"geometry_purpose": "interface_adapter"})
    with pytest.raises(ValidationError, match="incompatible purpose"):
        _bundle(bindings=(incompatible,))


def test_multiple_fragments_may_share_one_exact_geometry_binding() -> None:
    base = _bundle()
    contract = ProductFamilyContract.model_validate(
        {
            **_single_contract_data(),
            "geometry_fragments": [
                {
                    "fragment_key": "body_shape",
                    "owner_component_key": "body",
                    "binding_options": [{"binding_key": "body", "parameter_sources": {"overrides": {}}}],
                },
                {
                    "fragment_key": "body_right",
                    "owner_component_key": "body",
                    "binding_options": [{"binding_key": "body", "parameter_sources": {"overrides": {}}}],
                },
            ],
        }
    )
    contract_digest = sha256_digest(contract)
    version = base.version.model_copy(
        update={
            "family_contract": contract,
            "contract_digest": contract_digest,
            "content_digest": product_family_content_digest(
                contract_digest=contract_digest,
                program_entry=base.version.program_entry,
                geometry_bindings=base.geometry_bindings,
                assets=base.assets,
            ),
        }
    )
    reopened = ProductFamilyVersionBundle(
        family=base.family,
        version=version,
        geometry_bindings=base.geometry_bindings,
        assets=base.assets,
    )
    assert len(reopened.version.family_contract.geometry_fragments) == 2


def test_legacy_adapter_fields_and_program_frames_are_rejected() -> None:
    for patch in ({"adapter_binding_key": "inlet_adapter"}, {"attachment_role_key": "side_a"},
                  {"declared_frame": {}}, {"source": {"source_kind": "program_frame"}}):
        data = _model_interface_family_data()
        data["interfaces"][0].update(patch)
        with pytest.raises(ValidationError):
            ProductFamilyContract.model_validate(data)


def test_single_current_contract_rejects_retired_version_metadata() -> None:
    current = _single_contract_data()
    assert "schema" not in ProductFamilyContract.model_validate(current).model_dump()
    for schema in ("product_family_contract_v5", "product_family_contract_v3", "product_family_contract_v2", "product_family_contract_v1", None):
        data = _single_contract_data()
        data["schema"] = schema
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ProductFamilyContract.model_validate(data)
    for field in ("abi_profile", "runtime_profile", "budget_profile"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ProductFamilyVersionRecord.model_validate(_bundle().version.model_dump() | {field: "product_realization_v1"})

def test_family_and_version_pages_require_stable_descending_continuations() -> None:
    newer = _family(family_id="family-new").model_copy(
        update={"created_at": NOW + timedelta(seconds=2)}
    )
    older = _family(family_id="family-old").model_copy(
        update={"created_at": NOW + timedelta(seconds=1)}
    )
    page = ProductFamilyPage(
        entries=(
            ProductFamilyListEntry(family=newer, latest_version=_bundle(family=newer)),
            ProductFamilyListEntry(family=older, latest_version=_bundle(family=older)),
        ),
        complete=False,
        next_before_created_at=older.created_at,
        next_before_family_id=older.family_id,
    )
    assert page.next_before_family_id == "family-old"
    with pytest.raises(ValidationError, match="belongs to another root"):
        ProductFamilyListEntry(family=newer, latest_version=_bundle(family=older))
    with pytest.raises(ValidationError, match="strictly descending"):
        ProductFamilyPage(
            entries=(
                ProductFamilyListEntry(family=older, latest_version=_bundle(family=older)),
                ProductFamilyListEntry(family=newer, latest_version=_bundle(family=newer)),
            ),
            complete=True,
        )
    with pytest.raises(ValidationError, match="boundary is incomplete"):
        ProductFamilyListQuery(before_family_id="family-old")

    v2 = _bundle(family_version_id="family-version-2", version_no=2)
    v1 = _bundle()
    from tests.unit.backend.test_product_family_catalog_service import _metadata
    metadata = (_metadata(v2), _metadata(v1))
    versions = ProductFamilyMetadataPage(versions=metadata, complete=True)
    assert versions.versions == metadata
    with pytest.raises(ValidationError, match="strictly descending"):
        ProductFamilyMetadataPage(versions=tuple(reversed(metadata)), complete=True)
@pytest.mark.parametrize("value", [[1, 2], [1, True, 3], [1, math.inf, 3], [1, math.nan, 3], [1, 2, 11]])
def test_number_array_reuses_bounded_numeric_and_exact_array_validation(value):
    from CAD2BIM_backend.domain.product_family import normalize_product_family_parameter_values, ProductFamilyNumberArrayParameterContract
    parameter = ProductFamilyNumberArrayParameterContract(parameter_key="point", length=3,
        unit="metre", minimum=-10, maximum=10, required=False, default_value=[0, 0, 0])
    assert normalize_product_family_parameter_values((parameter,), {}) == {"point": [0.0, 0.0, 0.0]}
    assert parameter.model_dump()["parameter_kind"] == "number_array" and "items" not in parameter.model_dump()
    with pytest.raises(ValueError, match="point"):
        normalize_product_family_parameter_values((parameter,), {"point": value})


def test_anonymous_items_keep_named_object_members_and_original_tree_budget():
    from pydantic import TypeAdapter
    from CAD2BIM_backend.domain.product_family import ProductFamilyPublicParameterContract, normalize_product_family_parameter_values
    declaration = {"parameter_key": "samples", "parameter_kind": "array", "min_items": 1, "max_items": 2,
        "items": {"parameter_kind": "object", "properties": [{"parameter_key": "vector", "parameter_kind": "number_array",
            "length": 3, "minimum": -1, "maximum": 1}]}}
    adapter = TypeAdapter(ProductFamilyPublicParameterContract)
    parameter = adapter.validate_python(declaration)
    with pytest.raises(ValueError, match=r"samples\[0\]\.vector\[1\]"):
        normalize_product_family_parameter_values((parameter,), {"samples": [{"vector": [0, True, 0]}]})
    for key, value in (("parameter_key", "item"), ("required", True), ("default_value", {})):
        with pytest.raises(ValidationError, match="Extra inputs"):
            adapter.validate_python({**declaration, "items": {**declaration["items"], key: value}})
    # number_array costs the same additional number node as explicit array+number.
    nested = {"parameter_kind": "object", "properties": [{"parameter_key": "inner", **declaration}]}
    with pytest.raises(ValueError, match="depth 4"):
        adapter.validate_python({"parameter_key": "outer", "parameter_kind": "array", "min_items": 1, "max_items": 1, "items": nested})
