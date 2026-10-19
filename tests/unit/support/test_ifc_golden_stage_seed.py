from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from tests.process_support.ifc_golden_seed.closure import (
    ArtifactReachability,
    SeedClosure,
)
from tests.process_support.ifc_golden_seed.core import (
    SeedValidationError,
    load_manifest,
    seal_manifest,
    transition_seed_state,
    verify_seed,
    write_manifest_exclusive,
)
from tests.process_support.ifc_golden_seed.exporter import (
    SeedExportError,
    publish_seed_record,
)
from tests.process_support.ifc_golden_seed.stage_seed import (
    StageAgentRunEvidence,
    StageCandidateEvidence,
    StagePromotionApproval,
    StageSeedError,
    StageVerificationProof,
    build_stage_candidate_manifest,
    promote_verified_stage,
    verify_stage_candidate,
)


def _digest(character: str) -> str:
    return character * 64


def _base_manifest() -> dict[str, object]:
    return seal_manifest(
        {
            "format_version": "ifc-golden-seed/v1",
            "seed_kind": "ifc_stage",
            "state": "candidate",
            "source_commit": "0123456789abcdef0123456789abcdef01234567",
            "parent_seed_sha256": _digest("a"),
            "m4_run_seed_sha256": _digest("b"),
            "object_inventory": [],
        }
    )


def _closure() -> SeedClosure:
    return SeedClosure(
        catalog_frontier=4,
        operation_frontier=2,
        catalog_entries=(),
        packages=(),
        package_members=(),
        operations=(),
        operation_inputs=(),
        artifacts=(
            {
                "artifact_id": "artifact-ifc",
                "current_version_id": "artifact-version-2",
            },
        ),
        artifact_versions=(
            {
                "artifact_id": "artifact-ifc",
                "version_id": "artifact-version-2",
                "sha256": _digest("c"),
            },
        ),
        ifc_models=(
            {
                "ifc_model_id": "ifc-model-1",
                "current_revision_id": "ifc-revision-2",
                "write_epoch": 2,
                "status": "active",
            },
        ),
        ifc_revisions=(
            {
                "ifc_model_id": "ifc-model-1",
                "revision_id": "ifc-revision-2",
                "revision_no": 2,
                "artifact_id": "artifact-ifc",
                "artifact_version_id": "artifact-version-2",
            },
        ),
        artifact_reachability=(
            ArtifactReachability(
                artifact_id="artifact-ifc",
                version_id="artifact-version-2",
                reverse_references=(
                    "artifact:artifact-ifc:current",
                    "ifc_revision:ifc-revision-2",
                ),
            ),
        ),
    )


def _runs() -> tuple[StageAgentRunEvidence, ...]:
    return (
        StageAgentRunEvidence(
            run_key="foundation-goal-1",
            mode="goal_only_semiblind",
            parent_seed_sha256=_digest("a"),
            event_manifest_sha256=_digest("1"),
        ),
        StageAgentRunEvidence(
            run_key="foundation-goal-2",
            mode="goal_only_semiblind",
            parent_seed_sha256=_digest("a"),
            event_manifest_sha256=_digest("2"),
        ),
        StageAgentRunEvidence(
            run_key="foundation-diagnostic",
            mode="diagnostic_guided",
            parent_seed_sha256=_digest("a"),
            event_manifest_sha256=_digest("3"),
        ),
    )


def _evidence(*, clean: bool = True) -> StageCandidateEvidence:
    return StageCandidateEvidence(
        stage_key="foundation",
        provider_schema_sha256=_digest("4"),
        tool_descriptions_sha256=_digest("5"),
        system_prompt_sha256=_digest("6"),
        stage_goal_sha256=_digest("7"),
        enabled_tools_sha256=_digest("8"),
        model="gpt-5.6-sol",
        reasoning_effort="high",
        budget_sha256=_digest("9"),
        oracle_version="foundation-oracle/v1",
        source_tree_clean=clean,
        source_diff_sha256=hashlib.sha256(b"").hexdigest() if clean else _digest("d"),
        agent_runs=_runs(),
    )


def _candidate(*, clean: bool = True) -> dict[str, object]:
    return build_stage_candidate_manifest(
        base_manifest=_base_manifest(), closure=_closure(), evidence=_evidence(clean=clean)
    )


