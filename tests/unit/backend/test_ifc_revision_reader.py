from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from tests.process_support.ifc_revision_runtime import InProcessIfcRevisionRuntime

from CAD2BIM_backend.application.ifc_revision_reader import IfcRevisionReader
from CAD2BIM_backend.application.ifc_revision_writer import (
    IfcRevisionWriter,
    VerifiedExactIfcRevision as WriterVerifiedExactIfcRevision,
)
from CAD2BIM_backend.domain.ifc_model import (
    ExactIfcArtifactVersion,
    ExactIfcRevision,
    IfcModelFrozenError,
    IfcModelIntegrityError,
    IfcModelNotInitializedError,
    IfcModelState,
    IfcRevisionInspection,
    IfcRunIdentity,
)
from CAD2BIM_backend.ports.ifc_revision_reader import (
    IfcRevisionReaderPort,
    VerifiedExactIfcRevision,
)
from CAD2BIM_backend.ports.storage import StorageKey, StoredObject, VerifiedStorageRead


_NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)
_GLOBAL_ID = "0" * 22


def _inspection(content: bytes) -> IfcRevisionInspection:
    return IfcRevisionInspection(
        schema_identifier="IFC4X3_ADD2",
        project_global_id=_GLOBAL_ID,
        project_name="Reader Test",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        site_count=0,
        element_count=0,
        unit_assignment_count=0,
        geometric_context_count=0,
        product_body_contexts=(),
        project_length_unit_m=None,
        geometry_basis_configured=False,
        geometry_basis=None,
    )


class _Codec:
    def inspect(self, content: bytes) -> IfcRevisionInspection:
        return _inspection(content)


class _Repository:
    def __init__(self, current: ExactIfcRevision | None) -> None:
        self.current = current
        self.exact: dict[str, ExactIfcRevision] = {}
        if current is not None:
            self.exact[current.revision_id] = current
        self.allowed_scopes: list[IfcRunIdentity] = []
        self.current_scopes: list[IfcRunIdentity] = []
        self.exact_requests: list[tuple[IfcRunIdentity, str]] = []

    async def require_invocation_allowed(self, scope: IfcRunIdentity) -> None:
        self.allowed_scopes.append(scope)

    async def get_current(self, scope: IfcRunIdentity) -> ExactIfcRevision | None:
        self.current_scopes.append(scope)
        return self.current

    async def get_exact(
        self,
        scope: IfcRunIdentity,
        *,
        revision_id: str,
    ) -> ExactIfcRevision | None:
        self.exact_requests.append((scope, revision_id))
        return self.exact.get(revision_id)


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[StorageKey, tuple[StoredObject, bytes]] = {}

    def put(self, exact: ExactIfcRevision, content: bytes) -> StorageKey:
        key = StorageKey(
            owner_id=exact.scope.owner_id,
            task_id=exact.scope.task_id,
            object_id=exact.artifact.artifact_id,
            version=1,
        )
        self.objects[key] = (
            StoredObject(
                key=key,
                filename=exact.artifact.filename,
                content_type=exact.artifact.content_type,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                created_at=_NOW,
            ),
            content,
        )
        return key

    async def stat(self, key: StorageKey) -> StoredObject:
        return self.objects[key][0]

    async def read(self, key: StorageKey) -> bytes:
        return self.objects[key][1]

    async def read_verified(self, key: StorageKey) -> VerifiedStorageRead:
        stored, content = self.objects[key]
        return VerifiedStorageRead(stored=stored, content=content,
            verified_sha256=hashlib.sha256(content).hexdigest())

    async def iter_chunks(self, key: StorageKey, *, chunk_size: int, expected=None):
        stored, content = self.objects[key]
        assert expected is None or expected == stored
        for offset in range(0, len(content), chunk_size):
            yield content[offset : offset + chunk_size]


