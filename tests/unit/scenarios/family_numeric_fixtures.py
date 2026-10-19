"""Exact SYSTEM inputs for numeric tests using the current Domain assembly owner."""

import hashlib
import json

from pydantic import TypeAdapter

from CAD2BIM_backend.application.product_realization_input_service import ProductRealizationInputService
from CAD2BIM_backend.domain.geometry_runtime import CanonicalGeometry, CanonicalGeometryFormat
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationAuthorProposal,
    ProductRealizationInvocation,
    ResolvedMappedGeometryBinding,
    ResolvedModelingFunctionBinding,
    validate_product_realization_proposal,
)
from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle


def current_numeric_request(root, family, contract, context, *,
                            realization_kind="self_geometry"):
    manifest = json.loads((root / f"families/{family}/manifest.json").read_text())
    source_bundle = pack_source_bundle({
        item["bundle_path"]: (root / item["repo_path"]).read_bytes()
        for item in manifest["family"]["source_files"]
    })
    bindings = []
    schemas = {}
    for descriptor in manifest["geometry_bindings"]:
        key = descriptor["binding_key"]
        if descriptor["resource_kind"] == "modeling_function":
            bundle = pack_source_bundle({
                item["bundle_path"]: (root / item["repo_path"]).read_bytes()
                for item in descriptor["source_files"]
            })
            digest = hashlib.sha256(bundle).hexdigest()
            schema = json.loads((root / descriptor["parameter_schema_path"]).read_text())
            schemas[key] = schema
            bindings.append(ResolvedModelingFunctionBinding(
                binding_key=key, geometry_content_digest=digest,
                source_bundle=bundle, source_bundle_sha256=digest, parameter_schema=schema,
            ))
        else:
            intake = json.loads((root / descriptor["usage_path"]).with_name(
                "asset-intake-plan.json").read_text())
            payload = (root.parents[2] / intake["source_path"]).read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            assert digest == intake["source_sha256"]
            fmt = CanonicalGeometryFormat.OCCT_BREP_V1
            schemas[key] = None
            bindings.append(ResolvedMappedGeometryBinding(
                binding_key=key, geometry_content_digest=digest,
                geometry=CanonicalGeometry(format=fmt, content_type=fmt.content_type,
                                           payload=payload, sha256=digest),
                interfaces=intake["annotation"]["interfaces"],
            ))
    frozen = ProductRealizationInputService.normalize_contract_defaults(contract, schemas)
    invocation = ProductRealizationInvocation(
        realization_kind=realization_kind, public_parameters=context.parameters(),
        product_origin=context.product_origin(), length_unit=IfcLengthUnit(),
        external_constraints=(context.external_constraints()
                              if realization_kind == "self_geometry" else ()),
    )
    request, = ProductRealizationInputService.prepare_definition_requests(
        family_contract=frozen, source_bundle=source_bundle,
        geometry_bindings=tuple(bindings), invocations=(invocation,),
    )
    return request


def canonical_numeric_proposal(root, family, contract, context, author_proposal, *,
                               realization_kind="self_geometry"):
    request = current_numeric_request(root, family, contract, context,
                                      realization_kind=realization_kind)
    proposal = TypeAdapter(ProductRealizationAuthorProposal).validate_python(author_proposal)
    return validate_product_realization_proposal(request, proposal).model_dump(mode="json")
