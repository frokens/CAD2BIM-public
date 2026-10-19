from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from fastmcp import Client
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from CAD2BIM_backend.domain.cad_measurement import (
    CAD_MEASUREMENT_COMPUTE_BUDGET,
    CAD_MEASUREMENT_PAYLOAD_NAMES,
    CAD_TOLERANCE_ANALYZER_BUDGET,
    CadAnalyzeToleranceCandidatesCommand,
    CadComputeMeasurementsCommand,
    CadMeasurementErrorCode,
    CadToleranceAnalysisResult,
)
from CAD2BIM_mcp.direct.cad.contracts import (
    CadMeasurementOperationDetail,
)
from CAD2BIM_mcp.direct.catalog.contracts import (
    DataPackagePrimaryResult,
)
from CAD2BIM_mcp.direct.transform.contracts import (
    FailedCadMeasurementTransformationCodeResult,
    MultiOutputDataTransformResult,
    ParquetOperationOutputDeclaration,
    SucceededCadMeasurementTransformationCodeResult,
)
from CAD2BIM_mcp.direct.operation.contracts import (
    FailedDataTransformOperationCard,
    FailedOperationGetResult,
    OperationErrorDetail,
    SucceededDataTransformOperationCard,
    SucceededOperationGetResult,
    SourceIngestionOperationDetail,
    SourceIngestionSourceDetail,
)
from CAD2BIM_mcp.direct.errors import DirectApplicationError, _tool_error
from CAD2BIM_mcp.direct.cad.measurement_inputs import (
    CAD_ANALYZE_TOLERANCE_CANDIDATES_DESCRIPTION,
    CAD_COMPUTE_MEASUREMENTS_DESCRIPTION,
    DirectCadAnalyzeToleranceCandidatesInput,
    DirectCadComputeMeasurementsInput,
    to_domain_cad_analyze_tolerance_candidates_command,
    to_domain_cad_compute_measurements_command,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp


_FORBIDDEN_SCHEMA_KEYS = {"allOf", "if", "then", "else", "not"}


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _walk(child)


def _tolerance() -> dict[str, float]:
    return {
        "endpoint_quantization_m": 0.0001,
        "endpoint_snap_candidate_m": 0.01,
        "endpoint_on_path_candidate_m": 0.02,
        "host_endpoint_guard_m": 0.005,
        "point_target_uniqueness_margin_m": 0.01,
        "straight_turn_tolerance_deg": 1.0,
    }


def _common() -> dict[str, object]:
    return {
        "direct_session_alias": "direct_001",
        "package_alias": "data_001",
        "source_unit_to_m": 0.001,
        "path_layers": ["PIPE_MAIN", "PIPE_BRANCH"],
    }


def _point_groups() -> list[dict[str, object]]:
    return [{"scope_key": "device_points", "point_layers": ["VALVE", "METER"]}]


def _point_scopes() -> list[dict[str, object]]:
    return [
        {
            "scope_key": "device_points",
            "point_layers": ["VALVE", "METER"],
            "search_radius_m": 0.2,
        }
    ]


def _trial(trial_key: str = "balanced") -> dict[str, object]:
    return {
        "trial_key": trial_key,
        "tolerance_spec": _tolerance(),
        "point_search_radii": [{"scope_key": "device_points", "search_radius_m": 0.2}],
    }


def _output_package() -> dict[str, str]:
    return {
        "name": "gas_geometry_measurements",
        "description_markdown": (
            "# Gas geometry measurements\n"
            "## Summary\nFacts.\n"
            "## Intended use\nInvestigation.\n"
            "## Contents\nTen payloads.\n"
            "## Method\nM3.\n"
            "## Scope and completeness\nSelected layers.\n"
            "## Known limitations\nStraight segments only.\n"
        ),
    }


def test_direct_m3_inputs_convert_to_entry_neutral_domain_commands() -> None:
    analyze_payload = {
        **_common(),
        "point_groups": _point_groups(),
        "trials": [_trial()],
    }
    direct_analyze = DirectCadAnalyzeToleranceCandidatesInput.model_validate(analyze_payload)
    analyze = to_domain_cad_analyze_tolerance_candidates_command(direct_analyze)
    assert isinstance(analyze, CadAnalyzeToleranceCandidatesCommand)
    assert not hasattr(analyze, "direct_session_alias")
    assert analyze.package_alias == "data_001"
    assert analyze.trials[0].tolerance_spec.endpoint_snap_candidate_m == 0.01
    assert analyze.point_groups[0].scope_key == "device_points"
    assert analyze.trials[0].point_search_radii[0].search_radius_m == 0.2

    compute_payload = {
        **_common(),
        "point_scopes": _point_scopes(),
        "tolerance_spec": _tolerance(),
        "output_package": _output_package(),
    }
    direct_compute = DirectCadComputeMeasurementsInput.model_validate(compute_payload)
    compute = to_domain_cad_compute_measurements_command(direct_compute)
    assert isinstance(compute, CadComputeMeasurementsCommand)
    assert not hasattr(compute, "direct_session_alias")
    assert compute.output_package.name == "gas_geometry_measurements"


def test_zero_trials_and_zero_point_groups_are_explicitly_legal() -> None:
    payload = _common()
    payload["point_groups"] = []
    request = DirectCadAnalyzeToleranceCandidatesInput.model_validate(payload)
    assert request.point_groups == ()
    assert request.trials == ()


@pytest.mark.parametrize(
    "point_search_radii",
    [
        [],
        [{"scope_key": "other_points", "search_radius_m": 0.2}],
        [
            {"scope_key": "device_points", "search_radius_m": 0.2},
            {"scope_key": "other_points", "search_radius_m": 0.3},
        ],
    ],
)
def test_every_analyzer_trial_radius_scope_set_must_match_point_groups(
    point_search_radii: list[dict[str, object]],
) -> None:
    payload = {
        **_common(),
        "point_groups": _point_groups(),
        "trials": [{**_trial(), "point_search_radii": point_search_radii}],
    }
    with pytest.raises(ValidationError, match="must exactly match point_groups"):
        DirectCadAnalyzeToleranceCandidatesInput.model_validate(payload)


def test_analyzer_trial_cannot_omit_point_search_radii() -> None:
    trial = _trial()
    trial.pop("point_search_radii")
    with pytest.raises(ValidationError, match="point_search_radii"):
        DirectCadAnalyzeToleranceCandidatesInput.model_validate(
            {**_common(), "point_groups": _point_groups(), "trials": [trial]}
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(source_unit_to_m=0.0), "greater than 0"),
        (lambda value: value.update(path_layers=[]), "at least 1"),
        (lambda value: value.update(path_layers=["A", "A"]), "must be unique"),
        (
            lambda value: value.update(
                point_groups=[{"scope_key": "Bad-Key", "point_layers": ["P"]}]
            ),
            "string_pattern_mismatch",
        ),
        (
            lambda value: value.update(
                trials=[
                    _trial("same"),
                    _trial("same"),
                ]
            ),
            "must be unique",
        ),
    ],
)
def test_domain_constraints_are_enforced_at_provider_boundary(mutation, message: str) -> None:
    value = _common()
    value["point_groups"] = _point_groups()
    value["trials"] = []
    mutation(value)
    with pytest.raises(ValidationError, match=message):
        DirectCadAnalyzeToleranceCandidatesInput.model_validate(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint_quantization_m", 0.0),
        ("endpoint_snap_candidate_m", 0.0),
        ("endpoint_on_path_candidate_m", 0.0),
        ("host_endpoint_guard_m", -0.1),
        ("point_target_uniqueness_margin_m", -0.1),
        ("straight_turn_tolerance_deg", 180.1),
    ],
)
def test_complete_tolerance_spec_rejects_each_invalid_boundary(field: str, value: float) -> None:
    payload = {
        **_common(),
        "point_scopes": _point_scopes(),
        "tolerance_spec": _tolerance(),
        "output_package": _output_package(),
    }
    payload["tolerance_spec"][field] = value
    with pytest.raises(ValidationError):
        DirectCadComputeMeasurementsInput.model_validate(payload)


