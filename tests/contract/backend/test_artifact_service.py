from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.errors import ConflictError, NotFoundError, ValidationError
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, CreateTaskRequest
from CAD2BIM_backend.infrastructure.memory import InMemoryControlPlaneRepository
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.ports.storage import StorageKey


@pytest.mark.asyncio
async def test_download_matches_record_to_final_verified_read(tmp_path, monkeypatch):
    key = StorageKey("owner-1", "task-1", "artifact-1", 1)
    storage = LocalStorage(tmp_path)

    async def chunks():
        yield b"new"

    stored = await storage.write(
        key, filename="result.json", content_type="application/json", chunks=chunks(),
    )
    old = replace(stored, sha256=hashlib.sha256(b"old").hexdigest())
    # A prior stat can describe the old object while the verified read sees its replacement.
    monkeypatch.setattr(storage, "stat", AsyncMock(return_value=old))
    record = ArtifactRecord(
        artifact_id=key.object_id, version_id="version-1", owner_id=key.owner_id,
        task_id=key.task_id, kind="input", filename=old.filename,
        content_type=old.content_type, version_no=1, size_bytes=old.size_bytes,
        sha256=old.sha256, idempotency_key="download-race", created_at=old.created_at,
    )
    repository = AsyncMock()
    repository.get_artifact.return_value = record
    with pytest.raises(ConflictError, match="metadata does not match"):
        await ArtifactService(repository, storage).download(key.owner_id, key.object_id)


class _RaceState:
    def __init__(self) -> None:
        self.failing_register_started = asyncio.Event()
        self.successful_register_started = asyncio.Event()
        self.recovery_read_completed = asyncio.Event()
        self.artifact: ArtifactRecord | None = None


class _FailingRegistrar:
    def __init__(self, state: _RaceState) -> None:
        self._state = state

    async def get_task(self, _owner_id: str, _task_id: str):
        return object()

    async def register_artifact(self, *_args, **_kwargs):
        self._state.failing_register_started.set()
        await self._state.successful_register_started.wait()
        raise RuntimeError("database unavailable before commit")

    async def get_artifact(self, _owner_id: str, _artifact_id: str):
        assert self._state.artifact is None
        self._state.recovery_read_completed.set()
        return None


class _SuccessfulRegistrar:
    def __init__(self, state: _RaceState) -> None:
        self._state = state

    async def get_task(self, _owner_id: str, _task_id: str):
        return object()

    async def register_artifact(
        self,
        owner_id: str,
        task_id: str,
        *,
        artifact_id: str,
        version_no: int,
        filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        idempotency_key: str,
        kind: str = "input",
        run_id: str | None = None,
    ) -> ArtifactRecord:
        self._state.successful_register_started.set()
        await self._state.recovery_read_completed.wait()
        self._state.artifact = ArtifactRecord(
            artifact_id=artifact_id,
            version_id="artifact-version-1",
            owner_id=owner_id,
            task_id=task_id,
            kind=kind,
            filename=filename,
            content_type=content_type,
            version_no=version_no,
            size_bytes=size_bytes,
            sha256=sha256,
            idempotency_key=idempotency_key,
            created_at=datetime.now(timezone.utc),
            run_id=run_id,
        )
        return self._state.artifact

    async def get_artifact(self, _owner_id: str, _artifact_id: str):
        return self._state.artifact


