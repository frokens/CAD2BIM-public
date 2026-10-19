from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.control_plane import OpenInterruptRequest, UserGateResumePlan
from CAD2BIM_backend.domain.safe_point_publications import (
    MAX_CHECKPOINT_MAP_ENTRIES,
    checkpoint_cursor_digest,
)
from CAD2BIM_backend.domain.system_interrupts import (
    InterruptResumeEnvelope,
    SystemInterruptEnvelope,
    SystemInterruptKind,
    SystemInterruptRecoveryBinding,
    SystemInterruptRecoveryPolicy,
    SystemInterruptResponseReceipt,
    SystemInterruptRuntimeEnvelope,
    SystemInterruptSource,
    SystemInterruptTargetRef,
    SystemInterrupt,
    SystemInterruptDecision,
    interrupt_envelope_payload,
    parse_interrupt_envelope,
)


NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def _cursor(checkpoint_id: str = "cp-1") -> CheckpointCursor:
    checkpoint_map = (("", checkpoint_id),)
    return CheckpointCursor(
        thread_id="thread-1",
        checkpoint_id=checkpoint_id,
        root_checkpoint_id=checkpoint_id,
        checkpoint_map=checkpoint_map,
        checkpoint_digest=checkpoint_cursor_digest(
            checkpoint_id=checkpoint_id,
            checkpoint_ns="",
            checkpoint_map=checkpoint_map,
        ),
    )


def _interrupt(kind: SystemInterruptKind) -> SystemInterrupt:
    return SystemInterrupt(
        interrupt_id="interrupt-1",
        kind=kind,
        reason_code="test.boundary",
        source=SystemInterruptSource(
            module="ifc",
            graph="ifc_generation_graph",
            stage_key="ifc_generation",
            node="global_plan_confirm",
            agent="global_planner",
            attempt=1,
        ),
        question="Continue?",
        context_summary="A bounded decision is required.",
        allowed_actions=() if kind is SystemInterruptKind.USER_INPUT_REQUIRED else ("approve", "cancel"),
        checkpoint_cursor=_cursor(),
        recovery_policy=SystemInterruptRecoveryPolicy(mode="resume_exact_checkpoint"),
        created_at=NOW,
    )


def test_public_kind_allowlist_contains_current_kinds() -> None:
    kinds = {item.value for item in SystemInterruptKind}
    assert kinds == {
        "clarification_required",
        "approval_required",
        "policy_decision_required",
        "retryable_failure",
        "review_fix_exhausted",
        "resource_missing",
        "feedback_preempted",
        "user_input_required",
        "run_paused",
    }
    assert "internal_safe_point" not in kinds


def test_only_input_waiting_accepts_empty_actions() -> None:
    waiting = _interrupt(SystemInterruptKind.USER_INPUT_REQUIRED)
    assert waiting.allowed_actions == ()
    with pytest.raises(ValidationError, match="does not offer"):
        SystemInterrupt.model_validate({**waiting.model_dump(), "allowed_actions": ("approve",)})
    for kind in SystemInterruptKind:
        if kind is not SystemInterruptKind.USER_INPUT_REQUIRED:
            with pytest.raises(ValidationError, match="require an allowed action"):
                SystemInterrupt.model_validate({**waiting.model_dump(), "kind": kind})


def test_exact_resume_can_reference_all_feedback_members_above_former_batch_limit() -> None:
    selected = tuple(f"feedback-{index}" for index in range(101))
    envelope = InterruptResumeEnvelope(
        decision=SystemInterruptDecision(action="revise"), checkpoint_cursor=_cursor(),
        response_high_watermark=101, expected_writer_generation=1,
        feedback_batch_id="batch-1", selected_feedback_ids=selected,
    )
    assert envelope.selected_feedback_ids == selected