def test_provider_input_schemas_are_recursive_safe_closed_and_complete() -> None:
    expected = {
        DirectCadAnalyzeToleranceCandidatesInput: {
            "direct_session_alias",
            "package_alias",
            "source_unit_to_m",
            "path_layers",
            "point_groups",
            "trials",
        },
        DirectCadComputeMeasurementsInput: {
            "direct_session_alias",
            "package_alias",
            "source_unit_to_m",
            "path_layers",
            "point_scopes",
            "tolerance_spec",
            "output_package",
        },
    }
    for model, expected_fields in expected.items():
        schema = model.model_json_schema()
        serialized = json.dumps(schema, sort_keys=True).lower()
        assert set(schema["properties"]) == expected_fields
        assert schema["additionalProperties"] is False
        assert "unknown" not in serialized
        assert '"additionalproperties": {}' not in serialized
        for node in _walk(schema):
            if isinstance(node, Mapping):
                assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
                if node.get("type") == "object" and "properties" in node:
                    assert node.get("additionalProperties") is False
        assert all(item.get("description") for item in schema["properties"].values())

        scope_field = (
            "point_groups" if model is DirectCadAnalyzeToleranceCandidatesInput else "point_scopes"
        )
        scopes = schema["properties"][scope_field]
        scope_schema = schema["$defs"][scopes["items"]["$ref"].rsplit("/", 1)[-1]]
        expected_scope_fields = {"scope_key", "point_layers"}
        if model is DirectCadComputeMeasurementsInput:
            expected_scope_fields.add("search_radius_m")
        assert set(scope_schema["properties"]) == expected_scope_fields
        assert all(item.get("description") for item in scope_schema["properties"].values())
        assert scope_schema["properties"]["point_layers"]["items"] == {
            "maxLength": 255,
            "minLength": 1,
            "type": "string",
        }
        assert schema["properties"]["path_layers"]["items"] == {
            "maxLength": 255,
            "minLength": 1,
            "type": "string",
        }

    analyze_schema = DirectCadAnalyzeToleranceCandidatesInput.model_json_schema()
    trial_ref = analyze_schema["properties"]["trials"]["items"]["$ref"]
    trial = analyze_schema["$defs"][trial_ref.rsplit("/", 1)[-1]]
    assert set(trial["properties"]) == {
        "trial_key",
        "tolerance_spec",
        "point_search_radii",
    }
    radius_ref = trial["properties"]["point_search_radii"]["items"]["$ref"]
    radius = analyze_schema["$defs"][radius_ref.rsplit("/", 1)[-1]]
    assert set(radius["properties"]) == {"scope_key", "search_radius_m"}
    tolerance_ref = trial["properties"]["tolerance_spec"]["$ref"]
    tolerance = analyze_schema["$defs"][tolerance_ref.rsplit("/", 1)[-1]]
    assert set(tolerance["properties"]) == set(_tolerance())
    assert all(item.get("description") for item in tolerance["properties"].values())

    compute_schema = DirectCadComputeMeasurementsInput.model_json_schema()
    assert "outputs" not in compute_schema["properties"]


