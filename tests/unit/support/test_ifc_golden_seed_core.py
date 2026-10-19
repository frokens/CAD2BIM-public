from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from tests.process_support.ifc_golden_seed import (
    MAX_MANIFEST_BYTES,
    SeedValidationError,
    canonical_manifest_bytes,
    load_manifest,
    seal_manifest,
    transition_seed_state,
    verify_seed,
    write_manifest_exclusive,
)

_VERIFICATION_PROOF = {
    "oracle_result_sha256": "1" * 64,
    "parent_regression_sha256": "2" * 64,
    "goal_only_run_sha256s": ["3" * 64, "4" * 64],
}
_PROMOTION_PROOF = {"approval_sha256": "5" * 64}


def _seed(
    tmp_path: Path,
    content: bytes = b"synthetic parquet bytes",
    *,
    filename: str = "payload.parquet",
    media_type: str = "application/vnd.apache.parquet",
) -> dict[str, object]:
    object_path = tmp_path / "objects" / filename
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(content)
    return seal_manifest(
        {
            "format_version": "ifc-golden-seed/v1",
            "seed_kind": "m4_run",
            "state": "candidate",
            "object_inventory": [
                {
                    "path": f"objects/{filename}",
                    "filename": filename,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "media_type": media_type,
                }
            ],
        }
    )


def test_manifest_is_canonical_and_inventory_order_does_not_change_root() -> None:
    empty_sha = hashlib.sha256(b"").hexdigest()
    first = {
        "format_version": "ifc-golden-seed/v1",
        "seed_kind": "m4_run",
        "state": "candidate",
        "object_inventory": [
            {
                "path": "objects/z.bin",
                "filename": "z.bin",
                "media_type": "application/octet-stream",
                "size": 0,
                "sha256": empty_sha,
            },
            {
                "path": "objects/a.bin",
                "filename": "a.bin",
                "media_type": "application/octet-stream",
                "size": 0,
                "sha256": empty_sha,
            },
        ],
    }
    second = {
        "object_inventory": list(reversed(first["object_inventory"])),
        "state": "candidate",
        "seed_kind": "m4_run",
        "format_version": "ifc-golden-seed/v1",
    }

    sealed_first = seal_manifest(first)
    sealed_second = seal_manifest(second)

    assert sealed_first["root_sha256"] == sealed_second["root_sha256"]
    assert canonical_manifest_bytes({"z": 1, "a": 2}) == b'{"a":2,"z":1}'
    assert [item["path"] for item in sealed_first["object_inventory"]] == [
        "objects/a.bin",
        "objects/z.bin",
    ]


def test_verify_seed_accepts_small_synthetic_fixture(tmp_path: Path) -> None:
    manifest = _seed(tmp_path)

    verify_seed(manifest, tmp_path)


@pytest.mark.parametrize(
    "unsafe_path",
    ["/private/object", "../object", "objects/../object", "objects\\object", "objects//object"],
)
def test_seal_rejects_unsafe_object_paths(unsafe_path: str) -> None:
    with pytest.raises(SeedValidationError, match="path"):
        seal_manifest(
            {
                "format_version": "ifc-golden-seed/v1",
                "seed_kind": "m4_run",
                "state": "candidate",
                "object_inventory": [
                    {
                        "path": unsafe_path,
                        "filename": "object",
                        "media_type": "application/octet-stream",
                        "size": 0,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                    }
                ],
            }
        )


def test_verify_seed_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_bytes(b"outside")
    objects = tmp_path / "objects"
    objects.mkdir()
    (objects / "payload").symlink_to(outside)
    manifest = seal_manifest(
        {
            "format_version": "ifc-golden-seed/v1",
            "seed_kind": "m4_run",
            "state": "candidate",
            "object_inventory": [
                {
                    "path": "objects/payload",
                    "filename": "payload",
                    "media_type": "application/octet-stream",
                    "size": len(b"outside"),
                    "sha256": hashlib.sha256(b"outside").hexdigest(),
                }
            ],
        }
    )

    with pytest.raises(SeedValidationError, match="symbolic"):
        verify_seed(manifest, tmp_path)


