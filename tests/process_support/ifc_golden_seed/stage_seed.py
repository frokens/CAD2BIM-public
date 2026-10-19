"""Server-only evidence contract for immutable IFC Stage Seed records."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from tests.process_support.ifc_golden_seed.closure import SeedClosure
from tests.process_support.ifc_golden_seed.core import (
    SeedValidationError,
    compute_inventory_digest,
    compute_root_sha256,
    seal_manifest,
    transition_seed_state,
)

StageKey = Literal[
    "foundation",
    "spatial_root",
    "basic_products",
    "connected_products",
    "systems_and_groups",
    "enriched_products",
    "full_product_families",
    "full_golden_candidate",
]
RunMode = Literal["diagnostic_guided", "goal_only_semiblind"]

_STAGE_KEYS = {
    "foundation",
    "spatial_root",
    "basic_products",
    "connected_products",
    "systems_and_groups",
    "enriched_products",
    "full_product_families",
    "full_golden_candidate",
}
_RUN_MODES = {"diagnostic_guided", "goal_only_semiblind"}
_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}
_EMPTY_DIFF_SHA256 = hashlib.sha256(b"").hexdigest()


class StageSeedError(SeedValidationError):
    """An IFC Stage Seed lacks an exact or internally consistent evidence binding."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise StageSeedError(f"{field} must be a non-empty string")
    return value


