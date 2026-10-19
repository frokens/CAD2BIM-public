from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.data_transform import (
    DataTransformCommand,
    DataTransformWorkerError,
    DataTransformWorkerParameter,
    DataTransformWorkerPolicy,
    DataTransformWorkerRequest,
    DuckDbSqlExecution,
    IntegerParameter,
    OutputPackageDeclaration,
    ParquetOutputDeclaration,
    StringParameter,
    TransformInputBinding,
)
from CAD2BIM_backend.infrastructure.data_transform import (
    SubprocessDuckDBDataTransformWorker,
)
from CAD2BIM_backend.ports.data_transform import (
    DUCKDB_LOGICAL_TYPE_METADATA_KEY,
    ArrowTransformInput,
)


def _table() -> pa.Table:
    return pa.table(
        {
            "entity_key": ["ent_1", "ent_2", "ent_3"],
            "layer": ["燃气", "建筑", "燃气"],
            "diameter": [50, 80, 100],
        }
    )


def _inputs(*, name: str = "entities", table: pa.Table | None = None):
    return (ArrowTransformInput(name, (table or _table()).to_reader()),)


def _worker(
    *,
    capacity_root: Path | None = None,
    **changes: object,
) -> SubprocessDuckDBDataTransformWorker:
    defaults = {
        "wall_seconds": 5.0,
        "cpu_seconds": 10,
        "cleanup_seconds": 0.5,
    }
    defaults.update(changes)
    return SubprocessDuckDBDataTransformWorker(
        policy=DataTransformWorkerPolicy(**defaults),
        capacity_root=capacity_root,
    )


@pytest.mark.asyncio
async def test_worker_round_trips_arrow_and_binds_typed_parameter() -> None:
    result = await _worker().execute(
        DataTransformWorkerRequest(
            """
            WITH selected AS (
                SELECT entity_key, diameter
                FROM entities
                WHERE layer = $target_layer
            )
            SELECT entity_key, diameter FROM selected ORDER BY entity_key
            """,
            (DataTransformWorkerParameter("target_layer", "燃气"),),
        ),
        _inputs(),
    )

    assert len(result) == 1
    output = result[0]
    assert output.payload_name is None
    assert output.receipt.row_count == 2
    assert output.receipt.arrow_nbytes > 0
    assert output.schema.names == ["entity_key", "diameter"]
    assert output.schema.field("diameter").metadata == {DUCKDB_LOGICAL_TYPE_METADATA_KEY: b"BIGINT"}
    assert pa.Table.from_batches(output.batches).to_pylist() == [
        {"entity_key": "ent_1", "diameter": 50},
        {"entity_key": "ent_3", "diameter": 100},
    ]


@pytest.mark.asyncio
async def test_worker_publishes_an_empty_but_typed_arrow_result() -> None:
    result = await _worker().execute(
        DataTransformWorkerRequest(
            "SELECT entity_key, diameter FROM entities WHERE diameter > 1000"
        ),
        _inputs(),
    )

    assert len(result) == 1
    output = result[0]
    assert output.receipt.row_count == 0
    assert output.schema.names == ["entity_key", "diameter"]
    assert output.batches == ()


@pytest.mark.asyncio
async def test_worker_materializes_an_empty_inline_relation_with_explicit_schema() -> None:
    input_name = "inline_" + "x" * 113
    schema = pa.schema(
        [
            pa.field("trial_key", pa.string(), nullable=False),
            pa.field("radius", pa.float64(), nullable=True),
        ]
    )

    result = await _worker().execute(
        DataTransformWorkerRequest(f"SELECT trial_key, radius FROM {input_name}"),
        (
            ArrowTransformInput(
                input_name,
                pa.RecordBatchReader.from_batches(schema, []),
                input_format="inline",
            ),
        ),
    )

    assert len(input_name) == 120
    assert result[0].receipt.row_count == 0
    assert result[0].schema.names == ["trial_key", "radius"]
    assert result[0].schema.types == [pa.string(), pa.float64()]
    assert result[0].batches == ()

    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest("SELECT 1"),
            (
                ArrowTransformInput(
                    "inline_" + "x" * 114,
                    pa.RecordBatchReader.from_batches(schema, []),
                    input_format="inline",
                ),
            ),
        )

    assert rejected.value.code == "TRANSFORM_SQL_NOT_ALLOWED"
    assert rejected.value.phase == "preflight"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_worker_accepts_twenty_four_declared_arrow_relations() -> None:
    inputs = tuple(
        ArrowTransformInput(
            f"relation_{index:02d}",
            pa.table({"value": pa.array([index], type=pa.int64())}).to_reader(),
            input_format="inline" if index >= 16 else "parquet",
        )
        for index in range(24)
    )
    relation_list = ", ".join(f"relation_{index:02d}" for index in range(24))

    result = await _worker().execute(
        DataTransformWorkerRequest(
            f"SELECT relation_00.value AS value FROM {relation_list}"
        ),
        inputs,
    )

    assert result[0].receipt.row_count == 1
    assert pa.Table.from_batches(result[0].batches).to_pylist() == [{"value": 0}]


