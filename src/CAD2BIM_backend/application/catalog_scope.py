"""Stable authority digest shared by reusable catalog references."""

from __future__ import annotations

import hashlib

from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.identity import TenantExecutionContext


def catalog_scope_digest(scope: TenantExecutionContext) -> str:
    stable_scope = {
        "tenant_id": scope.tenant_id,
        "principal_id": scope.principal_id,
        "task_id": scope.task_id,
        "actor_kind": scope.actor_kind.value,
    }
    return hashlib.sha256(canonical_json(stable_scope).encode("utf-8")).hexdigest()


__all__ = ["catalog_scope_digest"]