def test_analyzer_response_is_closed_and_bounded() -> None:
    schema = TypeAdapter(CadToleranceAnalysisResult).json_schema(mode="serialization")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["distributions"]["maxItems"] == 32
    assert schema["properties"]["generated_probes"]["maxItems"] == 64
    assert schema["properties"]["trial_results"]["maxItems"] == 8
    assert schema["properties"]["diagnostic_samples"]["maxItems"] == 32
    for node in _walk(schema):
        if isinstance(node, Mapping):
            assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False


def _operation_outputs() -> tuple[ParquetOperationOutputDeclaration, ...]:
    return tuple(
        ParquetOperationOutputDeclaration(
            payload_name=name,
            format="parquet",
            description=f"{name} facts",
            logical_key_fields=(f"{name}_key",),
        )
        for name in CAD_MEASUREMENT_PAYLOAD_NAMES
    )


def test_measurement_operation_and_code_contracts_cover_fixed_server_outputs() -> None:
    detail = CadMeasurementOperationDetail(
        detail_kind="cad_measurement",
        engine="cad_measurement",
        source_package_alias="data_001",
        source_unit_to_m=0.001,
        path_layers=("PIPE",),
        point_scopes=(),
        tolerance_spec=_tolerance(),
        outputs=_operation_outputs(),
        output_package_name="measurements",
        code_available=True,
    )
    assert tuple(item.payload_name for item in detail.outputs) == CAD_MEASUREMENT_PAYLOAD_NAMES

    reversed_outputs = tuple(reversed(_operation_outputs()))
    with pytest.raises(ValidationError, match="fixed ten Payloads in order"):
        detail.model_copy(update={"outputs": reversed_outputs}, deep=True).__class__.model_validate(
            {**detail.model_dump(mode="python"), "outputs": reversed_outputs}
        )

    code = "def compute_measurements():\n    pass\n"
    common = {
        "operation_alias": "op_001",
        "size_bytes": len(code.encode("utf-8")),
        "code": code,
        "engine": "cad_measurement",
        "language": "python",
        "media_type": "text/x-python",
    }
    succeeded = SucceededCadMeasurementTransformationCodeResult(
        **common,
        operation_outcome="succeeded",
        result_package_alias="data_002",
    )
    failed = FailedCadMeasurementTransformationCodeResult(
        **common,
        operation_outcome="failed",
    )
    assert succeeded.engine == failed.engine == "cad_measurement"