def _fixture() -> tuple[IfcRunIdentity, ExactIfcRevision, bytes, _Repository, _Storage]:
    scope = IfcRunIdentity(
        owner_id="owner",
        tenant_id="tenant",
        task_id="task",
        run_id="run",
    )
    content = b"ISO-10303-21;reader"
    artifact = ExactIfcArtifactVersion(
        artifact_id="artifact_reader",
        version_id="av_reader",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    exact = ExactIfcRevision(
        scope=scope,
        model_id="ifcmodel_reader",
        model_state=IfcModelState.ACTIVE,
        revision_id="ifcrev_reader",
        revision_no=1,
        write_epoch=1,
        artifact=artifact,
        project_global_id=_GLOBAL_ID,
    )
    repository = _Repository(exact)
    storage = _Storage()
    storage.put(exact, content)
    return scope, exact, content, repository, storage


@pytest.mark.asyncio
async def test_reader_pins_current_once_and_reads_that_exact_revision() -> None:
    scope, exact, content, repository, storage = _fixture()
    reader: IfcRevisionReaderPort = IfcRevisionReader(repository, storage, InProcessIfcRevisionRuntime(_Codec()))

    pinned = await reader.pin_current(scope)
    repository.current = None
    verified = await reader.read_exact(pinned)

    assert pinned == exact
    assert verified == VerifiedExactIfcRevision(
        exact=exact,
        content=content,
        inspection=_inspection(content),
    )
    assert repository.allowed_scopes == [scope]
    assert repository.current_scopes == [scope]


@pytest.mark.asyncio
async def test_reader_pins_one_server_owned_exact_revision() -> None:
    scope, exact, _, repository, storage = _fixture()
    reader = IfcRevisionReader(repository, storage, InProcessIfcRevisionRuntime(_Codec()))

    pinned = await reader.pin_exact(scope, revision_id=exact.revision_id)

    assert pinned == exact
    assert repository.allowed_scopes == [scope]
    assert repository.exact_requests == [(scope, exact.revision_id)]


@pytest.mark.asyncio
async def test_reader_opens_repeatable_exact_stream_without_storage_read() -> None:
    _, exact, content, repository, storage = _fixture()
    reader = IfcRevisionReader(repository, storage, InProcessIfcRevisionRuntime(_Codec()))

    source = await reader.open_exact(exact)

    assert source.size_bytes == len(content)
    assert source.sha256 == hashlib.sha256(content).hexdigest()
    assert b"".join([chunk async for chunk in source.iter_chunks(chunk_size=5)]) == content
    assert b"".join([chunk async for chunk in source.iter_chunks(chunk_size=7)]) == content


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["current", "exact"])
async def test_reader_rejects_missing_revisions(method: str) -> None:
    scope, exact, _, repository, storage = _fixture()
    repository.current = None
    repository.exact.clear()
    reader = IfcRevisionReader(repository, storage, InProcessIfcRevisionRuntime(_Codec()))

    with pytest.raises(IfcModelNotInitializedError):
        if method == "current":
            await reader.pin_current(scope)
        else:
            await reader.pin_exact(scope, revision_id=exact.revision_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["current", "exact"])
async def test_reader_rejects_frozen_revisions(method: str) -> None:
    scope, exact, _, repository, storage = _fixture()
    frozen = exact.model_copy(update={"model_state": IfcModelState.FROZEN})
    repository.current = frozen
    repository.exact[frozen.revision_id] = frozen
    reader = IfcRevisionReader(repository, storage, InProcessIfcRevisionRuntime(_Codec()))

    with pytest.raises(IfcModelFrozenError, match="frozen pending reconciliation"):
        if method == "current":
            await reader.pin_current(scope)
        else:
            await reader.pin_exact(scope, revision_id=frozen.revision_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["current", "exact"])
async def test_reader_rejects_repository_scope_drift(method: str) -> None:
    scope, exact, _, repository, storage = _fixture()
    other_scope = scope.model_copy(update={"run_id": "other-run"})
    drifted = exact.model_copy(update={"scope": other_scope})
    repository.current = drifted
    repository.exact[drifted.revision_id] = drifted
    reader = IfcRevisionReader(repository, storage, InProcessIfcRevisionRuntime(_Codec()))

    with pytest.raises(IfcModelIntegrityError, match="does not match the requested Run"):
        if method == "current":
            await reader.pin_current(scope)
        else:
            await reader.pin_exact(scope, revision_id=drifted.revision_id)


@pytest.mark.asyncio
async def test_reader_rejects_storage_metadata_drift() -> None:
    _, exact, _, repository, storage = _fixture()
    key = next(iter(storage.objects))
    stored, content = storage.objects[key]
    storage.objects[key] = (
        StoredObject(
            key=stored.key,
            filename=stored.filename,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256="f" * 64,
            created_at=stored.created_at,
        ),
        content,
    )
    reader = IfcRevisionReader(repository, storage, InProcessIfcRevisionRuntime(_Codec()))

    with pytest.raises(IfcModelIntegrityError, match="Storage metadata"):
        await reader.read_exact(exact)


@pytest.mark.asyncio
async def test_reader_rejects_payload_replacement_after_runtime_inspection() -> None:
    _, exact, _, repository, storage = _fixture()

    class ReplacingCodec(_Codec):
        def inspect(self, content):
            storage.put(exact, b"replacement")
            return super().inspect(content)

    reader = IfcRevisionReader(repository, storage, InProcessIfcRevisionRuntime(ReplacingCodec()))
    with pytest.raises(IfcModelIntegrityError, match="changed after supervised inspection"):
        await reader.read_exact(exact)


@pytest.mark.asyncio
async def test_writer_keeps_legacy_reader_api_and_result_type() -> None:
    scope, exact, content, repository, storage = _fixture()
    codec = _Codec()
    reader = IfcRevisionReader(repository, storage, InProcessIfcRevisionRuntime(codec))
    writer = IfcRevisionWriter(repository, storage, reader, object())  # type: ignore[arg-type]

    verified = await writer.read_current(scope)

    assert WriterVerifiedExactIfcRevision is VerifiedExactIfcRevision
    assert verified.exact == exact
    assert verified.content == content