@pytest.mark.asyncio
async def test_failed_concurrent_registration_never_deletes_successful_payload(tmp_path):
    """A recovery read racing a later commit must leave immutable bytes intact."""

    state = _RaceState()
    failing = ArtifactService(_FailingRegistrar(state), LocalStorage(tmp_path))
    successful = ArtifactService(_SuccessfulRegistrar(state), LocalStorage(tmp_path))
    content = b"IFC-payload"
    request = {
        "owner_id": "owner-1",
        "task_id": "task-1",
        "filename": "network.ifc",
        "content_type": "application/x-ifc",
        "content": content,
        "idempotency_key": "upload-race-1",
    }

    failed_task = asyncio.create_task(failing.upload_bytes(**request))
    await state.failing_register_started.wait()
    successful_task = asyncio.create_task(successful.upload_bytes(**request))
    results = await asyncio.gather(
        failed_task,
        successful_task,
        return_exceptions=True,
    )

    assert len([item for item in results if isinstance(item, RuntimeError)]) == 1
    records = [item for item in results if isinstance(item, ArtifactRecord)]
    assert len(records) == 1
    artifact = records[0]
    assert artifact.sha256 == hashlib.sha256(content).hexdigest()

    storage = LocalStorage(tmp_path)
    key = StorageKey(
        artifact.owner_id,
        artifact.task_id,
        artifact.artifact_id,
        artifact.version_no,
    )
    assert await storage.read(key) == content


@pytest.mark.asyncio
async def test_missing_task_is_rejected_before_any_payload_is_written(tmp_path):
    class MissingTaskRepository:
        async def get_task(self, _owner_id: str, _task_id: str):
            return None

        async def register_artifact(self, *_args, **_kwargs):
            raise AssertionError("metadata registration must not start")

        async def get_artifact(self, _owner_id: str, _artifact_id: str):
            return None

    service = ArtifactService(MissingTaskRepository(), LocalStorage(tmp_path))

    with pytest.raises(NotFoundError, match="task not found"):
        await service.upload_bytes(
            owner_id="owner-1",
            task_id="missing-task",
            filename="network.ifc",
            content_type="application/x-ifc",
            content=b"IFC-payload",
            idempotency_key="missing-task-upload",
        )

    assert list(tmp_path.rglob("payload")) == []


@pytest.mark.asyncio
async def test_artifact_version_metadata_is_persisted_and_reloaded(tmp_path):
    repository = InMemoryControlPlaneRepository()
    task = await repository.create_task(
        "owner-1",
        CreateTaskRequest(
            title="Subject-bound payload",
            idempotency_key="task-subject-bound-payload",
        ),
    )
    service = ArtifactService(repository, LocalStorage(tmp_path))
    binding = {
        "binding_type": "mcp_tool_subject",
        "subject_refs": ["ifc_review_subject_" + "a" * 32],
        "subject_digest": "b" * 64,
    }

    created = await service.upload_bytes(
        owner_id="owner-1",
        task_id=task.task_id,
        filename="mcp-tool.json",
        content_type="application/json",
        content=b"{}",
        idempotency_key="mcp-tool-subject-bound",
        kind="mcp_tool_payload",
        version_metadata=binding,
    )
    reloaded = await service.get_metadata("owner-1", created.artifact_id)

    assert created.version_metadata == binding
    assert reloaded.version_metadata == binding


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "message"),
    (
        ({"unsupported": object()}, "canonical JSON"),
        ({"authorization": "Bearer must-not-persist"}, "sensitive field"),
        ({"nested": {"code_verifier": "must-not-persist"}}, "sensitive field"),
        ({"summary": "x" * 20_000}, "size limit"),
    ),
)
async def test_invalid_version_metadata_is_rejected_before_payload_write(
    tmp_path,
    metadata,
    message,
):
    repository = InMemoryControlPlaneRepository()
    task = await repository.create_task(
        "owner-1",
        CreateTaskRequest(
            title="Preflight metadata",
            idempotency_key=f"task-preflight-{message}",
        ),
    )
    service = ArtifactService(repository, LocalStorage(tmp_path))

    with pytest.raises(ValidationError, match=message):
        await service.upload_bytes(
            owner_id="owner-1",
            task_id=task.task_id,
            filename="payload.json",
            content_type="application/json",
            content=b"{}",
            idempotency_key=f"metadata-preflight-{message}",
            kind="mcp_tool_payload",
            version_metadata=metadata,
        )

    assert list(tmp_path.rglob("payload")) == []


