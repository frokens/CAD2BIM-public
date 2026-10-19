from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.control_plane import ArtifactRecord
from CAD2BIM_backend.domain.run_input_manifest import (
    RunInputManifestError,
    RunInputManifest,
    compile_run_input_manifest,
)


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


def _artifact(
    artifact_id: str = "artifact-1",
    *,
    version_id: str = "version-1",
    filename: str = "gas.dxf",
    sha256: str = "a" * 64,
) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        version_id=version_id,
        owner_id="owner-1",
        task_id="task-1",
        kind="input",
        filename=filename,
        content_type="application/dxf",
        size_bytes=3,
        sha256=sha256,
        idempotency_key=f"upload-{artifact_id}",
        created_at=NOW,
    )


def _manifest(*artifacts: ArtifactRecord) -> RunInputManifest:
    return compile_run_input_manifest(
        tenant_id="tenant-1",
        task_id="task-1",
        run_id="run-1",
        bundle_id="cad2bim_workflow",
        artifacts=artifacts or (_artifact(),),
        created_at=NOW,
    )


def test_manifest_is_stable_and_binds_exact_artifact_version() -> None:
    first = _manifest(_artifact())
    second = _manifest(_artifact())

    assert first == second
    assert first.digest == second.digest
    assert first.entries[0].artifact_version_id == "version-1"
    assert first.entries[0].logical_handle.startswith("artifact-")
    assert first.entries[0].logical_handle.endswith(".dxf")
    assert first.require_entry(
        artifact_id="artifact-1",
        artifact_version_id="version-1",
        logical_handle=first.entries[0].logical_handle,
        sha256="a" * 64,
    ) == first.entries[0]


def test_manifest_uses_the_one_current_unversioned_contract() -> None:
    manifest = _manifest(_artifact())
    payload = manifest.model_dump(mode="python")

    assert manifest.__class__.__name__ == "RunInputManifest"
    assert manifest.entries[0].__class__.__name__ == "RunInputEntry"
    assert "schema_version" not in payload
    assert "version" not in payload
    assert "schema_version" not in payload["entries"][0]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        manifest.__class__.model_validate(
            {**payload, "schema_version": "cad2bim-run-input-manifest/v1"}
        )


def test_manifest_supports_empty_closure_and_rejects_duplicate_or_unsupported_inputs() -> None:
    empty = compile_run_input_manifest(
        tenant_id="tenant-1",
        task_id="task-1",
        run_id="run-1",
        bundle_id="cad2bim_workflow",
        artifacts=(),
        created_at=NOW,
    )
    assert empty.entries == ()
    with pytest.raises(ValidationError, match="artifact IDs must be unique"):
        _manifest(_artifact(), _artifact(version_id="version-2"))
    with pytest.raises(RunInputManifestError, match="unsupported"):
        _manifest(_artifact(filename="notes.txt"))


def test_manifest_rejects_digest_or_membership_drift() -> None:
    manifest = _manifest(_artifact())
    with pytest.raises(ValidationError, match="digest changed"):
        RunInputManifest.model_validate(
            {**manifest.model_dump(mode="python"), "digest": "0" * 64}
        )
    with pytest.raises(RunInputManifestError, match="not a member"):
        manifest.require_entry(
            artifact_id="artifact-1",
            artifact_version_id="version-2",
            logical_handle=manifest.entries[0].logical_handle,
            sha256="a" * 64,
        )
