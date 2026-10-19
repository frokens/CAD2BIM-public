from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactDownload
from CAD2BIM_backend.application.exact_artifact_reader import (
    ExactArtifactReadError,
    ExactArtifactReader,
)
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ExecutionMode
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope


_CONTENT = b"exact source bytes"
_SHA = hashlib.sha256(_CONTENT).hexdigest()


def _scope() -> InvocationScope:
    return InvocationScope(
        identity=TenantExecutionContext(
            tenant_id="tenant-exact",
            principal_id="principal-exact",
            task_id="task-exact",
            actor_kind=TenantActorKind.USER,
            command_id="command-exact",
        ),
        owner_id="tenant-exact",
        run_id="run-exact",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=None,
    )


def _record(**changes: object) -> ArtifactRecord:
    values: dict[str, object] = {
        "artifact_id": "artifact-exact",
        "version_id": "version-exact",
        "owner_id": "tenant-exact",
        "task_id": "task-exact",
        "run_id": "run-exact",
        "kind": "query_source",
        "filename": "query.py",
        "content_type": "text/x-python",
        "size_bytes": len(_CONTENT),
        "sha256": _SHA,
        "idempotency_key": "exact-reader-test",
        "created_at": datetime(2026, 9, 3, tzinfo=UTC),
    }
    values.update(changes)
    return ArtifactRecord.model_validate(values)


@pytest.mark.asyncio
async def test_exact_artifact_reader_reauthorizes_verified_exact_version() -> None:
    artifacts = SimpleNamespace(
        download_version=AsyncMock(
            return_value=ArtifactDownload(artifact=_record(), content=_CONTENT)
        )
    )
    reference = SimpleNamespace(
        artifact_id="artifact-exact", version_id="version-exact", sha256=_SHA
    )

    downloaded = await ExactArtifactReader(artifacts).read(
        _scope(), reference, expected_content_type="text/x-python"
    )

    assert downloaded.content == _CONTENT
    artifacts.download_version.assert_awaited_once_with(
        "tenant-exact",
        "artifact-exact",
        "version-exact",
        run_id="run-exact",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record_changes", "reference_changes"),
    [
        ({"task_id": "task-other"}, {}),
        ({"run_id": "run-other"}, {}),
        ({"artifact_id": "artifact-other"}, {}),
        ({"version_id": "version-other"}, {}),
        ({"content_type": "application/json"}, {}),
        ({}, {"sha256": "0" * 64}),
    ],
)
async def test_exact_artifact_reader_rejects_scope_identity_or_sha_drift(
    record_changes: dict[str, object],
    reference_changes: dict[str, object],
) -> None:
    artifacts = SimpleNamespace(
        download_version=AsyncMock(
            return_value=ArtifactDownload(artifact=_record(**record_changes), content=_CONTENT)
        )
    )
    reference_values = {
        "artifact_id": "artifact-exact",
        "version_id": "version-exact",
        "sha256": _SHA,
    }
    reference_values.update(reference_changes)
    reference = SimpleNamespace(**reference_values)

    with pytest.raises(ExactArtifactReadError, match="integrity"):
        await ExactArtifactReader(artifacts).read(
            _scope(), reference, expected_content_type="text/x-python"
        )


@pytest.mark.asyncio
async def test_exact_artifact_reader_stabilizes_download_failures() -> None:
    artifacts = SimpleNamespace(download_version=AsyncMock(side_effect=OSError("private path")))
    reference = SimpleNamespace(
        artifact_id="artifact-exact", version_id="version-exact", sha256=_SHA
    )

    with pytest.raises(ExactArtifactReadError, match="unavailable") as caught:
        await ExactArtifactReader(artifacts).read(
            _scope(), reference, expected_content_type="text/x-python"
        )
    assert "private path" not in str(caught.value)