def test_operation_get_pairs_measurement_tool_name_with_measurement_detail_kind() -> None:
    now = datetime.now(timezone.utc)
    card = SucceededDataTransformOperationCard(
        operation_alias="op_001",
        sequence=1,
        initiator="direct_client",
        intent_summary="Deterministic CAD measurements",
        started_at=now,
        finished_at=now,
        operation_kind="data_transform",
        tool_name="cad_compute_measurements",
        outcome="succeeded",
        primary_result=DataPackagePrimaryResult(
            result_kind="data_package",
            package_alias="data_002",
            package_name="measurements",
        ),
    )
    detail = CadMeasurementOperationDetail(
        detail_kind="cad_measurement",
        engine="cad_measurement",
        source_package_alias="data_001",
        source_unit_to_m=0.001,
        path_layers=("PIPE",),
        point_scopes=(),
        tolerance_spec=_tolerance(),
        outputs=_operation_outputs(),
        output_package_name="measurements",
        code_available=True,
    )
    result = SucceededOperationGetResult(operation=card, detail=detail)
    assert result.detail.detail_kind == "cad_measurement"

    wrong_detail = SourceIngestionOperationDetail(
        detail_kind="source_ingestion",
        source=SourceIngestionSourceDetail(
            filename="source.dxf",
            media_type="application/dxf",
            size_bytes=1,
        ),
        code_available=False,
    )
    with pytest.raises(ValidationError, match="tool_name and detail kind"):
        SucceededOperationGetResult(operation=card, detail=wrong_detail)

    failed_card = FailedDataTransformOperationCard(
        operation_alias="op_002",
        sequence=2,
        initiator="direct_client",
        intent_summary="Deterministic CAD measurements",
        started_at=now,
        finished_at=now,
        operation_kind="data_transform",
        tool_name="cad_compute_measurements",
        outcome="failed",
        error_code="WORKER_FAILED",
    )
    error = OperationErrorDetail(
        code="WORKER_FAILED",
        phase="execution",
        message="Measurement worker failed",
        retryable=False,
    )
    failed = FailedOperationGetResult(operation=failed_card, detail=detail, error=error)
    assert failed.detail.detail_kind == "cad_measurement"
    with pytest.raises(ValidationError, match="tool_name and detail kind"):
        FailedOperationGetResult(operation=failed_card, detail=wrong_detail, error=error)


def test_compute_reuses_multi_output_data_transform_response() -> None:
    result = MultiOutputDataTransformResult(
        operation_alias="op_001",
        package_alias="data_002",
        package_name="measurements",
        outputs=tuple(
            {
                "payload_name": name,
                "format": "parquet",
                "row_count": 0,
                "logical_key_fields": [f"{name}_key"],
            }
            for name in CAD_MEASUREMENT_PAYLOAD_NAMES
        ),
    )
    assert len(result.outputs) == 10