def _verification() -> StageVerificationProof:
    return StageVerificationProof(
        oracle_result_sha256=_digest("e"),
        parent_regression_sha256=_digest("f"),
        goal_only_run_sha256s=(_digest("1"), _digest("2")),
    )


def test_candidate_binds_exact_db_current_and_closed_experiment_contract() -> None:
    candidate = _candidate()

    assert candidate["source_commit"] == "0123456789abcdef0123456789abcdef01234567"
    assert candidate["parent_seed_sha256"] == _digest("a")
    assert candidate["m4_run_seed_sha256"] == _digest("b")
    evidence = candidate["stage_evidence"]
    assert evidence == {
        "visibility": "server_only",
        "stage_key": "foundation",
        "current_ifc_model_id": "ifc-model-1",
        "current_ifc_revision_id": "ifc-revision-2",
        "current_ifc_revision_no": 2,
        "current_ifc_artifact_id": "artifact-ifc",
        "current_ifc_artifact_version_id": "artifact-version-2",
        "current_ifc_sha256": _digest("c"),
        "provider_schema_sha256": _digest("4"),
        "tool_descriptions_sha256": _digest("5"),
        "system_prompt_sha256": _digest("6"),
        "stage_goal_sha256": _digest("7"),
        "enabled_tools_sha256": _digest("8"),
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "budget_sha256": _digest("9"),
        "oracle_version": "foundation-oracle/v1",
        "source_tree_clean": True,
        "source_diff_sha256": hashlib.sha256(b"").hexdigest(),
        "agent_runs": [run.manifest_value() for run in _runs()],
    }
    assert "prompt" not in evidence
    assert "provider_schema" not in evidence
    assert candidate["root_sha256"] != _base_manifest()["root_sha256"]


def test_stage_evidence_schema_is_recursively_closed() -> None:
    candidate = _candidate()
    evidence = dict(candidate["stage_evidence"])

    with pytest.raises(SeedValidationError, match="unsupported fields"):
        seal_manifest({**candidate, "stage_evidence": {**evidence, "oracle_answer": "leak"}})

    runs = list(evidence["agent_runs"])
    with pytest.raises(SeedValidationError, match="unsupported fields"):
        seal_manifest(
            {
                **candidate,
                "stage_evidence": {
                    **evidence,
                    "agent_runs": [{**runs[0], "previous_agent_trace": _digest("0")}, *runs[1:]],
                },
            }
        )


@pytest.mark.parametrize(
    "closure",
    [
        replace(_closure(), ifc_models=()),
        replace(
            _closure(),
            ifc_models=({**_closure().ifc_models[0], "current_revision_id": "missing"},),
        ),
        replace(
            _closure(),
            artifacts=({**_closure().artifacts[0], "current_version_id": "stale"},),
        ),
        replace(_closure(), artifact_reachability=()),
    ],
    ids=["no-model", "missing-current", "artifact-not-current", "outside-closure"],
)
def test_candidate_rejects_non_exact_or_unreachable_db_current(closure: SeedClosure) -> None:
    with pytest.raises(StageSeedError, match="Stage closure|CURRENT|closure"):
        build_stage_candidate_manifest(
            base_manifest=_base_manifest(), closure=closure, evidence=_evidence()
        )


def test_candidate_rejects_tampered_base_and_agent_from_another_parent() -> None:
    base = _base_manifest()
    with pytest.raises(StageSeedError, match="root SHA-256"):
        build_stage_candidate_manifest(
            base_manifest={**base, "source_commit": "tampered"},
            closure=_closure(),
            evidence=_evidence(),
        )

    foreign_run = replace(_runs()[0], parent_seed_sha256=_digest("0"))
    with pytest.raises(StageSeedError, match="parent Seed"):
        build_stage_candidate_manifest(
            base_manifest=base,
            closure=_closure(),
            evidence=replace(_evidence(), agent_runs=(foreign_run, *_runs()[1:])),
        )


def test_candidate_dataclasses_reject_unfixed_or_ambiguous_inputs() -> None:
    with pytest.raises(StageSeedError, match="empty diff"):
        replace(_evidence(), source_diff_sha256=_digest("d"))
    with pytest.raises(StageSeedError, match="event manifests"):
        replace(
            _evidence(),
            agent_runs=(_runs()[0], replace(_runs()[1], event_manifest_sha256=_digest("1"))),
        )
    with pytest.raises(StageSeedError, match="StageAgentRunEvidence"):
        replace(_evidence(), agent_runs=({"run_key": "unclosed"},))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StageAgentRunEvidence(  # type: ignore[call-arg]
            run_key="run",
            mode="goal_only_semiblind",
            parent_seed_sha256=_digest("a"),
            event_manifest_sha256=_digest("1"),
            prompt="must-not-enter-evidence",
        )


