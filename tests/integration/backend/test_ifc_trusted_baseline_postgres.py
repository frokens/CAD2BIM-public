"""Focused trusted-source admission on the existing durable IFC kernel."""

from pathlib import Path
import asyncio
import hashlib
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef, IfcTrustedBaselineLoadOperationDetail,
    IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY, CriticalOperationErrorPhase,
)
from CAD2BIM_backend.domain.ifc_model import (
    IfcModelConflictError, IfcModelIntegrityError, InitializeIfcModelCommand,
)
from CAD2BIM_backend.infrastructure.postgres.models import CriticalOperationModel
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.ports.storage import StorageKey
from tests.integration.backend.test_data_transform_entry_migration_postgres import _isolated_database
from tests.integration.backend.test_ifc_model_durable_kernel import _create_runtime, _open_direct_scope
from tests.process_support.ifc_revision_runtime import BytesIfcCandidate, build_in_process_ifc_stack


pytestmark = [pytest.mark.integration, pytest.mark.network]


@pytest.mark.asyncio
async def test_trusted_baseline_exact_source_replay_failure_and_commit_fence(tmp_path: Path, monkeypatch) -> None:
    # A fresh schema proves migration and Repo behavior without changing existing DBs/roles.
    with _isolated_database("trusted_baseline") as (config, engine):
        command.upgrade(config, "20261017_0111")
        command.upgrade(config, "20261017_0112")
        runtime = _create_runtime(engine.url.render_as_string(hide_password=False), tmp_path / "storage")
        try:
            context, scope = await _open_direct_scope(
                runtime, configured_subject=f"trusted-baseline-{uuid4().hex}", session_name="baseline owner",
            )
            other_context, other_scope = await _open_direct_scope(
                runtime, configured_subject=f"trusted-baseline-{uuid4().hex}", session_name="other baseline",
            )
            stack = build_in_process_ifc_stack(runtime.repository, runtime.storage)
            assets = ArtifactService(runtime.repository, runtime.storage)
            with bind_tenant_execution_context(context):
                await stack.model_service.initialize(scope, InitializeIfcModelCommand(project_name="Trusted baseline"))
                before = await runtime.repository.get_current(scope)
                content = await runtime.storage.read(StorageKey(scope.owner_id, scope.task_id,
                                                               before.artifact.artifact_id, 1))
                source = await assets.upload_bytes(owner_id=scope.owner_id, task_id=scope.task_id,
                    run_id=scope.run_id, filename="baseline.ifc", content_type="application/x-step",
                    content=content, kind="ifc_trusted_baseline_source")
            with bind_tenant_execution_context(other_context):
                foreign = await assets.upload_bytes(owner_id=other_scope.owner_id, task_id=other_scope.task_id,
                    run_id=other_scope.run_id, filename="baseline.ifc", content_type="application/x-step",
                    content=content, kind="ifc_trusted_baseline_source")

            def request(asset=source):
                return dict(tool_name="ifc_trusted_baseline_load", idempotency_key=f"baseline:{uuid4().hex}",
                    request_digest="b" * 64, intent_summary=IFC_TRUSTED_BASELINE_LOAD_INTENT_SUMMARY,
                    operation_detail=IfcTrustedBaselineLoadOperationDetail(source_asset=ArtifactVersionRef(
                        artifact_id=asset.artifact_id, version_id=asset.version_id,
                        sha256=asset.sha256)).persisted_payload())

            with bind_tenant_execution_context(context):
                with pytest.raises(IfcModelIntegrityError, match="exact asset"):
                    await runtime.repository.prepare_revision(scope, **request(foreign))
                wrong = request()
                wrong["operation_detail"]["source_asset"]["sha256"] = "c" * 64
                with pytest.raises(IfcModelIntegrityError, match="exact asset"):
                    await runtime.repository.prepare_revision(scope, **wrong)

                failed = await runtime.repository.prepare_revision(scope, **request())
                failed_claim = await runtime.repository.claim_execution(scope, operation_id=failed.operation_id,
                                                                        request_digest=failed.request_digest)
                altered = content.replace(b"Trusted baseline", b"Wrong baseline")
                assert altered != content
                with pytest.raises(IfcModelConflictError, match="registered trusted source"):
                    await stack.writer.commit_trusted_baseline(scope, failed, BytesIfcCandidate(
                        altered, sha256=hashlib.sha256(altered).hexdigest()),
                                                               claim=failed_claim)
                async with runtime.database.session_factory() as session:
                    rejected = await session.get(CriticalOperationModel, failed.operation_id)
                    assert rejected.ifc_candidate_json is None
                await runtime.repository.record_failed_ifc_operation(scope, operation_id=failed.operation_id,
                    error_code="IFC_VALIDATION_FAILED", error_message="invalid source fixture",
                    error_phase=CriticalOperationErrorPhase.IFC_VALIDATION, claim=failed_claim)
                assert await runtime.repository.get_current(scope) == before

                args = request()
                prepared = await runtime.repository.prepare_revision(scope, **args)
                contender = await runtime.repository.prepare_revision(scope, **request())
                claim = await runtime.repository.claim_execution(scope, operation_id=prepared.operation_id,
                                                                  request_digest=prepared.request_digest)
                loser_claim = await runtime.repository.claim_execution(scope, operation_id=contender.operation_id,
                                                                        request_digest=contender.request_digest)
                entered, release = asyncio.Event(), asyncio.Event()
                original_commit = runtime.repository.commit_revision

                async def hold_contender(*call_args, **kwargs):
                    if kwargs["prepared"].operation_id == contender.operation_id:
                        entered.set()
                        await release.wait()
                    return await original_commit(*call_args, **kwargs)

                monkeypatch.setattr(runtime.repository, "commit_revision", hold_contender)
                losing_write = asyncio.create_task(stack.writer.commit_trusted_baseline(
                    scope, contender, BytesIfcCandidate(content, sha256=source.sha256), claim=loser_claim))
                await asyncio.wait_for(entered.wait(), timeout=5)
                assert await runtime.repository.get_selected_candidate(scope, contender, loser_claim) is not None
                await stack.writer.commit_trusted_baseline(scope, prepared, BytesIfcCandidate(
                    content, sha256=source.sha256),
                                                                       claim=claim)
                current = await runtime.repository.get_current(scope)
                assert current.revision_no == 2
                replay = await runtime.repository.prepare_revision(scope, **args)
                assert replay.operation_id == prepared.operation_id
                assert replay.before.revision_no == 1
                changed_source = dict(args, operation_detail=request(foreign)["operation_detail"])
                with pytest.raises(IfcModelConflictError, match="request detail changed"):
                    await runtime.repository.prepare_revision(scope, **changed_source)
                assert (await runtime.repository.get_revision_operation_state(scope, **args)).status.value == "succeeded"
                with pytest.raises(IfcModelConflictError, match="revision 1"):
                    await runtime.repository.prepare_revision(scope, **request())
                release.set()
                with pytest.raises(IfcModelConflictError, match="CURRENT or write epoch changed"):
                    await losing_write
                assert await runtime.repository.get_current(scope) == current
                async with runtime.database.session_factory() as session:
                    for operation_id, expected_status in ((prepared.operation_id, "succeeded"),
                                                         (failed.operation_id, "failed"),
                                                         (contender.operation_id, "failed")):
                        model = await session.get(CriticalOperationModel, operation_id)
                        record = await runtime.repository._operation_record(session, model)
                        assert record.status.value == expected_status
                        assert record.detail.source_asset.artifact_id == source.artifact_id
                        assert record.detail.result == ("baseline_loaded" if expected_status == "succeeded" else None)
                    loser = await session.get(CriticalOperationModel, contender.operation_id)
                    assert loser.error_phase == "commit_fence"
                # The database also rejects an invented prepared result independently of DTO validation.
                with pytest.raises(IntegrityError):
                    async with runtime.database.session_factory() as session, session.begin():
                        await session.execute(text("UPDATE critical_operations SET operation_detail_json = "
                            "operation_detail_json || '{\"result\":\"baseline_loaded\"}'::jsonb WHERE operation_id=:id"),
                            {"id": failed.operation_id})
            with pytest.raises(Exception, match="Cannot discard trusted baseline"):
                command.downgrade(config, "20261017_0111")
        finally:
            await runtime.close()
