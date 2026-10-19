"""Geometry-authoring adapter over the shared exact source-bundle utilities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from CAD2BIM_backend.domain.geometry_catalog import GeometrySourceMember
from CAD2BIM_backend.domain.text_source import SourceBundleChange
from CAD2BIM_backend.infrastructure.source_text.source_bundle import (
    MAX_SOURCE_BUNDLE_MEMBER_BYTES,
    SourceBundle,
    SourceBundleError,
    SourceBundleMemberNotFoundError,
    SourceBundleMemberNotTextError,
    inspect_source_bundle,
    pack_source_bundle,
)
from CAD2BIM_backend.infrastructure.source_text.source_bundle_patch import (
    apply_python_source_bundle_changes,
)
from CAD2BIM_backend.infrastructure.source_text.source_patch import SourcePatchError
from CAD2BIM_backend.ports.geometry_authoring import (
    GeometryAuthoringPackedSource,
    GeometryAuthoringSourceError,
    GeometryAuthoringSourceFailure,
)


@dataclass(frozen=True, slots=True)
class _InspectedSourceBundle:
    bundle: SourceBundle

    @property
    def sha256(self) -> str:
        return self.bundle.sha256

    @property
    def members(self) -> tuple[GeometrySourceMember, ...]:
        return tuple(
            GeometrySourceMember(
                source_path=member.path,
                size_bytes=member.size_bytes,
                sha256=member.sha256,
                text_available=member.text_available,
            )
            for member in self.bundle.members
        )

    def read_text(self, path: str) -> str:
        try:
            return self.bundle.read_text(path)
        except SourceBundleMemberNotFoundError as exc:
            raise _source_error(GeometryAuthoringSourceFailure.MEMBER_NOT_FOUND, exc) from exc
        except SourceBundleMemberNotTextError as exc:
            raise _source_error(GeometryAuthoringSourceFailure.MEMBER_NOT_TEXT, exc) from exc
        except SourceBundleError as exc:
            raise _source_error(GeometryAuthoringSourceFailure.INVALID, exc) from exc


class SourceTextGeometryAuthoringSource:
    """Reuse the canonical ZIP codec and Textum-backed CAS patch implementation."""

    @property
    def maximum_text_member_bytes(self) -> int:
        return MAX_SOURCE_BUNDLE_MEMBER_BYTES

    def pack(self, members: Mapping[str, bytes]) -> GeometryAuthoringPackedSource:
        try:
            content = pack_source_bundle(members)
            inspected = inspect_source_bundle(content)
        except SourceBundleError as exc:
            raise _source_error(GeometryAuthoringSourceFailure.INVALID, exc) from exc
        return GeometryAuthoringPackedSource(content=content, sha256=inspected.sha256)

    def inspect(self, content: bytes) -> _InspectedSourceBundle:
        try:
            return _InspectedSourceBundle(inspect_source_bundle(content))
        except SourceBundleError as exc:
            raise _source_error(GeometryAuthoringSourceFailure.INVALID, exc) from exc

    def validate_function_source(self, content: bytes) -> None:
        from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_execution import compile_geometry_interfaces
        from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import PythonBundleValidationError
        try:
            compile_geometry_interfaces(content, "cad2bim_function_interfaces")
        except PythonBundleValidationError as exc:
            raise _source_error(GeometryAuthoringSourceFailure.INVALID, exc) from exc

    def mutate(
        self,
        content: bytes,
        expected_sha256: str,
        changes: Sequence[SourceBundleChange],
        *,
        maximum_members: int,
        maximum_member_bytes: int,
        maximum_total_bytes: int,
    ) -> GeometryAuthoringPackedSource:
        try:
            mutated = apply_python_source_bundle_changes(
                content,
                expected_sha256,
                tuple(changes),
                maximum_members=maximum_members,
                maximum_member_bytes=maximum_member_bytes,
                maximum_total_bytes=maximum_total_bytes,
            )
        except SourcePatchError as exc:
            raise _source_error(GeometryAuthoringSourceFailure(exc.failure.value), exc) from exc
        except SourceBundleMemberNotFoundError as exc:
            raise _source_error(GeometryAuthoringSourceFailure.MEMBER_NOT_FOUND, exc) from exc
        except SourceBundleMemberNotTextError as exc:
            raise _source_error(GeometryAuthoringSourceFailure.MEMBER_NOT_TEXT, exc) from exc
        except SourceBundleError as exc:
            raise _source_error(GeometryAuthoringSourceFailure.INVALID, exc) from exc
        return GeometryAuthoringPackedSource(content=mutated.content, sha256=mutated.sha256)


def _source_error(
    failure: GeometryAuthoringSourceFailure,
    error: Exception,
) -> GeometryAuthoringSourceError:
    return GeometryAuthoringSourceError(failure, str(error))


__all__ = ["SourceTextGeometryAuthoringSource"]