@pytest.mark.asyncio
async def test_stream_version_reads_the_pinned_version_not_the_current_version(tmp_path):
    old_content = b"DXF-V1"
    new_content = b"DXF-V2"
    created_at = datetime.now(timezone.utc)

    def record(version_id: str, version_no: int, content: bytes) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id="artifact-1",
            version_id=version_id,
            owner_id="owner-1",
            task_id="task-1",
            kind="input",
            filename="gas.dxf",
            content_type="application/dxf",
            version_no=version_no,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            idempotency_key=f"upload-{version_no}",
            created_at=created_at,
        )

    old = record("version-1", 1, old_content)
    current = record("version-2", 2, new_content)

    class VersionRepository:
        async def get_artifact(self, _owner_id: str, _artifact_id: str):
            return current

        async def get_artifact_version(
            self,
            _owner_id: str,
            _artifact_id: str,
            version_id: str,
        ):
            return old if version_id == old.version_id else None

        async def get_artifact_version_number(
            self,
            _owner_id: str,
            _artifact_id: str,
            version_no: int,
        ):
            return old if version_no == old.version_no else None

    storage = LocalStorage(tmp_path)
    for artifact, content in ((old, old_content), (current, new_content)):

        async def chunks(value=content):
            yield value

        await storage.write(
            StorageKey(
                artifact.owner_id,
                artifact.task_id,
                artifact.artifact_id,
                artifact.version_no,
            ),
            filename=artifact.filename,
            content_type=artifact.content_type,
            chunks=chunks(),
        )

    stream = await ArtifactService(VersionRepository(), storage).stream_version(
        "owner-1",
        "artifact-1",
        "version-1",
    )
    content = b"".join([chunk async for chunk in stream.chunks])
    exact_download = await ArtifactService(
        VersionRepository(),
        storage,
    ).download_version_by_number(
        "owner-1",
        "artifact-1",
        1,
    )
    exact_download_by_id = await ArtifactService(
        VersionRepository(),
        storage,
    ).download_version(
        "owner-1",
        "artifact-1",
        "version-1",
    )

    assert stream.artifact.version_id == "version-1"
    assert content == old_content
    assert exact_download.artifact.version_id == "version-1"
    assert exact_download.content == old_content
    assert exact_download_by_id.artifact.version_id == "version-1"
    assert exact_download_by_id.content == old_content


@pytest.mark.asyncio
async def test_exact_download_rejects_repository_metadata_that_drifted_from_storage(
    tmp_path,
):
    content = b"immutable-stage-context"
    stored_digest = hashlib.sha256(content).hexdigest()
    created_at = datetime.now(timezone.utc)
    drifted = ArtifactRecord(
        artifact_id="artifact-drifted",
        version_id="version-drifted",
        owner_id="owner-1",
        task_id="task-1",
        kind="stage_context_card_part",
        filename="outputs.json",
        content_type="application/json",
        version_no=1,
        size_bytes=len(content),
        sha256="0" * 64,
        idempotency_key="drifted-artifact",
        created_at=created_at,
        run_id="run-1",
    )

    class DriftedRepository:
        async def get_artifact_version_number(
            self,
            _owner_id: str,
            _artifact_id: str,
            _version_no: int,
        ):
            return drifted

    storage = LocalStorage(tmp_path)

    async def chunks():
        yield content

    stored = await storage.write(
        StorageKey(
            drifted.owner_id,
            drifted.task_id,
            drifted.artifact_id,
            drifted.version_no,
        ),
        filename=drifted.filename,
        content_type=drifted.content_type,
        chunks=chunks(),
    )
    assert stored.sha256 == stored_digest

    with pytest.raises(
        ConflictError,
        match="artifact metadata does not match its stored payload",
    ):
        await ArtifactService(
            DriftedRepository(),  # type: ignore[arg-type]
            storage,
        ).download_version_by_number(
            drifted.owner_id,
            drifted.artifact_id,
            drifted.version_no,
        )