def test_verified_binds_oracle_parent_regression_and_two_goal_only_runs() -> None:
    candidate = _candidate()
    verified = verify_stage_candidate(candidate, _verification())

    assert verified["state"] == "verified"
    assert verified["previous_record_sha256"] == candidate["root_sha256"]
    assert verified["verification_proof"] == _verification().manifest_value()

    diagnostic_proof = replace(
        _verification(), goal_only_run_sha256s=(_digest("1"), _digest("3"))
    )
    with pytest.raises(StageSeedError, match="goal-only parent runs"):
        verify_stage_candidate(candidate, diagnostic_proof)

    unknown_proof = replace(
        _verification(), goal_only_run_sha256s=(_digest("1"), _digest("0"))
    )
    with pytest.raises(StageSeedError, match="goal-only parent runs"):
        verify_stage_candidate(candidate, unknown_proof)


def test_promotion_requires_approval_but_not_a_clean_source_tree() -> None:
    verified = verify_stage_candidate(_candidate(), _verification())
    approval = StagePromotionApproval(approval_sha256=_digest("0"))
    promoted = promote_verified_stage(verified, approval)

    assert promoted["state"] == "promoted"
    assert promoted["previous_record_sha256"] == verified["root_sha256"]
    assert promoted["promotion_proof"] == {"approval_sha256": _digest("0")}

    dirty = verify_stage_candidate(_candidate(clean=False), _verification())
    dirty_promoted = promote_verified_stage(dirty, approval)
    assert dirty_promoted["state"] == "promoted"
    assert dirty_promoted["stage_evidence"]["source_tree_clean"] is False
    assert dirty_promoted["stage_evidence"] == dirty["stage_evidence"]
    with pytest.raises(TypeError):
        promote_verified_stage(verified)  # type: ignore[call-arg]


def test_stage_state_record_is_published_without_overwriting_its_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "fixtures"
    object_path = source / "objects" / "payload.bin"
    object_path.parent.mkdir(parents=True)
    destination.mkdir()
    content = b"immutable-stage-object"
    object_path.write_bytes(content)
    candidate = seal_manifest(
        {
            "format_version": "ifc-golden-seed/v1",
            "seed_kind": "ifc_stage",
            "state": "candidate",
            "source_commit": "0123456789abcdef0123456789abcdef01234567",
            "parent_seed_sha256": _digest("a"),
            "m4_run_seed_sha256": _digest("b"),
            "object_inventory": [
                {
                    "path": "objects/payload.bin",
                    "filename": "payload.bin",
                    "media_type": "application/octet-stream",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "reverse_references": ["test"],
                }
            ],
        }
    )
    write_manifest_exclusive(source / "manifest.json", candidate)
    verified = transition_seed_state(
        candidate,
        "verified",
        proof=_verification().manifest_value(),
    )

    published = publish_seed_record(
        source_root=source,
        destination_parent=destination,
        manifest=verified,
    )

    assert published == (destination / verified["root_sha256"]).resolve()
    assert (published / "objects" / "payload.bin").read_bytes() == content
    assert load_manifest(source / "manifest.json") == candidate
    verify_seed(load_manifest(published / "manifest.json"), published)
    with pytest.raises(SeedExportError, match="overwrite"):
        publish_seed_record(
            source_root=source,
            destination_parent=destination,
            manifest=verified,
        )


def test_stage_state_publication_rejects_inventory_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "fixtures"
    source.mkdir()
    destination.mkdir()
    candidate = _base_manifest()
    write_manifest_exclusive(source / "manifest.json", candidate)
    changed = seal_manifest(
        {
            **candidate,
            "object_inventory": [
                {
                    "path": "objects/missing.bin",
                    "filename": "missing.bin",
                    "media_type": "application/octet-stream",
                    "size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            ],
        }
    )

    with pytest.raises(SeedExportError, match="inventory"):
        publish_seed_record(
            source_root=source,
            destination_parent=destination,
            manifest=changed,
        )
