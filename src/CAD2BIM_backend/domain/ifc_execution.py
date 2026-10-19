"""Server-owned IFC stage execution policy.

The public API accepts a stage key for protocol compatibility, but clients do
not choose the MCP write policy.  The backend deterministically compiles the
stage type and allowed scope from the supported stage-key grammar.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field


IFC_EXECUTION_POLICY_KEY = "execution_policy"
MAX_IFC_PRODUCT_STAGES = 32
MAX_IFC_SYSTEM_STAGES = 32
_PRODUCT_STAGE = re.compile(r"^ifc_product_stage_([0-9]{3})$")
_SYSTEM_STAGE = re.compile(r"^ifc_system_stage_([0-9]{3})$")


class IFCStageType(StrEnum):
    COMPONENT_AUTHORING = "component_authoring"
    IFC_GENERATION = "ifc_generation"
    PROJECT_INIT = "project_init"
    PRODUCT_MODELING = "product_modeling"
    SYSTEM_MODELING = "system_modeling"


class IFCStageExecutionPolicy(BaseModel):
    """Serializable policy reference; contains no token or physical path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_key: str = Field(min_length=1, max_length=200)
    stage_type: IFCStageType
    allows_child_stages: bool = False


def compile_ifc_stage_execution_policy(stage_key: str) -> IFCStageExecutionPolicy:
    """Compile a trusted policy or reject an unsupported IFC stage."""

    normalized = str(stage_key or "").strip().lower()
    if normalized == IFCStageType.COMPONENT_AUTHORING:
        stage_type = IFCStageType.COMPONENT_AUTHORING
    elif normalized == IFCStageType.IFC_GENERATION:
        stage_type = IFCStageType.IFC_GENERATION
    elif normalized == IFCStageType.PROJECT_INIT:
        stage_type = IFCStageType.PROJECT_INIT
    elif normalized == IFCStageType.SYSTEM_MODELING:
        stage_type = IFCStageType.SYSTEM_MODELING
    elif (match := _SYSTEM_STAGE.fullmatch(normalized)) and (
        1 <= int(match.group(1)) <= MAX_IFC_SYSTEM_STAGES
    ):
        stage_type = IFCStageType.SYSTEM_MODELING
    elif (match := _PRODUCT_STAGE.fullmatch(normalized)) and (
        1 <= int(match.group(1)) <= MAX_IFC_PRODUCT_STAGES
    ):
        stage_type = IFCStageType.PRODUCT_MODELING
    else:
        raise ValueError(
            "unsupported IFC stage_key; expected component_authoring, ifc_generation, project_init, "
            f"ifc_product_stage_001..{MAX_IFC_PRODUCT_STAGES:03d}, system_modeling, "
            f"or ifc_system_stage_001..{MAX_IFC_SYSTEM_STAGES:03d}"
        )
    return IFCStageExecutionPolicy(
        stage_key=normalized,
        stage_type=stage_type,
        allows_child_stages=stage_type is IFCStageType.IFC_GENERATION,
    )


def compile_ifc_child_stage_execution_policy(
    root_policy: IFCStageExecutionPolicy,
    stage_key: str,
) -> IFCStageExecutionPolicy:
    """Compile one supported child under a server-owned generation scope."""

    trusted_root = compile_ifc_stage_execution_policy(root_policy.stage_key)
    if root_policy != trusted_root:
        raise ValueError("Run IFC execution policy does not match server compilation")
    if not root_policy.allows_child_stages:
        raise ValueError("Run IFC execution policy does not allow child stages")
    child = compile_ifc_stage_execution_policy(stage_key)
    if child.stage_type in {
        IFCStageType.COMPONENT_AUTHORING,
        IFCStageType.IFC_GENERATION,
    }:
        raise ValueError("root-only execution scope cannot be nested as an IFC child stage")
    return child


def resolve_ifc_context_policy(
    root_policy: IFCStageExecutionPolicy,
    *,
    stage_key: str,
    stage_type: str,
) -> IFCStageExecutionPolicy:
    """Resolve a Graph context inside one Run's immutable execution scope."""

    trusted_root = compile_ifc_stage_execution_policy(root_policy.stage_key)
    if root_policy != trusted_root:
        raise ValueError("Run IFC execution policy does not match server compilation")
    context_policy = compile_ifc_stage_execution_policy(stage_key)
    if context_policy.stage_type.value != str(stage_type).strip().lower():
        raise ValueError("Graph IFC execution policy does not match stage_key")
    if context_policy == root_policy:
        return context_policy
    if root_policy.allows_child_stages:
        return compile_ifc_child_stage_execution_policy(root_policy, context_policy.stage_key)
    raise ValueError("Graph IFC stage is outside the Run execution scope")


def load_ifc_stage_execution_policy(
    *,
    stage_key: str,
    input_payload: Mapping[str, Any],
) -> IFCStageExecutionPolicy:
    """Load the persisted policy and verify it against server compilation."""

    compiled = compile_ifc_stage_execution_policy(stage_key)
    raw = input_payload.get(IFC_EXECUTION_POLICY_KEY)
    if not isinstance(raw, Mapping):
        raise ValueError("Run has no persisted IFC execution policy")
    policy = IFCStageExecutionPolicy.model_validate(raw)
    if policy != compiled:
        raise ValueError("persisted IFC execution policy does not match stage_key")
    return policy


__all__ = [
    "IFC_EXECUTION_POLICY_KEY",
    "MAX_IFC_PRODUCT_STAGES",
    "IFCStageExecutionPolicy",
    "IFCStageType",
    "compile_ifc_child_stage_execution_policy",
    "compile_ifc_stage_execution_policy",
    "load_ifc_stage_execution_policy",
    "resolve_ifc_context_policy",
]