def test_exact_cursor_rejects_branch_or_digest_mismatch() -> None:
    with pytest.raises(ValidationError, match="branch root"):
        CheckpointCursor(
            thread_id="thread-1",
            checkpoint_id="cp-child",
            checkpoint_ns="child",
            root_checkpoint_id="cp-root",
            checkpoint_map=(("", "wrong-root"), ("child", "cp-child")),
            checkpoint_digest="0" * 64,
        )

    with pytest.raises(ValidationError, match="size limit"):
        CheckpointCursor(
            thread_id="thread-1",
            checkpoint_id="cp-child",
            checkpoint_ns="child",
            root_checkpoint_id="cp-root",
            checkpoint_map=(
                ("", "cp-root"),
                ("child", "cp-child"),
                ("x" * 1_025, "cp-extra"),
            ),
            checkpoint_digest="0" * 64,
        )

    cursor = _cursor()
    with pytest.raises(ValidationError, match="digest"):
        CheckpointCursor(
            **{
                **cursor.model_dump(),
                "checkpoint_digest": "0" * 64,
            }
        )


def test_exact_cursor_accepts_topology_budget_and_rejects_one_entry_over_cap() -> None:
    checkpoint_map = (("", "cp-root"),) + tuple(
        (f"stage_{index}:task-{index}", f"cp-{index}")
        for index in range(1, MAX_CHECKPOINT_MAP_ENTRIES)
    )
    namespace, checkpoint_id = checkpoint_map[-1]
    cursor = CheckpointCursor(
        thread_id="thread-topology-budget",
        checkpoint_id=checkpoint_id,
        checkpoint_ns=namespace,
        root_checkpoint_id="cp-root",
        checkpoint_map=checkpoint_map,
        checkpoint_digest=checkpoint_cursor_digest(
            checkpoint_id=checkpoint_id,
            checkpoint_ns=namespace,
            checkpoint_map=checkpoint_map,
        ),
    )
    assert len(cursor.checkpoint_map) == MAX_CHECKPOINT_MAP_ENTRIES

    over_cap_map = (
        *checkpoint_map,
        ("stage_over:task-over", "cp-over"),
    )
    with pytest.raises(ValidationError, match="at most 64 items"):
        CheckpointCursor(
            thread_id="thread-topology-budget",
            checkpoint_id="cp-over",
            checkpoint_ns="stage_over:task-over",
            root_checkpoint_id="cp-root",
            checkpoint_map=over_cap_map,
            checkpoint_digest=checkpoint_cursor_digest(
                checkpoint_id="cp-over",
                checkpoint_ns="stage_over:task-over",
                checkpoint_map=over_cap_map,
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("checkpoint_id", "/Users/alice/private/.env"),
        ("root_checkpoint_id", r"C:\\private\\checkpoint"),
        ("thread_id", "DEEPSEEK_API_KEY=secret"),
        ("checkpoint_ns", "authorization:Bearer secret"),
        ("checkpoint_ns", "child\nnamespace"),
    ),
)
def test_exact_cursor_rejects_paths_credentials_and_control_text(
    field: str,
    value: str,
) -> None:
    data = _cursor().model_dump()
    data[field] = value
    if field == "checkpoint_id":
        data["root_checkpoint_id"] = value
        data["checkpoint_map"] = (("", value),)
    elif field == "root_checkpoint_id":
        data["checkpoint_id"] = value
        data["checkpoint_map"] = (("", value),)
    with pytest.raises(ValidationError, match="stable non-sensitive|namespace"):
        CheckpointCursor.model_validate(data)


def test_public_and_private_envelope_must_name_the_same_exact_cursor() -> None:
    public = _interrupt(SystemInterruptKind.APPROVAL_REQUIRED)
    with pytest.raises(ValidationError, match="cursors must match"):
        SystemInterruptEnvelope(
            system_interrupt=public,
            runtime=SystemInterruptRuntimeEnvelope(
                graph_interrupt_id="graph-int-1",
                checkpoint_cursor=_cursor("cp-2"),
            ),
        )


