"""Small current-contract fixtures shared by Family and Runtime tests.

These construct current inputs explicitly; they do not upgrade legacy documents.
Actual Geometry bytes and observed interface frames remain each test's witnesses.
"""

from __future__ import annotations

from CAD2BIM_backend.domain.geometry_frame import RigidTransform3D
from CAD2BIM_backend.domain.geometry_interface import GeometryModelInterface
from CAD2BIM_backend.domain.product_family import ProductFamilyContract
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle


def current_family_document(
    *,
    component_key="body",
    fragment_key="body_shape",
    binding_key="body_geometry",
    model_interface_key="datum",
    parameter_sources=None,
    standard=False,
    **fields,
):
    document = {
        "product_structure_kind": "single_product",
        "components": [{"component_key": component_key, "ifc_class": "IfcValve"}],
        "geometry_fragments": [{
            "fragment_key": fragment_key,
            "owner_component_key": component_key,
            "binding_options": [{
                "binding_key": binding_key,
                "parameter_sources": {"overrides": {} if parameter_sources is None else parameter_sources},
            }],
        }],
    }
    if standard:
        document["standard_model"] = {}
    document.update(fields)
    return document


def current_family_contract(**fields):
    document = current_family_document(**fields)
    return ProductFamilyContract.model_validate(document)


def current_model_interface(*, key="datum", frame=None, profile=None):
    return GeometryModelInterface(
        interface_key=key,
        local_frame=frame or RigidTransform3D(),
        profile=profile,
    )


def current_realization_source(*, component_key="body", fragment_key="body_shape", binding_key="body_geometry"):
    return pack_source_bundle({"main.py": (
        "def construct(ctx):\n"
        "    return {'status': 'SOLVED',\n"
        f"            'components': [{{'component_key': {component_key!r}}}],\n"
        f"            'fragments': [{{'fragment_key': {fragment_key!r}, 'binding_key': {binding_key!r}}}]}}\n"
        "def realize(ctx):\n"
        "    return construct(ctx)\n"
        "def standard_model(ctx):\n"
        "    return construct(ctx)\n"
    ).encode()})
