"""Integrity boundary for exact ProductFamilyVersion-owned assets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyProgramSource

from CAD2BIM_backend.domain.product_family import (
    ProductFamilyRecord,
    ProductFamilyVersionAssetRecord,
    ProductFamilyVersionAssetRole,
    ProductFamilyVersionBundle,
)
from CAD2BIM_backend.domain.product_realization import (
    ProductFamilyStandardModelManifest,
    product_geometry_binding_digest,
)
from CAD2BIM_backend.ports.geometry_authoring import (
    GeometryAuthoringSourceError,
    GeometryAuthoringSourcePort,
)
from CAD2BIM_backend.ports.storage import (
    ProductFamilyVersionStorageKey,
    StorageConflictError,
    StorageObjectNotFoundError,
    StoragePort,
    StorageValidationError,
)


class ProductFamilyAssetIntegrityError(RuntimeError):
    """Database-owned asset metadata and immutable stored bytes disagree."""


class ProductFamilyAssetService:
    """Read and verify version-owned bytes through the shared StoragePort."""

    def __init__(
        self,
        storage: StoragePort,
        source_bundles: GeometryAuthoringSourcePort,
    ) -> None:
        self._storage = storage
        self._source_bundles = source_bundles

    async def verify_bundle(self, bundle: ProductFamilyVersionBundle) -> None:
        """Require every version asset to be independently readable and exact."""
        await self._read_verified_bundle(bundle)

    async def _read_verified_bundle(
        self,
        bundle: ProductFamilyVersionBundle,
    ) -> tuple[
        dict[ProductFamilyVersionAssetRole, bytes],
        ProductFamilyStandardModelManifest | None,
    ]:
        content_by_role: dict[ProductFamilyVersionAssetRole, bytes] = {}
        for asset in bundle.assets:
            content_by_role[asset.asset_role] = await self.read_exact_asset(bundle, asset)
        if ProductFamilyVersionAssetRole.VALIDATION_REPORT in content_by_role:
            raise ProductFamilyAssetIntegrityError(
                "legacy ProductFamily validation-report bundles are unsupported; "
                "only their exact source bytes remain readable"
            )
        manifest = None
        if bundle.version.family_contract.standard_model is not None:
            manifest = self._parse_standard_model_manifest(
                bundle,
                content_by_role[ProductFamilyVersionAssetRole.STANDARD_MODEL_MANIFEST],
            )
        return content_by_role, manifest

    async def standard_model_assets(
        self,
        bundle: ProductFamilyVersionBundle,
    ) -> tuple[
        ProductFamilyStandardModelManifest,
        bytes,
    ]:
        """Return the exact nominal manifest and BREP after full bundle verification."""
        if bundle.version.family_contract.standard_model is None:
            raise ProductFamilyAssetIntegrityError("historical Family has no standard model assets")
        content, manifest = await self._read_verified_bundle(bundle)
        if manifest is None:
            raise ProductFamilyAssetIntegrityError("standard Family is missing its verified manifest")
        return manifest, content[ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY]

    @staticmethod
    def _parse_standard_model_manifest(
        bundle: ProductFamilyVersionBundle,
        content: bytes,
    ) -> ProductFamilyStandardModelManifest:
        try:
            raw = json.loads(content.decode("utf-8"))
            if canonical_json(raw).encode("utf-8") != content:
                raise ValueError("noncanonical manifest")
            geometry = next(item for item in bundle.assets if item.asset_role is ProductFamilyVersionAssetRole.STANDARD_MODEL_GEOMETRY)
            manifest = ProductFamilyStandardModelManifest.model_validate(raw)
            source = next(
                item
                for item in bundle.assets
                if item.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE
            )
            expected_binding_digest = _binding_digest(bundle)
            if (
                manifest.family_version_id != bundle.version.family_version_id
                or manifest.contract_digest != bundle.version.contract_digest
                or manifest.source_bundle_sha256 != source.sha256
                or manifest.binding_digest != expected_binding_digest
                or manifest.geometry_sha256 != geometry.sha256
            ):
                raise ValueError("inconsistent standard model manifest")
            return manifest
        except (UnicodeDecodeError, ValueError, TypeError, StopIteration, AttributeError) as exc:
            raise ProductFamilyAssetIntegrityError("standard model manifest disagrees with the exact Family and geometry") from exc


    async def write_exact_asset(
        self,
        family: ProductFamilyRecord,
        asset: ProductFamilyVersionAssetRecord,
        content: bytes,
    ) -> ProductFamilyVersionAssetRecord:
        """Write and independently reread one immutable candidate asset."""

        if len(content) != asset.size_bytes or hashlib.sha256(content).hexdigest() != asset.sha256:
            raise ProductFamilyAssetIntegrityError(
                "product family candidate asset bytes do not match their metadata"
            )
        key = self.candidate_storage_key(family, asset)
        try:
            stored = await self._storage.write(
                key,
                filename=asset.filename,
                content_type=asset.content_type,
                chunks=_one_chunk(content),
            )
            reread = await self._storage.read_verified(key)
        except (StorageConflictError, StorageValidationError) as exc:
            raise ProductFamilyAssetIntegrityError(
                "product family candidate asset could not be stored exactly"
            ) from exc
        if (
            stored.key != key
            or stored.filename != asset.filename
            or stored.content_type != asset.content_type
            or stored.size_bytes != asset.size_bytes
            or stored.sha256 != asset.sha256
            or reread.stored != stored
            or reread.content != content
            or reread.verified_sha256 != asset.sha256
        ):
            raise ProductFamilyAssetIntegrityError(
                "product family candidate asset changed before commit"
            )
        return asset.model_copy(update={"created_at": stored.created_at})

    async def read_exact_asset(
        self,
        bundle: ProductFamilyVersionBundle,
        asset: ProductFamilyVersionAssetRecord,
    ) -> bytes:
        """Read one exact asset after metadata, digest, and format validation."""

        if asset.family_version_id != bundle.version.family_version_id:
            raise ProductFamilyAssetIntegrityError(
                "product family asset belongs to another exact version"
            )
        content = await self._read_asset_bytes(bundle.family, asset)
        return content

    async def read_program_source(self, source: ProductFamilyProgramSource) -> bytes:
        """Read verified bytes for the exact authorized source owner."""
        return await self._read_asset_bytes(source.family, source.source_asset)

    async def _read_asset_bytes(
        self, family: ProductFamilyRecord, asset: ProductFamilyVersionAssetRecord,
    ) -> bytes:
        key = self.candidate_storage_key(family, asset)
        try:
            verified = await self._storage.read_verified(key)
        except (
            FileNotFoundError,
            StorageConflictError,
            StorageObjectNotFoundError,
            StorageValidationError,
        ) as exc:
            raise ProductFamilyAssetIntegrityError(
                "product family version asset is missing or corrupt"
            ) from exc
        stored = verified.stored
        content = verified.content
        if (
            stored.key != key
            or stored.filename != asset.filename
            or stored.content_type != asset.content_type
            or stored.size_bytes != asset.size_bytes
            or stored.sha256 != asset.sha256
            or len(content) != asset.size_bytes
            or verified.verified_sha256 != asset.sha256
        ):
            raise ProductFamilyAssetIntegrityError(
                "product family version asset metadata or digest does not match"
            )
        if asset.asset_role is ProductFamilyVersionAssetRole.REALIZATION_SOURCE_BUNDLE:
            try:
                source = self._source_bundles.inspect(content)
                entry = next(member for member in source.members if member.source_path == "main.py")
            except (GeometryAuthoringSourceError, StopIteration) as exc:
                raise ProductFamilyAssetIntegrityError(
                    "product family realization source is not a canonical bundle with main.py"
                ) from exc
            if not entry.text_available:
                raise ProductFamilyAssetIntegrityError(
                    "product family realization main.py is not UTF-8 source text"
                )
        return content

    @staticmethod
    def storage_key(
        bundle: ProductFamilyVersionBundle,
        asset: ProductFamilyVersionAssetRecord,
    ) -> ProductFamilyVersionStorageKey:
        """Derive the only logical address from database-owned immutable identities."""

        return ProductFamilyAssetService.candidate_storage_key(bundle.family, asset)

    @staticmethod
    def candidate_storage_key(
        family: ProductFamilyRecord,
        asset: ProductFamilyVersionAssetRecord,
    ) -> ProductFamilyVersionStorageKey:
        return ProductFamilyVersionStorageKey(
            scope_kind=family.owner_scope.scope_kind.name,
            scope_id=family.owner_scope.scope_id,
            family_version_id=asset.family_version_id,
            asset_role=asset.asset_role.value,
        )


def _binding_digest(bundle: ProductFamilyVersionBundle) -> str:
    return product_geometry_binding_digest(
        (item.binding_key, item.expected_resource_kind, item.geometry_content_digest)
        for item in bundle.geometry_bindings
    )


async def _one_chunk(content: bytes) -> AsyncIterator[bytes]:
    yield content


__all__ = ["ProductFamilyAssetIntegrityError", "ProductFamilyAssetService"]