def test_m3_descriptions_explain_sources_scope_zero_trials_and_decision_boundaries() -> None:
    analyze_fragments = (
        "one exact source_l0",
        "data_package_list",
        "cad_query_entities",
        "Requirement, user approval, CAD fact",
        "separate calls",
        "no filter objects or AND/OR",
        "point_groups",
        "point_search_radii",
        "exactly the same scope_key set",
        "Zero trials",
        "not hidden defaults",
        "not compute-ready",
        "Do not pass them unchanged",
        "cad_compute_measurements",
        "edge-collapse",
        "blocker diagnostics",
        "no cursor, Package or Operation",
        "2,000,000 L0 rows",
        "response is limited to 256 KiB",
        "490s total",
        "PACKAGE_KIND_MISMATCH",
        "CAD_MEASUREMENT_INPUT_BUDGET_EXCEEDED",
        "single line of at most 2,000 UTF-8 bytes",
        "not business topology, CAD classification or IFC decisions",
    )
    compute_fragments = (
        "one exact source_l0",
        "source_unit_to_m",
        "separate calls",
        "no filter objects or AND/OR",
        "exactly one complete tolerance_spec",
        "no outputs declaration",
        "ten Parquet Payloads",
        "whole synchronous call without partial publication",
        "2,000,000 generated candidates",
        "rather than truncating candidates",
        "response is limited to 64 KiB",
        "550s total",
        "CAD_ENDPOINT_QUANTIZATION_COLLAPSE",
        "PUBLICATION_UNCERTAIN",
        "not topology nodes, product categories or IFC decisions",
    )
    for fragment in analyze_fragments:
        assert fragment in CAD_ANALYZE_TOLERANCE_CANDIDATES_DESCRIPTION
    for fragment in compute_fragments:
        assert fragment in CAD_COMPUTE_MEASUREMENTS_DESCRIPTION


def test_m3_server_owned_budgets_and_error_codes_are_exact() -> None:
    analyzer = CAD_TOLERANCE_ANALYZER_BUDGET
    assert (
        analyzer.total_timeout_seconds,
        analyzer.worker_wall_seconds,
        analyzer.worker_cpu_seconds,
        analyzer.worker_rss_bytes,
        analyzer.worker_scratch_bytes,
        analyzer.worker_threads,
        analyzer.max_concurrent_workers,
        analyzer.capacity_wait_seconds,
        analyzer.response_bytes,
        analyzer.warm_p95_seconds,
    ) == (490, 90, 180, 4 * 1024**3, 4 * 1024**3, 2, 1, 360, 256 * 1024, 30)
    assert (analyzer.input_rows, analyzer.input_parquet_bytes, analyzer.input_arrow_bytes) == (
        2_000_000,
        256 * 1024**2,
        512 * 1024**2,
    )

    compute = CAD_MEASUREMENT_COMPUTE_BUDGET
    assert (
        compute.total_timeout_seconds,
        compute.worker_wall_seconds,
        compute.worker_cpu_seconds,
        compute.response_bytes,
        compute.warm_p95_seconds,
    ) == (550, 150, 300, 64 * 1024, 60)
    assert (
        compute.output_rows,
        compute.output_arrow_bytes,
        compute.output_parquet_bytes,
        compute.output_payload_parquet_bytes,
        compute.candidate_count,
    ) == (2_000_000, 512 * 1024**2, 256 * 1024**2, 100 * 1024**2, 2_000_000)
    assert {item.value for item in CadMeasurementErrorCode} == {
        "INVALID_ARGUMENT",
        "DIRECT_SESSION_NOT_FOUND",
        "PACKAGE_NOT_FOUND",
        "PACKAGE_KIND_MISMATCH",
        "PACKAGE_INTEGRITY_ERROR",
        "CAD_LAYER_NOT_FOUND",
        "CAD_PATH_SCOPE_EMPTY",
        "CAD_MEASUREMENT_INPUT_BUDGET_EXCEEDED",
        "CAD_GEOMETRY_UNSUPPORTED",
        "CAD_NONZERO_BULGE_UNSUPPORTED",
        "CAD_GEOMETRY_NONFINITE",
        "CAD_ZERO_LENGTH_EDGE",
        "CAD_ENDPOINT_QUANTIZATION_RANGE_EXCEEDED",
        "CAD_ENDPOINT_QUANTIZATION_COLLAPSE",
        "CAD_MEASUREMENT_CANDIDATE_BUDGET_EXCEEDED",
        "CAPACITY_BUSY",
        "TIMEOUT",
        "RESOURCE_LIMIT",
        "WORKER_FAILED",
        "OUTPUT_INVALID",
        "OUTPUT_BUDGET_EXCEEDED",
        "SERVICE_UNAVAILABLE",
        "PUBLICATION_UNCERTAIN",
    }


