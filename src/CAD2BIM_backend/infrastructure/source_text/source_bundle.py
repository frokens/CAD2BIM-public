"""Deterministic, bounded ZIP codec shared by exact-source bundle consumers."""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath


# A canonical bundle may preserve one approved raw IFC/BRep source as well as
# authoring text.  The extra MiB on the ZIP envelope prevents a 64 MiB
# incompressible member plus deterministic headers from contradicting the
# uncompressed member/total boundary.
MAX_SOURCE_BUNDLE_BYTES = 65 * 1024 * 1024
MAX_SOURCE_BUNDLE_MEMBER_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BUNDLE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BUNDLE_MEMBERS = 64

_CANONICAL_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_CANONICAL_FILE_MODE = (stat.S_IFREG | 0o644) << 16


class SourceBundleError(ValueError):
    """A bundle violates the shared deterministic ZIP safety contract."""


class SourceBundleMemberNotFoundError(SourceBundleError, LookupError):
    """A normalized source path is absent from an exact bundle."""


class SourceBundleMemberNotTextError(SourceBundleError):
    """A selected exact member cannot be viewed as UTF-8 source text."""


@dataclass(frozen=True, slots=True)
class SourceBundleMember:
    path: str
    size_bytes: int
    sha256: str
    text_available: bool


@dataclass(frozen=True, slots=True)
class SourceBundle:
    """A fully bounded private bundle; member bytes never become a physical path."""

    sha256: str
    members: tuple[SourceBundleMember, ...]
    _contents: Mapping[str, bytes]

    def read(self, path: str) -> bytes:
        normalized = normalize_source_bundle_path(path)
        try:
            return self._contents[normalized]
        except KeyError as exc:
            raise SourceBundleMemberNotFoundError(
                "source path is not present in the exact source bundle"
            ) from exc

    def read_text(self, path: str) -> str:
        content = self.read(path)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceBundleMemberNotTextError(
                "selected source bundle member is binary, not UTF-8 text"
            ) from exc
        if "\x00" in text:
            raise SourceBundleMemberNotTextError(
                "selected source bundle member is binary, not UTF-8 text"
            )
        return text


def pack_source_bundle(members: Mapping[str, bytes]) -> bytes:
    """Pack exact bytes into the one canonical source-bundle ZIP representation."""

    if not isinstance(members, Mapping) or not 1 <= len(members) <= MAX_SOURCE_BUNDLE_MEMBERS:
        raise SourceBundleError("source bundle has an invalid member count")
    normalized: dict[str, bytes] = {}
    total_bytes = 0
    for raw_path, content in members.items():
        path = normalize_source_bundle_path(raw_path)
        if path in normalized:
            raise SourceBundleError("source bundle member paths must be unique")
        if not isinstance(content, bytes):
            raise SourceBundleError("source bundle members must contain exact bytes")
        if len(content) > MAX_SOURCE_BUNDLE_MEMBER_BYTES:
            raise SourceBundleError("source bundle member exceeds its byte budget")
        total_bytes += len(content)
        if total_bytes > MAX_SOURCE_BUNDLE_TOTAL_BYTES:
            raise SourceBundleError("source bundle exceeds its total member byte budget")
        normalized[path] = content

    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for path in sorted(normalized):
            info = zipfile.ZipInfo(path, date_time=_CANONICAL_TIMESTAMP)
            info.create_system = 3
            info.external_attr = _CANONICAL_FILE_MODE
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, normalized[path], compresslevel=9)
    content = output.getvalue()
    if len(content) > MAX_SOURCE_BUNDLE_BYTES:
        raise SourceBundleError("source bundle ZIP exceeds its byte budget")
    # The same decoder verifies that the emitted archive satisfies all read invariants.
    inspect_source_bundle(content)
    return content


def inspect_source_bundle(content: bytes) -> SourceBundle:
    """Validate and privately materialize one exact bounded source bundle."""

    if not isinstance(content, bytes) or not content or len(content) > MAX_SOURCE_BUNDLE_BYTES:
        raise SourceBundleError("source bundle ZIP exceeds its byte budget")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), mode="r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise SourceBundleError("source bundle is not a valid ZIP") from exc

    with archive:
        infos = archive.infolist()
        if not 1 <= len(infos) <= MAX_SOURCE_BUNDLE_MEMBERS:
            raise SourceBundleError("source bundle has an invalid member count")
        names = tuple(info.filename for info in infos)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise SourceBundleError("source bundle members must be unique and sorted")
        if sum(info.file_size for info in infos) > MAX_SOURCE_BUNDLE_TOTAL_BYTES:
            raise SourceBundleError("source bundle exceeds its total member byte budget")

        records: list[SourceBundleMember] = []
        contents: dict[str, bytes] = {}
        for info in infos:
            path = normalize_source_bundle_path(info.filename)
            unix_mode = info.external_attr >> 16
            if (
                info.is_dir()
                or stat.S_ISLNK(unix_mode)
                or info.flag_bits & 0x1
                or info.compress_type != zipfile.ZIP_DEFLATED
                or info.date_time != _CANONICAL_TIMESTAMP
                or info.create_system != 3
                or info.external_attr != _CANONICAL_FILE_MODE
                or info.file_size > MAX_SOURCE_BUNDLE_MEMBER_BYTES
            ):
                raise SourceBundleError("source bundle contains an unsupported member")
            try:
                member = archive.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise SourceBundleError("source bundle member could not be read") from exc
            if len(member) != info.file_size:
                raise SourceBundleError("source bundle member size is inconsistent")
            text_available = _is_utf8_text(member)
            records.append(
                SourceBundleMember(
                    path=path,
                    size_bytes=len(member),
                    sha256=hashlib.sha256(member).hexdigest(),
                    text_available=text_available,
                )
            )
            contents[path] = member
    return SourceBundle(
        sha256=hashlib.sha256(content).hexdigest(),
        members=tuple(records),
        _contents=contents,
    )


def normalize_source_bundle_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255 or "\\" in value:
        raise SourceBundleError("source path must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SourceBundleError("source path must be a normalized relative POSIX path")
    return value


def _is_utf8_text(content: bytes) -> bool:
    try:
        return "\x00" not in content.decode("utf-8")
    except UnicodeDecodeError:
        return False


__all__ = [
    "MAX_SOURCE_BUNDLE_BYTES",
    "MAX_SOURCE_BUNDLE_MEMBER_BYTES",
    "MAX_SOURCE_BUNDLE_MEMBERS",
    "MAX_SOURCE_BUNDLE_TOTAL_BYTES",
    "SourceBundle",
    "SourceBundleError",
    "SourceBundleMember",
    "SourceBundleMemberNotFoundError",
    "SourceBundleMemberNotTextError",
    "inspect_source_bundle",
    "normalize_source_bundle_path",
    "pack_source_bundle",
]