def test_gate_private_binding_is_paired_and_uses_saved_pause_root() -> None:
    cursor = _cursor("cp-1")
    with pytest.raises(ValidationError, match="bound together"):
        SystemInterruptRuntimeEnvelope(
            graph_interrupt_id="graph-int-1",
            checkpoint_cursor=cursor,
            business_entry_id="entry-1",
        )
    with pytest.raises(ValidationError, match="contradicts"):
        SystemInterruptRuntimeEnvelope(
            graph_interrupt_id="graph-int-1",
            checkpoint_cursor=cursor,
            business_entry_id="entry-1",
            accepted_root_checkpoint_id="old-root",
        )
    envelope = SystemInterruptRuntimeEnvelope(
        graph_interrupt_id="graph-int-1",
        checkpoint_cursor=cursor,
        business_entry_id="entry-1",
        accepted_root_checkpoint_id=cursor.root_checkpoint_id,
    )
    assert envelope.require_gate_binding() == ("entry-1", cursor.root_checkpoint_id)
    with pytest.raises(ValueError, match="Gate resume requires"):
        SystemInterruptRuntimeEnvelope(
            graph_interrupt_id="technical-1", checkpoint_cursor=cursor
        ).require_gate_binding()


def test_gate_open_rejects_a_technical_interrupt_with_gate_binding() -> None:
    cursor = _cursor()
    runtime = SystemInterruptRuntimeEnvelope(
        graph_interrupt_id="graph-int-1",
        checkpoint_cursor=cursor,
        business_entry_id="entry-1",
        accepted_root_checkpoint_id=cursor.root_checkpoint_id,
    )
    with pytest.raises(ValidationError, match="Gate source"):
        OpenInterruptRequest(
            system_interrupt=_interrupt(SystemInterruptKind.APPROVAL_REQUIRED),
            runtime_envelope=runtime,
            idempotency_key="gate-1",
        )


@pytest.mark.parametrize(
    ("action", "message"),
    (("approve", "alternative text"), ("continue", None)),
)
def test_gate_resume_rejects_non_gate_action_or_alternative_text(
    action: str, message: str | None
) -> None:
    cursor = _cursor()
    with pytest.raises(ValidationError, match="Gate response"):
        UserGateResumePlan(
            interrupt_id="interrupt-1",
            source_checkpoint_cursor=cursor,
            operation_id="operation-1",
            business_entry_id="entry-1",
            accepted_root_checkpoint_id=cursor.root_checkpoint_id,
            response=InterruptResumeEnvelope(
                decision=SystemInterruptDecision(action=action, message=message),
                checkpoint_cursor=cursor,
                response_high_watermark=0,
                expected_writer_generation=1,
            ),
        )


def test_interrupt_envelope_uses_only_the_current_unversioned_shape() -> None:
    public = _interrupt(SystemInterruptKind.APPROVAL_REQUIRED)
    runtime = SystemInterruptRuntimeEnvelope(
        graph_interrupt_id="graph-int-1",
        checkpoint_cursor=public.checkpoint_cursor,
    )
    payload = interrupt_envelope_payload(public, runtime)
    assert "schema_version" not in payload
    assert "schema_version" not in payload["system_interrupt"]
    assert "schema_version" not in payload["runtime"]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_interrupt_envelope({**payload, "schema_version": "1"})