def test_tool_error_is_one_line_and_bounded_in_utf8_bytes() -> None:
    error = DirectApplicationError(
        "WORKER_FAILED",
        "first line\nsecond line\r\n" + ("错" * 2_000),
        phase="execution",
        retryable=False,
    )

    rendered = str(_tool_error(error))

    assert "\n" not in rendered and "\r" not in rendered
    assert len(rendered.encode("utf-8")) <= 2_000


async def test_fastmcp_exposes_recursive_provider_safe_m3_schemas_and_descriptions() -> None:
    server = create_direct_mcp(Mock())
    registered = {tool.name: tool for tool in await server.list_tools()}

    async with Client(server) as client:
        provider = {tool.name: tool for tool in await client.list_tools()}

    expected = {
        "cad_analyze_tolerance_candidates": CAD_ANALYZE_TOLERANCE_CANDIDATES_DESCRIPTION,
        "cad_compute_measurements": CAD_COMPUTE_MEASUREMENTS_DESCRIPTION,
    }
    for name, description in expected.items():
        tool = registered[name]
        Draft202012Validator.check_schema(tool.parameters)
        assert provider[name].inputSchema == tool.parameters
        assert provider[name].outputSchema == tool.output_schema
        assert provider[name].description == description == tool.description
        for node in _walk(tool.parameters):
            if isinstance(node, Mapping):
                assert not (_FORBIDDEN_SCHEMA_KEYS & set(node))
                if node.get("type") == "object" and "properties" in node:
                    assert node.get("additionalProperties") is False

        scope_field = (
            "point_groups" if name == "cad_analyze_tolerance_candidates" else "point_scopes"
        )
        scopes = tool.parameters["properties"][scope_field]
        assert scopes["maxItems"] == 16
        expected_scope_fields = {"scope_key", "point_layers"}
        if name == "cad_compute_measurements":
            expected_scope_fields.add("search_radius_m")
        assert set(scopes["items"]["properties"]) == expected_scope_fields
        assert scopes["items"]["additionalProperties"] is False
        assert tool.parameters["properties"]["path_layers"]["items"]["maxLength"] == 255

    analyzer = registered["cad_analyze_tolerance_candidates"]
    assert "whole-network spacing" in analyzer.description
    assert "not topology-safe tolerances" in analyzer.description
    assert "POINT groups separately" in analyzer.description
    assert analyzer.parameters["properties"]["trials"]["maxItems"] == 8
    trial_properties = analyzer.parameters["properties"]["trials"]["items"]["properties"]
    assert set(trial_properties) == {
        "trial_key",
        "tolerance_spec",
        "point_search_radii",
    }
    assert set(trial_properties["point_search_radii"]["items"]["properties"]) == {
        "scope_key",
        "search_radius_m",
    }
    assert analyzer.output_schema["properties"]["generated_probes"]["maxItems"] == 64

    compute = registered["cad_compute_measurements"]
    assert "raw_candidate_rank" in compute.description
    assert "distinct_target_rank" in compute.description
    assert "may still return several rows" in compute.description
    assert "outputs" not in compute.parameters["properties"]
    assert tuple(compute.output_schema["properties"]) == (
        "operation_alias",
        "package_alias",
        "package_name",
        "outputs",
    )
