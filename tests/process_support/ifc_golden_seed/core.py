"""Deterministic, filesystem-only core for IFC Golden Seed test fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Mapping
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

import rfc8785

CHUNK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_OBJECT_BYTES = 512 * 1024 * 1024
MAX_FILENAME_BYTES = 255

_FORMAT_VERSION = "ifc-golden-seed/v1"
_SEED_KINDS = {"m4_run", "ifc_stage"}
_SEED_STATES = ("candidate", "verified", "promoted")
_SHA256_LENGTH = 64
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
_TOP_LEVEL_FIELDS = {
    "format_version",
    "seed_kind",
    "state",
    "source_commit",
    "source_migration_head",
    "source_m4_round",
    "source_arm",
    "source_direct_session_alias",
    "source_task_id",
    "source_run_id",
    "source_catalog_frontier",
    "source_operation_frontier",
    "source_catalog_frontier_digest",
    "source_operation_frontier_digest",
    "created_at",
    "object_inventory",
    "artifact_inventory_digest",
    "database_rows_digest",
    "parent_seed_sha256",
    "m4_run_seed_sha256",
    "previous_record_sha256",
    "verification_proof",
    "promotion_proof",
    "stage_evidence",
    "root_sha256",
}
_REQUIRED_FIELDS = {"format_version", "seed_kind", "state", "object_inventory"}
_OBJECT_FIELDS = {
    "path",
    "filename",
    "media_type",
    "size",
    "sha256",
    "source_artifact_id",
    "source_artifact_version_id",
    "reverse_references",
}
_REQUIRED_OBJECT_FIELDS = {"path", "filename", "media_type", "size", "sha256"}
_VERIFICATION_PROOF_FIELDS = {
    "oracle_result_sha256",
    "parent_regression_sha256",
    "goal_only_run_sha256s",
}
_PROMOTION_PROOF_FIELDS = {"approval_sha256"}
_STAGE_EVIDENCE_FIELDS = {
    "visibility",
    "stage_key",
    "current_ifc_model_id",
    "current_ifc_revision_id",
    "current_ifc_revision_no",
    "current_ifc_artifact_id",
    "current_ifc_artifact_version_id",
    "current_ifc_sha256",
    "provider_schema_sha256",
    "tool_descriptions_sha256",
    "system_prompt_sha256",
    "stage_goal_sha256",
    "enabled_tools_sha256",
    "model",
    "reasoning_effort",
    "budget_sha256",
    "oracle_version",
    "source_tree_clean",
    "source_diff_sha256",
    "agent_runs",
}
_STAGE_AGENT_RUN_FIELDS = {
    "run_key",
    "mode",
    "parent_seed_sha256",
    "event_manifest_sha256",
}
_STAGE_KEYS = {
    "foundation",
    "spatial_root",
    "basic_products",
    "connected_products",
    "systems_and_groups",
    "enriched_products",
    "full_product_families",
    "full_golden_candidate",
}
_STAGE_RUN_MODES = {"diagnostic_guided", "goal_only_semiblind"}
_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_EMPTY_DIFF_SHA256 = hashlib.sha256(b"").hexdigest()
_SENSITIVE_KEY_PARTS = {"password", "passwd", "secret", "token", "credential", "api_key"}
_SENSITIVE_ASSIGNMENT = re.compile(
    rb"(?i)(?P<name>[a-z_][a-z0-9_-]*)(?=\s*[:=]\s*[^\s,;]+)"
)
_SENSITIVE_ASSIGNMENT_NAME = re.compile(
    rb"(?i)(?:password|passwd|secret|api[_-]?key|token)"
)
_NON_CREDENTIAL_TOKEN_ASSIGNMENTS = {
    "angle_token",
    "diameter_token",
    "model_token",
}
_TEXT_SUFFIXES = {
    ".csv",
    ".dxf",
    ".json",
    ".md",
    ".py",
    ".sql",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_SENSITIVE_BYTES = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"(?i)[a-z][a-z0-9+.-]*://[^/@\s]+@"),
    re.compile(rb"(?i)file://"),
    re.compile(rb"(?i)(?:^|[\s\"'=])[a-z]:[\\/][^\s\"']+"),
    re.compile(rb"(?:^|[\s\"'=])\\\\[^\s\"']+"),
    re.compile(rb"(?:^|[\s\"'=])//[^\s\"']+"),
    re.compile(rb"(?:^|[\s\"'=])/(?!/)[A-Za-z0-9._-]+(?:/[^\s\"']+)*"),
    re.compile(rb"(?:^|[\s\"'=])~/[^\s\"']+"),
    re.compile(rb"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$(?:HOME|USER|PWD|CODEX_HOME)\b"),
)


class SeedValidationError(ValueError):
    """Raised when a Seed manifest or its object inventory is invalid."""


def _validate_size(value: Any, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise SeedValidationError(f"{field} is outside the permitted byte range")
    return value


def _validate_sha256(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SeedValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _contains_sensitive_bytes(value: bytes) -> bool:
    if any(pattern.search(value) for pattern in _SENSITIVE_BYTES):
        return True
    for match in _SENSITIVE_ASSIGNMENT.finditer(value):
        name = match.group("name").decode("ascii").lower()
        if name in _NON_CREDENTIAL_TOKEN_ASSIGNMENTS:
            continue
        if _SENSITIVE_ASSIGNMENT_NAME.search(match.group("name")):
            return True
    return False


def _scan_manifest_value(value: Any, *, key: str | None = None) -> None:
    if key is not None:
        normalized_key = key.lower().replace("-", "_")
        if any(marker in normalized_key for marker in _SENSITIVE_KEY_PARTS):
            raise SeedValidationError("manifest contains a forbidden sensitive field")
    if isinstance(value, Mapping):
        for nested_key, nested_value in value.items():
            if not isinstance(nested_key, str):
                raise SeedValidationError("manifest mapping keys must be strings")
            _scan_manifest_value(nested_value, key=nested_key)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _scan_manifest_value(item)
    elif isinstance(value, str) and _contains_sensitive_bytes(value.encode("utf-8")):
        raise SeedValidationError("manifest contains forbidden sensitive content")


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Return bounded RFC 8785 canonical JSON bytes for a manifest-like mapping."""

    _scan_manifest_value(manifest)
    try:
        payload = rfc8785.dumps(dict(manifest))
    except (rfc8785.CanonicalizationError, TypeError, ValueError):
        raise SeedValidationError("manifest is outside the canonical JSON domain") from None
    if len(payload) > MAX_MANIFEST_BYTES:
        raise SeedValidationError("canonical manifest exceeds the byte limit")
    return payload