@pytest.mark.asyncio
async def test_worker_registers_json_document_as_a_duckdb_json_relation() -> None:
    inputs = (
        *_inputs(),
        ArrowTransformInput(
            "runtime_config",
            pa.table({"json_value": ['{"minimum":80}']}).to_reader(),
            input_format="json",
            json_kind="document",
        ),
    )

    result = await _worker().execute(
        DataTransformWorkerRequest(
            """
            SELECT entity_key, typeof(json_value) AS config_type
            FROM entities, runtime_config
            WHERE diameter >= CAST(json_extract(json_value, '$.minimum') AS BIGINT)
            ORDER BY entity_key
            """
        ),
        inputs,
    )

    assert pa.Table.from_batches(result[0].batches).to_pylist() == [
        {"entity_key": "ent_2", "config_type": "JSON"},
        {"entity_key": "ent_3", "config_type": "JSON"},
    ]


@pytest.mark.asyncio
async def test_worker_registers_json_records_with_stable_indices() -> None:
    records = pa.table(
        {
            "record_index": pa.array([1, 0], type=pa.int64()),
            "json_value": ['{"name":"second"}', '{"name":"first"}'],
        }
    )

    result = await _worker().execute(
        DataTransformWorkerRequest(
            """
            SELECT record_index,
                   json_extract_string(json_value, '$.name') AS name,
                   typeof(json_value) AS value_type
            FROM corrections ORDER BY record_index
            """
        ),
        (
            ArrowTransformInput(
                "corrections",
                records.to_reader(),
                input_format="json",
                json_kind="records",
            ),
        ),
    )

    assert pa.Table.from_batches(result[0].batches).to_pylist() == [
        {"record_index": 0, "name": "first", "value_type": "JSON"},
        {"record_index": 1, "name": "second", "value_type": "JSON"},
    ]


@pytest.mark.asyncio
async def test_worker_rejects_more_than_twenty_four_arrow_relations() -> None:
    inputs = tuple(
        ArrowTransformInput(
            f"relation_{index:02d}",
            pa.table({"value": [index]}).to_reader(),
        )
        for index in range(25)
    )

    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(DataTransformWorkerRequest("SELECT 1"), inputs)

    assert rejected.value.code == "TRANSFORM_SQL_NOT_ALLOWED"
    assert rejected.value.phase == "preflight"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_worker_executes_multi_statement_named_outputs_in_declaration_order() -> None:
    result = await _worker().execute(
        DataTransformWorkerRequest(
            """
            CREATE TEMP TABLE working AS
            SELECT entity_key, diameter FROM entities WHERE diameter >= $minimum;
            UPDATE working SET diameter = diameter + $increment;
            CREATE TEMP VIEW ports AS
            SELECT entity_key || '_port' AS port_key, diameter FROM working;
            CREATE TEMP TABLE products AS
            SELECT entity_key AS product_key, diameter FROM working;
            CREATE TEMP VIEW undeclared_summary AS SELECT count(*) AS row_count FROM working;
            """,
            (
                DataTransformWorkerParameter("minimum", 50),
                DataTransformWorkerParameter("increment", 1),
            ),
            result_mode="named_relations",
            output_names=("products", "ports"),
        ),
        _inputs(),
    )

    assert [item.payload_name for item in result] == ["products", "ports"]
    assert pa.Table.from_batches(result[0].batches).to_pylist() == [
        {"product_key": "ent_1", "diameter": 51},
        {"product_key": "ent_2", "diameter": 81},
        {"product_key": "ent_3", "diameter": 101},
    ]
    assert result[1].schema.names == ["port_key", "diameter"]


@pytest.mark.asyncio
async def test_named_relation_mode_keeps_single_select_m2a_compatibility() -> None:
    result = await _worker().execute(
        DataTransformWorkerRequest(
            "SELECT entity_key FROM entities ORDER BY entity_key",
            result_mode="named_relations",
            output_names=("selected_entities",),
        ),
        _inputs(),
    )

    assert len(result) == 1
    assert result[0].payload_name == "selected_entities"
    assert result[0].receipt.row_count == 3


@pytest.mark.asyncio
async def test_legacy_select_accepts_a_quoted_case_variant_of_its_input() -> None:
    result = await _worker().execute(
        DataTransformWorkerRequest('SELECT entity_key FROM "EnTiTiEs" ORDER BY entity_key'),
        _inputs(),
    )

    assert result[0].receipt.row_count == 3


@pytest.mark.asyncio
async def test_query_final_result_executes_script_once_and_returns_exact_logical_types() -> None:
    result = await _worker().execute(
        DataTransformWorkerRequest(
            """
            CREATE TEMP TABLE selected AS
            SELECT entity_key FROM entities WHERE diameter >= $minimum;
            SELECT entity_key, CAST('{"ok":true}' AS JSON) AS detail
            FROM selected ORDER BY entity_key;
            """,
            (DataTransformWorkerParameter("minimum", 80),),
            result_mode="query_final_result",
        ),
        _inputs(),
    )

    assert len(result) == 1
    output = result[0]
    assert output.payload_name is None
    assert output.receipt.row_count == 2
    assert output.schema.field("detail").metadata == {DUCKDB_LOGICAL_TYPE_METADATA_KEY: b"JSON"}


