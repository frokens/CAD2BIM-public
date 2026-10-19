from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.application.ifc_model_service import IfcInvocationSupervisor
from CAD2BIM_backend.application.ifc_trusted_baseline_service import IfcTrustedBaselineService
from CAD2BIM_backend.domain.control_plane import ArtifactRecord
from CAD2BIM_backend.domain.data_runtime import ArtifactVersionRef
from CAD2BIM_backend.domain.ifc_model import (
    IfcCommittedVerificationError, IfcModelIntegrityError, IfcRunIdentity, IfcSettledOperationError,
)
from CAD2BIM_backend.domain.runtime_execution import IFC_REVISION_INVOCATION_EXECUTION
from CAD2BIM_backend.ports.ifc_revision_runtime import IfcRevisionRuntimeError


def _stack():
    events = []
    scope = IfcRunIdentity(owner_id="owner", tenant_id="tenant", task_id="task", run_id="run")
    content = b"registered immutable IFC source bytes"
    artifact = ArtifactRecord(
        owner_id=scope.owner_id, task_id=scope.task_id, run_id=scope.run_id,
        artifact_id="artifact_source", version_id="version_source", kind="ifc_trusted_baseline_source",
        filename="source.ifc", content_type="application/x-ifc", size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(), idempotency_key="registered-source",
        created_at=datetime(2026, 10, 17, tzinfo=timezone.utc),
    )
    reference = ArtifactVersionRef(artifact_id=artifact.artifact_id, version_id=artifact.version_id, sha256=artifact.sha256)
    prepared, claim = object(), object()

    async def metadata(*args, **kwargs):
        events.append("metadata")
        return artifact

    async def stream(*args):
        events.append("stream")

        async def chunks():
            yield content[:10]
            yield content[10:]

        return SimpleNamespace(artifact=artifact, chunks=chunks())

    artifacts = SimpleNamespace(get_version_metadata=AsyncMock(side_effect=metadata),
                                stream_version=AsyncMock(side_effect=stream))
    active = False

    @asynccontextmanager
    async def execution(actual_scope, actual_claim, policy):
        nonlocal active
        assert (actual_scope, actual_claim, policy) == (scope, claim, IFC_REVISION_INVOCATION_EXECUTION)
        active = True
        events.append("invocation-enter")
        try:
            yield
        finally:
            active = False
            events.append("claim-retired")

    async def commit(actual_scope, actual_prepared, source, *, claim):
        assert active and actual_scope == scope and actual_prepared is prepared
        events.append("commit")
        assert (source.size_bytes, source.sha256) == (artifact.size_bytes, artifact.sha256)
        for _ in range(2):
            assert b"".join([chunk async for chunk in source.iter_chunks()]) == content
        return SimpleNamespace(operation_alias="op_committed")

    writer = SimpleNamespace(
        recover_revision=AsyncMock(return_value=None), prepare=AsyncMock(return_value=prepared),
        claim_execution=AsyncMock(return_value=claim), resume_selected_revision=AsyncMock(return_value=None),
        execution_invocation=execution, commit_trusted_baseline=AsyncMock(side_effect=commit),
        fail_prepared=AsyncMock(return_value="op_failed"), freeze_indeterminate=AsyncMock(),
    )

    async def invoke(actual_scope, *, logical_operation_identity, operation, deadline):
        assert actual_scope == scope and logical_operation_identity.startswith("ifc_trusted_baseline_load:")
        return await operation()

    service = IfcTrustedBaselineService(writer, artifacts, SimpleNamespace(invoke=invoke),
                                       supervisor=IfcInvocationSupervisor(max_attempts=1))
    return SimpleNamespace(service=service, writer=writer, artifacts=artifacts, scope=scope,
                           reference=reference, artifact=artifact, events=events)


@pytest.mark.asyncio
async def test_original_success_recovery_never_prepares_or_reads_source():
    stack = _stack()
    stack.writer.recover_revision.return_value = SimpleNamespace(operation_alias="op_original")
    assert await stack.service.load(stack.scope, stack.reference, idempotency_key="baseline") == "op_original"
    stack.writer.prepare.assert_not_awaited()
    stack.writer.claim_execution.assert_not_awaited()
    stack.artifacts.get_version_metadata.assert_not_awaited()
    stack.artifacts.stream_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_recovery_never_reads_registered_source_again():
    stack = _stack()
    stack.writer.resume_selected_revision.return_value = (SimpleNamespace(operation_alias="op_selected"), object())
    assert await stack.service.load(stack.scope, stack.reference, idempotency_key="baseline") == "op_selected"
    stack.artifacts.get_version_metadata.assert_not_awaited()
    stack.artifacts.stream_version.assert_not_awaited()
    stack.writer.commit_trusted_baseline.assert_not_awaited()
    assert stack.events == ["invocation-enter", "claim-retired"]


