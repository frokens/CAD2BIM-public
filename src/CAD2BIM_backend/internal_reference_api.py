"""Translate current Runner-scope navigation references through their existing owner."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Callable, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from CAD2BIM_backend.application.opaque_reference_service import OpaqueReferenceService, REFERENCE_KINDS
from CAD2BIM_backend.domain.identity import TenantExecutionContext
from CAD2BIM_backend.ports.reference_index import ReferenceIndexError


class _ReferenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str = Field(min_length=1)
    kinds: list[str] = Field(min_length=1)

    @field_validator("kinds")
    @classmethod
    def require_current_kinds(cls, values: list[str]) -> list[str]:
        if not set(values) <= REFERENCE_KINDS:
            raise ValueError("unsupported reference kind")
        return values


class _ReferenceTranslation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    direction: Literal["expand", "shorten"]
    items: list[_ReferenceItem]


def create_internal_reference_router(
    *,
    authorized: Callable,
    bind_identity: Callable[[TenantExecutionContext], AbstractContextManager[None]],
    references: OpaqueReferenceService,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.post("/references/translate")
    async def translate(request: Request, body: _ReferenceTranslation) -> dict[str, list[str]]:
        # The caller uses the current business tool credential for both projections.
        # This endpoint changes navigation only, not domain permission or write state.
        _, identity = await authorized(request)
        translate_one = references.expand if body.direction == "expand" else references.shorten
        try:
            with bind_identity(identity):
                values = [await translate_one(identity, item.value, item.kinds) for item in body.items]
        except ReferenceIndexError as exc:
            raise HTTPException(status_code=409, detail={
                "code": "REFERENCE_INVALID", "message": str(exc),
                "phase": "preflight", "retryable": False,
            }) from None
        return {"values": values}

    return router
