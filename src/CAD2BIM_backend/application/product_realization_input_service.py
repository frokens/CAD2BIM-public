"""Entry-neutral loader for one exact Product Realization Runtime request."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Sequence
from typing import Literal

from pydantic import ValidationError

from CAD2BIM_backend.application.geometry_resource_service import (
    GeometryResourceBuildError,
    GeometryResourceIntegrityError,
    GeometryResourceService,
)
from CAD2BIM_backend.application.product_family_asset_service import (
    ProductFamilyAssetIntegrityError,
    ProductFamilyAssetService,
)
from CAD2BIM_backend.application.validation_support import validation_error_message
from CAD2BIM_backend.application.system_geometry_ifc_routes import (
    system_geometry_ifc_route, system_geometry_source_reuse_mode,
)
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.geometry_catalog import GeometryCatalogIntegrityError
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourceVersionBundle,
    geometry_resource_inputs,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    CanonicalGeometry,
    CanonicalGeometryFormat,
    GeometrySummary,
    normalize_geometry_function_parameters,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.ifc_geometry_basis import IfcLengthUnit
from CAD2BIM_backend.ports.product_realization_runtime import ProductRealizationRuntimePort
from CAD2BIM_backend.ports.product_families import ProductFamilyRepository
from CAD2BIM_backend.domain.product_family import (
    ProductFamilyContract,
    ProductFamilyFixedParameterSource,
    ProductFamilyPublicParameterSource,
    ProductFamilySolvedParameterSource,
    ProductFamilyParameterSources,
    ProductFamilyGeometryBindingRecord,
    ProductFamilyVersionAssetRole,
    ProductFamilyVersionBundle,
)
from CAD2BIM_backend.domain.product_family_authoring import (
    ProductFamilyInvocationSemanticError,
    ResolvedProductFamilyGeometryBindingInput,
    normalize_product_family_validation_scenario,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductRealizationInvocation,
    ProductRealizationRequest,
    ProductRealizationStatus,
    ProductRealizationValidationScenario,
    ResolvedMappedGeometryBinding,
    ResolvedModelingFunctionBinding,
    ResolvedProductGeometryBinding,
)


_MAX_TECHNICAL_ATTEMPTS = 3
_MAX_DIAGNOSTIC_BYTES = 2_000
_Binding = ResolvedProductFamilyGeometryBindingInput | ProductFamilyGeometryBindingRecord
_PRODUCT_REALIZATION_INPUT_INVALID_MESSAGE = (
    "Exact Product Realization inputs are invalid; inspect the exact Geometry bindings and "
    "invocation against the Family contract"
)


class ProductRealizationInputError(RuntimeError):
    """Bounded entry-neutral failure while loading exact realization inputs."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: Literal[
            "preflight",
            "output_validation",
            "storage_verification",
            "reconcile",
        ] = "preflight",
        retryable: bool = False,
        public_message: str | None = None,
        item_index: int | None = None,
    ) -> None:
        super().__init__(_bounded_message(message))
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self._public_message = None if public_message is None else _bounded_message(public_message)
        self.item_index = item_index

    @property
    def public_message(self) -> str | None:
        """Return only a caller-approved, bounded public diagnostic."""

        return self._public_message


class _PreparedPublishedProductRealizationInputs:
    """One authorized exact closure; no persistent cache or alternate contract."""

    def __init__(self, request: ProductRealizationRequest):
        from CAD2BIM_backend.domain.product_realization import _PreparedProductRealizationRequest
        self._family_contract = request.family_contract
        self._prepared = _PreparedProductRealizationRequest(request)

    def normalize_invocation(self, invocation: ProductRealizationInvocation) -> ProductRealizationInvocation:
        from CAD2BIM_backend.domain.product_realization import validate_product_realization_explicit_requirements
        result = normalize_product_realization_invocation(self._family_contract, invocation)
        validate_product_realization_explicit_requirements(self._family_contract, result)
        return result

    def requests(self, invocations: Sequence[ProductRealizationInvocation]) -> tuple[ProductRealizationRequest, ...]:
        from CAD2BIM_backend.domain.product_realization import MAX_PRODUCT_REALIZATION_BATCH_ITEMS
        if len(invocations) > MAX_PRODUCT_REALIZATION_BATCH_ITEMS:
            raise ProductRealizationInputError("INPUT_INVALID", "Product batch exceeds its bounded input page")
        return tuple(self._prepared.project_normalized_invocation(item) for item in invocations)