@pytest.mark.asyncio
@pytest.mark.parametrize("result_mode", ("named_relations", "query_final_result"))
@pytest.mark.parametrize(
    ("logical_type", "expression"),
    (
        ("UHUGEINT", f"CAST('{2**127 - 1}' AS UHUGEINT)"),
        ("BIGNUM", "CAST('1' AS BIGNUM)"),
        ("TIME WITH TIME ZONE", "CAST('12:34:56+05:30' AS TIMETZ)"),
        ("UHUGEINT", "[CAST('1' AS UHUGEINT)]"),
        ("BIGNUM", "struct_pack(value := CAST('1' AS BIGNUM))"),
        (
            "TIME WITH TIME ZONE",
            "struct_pack(value := CAST('12:34:56+05:30' AS TIMETZ))",
        ),
    ),
)
async def test_worker_rejects_unfaithful_duckdb_arrow_values(
    result_mode: str,
    logical_type: str,
    expression: str,
) -> None:
    if result_mode == "named_relations":
        code = (
            "CREATE TEMP TABLE unsafe_output AS "
            f"SELECT {expression} AS unsafe_value FROM entities LIMIT 1"
        )
        output_names = ("unsafe_output",)
    else:
        code = f"SELECT {expression} AS unsafe_value FROM entities LIMIT 1"
        output_names = ()

    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest(
                code,
                result_mode=result_mode,  # type: ignore[arg-type]
                output_names=output_names,
            ),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert rejected.value.phase == "output_validation"
    assert rejected.value.execution_started is True
    assert logical_type in str(rejected.value)
    assert "CAST" in str(rejected.value) and "AS VARCHAR" in str(rejected.value)


@pytest.mark.asyncio
async def test_worker_preserves_safe_structs_and_lists() -> None:
    result = await _worker().execute(
        DataTransformWorkerRequest(
            """
            SELECT {
                'BIGNUM': diameter,
                'items': [diameter, diameter + 1]
            } AS safe_value
            FROM entities ORDER BY entity_key LIMIT 1
            """,
            result_mode="query_final_result",
        ),
        _inputs(),
    )

    assert result[0].schema.field("safe_value").metadata == {
        DUCKDB_LOGICAL_TYPE_METADATA_KEY: b"STRUCT(BIGNUM BIGINT, items BIGINT[])"
    }
    value = pa.Table.from_batches(result[0].batches).to_pylist()[0]["safe_value"]
    assert value == {"BIGNUM": 50, "items": [50, 51]}


@pytest.mark.asyncio
async def test_named_outputs_support_the_approved_sixteen_output_boundary() -> None:
    output_names = tuple(f"output_{index:02d}" for index in range(16))
    code = "\n".join(
        ["CREATE TEMP TABLE seed AS SELECT entity_key FROM entities;"]
        + [f"CREATE TEMP VIEW {name} AS SELECT entity_key FROM seed;" for name in output_names]
    )

    result = await _worker().execute(
        DataTransformWorkerRequest(
            code,
            result_mode="named_relations",
            output_names=output_names,
        ),
        _inputs(),
    )

    assert tuple(item.payload_name for item in result) == output_names
    assert all(item.receipt.row_count == 3 for item in result)


@pytest.mark.asyncio
async def test_missing_named_output_relation_is_stable_output_validation_error() -> None:
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest(
                "CREATE TEMP TABLE other AS SELECT entity_key FROM entities",
                result_mode="named_relations",
                output_names=("products",),
            ),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_OUTPUT_RELATION_NOT_FOUND"
    assert rejected.value.phase == "output_validation"
    assert rejected.value.execution_started is True


