"""IfcOpenShell codec for exact, process-local IFC-SPF revisions."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import ifcopenshell
import ifcopenshell.guid
import ifcopenshell.util.unit

from CAD2BIM_backend.domain.ifc_model import (
    EncodedIfcRevision,
    IFC_TARGET_SCHEMA,
    IFC_TARGET_SCHEMA_FAMILY,
    IfcModelValidationError,
    IfcRevisionInspection,
)


# Durable IFC revisions are larger than CAD/Data payloads.  The validated
# Golden Candidate is about 231 MiB, while individual public Data payloads
# remain capped by their own 100 MiB contracts.
MAX_IFC_REVISION_BYTES = 512 * 1024 * 1024
_IFC_GLOBAL_ID = re.compile(r"^[0-9A-Za-z_$]{22}$")


@dataclass(slots=True)
class IfcOpenShellPrivateModel:
    """One mutable private copy reconstructed from immutable exact bytes."""

    model: Any

    def to_bytes(self) -> bytes:
        return self.model.to_string().encode("utf-8")


class IfcOpenShellModelCodec:
    """Create, copy, serialize, and independently inspect IFC4X3 ADD2 SPF."""

    @staticmethod
    def stable_project_global_id(identity_seed: str) -> str:
        normalized = identity_seed.strip() if isinstance(identity_seed, str) else ""
        if not normalized:
            raise ValueError("identity_seed must not be empty")
        value = uuid5(NAMESPACE_URL, f"cad2bim:ifc-model:project:{normalized}")
        return ifcopenshell.guid.compress(value.hex)

    def create_revision1(
        self,
        *,
        project_global_id: str,
        project_name: str,
        header_timestamp: datetime,
    ) -> EncodedIfcRevision:
        import ifcopenshell.api.project

        if _IFC_GLOBAL_ID.fullmatch(project_global_id) is None:
            raise IfcModelValidationError("project_global_id is not a valid IFC GlobalId")
        normalized_project_name = _normalize_project_name(project_name)
        timestamp = _stable_timestamp(header_timestamp)
        try:
            model = ifcopenshell.api.project.create_file(version=IFC_TARGET_SCHEMA_FAMILY)
            _set_stable_header(model, timestamp)
            model.create_entity(
                "IfcProject",
                GlobalId=project_global_id,
                Name=normalized_project_name,
            )
            content = model.to_string().encode("utf-8")
        except Exception as exc:
            raise IfcModelValidationError("IFC revision 1 could not be created") from exc
        inspection = self.inspect_revision1(
            content,
            expected_project_global_id=project_global_id,
            expected_project_name=normalized_project_name,
        )
        return EncodedIfcRevision(content=content, inspection=inspection)

    def inspect(self, content: bytes) -> IfcRevisionInspection:
        model = _open_exact(content)
        return self.inspect_from_model(content, model)

    def inspect_from_model(self, content: bytes, model: Any) -> IfcRevisionInspection:
        """Inspect actual bytes against their same-call independently opened model."""
        schema_identifier = str(getattr(model, "schema_identifier", "") or "")
        if schema_identifier != IFC_TARGET_SCHEMA:
            raise IfcModelValidationError(
                f"IFC schema must be {IFC_TARGET_SCHEMA}, got {schema_identifier or 'unknown'}"
            )
        projects = tuple(model.by_type("IfcProject"))
        if len(projects) != 1:
            raise IfcModelValidationError("IFC revision must contain exactly one IfcProject")
        project = projects[0]
        global_id = str(getattr(project, "GlobalId", "") or "")
        if _IFC_GLOBAL_ID.fullmatch(global_id) is None:
            raise IfcModelValidationError("IfcProject must have a valid GlobalId")

        contexts = tuple(model.by_type("IfcGeometricRepresentationContext"))
        units = tuple(model.by_type("IfcUnitAssignment"))
        # Imported lazily to keep the base codec independent at module import
        # time while making the strict inspector the only geometry-basis fact
        # source.
        from CAD2BIM_backend.infrastructure.ifc.geometry_basis import (
            IfcOpenShellGeometryBasisCompiler,
        )

        basis_compiler = IfcOpenShellGeometryBasisCompiler(self)
        geometry_basis = basis_compiler.inspect_geometry_basis_from_model(model)
        project_name = _normalize_project_name(getattr(project, "Name", None))
        return IfcRevisionInspection(
            schema_identifier=IFC_TARGET_SCHEMA,
            project_global_id=global_id,
            project_name=project_name,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            site_count=len(model.by_type("IfcSite")),
            element_count=len(model.by_type("IfcElement")),
            unit_assignment_count=len(units),
            geometric_context_count=len(contexts),
            product_body_contexts=basis_compiler.inspect_product_body_contexts_from_model(model),
            project_length_unit_m=(float(ifcopenshell.util.unit.calculate_unit_scale(model))
                                   if geometry_basis is not None else None),
            geometry_basis_configured=geometry_basis is not None,
            geometry_basis=geometry_basis,
        )

    def inspect_revision1(
        self,
        content: bytes,
        *,
        expected_project_global_id: str,
        expected_project_name: str,
    ) -> IfcRevisionInspection:
        return self.inspect_revision1_from_model(content, _open_exact(content),
            expected_project_global_id=expected_project_global_id,
            expected_project_name=expected_project_name)

    def inspect_revision1_from_model(self, content: bytes, model: Any, *,
                                    expected_project_global_id: str,
                                    expected_project_name: str) -> IfcRevisionInspection:
        inspection = self.inspect_from_model(content, model)
        if inspection.project_global_id != expected_project_global_id:
            raise IfcModelValidationError("IfcProject GlobalId changed")
        if inspection.project_name != _normalize_project_name(expected_project_name):
            raise IfcModelValidationError("IfcProject Name changed")
        if inspection.site_count:
            raise IfcModelValidationError("IFC revision 1 must not contain IfcSite")
        if inspection.unit_assignment_count:
            raise IfcModelValidationError("IFC revision 1 must not contain IfcUnitAssignment")
        if inspection.geometric_context_count:
            raise IfcModelValidationError("IFC revision 1 must not contain representation contexts")
        if model.by_type("IfcProduct"):
            raise IfcModelValidationError("IFC revision 1 must not contain IfcProduct")
        if inspection.geometry_basis_configured or inspection.geometry_basis is not None:
            raise IfcModelValidationError("IFC revision 1 must not configure geometry basis")
        return inspection

    def private_copy(self, content: bytes) -> IfcOpenShellPrivateModel:
        return IfcOpenShellPrivateModel(_open_exact(content))

    def private_copy_from_source_fd(
        self, content: bytes, *, descriptor: int,
    ) -> IfcOpenShellPrivateModel:
        """Parse an owned source capability whose bytes the caller verified.

        The revision worker owns the unlinked read-only source for the whole
        command and has checked its actual bytes against the staged size/SHA.
        Reuse that verification instead of scanning or hashing the file again.
        """
        import fcntl

        try:
            source = os.fstat(descriptor)
            if (not stat.S_ISREG(source.st_mode)
                    or fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
                    or source.st_size != len(content)):
                raise ValueError("source descriptor must identify the exact read-only regular file")
            os.lseek(descriptor, 0, os.SEEK_SET)
            model = _open_exact(content, saved_path=Path(f"/dev/fd/{descriptor}"))
            current = os.fstat(descriptor)
            if (source.st_dev, source.st_ino, source.st_size) != (
                current.st_dev, current.st_ino, current.st_size,
            ):
                raise ValueError("source descriptor identity changed during native parsing")
            return IfcOpenShellPrivateModel(model)
        except IfcModelValidationError:
            raise
        except Exception as exc:
            raise IfcModelValidationError("exact IFC source could not be opened") from exc

    def private_copy_from_saved_file(
        self, content: bytes, *, path: Path, descriptor: int,
    ) -> IfcOpenShellPrivateModel:
        """Reopen an owned candidate, binding its actual file bytes to content.

        The descriptor is the existing candidate read capability. No source or
        public path uses this method; the caller keeps the private candidate
        alive and does not write it again during inspection.
        """
        try:
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                stream.seek(0)
                saved = os.fstat(stream.fileno())
                named = path.lstat()
                if (not stat.S_ISREG(named.st_mode)
                        or (saved.st_dev, saved.st_ino) != (named.st_dev, named.st_ino)):
                    raise ValueError("saved candidate path does not identify its read descriptor")
                offset = 0
                while chunk := stream.read(1024 * 1024):
                    if chunk != content[offset : offset + len(chunk)]:
                        raise ValueError("saved candidate differs from compiler output")
                    offset += len(chunk)
                if offset != len(content):
                    raise ValueError("saved candidate differs from compiler output")
            return IfcOpenShellPrivateModel(_open_exact(content, saved_path=path))
        except IfcModelValidationError:
            raise
        except Exception as exc:
            raise IfcModelValidationError("saved IFC candidate could not be reopened") from exc


def _open_exact(content: bytes, *, saved_path: Path | None = None) -> Any:
    if not isinstance(content, bytes) or not content:
        raise IfcModelValidationError("IFC revision must be non-empty bytes")
    if len(content) > MAX_IFC_REVISION_BYTES:
        raise IfcModelValidationError("IFC revision exceeds the size limit")
    if not content.lstrip().startswith(b"ISO-10303-21;"):
        raise IfcModelValidationError("IFC revision is not an SPF payload")
    try:
        text = content.decode("utf-8")
        if saved_path is not None:
            # Strict UTF-8 validation remains identical to the bytes path.
            # The native file reader avoids from_string's slower in-memory
            # parsing. Its caller has already verified the actual file bytes.
            del text
            return ifcopenshell.open(saved_path, format=".ifc")
        return ifcopenshell.file.from_string(text)
    except Exception as exc:
        raise IfcModelValidationError("IFC revision could not be opened") from exc


def _stable_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("header_timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_project_name(value: object) -> str:
    if not isinstance(value, str):
        raise IfcModelValidationError("IfcProject Name must be a string")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise IfcModelValidationError("IfcProject Name must not contain control characters")
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise IfcModelValidationError("IfcProject Name must contain 1 to 255 characters")
    return normalized


def _set_stable_header(model: Any, timestamp: str) -> None:
    model.header.file_name.name = "model.ifc"
    model.header.file_name.time_stamp = timestamp
    model.header.file_name.author = ("CAD2BIM",)
    model.header.file_name.organization = ("CAD2BIM",)
    model.header.file_name.preprocessor_version = "IfcOpenShell"
    model.header.file_name.originating_system = "CAD2BIM"
    model.header.file_name.authorization = ""


__all__ = [
    "IfcOpenShellModelCodec",
    "IfcOpenShellPrivateModel",
    "MAX_IFC_REVISION_BYTES",
]
