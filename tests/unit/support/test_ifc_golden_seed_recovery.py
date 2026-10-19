from __future__ import annotations

import hashlib
from pathlib import Path
import struct

import pytest

from tests.process_support.ifc_golden_seed import recovery


def test_manifest_is_relative_deterministic_and_rejects_links(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "base").mkdir()
    (source / "PG_VERSION").write_bytes(b"16\n")
    (source / "base" / "1").write_bytes(b"heap")

    version = b"16\n"
    heap = b"heap"
    lines = "".join(
        [
            f"{hashlib.sha256(version).hexdigest()}  ./PG_VERSION\n",
            f"{hashlib.sha256(heap).hexdigest()}  ./base/1\n",
        ]
    )
    manifest = recovery.compute_file_manifest(source)

    assert manifest == recovery.FileManifest(
        file_count=2,
        total_bytes=7,
        sha256=hashlib.sha256(lines.encode()).hexdigest(),
    )

    linked = tmp_path / "linked"
    linked.symlink_to(source, target_is_directory=True)
    with pytest.raises(recovery.M4RecoveryError) as captured:
        recovery.compute_file_manifest(linked)
    assert captured.value.rule_code == "I0_M4_SOURCE_INVALID"


def test_source_validation_requires_exact_manifest_and_never_accepts_original(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one").write_bytes(b"attested")
    manifest = recovery.compute_file_manifest(source)
    monkeypatch.setattr(recovery, "SOURCE_FILE_COUNT", manifest.file_count)
    monkeypatch.setattr(recovery, "SOURCE_FILE_BYTES", manifest.total_bytes)
    monkeypatch.setattr(recovery, "SOURCE_MANIFEST_SHA256", manifest.sha256)
    original = tmp_path / "original"
    original.mkdir()

    assert recovery.validate_source_copy(source, original) == source.resolve()

    (source / "one").write_bytes(b"changed")
    with pytest.raises(recovery.M4RecoveryError) as changed:
        recovery.validate_source_copy(source, original)
    assert changed.value.rule_code == "I0_M4_MANIFEST_MISMATCH"

    with pytest.raises(recovery.M4RecoveryError) as forbidden:
        recovery.validate_source_copy(original, original)
    assert forbidden.value.rule_code == "I0_M4_ORIGINAL_FORBIDDEN"


def test_work_root_must_be_new_external_and_separate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(recovery, "REPOSITORY_ROOT", tmp_path / "repository")

    target = tmp_path / "work"
    assert recovery.validate_new_work_root(source.resolve(), target) == target

    target.mkdir()
    with pytest.raises(recovery.M4RecoveryError) as existing:
        recovery.validate_new_work_root(source.resolve(), target)
    assert existing.value.rule_code == "I0_M4_WORK_ROOT_EXISTS"

    with pytest.raises(recovery.M4RecoveryError) as nested:
        recovery.validate_new_work_root(source.resolve(), source / "nested")
    assert nested.value.rule_code == "I0_M4_WORK_ROOT_INVALID"


def test_attested_wal_headers_and_relational_root_are_exact(tmp_path: Path) -> None:
    header = bytearray(32)
    struct.pack_into("<H", header, 0, recovery.WAL_MAGIC)
    struct.pack_into("<Q", header, 8, recovery.WAL_SEGMENT_7_PAGE_ADDRESS)
    struct.pack_into("<Q", header, 24, int(recovery.SYSTEM_IDENTIFIER))
    path = tmp_path / "wal"
    path.write_bytes(header)

    assert recovery._wal_header(path) == (
        recovery.WAL_MAGIC,
        recovery.WAL_SEGMENT_7_PAGE_ADDRESS,
        int(recovery.SYSTEM_IDENTIFIER),
    )
    assert (
        recovery.relation_root(dict(recovery.EXPECTED_RELATION_DIGESTS))
        == recovery.RELATIONAL_ROOT_SHA256
    )

    changed = dict(recovery.EXPECTED_RELATION_DIGESTS)
    changed["runs"] = recovery.RelationDigest(2, changed["runs"].sha256)
    assert recovery.relation_root(changed) != recovery.RELATIONAL_ROOT_SHA256


def test_subprocess_rejection_never_echoes_command_or_path() -> None:
    secret = "do-not-echo-this-path"
    with pytest.raises(recovery.M4RecoveryError) as captured:
        recovery._run([f"/missing/{secret}"], "I0_M4_TEST_FAILED")

    assert captured.value.rule_code == "I0_M4_TEST_FAILED"
    assert secret not in str(captured.value)


def test_subprocess_calls_are_bounded_and_timeout_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, int] = {}

    def complete(_command: list[str], **options: object) -> object:
        captured["timeout"] = int(options["timeout"])
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(recovery.subprocess, "run", complete)
    recovery._run(["postgres", "--version"], "I0_M4_TEST_FAILED")
    assert captured["timeout"] == recovery.COMMAND_TIMEOUT_SECONDS

    def timeout(command: list[str], **_options: object) -> object:
        raise recovery.subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(recovery.subprocess, "run", timeout)
    with pytest.raises(recovery.M4RecoveryError) as rejected:
        recovery._run(["private-value"], "I0_M4_TEST_FAILED")
    assert rejected.value.rule_code == "I0_M4_TEST_FAILED"
    assert "private-value" not in str(rejected.value)


@pytest.mark.parametrize("count", [0, 2])
def test_source_binding_must_be_unique(count: int) -> None:
    class Result:
        def fetchone(self) -> tuple[int]:
            return (count,)

    class Connection:
        def execute(self, _statement: str) -> Result:
            return Result()

    with pytest.raises(recovery.M4RecoveryError) as rejected:
        recovery._create_imported_scope(Connection())  # type: ignore[arg-type]
    assert rejected.value.rule_code == "I0_M4_BINDING_MISMATCH"


def test_migration_escapes_config_interpolation_and_targets_exact_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, str] = {}

    class Configuration:
        def __init__(self, path: str) -> None:
            captured["config"] = path

        def set_main_option(self, name: str, value: str) -> None:
            captured[name] = value

    def upgrade(_configuration: Configuration, revision: str) -> None:
        captured["revision"] = revision

    monkeypatch.setattr(recovery, "Config", Configuration)
    monkeypatch.setattr(recovery.command, "upgrade", upgrade)

    recovery._migrate(tmp_path / "socket", recovery.SOURCE_MIGRATION)

    assert captured["revision"] == recovery.SOURCE_MIGRATION
    assert "%%2F" in captured["sqlalchemy.url"]
