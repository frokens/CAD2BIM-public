"""Data Transform source capacity; no subprocess or database is started."""
from dataclasses import asdict
from io import BytesIO

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.domain.data_transform import (
    MAX_TRANSFORM_CODE_BYTES, DuckDbSqlExecution, PythonArrowExecution,
    DataTransformWorkerRequest, PythonArrowWorkerRequest,
    PythonArrowWorkerOutputDeclaration, PythonArrowWorkerPolicy,
)
from CAD2BIM_backend.infrastructure.source_text.source_patch import (
    SourceEdit, SourcePatchError, apply_text_edits, MAX_SOURCE_BYTES,
)
from CAD2BIM_backend.infrastructure.worker_runtime.protocol import (
    MAX_CONTROL_BYTES, WorkerProtocolError, encode_json_frame, read_json_frame,
)
from CAD2BIM_mcp.direct.transform.inputs import (
    DuckDbSqlExecution as ProviderSql, PythonArrowExecution as ProviderPython,
)


@pytest.mark.parametrize("engine,domain,provider", [
    ("duckdb_sql", DuckDbSqlExecution, ProviderSql),
    ("python_arrow", PythonArrowExecution, ProviderPython),
])
@pytest.mark.parametrize("multibyte", [False, True])
def test_transform_source_utf8_boundary_and_provider(engine, domain, provider, multibyte):
    # Multibyte boundary catches byte-validation drift even below maxLength.
    code = "中" * (MAX_TRANSFORM_CODE_BYTES // 3) + "ab" if multibyte else "x" * MAX_TRANSFORM_CODE_BYTES
    assert len(code.encode()) == 131_072
    for model in (domain, provider):
        assert model(engine=engine, code=code).code == code
        with pytest.raises(ValidationError, match="131072"):
            model(engine=engine, code=code + "x")
        assert model.model_json_schema()["properties"]["code"]["maxLength"] == 131_072
    DataTransformWorkerRequest(code=code)
    PythonArrowWorkerRequest(code=code, outputs=(
        PythonArrowWorkerOutputDeclaration(payload_name="result", format="parquet"),))
    with pytest.raises(ValueError, match="131072"):
        DataTransformWorkerRequest(code=code + "x")
    with pytest.raises(ValueError, match="131072"):
        PythonArrowWorkerRequest(code=code + "x", outputs=(
            PythonArrowWorkerOutputDeclaration(payload_name="result", format="parquet"),))


def test_transform_patch_uses_explicit_capacity_without_changing_shared_default():
    source = "old\n" + "# padding\n" * 7000
    edits = (SourceEdit(old_text="old", new_text="new"),)
    with pytest.raises(SourcePatchError, match="65,536"):
        apply_text_edits(source, edits, maximum_source_bytes=MAX_SOURCE_BYTES)
    patched = apply_text_edits(source, edits, maximum_source_bytes=MAX_TRANSFORM_CODE_BYTES)
    assert patched == source.replace("old", "new", 1)
    boundary = "a" * (MAX_TRANSFORM_CODE_BYTES - 1) + "z"
    with pytest.raises(SourcePatchError, match="131,072"):
        apply_text_edits(boundary, (SourceEdit(old_text="z", new_text="zz"),), maximum_source_bytes=MAX_TRANSFORM_CODE_BYTES)


def test_query_source_budgets_remain_64k():
    from CAD2BIM_backend.domain.data_query_sql import DataQuerySqlQuery
    from CAD2BIM_backend.domain.data_query_python import DataQueryPythonQuery
    from CAD2BIM_mcp.direct.query.sql_inputs import QuerySqlQuery
    from CAD2BIM_mcp.direct.query.python_inputs import QueryPythonQuery
    inputs = [{"input_name": "rows", "package_alias": "data_001", "payload_name": "rows"}]
    code = "中" * 21845 + "x"
    assert len(code.encode()) == 65_536
    DataQuerySqlQuery(inputs=inputs, execution={"engine": "duckdb_sql", "code": code})
    DataQueryPythonQuery(inputs=inputs, code=code)
    with pytest.raises(ValidationError, match="65536"):
        DataQuerySqlQuery(inputs=inputs, execution={"engine": "duckdb_sql", "code": code + "x"})
    with pytest.raises(ValidationError, match="65536"):
        DataQueryPythonQuery(inputs=inputs, code=code + "x")
    with pytest.raises(ValidationError, match="65.?536"):
        QuerySqlQuery(inputs=inputs, execution={"engine": "duckdb_sql", "code": code + "x"})
    with pytest.raises(ValidationError, match="65.?536"):
        QueryPythonQuery(inputs=inputs, code=code + "x")


def test_transform_source_and_parameters_keep_existing_control_frame_limit():
    payload = {"mode": "transform", "code": "x" * MAX_TRANSFORM_CODE_BYTES,
        "parameters": [{"name": "value", "value": "p" * 32768}],
        "inputs": [], "outputs": [{"payload_name": "result", "format": "parquet", "fd": 9}],
        "policy": asdict(PythonArrowWorkerPolicy())}
    frame = encode_json_frame(payload, limit=MAX_CONTROL_BYTES)
    assert MAX_CONTROL_BYTES == 256 * 1024
    assert read_json_frame(BytesIO(frame), limit=MAX_CONTROL_BYTES) == payload
    # A source inside its own budget can still exceed the unchanged total JSON
    # frame budget through escaping. The protocol must reject that composition.
    payload["code"] = "\t" * MAX_TRANSFORM_CODE_BYTES
    with pytest.raises(WorkerProtocolError, match="byte limit"):
        encode_json_frame(payload, limit=MAX_CONTROL_BYTES)
