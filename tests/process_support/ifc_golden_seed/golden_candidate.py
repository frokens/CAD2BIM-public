"""Test-only durable materialization of one explicit IFC Golden Candidate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcRevision,
    IfcModelInitializeResult,
)
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    bind_tenant_execution_context,
)
from CAD2BIM_backend.ports.ifc_revision_runtime import GeneratedIfcRevision
from tests.process_support.ifc_golden_seed.test_support import (
    OpenedSeedScope,
    SeedTestRuntime,
    ifc_scope,
    open_seed_scope,
)
from tests.process_support.ifc_revision_runtime import BytesIfcCandidate


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GoldenCandidateMaterializationError(ValueError):
    """The explicit Candidate cannot be safely committed as a test fixture."""


@dataclass(frozen=True, slots=True)
class MaterializedGoldenCandidate:
    """Fresh Run scope and its one committed Candidate revision."""

    opened_scope: OpenedSeedScope
    initialize_result: IfcModelInitializeResult
    exact_revision: ExactIfcRevision
    source_sha256: str


async def materialize_golden_candidate(
    runtime: SeedTestRuntime,
    *,
    source_path: Path,
    expected_sha256: str,
    configured_subject: str,
    session_name: str,
) -> MaterializedGoldenCandidate:
    """Copy one verified Candidate into a fresh Run through the durable kernel.

    This helper is intentionally test-only.  It creates no public import path:
    the production repository still owns Artifact, revision, CURRENT, and
    Operation persistence.
    """

    source = Path(source_path)
    if not source.is_absolute() or not source.is_file():
        raise GoldenCandidateMaterializationError(
            "Golden Candidate source_path must be an existing absolute file"
        )
    if _SHA256.fullmatch(expected_sha256) is None:
        raise GoldenCandidateMaterializationError(
            "Golden Candidate expected_sha256 must be lowercase hexadecimal"
        )

    content = await asyncio.to_thread(source.read_bytes)
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != expected_sha256:
        raise GoldenCandidateMaterializationError(
            "Golden Candidate bytes do not match the approved test fixture digest"
        )

    codec = IfcOpenShellModelCodec()
    inspection = codec.inspect(content)
    opened = await open_seed_scope(
        runtime,
        configured_subject=configured_subject,
        session_name=session_name,
    )
    scope = ifc_scope(opened.invocation_scope)
    request_digest = hashlib.sha256(
        json.dumps(
            {
                "fixture_kind": "ifc_golden_candidate",
                "project_global_id": inspection.project_global_id,
                "project_name": inspection.project_name,
                "source_sha256": actual_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    with bind_tenant_execution_context(opened.invocation_scope.identity):
        prepared = await runtime.repository.prepare_initialize(
            scope,
            request_digest=request_digest,
            project_global_id=inspection.project_global_id,
            project_name=inspection.project_name,
        )
        writer = runtime.ifc.writer
        claim = await writer.claim_execution(scope, prepared)
        generated = GeneratedIfcRevision(
            candidate=BytesIfcCandidate(content, sha256=actual_sha256),
            inspection=inspection,
        )
        try:
            initialized = await writer.commit_initialize(
                scope, prepared, generated, claim=claim,
            )
        finally:
            await generated.candidate.aclose()
            await writer.retire_execution(scope, claim)
        exact = await runtime.repository.get_current(scope)

    if exact is None or exact.revision_no != 1 or exact.write_epoch != 1:
        raise GoldenCandidateMaterializationError(
            "Golden Candidate did not become the sole revision-1 CURRENT"
        )
    if exact.artifact.sha256 != actual_sha256:
        raise GoldenCandidateMaterializationError(
            "Golden Candidate CURRENT does not bind the expected ArtifactVersion"
        )
    return MaterializedGoldenCandidate(
        opened_scope=opened,
        initialize_result=initialized.result,
        exact_revision=exact,
        source_sha256=actual_sha256,
    )


__all__ = [
    "GoldenCandidateMaterializationError",
    "MaterializedGoldenCandidate",
    "materialize_golden_candidate",
]