@pytest.mark.asyncio
async def test_named_outputs_share_one_aggregate_row_budget() -> None:
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker(output_row_limit=3).execute(
            DataTransformWorkerRequest(
                """
                CREATE TEMP TABLE first_output AS
                SELECT entity_key FROM entities WHERE diameter >= 80;
                CREATE TEMP TABLE second_output AS
                SELECT entity_key FROM entities WHERE diameter <= 80;
                """,
                result_mode="named_relations",
                output_names=("first_output", "second_output"),
            ),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_OUTPUT_BUDGET_EXCEEDED"
    assert rejected.value.phase == "output_validation"
    assert rejected.value.execution_started is True


@pytest.mark.asyncio
async def test_named_outputs_share_one_aggregate_arrow_budget() -> None:
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker(output_arrow_bytes=40).execute(
            DataTransformWorkerRequest(
                """
                CREATE TEMP VIEW first_output AS SELECT entity_key FROM entities;
                CREATE TEMP VIEW second_output AS SELECT entity_key FROM entities;
                """,
                result_mode="named_relations",
                output_names=("first_output", "second_output"),
            ),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_OUTPUT_BUDGET_EXCEEDED"
    assert rejected.value.phase == "output_validation"


@pytest.mark.asyncio
async def test_worker_rejects_more_than_512_output_columns() -> None:
    columns = ", ".join(f"{index} AS column_{index:03d}" for index in range(513))
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest(
                f"SELECT {columns} FROM entities LIMIT 1",
                result_mode="named_relations",
                output_names=("wide_output",),
            ),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_OUTPUT_INVALID"
    assert rejected.value.phase == "output_validation"


@pytest.mark.asyncio
async def test_worker_rejects_output_input_collision_before_execution() -> None:
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest(
                "CREATE TEMP TABLE other AS SELECT * FROM entities",
                result_mode="named_relations",
                output_names=("entities",),
            ),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_SQL_NOT_ALLOWED"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_multi_statement_script_cannot_mutate_inputs_or_request_external_effects() -> None:
    with pytest.raises(DataTransformWorkerError) as mutation:
        await _worker().execute(
            DataTransformWorkerRequest(
                """
                UPDATE entities SET diameter = 1;
                CREATE TEMP VIEW output AS SELECT * FROM entities;
                """,
                result_mode="named_relations",
                output_names=("output",),
            ),
            _inputs(),
        )
    assert mutation.value.code == "TRANSFORM_SQL_NOT_ALLOWED"
    assert mutation.value.execution_started is True

    with pytest.raises(DataTransformWorkerError) as external:
        await _worker().execute(
            DataTransformWorkerRequest(
                """
                CREATE TEMP TABLE output AS SELECT * FROM entities;
                COPY output TO 'forbidden.parquet';
                """,
                result_mode="named_relations",
                output_names=("output",),
            ),
            _inputs(),
        )
    assert external.value.code == "TRANSFORM_SQL_NOT_ALLOWED"
    assert external.value.execution_started is False


@pytest.mark.asyncio
async def test_quoted_case_variant_cannot_replace_a_read_only_input() -> None:
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest(
                """
                CREATE OR REPLACE TEMP TABLE "ENTITIES" AS
                SELECT entity_key, diameter FROM entities;
                """,
                result_mode="named_relations",
                output_names=("output",),
            ),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_SQL_NOT_ALLOWED"
    assert rejected.value.phase == "preflight"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_quoted_case_variants_track_inputs_and_private_mutation_targets() -> None:
    result = await _worker().execute(
        DataTransformWorkerRequest(
            """
            CREATE TEMP TABLE "WoRkInG" AS
            SELECT entity_key, diameter FROM "EnTiTiEs";
            UPDATE "wOrKiNg" SET diameter = diameter + 1;
            CREATE TEMP VIEW output AS
            SELECT entity_key, diameter FROM "WORKING" ORDER BY entity_key;
            """,
            result_mode="named_relations",
            output_names=("output",),
        ),
        _inputs(),
    )

    assert pa.Table.from_batches(result[0].batches).to_pylist() == [
        {"entity_key": "ent_1", "diameter": 51},
        {"entity_key": "ent_2", "diameter": 81},
        {"entity_key": "ent_3", "diameter": 101},
    ]


@pytest.mark.asyncio
async def test_same_input_can_be_scanned_fully_by_multiple_statements() -> None:
    result = await _worker().execute(
        DataTransformWorkerRequest(
            """
            CREATE TEMP VIEW first_output AS SELECT entity_key FROM entities;
            CREATE TEMP VIEW second_output AS SELECT entity_key FROM entities;
            """,
            result_mode="named_relations",
            output_names=("first_output", "second_output"),
        ),
        _inputs(),
    )

    assert [item.receipt.row_count for item in result] == [3, 3]


@pytest.mark.asyncio
async def test_query_result_counts_each_input_used_only_in_scalar_subqueries() -> None:
    inputs = (
        ArrowTransformInput(
            "ae",
            pa.table(
                {
                    "atomic_edge_key": ["edge_1"],
                    "would_collapse_under_quantization": [False],
                    "length_3d_m": [1.0],
                    "unit_x": [1.0],
                    "unit_y": [0.0],
                    "unit_z": [0.0],
                }
            ).to_reader(),
        ),
        ArrowTransformInput(
            "ep",
            pa.table(
                {
                    "atomic_edge_key": ["edge_1"],
                    "quantization_deviation_3d_m": [0.0],
                }
            ).to_reader(),
        ),
        ArrowTransformInput(
            "pt",
            pa.table(
                {
                    "raw_candidate_rank": [1],
                    "point_entity_key": ["point_1"],
                    "within_uniqueness_margin": [True],
                }
            ).to_reader(),
        ),
        ArrowTransformInput("td", pa.table({"severity": ["INFO"]}).to_reader()),
    )

    result = await _worker().execute(
        DataTransformWorkerRequest(
            """
            SELECT
                (SELECT count(*) FROM ae WHERE would_collapse_under_quantization)
                    AS collapsed_edges,
                (SELECT count(*) FROM ae
                 WHERE length_3d_m <= 0
                    OR abs(sqrt(unit_x * unit_x + unit_y * unit_y + unit_z * unit_z) - 1)
                       > 1e-9) AS invalid_edges,
                (SELECT count(*) FROM ep
                 WHERE quantization_deviation_3d_m > 0.0000867)
                    AS excessive_quantization_deviation,
                (SELECT count(*) FROM ep
                 LEFT JOIN ae USING (atomic_edge_key)
                 WHERE ae.atomic_edge_key IS NULL) AS endpoint_edge_orphans,
                (SELECT count(*) FROM pt WHERE raw_candidate_rank = 1) AS point_best_raw,
                (SELECT count(DISTINCT point_entity_key) FROM pt) AS matched_points,
                (SELECT count(DISTINCT point_entity_key) FROM pt
                 WHERE raw_candidate_rank = 1 AND NOT within_uniqueness_margin)
                    AS ambiguous_best_points,
                (SELECT count(*) FROM td WHERE severity IN ('ERROR', 'BLOCKER'))
                    AS blocking_diagnostics
            """,
            result_mode="query_final_result",
        ),
        inputs,
    )

    assert pa.Table.from_batches(result[0].batches).to_pylist() == [
        {
            "collapsed_edges": 0,
            "invalid_edges": 0,
            "excessive_quantization_deviation": 0,
            "endpoint_edge_orphans": 0,
            "point_best_raw": 1,
            "matched_points": 1,
            "ambiguous_best_points": 0,
            "blocking_diagnostics": 0,
        }
    ]


@pytest.mark.asyncio
async def test_worker_drains_an_arrow_batch_larger_than_the_pipe_buffer() -> None:
    row_count = 40_000
    geometry = pa.table(
        {
            "entity_key": [f"entity_{index:05d}" for index in range(row_count)],
            "geometry_component_kind": ["polyline_vertex"] * row_count,
            "ordinal": pa.array(range(row_count), type=pa.int32()),
            "x": [float(index) for index in range(row_count)],
            "y": [None] * row_count,
            "z": [None] * row_count,
        }
    )
    result = await _worker().execute(
        DataTransformWorkerRequest(
            """
            CREATE TEMP TABLE geom AS
            SELECT entity_key, geometry_component_kind, ordinal, x, y, z
            FROM geometry_elements;
            """,
            result_mode="named_relations",
            output_names=("geom",),
        ),
        _inputs(name="geometry_elements", table=geometry),
    )

    assert result[0].payload_name == "geom"
    assert result[0].receipt.row_count == row_count
    assert len(result[0].batches) == 1


@pytest.mark.asyncio
async def test_worker_rejects_an_unused_declared_input() -> None:
    inputs = (
        *_inputs(),
        ArrowTransformInput("geometry", pa.table({"geometry_key": ["geo_1"]}).to_reader()),
    )
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest(
                "CREATE TEMP VIEW output AS SELECT entity_key FROM entities",
                result_mode="named_relations",
                output_names=("output",),
            ),
            inputs,
        )

    assert rejected.value.code == "TRANSFORM_SQL_NOT_ALLOWED"
    assert rejected.value.execution_started is True
    assert "unused input_name(s): geometry" in str(rejected.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("parameterized", (False, True))
async def test_worker_rejects_serialized_sql_execution_bypass(parameterized: bool) -> None:
    with duckdb.connect(":memory:") as connection:
        serialized = connection.execute(
            "SELECT json_serialize_sql('SELECT current_setting(''temp_directory'') AS secret')"
        ).fetchone()[0]
    if parameterized:
        code = (
            "SELECT e.entity_key FROM entities e, json_execute_serialized_sql($serialized) leaked"
        )
        parameters = (DataTransformWorkerParameter("serialized", serialized),)
    else:
        literal = "'" + str(serialized).replace("'", "''") + "'"
        code = f"SELECT e.entity_key FROM entities e, json_execute_serialized_sql({literal}) leaked"
        parameters = ()

    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest(code, parameters),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_SQL_NOT_ALLOWED"
    assert rejected.value.phase == "preflight"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_code"),
    (
        ("SELECT * FROM entities; SELECT * FROM entities", "TRANSFORM_SQL_INVALID"),
        ("CREATE TABLE copied AS SELECT * FROM entities", "TRANSFORM_SQL_NOT_ALLOWED"),
        ("SELECT 1", "TRANSFORM_SQL_NOT_ALLOWED"),
        (
            "SELECT e.entity_key FROM entities e, duckdb_tables() d",
            "TRANSFORM_SQL_NOT_ALLOWED",
        ),
        (
            "SELECT e.entity_key FROM entities e, duckdb_settings() d",
            "TRANSFORM_SQL_NOT_ALLOWED",
        ),
        (
            "SELECT current_setting('temp_directory') FROM entities",
            "TRANSFORM_SQL_NOT_ALLOWED",
        ),
        (
            "SELECT current_database() FROM entities",
            "TRANSFORM_SQL_NOT_ALLOWED",
        ),
        (
            "SELECT e.entity_key, d.x FROM entities e, "
            "query('SELECT current_setting(''temp_directory'') AS x') d",
            "TRANSFORM_SQL_NOT_ALLOWED",
        ),
        (
            "SELECT e.entity_key, d.x FROM entities e, query('SELECT current_database() AS x') d",
            "TRANSFORM_SQL_NOT_ALLOWED",
        ),
        (
            "SELECT e.entity_key, d.x FROM entities e, query_table('duckdb_settings') d",
            "TRANSFORM_SQL_NOT_ALLOWED",
        ),
        (
            "SELECT e.entity_key FROM entities e, enable_profiling() p",
            "TRANSFORM_SQL_NOT_ALLOWED",
        ),
        ("CALL enable_profiling()", "TRANSFORM_SQL_NOT_ALLOWED"),
        (
            "SELECT e.entity_key FROM entities e, read_csv('/etc/passwd') x",
            "TRANSFORM_SQL_NOT_ALLOWED",
        ),
    ),
)
async def test_worker_preflight_rejects_non_m2a_sql(code: str, expected_code: str) -> None:
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(DataTransformWorkerRequest(code), _inputs())

    assert rejected.value.code == expected_code
    assert rejected.value.phase == "preflight"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_worker_preflight_requires_exact_parameter_set() -> None:
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest(
                "SELECT entity_key FROM entities WHERE layer=$missing",
                (DataTransformWorkerParameter("extra", "燃气"),),
            ),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_PARAMETER_MISMATCH"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_worker_returns_bounded_actionable_duckdb_binder_detail() -> None:
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest(
                "SELECT missing_outer_diameter FROM entities"
            ),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_SQL_INVALID"
    assert rejected.value.phase == "preflight"
    assert rejected.value.execution_started is False
    message = str(rejected.value)
    assert "DuckDB could not bind the SQL" in message
    assert "missing_outer_diameter" in message
    assert "Binder Error" in message
    assert len(message.encode("utf-8")) <= 2_000


@pytest.mark.asyncio
async def test_worker_returns_bounded_actionable_duckdb_parser_detail() -> None:
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest(
                "SELECT entity_key, AS broken_alias FROM entities"
            ),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_SQL_INVALID"
    assert rejected.value.phase == "preflight"
    assert rejected.value.execution_started is False
    message = str(rejected.value)
    assert "DuckDB could not parse the SQL" in message
    assert "Parser Error" in message
    assert "AS" in message or "broken_alias" in message
    assert len(message.encode("utf-8")) <= 2_000


@pytest.mark.asyncio
async def test_worker_rejects_input_and_output_budgets_at_boundary() -> None:
    with pytest.raises(DataTransformWorkerError) as input_error:
        await _worker(input_row_limit=2).execute(
            DataTransformWorkerRequest("SELECT entity_key FROM entities"),
            _inputs(),
        )
    assert input_error.value.code == "TRANSFORM_INPUT_BUDGET_EXCEEDED"
    assert input_error.value.execution_started is False

    with pytest.raises(DataTransformWorkerError) as output_error:
        await _worker(output_row_limit=1).execute(
            DataTransformWorkerRequest("SELECT entity_key FROM entities ORDER BY entity_key"),
            _inputs(),
        )
    assert output_error.value.code == "TRANSFORM_OUTPUT_BUDGET_EXCEEDED"
    assert output_error.value.phase == "output_validation"
    assert output_error.value.execution_started is True


@pytest.mark.asyncio
async def test_worker_applies_input_budgets_across_package_and_inline_relations() -> None:
    def inputs() -> tuple[ArrowTransformInput, ...]:
        return (
            ArrowTransformInput("entities", pa.table({"value": [1, 2]}).to_reader()),
            ArrowTransformInput(
                "inline_trials",
                pa.table({"value": [3, 4]}).to_reader(),
                input_format="inline",
            ),
        )

    request = DataTransformWorkerRequest(
        "SELECT value FROM entities UNION ALL SELECT value FROM inline_trials"
    )

    with pytest.raises(DataTransformWorkerError) as row_error:
        await _worker(input_row_limit=3).execute(request, inputs())

    assert row_error.value.code == "TRANSFORM_INPUT_BUDGET_EXCEEDED"
    assert row_error.value.phase == "preflight"
    assert row_error.value.execution_started is False

    with pytest.raises(DataTransformWorkerError) as arrow_error:
        await _worker(input_arrow_bytes=24).execute(request, inputs())

    assert arrow_error.value.code == "TRANSFORM_INPUT_BUDGET_EXCEEDED"
    assert arrow_error.value.phase == "preflight"
    assert arrow_error.value.execution_started is False


@pytest.mark.asyncio
async def test_worker_interrupts_expensive_sql_and_removes_private_scratch() -> None:
    before = set(Path(tempfile.gettempdir()).glob("cad2bim-transform-*"))
    worker = _worker(wall_seconds=0.1, cpu_seconds=5)
    with pytest.raises(DataTransformWorkerError) as timeout:
        await worker.execute(
            DataTransformWorkerRequest(
                """
                WITH RECURSIVE counter(value) AS (
                    SELECT 1
                    UNION ALL
                    SELECT value + 1 FROM counter WHERE value < 1000000000
                )
                SELECT count(*) FROM entities, counter
                """
            ),
            _inputs(),
        )

    assert timeout.value.code == "TRANSFORM_TIMEOUT"
    assert timeout.value.execution_started is True
    assert set(Path(tempfile.gettempdir()).glob("cad2bim-transform-*")) == before


@pytest.mark.asyncio
async def test_worker_capacity_wait_is_bounded(tmp_path: Path) -> None:
    worker = _worker(
        capacity_root=tmp_path / "capacity",
        max_concurrent_workers=1,
        capacity_wait_seconds=0.01,
    )
    first = asyncio.create_task(
        worker.execute(
            DataTransformWorkerRequest("SELECT entity_key FROM entities"),
            _inputs(),
        )
    )
    await asyncio.sleep(0)
    with pytest.raises(DataTransformWorkerError) as busy:
        await worker.execute(
            DataTransformWorkerRequest("SELECT entity_key FROM entities"),
            _inputs(),
        )
    assert busy.value.code == "TRANSFORM_CAPACITY_BUSY"
    assert busy.value.execution_started is False
    await first


@pytest.mark.asyncio
async def test_host_shared_capacity_limits_distinct_worker_instances(tmp_path: Path) -> None:
    capacity_root = tmp_path / "capacity"
    policy = DataTransformWorkerPolicy(
        wall_seconds=5.0,
        cpu_seconds=10,
        cleanup_seconds=0.5,
        max_concurrent_workers=2,
        capacity_wait_seconds=0.02,
    )
    workers = tuple(
        SubprocessDuckDBDataTransformWorker(policy=policy, capacity_root=capacity_root)
        for _ in range(3)
    )
    entered = 0
    both_entered = asyncio.Event()
    release = asyncio.Event()
    sentinel = object()

    async def hold_capacity(*args: object) -> object:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await release.wait()
        return sentinel

    for worker in workers:
        worker._execute_with_capacity = hold_capacity  # type: ignore[method-assign]

    first = asyncio.create_task(
        workers[0].execute(DataTransformWorkerRequest("SELECT 1"), _inputs())
    )
    second = asyncio.create_task(
        workers[1].execute(DataTransformWorkerRequest("SELECT 1"), _inputs())
    )
    await asyncio.wait_for(both_entered.wait(), timeout=1.0)
    try:
        with pytest.raises(DataTransformWorkerError) as busy:
            await workers[2].execute(DataTransformWorkerRequest("SELECT 1"), _inputs())
        assert busy.value.code == "TRANSFORM_CAPACITY_BUSY"
        assert busy.value.execution_started is False
    finally:
        release.set()

    assert await first is sentinel
    assert await second is sentinel


@pytest.mark.asyncio
async def test_worker_readiness_probe_and_close_are_real_and_bounded() -> None:
    worker = _worker()
    await worker.verify_ready()
    await worker.close()


@pytest.mark.asyncio
async def test_worker_retries_one_spawn_failure_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    call_root = tmp_path / "retry-call"

    def make_call_root(*, prefix: str) -> str:
        assert prefix == "cad2bim-transform-"
        call_root.mkdir()
        return str(call_root)

    monkeypatch.setattr(tempfile, "mkdtemp", make_call_root)
    original_spawn = asyncio.create_subprocess_exec
    calls = 0

    async def flaky_spawn(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected transient spawn failure")
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", flaky_spawn)
    result = await _worker().execute(
        DataTransformWorkerRequest("SELECT entity_key FROM entities ORDER BY entity_key"),
        _inputs(),
    )

    assert calls == 2
    assert result[0].receipt.row_count == 3
    assert not call_root.exists()


@pytest.mark.asyncio
async def test_worker_second_spawn_failure_is_stable_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def failed_spawn(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        raise OSError("injected persistent spawn failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_spawn)
    with pytest.raises(DataTransformWorkerError) as rejected:
        await _worker().execute(
            DataTransformWorkerRequest("SELECT entity_key FROM entities"),
            _inputs(),
        )

    assert calls == 2
    assert rejected.value.code == "TRANSFORM_SERVICE_UNAVAILABLE"
    assert rejected.value.phase == "preflight"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_worker_prestart_child_crash_is_service_unavailable() -> None:
    false_executable = shutil.which("false")
    assert false_executable is not None
    worker = SubprocessDuckDBDataTransformWorker(
        policy=DataTransformWorkerPolicy(
            wall_seconds=5.0,
            cpu_seconds=10,
            cleanup_seconds=0.5,
        ),
        python_executable=false_executable,
    )

    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(
            DataTransformWorkerRequest("SELECT entity_key FROM entities"),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_SERVICE_UNAVAILABLE"
    assert rejected.value.phase == "preflight"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_worker_staging_failure_is_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker()

    def failed_staging(*args: object, **kwargs: object):
        raise OSError("injected staging failure")

    monkeypatch.setattr(worker, "_stage_inputs", failed_staging)
    with pytest.raises(DataTransformWorkerError) as rejected:
        await worker.execute(
            DataTransformWorkerRequest("SELECT entity_key FROM entities"),
            _inputs(),
        )

    assert rejected.value.code == "TRANSFORM_SERVICE_UNAVAILABLE"
    assert rejected.value.phase == "preflight"
    assert rejected.value.execution_started is False


@pytest.mark.asyncio
async def test_worker_never_retries_business_sql_or_input_budget_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_spawn = asyncio.create_subprocess_exec
    calls = 0

    async def counting_spawn(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_spawn)
    with pytest.raises(DataTransformWorkerError) as sql_error:
        await _worker().execute(
            DataTransformWorkerRequest("SELECT * FROM entities; SELECT 1"),
            _inputs(),
        )
    assert sql_error.value.code == "TRANSFORM_SQL_INVALID"
    assert calls == 1

    with pytest.raises(DataTransformWorkerError) as budget_error:
        await _worker(input_row_limit=2).execute(
            DataTransformWorkerRequest("SELECT entity_key FROM entities"),
            _inputs(),
        )
    assert budget_error.value.code == "TRANSFORM_INPUT_BUDGET_EXCEEDED"
    assert calls == 1


@pytest.mark.asyncio
async def test_staging_cancellation_returns_promptly_but_transfers_resource_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capacity_root = tmp_path / "capacity"
    worker = _worker(
        capacity_root=capacity_root,
        max_concurrent_workers=1,
        capacity_wait_seconds=0.01,
    )
    other_worker = _worker(
        capacity_root=capacity_root,
        max_concurrent_workers=1,
        capacity_wait_seconds=0.01,
    )
    original_staging = worker._stage_inputs
    entered = threading.Event()
    release = threading.Event()
    scratch_roots: list[Path] = []

    def slow_staging(root: Path, inputs: tuple[ArrowTransformInput, ...]):
        scratch_roots.append(root)
        entered.set()
        release.wait(timeout=2.0)
        return original_staging(root, inputs)

    monkeypatch.setattr(worker, "_stage_inputs", slow_staging)
    running = asyncio.create_task(
        worker.execute(
            DataTransformWorkerRequest("SELECT entity_key FROM entities"),
            _inputs(),
        )
    )
    assert await asyncio.to_thread(entered.wait, 1.0)
    cancelled_at = asyncio.get_running_loop().time()
    running.cancel()
    with pytest.raises(DataTransformWorkerError) as cancelled:
        await asyncio.wait_for(running, timeout=0.1)
    assert cancelled.value.code == "TRANSFORM_TIMEOUT"
    assert cancelled.value.phase == "preflight"
    assert cancelled.value.execution_started is False
    assert asyncio.get_running_loop().time() - cancelled_at < 0.1

    try:
        with pytest.raises(DataTransformWorkerError) as busy:
            await other_worker.execute(
                DataTransformWorkerRequest("SELECT entity_key FROM entities"),
                _inputs(),
            )
        assert busy.value.code == "TRANSFORM_CAPACITY_BUSY"
    finally:
        asyncio.get_running_loop().call_later(0.05, release.set)

    close_started = asyncio.get_running_loop().time()
    await worker.close()
    assert asyncio.get_running_loop().time() - close_started >= 0.03
    assert scratch_roots
    assert all(not root.exists() for root in scratch_roots)
    assert not worker._capacity.locked()

    result = await other_worker.execute(
        DataTransformWorkerRequest("SELECT entity_key FROM entities ORDER BY entity_key"),
        _inputs(),
    )
    assert result[0].receipt.row_count == 3


def test_whole_call_scratch_budget_reduces_each_child_attempt_allowance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "call"
    root.mkdir()
    worker = _worker(scratch_bytes=4_096)

    worker._stage_inputs(root, _inputs())
    staged_bytes = worker._directory_size(root)
    assert 0 < staged_bytes < 4_096
    assert worker._child_policy_for_root(root)["scratch_bytes"] == 4_096 - staged_bytes

    prior_attempt = root / "worker-1"
    prior_attempt.mkdir()
    (prior_attempt / "spill.bin").write_bytes(b"x" * 257)
    assert worker._child_policy_for_root(root)["scratch_bytes"] == (4_096 - staged_bytes - 257)

    over_budget_root = tmp_path / "over-budget"
    over_budget_root.mkdir()
    over_budget_worker = _worker(scratch_bytes=staged_bytes - 1)
    with pytest.raises(DataTransformWorkerError) as rejected:
        over_budget_worker._stage_inputs(over_budget_root, _inputs())
    assert rejected.value.code == "TRANSFORM_RESOURCE_LIMIT"
    assert rejected.value.execution_started is False


def test_entry_neutral_command_is_closed_typed_and_projects_to_worker() -> None:
    command = DataTransformCommand(
        inputs=(
            TransformInputBinding(
                input_name="entities",
                package_alias="data_001",
                payload_name="entities",
            ),
        ),
        parameters=(
            StringParameter(name="layer", kind="string", value="燃气"),
            IntegerParameter(name="minimum", kind="integer", value=50),
        ),
        execution=DuckDbSqlExecution(
            engine="duckdb_sql",
            code=("SELECT entity_key FROM entities WHERE layer=$layer AND diameter >= $minimum"),
        ),
        outputs=(
            ParquetOutputDeclaration(
                payload_name="selected_entities",
                format="parquet",
                description="Selected entities",
                logical_key_fields=("entity_key",),
            ),
        ),
        output_package=OutputPackageDeclaration(
            name="selected_entities_v1",
            description_markdown="# Selected\n## Summary\nSelected entities.",
        ),
    )

    worker_request = DataTransformWorkerRequest.from_command(command)
    assert worker_request.code == command.execution.code
    assert [(item.name, item.value) for item in worker_request.parameters] == [
        ("layer", "燃气"),
        ("minimum", 50),
    ]
    assert worker_request.result_mode == "named_relations"
    assert worker_request.output_names == ("selected_entities",)
    assert "direct_session_alias" not in DataTransformCommand.model_json_schema()["properties"]

    with pytest.raises(ValidationError):
        DataTransformCommand.model_validate({**command.model_dump(mode="json"), "unknown": True})


def test_command_rejects_duplicate_bindings_and_utf8_byte_overflow() -> None:
    common = {
        "execution": {"engine": "duckdb_sql", "code": "SELECT * FROM entities"},
        "outputs": [
            {
                "payload_name": "selected",
                "format": "parquet",
                "description": "selected",
                "logical_key_fields": ["entity_key"],
            }
        ],
        "output_package": {
            "name": "selected",
            "description_markdown": "# Selected",
        },
    }
    binding = {
        "input_name": "entities",
        "package_alias": "data_001",
        "payload_name": "entities",
    }
    with pytest.raises(ValidationError, match="bound twice"):
        DataTransformCommand.model_validate(
            {
                **common,
                "inputs": [binding, {**binding, "input_name": "entities_again"}],
            }
        )
    with pytest.raises(ValidationError, match="4096 UTF-8 bytes"):
        StringParameter(name="layer", kind="string", value="界" * 1_366)


def test_command_accepts_sixteen_unique_outputs_and_rejects_invalid_namespaces() -> None:
    binding = {
        "input_name": "entities",
        "package_alias": "data_001",
        "payload_name": "entities",
    }
    output = {
        "format": "parquet",
        "description": "selected",
        "logical_key_fields": ["entity_key"],
    }
    common = {
        "inputs": [binding],
        "execution": {
            "engine": "duckdb_sql",
            "code": "CREATE TEMP TABLE output_00 AS SELECT * FROM entities",
        },
        "output_package": {
            "name": "selected",
            "description_markdown": "# Selected",
        },
    }
    accepted = DataTransformCommand.model_validate(
        {
            **common,
            "outputs": [{**output, "payload_name": f"output_{index:02d}"} for index in range(16)],
        }
    )
    assert len(accepted.outputs) == 16

    with pytest.raises(ValidationError):
        DataTransformCommand.model_validate(
            {
                **common,
                "outputs": [
                    {**output, "payload_name": f"output_{index:02d}"} for index in range(17)
                ],
            }
        )
    with pytest.raises(ValidationError, match="output payload_name must be unique"):
        DataTransformCommand.model_validate(
            {
                **common,
                "outputs": [
                    {**output, "payload_name": "selected"},
                    {**output, "payload_name": "selected"},
                ],
            }
        )
    with pytest.raises(ValidationError, match="must not collide"):
        DataTransformCommand.model_validate(
            {
                **common,
                "outputs": [{**output, "payload_name": "entities"}],
            }
        )