def _validate_relative_object_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise SeedValidationError("object path must be a non-empty string")
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value == "manifest.json"
        or any(
            part in {"", ".", ".."} or _SAFE_SEGMENT.fullmatch(part) is None
            for part in raw_parts
        )
    ):
        raise SeedValidationError("object path is unsafe or non-portable")
    return path


def _validate_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SeedValidationError(f"{field} must be a non-empty string")
    return value


def _validate_original_filename(value: Any, *, field: str) -> str:
    filename = _validate_string(value, field=field)
    if (
        filename in {".", ".."}
        or len(filename.encode("utf-8")) > MAX_FILENAME_BYTES
        or "\x00" in filename
        or "/" in filename
        or "\\" in filename
        or re.match(r"^[A-Za-z]:", filename) is not None
        or filename.lower().startswith("file:")
    ):
        raise SeedValidationError(f"{field} is unsafe or exceeds the byte limit")
    return filename


def _validate_closed_fields(value: Mapping[str, Any], allowed: set[str], *, field: str) -> None:
    if set(value) - allowed:
        raise SeedValidationError(f"{field} contains unsupported fields")


def _canonical_inventory(inventory: Any) -> list[dict[str, Any]]:
    if not isinstance(inventory, list):
        raise SeedValidationError("object_inventory must be a list")

    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_casefolded_paths: set[str] = set()
    for index, item in enumerate(inventory):
        if not isinstance(item, Mapping):
            raise SeedValidationError(f"object_inventory[{index}] must be an object")
        _validate_closed_fields(item, _OBJECT_FIELDS, field=f"object_inventory[{index}]")
        if not _REQUIRED_OBJECT_FIELDS <= set(item):
            raise SeedValidationError(f"object_inventory[{index}] is missing required fields")

        path = _validate_relative_object_path(item["path"]).as_posix()
        folded_path = path.casefold()
        if path in seen_paths or folded_path in seen_casefolded_paths:
            raise SeedValidationError("object inventory contains a path collision")
        seen_paths.add(path)
        seen_casefolded_paths.add(folded_path)

        filename = _validate_original_filename(
            item["filename"], field=f"object_inventory[{index}].filename"
        )
        media_type = _validate_string(
            item["media_type"], field=f"object_inventory[{index}].media_type"
        )
        if _MEDIA_TYPE.fullmatch(media_type) is None:
            raise SeedValidationError(f"object_inventory[{index}].media_type is invalid")
        size = _validate_size(
            item["size"], field=f"object_inventory[{index}].size", maximum=MAX_OBJECT_BYTES
        )
        sha256 = _validate_sha256(
            item["sha256"], field=f"object_inventory[{index}].sha256"
        )

        normalized_item = dict(item)
        normalized_item.update(
            {
                "path": path,
                "filename": filename,
                "media_type": media_type,
                "size": size,
                "sha256": sha256,
            }
        )
        for optional_identity in ("source_artifact_id", "source_artifact_version_id"):
            if optional_identity in normalized_item:
                _validate_string(normalized_item[optional_identity], field=optional_identity)
        if "reverse_references" in normalized_item:
            references = normalized_item["reverse_references"]
            if not isinstance(references, list) or any(
                not isinstance(reference, str) or not reference for reference in references
            ):
                raise SeedValidationError("reverse_references must be a list of non-empty strings")
        normalized.append(normalized_item)

    return sorted(normalized, key=lambda item: item["path"])