@pytest.mark.asyncio
async def test_new_baseline_uses_exact_registered_asset_inside_existing_invocation():
    stack = _stack()
    assert await stack.service.load(stack.scope, stack.reference, idempotency_key="baseline") == "op_committed"
    stack.artifacts.get_version_metadata.assert_awaited_once_with(
        stack.scope.owner_id, stack.reference.artifact_id, stack.reference.version_id, run_id=stack.scope.run_id,
    )
    assert all(call.args == (stack.scope.owner_id, stack.reference.artifact_id, stack.reference.version_id)
               for call in stack.artifacts.stream_version.await_args_list)
    assert stack.artifacts.stream_version.await_count == 2
    assert stack.events == ["invocation-enter", "metadata", "commit", "stream", "stream", "claim-retired"]
    prepared_request = stack.writer.prepare.call_args.kwargs
    assert prepared_request["operation_detail"]["source_asset"] == stack.reference.model_dump()
    stack.writer.fail_prepared.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", (
    IfcModelIntegrityError("candidate IFC changed the IfcProject GlobalId"),
    IfcRevisionRuntimeError("IFC_REVISION_SOURCE_INVALID", "invalid IFC source", phase="input_validation", execution_started=False),
))
async def test_unselected_deterministic_source_failure_records_original_failure(failure):
    stack = _stack()
    stack.writer.commit_trusted_baseline.side_effect = failure
    with pytest.raises(IfcSettledOperationError) as caught:
        await stack.service.load(stack.scope, stack.reference, idempotency_key="baseline")
    assert caught.value.operation_alias == "op_failed"
    assert caught.value.code == "IFC_TRUSTED_BASELINE_INVALID"
    stack.writer.fail_prepared.assert_awaited_once()
    assert stack.writer.fail_prepared.call_args.kwargs["error_message"] == str(failure)
    stack.writer.freeze_indeterminate.assert_not_awaited()
    assert stack.events.count("claim-retired") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ("commit", "original", "selected"))
async def test_committed_verification_failure_is_preserved_without_retry_or_failure_rewrite(origin):
    stack = _stack()
    failure = IfcCommittedVerificationError("committed exact cannot be read",
                                            operation_alias="op_committed", revision_id="revision_after")
    if origin == "commit":
        stack.writer.commit_trusted_baseline.side_effect = failure
    elif origin == "original":
        stack.writer.recover_revision.side_effect = failure
    else:
        stack.writer.resume_selected_revision.side_effect = failure
    with pytest.raises(IfcCommittedVerificationError) as caught:
        await stack.service.load(stack.scope, stack.reference, idempotency_key="baseline")
    assert caught.value is failure
    stack.writer.fail_prepared.assert_not_awaited()
    stack.writer.freeze_indeterminate.assert_not_awaited()
    stack.writer.recover_revision.assert_awaited_once()
    assert stack.writer.commit_trusted_baseline.await_count == (1 if origin == "commit" else 0)
    assert stack.events.count("claim-retired") == (0 if origin == "original" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_state", ("committed", "selected"))
async def test_lost_committed_or_selected_response_is_not_recorded_as_failed(durable_state):
    stack = _stack()
    stack.writer.commit_trusted_baseline.side_effect = IfcModelIntegrityError("lost verification response")
    if durable_state == "committed":
        stack.writer.recover_revision.side_effect = [None, SimpleNamespace(operation_alias="op_original")]
    else:
        stack.writer.resume_selected_revision.side_effect = [None, (SimpleNamespace(operation_alias="op_original"), object())]
    assert await stack.service.load(stack.scope, stack.reference, idempotency_key="baseline") == "op_original"
    stack.writer.fail_prepared.assert_not_awaited()
    stack.writer.freeze_indeterminate.assert_not_awaited()
    assert stack.events.count("claim-retired") == 1