def test_verify_seed_rejects_size_sha_and_root_drift(tmp_path: Path) -> None:
    manifest = _seed(tmp_path)
    object_path = tmp_path / "objects" / "payload.parquet"

    object_path.write_bytes(b"different length")
    with pytest.raises(SeedValidationError, match="size mismatch"):
        verify_seed(manifest, tmp_path)

    object_path.write_bytes(b"synthetic parquet bytez")
    with pytest.raises(SeedValidationError, match="SHA-256 mismatch"):
        verify_seed(manifest, tmp_path)

    object_path.write_bytes(b"synthetic parquet bytes")
    drifted = {**manifest, "source_arm": "changed"}
    with pytest.raises(SeedValidationError, match="root SHA-256 mismatch"):
        verify_seed(drifted, tmp_path)


def test_state_transition_requires_candidate_then_verified_then_promoted(tmp_path: Path) -> None:
    candidate = _seed(tmp_path)

    verified = transition_seed_state(candidate, "verified", proof=_VERIFICATION_PROOF)
    promoted = transition_seed_state(verified, "promoted", proof=_PROMOTION_PROOF)

    assert verified["state"] == "verified"
    assert promoted["state"] == "promoted"
    assert candidate["state"] == "candidate"
    assert verified["previous_record_sha256"] == candidate["root_sha256"]
    assert promoted["previous_record_sha256"] == verified["root_sha256"]
    assert len({candidate["root_sha256"], verified["root_sha256"], promoted["root_sha256"]}) == 3
    with pytest.raises(SeedValidationError, match="invalid Seed state transition"):
        transition_seed_state(candidate, "promoted", proof=_PROMOTION_PROOF)
    with pytest.raises(SeedValidationError, match="invalid Seed state transition"):
        transition_seed_state(promoted, "promoted", proof=_PROMOTION_PROOF)

    with pytest.raises(SeedValidationError, match="root SHA-256 mismatch"):
        transition_seed_state(
            {**candidate, "source_arm": "tampered"}, "verified", proof=_VERIFICATION_PROOF
        )