def _validate_verification_proof(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _VERIFICATION_PROOF_FIELDS:
        raise SeedValidationError("verified Seed requires a closed verification_proof")
    result = dict(value)
    _validate_sha256(result["oracle_result_sha256"], field="oracle_result_sha256")
    _validate_sha256(result["parent_regression_sha256"], field="parent_regression_sha256")
    runs = result["goal_only_run_sha256s"]
    if not isinstance(runs, list) or len(runs) != 2 or len(set(runs)) != 2:
        raise SeedValidationError("verification_proof requires two distinct goal-only run digests")
    for digest in runs:
        _validate_sha256(digest, field="goal_only_run_sha256s")
    return result


def _validate_promotion_proof(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PROMOTION_PROOF_FIELDS:
        raise SeedValidationError("promoted Seed requires a closed promotion_proof")
    result = dict(value)
    _validate_sha256(result["approval_sha256"], field="approval_sha256")
    return result


def _validate_stage_evidence(value: Any, *, parent_seed_sha256: Any) -> None:
    if not isinstance(value, Mapping):
        raise SeedValidationError("stage_evidence must be an object")
    _validate_closed_fields(value, _STAGE_EVIDENCE_FIELDS, field="stage_evidence")
    if set(value) != _STAGE_EVIDENCE_FIELDS:
        raise SeedValidationError("stage_evidence is missing required fields")
    if value["visibility"] != "server_only":
        raise SeedValidationError("stage_evidence visibility must be server_only")
    if value["stage_key"] not in _STAGE_KEYS:
        raise SeedValidationError("stage_evidence stage_key is unsupported")
    for identity_field in (
        "current_ifc_model_id",
        "current_ifc_revision_id",
        "current_ifc_artifact_id",
        "current_ifc_artifact_version_id",
        "model",
        "oracle_version",
    ):
        _validate_string(value[identity_field], field=f"stage_evidence.{identity_field}")
    revision_no = value["current_ifc_revision_no"]
    if isinstance(revision_no, bool) or not isinstance(revision_no, int) or revision_no < 1:
        raise SeedValidationError("stage_evidence revision number must be positive")
    for digest_field in (
        "current_ifc_sha256",
        "provider_schema_sha256",
        "tool_descriptions_sha256",
        "system_prompt_sha256",
        "stage_goal_sha256",
        "enabled_tools_sha256",
        "budget_sha256",
        "source_diff_sha256",
    ):
        _validate_sha256(value[digest_field], field=f"stage_evidence.{digest_field}")
    if value["reasoning_effort"] not in _REASONING_EFFORTS:
        raise SeedValidationError("stage_evidence reasoning_effort is unsupported")
    source_tree_clean = value["source_tree_clean"]
    if not isinstance(source_tree_clean, bool):
        raise SeedValidationError("stage_evidence source_tree_clean must be boolean")
    if source_tree_clean != (value["source_diff_sha256"] == _EMPTY_DIFF_SHA256):
        raise SeedValidationError("stage_evidence source tree and diff digest disagree")

    runs = value["agent_runs"]
    if not isinstance(runs, list) or not runs:
        raise SeedValidationError("stage_evidence agent_runs must be a non-empty list")
    run_keys: set[str] = set()
    event_digests: set[str] = set()
    for index, run in enumerate(runs):
        if not isinstance(run, Mapping):
            raise SeedValidationError(f"stage_evidence.agent_runs[{index}] must be an object")
        _validate_closed_fields(
            run, _STAGE_AGENT_RUN_FIELDS, field=f"stage_evidence.agent_runs[{index}]"
        )
        if set(run) != _STAGE_AGENT_RUN_FIELDS:
            raise SeedValidationError("stage_evidence agent run is missing required fields")
        run_key = _validate_string(run["run_key"], field="stage_evidence.agent_runs.run_key")
        if run_key in run_keys:
            raise SeedValidationError("stage_evidence agent run keys must be distinct")
        run_keys.add(run_key)
        if run["mode"] not in _STAGE_RUN_MODES:
            raise SeedValidationError("stage_evidence agent run mode is unsupported")
        parent_digest = _validate_sha256(
            run["parent_seed_sha256"], field="stage_evidence.agent_runs.parent_seed_sha256"
        )
        if parent_digest != parent_seed_sha256:
            raise SeedValidationError("stage_evidence agent run parent does not match the Seed")
        event_digest = _validate_sha256(
            run["event_manifest_sha256"],
            field="stage_evidence.agent_runs.event_manifest_sha256",
        )
        if event_digest in event_digests:
            raise SeedValidationError("stage_evidence agent event manifests must be distinct")
        event_digests.add(event_digest)


def _validate_state_contract(manifest: Mapping[str, Any]) -> str:
    state = manifest.get("state")
    if state not in _SEED_STATES:
        raise SeedValidationError("state must be one of candidate, verified, or promoted")
    if state == "candidate":
        if any(
            field in manifest
            for field in ("previous_record_sha256", "verification_proof", "promotion_proof")
        ):
            raise SeedValidationError("candidate Seed contains premature proof fields")
    else:
        _validate_sha256(manifest.get("previous_record_sha256"), field="previous_record_sha256")
        _validate_verification_proof(manifest.get("verification_proof"))
        if state == "verified" and "promotion_proof" in manifest:
            raise SeedValidationError("verified Seed contains a premature promotion_proof")
        if state == "promoted":
            _validate_promotion_proof(manifest.get("promotion_proof"))
    return state


def _validate_manifest_schema(manifest: Mapping[str, Any]) -> None:
    _validate_closed_fields(manifest, _TOP_LEVEL_FIELDS, field="manifest")
    if not _REQUIRED_FIELDS <= set(manifest):
        raise SeedValidationError("manifest is missing required fields")
    if manifest["format_version"] != _FORMAT_VERSION:
        raise SeedValidationError("unsupported format_version")
    if manifest["seed_kind"] not in _SEED_KINDS:
        raise SeedValidationError("unsupported seed_kind")
    _validate_state_contract(manifest)
    for digest_field in (
        "database_rows_digest",
        "parent_seed_sha256",
        "m4_run_seed_sha256",
        "source_catalog_frontier_digest",
        "source_operation_frontier_digest",
    ):
        if digest_field in manifest:
            _validate_sha256(manifest[digest_field], field=digest_field)
    for frontier_field in ("source_catalog_frontier", "source_operation_frontier"):
        if frontier_field in manifest:
            value = manifest[frontier_field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SeedValidationError(f"{frontier_field} must be a non-negative integer")
    for source_field in (
        "source_commit",
        "source_migration_head",
        "source_m4_round",
        "source_arm",
        "source_direct_session_alias",
        "source_task_id",
        "source_run_id",
        "created_at",
    ):
        if source_field in manifest:
            _validate_string(manifest[source_field], field=source_field)
    if manifest["seed_kind"] == "ifc_stage":
        _validate_string(manifest.get("source_commit"), field="source_commit")
        _validate_sha256(manifest.get("parent_seed_sha256"), field="parent_seed_sha256")
        _validate_sha256(manifest.get("m4_run_seed_sha256"), field="m4_run_seed_sha256")
        if "stage_evidence" in manifest:
            _validate_stage_evidence(
                manifest["stage_evidence"],
                parent_seed_sha256=manifest["parent_seed_sha256"],
            )
    elif "stage_evidence" in manifest:
        raise SeedValidationError("M4 Run Seed must not contain stage_evidence")


def compute_inventory_digest(inventory: Iterable[Mapping[str, Any]]) -> str:
    """Digest the path-sorted canonical object inventory."""

    normalized = _canonical_inventory(list(inventory))
    return hashlib.sha256(canonical_manifest_bytes({"objects": normalized})).hexdigest()


def compute_root_sha256(manifest: Mapping[str, Any]) -> str:
    """Digest a canonical manifest without its self-referential root field."""

    root_payload = deepcopy(dict(manifest))
    root_payload.pop("root_sha256", None)
    if "object_inventory" in root_payload:
        root_payload["object_inventory"] = _canonical_inventory(root_payload["object_inventory"])
    return hashlib.sha256(canonical_manifest_bytes(root_payload)).hexdigest()


def seal_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated canonical-order record with inventory and root digests."""

    sealed = deepcopy(dict(manifest))
    _validate_manifest_schema(sealed)
    inventory = _canonical_inventory(sealed["object_inventory"])
    sealed["object_inventory"] = inventory
    sealed["artifact_inventory_digest"] = compute_inventory_digest(inventory)
    sealed["root_sha256"] = compute_root_sha256(sealed)
    return sealed


def _verify_manifest_digests(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    _validate_manifest_schema(manifest)
    inventory = _canonical_inventory(manifest["object_inventory"])
    expected_inventory_digest = _validate_sha256(
        manifest.get("artifact_inventory_digest"), field="artifact_inventory_digest"
    )
    if compute_inventory_digest(inventory) != expected_inventory_digest:
        raise SeedValidationError("artifact inventory digest mismatch")
    expected_root = _validate_sha256(manifest.get("root_sha256"), field="root_sha256")
    if compute_root_sha256(manifest) != expected_root:
        raise SeedValidationError("root SHA-256 mismatch")
    return inventory


def _root_directory(seed_root: Path) -> Path:
    try:
        root_stat = seed_root.lstat()
    except OSError:
        raise SeedValidationError("Seed root is missing or unreadable") from None
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise SeedValidationError("Seed root must be a real directory")
    return seed_root.absolute()


def _walk_seed_tree(root: Path) -> dict[str, os.stat_result]:
    files: dict[str, os.stat_result] = {}
    folded_paths: set[str] = set()

    def visit(directory: Path, prefix: PurePosixPath | None = None) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            raise SeedValidationError("Seed tree is unreadable") from None
        for entry in entries:
            relative = PurePosixPath(entry.name) if prefix is None else prefix / entry.name
            if _SAFE_SEGMENT.fullmatch(entry.name) is None:
                raise SeedValidationError("Seed tree contains an unsafe or non-portable name")
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                raise SeedValidationError("Seed tree entry is unreadable") from None
            if entry.is_symlink():
                raise SeedValidationError("Seed tree must not contain symbolic links")
            folded = relative.as_posix().casefold()
            if folded in folded_paths:
                raise SeedValidationError("Seed tree contains a case-folding path collision")
            folded_paths.add(folded)
            if stat.S_ISDIR(entry_stat.st_mode):
                visit(Path(entry.path), relative)
            elif stat.S_ISREG(entry_stat.st_mode):
                if entry_stat.st_nlink != 1:
                    raise SeedValidationError("Seed objects must not be hard-linked")
                files[relative.as_posix()] = entry_stat
            else:
                raise SeedValidationError("Seed tree contains a non-regular entry")

    visit(root)
    return files


def _is_text_object(item: Mapping[str, Any]) -> bool:
    media_type = item["media_type"]
    return (
        media_type.startswith("text/")
        or media_type in {"application/json", "application/sql", "application/xml"}
        or PurePosixPath(item["path"]).suffix.lower() in _TEXT_SUFFIXES
    )


def _hash_stream(stream: BinaryIO, *, scan_sensitive: bool, maximum: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    overlap = b""
    while True:
        chunk = stream.read(CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise SeedValidationError("object exceeds the byte limit")
        digest.update(chunk)
        if scan_sensitive:
            window = overlap + chunk
            if _contains_sensitive_bytes(window):
                raise SeedValidationError("text object contains forbidden sensitive content")
            overlap = window[-4096:]
    return total, digest.hexdigest()


def verify_seed(
    manifest: Mapping[str, Any],
    seed_root: Path,
    *,
    max_object_bytes: int = MAX_OBJECT_BYTES,
) -> None:
    """Verify a closed manifest and the complete immutable filesystem tree."""

    if not 0 < max_object_bytes <= MAX_OBJECT_BYTES:
        raise SeedValidationError("max_object_bytes is outside the permitted range")
    inventory = _verify_manifest_digests(manifest)
    root = _root_directory(seed_root)
    tree_files = _walk_seed_tree(root)
    expected_paths = {item["path"] for item in inventory}
    actual_object_paths = set(tree_files) - {"manifest.json"}
    if actual_object_paths != expected_paths:
        raise SeedValidationError("Seed tree contains missing or unlisted files")

    manifest_file = root / "manifest.json"
    if "manifest.json" in tree_files:
        persisted = _read_bounded(manifest_file, maximum=MAX_MANIFEST_BYTES)
        canonical = canonical_manifest_bytes(manifest)
        if persisted not in {canonical, canonical + b"\n"}:
            raise SeedValidationError("persisted manifest is not the verified canonical record")

    for item in inventory:
        item_limit = min(max_object_bytes, MAX_OBJECT_BYTES)
        file_stat = tree_files[item["path"]]
        if file_stat.st_size > item_limit or item["size"] > item_limit:
            raise SeedValidationError("object exceeds the byte limit")
        object_path = root.joinpath(*PurePosixPath(item["path"]).parts)
        descriptor = -1
        try:
            descriptor = os.open(object_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_nlink != 1
                or (opened_stat.st_dev, opened_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino)
            ):
                raise SeedValidationError("Seed object changed during verification")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                size, sha256 = _hash_stream(
                    stream, scan_sensitive=_is_text_object(item), maximum=item_limit
                )
        except SeedValidationError:
            raise
        except OSError:
            raise SeedValidationError("Seed object is unreadable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if size != item["size"]:
            raise SeedValidationError("object size mismatch")
        if sha256 != item["sha256"]:
            raise SeedValidationError("object SHA-256 mismatch")


def transition_seed_state(
    manifest: Mapping[str, Any],
    target_state: str,
    *,
    proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the next state record, linked to an already sealed immutable record."""

    _verify_manifest_digests(manifest)
    current_state = manifest["state"]
    if target_state not in _SEED_STATES:
        raise SeedValidationError("target state is unsupported")
    current_index = _SEED_STATES.index(current_state)
    if current_index + 1 >= len(_SEED_STATES) or _SEED_STATES[current_index + 1] != target_state:
        raise SeedValidationError("invalid Seed state transition")

    advanced = deepcopy(dict(manifest))
    advanced.pop("root_sha256", None)
    advanced["state"] = target_state
    advanced["previous_record_sha256"] = manifest["root_sha256"]
    if target_state == "verified":
        advanced["verification_proof"] = _validate_verification_proof(proof)
    else:
        advanced["promotion_proof"] = _validate_promotion_proof(proof)
    return seal_manifest(advanced)


def write_manifest_exclusive(path: Path, manifest: Mapping[str, Any]) -> None:
    """Atomically create a canonical manifest without replacing an existing path."""

    _verify_manifest_digests(manifest)
    payload = canonical_manifest_bytes(manifest) + b"\n"
    parent = path.parent
    try:
        parent_stat = parent.lstat()
    except OSError:
        raise SeedValidationError("manifest parent directory is unavailable") from None
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise SeedValidationError("manifest parent must be a real directory")

    descriptor = -1
    staging_name: str | None = None
    try:
        descriptor, staging_name = tempfile.mkstemp(prefix=".seed-manifest-", dir=parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staging_name, path, follow_symlinks=False)
        except FileExistsError:
            raise SeedValidationError("refusing to overwrite an existing manifest") from None
        os.unlink(staging_name)
        staging_name = None
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except SeedValidationError:
        raise
    except OSError:
        raise SeedValidationError("manifest publication failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if staging_name is not None:
            try:
                os.unlink(staging_name)
            except FileNotFoundError:
                pass


def _read_bounded(path: Path, *, maximum: int) -> bytes:
    descriptor = -1
    try:
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise SeedValidationError("manifest path must be a regular file")
        if file_stat.st_nlink != 1:
            raise SeedValidationError("manifest path must not be hard-linked")
        if file_stat.st_size > maximum:
            raise SeedValidationError("manifest exceeds the byte limit")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_nlink != 1
            or (opened_stat.st_dev, opened_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino)
        ):
            raise SeedValidationError("manifest changed during verification")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            payload = stream.read(maximum + 1)
    except SeedValidationError:
        raise
    except OSError:
        raise SeedValidationError("manifest is missing or unreadable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > maximum:
        raise SeedValidationError("manifest exceeds the byte limit")
    return payload


def load_manifest(path: Path, *, max_bytes: int = MAX_MANIFEST_BYTES) -> dict[str, Any]:
    """Load a bounded regular JSON manifest without following links."""

    if not 0 < max_bytes <= MAX_MANIFEST_BYTES:
        raise SeedValidationError("max_bytes is outside the permitted range")
    payload = _read_bounded(path, maximum=max_bytes)
    if _contains_sensitive_bytes(payload):
        raise SeedValidationError("manifest contains forbidden sensitive content")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SeedValidationError("manifest is not valid UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise SeedValidationError("Seed manifest root must be a JSON object")
    _verify_manifest_digests(value)
    return value