class ProductRealizationInputService:
    """Resolve exact family-owned source and geometry into the shared Runtime ABI."""

    def __init__(
        self,
        family_assets: ProductFamilyAssetService,
        geometry_resources: GeometryResourceService,
        *, runtime: ProductRealizationRuntimePort | None,
        family_repository: ProductFamilyRepository,
    ) -> None:
        self._family_assets = family_assets
        self._geometry_resources = geometry_resources
        self._runtime = runtime
        self._family_repository = family_repository

    async def normalize_authoring_contract(
        self, scope: InvocationScope, *, family_contract: ProductFamilyContract,
        bindings: Sequence[_Binding],
        exact_geometry: Sequence[GeometryResourceVersionBundle],
    ) -> ProductFamilyContract:
        """Freeze omitted Geometry defaults as Family-owned explicit fixed sources."""
        bundle_by_id = {item.version.resource_version_id: item for item in exact_geometry}
        schemas = {}
        for binding in bindings:
            bundle = bundle_by_id.get(binding.geometry_resource_version_id)
            if bundle is None or not _binding_matches_bundle(binding, bundle):
                raise ProductRealizationInputError("GEOMETRY_CHANGED", "Exact Geometry binding changed during Family parameter normalization")
            if binding.expected_resource_kind is GeometryResourceKind.MODELING_FUNCTION:
                asset, content = await self._required_geometry_asset_pair(
                    scope, binding, GeometryResourceAssetRole.PARAMETER_SCHEMA,
                )
                if asset.asset_format != "json_schema" or asset.content_type != "application/schema+json":
                    raise ProductRealizationInputError("INPUT_INVALID", "Exact Geometry parameter schema has an invalid format")
                schema = json.loads(content)
                if not isinstance(schema, dict) or canonical_json(schema).encode("utf-8") != content:
                    raise ProductRealizationInputError("INPUT_INVALID", "Exact Geometry parameter schema is not canonical JSON")
                schemas[binding.binding_key] = schema
        return self.normalize_contract_defaults(family_contract, schemas)

    @staticmethod
    def normalize_contract_defaults(family_contract, schemas):
        """Share parameter default freezing between published and draft bindings."""
        fragments = []
        for fragment in family_contract.geometry_fragments:
            options = []
            for option_index, option in enumerate(fragment.binding_options):
                sources = dict(option.parameter_sources.overrides)
                schema = schemas.get(option.binding_key)
                properties = schema["properties"] if schema is not None else {}
                if set(sources) - set(properties):
                    raise ProductRealizationInputError(
                        "INPUT_INVALID",
                        f"Fragment {fragment.fragment_key} binding_options.{option_index} has unknown Geometry parameter sources",
                    )
                for key, definition in properties.items():
                    if key in sources:
                        continue
                    default_source = option.parameter_sources.default_source
                    if default_source is not None:
                        declarations = (family_contract.public_parameters if default_source == "public_parameter"
                                        else family_contract.solved_parameters)
                        if key not in {item.parameter_key for item in declarations}:
                            raise ProductRealizationInputError("INPUT_INVALID",
                                f"Fragment {fragment.fragment_key} binding_options.{option_index} "
                                f"has no same-name {default_source} declaration for Geometry parameter {key}")
                        source_type = (ProductFamilyPublicParameterSource if default_source == "public_parameter"
                                       else ProductFamilySolvedParameterSource)
                        sources[key] = source_type(parameter_key=key)
                        continue
                    if "default" not in definition:
                        raise ProductRealizationInputError(
                            "INPUT_INVALID",
                            f"Fragment {fragment.fragment_key} binding_options.{option_index} requires an explicit source for Geometry parameter {key}",
                        )
                    # Use the same bounded Geometry normalizer, including nested defaults.
                    single_schema = {"type": "object", "additionalProperties": False,
                                     "properties": {key: definition}, "required": []}
                    default = normalize_geometry_function_parameters(single_schema, {})[key]
                    sources[key] = ProductFamilyFixedParameterSource(value=default)
                for key, source in sources.items():
                    if isinstance(source, ProductFamilyFixedParameterSource):
                        single_schema = {"type": "object", "additionalProperties": False,
                            "properties": {key: properties[key]}, "required": [key]}
                        value = normalize_geometry_function_parameters(single_schema, {key: source.value})[key]
                        sources[key] = ProductFamilyFixedParameterSource(value=value)
                options.append(option.model_copy(update={"parameter_sources": ProductFamilyParameterSources(overrides=sources)}))
            fragments.append(fragment.model_copy(update={"binding_options": tuple(options)}))
        return ProductFamilyContract.model_validate({
            **family_contract.model_dump(mode="python"), "geometry_fragments": tuple(fragments),
        })

    @staticmethod
    def normalize_standard_definition(family_contract):
        """Resolve new standard construction inputs from this Family definition only."""
        from CAD2BIM_backend.domain.product_family import normalize_product_family_public_parameters
        standard = family_contract.standard_model
        if standard is None:
            return family_contract
        values = {parameter.parameter_key: parameter.default_value
            for parameter in family_contract.public_parameters if parameter.default_value is not None}
        values.update(standard.public_parameters)
        parameters = normalize_product_family_public_parameters(family_contract, values)
        return ProductFamilyContract.model_validate({
            **family_contract.model_dump(mode="python"),
            "standard_model": {"public_parameters": parameters},
        })

    @staticmethod
    def prepare_definition_requests(*, family_contract, source_bundle, geometry_bindings, invocations):
        """One Domain request assembly for frozen draft or publication contents."""
        request = ProductRealizationRequest.model_construct(
            family_contract=family_contract, family_contract_digest=sha256_digest(family_contract),
            source_bundle=source_bundle, source_bundle_sha256=hashlib.sha256(source_bundle).hexdigest(),
            geometry_bindings=geometry_bindings, invocation=None)
        request._require_closure()
        prepared = _PreparedPublishedProductRealizationInputs(request)
        return prepared.requests(tuple(prepared.normalize_invocation(item) for item in invocations))

    async def reauthorize_geometry_bindings(
        self,
        scope: InvocationScope,
        bindings: Sequence[_Binding],
    ) -> tuple[GeometryResourceVersionBundle, ...]:
        """Reopen every supplied exact version in the current InvocationScope."""

        exact: list[GeometryResourceVersionBundle] = []
        for binding in bindings:
            last_error: Exception | None = None
            bundle = None
            for _ in range(_MAX_TECHNICAL_ATTEMPTS):
                try:
                    bundle = await self._geometry_resources.get_exact_version(
                        scope.identity,
                        resource_id=binding.geometry_resource_id,
                        resource_version_id=binding.geometry_resource_version_id,
                    )
                    break
                except GeometryCatalogIntegrityError as exc:
                    message = "An exact Geometry version failed stored asset integrity validation"
                    raise ProductRealizationInputError(
                        "GEOMETRY_ASSET_INTEGRITY_FAILED", message,
                        phase="storage_verification", public_message=message,
                    ) from exc
                except (ConnectionError, TimeoutError) as exc:
                    last_error = exc
            else:
                raise ProductRealizationInputError(
                    "GEOMETRY_SERVICE_UNAVAILABLE",
                    "Exact Product Realization geometry reauthorization exhausted database retries",
                    phase="reconcile",
                    retryable=True,
                ) from last_error
            if bundle is None:
                raise ProductRealizationInputError(
                    "GEOMETRY_NOT_FOUND",
                    "An exact Product Realization geometry binding is not visible in this scope",
                )
            if not _binding_matches_bundle(binding, bundle):
                raise ProductRealizationInputError(
                    "GEOMETRY_CHANGED",
                    "An exact Product Realization geometry binding changed after resolution",
                )
            exact.append(bundle)
        return tuple(exact)


    async def load_published_request(
        self,
        scope: InvocationScope,
        *,
        family_version: ProductFamilyVersionBundle,
        invocation: ProductRealizationInvocation,
        numeric_only: bool = False,
    ) -> ProductRealizationRequest:
        """Load a request only from one immutable published ProductFamilyVersion."""
        return (await self.load_published_requests(scope, family_version=family_version,
                                                   invocations=(invocation,), numeric_only=numeric_only))[0]

    async def load_published_requests(
        self, scope: InvocationScope, *, family_version: ProductFamilyVersionBundle,
        invocations: Sequence[ProductRealizationInvocation],
        numeric_only: bool = False,
    ) -> tuple[ProductRealizationRequest, ...]:
        """Prepare the current published Family's exact closure for a bounded page."""
        from CAD2BIM_backend.domain.product_realization import MAX_PRODUCT_REALIZATION_BATCH_ITEMS
        if not 1 <= len(invocations) <= MAX_PRODUCT_REALIZATION_BATCH_ITEMS:
            raise ProductRealizationInputError("INPUT_INVALID", "Product batch exceeds its bounded input page")

        source_asset = next(
            asset
            for asset in family_version.assets
            if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE
        )
        source_bundle = await self._read_published_source(family_version, source_asset)
        if not numeric_only:
            await self._verify_published_assets(family_version)
        exact_geometry = await self.reauthorize_geometry_bindings(
            scope,
            family_version.geometry_bindings,
        )
        requests = await self._load_requests(
            scope,
            family_contract=family_version.version.family_contract,
            family_contract_digest=family_version.version.contract_digest,
            bindings=family_version.geometry_bindings,
            source_bundle=source_bundle,
            source_bundle_sha256=source_asset.sha256,
            invocations=invocations,
            exact_geometry=exact_geometry,
            numeric_only=numeric_only,
        )
        return requests

    async def prepare_published_closure(self, scope: InvocationScope, *, family_version: ProductFamilyVersionBundle):
        """Prepare numeric inputs once, leaving normal per-row errors to the caller."""
        source_asset = next(asset for asset in family_version.assets
            if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE)
        source_bundle = await self._read_published_source(family_version, source_asset)
        exact = await self.reauthorize_geometry_bindings(scope, family_version.geometry_bindings)
        bindings = await self._load_runtime_bindings(scope, family_version.geometry_bindings, exact, numeric_only=True)
        request = ProductRealizationRequest.model_construct(
            family_contract=family_version.version.family_contract,
            family_contract_digest=family_version.version.contract_digest,
            source_bundle=source_bundle, source_bundle_sha256=source_asset.sha256,
            geometry_bindings=bindings, invocation=None)
        request._require_closure()
        return _PreparedPublishedProductRealizationInputs(request)

    async def _load_requests(
        self,
        scope: InvocationScope,
        *,
        family_contract: ProductFamilyContract,
        family_contract_digest: str,
        bindings: Sequence[_Binding],
        source_bundle: bytes,
        source_bundle_sha256: str,
        invocations: Sequence[ProductRealizationInvocation],
        exact_geometry: Sequence[GeometryResourceVersionBundle],
        numeric_only: bool = False,
    ) -> tuple[ProductRealizationRequest, ...]:
        invocation_index: int | None = None
        try:
            runtime_bindings = await self._load_runtime_bindings(
                scope,
                bindings,
                exact_geometry,
                numeric_only=numeric_only,
            )
            base = ProductRealizationRequest.model_construct(
                    family_contract=family_contract,
                    family_contract_digest=family_contract_digest,
                    source_bundle=source_bundle,
                    source_bundle_sha256=source_bundle_sha256,
                    geometry_bindings=runtime_bindings,
                    invocation=None,
                )
            base._require_closure()
            prepared = _PreparedPublishedProductRealizationInputs(base)
            normalized_invocations = []
            for invocation_index, invocation in enumerate(invocations):
                normalized_invocations.append(prepared.normalize_invocation(invocation))
            invocation_index = None
            return prepared.requests(normalized_invocations)
        except ProductRealizationInputError:
            raise
        except ProductFamilyInvocationSemanticError as exc:
            public_message = (
                "Exact Product Realization inputs are invalid; "
                f"input_error_code={exc.reason}; domain_path={exc.invalid_path}; "
                f"correction={exc.correction}"
            )
            raise ProductRealizationInputError(
                "INPUT_INVALID",
                public_message,
                phase="output_validation",
                public_message=public_message,
                item_index=invocation_index,
            ) from exc
        except ValidationError as exc:
            public_message = validation_error_message(
                "Exact Product Realization inputs are invalid",
                exc,
            )
            raise ProductRealizationInputError(
                "INPUT_INVALID",
                public_message,
                phase="output_validation",
                public_message=public_message,
                item_index=invocation_index,
            ) from exc
        except (GeometryResourceBuildError, GeometryResourceIntegrityError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProductRealizationInputError(
                "INPUT_INVALID",
                _PRODUCT_REALIZATION_INPUT_INVALID_MESSAGE,
                phase="output_validation",
                item_index=invocation_index,
            ) from exc

    async def _load_runtime_bindings(
        self,
        scope: InvocationScope,
        bindings: Sequence[_Binding],
        exact_geometry: Sequence[GeometryResourceVersionBundle],
        *, candidate_assets=None,
        numeric_only: bool = False,
    ) -> tuple[ResolvedProductGeometryBinding, ...]:
        bundle_by_id = {bundle.version.resource_version_id: bundle for bundle in exact_geometry}
        resolved: list[ResolvedProductGeometryBinding] = []
        async def read_pair(binding, bundle, role):
            contents = candidate_assets.get(bundle.version.resource_version_id) if candidate_assets is not None else None
            if contents is None:
                return await self._required_geometry_asset_pair(scope, binding, role)
            asset = next((item for item in bundle.assets if item.asset_role is role), None)
            body = contents.get(role)
            if asset is None or body is None or len(body) != asset.size_bytes or hashlib.sha256(body).hexdigest() != asset.sha256:
                raise ProductRealizationInputError("GEOMETRY_CHANGED", "The prepared Geometry asset differs from its exact candidate")
            return asset, body
        for binding in bindings:
            bundle = bundle_by_id.get(binding.geometry_resource_version_id)
            if bundle is None or not _binding_matches_bundle(binding, bundle):
                raise ProductRealizationInputError(
                    "GEOMETRY_CHANGED",
                    "The exact Product Realization runtime geometry binding set is incomplete",
                    phase="output_validation",
                )
            if binding.expected_resource_kind is GeometryResourceKind.MAPPED_MODEL:
                if numeric_only:
                    resolved.append(ResolvedMappedGeometryBinding(
                        binding_key=binding.binding_key,
                        geometry_content_digest=binding.geometry_content_digest,
                        interfaces=bundle.version.manifest.get("interfaces", ()),
                    ))
                    continue
                asset, content = await read_pair(
                    binding, bundle,
                    GeometryResourceAssetRole.CANONICAL_GEOMETRY,
                )
                resolved.append(
                    ResolvedMappedGeometryBinding(
                        binding_key=binding.binding_key,
                        geometry_content_digest=binding.geometry_content_digest,
                        interfaces=bundle.version.manifest.get("interfaces", ()),
                        geometry=CanonicalGeometry(
                            format=CanonicalGeometryFormat(asset.asset_format),
                            content_type=asset.content_type,
                            payload=content,
                            sha256=asset.sha256,
                            summary=GeometrySummary.model_validate(
                                bundle.version.manifest["geometry_summary"]
                            ),
                        ),
                    )
                )
                continue

            source_asset, source_bundle = await read_pair(
                binding, bundle,
                GeometryResourceAssetRole.SOURCE_BUNDLE,
            )
            schema_asset, schema_content = await read_pair(
                binding, bundle,
                GeometryResourceAssetRole.PARAMETER_SCHEMA,
            )
            if (
                source_asset.asset_format != "source_bundle_v1"
                or source_asset.content_type != "application/zip"
                or schema_asset.asset_format != "json_schema"
                or schema_asset.content_type != "application/schema+json"
            ):
                raise ValueError("modeling-function source or parameter schema format changed")
            schema = json.loads(schema_content.decode("utf-8"))
            if (
                not isinstance(schema, dict)
                or canonical_json(schema).encode("utf-8") != schema_content
            ):
                raise ValueError("modeling-function parameter schema is not canonical JSON")
            resolved.append(
                ResolvedModelingFunctionBinding(
                    binding_key=binding.binding_key,
                    geometry_content_digest=binding.geometry_content_digest,
                    source_bundle=source_bundle,
                    source_bundle_sha256=source_asset.sha256,
                    parameter_schema=schema,
                    geometry_inputs=(await self._geometry_resources.load_build_inputs(scope.identity, inputs)
                        if not numeric_only and (inputs := geometry_resource_inputs(bundle.version.manifest)) else ()),
                )
            )
        return tuple(resolved)

    async def load_geometry_materials(self, scope: InvocationScope, *,
        family_version: ProductFamilyVersionBundle,
        requests: Sequence[ProductRealizationRequest], prechecks,
    ) -> tuple[ProductRealizationRequest, ...]:
        """Read selected heavy assets by the same already-fixed immutable versions."""
        if len(requests) != len(prechecks):
            raise ProductRealizationInputError("INPUT_INVALID", "Prechecks do not match the prepared input page")
        if not requests or any(result.execution_stage != "interface_constraints_evaluated" or result.frame_check_status != "PASS" for result in prechecks):
            raise ProductRealizationInputError("INPUT_INVALID", "Heavy geometry materials require every precheck to pass")
        for request, result in zip(requests, prechecks, strict=True):
            if (request.family_contract != family_version.version.family_contract
                    or request.invocation != result.invocation or request.policy != result.policy
                    or request.family_contract_digest != result.family_contract_digest
                    or request.source_bundle_sha256 != result.source_bundle_sha256):
                raise ProductRealizationInputError("INPUT_INVALID", "Precheck differs from the fixed Family invocation")
        selected = {fragment.binding_key for result in prechecks
                    if result.execution_stage == "interface_constraints_evaluated" and result.frame_check_status == "PASS"
                    for fragment in result.proposal.fragments}
        exact = await self.reauthorize_geometry_bindings(scope, family_version.geometry_bindings)
        bundle_by_id = {item.version.resource_version_id: item for item in exact}
        loaded = {item.binding_key: item for item in requests[0].geometry_bindings}
        # Inspect only selected exact metadata before consuming any heavy asset.
        for binding in family_version.geometry_bindings:
            if binding.binding_key not in selected or not isinstance(loaded[binding.binding_key], ResolvedMappedGeometryBinding):
                continue
            bundle = bundle_by_id[binding.geometry_resource_version_id]
            asset = next((item for item in bundle.assets
                          if item.asset_role is GeometryResourceAssetRole.CANONICAL_GEOMETRY), None)
            if asset is None or asset.asset_format != CanonicalGeometryFormat.OCCT_BREP_V1.value:
                raise ProductRealizationInputError("GEOMETRY_FORMAT_UNSUPPORTED",
                    "Selected exact mapped Geometry must use canonical OCCT BREP", phase="input_validation")
        for binding in family_version.geometry_bindings:
            if binding.binding_key not in selected:
                continue
            bundle = bundle_by_id[binding.geometry_resource_version_id]
            current = loaded[binding.binding_key]
            route = system_geometry_ifc_route(bundle)
            if isinstance(current, ResolvedMappedGeometryBinding):
                asset, body = await self._required_geometry_asset_pair(scope, binding, GeometryResourceAssetRole.CANONICAL_GEOMETRY)
                loaded[binding.binding_key] = current.model_copy(update={"ifc_representation_route": route, "geometry": CanonicalGeometry(
                    format=CanonicalGeometryFormat(asset.asset_format), content_type=asset.content_type,
                    payload=body, sha256=asset.sha256,
                    summary=GeometrySummary.model_validate(bundle.version.manifest["geometry_summary"]),
                )})
            else:
                inputs = geometry_resource_inputs(bundle.version.manifest)
                loaded[binding.binding_key] = current.model_copy(update={"ifc_representation_route": route,
                    "source_reuse_mode": system_geometry_source_reuse_mode(bundle), "geometry_inputs":
                    await self._geometry_resources.load_build_inputs(scope.identity, inputs) if inputs else ()})
        closure = tuple(loaded[item.binding_key] for item in requests[0].geometry_bindings)
        return tuple(request.model_copy(update={"geometry_bindings": closure}) for request in requests)


    async def _required_geometry_asset_pair(
        self,
        scope: InvocationScope,
        binding: _Binding,
        role: GeometryResourceAssetRole,
    ):
        last_error: Exception | None = None
        exact = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                exact = await self._geometry_resources.read_exact_asset(
                    scope.identity,
                    resource_id=binding.geometry_resource_id,
                    resource_version_id=binding.geometry_resource_version_id,
                    asset_role=role,
                )
                break
            except GeometryResourceIntegrityError as exc:
                raise ProductRealizationInputError(
                    "GEOMETRY_ASSET_INTEGRITY_FAILED", str(exc), phase="storage_verification",
                ) from exc
            except (ConnectionError, TimeoutError) as exc:
                last_error = exc
        else:
            raise ProductRealizationInputError(
                "STORAGE_UNAVAILABLE",
                "Exact Product Realization geometry asset exhausted bounded storage retries",
                phase="reconcile",
                retryable=True,
            ) from last_error
        if exact is None:
            raise GeometryResourceIntegrityError(
                f"Product Realization geometry is missing its exact {role.value} asset"
            )
        return exact

    async def _read_published_source(self, family_version, source_asset) -> bytes:
        last_error: Exception | None = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                return await self._family_assets.read_exact_asset(
                    family_version,
                    source_asset,
                )
            except ProductFamilyAssetIntegrityError as exc:
                raise ProductRealizationInputError(
                    "PUBLISHED_SOURCE_INTEGRITY_FAILED",
                    str(exc),
                    phase="storage_verification",
                ) from exc
            except (ConnectionError, TimeoutError) as exc:
                last_error = exc
        raise ProductRealizationInputError(
            "STORAGE_UNAVAILABLE",
            "Published ProductFamily source exhausted bounded storage retries",
            phase="reconcile",
            retryable=True,
        ) from last_error

    async def _verify_published_assets(self, family_version) -> None:
        last_error = None
        for _ in range(_MAX_TECHNICAL_ATTEMPTS):
            try:
                await self._family_assets.verify_bundle(family_version)
                return
            except ProductFamilyAssetIntegrityError as exc:
                raise ProductRealizationInputError(
                    "PUBLISHED_SOURCE_INTEGRITY_FAILED", str(exc), phase="storage_verification"
                ) from exc
            except (ConnectionError, TimeoutError) as exc:
                last_error = exc
        raise ProductRealizationInputError(
            "STORAGE_UNAVAILABLE", "Published Family assets exhausted bounded storage retries",
            phase="reconcile", retryable=True,
        ) from last_error



def _binding_matches_bundle(
    binding: _Binding,
    bundle: GeometryResourceVersionBundle,
) -> bool:
    if (
        binding.geometry_resource_id != bundle.resource.resource_id
        or binding.geometry_resource_version_id != bundle.version.resource_version_id
        or binding.geometry_content_digest != bundle.version.content_digest
        or binding.expected_resource_kind is not bundle.resource.resource_kind
    ):
        return False
    owner_scope = getattr(binding, "geometry_owner_scope", None)
    return owner_scope is None or owner_scope == bundle.resource.owner_scope


def normalize_product_realization_invocation(
    contract: ProductFamilyContract,
    invocation: ProductRealizationInvocation,
) -> ProductRealizationInvocation:
    """Apply the current parameter and explicit external-condition closure."""

    scenario = ProductRealizationValidationScenario(
        **invocation.model_dump(),
        scenario_key="realization_invocation",
        expected_status=ProductRealizationStatus.AMBIGUOUS,
    )
    try:
        normalized = normalize_product_family_validation_scenario(contract, scenario)
    except ProductFamilyInvocationSemanticError as exc:
        raise exc.for_realization_invocation() from exc
    except ValueError as exc:
        raise ValueError(
            str(exc).replace("validation scenario", "product realization invocation")
        ) from exc
    # Only the invocation origin uses length_unit. Contract parameters and the
    # explicit *_m conditions already carry their own metre semantics.
    powers = {
        None: 0, "ATTO": -18, "FEMTO": -15, "PICO": -12, "NANO": -9,
        "MICRO": -6, "MILLI": -3, "CENTI": -2, "DECI": -1,
        "DECA": 1, "HECTO": 2, "KILO": 3, "MEGA": 6, "GIGA": 9,
        "TERA": 12, "PETA": 15, "EXA": 18,
    }
    factor = 10.0 ** powers[normalized.length_unit.prefix]
    return ProductRealizationInvocation.model_validate({
        **normalized.to_invocation().model_dump(),
        "product_origin": tuple(value * factor for value in normalized.product_origin),
        "length_unit": IfcLengthUnit(name="METRE"),
    })


def normalize_product_realization_validation_scenario(
    contract: ProductFamilyContract,
    scenario: ProductRealizationValidationScenario,
) -> ProductRealizationValidationScenario:
    """Persist the same normalized invocation used by Runtime and input digests."""
    invocation = normalize_product_realization_invocation(contract, scenario.to_invocation())
    return ProductRealizationValidationScenario.model_validate({
        **scenario.model_dump(), **invocation.model_dump(),
    })


def _bounded_message(message: str) -> str:
    content = message.encode("utf-8")
    if len(content) <= _MAX_DIAGNOSTIC_BYTES:
        return message
    return content[: _MAX_DIAGNOSTIC_BYTES - 3].decode("utf-8", errors="ignore") + "..."


__all__ = [
    "ProductRealizationInputError",
    "ProductRealizationInputService",
    "normalize_product_realization_invocation",
]