def _sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise StageSeedError(f"{field} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class StageAgentRunEvidence:
    """One immutable native-agent event manifest known only to the Seed harness."""

    run_key: str
    mode: RunMode
    parent_seed_sha256: str
    event_manifest_sha256: str

    def __post_init__(self) -> None:
        _text(self.run_key, "run_key")
        if self.mode not in _RUN_MODES:
            raise StageSeedError("mode is not an approved native experiment mode")
        _sha256(self.parent_seed_sha256, "parent_seed_sha256")
        _sha256(self.event_manifest_sha256, "event_manifest_sha256")

    def manifest_value(self) -> dict[str, object]:
        return {
            "run_key": self.run_key,
            "mode": self.mode,
            "parent_seed_sha256": self.parent_seed_sha256,
            "event_manifest_sha256": self.event_manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class StageCandidateEvidence:
    """Inputs frozen before a Stage experiment; raw prompts and schemas stay elsewhere."""

    stage_key: StageKey
    provider_schema_sha256: str
    tool_descriptions_sha256: str
    system_prompt_sha256: str
    stage_goal_sha256: str
    enabled_tools_sha256: str
    model: str
    reasoning_effort: str
    budget_sha256: str
    oracle_version: str
    source_tree_clean: bool
    source_diff_sha256: str
    agent_runs: tuple[StageAgentRunEvidence, ...]

    def __post_init__(self) -> None:
        if self.stage_key not in _STAGE_KEYS:
            raise StageSeedError("stage_key is not an approved IFC Stage Seed key")
        for field in (
            "provider_schema_sha256",
            "tool_descriptions_sha256",
            "system_prompt_sha256",
            "stage_goal_sha256",
            "enabled_tools_sha256",
            "budget_sha256",
            "source_diff_sha256",
        ):
            _sha256(getattr(self, field), field)
        _text(self.model, "model")
        if self.reasoning_effort not in _REASONING_EFFORTS:
            raise StageSeedError("reasoning_effort is unsupported")
        _text(self.oracle_version, "oracle_version")
        if not isinstance(self.source_tree_clean, bool):
            raise StageSeedError("source_tree_clean must be a boolean")
        if self.source_tree_clean and self.source_diff_sha256 != _EMPTY_DIFF_SHA256:
            raise StageSeedError("a clean source tree must bind the empty diff digest")
        if not self.source_tree_clean and self.source_diff_sha256 == _EMPTY_DIFF_SHA256:
            raise StageSeedError("a dirty source tree must bind a non-empty diff digest")
        if not isinstance(self.agent_runs, tuple) or not self.agent_runs:
            raise StageSeedError("agent_runs must be a non-empty tuple")
        if any(not isinstance(run, StageAgentRunEvidence) for run in self.agent_runs):
            raise StageSeedError("agent_runs must contain only StageAgentRunEvidence records")
        run_keys = [run.run_key for run in self.agent_runs]
        event_digests = [run.event_manifest_sha256 for run in self.agent_runs]
        if len(set(run_keys)) != len(run_keys):
            raise StageSeedError("agent run keys must be distinct")
        if len(set(event_digests)) != len(event_digests):
            raise StageSeedError("agent event manifests must be distinct")


@dataclass(frozen=True, slots=True)
class StageVerificationProof:
    """Independent oracle, parent regression, and exactly two qualifying runs."""

    oracle_result_sha256: str
    parent_regression_sha256: str
    goal_only_run_sha256s: tuple[str, str]

    def __post_init__(self) -> None:
        _sha256(self.oracle_result_sha256, "oracle_result_sha256")
        _sha256(self.parent_regression_sha256, "parent_regression_sha256")
        if (
            not isinstance(self.goal_only_run_sha256s, tuple)
            or len(self.goal_only_run_sha256s) != 2
            or len(set(self.goal_only_run_sha256s)) != 2
        ):
            raise StageSeedError("verification requires two distinct goal-only runs")
        for digest in self.goal_only_run_sha256s:
            _sha256(digest, "goal_only_run_sha256s")

    def manifest_value(self) -> dict[str, object]:
        return {
            "oracle_result_sha256": self.oracle_result_sha256,
            "parent_regression_sha256": self.parent_regression_sha256,
            "goal_only_run_sha256s": list(self.goal_only_run_sha256s),
        }


@dataclass(frozen=True, slots=True)
class StagePromotionApproval:
    """Explicit technical-Seed promotion approval, separate from visual acceptance."""

    approval_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.approval_sha256, "approval_sha256")

    def manifest_value(self) -> dict[str, str]:
        return {"approval_sha256": self.approval_sha256}


def _require_sealed_stage_candidate(manifest: Mapping[str, Any]) -> None:
    if manifest.get("seed_kind") != "ifc_stage" or manifest.get("state") != "candidate":
        raise StageSeedError("Stage candidate must be an ifc_stage candidate record")
    if manifest.get("root_sha256") != compute_root_sha256(manifest):
        raise StageSeedError("base Stage candidate root SHA-256 does not verify")
    inventory = manifest.get("object_inventory")
    if not isinstance(inventory, list) or manifest.get(
        "artifact_inventory_digest"
    ) != compute_inventory_digest(inventory):
        raise StageSeedError("base Stage candidate inventory digest does not verify")
    _text(manifest.get("source_commit"), "source_commit")
    _sha256(manifest.get("parent_seed_sha256"), "parent_seed_sha256")
    _sha256(manifest.get("m4_run_seed_sha256"), "m4_run_seed_sha256")
    if "stage_evidence" in manifest:
        raise StageSeedError("Stage candidate already contains stage_evidence")


def _current_ifc_binding(closure: SeedClosure) -> dict[str, object]:
    if len(closure.ifc_models) != 1:
        raise StageSeedError("Stage closure must contain exactly one IFC model")
    model = closure.ifc_models[0]
    if model.get("status") != "active":
        raise StageSeedError("Stage closure IFC model is not active")
    model_id = _text(model.get("ifc_model_id"), "current_ifc_model_id")
    revision_id = _text(model.get("current_revision_id"), "current_ifc_revision_id")
    revisions = [
        row
        for row in closure.ifc_revisions
        if row.get("ifc_model_id") == model_id and row.get("revision_id") == revision_id
    ]
    if len(revisions) != 1:
        raise StageSeedError("IFC CURRENT does not resolve to one exact revision")
    revision = revisions[0]
    revision_no = revision.get("revision_no")
    if isinstance(revision_no, bool) or not isinstance(revision_no, int) or revision_no < 1:
        raise StageSeedError("CURRENT revision number must be a positive integer")
    if model.get("write_epoch") != revision_no:
        raise StageSeedError("IFC CURRENT revision does not match the model write epoch")
    artifact_id = _text(revision.get("artifact_id"), "current_ifc_artifact_id")
    version_id = _text(
        revision.get("artifact_version_id"), "current_ifc_artifact_version_id"
    )
    versions = [
        row
        for row in closure.artifact_versions
        if row.get("artifact_id") == artifact_id and row.get("version_id") == version_id
    ]
    if len(versions) != 1:
        raise StageSeedError("CURRENT revision does not resolve to one exact ArtifactVersion")
    artifacts = [row for row in closure.artifacts if row.get("artifact_id") == artifact_id]
    if len(artifacts) != 1 or artifacts[0].get("current_version_id") != version_id:
        raise StageSeedError("CURRENT IFC revision is not the Artifact CURRENT version")
    reverse_references = {
        (item.artifact_id, item.version_id): set(item.reverse_references)
        for item in closure.artifact_reachability
    }.get((artifact_id, version_id), set())
    required_references = {
        f"ifc_revision:{revision_id}",
        f"artifact:{artifact_id}:current",
    }
    if not required_references <= reverse_references:
        raise StageSeedError("CURRENT IFC ArtifactVersion is outside the DB closure")
    return {
        "current_ifc_model_id": model_id,
        "current_ifc_revision_id": revision_id,
        "current_ifc_revision_no": revision_no,
        "current_ifc_artifact_id": artifact_id,
        "current_ifc_artifact_version_id": version_id,
        "current_ifc_sha256": _sha256(versions[0].get("sha256"), "current_ifc_sha256"),
    }


def build_stage_candidate_manifest(
    *,
    base_manifest: Mapping[str, Any],
    closure: SeedClosure,
    evidence: StageCandidateEvidence,
) -> dict[str, Any]:
    """Bind a sealed exporter record to exact DB CURRENT and experiment evidence."""

    _require_sealed_stage_candidate(base_manifest)
    parent_digest = str(base_manifest["parent_seed_sha256"])
    for run in evidence.agent_runs:
        if run.parent_seed_sha256 != parent_digest:
            raise StageSeedError("every agent run must start from the candidate parent Seed")
    stage_evidence: dict[str, object] = {
        "visibility": "server_only",
        "stage_key": evidence.stage_key,
        **_current_ifc_binding(closure),
        "provider_schema_sha256": evidence.provider_schema_sha256,
        "tool_descriptions_sha256": evidence.tool_descriptions_sha256,
        "system_prompt_sha256": evidence.system_prompt_sha256,
        "stage_goal_sha256": evidence.stage_goal_sha256,
        "enabled_tools_sha256": evidence.enabled_tools_sha256,
        "model": evidence.model,
        "reasoning_effort": evidence.reasoning_effort,
        "budget_sha256": evidence.budget_sha256,
        "oracle_version": evidence.oracle_version,
        "source_tree_clean": evidence.source_tree_clean,
        "source_diff_sha256": evidence.source_diff_sha256,
        "agent_runs": [run.manifest_value() for run in evidence.agent_runs],
    }
    candidate = dict(base_manifest)
    candidate.pop("root_sha256", None)
    candidate.pop("artifact_inventory_digest", None)
    candidate["stage_evidence"] = stage_evidence
    return seal_manifest(candidate)


def verify_stage_candidate(
    candidate: Mapping[str, Any], proof: StageVerificationProof
) -> dict[str, Any]:
    """Advance a candidate only when two independent goal-only runs are bound."""

    evidence = candidate.get("stage_evidence")
    if not isinstance(evidence, Mapping) or evidence.get("visibility") != "server_only":
        raise StageSeedError("candidate lacks closed server-only stage_evidence")
    parent_digest = candidate.get("parent_seed_sha256")
    runs = evidence.get("agent_runs")
    if not isinstance(runs, list):
        raise StageSeedError("candidate agent_runs are invalid")
    qualifying = {
        run.get("event_manifest_sha256")
        for run in runs
        if isinstance(run, Mapping)
        and run.get("mode") == "goal_only_semiblind"
        and run.get("parent_seed_sha256") == parent_digest
    }
    if set(proof.goal_only_run_sha256s) - qualifying:
        raise StageSeedError(
            "verification proof is not bound to two independent goal-only parent runs"
        )
    return transition_seed_state(candidate, "verified", proof=proof.manifest_value())


def promote_verified_stage(
    verified: Mapping[str, Any], approval: StagePromotionApproval
) -> dict[str, Any]:
    """Promote a verified record; source-tree provenance is not an eligibility gate."""

    evidence = verified.get("stage_evidence")
    if not isinstance(evidence, Mapping):
        raise StageSeedError("an incomplete Stage record cannot be promoted")
    return transition_seed_state(verified, "promoted", proof=approval.manifest_value())


__all__ = [
    "RunMode",
    "StageAgentRunEvidence",
    "StageCandidateEvidence",
    "StageKey",
    "StagePromotionApproval",
    "StageSeedError",
    "StageVerificationProof",
    "build_stage_candidate_manifest",
    "promote_verified_stage",
    "verify_stage_candidate",
]
