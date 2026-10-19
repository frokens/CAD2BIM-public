"""Scope-bound immutable reference indexing through the shared PostgreSQL repository."""

import hashlib
import re
import secrets

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.infrastructure.postgres.models import OpaqueReferenceIndexModel
from CAD2BIM_backend.infrastructure.postgres.repository_base import PostgresRepositoryBase
from CAD2BIM_backend.infrastructure.postgres.tenant_context import (
    current_tenant_execution_context, set_tenant_execution_context,
)
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError


_KINDS = frozenset({"gr", "gv", "fr", "fv", "sc", "gq", "gi", "gd", "fd", "or"})


class PostgresReferenceIndexRepositoryMixin(PostgresRepositoryBase):
    @staticmethod
    def _require_reference_context(context, scope_digest):
        if not context.task_id or current_tenant_execution_context() != context:
            raise ReferenceIndexError("Reference index requires the bound task scope")
        if re.fullmatch(r"[0-9a-f]{64}", scope_digest) is None:
            raise ReferenceIndexError("Reference index scope digest is invalid")

    async def get_or_create_reference(
        self, context: TenantExecutionContext, *, scope_digest: str,
        reference_kind: str, sealed_reference: str,
    ) -> str:
        self._require_reference_context(context, scope_digest)
        if reference_kind not in _KINDS or not (
            sealed_reference.startswith("cur.") and len(sealed_reference) <= 16_384
        ):
            raise ReferenceIndexError("Reference index input is invalid")
        fingerprint = hashlib.sha256(sealed_reference.encode()).hexdigest()
        target = select(OpaqueReferenceIndexModel).where(
            OpaqueReferenceIndexModel.tenant_id == context.tenant_id,
            OpaqueReferenceIndexModel.principal_id == context.principal_id,
            OpaqueReferenceIndexModel.task_id == context.task_id,
            OpaqueReferenceIndexModel.scope_digest == scope_digest,
            OpaqueReferenceIndexModel.reference_sha256 == fingerprint,
        )
        async with self._sessions() as session, session.begin():
            await set_tenant_execution_context(session, context)
            for _ in range(3):
                previous = await session.scalar(target)
                if previous is not None:
                    if (previous.reference_kind != reference_kind
                            or previous.sealed_reference != sealed_reference):
                        raise ReferenceIndexError("Reference index digest collision")
                    return previous.handle
                handle = reference_kind + "_" + secrets.token_urlsafe(16)
                inserted = await session.scalar(
                    insert(OpaqueReferenceIndexModel).values(
                        handle=handle, tenant_id=context.tenant_id,
                        principal_id=context.principal_id, task_id=context.task_id,
                        scope_digest=scope_digest, reference_kind=reference_kind,
                        reference_sha256=fingerprint, sealed_reference=sealed_reference,
                    ).on_conflict_do_nothing().returning(OpaqueReferenceIndexModel.handle)
                )
                if inserted is not None:
                    return inserted
                # Concurrent exact publication or a random handle collision. Re-read
                # the exact binding; never retarget an existing handle with UPDATE.
            raise ReferenceIndexError("Reference index allocation conflict")

    async def resolve_reference(
        self, context: TenantExecutionContext, *, scope_digest: str, handle: str,
    ) -> tuple[str, str] | None:
        self._require_reference_context(context, scope_digest)
        async with self._sessions() as session:
            await set_tenant_execution_context(session, context)
            row = await session.scalar(select(OpaqueReferenceIndexModel).where(
                OpaqueReferenceIndexModel.handle == handle,
                OpaqueReferenceIndexModel.tenant_id == context.tenant_id,
                OpaqueReferenceIndexModel.principal_id == context.principal_id,
                OpaqueReferenceIndexModel.task_id == context.task_id,
                OpaqueReferenceIndexModel.scope_digest == scope_digest,
            ))
            if row is None:
                return None
            if hashlib.sha256(row.sealed_reference.encode()).hexdigest() != row.reference_sha256:
                raise ReferenceIndexError("Reference index integrity check failed")
            return row.reference_kind, row.sealed_reference
