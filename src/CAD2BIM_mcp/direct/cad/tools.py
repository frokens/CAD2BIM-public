"""Direct tool registrations owned by this capability family."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from pydantic import Field, ValidationError

from CAD2BIM_backend.domain.cad_measurement import (
    CadToleranceAnalysisResult,
)

from CAD2BIM_backend.domain.data_query import (
    CadGetEntityResult,
    CadQueryEntitiesResult,
    CadQueryXdataResult,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.server_support import _validated_query
from CAD2BIM_mcp.direct.protocol import DirectDataMcpApplication
from CAD2BIM_mcp.direct.provider_schema import (
    _apply_query_cursor_xor_schema,
    _object_union_output_schema,
    compact_provider_input_schema,
    provider_output_schema,
)
from CAD2BIM_mcp.direct.transform.contracts import (
    MultiOutputDataTransformResult,
)
from CAD2BIM_mcp.direct.transform.inputs import (
    OutputPackageDeclaration,
)
from CAD2BIM_mcp.direct.cad.measurement_inputs import (
    DirectCadAnalyzeToleranceCandidatesInput,
    DirectCadComputeMeasurementsInput,
    CadLayerNameInput,
    DirectCadPointGroup,
    DirectCadPointScope,
    DirectCadToleranceSpec,
    DirectCadToleranceTrial,
    to_domain_cad_analyze_tolerance_candidates_command,
    to_domain_cad_compute_measurements_command,
)
from CAD2BIM_mcp.direct.query.cad_inputs import (
    CadEntityToolQuery,
    CadEntitiesToolQuery,
    CadXdataToolQuery,
    to_domain_cad_entity_query,
    to_domain_cad_entities_query,
    to_domain_cad_xdata_query,
)
from CAD2BIM_mcp.tool_docs import cad as _tool_docs
from CAD2BIM_mcp.tool_docs import common as _common_tool_docs


def register_cad_analyze_tolerance_candidates(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def cad_analyze_tolerance_candidates(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
            ),
        ],
        package_alias: Annotated[
            str,
            Field(
                pattern=r"^data_[0-9]{3,}$",
                max_length=32,
                description=(_tool_docs.CAD_ANALYZE_TOLERANCE_CANDIDATES_PACKAGE_ALIAS_DESCRIPTION),
            ),
        ],
        source_unit_to_m: Annotated[
            float,
            Field(
                strict=True,
                gt=0,
                description=(
                    _tool_docs.CAD_ANALYZE_TOLERANCE_CANDIDATES_SOURCE_UNIT_TO_M_DESCRIPTION
                ),
            ),
        ],
        path_layers: Annotated[
            tuple[CadLayerNameInput, ...],
            Field(
                min_length=1,
                max_length=100,
                description=(_tool_docs.CAD_ANALYZE_TOLERANCE_CANDIDATES_PATH_LAYERS_DESCRIPTION),
            ),
        ],
        point_groups: Annotated[
            tuple[DirectCadPointGroup, ...],
            Field(
                max_length=16,
                description=(_tool_docs.CAD_ANALYZE_TOLERANCE_CANDIDATES_POINT_GROUPS_DESCRIPTION),
            ),
        ] = (),
        trials: Annotated[
            tuple[DirectCadToleranceTrial, ...],
            Field(
                max_length=8,
                description=(_tool_docs.CAD_ANALYZE_TOLERANCE_CANDIDATES_TRIALS_DESCRIPTION),
            ),
        ] = (),
    ) -> CadToleranceAnalysisResult:
        try:
            request = DirectCadAnalyzeToleranceCandidatesInput(
                direct_session_alias=direct_session_alias,
                package_alias=package_alias,
                source_unit_to_m=source_unit_to_m,
                path_layers=path_layers,
                point_groups=point_groups,
                trials=trials,
            )
            return await application.analyze_tolerance_candidates(
                request.direct_session_alias,
                to_domain_cad_analyze_tolerance_candidates_command(request),
            )
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "The tolerance analysis request is invalid",
                    phase="preflight",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    analyzer_tool = FunctionTool.from_function(
        cad_analyze_tolerance_candidates,
        name="cad_analyze_tolerance_candidates",
        description=_tool_docs.CAD_ANALYZE_TOLERANCE_CANDIDATES_DESCRIPTION,
        output_schema=provider_output_schema(CadToleranceAnalysisResult, compact=True),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    server.add_tool(analyzer_tool)


def register_cad_compute_measurements(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def cad_compute_measurements(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                max_length=32,
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_DESCRIPTION,
            ),
        ],
        package_alias: Annotated[
            str,
            Field(
                pattern=r"^data_[0-9]{3,}$",
                max_length=32,
                description=(_tool_docs.CAD_ANALYZE_TOLERANCE_CANDIDATES_PACKAGE_ALIAS_DESCRIPTION),
            ),
        ],
        source_unit_to_m: Annotated[
            float,
            Field(
                strict=True,
                gt=0,
                description=(
                    _tool_docs.CAD_ANALYZE_TOLERANCE_CANDIDATES_SOURCE_UNIT_TO_M_DESCRIPTION
                ),
            ),
        ],
        path_layers: Annotated[
            tuple[CadLayerNameInput, ...],
            Field(
                min_length=1,
                max_length=100,
                description=(_tool_docs.CAD_ANALYZE_TOLERANCE_CANDIDATES_PATH_LAYERS_DESCRIPTION),
            ),
        ],
        tolerance_spec: Annotated[
            DirectCadToleranceSpec,
            Field(description=(_tool_docs.CAD_COMPUTE_MEASUREMENTS_TOLERANCE_SPEC_DESCRIPTION)),
        ],
        output_package: Annotated[
            OutputPackageDeclaration,
            Field(description=(_tool_docs.CAD_COMPUTE_MEASUREMENTS_OUTPUT_PACKAGE_DESCRIPTION)),
        ],
        point_scopes: Annotated[
            tuple[DirectCadPointScope, ...],
            Field(
                max_length=16,
                description=_tool_docs.CAD_COMPUTE_MEASUREMENTS_POINT_SCOPES_DESCRIPTION,
            ),
        ] = (),
    ) -> MultiOutputDataTransformResult:
        try:
            request = DirectCadComputeMeasurementsInput(
                direct_session_alias=direct_session_alias,
                package_alias=package_alias,
                source_unit_to_m=source_unit_to_m,
                path_layers=path_layers,
                point_scopes=point_scopes,
                tolerance_spec=tolerance_spec,
                output_package=output_package,
            )
            return await application.compute_measurements(
                request.direct_session_alias,
                to_domain_cad_compute_measurements_command(request),
            )
        except ValidationError as exc:
            raise _tool_error(
                DirectApplicationError(
                    "INVALID_ARGUMENT",
                    "The CAD measurement request is invalid",
                    phase="preflight",
                )
            ) from exc
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    compute_measurements_tool = FunctionTool.from_function(
        cad_compute_measurements,
        name="cad_compute_measurements",
        description=_tool_docs.CAD_COMPUTE_MEASUREMENTS_DESCRIPTION,
        output_schema=provider_output_schema(
            MultiOutputDataTransformResult,
            compact=False,
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    )
    server.add_tool(compute_measurements_tool)


def register_cad_query_entities(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def cad_query_entities(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION,
            ),
        ],
        query: Annotated[
            CadEntitiesToolQuery | None,
            Field(description=(_tool_docs.CAD_QUERY_ENTITIES_QUERY_DESCRIPTION)),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(
                pattern=r"^cur\..+$",
                max_length=16_384,
                description=(_tool_docs.CAD_QUERY_ENTITIES_CURSOR_DESCRIPTION),
            ),
        ] = None,
    ) -> CadQueryEntitiesResult:
        try:
            domain_query = _validated_query(
                query,
                to_domain_cad_entities_query,
                "cad_query_entities",
            )
            return await application.query_entities(direct_session_alias, domain_query, cursor)
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    cad_query_entities_tool = FunctionTool.from_function(
        cad_query_entities,
        name="cad_query_entities",
        description=(_tool_docs.CAD_QUERY_ENTITIES_DESCRIPTION),
        output_schema=_object_union_output_schema(CadQueryEntitiesResult),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    _apply_query_cursor_xor_schema(cad_query_entities_tool)
    cad_query_entities_tool.parameters = compact_provider_input_schema(
        cad_query_entities_tool.parameters
    )
    server.add_tool(cad_query_entities_tool)


def register_cad_get_entity(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def cad_get_entity(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                description=_tool_docs.CAD_GET_ENTITY_DIRECT_SESSION_ALIAS_DESCRIPTION,
            ),
        ],
        query: Annotated[
            CadEntityToolQuery | None,
            Field(description=_tool_docs.CAD_GET_ENTITY_QUERY_DESCRIPTION),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(
                pattern=r"^cur\..+$",
                max_length=16_384,
                description=_tool_docs.CAD_GET_ENTITY_CURSOR_DESCRIPTION,
            ),
        ] = None,
    ) -> CadGetEntityResult:
        try:
            domain_query = _validated_query(
                query,
                to_domain_cad_entity_query,
                "cad_get_entity",
            )
            return await application.get_entity(direct_session_alias, domain_query, cursor)
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    cad_get_entity_tool = FunctionTool.from_function(
        cad_get_entity,
        name="cad_get_entity",
        description=(_tool_docs.CAD_GET_ENTITY_DESCRIPTION),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    _apply_query_cursor_xor_schema(cad_get_entity_tool)
    server.add_tool(cad_get_entity_tool)


def register_cad_query_xdata(
    server: FastMCP,
    application: DirectDataMcpApplication,
) -> None:
    async def cad_query_xdata(
        direct_session_alias: Annotated[
            str,
            Field(
                pattern=r"^direct_[0-9]{3,}$",
                description=_common_tool_docs.DIRECT_SESSION_ALIAS_SHORT_DESCRIPTION,
            ),
        ],
        query: Annotated[
            CadXdataToolQuery | None,
            Field(description=(_tool_docs.CAD_QUERY_XDATA_QUERY_DESCRIPTION)),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(
                pattern=r"^cur\..+$",
                max_length=16_384,
                description=(_tool_docs.CAD_QUERY_ENTITIES_CURSOR_DESCRIPTION),
            ),
        ] = None,
    ) -> CadQueryXdataResult:
        try:
            domain_query = _validated_query(
                query,
                to_domain_cad_xdata_query,
                "cad_query_xdata",
            )
            return await application.query_xdata(direct_session_alias, domain_query, cursor)
        except DirectApplicationError as exc:
            raise _tool_error(exc) from None

    cad_query_xdata_tool = FunctionTool.from_function(
        cad_query_xdata,
        name="cad_query_xdata",
        description=(_tool_docs.CAD_QUERY_XDATA_DESCRIPTION),
        output_schema=_object_union_output_schema(CadQueryXdataResult, compact=True),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    )
    _apply_query_cursor_xor_schema(cad_query_xdata_tool)
    server.add_tool(cad_query_xdata_tool)