def test_manifest_publication_refuses_overwrite(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest = _seed(tmp_path)
    write_manifest_exclusive(manifest_path, manifest)
    first_bytes = manifest_path.read_bytes()
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600

    with pytest.raises(SeedValidationError, match="refusing to overwrite"):
        write_manifest_exclusive(manifest_path, manifest)

    assert manifest_path.read_bytes() == first_bytes
    verify_seed(manifest, tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("format_version", "ifc-golden-seed/v2", "format_version"),
        ("seed_kind", "unknown", "seed_kind"),
        ("state", "approved", "state"),
        ("unsupported", "value", "unsupported fields"),
    ],
)
def test_manifest_schema_is_closed(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    manifest = _seed(tmp_path)

    with pytest.raises(SeedValidationError, match=message):
        seal_manifest({**manifest, field: value})


def test_inventory_schema_is_closed_and_requires_descriptors(tmp_path: Path) -> None:
    manifest = _seed(tmp_path)
    item = dict(manifest["object_inventory"][0])

    with pytest.raises(SeedValidationError, match="unsupported fields"):
        seal_manifest({**manifest, "object_inventory": [{**item, "absolute_path": "/tmp/x"}]})

    item.pop("filename")
    with pytest.raises(SeedValidationError, match="missing required fields"):
        seal_manifest({**manifest, "object_inventory": [item]})


def test_original_unicode_filename_is_metadata_not_the_fixture_object_path() -> None:
    manifest = seal_manifest(
        {
            "format_version": "ifc-golden-seed/v1",
            "seed_kind": "m4_run",
            "state": "candidate",
            "object_inventory": [
                {
                    "path": "objects/0123456789abcdef.dxf",
                    "filename": "燃气管网原始图.dxf",
                    "media_type": "image/vnd.dxf",
                    "size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            ],
        }
    )

    assert manifest["object_inventory"][0]["filename"] == "燃气管网原始图.dxf"
    assert manifest["object_inventory"][0]["path"] == "objects/0123456789abcdef.dxf"


@pytest.mark.parametrize(
    "filename",
    ["../secret.dxf", r"C:\secret.dxf", r"\\server\share.dxf", "file://secret.dxf", "bad\x00.dxf"],
)
def test_original_filename_rejects_path_and_nul_forms(filename: str) -> None:
    with pytest.raises(SeedValidationError, match="filename"):
        seal_manifest(
            {
                "format_version": "ifc-golden-seed/v1",
                "seed_kind": "m4_run",
                "state": "candidate",
                "object_inventory": [
                    {
                        "path": "objects/digest.dxf",
                        "filename": filename,
                        "media_type": "image/vnd.dxf",
                        "size": 0,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "unsafe_path",
    ["objects/管道.parquet", "Objects/A.parquet"],
)
def test_inventory_rejects_unicode_and_casefold_collisions(
    tmp_path: Path, unsafe_path: str
) -> None:
    manifest = _seed(tmp_path)
    original = dict(manifest["object_inventory"][0])
    if unsafe_path.startswith("Objects"):
        collision = {
            **original,
            "path": "Objects/PAYLOAD.parquet",
            "filename": "PAYLOAD.parquet",
        }
        inventory = [original, collision]
        match = "collision"
    else:
        unsafe = {**original, "path": unsafe_path, "filename": "管道.parquet"}
        inventory = [unsafe]
        match = "unsafe"

    with pytest.raises(SeedValidationError, match=match):
        seal_manifest({**manifest, "object_inventory": inventory})


def test_verify_rejects_root_and_tree_links_hardlinks_and_special_files(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    manifest = _seed(real_root)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(SeedValidationError, match="real directory"):
        verify_seed(manifest, linked_root)

    linked_directory = real_root / "linked-directory"
    linked_directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SeedValidationError, match="symbolic links"):
        verify_seed(manifest, real_root)
    linked_directory.unlink()

    hardlink = real_root / "objects" / "hardlink.parquet"
    os.link(real_root / "objects" / "payload.parquet", hardlink)
    with pytest.raises(SeedValidationError, match="hard-linked"):
        verify_seed(manifest, real_root)
    hardlink.unlink()

    fifo = real_root / "objects" / "unexpected.fifo"
    os.mkfifo(fifo)
    with pytest.raises(SeedValidationError, match="non-regular"):
        verify_seed(manifest, real_root)


def test_verify_rejects_unlisted_and_nonportable_tree_entries(tmp_path: Path) -> None:
    manifest = _seed(tmp_path)
    (tmp_path / "extra.bin").write_bytes(b"extra")
    with pytest.raises(SeedValidationError, match="unlisted"):
        verify_seed(manifest, tmp_path)
    (tmp_path / "extra.bin").unlink()

    (tmp_path / "objects" / "管道.txt").write_bytes(b"extra")
    with pytest.raises(SeedValidationError, match="non-portable"):
        verify_seed(manifest, tmp_path)


def test_object_read_is_bounded_and_text_is_scanned(tmp_path: Path) -> None:
    content = b"postgresql://fixture_user:fixture_password@db.invalid/fixture"
    manifest = _seed(tmp_path, content, filename="source.txt", media_type="text/plain")

    with pytest.raises(SeedValidationError, match="sensitive content"):
        verify_seed(manifest, tmp_path)
    with pytest.raises(SeedValidationError, match="byte limit"):
        verify_seed(manifest, tmp_path, max_object_bytes=len(content) - 1)


def test_text_scan_distinguishes_identifier_suffix_from_sensitive_assignment(
    tmp_path: Path,
) -> None:
    source = (
        b"diameter_token = 'unknown'\n"
        b"angle_token = 'unknown'\n"
        b"model_token = 'unknown'\n"
    )
    manifest = _seed(tmp_path, source, filename="source.py", media_type="text/x-python")
    verify_seed(manifest, tmp_path)


@pytest.mark.parametrize(
    "sensitive_assignment",
    (
        "token = synthetic-value",
        "access_token = synthetic-value",
        "client_secret = synthetic-value",
        "db_password = synthetic-value",
        "api-key = synthetic-value",
        "apikey = synthetic-value",
        "APIKey = synthetic-value",
        "alias = access_token = synthetic-value",
        "example=token=synthetic-value",
    ),
)
def test_text_scan_still_rejects_credential_assignments(
    tmp_path: Path, sensitive_assignment: str
) -> None:
    sensitive = f"{sensitive_assignment}\n".encode()
    sensitive_manifest = _seed(
        tmp_path,
        sensitive,
        filename="source.py",
        media_type="text/x-python",
    )
    with pytest.raises(SeedValidationError, match="sensitive content"):
        verify_seed(sensitive_manifest, tmp_path)


def test_binary_object_does_not_claim_parquet_deep_content_scanning(tmp_path: Path) -> None:
    content = b"password=synthetic-is-not-a-real-parquet-secret"
    manifest = _seed(tmp_path, content)

    verify_seed(manifest, tmp_path)


@pytest.mark.parametrize(
    "forbidden",
    [
        "/Users/example/private/file",
        "/mnt/private-fixture",
        r"C:\Users\example\private\file",
        r"\\server\share\private",
        "file:///private/tmp/fixture",
        "postgresql://fixture_user@db.invalid/database",
        "postgresql://fixture_user:fixture_password@db.invalid/database",
        "${PRIVATE_FIXTURE_PATH}",
    ],
)
def test_nested_manifest_sensitive_values_are_rejected_without_echo(
    tmp_path: Path, forbidden: str
) -> None:
    manifest = _seed(tmp_path)
    item = {**manifest["object_inventory"][0], "reverse_references": [forbidden]}

    with pytest.raises(SeedValidationError) as error:
        seal_manifest({**manifest, "object_inventory": [item]})

    assert forbidden not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_nested_sensitive_key_is_rejected_without_echo() -> None:
    with pytest.raises(SeedValidationError) as error:
        canonical_manifest_bytes({"nested": {"database_password": "not-a-real-password"}})

    assert "not-a-real-password" not in str(error.value)


def test_manifest_loading_is_bounded_and_verifies_the_record(tmp_path: Path) -> None:
    manifest = _seed(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    write_manifest_exclusive(manifest_path, manifest)

    assert load_manifest(manifest_path) == manifest
    with pytest.raises(SeedValidationError, match="byte limit"):
        load_manifest(manifest_path, max_bytes=16)

    oversized_path = tmp_path / "oversized.json"
    oversized_path.write_bytes(b"{" + b" " * MAX_MANIFEST_BYTES + b"}")
    with pytest.raises(SeedValidationError, match="byte limit"):
        load_manifest(oversized_path)

    sensitive_path = tmp_path / "sensitive.json"
    sensitive_path.write_bytes(b'{"dsn":"postgresql://fixture_user@db.invalid/database"}')
    with pytest.raises(SeedValidationError) as error:
        load_manifest(sensitive_path)
    assert "fixture_user" not in str(error.value)
    assert error.value.__cause__ is None


def test_transition_requires_closed_proofs_and_preserves_source_record(tmp_path: Path) -> None:
    candidate = _seed(tmp_path)

    with pytest.raises(SeedValidationError, match="verification_proof"):
        transition_seed_state(candidate, "verified")
    with pytest.raises(SeedValidationError, match="two distinct"):
        transition_seed_state(
            candidate,
            "verified",
            proof={**_VERIFICATION_PROOF, "goal_only_run_sha256s": ["3" * 64, "3" * 64]},
        )

    verified = transition_seed_state(candidate, "verified", proof=_VERIFICATION_PROOF)
    with pytest.raises(SeedValidationError, match="promotion_proof"):
        transition_seed_state(verified, "promoted")

    assert candidate["state"] == "candidate"
    assert "verification_proof" not in candidate


@pytest.mark.parametrize("reasoning_effort", ["max", "ultra"])
def test_stage_evidence_accepts_current_codex_reasoning_efforts(reasoning_effort: str) -> None:
    evidence = {
        "visibility": "server_only",
        "stage_key": "foundation",
        "current_ifc_model_id": "model-1",
        "current_ifc_revision_id": "revision-1",
        "current_ifc_revision_no": 1,
        "current_ifc_artifact_id": "artifact-1",
        "current_ifc_artifact_version_id": "version-1",
        "current_ifc_sha256": "1" * 64,
        "provider_schema_sha256": "2" * 64,
        "tool_descriptions_sha256": "3" * 64,
        "system_prompt_sha256": "4" * 64,
        "stage_goal_sha256": "5" * 64,
        "enabled_tools_sha256": "6" * 64,
        "model": "gpt-5.6-sol",
        "reasoning_effort": reasoning_effort,
        "budget_sha256": "7" * 64,
        "oracle_version": "foundation-oracle/v1",
        "source_tree_clean": True,
        "source_diff_sha256": hashlib.sha256(b"").hexdigest(),
        "agent_runs": [
            {
                "run_key": "goal-1",
                "mode": "goal_only_semiblind",
                "parent_seed_sha256": "a" * 64,
                "event_manifest_sha256": "8" * 64,
            }
        ],
    }

    manifest = seal_manifest(
        {
            "format_version": "ifc-golden-seed/v1",
            "seed_kind": "ifc_stage",
            "state": "candidate",
            "source_commit": "0123456789abcdef0123456789abcdef01234567",
            "parent_seed_sha256": "a" * 64,
            "m4_run_seed_sha256": "b" * 64,
            "object_inventory": [],
            "stage_evidence": evidence,
        }
    )

    assert manifest["stage_evidence"]["reasoning_effort"] == reasoning_effort
