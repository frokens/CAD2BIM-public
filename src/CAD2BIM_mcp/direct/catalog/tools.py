"""Direct tool registrations owned by this capability family."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field


from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
from CAD2BIM_mcp.direct.catalog.contracts import (
    DirectDataDescribePackageResult,
    DirectPackageListResult,
    PackageDescriptionResult,
)
from CAD2BIM_mcp.direct.transform.common_inputs import (
    DirectPackageListInput,
    DirectoryPageSize,
    PositiveInt64,
)
from CAD2BIM_mcp.tool_docs import catalog as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


def register_data_package_list(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="data_package_list",
        description=(_tool_docs.DATA_PACKAGE_LIST_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    async def data_package_list(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION,
            ),
        ],
        before_sequence: Annotated[
            PositiveInt64 | None,
            Field(description=(_tool_docs.DATA_PACKAGE_LIST_BEFORE_SEQUENCE_DESCRIPTION)),
        ] = None,
        page_size: Annotated[
            DirectoryPageSize,
            Field(description=_tool_docs.DATA_PACKAGE_LIST_PAGE_SIZE_DESCRIPTION),
        ] = 20,
    ) -> DirectPackageListResult:
        try:
            request = DirectPackageListInput(
                direct_session_alias=direct_session_alias,
                before_sequence=before_sequence,
                page_size=page_size,
            )
            return await application.list_data_packages(
                request.direct_session_alias,
                request.before_sequence,
                request.page_size,
            )
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None


def register_data_get_package_description(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="data_get_package_description",
        description=(_tool_docs.DATA_GET_PACKAGE_DESCRIPTION_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    async def data_get_package_description(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                description=_tool_docs.DATA_GET_PACKAGE_DESCRIPTION_DIRECT_SESSION_ALIAS_DESCRIPTION,
            ),
        ],
        package_alias: Annotated[
            str,
            Field(
                pattern=r"^data_[0-9]{3,}$",
                description=_tool_docs.DATA_GET_PACKAGE_DESCRIPTION_PACKAGE_ALIAS_DESCRIPTION,
            ),
        ],
    ) -> PackageDescriptionResult:
        try:
            return await application.get_package_description(
                direct_session_alias,
                package_alias,
            )
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None


def register_data_describe_package(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    @server.tool(
        name="data_describe_package",
        description=(_tool_docs.DATA_DESCRIBE_PACKAGE_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    async def data_describe_package(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                description=_tool_docs.DATA_GET_PACKAGE_DESCRIPTION_DIRECT_SESSION_ALIAS_DESCRIPTION,
            ),
        ],
        package_alias: Annotated[
            str,
            Field(
                pattern=r"^data_[0-9]{3,}$",
                description=_tool_docs.DATA_DESCRIBE_PACKAGE_PACKAGE_ALIAS_DESCRIPTION,
            ),
        ],
        payload_name: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=120,
                description=(_tool_docs.DATA_DESCRIBE_PACKAGE_PAYLOAD_NAME_DESCRIPTION),
            ),
        ] = None,
    ) -> DirectDataDescribePackageResult:
        try:
            return await application.describe_package(
                direct_session_alias,
                package_alias,
                payload_name,
            )
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None
