"""Fail-closed offline recovery for the attested M4 Round 6 PostgreSQL slice.

This module is test/process support.  It never opens the damaged source cluster in
place.  The caller supplies an already-created exact copy and a nonexistent work
root; every PostgreSQL process runs below that work root on a private Unix socket.
"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
from typing import Final

from alembic import command
from alembic.config import Config
import psycopg
from psycopg import sql
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from tests.process_support.ifc_golden_seed.postgres_adapter import (
    PostgresSeedSnapshotReader,
)


SOURCE_FILE_COUNT: Final = 130
SOURCE_FILE_BYTES: Final = 45_776_904
SOURCE_MANIFEST_SHA256: Final = (
    "c8e9df64844b9d505321ff95c5b4d99299e4da3c7ae8fabe245ab681db46746e"
)
RELATIONAL_ROOT_SHA256: Final = (
    "69c2855c684677803cb2570b71a8d6e800a1d7b9b74be5593fcf23dbaf55183a"
)
SOURCE_SCHEMA_SHA256: Final = (
    "221e053eb0f6733caf54a2e2aef6e99ac0165858e97c717a85bdddba673be3a4"
)
SOURCE_MIGRATION: Final = "20260828_0063"
TARGET_MIGRATION: Final = "20260830_0064"
POSTGRES_VERSION: Final = "16.14"
CATALOG_VERSION: Final = "202307071"
SYSTEM_IDENTIFIER: Final = "7677548509991177425"
DATABASE_OID: Final = 16384
DATABASE_NAME: Final = "recovered"
PORT: Final = 65439
COMMAND_TIMEOUT_SECONDS: Final = 180
STOP_TIMEOUT_SECONDS: Final = 90
PG_CTL_WAIT_SECONDS: Final = 60

CHECKPOINT_REDO: Final = "0/7AA77A0"
CHECKPOINT_RECORD: Final = "0/7AA77D8"
WAL_END: Final = "0/7AA7888"
WAL_SEGMENT_7: Final = "000000010000000000000007"
MISNAMED_WAL_SEGMENT_9: Final = "000000010000000000000009"
WAL_MAGIC: Final = 0xD113
WAL_SEGMENT_7_PAGE_ADDRESS: Final = 0x07000000
MISNAMED_WAL_PAGE_ADDRESS: Final = 0x06000000

TOAST_HEAP_OID: Final = 17953
TOAST_INDEX_OID: Final = 17954
TOAST_HEAP_FILE_BYTES: Final = 40_960
TOAST_INDEX_FILE_BYTES: Final = 16_384
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]


class M4RecoveryError(RuntimeError):
    """A stable, non-sensitive recovery rejection."""

    def __init__(self, rule_code: str, safe_summary: str) -> None:
        super().__init__(safe_summary)
        self.rule_code = rule_code
        self.safe_summary = safe_summary


@dataclass(frozen=True, slots=True)
class FileManifest:
    file_count: int
    total_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RelationSpec:
    name: str
    key_sql: str
    where_sql: str


@dataclass(frozen=True, slots=True)
class RelationDigest:
    row_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    work_root: Path
    target_pgdata: Path
    report_path: Path
    relation_count: int
    row_count: int
    relational_root_sha256: str
    target_migration: str


RELATIONS: Final = (
    RelationSpec(
        "users",
        "x.owner_id",
        "x.owner_id=(SELECT owner_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "principals",
        "x.principal_id",
        "x.principal_id=(SELECT principal_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "tenants",
        "x.tenant_id",
        "x.tenant_id=(SELECT tenant_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "tenant_principals",
        "x.tenant_id||':'||x.principal_id",
        "(x.tenant_id,x.principal_id)=(SELECT tenant_id,principal_id "
        "FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "task_tenant_bindings",
        "x.tenant_id||':'||x.task_id",
        "(x.tenant_id,x.task_id,x.principal_id)="
        "(SELECT tenant_id,task_id,principal_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "direct_session_bindings",
        "x.session_id",
        "x.session_id=(SELECT session_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "tasks",
        "x.task_id",
        "x.task_id=(SELECT task_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "runs",
        "x.run_id",
        "x.run_id=(SELECT run_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "data_packages",
        "x.package_id",
        "x.created_by_run_id=(SELECT run_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "data_package_members",
        "x.member_id",
        "x.package_id IN (SELECT package_id FROM data_packages WHERE "
        "created_by_run_id=(SELECT run_id FROM pg_temp.m4_recovery_scope))",
    ),
    RelationSpec(
        "critical_operations",
        "x.operation_id",
        "x.run_id=(SELECT run_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "critical_operation_inputs",
        "x.input_binding_id",
        "x.operation_id IN (SELECT operation_id FROM critical_operations WHERE "
        "run_id=(SELECT run_id FROM pg_temp.m4_recovery_scope))",
    ),
    RelationSpec(
        "data_catalog_entries",
        "x.catalog_entry_id",
        "x.run_id=(SELECT run_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "artifacts",
        "x.artifact_id",
        "x.run_id=(SELECT run_id FROM pg_temp.m4_recovery_scope)",
    ),
    RelationSpec(
        "artifact_versions",
        "x.version_id",
        "x.artifact_id IN (SELECT artifact_id FROM artifacts WHERE "
        "run_id=(SELECT run_id FROM pg_temp.m4_recovery_scope))",
    ),
)

EXPECTED_RELATION_DIGESTS: Final = {
    "artifact_versions": RelationDigest(
        107, "c26124c748f66f8b9585dfbe507c8951cfa25459859e074a7f6b54ddf01224b2"
    ),
    "artifacts": RelationDigest(
        107, "6790e864e036cc126ea579589c72a93bffe4946cc75ddecc1a34cf7b3d571178"
    ),
    "critical_operation_inputs": RelationDigest(
        65, "2c61eb2d0d4eeec85cdfa2ddf52b94f3718d53cb2991798a863fda4cb2e8e71d"
    ),
    "critical_operations": RelationDigest(
        11, "9b10119596d03004c4b61b59ae380ec14081be2902ceb68f1e5099358880a5d2"
    ),
    "data_catalog_entries": RelationDigest(
        21, "65f661e84f16f0a4fbfaa0507ff2ce0a5f5c6d18a2d01c87a099691d3fc292c2"
    ),
    "data_package_members": RelationDigest(
        109, "e0cdcf4101165ad7d481f7f9a8507ecae124b902ef81ce85e7a75a7dd0aa21b1"
    ),
    "data_packages": RelationDigest(
        10, "2a687929dbfb8207f57a188c6543b5db8f8dca720560ec93a7a1c48506a2ac50"
    ),
    "direct_session_bindings": RelationDigest(
        1, "bced04952728197890f97d1961ca2f72f3576c2dba6e8d7882a5b50384002909"
    ),
    "principals": RelationDigest(
        1, "4b114742c12d1ca9ef47480b76c485e57c0ad1c84890dd4a27eab55b6330dd7c"
    ),
    "runs": RelationDigest(
        1, "7a310c18d1a1e231f6dd8f698cb258f2f34aecfd55a54d629df02445c466514f"
    ),
    "task_tenant_bindings": RelationDigest(
        1, "9bd5a0cbfb295dc28c4be728dd9820dc7052d7ebc04caed081167c1bb595ab91"
    ),
    "tasks": RelationDigest(
        1, "98c66f1a299fb089e8b1d39b5362d3cbca71f96e9c1eb4f3ca2ee96e7d714d4f"
    ),
    "tenant_principals": RelationDigest(
        1, "7c5b02935aaef5dd9ed79b64b070efc09b99d4991dafdaef765558487da0bb2b"
    ),
    "tenants": RelationDigest(
        1, "3bd61ad496b8e4c1e38f1f74ede264156698418e6e680c181026bc5f3f111415"
    ),
    "users": RelationDigest(
        1, "adfae5a09a1951f7c11690b76520d8c1c61ac77adb373bab35b12599e30bd59e"
    ),
}

EXPECTED_RELATION_FILES: Final = {
    "users": (16390, 16390),
    "principals": (16985, 16985),
    "tenants": (16999, 16999),
    "tenant_principals": (17010, 17010),
    "task_tenant_bindings": (17147, 17147),
    "direct_session_bindings": (17834, 17834),
    "tasks": (16399, 16399),
    "runs": (16420, 16420),
    "data_packages": (17874, 17874),
    "data_package_members": (17911, 17911),
    "critical_operations": (17946, 17946),
    "critical_operation_inputs": (18064, 18064),
    "data_catalog_entries": (17985, 18117),
    "artifacts": (16449, 16449),
    "artifact_versions": (16594, 16594),
}

_BINARIES: Final = (
    "postgres",
    "pg_controldata",
    "pg_waldump",
    "pg_ctl",
    "pg_amcheck",
    "initdb",
    "createdb",
)


def _reject(rule_code: str, safe_summary: str) -> M4RecoveryError:
    return M4RecoveryError(rule_code, safe_summary)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def compute_file_manifest(root: Path) -> FileManifest:
    """Hash a real directory without following links or special files."""

    candidate = Path(root)
    try:
        root_info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _reject("I0_M4_SOURCE_INVALID", "source PGDATA copy is unavailable") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise _reject("I0_M4_SOURCE_INVALID", "source PGDATA copy must be a real directory")

    files: list[tuple[str, Path, int]] = []
    for directory, subdirectories, filenames in os.walk(resolved, followlinks=False):
        current = Path(directory)
        for name in tuple(subdirectories):
            info = (current / name).lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _reject(
                    "I0_M4_SOURCE_INVALID",
                    "source PGDATA copy contains a link or special directory",
                )
        for name in filenames:
            path = current / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise _reject(
                    "I0_M4_SOURCE_INVALID",
                    "source PGDATA copy contains a link or special file",
                )
            relative = path.relative_to(resolved).as_posix()
            files.append((relative, path, info.st_size))

    lines: list[str] = []
    total_bytes = 0
    for relative, path, size in sorted(files):
        lines.append(f"{_sha256_file(path)}  ./{relative}\n")
        total_bytes += size
    digest = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
    return FileManifest(len(files), total_bytes, digest)


def validate_source_copy(source_pgdata: Path, original_pgdata: Path) -> Path:
    resolved = Path(source_pgdata).resolve(strict=True)
    try:
        original = Path(original_pgdata).resolve(strict=True)
    except OSError as exc:
        raise _reject(
            "I0_M4_ORIGINAL_INVALID",
            "the protected original PGDATA is unavailable",
        ) from exc
    if resolved == original:
        raise _reject("I0_M4_ORIGINAL_FORBIDDEN", "the original PGDATA is never a recovery input")
    manifest = compute_file_manifest(resolved)
    expected = FileManifest(
        SOURCE_FILE_COUNT,
        SOURCE_FILE_BYTES,
        SOURCE_MANIFEST_SHA256,
    )
    if manifest != expected:
        raise _reject("I0_M4_MANIFEST_MISMATCH", "source PGDATA copy does not match the attested manifest")
    return resolved


def validate_new_work_root(source: Path, work_root: Path) -> Path:
    candidate = Path(work_root)
    if not candidate.is_absolute() or candidate == Path(candidate.anchor):
        raise _reject("I0_M4_WORK_ROOT_INVALID", "recovery work root must be a safe absolute path")
    try:
        candidate.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise _reject("I0_M4_WORK_ROOT_INVALID", "recovery work root is invalid") from exc
    else:
        raise _reject("I0_M4_WORK_ROOT_EXISTS", "recovery work root must not already exist")
    try:
        parent_info = candidate.parent.lstat()
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise _reject("I0_M4_WORK_ROOT_INVALID", "recovery work parent is unavailable") from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise _reject("I0_M4_WORK_ROOT_INVALID", "recovery work parent must be a real directory")
    resolved_target = parent / candidate.name
    if resolved_target == REPOSITORY_ROOT or resolved_target.is_relative_to(REPOSITORY_ROOT):
        raise _reject("I0_M4_WORK_ROOT_INVALID", "recovery work root must be outside the repository")
    if resolved_target == source or resolved_target.is_relative_to(source):
        raise _reject("I0_M4_WORK_ROOT_INVALID", "recovery work root must be separate from the source")
    return resolved_target


def _run(command_line: list[str], rule_code: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command_line,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _reject(rule_code, "required PostgreSQL operation could not start") from exc
    if result.returncode != 0:
        raise _reject(rule_code, "required PostgreSQL operation failed")
    return result


def _postgres_binaries() -> dict[str, str]:
    binaries: dict[str, str] = {}
    for name in _BINARIES:
        path = shutil.which(name)
        if path is None:
            raise _reject("I0_M4_POSTGRES_UNAVAILABLE", "required PostgreSQL 16 tool is unavailable")
        binaries[name] = path
    for name in ("postgres", "pg_controldata", "pg_waldump"):
        version = _run([binaries[name], "--version"], "I0_M4_POSTGRES_UNAVAILABLE").stdout
        if f"PostgreSQL) {POSTGRES_VERSION}" not in version:
            raise _reject("I0_M4_POSTGRES_VERSION", "PostgreSQL tool version is not the attested version")
    return binaries


def _control_values(pg_controldata: str, pgdata: Path) -> dict[str, str]:
    output = _run([pg_controldata, str(pgdata)], "I0_M4_CONTROL_INVALID").stdout
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    return values


def _wal_header(path: Path) -> tuple[int, int, int]:
    try:
        header = path.read_bytes()[:32]
    except OSError as exc:
        raise _reject("I0_M4_WAL_INVALID", "required WAL file is unreadable") from exc
    if len(header) != 32:
        raise _reject("I0_M4_WAL_INVALID", "required WAL header is incomplete")
    magic = struct.unpack_from("<H", header, 0)[0]
    page_address = struct.unpack_from("<Q", header, 8)[0]
    system_identifier = struct.unpack_from("<Q", header, 24)[0]
    return magic, page_address, system_identifier


def _validate_control_and_wal(
    source: Path,
    binaries: dict[str, str],
) -> None:
    control = _control_values(binaries["pg_controldata"], source)
    required = {
        "pg_control version number": "1300",
        "Catalog version number": CATALOG_VERSION,
        "Database system identifier": SYSTEM_IDENTIFIER,
        "Database cluster state": "in production",
        "Latest checkpoint location": CHECKPOINT_RECORD,
        "Latest checkpoint's REDO location": CHECKPOINT_REDO,
        "Latest checkpoint's REDO WAL file": WAL_SEGMENT_7,
        "Latest checkpoint's NextXID": "0:2228",
        "Latest checkpoint's NextOID": "32768",
        "Database block size": "8192",
        "Bytes per WAL segment": "16777216",
    }
    if any(control.get(key) != value for key, value in required.items()):
        raise _reject("I0_M4_CONTROL_MISMATCH", "pg_control does not match the attested source")

    wal_directory = source / "pg_wal"
    seven = _wal_header(wal_directory / WAL_SEGMENT_7)
    nine = _wal_header(wal_directory / MISNAMED_WAL_SEGMENT_9)
    expected_system_id = int(SYSTEM_IDENTIFIER)
    if seven != (WAL_MAGIC, WAL_SEGMENT_7_PAGE_ADDRESS, expected_system_id):
        raise _reject("I0_M4_WAL_MISMATCH", "checkpoint WAL header does not match the attested source")
    if nine != (WAL_MAGIC, MISNAMED_WAL_PAGE_ADDRESS, expected_system_id):
        raise _reject("I0_M4_WAL_MISMATCH", "misnamed WAL evidence does not match the attested source")
    if (wal_directory / "000000010000000000000008").exists():
        raise _reject("I0_M4_WAL_MISMATCH", "unexpected WAL segment changes the attested recovery path")

    records = _run(
        [
            binaries["pg_waldump"],
            "-p",
            str(wal_directory),
            "-s",
            CHECKPOINT_REDO,
            "-n",
            "3",
        ],
        "I0_M4_WAL_INVALID",
    ).stdout
    required_records = (
        "lsn: 0/07AA77A0",
        "lsn: 0/07AA77D8",
        f"redo {CHECKPOINT_REDO}",
        "lsn: 0/07AA7850",
    )
    if any(record not in records for record in required_records):
        raise _reject("I0_M4_WAL_MISMATCH", "checkpoint WAL records do not match the attested source")


def relation_root(digests: dict[str, RelationDigest]) -> str:
    payload = "".join(
        f"{len(name)}:{name}:{digests[name].row_count}:{digests[name].sha256}"
        for name in sorted(digests)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _schema_digest(connection: psycopg.Connection[object]) -> str:
    descriptor: list[tuple[str, list[tuple[object, ...]]]] = []
    with connection.cursor() as cursor:
        for relation in RELATIONS:
            cursor.execute(
                """
                SELECT a.attnum, a.attname,
                       pg_catalog.format_type(a.atttypid, a.atttypmod),
                       a.attnotnull
                FROM pg_catalog.pg_attribute AS a
                WHERE a.attrelid = %s::regclass
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY a.attnum
                """,
                (f"public.{relation.name}",),
            )
            descriptor.append((relation.name, [tuple(row) for row in cursor.fetchall()]))
    encoded = json.dumps(
        descriptor,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _relation_digests(
    connection: psycopg.Connection[object],
) -> dict[str, RelationDigest]:
    result: dict[str, RelationDigest] = {}
    with connection.cursor() as cursor:
        for relation in RELATIONS:
            cursor.execute(
                f"SELECT ({relation.key_sql})::text, to_jsonb(x)::text "
                f"FROM public.{relation.name} AS x "
                f"WHERE {relation.where_sql} ORDER BY 1"
            )
            rows = cursor.fetchall()
            content = "".join(f"{len(row[1])}:{row[1]}" for row in rows)
            result[relation.name] = RelationDigest(
                len(rows),
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
    return result


def _validate_relation_digests(connection: psycopg.Connection[object]) -> None:
    actual = _relation_digests(connection)
    if actual != EXPECTED_RELATION_DIGESTS:
        raise _reject("I0_M4_RELATION_MISMATCH", "M4 relation rows do not match the attested slice")
    if relation_root(actual) != RELATIONAL_ROOT_SHA256:
        raise _reject("I0_M4_RELATION_ROOT_MISMATCH", "M4 relational root does not match")


def _create_attested_source_scope(connection: psycopg.Connection[object]) -> None:
    candidates = connection.execute(
        """
        SELECT b.session_id
        FROM direct_session_bindings b
        WHERE b.ordinal=18
          AND (SELECT count(*) FROM data_packages p
               WHERE (p.owner_id,p.tenant_id,p.task_id,p.created_by_run_id)=
                     (b.owner_id,b.tenant_id,b.task_id,b.run_id))=10
          AND (SELECT count(*) FROM critical_operations o
               WHERE (o.owner_id,o.tenant_id,o.task_id,o.run_id)=
                     (b.owner_id,b.tenant_id,b.task_id,b.run_id))=11
          AND (SELECT count(*) FROM data_catalog_entries c
               WHERE (c.owner_id,c.tenant_id,c.task_id,c.run_id)=
                     (b.owner_id,b.tenant_id,b.task_id,b.run_id))=21
          AND (SELECT count(*) FROM artifacts a
               WHERE (a.owner_id,a.tenant_id,a.task_id,a.run_id)=
                     (b.owner_id,b.tenant_id,b.task_id,b.run_id))=107
          AND (SELECT count(*) FROM artifact_versions v JOIN artifacts a
               ON (a.owner_id,a.tenant_id,a.task_id,a.artifact_id)=
                  (v.owner_id,v.tenant_id,v.task_id,v.artifact_id)
               WHERE (a.owner_id,a.tenant_id,a.task_id,a.run_id)=
                     (b.owner_id,b.tenant_id,b.task_id,b.run_id))=107
        ORDER BY b.session_id
        """
    ).fetchall()
    if len(candidates) != 1:
        raise _reject(
            "I0_M4_BINDING_MISMATCH",
            "M4 source does not have one structurally attested Direct binding",
        )
    connection.execute(
        """
        CREATE TEMP TABLE m4_recovery_scope ON COMMIT PRESERVE ROWS AS
        SELECT * FROM direct_session_bindings WHERE session_id=%s
        """,
        (candidates[0][0],),
    )


def _create_imported_scope(connection: psycopg.Connection[object]) -> None:
    if connection.execute("SELECT count(*) FROM direct_session_bindings").fetchone() != (1,):
        raise _reject(
            "I0_M4_BINDING_MISMATCH",
            "imported target does not contain exactly one Direct binding",
        )
    connection.execute(
        """
        CREATE TEMP TABLE m4_recovery_scope ON COMMIT PRESERVE ROWS AS
        SELECT * FROM direct_session_bindings
        """
    )


def _validate_closure(connection: psycopg.Connection[object]) -> None:
    metrics = connection.execute(
        """
        WITH t AS (
          SELECT * FROM pg_temp.m4_recovery_scope
        ),
        p AS (
          SELECT p.* FROM data_packages p JOIN t
          ON (p.owner_id,p.tenant_id,p.task_id,p.created_by_run_id)=
             (t.owner_id,t.tenant_id,t.task_id,t.run_id)
        ),
        o AS (
          SELECT o.* FROM critical_operations o JOIN t
          ON (o.owner_id,o.tenant_id,o.task_id,o.run_id)=
             (t.owner_id,t.tenant_id,t.task_id,t.run_id)
        ),
        c AS (
          SELECT c.* FROM data_catalog_entries c JOIN t
          ON (c.owner_id,c.tenant_id,c.task_id,c.run_id)=
             (t.owner_id,t.tenant_id,t.task_id,t.run_id)
        ),
        m AS (
          SELECT m.* FROM data_package_members m JOIN p
          ON (p.owner_id,p.tenant_id,p.task_id,p.package_id)=
             (m.owner_id,m.tenant_id,m.task_id,m.package_id)
        ),
        i AS (
          SELECT i.* FROM critical_operation_inputs i JOIN o
          ON (o.owner_id,o.tenant_id,o.task_id,o.operation_id)=
             (i.owner_id,i.tenant_id,i.task_id,i.operation_id)
        )
        SELECT
          (SELECT count(*) FROM t),
          (SELECT count(*) FROM t WHERE ordinal=18),
          (SELECT count(*) FROM p),
          (SELECT count(*) FROM m),
          (SELECT count(*) FROM o),
          (SELECT count(*) FROM i),
          (SELECT count(*) FROM c),
          (SELECT count(*) FROM c WHERE resource_kind='package' AND ordinal BETWEEN 1 AND 10),
          (SELECT count(*) FROM c WHERE resource_kind='operation' AND ordinal BETWEEN 1 AND 11),
          (SELECT count(DISTINCT catalog_revision) FROM c),
          (SELECT count(*) FROM c WHERE catalog_revision=10 AND resource_kind='package'),
          (SELECT count(*) FROM c WHERE catalog_revision=11 AND resource_kind='operation'),
          (SELECT count(*) FROM m JOIN c ON c.package_id=m.package_id
             WHERE c.catalog_revision=10 AND c.resource_kind='package'
               AND m.member_kind='payload'),
          (SELECT count(DISTINCT logical_payload_name) FROM m JOIN c ON c.package_id=m.package_id
             WHERE c.catalog_revision=10 AND c.resource_kind='package'
               AND m.member_kind='payload'),
          (SELECT count(*) FROM m LEFT JOIN artifact_versions v
             ON (v.owner_id,v.tenant_id,v.task_id,v.artifact_id,v.version_id,v.sha256)=
                (m.owner_id,m.tenant_id,m.task_id,m.artifact_id,m.artifact_version_id,m.artifact_sha256)
             WHERE v.version_id IS NULL),
          (SELECT count(*) FROM i JOIN m
             ON (m.owner_id,m.tenant_id,m.task_id,m.package_id,m.member_id)=
                (i.owner_id,i.tenant_id,i.task_id,i.package_id,i.member_id)
             WHERE i.payload_name IS DISTINCT FROM COALESCE(
               m.logical_payload_name,
               CASE WHEN m.member_kind='payload'
                     AND split_part(m.member_name,'/',1)='payloads'
                    THEN split_part(m.member_name,'/',2) END)),
          (SELECT count(*) FROM o LEFT JOIN p
             ON (p.owner_id,p.tenant_id,p.task_id,p.package_id)=
                (o.owner_id,o.tenant_id,o.task_id,o.result_package_id)
             WHERE o.result_package_id IS NOT NULL AND p.package_id IS NULL),
          (SELECT count(*) FROM o child LEFT JOIN o parent
             ON (parent.owner_id,parent.tenant_id,parent.task_id,parent.operation_id)=
                (child.owner_id,child.tenant_id,child.task_id,child.repair_parent_operation_id)
             WHERE child.repair_parent_operation_id IS NOT NULL
               AND parent.operation_id IS NULL)
        """
    ).fetchone()
    expected = (1, 1, 10, 109, 11, 65, 21, 10, 11, 11, 1, 1, 14, 14, 0, 0, 0, 0)
    if metrics != expected:
        raise _reject("I0_M4_CLOSURE_MISMATCH", "M4 relation closure is incomplete")


def _validate_relation_files(connection: psycopg.Connection[object]) -> None:
    connection.execute("SET enable_indexscan=off")
    connection.execute("SET enable_indexonlyscan=off")
    connection.execute("SET enable_bitmapscan=off")
    rows = connection.execute(
        """
        SELECT relname, oid, relfilenode
        FROM pg_catalog.pg_class
        WHERE relname = ANY(%s)
        """,
        (list(EXPECTED_RELATION_FILES),),
    ).fetchall()
    actual = {str(name): (int(oid), int(filenode)) for name, oid, filenode in rows}
    if actual != EXPECTED_RELATION_FILES:
        raise _reject("I0_M4_RELATION_FILE_MISMATCH", "relation OID mapping does not match")


def _repair_attested_toast_index(
    connection: psycopg.Connection[object],
    pgdata: Path,
) -> None:
    toast_rows = connection.execute(
        """
        SELECT oid, relname, relkind, relfilenode
        FROM pg_catalog.pg_class
        WHERE oid IN (17953,17954)
        ORDER BY oid
        """
    ).fetchall()
    if toast_rows != [
        (TOAST_HEAP_OID, "pg_toast_17946", "t", TOAST_HEAP_OID),
        (TOAST_INDEX_OID, "pg_toast_17946_index", "i", TOAST_INDEX_OID),
    ]:
        raise _reject("I0_M4_TOAST_MISMATCH", "Operation TOAST relation mapping does not match")
    heap_path = pgdata / "base" / str(DATABASE_OID) / str(TOAST_HEAP_OID)
    index_path = pgdata / "base" / str(DATABASE_OID) / str(TOAST_INDEX_OID)
    try:
        sizes = (heap_path.stat().st_size, index_path.stat().st_size)
    except OSError as exc:
        raise _reject("I0_M4_TOAST_MISMATCH", "Operation TOAST files are unavailable") from exc
    if sizes != (TOAST_HEAP_FILE_BYTES, TOAST_INDEX_FILE_BYTES):
        raise _reject("I0_M4_TOAST_MISMATCH", "Operation TOAST files do not match")
    existing = connection.execute(
        "SELECT count(*) FROM pg_catalog.pg_index WHERE indrelid=17953 OR indexrelid=17954"
    ).fetchone()
    if existing != (0,):
        raise _reject("I0_M4_TOAST_MISMATCH", "Operation TOAST catalog state is not the attested gap")
    connection.execute(
        """
        INSERT INTO pg_catalog.pg_index (
          indexrelid, indrelid, indnatts, indnkeyatts,
          indisunique, indnullsnotdistinct, indisprimary, indisexclusion,
          indimmediate, indisclustered, indisvalid, indcheckxmin,
          indisready, indislive, indisreplident,
          indkey, indcollation, indclass, indoption, indexprs, indpred
        ) VALUES (
          17954,17953,2,2,true,false,true,false,true,false,true,false,
          true,true,false,'1 2','0 0','1981 1978','0 0',NULL,NULL
        )
        """
    )
    restored = connection.execute(
        """
        SELECT indexrelid,indrelid,indnatts,indnkeyatts,indisunique,
               indisprimary,indisvalid,indisready,indkey::text,
               indcollation::text,indclass::text,indoption::text
        FROM pg_catalog.pg_index WHERE indrelid=17953
        """
    ).fetchall()
    if restored != [
        (17954, 17953, 2, 2, True, True, True, True, "1 2", "0 0", "1981 1978", "0 0")
    ]:
        raise _reject("I0_M4_TOAST_MISMATCH", "Operation TOAST catalog repair did not verify")


def _copy_directory_contents(source: Path, target: Path) -> None:
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True, copy_function=shutil.copy2)
        else:
            shutil.copy2(item, destination)


def _configure_cluster(pgdata: Path, socket_directory: Path) -> None:
    if len(str(socket_directory)) > 88:
        raise _reject("I0_M4_WORK_ROOT_INVALID", "recovery Unix socket path is too long")
    escaped = str(socket_directory).replace("'", "''")
    with (pgdata / "postgresql.conf").open("a", encoding="utf-8") as stream:
        stream.write("\nlisten_addresses = ''\n")
        stream.write(f"unix_socket_directories = '{escaped}'\n")
        stream.write(f"port = {PORT}\n")
        stream.write("autovacuum = off\n")
        stream.write("allow_system_table_mods = on\n")
        stream.write("statement_timeout = '120s'\n")
        stream.write("lock_timeout = '10s'\n")


def _start_cluster(binaries: dict[str, str], pgdata: Path, log_path: Path) -> None:
    _run(
        [
            binaries["pg_ctl"],
            "-D",
            str(pgdata),
            "-l",
            str(log_path),
            "-w",
            "-t",
            str(PG_CTL_WAIT_SECONDS),
            "start",
        ],
        "I0_M4_POSTGRES_START_FAILED",
    )


def _stop_cluster(binaries: dict[str, str], pgdata: Path) -> None:
    subprocess.run(
        [
            binaries["pg_ctl"],
            "-D",
            str(pgdata),
            "-w",
            "-t",
            str(PG_CTL_WAIT_SECONDS),
            "stop",
            "-m",
            "fast",
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=STOP_TIMEOUT_SECONDS,
    )


def _connect(socket_directory: Path) -> psycopg.Connection[object]:
    try:
        return psycopg.connect(
            dbname=DATABASE_NAME,
            host=str(socket_directory),
            port=PORT,
            autocommit=True,
            connect_timeout=5,
        )
    except psycopg.Error as exc:
        raise _reject("I0_M4_DATABASE_CONNECT_FAILED", "isolated recovery database is unavailable") from exc


def _initialize_cluster(
    binaries: dict[str, str],
    pgdata: Path,
    socket_directory: Path,
    log_path: Path,
) -> None:
    _run(
        [
            binaries["initdb"],
            "-D",
            str(pgdata),
            "--no-locale",
            "--encoding=UTF8",
            "-A",
            "trust",
        ],
        "I0_M4_INITDB_FAILED",
    )
    _configure_cluster(pgdata, socket_directory)
    _start_cluster(binaries, pgdata, log_path)
    try:
        _run(
            [
                binaries["createdb"],
                "-h",
                str(socket_directory),
                "-p",
                str(PORT),
                DATABASE_NAME,
            ],
            "I0_M4_DATABASE_CREATE_FAILED",
        )
        with _connect(socket_directory) as connection:
            row = connection.execute(
                "SELECT oid FROM pg_catalog.pg_database WHERE datname=%s",
                (DATABASE_NAME,),
            ).fetchone()
            if row != (DATABASE_OID,):
                raise _reject("I0_M4_DATABASE_OID_MISMATCH", "scaffold database OID is not attested")
            # The source pg_xact replaces the scaffold transaction-status file.
            # Freeze the one scaffold-created shared-catalog tuple first so that
            # later maintenance connections cannot lose the database row.
            connection.execute("VACUUM FREEZE pg_catalog.pg_database")
    finally:
        _stop_cluster(binaries, pgdata)


def _build_forensic_cluster(scaffold: Path, source: Path, forensic: Path) -> None:
    shutil.copytree(scaffold, forensic, copy_function=shutil.copy2)
    _copy_directory_contents(source / "base" / str(DATABASE_OID), forensic / "base" / str(DATABASE_OID))
    shutil.rmtree(forensic / "pg_wal")
    (forensic / "pg_wal" / "archive_status").mkdir(parents=True)
    shutil.copy2(source / "pg_wal" / WAL_SEGMENT_7, forensic / "pg_wal" / WAL_SEGMENT_7)
    for directory in ("pg_xact", "pg_subtrans", "pg_multixact"):
        _copy_directory_contents(source / directory, forensic / directory)
    shutil.copy2(
        source / "pg_logical" / "replorigin_checkpoint",
        forensic / "pg_logical" / "replorigin_checkpoint",
    )
    shutil.copy2(source / "global" / "pg_control", forensic / "global" / "pg_control")


def _migration_url(socket_directory: Path) -> str:
    return URL.create(
        "postgresql+psycopg",
        database=DATABASE_NAME,
        query={"host": str(socket_directory), "port": str(PORT)},
    ).render_as_string(hide_password=False)


def _migrate(socket_directory: Path, revision: str) -> None:
    try:
        configuration = Config(str(REPOSITORY_ROOT / "alembic.ini"))
        configuration.set_main_option(
            "sqlalchemy.url",
            _migration_url(socket_directory).replace("%", "%%"),
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            command.upgrade(configuration, revision)
    except Exception as exc:
        raise _reject("I0_M4_MIGRATION_FAILED", "isolated target migration failed") from exc


def _require_empty_target(connection: psycopg.Connection[object]) -> None:
    tables = connection.execute(
        """
        SELECT c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r' AND c.relname<>'alembic_version'
        ORDER BY c.relname
        """
    ).fetchall()
    for (name,) in tables:
        count = connection.execute(
            sql.SQL("SELECT count(*) FROM public.{} ").format(sql.Identifier(name))
        ).fetchone()
        if count != (0,):
            raise _reject("I0_M4_TARGET_NOT_EMPTY", "isolated target database contains business rows")


def _copy_out(connection: psycopg.Connection[object], output_root: Path) -> None:
    output_root.mkdir(mode=0o700)
    with connection.cursor() as cursor:
        for relation in RELATIONS:
            path = output_root / f"{relation.name}.bin"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                statement = (
                    f"COPY (SELECT x.* FROM public.{relation.name} AS x "
                    f"WHERE {relation.where_sql} ORDER BY {relation.key_sql}) "
                    "TO STDOUT (FORMAT BINARY)"
                )
                with cursor.copy(statement) as copy:
                    for chunk in copy:
                        stream.write(chunk)


def _copy_in(connection: psycopg.Connection[object], input_root: Path) -> None:
    try:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL session_replication_role='replica'")
            for relation in RELATIONS:
                with (input_root / f"{relation.name}.bin").open("rb") as stream:
                    with cursor.copy(
                        f"COPY public.{relation.name} FROM STDIN (FORMAT BINARY)"
                    ) as copy:
                        while chunk := stream.read(1024 * 1024):
                            copy.write(chunk)
            cursor.execute("SET LOCAL session_replication_role='origin'")
    except (OSError, psycopg.Error) as exc:
        raise _reject("I0_M4_IMPORT_FAILED", "isolated target import failed") from exc


def _amcheck(
    binaries: dict[str, str],
    socket_directory: Path,
) -> None:
    base = [
        binaries["pg_amcheck"],
        "-h",
        str(socket_directory),
        "-p",
        str(PORT),
        "-d",
        DATABASE_NAME,
    ]
    tables = [item for relation in RELATIONS for item in ("--table", f"public.{relation.name}")]
    _run(
        base
        + tables
        + [
            "--no-dependent-indexes",
            "--no-dependent-toast",
            "--exclude-toast-pointers",
            "--on-error-stop",
        ],
        "I0_M4_HEAP_CHECK_FAILED",
    )
    _run(
        base
        + [
            "--table",
            "public.critical_operations",
            "--no-dependent-indexes",
            "--on-error-stop",
        ],
        "I0_M4_TOAST_CHECK_FAILED",
    )


async def _formal_exporter_read(
    socket_directory: Path,
    run_id: str,
) -> None:
    engine = create_async_engine(_migration_url(socket_directory), pool_pre_ping=True)
    try:
        reader = PostgresSeedSnapshotReader(engine)
        resolved = await reader.resolve_source_run(run_id)
        snapshot = await reader.load(resolved.scope)
        counts = (
            len(snapshot.packages),
            len(snapshot.package_members),
            len(snapshot.operations),
            len(snapshot.operation_inputs),
            len(snapshot.catalog_entries),
            len(snapshot.artifacts),
            len(snapshot.artifact_versions),
            len(snapshot.ifc_models),
            len(snapshot.ifc_revisions),
        )
        if counts != (10, 109, 11, 65, 21, 107, 107, 0, 0):
            raise _reject("I0_M4_EXPORTER_READBACK", "existing exporter readback changed the recovered slice")
    finally:
        await engine.dispose()


def _write_report(work_root: Path) -> Path:
    report = {
        "status": "verified_clean_round_trip",
        "source_manifest_sha256": SOURCE_MANIFEST_SHA256,
        "source_migration": SOURCE_MIGRATION,
        "target_migration": TARGET_MIGRATION,
        "postgres_version": POSTGRES_VERSION,
        "relation_count": len(EXPECTED_RELATION_DIGESTS),
        "row_count": sum(item.row_count for item in EXPECTED_RELATION_DIGESTS.values()),
        "relational_root_sha256": RELATIONAL_ROOT_SHA256,
        "exporter_readback": "passed",
        "seed_generated": False,
    }
    path = work_root / "recovery-report.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
    return path


def recover_m4_round6_database(
    *,
    original_pgdata: Path,
    source_pgdata_copy: Path,
    work_root: Path,
) -> RecoveryResult:
    """Recover the one attested Round 6 relation slice into a clean 0064 database."""

    source = validate_source_copy(source_pgdata_copy, original_pgdata)
    target_root = validate_new_work_root(source, work_root)
    binaries = _postgres_binaries()
    _validate_control_and_wal(source, binaries)

    target_root.mkdir(mode=0o700)
    source_copy = target_root / "source-copy"
    shutil.copytree(source, source_copy, copy_function=shutil.copy2)
    if compute_file_manifest(source_copy) != FileManifest(
        SOURCE_FILE_COUNT, SOURCE_FILE_BYTES, SOURCE_MANIFEST_SHA256
    ):
        raise _reject("I0_M4_COPY_MISMATCH", "private recovery copy changed source bytes")

    socket_directory = target_root / "socket"
    socket_directory.mkdir(mode=0o700)
    scaffold = target_root / "scaffold"
    forensic = target_root / "forensic-pgdata"
    clean = target_root / "clean-pgdata"
    relation_copy = target_root / "relation-copy"

    _initialize_cluster(
        binaries,
        scaffold,
        socket_directory,
        target_root / "scaffold-postgres.log",
    )
    _build_forensic_cluster(scaffold, source_copy, forensic)

    forensic_started = False
    try:
        _start_cluster(binaries, forensic, target_root / "forensic-postgres.log")
        forensic_started = True
        with _connect(socket_directory) as connection:
            _validate_relation_files(connection)
            connection.execute(f"REINDEX SYSTEM {DATABASE_NAME}")
            migration = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
            if migration != (SOURCE_MIGRATION,):
                raise _reject("I0_M4_MIGRATION_MISMATCH", "source migration is not attested 0063")
            if _schema_digest(connection) != SOURCE_SCHEMA_SHA256:
                raise _reject("I0_M4_SCHEMA_MISMATCH", "source relation schema does not match")
            _repair_attested_toast_index(connection, forensic)
            connection.execute("CREATE EXTENSION amcheck")
            _create_attested_source_scope(connection)
            _validate_relation_digests(connection)
            _validate_closure(connection)
            _copy_out(connection, relation_copy)
        _amcheck(binaries, socket_directory)
    finally:
        if forensic_started:
            _stop_cluster(binaries, forensic)

    _initialize_cluster(
        binaries,
        clean,
        socket_directory,
        target_root / "clean-postgres.log",
    )
    clean_started = False
    try:
        _start_cluster(binaries, clean, target_root / "clean-postgres.log")
        clean_started = True
        _migrate(socket_directory, SOURCE_MIGRATION)
        with _connect(socket_directory) as connection:
            if _schema_digest(connection) != SOURCE_SCHEMA_SHA256:
                raise _reject("I0_M4_SCHEMA_MISMATCH", "clean 0063 schema does not match source")
            _require_empty_target(connection)
            _copy_in(connection, relation_copy)
            _create_imported_scope(connection)
            _validate_relation_digests(connection)
            _validate_closure(connection)
            run_id_row = connection.execute(
                "SELECT run_id FROM pg_temp.m4_recovery_scope"
            ).fetchone()
            if run_id_row is None:
                raise _reject("I0_M4_CLOSURE_MISMATCH", "recovered Run is unavailable")
            run_id = str(run_id_row[0])
        _migrate(socket_directory, TARGET_MIGRATION)
        with _connect(socket_directory) as connection:
            migration = connection.execute(
                "SELECT version_num FROM public.alembic_version"
            ).fetchone()
            if migration != (TARGET_MIGRATION,):
                raise _reject("I0_M4_MIGRATION_MISMATCH", "clean target did not reach 0064")
        asyncio.run(_formal_exporter_read(socket_directory, run_id))
    finally:
        if clean_started:
            _stop_cluster(binaries, clean)

    shutil.rmtree(relation_copy)
    expected_manifest = FileManifest(
        SOURCE_FILE_COUNT,
        SOURCE_FILE_BYTES,
        SOURCE_MANIFEST_SHA256,
    )
    if (
        compute_file_manifest(source) != expected_manifest
        or compute_file_manifest(source_copy) != expected_manifest
    ):
        raise _reject("I0_M4_COPY_MISMATCH", "source bytes changed during recovery")
    report = _write_report(target_root)
    return RecoveryResult(
        work_root=target_root,
        target_pgdata=clean,
        report_path=report,
        relation_count=len(EXPECTED_RELATION_DIGESTS),
        row_count=sum(item.row_count for item in EXPECTED_RELATION_DIGESTS.values()),
        relational_root_sha256=RELATIONAL_ROOT_SHA256,
        target_migration=TARGET_MIGRATION,
    )
