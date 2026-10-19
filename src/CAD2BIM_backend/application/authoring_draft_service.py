"""Shared immutable snapshot I/O for the Geometry and Family draft owners.

Root selection, content validation and publication remain domain responsibilities.
The existing repositories own the one root/CAS/Operation transaction.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.catalog_scope import catalog_scope_digest
from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftRecord
from CAD2BIM_backend.domain.authoring_draft import AuthoringDraftChanged
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest, stable_id
from CAD2BIM_backend.domain.control_plane import ExecutionMode
from CAD2BIM_backend.domain.data_runtime import (
    ArtifactVersionRef, AuthoringDraftWriteOperationDetail, CriticalOperationInitiator,
    CriticalOperationKind, CriticalOperationRecord, CriticalOperationStatus,
)
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.identity import TenantActorKind
from CAD2BIM_backend.domain.resource_scope import ResourceOwnerScope, ResourceOwnerScopeKind
from CAD2BIM_backend.ports.opaque_cursor import OpaqueCursorCodecPort
from CAD2BIM_backend.ports.geometry_authoring import GeometryAuthoringSourceError, GeometryAuthoringSourceFailure

_CONTENT_TYPE = "application/vnd.cad2bim.authoring-draft+json"
_TOOLS = {"geometry": "ifc_geometry_resource_write", "family": "ifc_product_family_write"}
_KINDS = {"geometry": CriticalOperationKind.GEOMETRY_RESOURCE_WRITE,
          "family": CriticalOperationKind.PRODUCT_FAMILY_WRITE}


class AuthoringDraftError(ValueError):
    def __init__(self, code: str, message: str, *, operation_alias: str | None = None, phase: str = "preflight",
                 operation_status: Literal["failed"] | None = None):
        super().__init__(message)
        self.code = code
        self.operation_alias = operation_alias
        self.phase = phase
        self.operation_status = operation_status


class AuthoringDraftService:
    def __init__(self, artifacts: ArtifactService, repository, codec: OpaqueCursorCodecPort,
                 *, clock: Callable[[], datetime] | None = None,
                 owner_resolver: Callable[[InvocationScope], Awaitable[ResourceOwnerScope]] | None = None):
        self._artifacts = artifacts
        self._repository = repository
        self._codec = codec
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_resolver = owner_resolver

    async def resource_owner(self, scope: InvocationScope) -> ResourceOwnerScope:
        """The authenticated entry chooses ownership; it is never a tool input."""
        if self._owner_resolver is not None:
            owner = await self._owner_resolver(scope)
        else:
            owner = ResourceOwnerScope(scope_kind=ResourceOwnerScopeKind.TASK,
                tenant_id=scope.identity.tenant_id, task_id=scope.identity.task_id)
        if (owner.scope_kind is ResourceOwnerScopeKind.SYSTEM
            or owner.tenant_id != scope.identity.tenant_id
            or (owner.scope_kind is ResourceOwnerScopeKind.TASK and owner.task_id != scope.identity.task_id)):
            raise AuthoringDraftError("AUTHORING_SCOPE_INVALID", "The authoring owner differs from its authenticated entry")
        return owner

    async def require_write_owner(self, scope, owner):
        if owner.scope_kind is ResourceOwnerScopeKind.SYSTEM and scope.identity.actor_kind is TenantActorKind.SYSTEM_CURATOR:
            return
        if owner != await self.resource_owner(scope):
            raise AuthoringDraftError("AUTHORING_SCOPE_INVALID", "This entry cannot write or publish this resource owner")

    @staticmethod
    def fixed_input_rejection(error):
        # These failures concern immutable input bytes or a monotonic root CAS.
        # With the same request they cannot turn into a successful late write.
        return (
            isinstance(error, AuthoringDraftChanged)
            or (isinstance(error, AuthoringDraftError) and error.code == "AUTHORING_SOURCE_INVALID")
            or (isinstance(error, GeometryAuthoringSourceError) and error.failure in {
                GeometryAuthoringSourceFailure.INVALID, GeometryAuthoringSourceFailure.MEMBER_NOT_FOUND,
                GeometryAuthoringSourceFailure.MEMBER_NOT_TEXT, GeometryAuthoringSourceFailure.STALE_SOURCE,
                GeometryAuthoringSourceFailure.MATCH_NOT_FOUND, GeometryAuthoringSourceFailure.MATCH_NOT_UNIQUE,
                GeometryAuthoringSourceFailure.OVERLAP, GeometryAuthoringSourceFailure.BUDGET_EXCEEDED,
            })
        )

    async def request_rejection_status(self, scope, *, tool_name, request_identity, definite_rejection) -> Literal["not_started", "unknown"]:
        """Settle only a proven fixed-request rejection with no original admission."""
        if not definite_rejection:
            return "unknown"
        kinds = {**{tool: _KINDS[root_kind] for root_kind, tool in _TOOLS.items()},
            "ifc_authoring_publish": CriticalOperationKind.AUTHORING_PUBLISH}
        try:
            operation_id = self.operation_identity(scope, tool_name, request_identity)
            state = await self._repository.get_authoring_execution_state(scope, operation_id,
                operation_kind=kinds[tool_name])
            published = await self._repository.get_operation(scope, operation_id)
        except Exception:
            return "unknown"
        return "not_started" if state is None and published is None else "unknown"

    @staticmethod
    def operation_identity(scope: InvocationScope, tool_name: str, request_identity: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request_identity):
            raise AuthoringDraftError("AUTHORING_INPUT_INVALID", "Invalid original call identity")
        if scope.identity.task_id is None:
            raise AuthoringDraftError("AUTHORING_SCOPE_INVALID", "Authoring requires a Task scope")
        return stable_id("operation", {"tenant_id": scope.identity.tenant_id,
            "task_id": scope.identity.task_id, "run_id": scope.run_id,
            "tool_name": tool_name, "request_identity": request_identity})

    async def replay(self, scope, *, root_kind, declaration, request_identity):
        operation_id = self.operation_identity(scope, _TOOLS[root_kind], request_identity)
        fingerprint = sha256_digest(declaration)
        existing = await self._repository.get_operation(scope, operation_id)
        if existing is None:
            return operation_id, fingerprint, None
        operation = existing.operation
        if (operation.request_fingerprint != fingerprint
            or operation.operation_kind != _KINDS[root_kind]
            or not isinstance(operation.detail, AuthoringDraftWriteOperationDetail)):
            raise AuthoringDraftError("AUTHORING_IDEMPOTENCY_CONFLICT",
                                     "The original call identity has different saved inputs")
        return operation_id, fingerprint, existing

    def reference(self, scope: InvocationScope, draft: AuthoringDraftRecord) -> str:
        return self._reference_for_root(scope, draft.root_kind, draft.root_id)

    def _reference_for_root(self, scope, root_kind, root_id):
        return self._codec.encode({"domain": "authoring_draft", "root_kind": root_kind,
            "root_id": root_id, "scope_digest": catalog_scope_digest(scope.identity)})

    def operation_result(self, scope, published):
        """Recover the original save token without substituting the current draft."""
        operation = published.operation
        detail = operation.detail
        if (operation.owner_id != scope.owner_id or operation.tenant_id != scope.identity.tenant_id
            or operation.task_id != scope.identity.task_id
            or operation.status is not CriticalOperationStatus.SUCCEEDED
            or not isinstance(detail, AuthoringDraftWriteOperationDetail)):
            raise AuthoringDraftError("AUTHORING_DRAFT_NOT_AVAILABLE", "The saved draft Operation is outside this scope")
        return {"root_kind": detail.root_kind,
            "draft_ref": self._reference_for_root(scope, detail.root_kind, detail.root_id),
            "edit_token": detail.result_generation}

    async def save_navigation(self, scope, draft):
        from CAD2BIM_backend.application.opaque_reference_service import encode_operation_reference
        published = await self._repository.get_authoring_save_for_snapshot(scope,
            root_kind=draft.root_kind, root_id=draft.root_id,
            snapshot_artifact_version_id=draft.snapshot.version_id)
        if published is None:
            return {"operation_ref": None, "operation_alias": None}
        record = published.operation
        return {"operation_ref": encode_operation_reference(self._codec, scope.identity,
            origin_run_id=record.run_id, operation_id=record.operation_id),
            "operation_alias": published.catalog_entry.alias if record.run_id == scope.run_id else None}

    async def publication_navigation(self, scope, *, root_kind, version_id):
        from CAD2BIM_backend.application.opaque_reference_service import encode_operation_reference
        published = await self._repository.get_authoring_publication_for_version(
            scope, root_kind=root_kind, version_id=version_id)
        if published is None:
            return {"availability": "unavailable"}
        record = published.operation
        return {"availability": "available", "operation_ref": encode_operation_reference(
            self._codec, scope.identity, origin_run_id=record.run_id, operation_id=record.operation_id),
            "operation_alias": published.catalog_entry.alias if record.run_id == scope.run_id else None}

    def resolve_reference(self, scope: InvocationScope, value: str, *, root_kind: str) -> str:
        claims = self._codec.decode(value)
        if (claims.get("domain") != "authoring_draft" or claims.get("root_kind") != root_kind
            or claims.get("scope_digest") != catalog_scope_digest(scope.identity)
            or not isinstance(claims.get("root_id"), str)):
            raise AuthoringDraftError("AUTHORING_DRAFT_NOT_AVAILABLE",
                                     "The draft reference is not available in this scope")
        return claims["root_id"]

    async def read(self, scope, draft: AuthoringDraftRecord, content_type: type[BaseModel]):
        return await self.read_snapshot(scope, draft.snapshot, content_type, draft=draft)

    async def read_snapshot(self, scope, snapshot: ArtifactVersionRef, content_type: type[BaseModel], *, draft=None):
        authorized = None
        if draft is not None and draft.owner_scope.scope_kind is ResourceOwnerScopeKind.TENANT:
            if draft.snapshot != snapshot:
                raise AuthoringDraftError("AUTHORING_DRAFT_INTEGRITY_FAILED", "The selected root snapshot changed")
            authorized = await self._repository.get_authoring_draft_snapshot(scope, draft=draft)
        downloaded = await self._artifacts.download_version(
            authorized.owner_id if authorized is not None else scope.owner_id,
            snapshot.artifact_id, snapshot.version_id)
        record = downloaded.artifact
        if (((record.owner_id != scope.owner_id or record.task_id != scope.identity.task_id) if authorized is None
             else record != authorized)
            or record.content_type != _CONTENT_TYPE or record.sha256 != snapshot.sha256):
            raise AuthoringDraftError("AUTHORING_DRAFT_INTEGRITY_FAILED",
                                     "Saved draft content does not match its exact snapshot")
        return content_type.model_validate_json(downloaded.content)

    async def save(self, scope, *, root_kind, root_id, content: BaseModel,
                   expected_generation, operation_id, fingerprint, request_identity):
        """Prepare bytes and the terminal record; the resource owner commits both."""
        body = canonical_json(content.model_dump(mode="json")).encode("utf-8")
        artifact = await self._artifacts.upload_bytes(owner_id=scope.owner_id,
            task_id=scope.identity.task_id, run_id=scope.run_id,
            filename=f"{root_kind}_draft.json", content_type=_CONTENT_TYPE,
            content=body, kind="authoring_draft",
            idempotency_key=f"authoring-draft:{operation_id}")
        snapshot = ArtifactVersionRef(artifact_id=artifact.artifact_id,
            version_id=artifact.version_id, sha256=artifact.sha256)
        await self.read_snapshot(scope, snapshot, type(content))
        operation = CriticalOperationRecord(operation_id=operation_id,
            owner_id=scope.owner_id, tenant_id=scope.identity.tenant_id,
            task_id=scope.identity.task_id, run_id=scope.run_id,
            operation_kind=_KINDS[root_kind], tool_name=_TOOLS[root_kind],
            initiator=(CriticalOperationInitiator.DIRECT_CLIENT
                       if scope.execution_mode is ExecutionMode.DIRECT_MCP else CriticalOperationInitiator.RUNNER),
            intent_summary=f"Save {root_kind} draft {content.display_name}; edit {(expected_generation or 0) + 1}.",
            idempotency_key=f"authoring-draft:{request_identity}",
            request_fingerprint=fingerprint, status=CriticalOperationStatus.SUCCEEDED,
            code_artifact=snapshot, started_at=artifact.created_at, finished_at=self._clock(),
            target_geometry_resource_id=root_id if root_kind == "geometry" else None,
            target_product_family_id=root_id if root_kind == "family" else None,
            detail=AuthoringDraftWriteOperationDetail(root_kind=root_kind, root_id=root_id,
                expected_generation=expected_generation, result_generation=(expected_generation or 0) + 1))
        return snapshot, operation


def validate_draft_source(members: Mapping[str, bytes], *, entry_functions: tuple[str, ...]) -> None:
    """Only syntax and declared entry points; never import or execute author code."""
    for path, body in members.items():
        try:
            module = ast.parse(body.decode("utf-8"), filename=path)
        except (SyntaxError, UnicodeDecodeError) as error:
            raise AuthoringDraftError("AUTHORING_SOURCE_INVALID", f"Invalid Python source in {path}") from error
        if path == "main.py":
            names = {node.name for node in module.body if isinstance(node, ast.FunctionDef)}
            missing = tuple(name for name in entry_functions if name not in names)
            if missing:
                raise AuthoringDraftError("AUTHORING_SOURCE_INVALID",
                    "main.py requires " + ", ".join(missing))
    if "main.py" not in members:
        raise AuthoringDraftError("AUTHORING_SOURCE_INVALID", "Source requires main.py")