def test_restore_policy_requires_one_private_pre_stage_binding() -> None:
    public = _interrupt(SystemInterruptKind.RETRYABLE_FAILURE).model_copy(
        update={
            "source": _interrupt(SystemInterruptKind.RETRYABLE_FAILURE).source.model_copy(
                update={"stage_key": "ifc_product_stage_001"}
            ),
            "recovery_policy": SystemInterruptRecoveryPolicy(
                mode="restore_pre_stage_then_resume",
                retry_node="modeler",
                requires_mcp_restore=True,
            )
        }
    )
    runtime = SystemInterruptRuntimeEnvelope(
        graph_interrupt_id="graph-int-1",
        checkpoint_cursor=public.checkpoint_cursor,
    )

    with pytest.raises(ValidationError, match="recovery binding"):
        SystemInterruptEnvelope(system_interrupt=public, runtime=runtime)

    binding = SystemInterruptRecoveryBinding(
        savepoint_id="savepoint-1",
        stage_key="ifc_product_stage_001",
        mcp_session_id="session-1",
        snapshot_id="snapshot-1",
        artifact_id="artifact-1",
        artifact_version_id="version-1",
    )
    envelope = SystemInterruptEnvelope(
        system_interrupt=public,
        runtime=runtime.model_copy(update={"recovery_binding": binding}),
    )
    assert envelope.runtime.recovery_binding == binding

    exact_public = _interrupt(SystemInterruptKind.APPROVAL_REQUIRED)
    with pytest.raises(ValidationError, match="must not carry"):
        SystemInterruptEnvelope(
            system_interrupt=exact_public,
            runtime=SystemInterruptRuntimeEnvelope(
                graph_interrupt_id="graph-int-2",
                checkpoint_cursor=exact_public.checkpoint_cursor,
                recovery_binding=binding,
            ),
        )


def test_interrupt_rejects_unknown_action_and_naive_timestamp() -> None:
    public = _interrupt(SystemInterruptKind.APPROVAL_REQUIRED)
    with pytest.raises(ValidationError, match="unsupported action"):
        SystemInterrupt(
            **{
                **public.model_dump(),
                "allowed_actions": ("approve", "execute_arbitrary_code"),
            }
        )
    with pytest.raises(ValidationError, match="unsupported interrupt action"):
        SystemInterruptResponseReceipt(
            interrupt_id="interrupt-1",
            action="execute_arbitrary_code",
            responded_at=NOW,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        SystemInterrupt(
            **{
                **public.model_dump(),
                "created_at": datetime(2026, 7, 12),
            }
        )


@pytest.mark.parametrize(
    "ref_id",
    (
        "/Users/alice/private/model.ifc",
        r"C:\\private\\model.ifc",
        "DEEPSEEK_API_KEY=secret",
        "arbitrary-unscoped-id",
    ),
)
def test_public_target_ref_rejects_paths_credentials_and_unscoped_ids(ref_id: str) -> None:
    with pytest.raises(ValidationError, match="stable public domain identifier"):
        SystemInterruptTargetRef(kind="check", ref_id=ref_id)


def test_public_source_recovery_and_summary_reject_runtime_secrets() -> None:
    with pytest.raises(ValidationError, match="stable public identifiers"):
        SystemInterruptSource(
            module="ifc",
            graph="ifc_generation_graph",
            stage_key="/Users/alice/private/stage",
            node="user_interrupt",
        )
    with pytest.raises(ValidationError, match="stable public identifier"):
        SystemInterruptRecoveryPolicy(
            mode="resume_exact_checkpoint",
            retry_node=r"C:\\private\\retry",
        )
    public = _interrupt(SystemInterruptKind.APPROVAL_REQUIRED)
    with pytest.raises(ValidationError, match="sensitive runtime detail"):
        SystemInterrupt(
            **{
                **public.model_dump(),
                "context_summary": '{"api_key":"sk-secret"}',
            }
        )


@pytest.mark.parametrize(
    "summary",
    (
        "path=/Users/alice/private/model.ifc",
        "See (/home/alice/private/model.ifc) before continuing.",
        r"Workspace C:\\private\\model.ifc is unavailable.",
        r"Workspace \\server\\share\\model.ifc is unavailable.",
    ),
)
def test_public_interrupt_text_rejects_embedded_physical_paths(summary: str) -> None:
    public = _interrupt(SystemInterruptKind.APPROVAL_REQUIRED)
    with pytest.raises(ValidationError, match="sensitive runtime detail"):
        SystemInterrupt(
            **{
                **public.model_dump(),
                "context_summary": summary,
            }
        )
