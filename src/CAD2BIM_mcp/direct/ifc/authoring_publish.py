"""Publish draft definitions through the shared authoring services."""

from typing import Literal

from fastmcp import Context, FastMCP
from pydantic import Field, ValidationError

from CAD2BIM_backend.domain.data_runtime import AuthoringPreviewSelection
from CAD2BIM_backend.internal_authoring_contracts import AuthoringPublishInput
from CAD2BIM_mcp.direct.contract_base import DirectContract
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.ifc.invocation import server_ifc_invocation_key
from CAD2BIM_mcp.direct.tool_support import DIRECT_SESSION_ALIAS, invalid_provider_input, register_provider_tool
from CAD2BIM_mcp.tool_docs.common import AUTHORING_PUBLISH_DESCRIPTION as PUBLISH_DESCRIPTION
from CAD2BIM_mcp.tool_docs import common as _tool_docs


class DirectAuthoringPublishInput(AuthoringPublishInput):
    direct_session_alias: str = Field(pattern=r"^direct_[0-9]{3,}$", max_length=32, description=_tool_docs.DIRECT_SESSION_TASK_SCOPE_ALIAS_DESCRIPTION)
    draft_ref: str = Field(min_length=1, max_length=16_384, description=_tool_docs.AUTHORING_DRAFT_REF_DESCRIPTION)
    expected_edit_token: int = Field(strict=True, ge=1, description=_tool_docs.AUTHORING_EXPECTED_EDIT_TOKEN_DESCRIPTION)
    preview_examples: tuple[AuthoringPreviewSelection, ...] = Field(default=(), max_length=32, description=_tool_docs.AUTHORING_PREVIEW_EXAMPLES_DESCRIPTION)


class AuthoringPublishedGeometryResult(DirectContract):
    resource_version_alias: str = Field(min_length=1, max_length=16_384)


class AuthoringPublishResult(DirectContract):
    operation_alias: str = Field(pattern=r"^op_[0-9]{3,}$", max_length=32)
    geometry_versions: tuple[AuthoringPublishedGeometryResult, ...] = ()
    family_version_alias: str | None = Field(default=None, min_length=1, max_length=16_384)


class AuthoringPublishApplicationMixin:
    async def publish_ifc_authoring(self, request: DirectAuthoringPublishInput, *, request_identity: str):
        async def action(scope):
            service = (self._require_geometry_authoring_service() if request.root_kind == "geometry"
                       else self._require_product_family_authoring_service())
            committed = await service.publish_draft(
                scope, draft_ref=request.draft_ref, expected_edit_token=request.expected_edit_token,
                preview_examples=request.preview_examples, request_identity=request_identity,
            )
            return AuthoringPublishResult(
                operation_alias=committed.operation.catalog_entry.alias,
                geometry_versions=tuple(AuthoringPublishedGeometryResult(
                    resource_version_alias=self._require_geometry_catalog_service().version_ref(scope.identity, item)
                ) for item in committed.geometry_versions),
                family_version_alias=(self._require_product_family_catalog_service().version_ref(
                    scope.identity, committed.family_version,
                ) if committed.family_version is not None else None),
            )
        return await self._authoring_call(request.direct_session_alias, "ifc_authoring_publish", action)


def register_ifc_authoring_publish(server: FastMCP, application) -> None:
    async def ifc_authoring_publish(
        direct_session_alias: DIRECT_SESSION_ALIAS, ctx: Context,
        root_kind: Literal["geometry", "family"], draft_ref: str,
        expected_edit_token: int, preview_examples: tuple[AuthoringPreviewSelection, ...] = (),
    ) -> AuthoringPublishResult:
        try:
            request = DirectAuthoringPublishInput(
                direct_session_alias=direct_session_alias, root_kind=root_kind, draft_ref=draft_ref,
                expected_edit_token=expected_edit_token, preview_examples=preview_examples,
            )
            return await application.publish_ifc_authoring(request, request_identity=server_ifc_invocation_key(ctx))
        except ValidationError as exc:
            raise invalid_provider_input("Use a current draft and edit token; Preview examples are optional", exc) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    register_provider_tool(
        server, ifc_authoring_publish, name="ifc_authoring_publish",
        input_type=DirectAuthoringPublishInput, output_type=AuthoringPublishResult,
        compact_input=True, compact_output=True, description=PUBLISH_DESCRIPTION,
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    )
