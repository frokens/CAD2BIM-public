from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from CAD2BIM_backend.infrastructure.cad_l0 import (
    CadL0ExtractionError,
    ExternalCadDependencyError,
    UnsupportedCadSourceError,
    extract_ascii_dxf_l0,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "cad"


@pytest.mark.parametrize("cancel_at", [1, 2, 3])
def test_cancelled_extraction_leaves_no_partial_package(tmp_path: Path, cancel_at: int) -> None:
    count = 0

    def check_cancelled() -> None:
        nonlocal count
        count += 1
        if count == cancel_at:
            raise InterruptedError("extraction cancelled")

    with pytest.raises(InterruptedError, match="extraction cancelled"):
        extract_ascii_dxf_l0(
            FIXTURE_ROOT / "minimal_line.dxf", tmp_path / "l0",
            check_cancelled=check_cancelled,
        )
    assert count == cancel_at
    assert list(tmp_path.iterdir()) == []


def test_minimal_ascii_dxf_stages_exact_queryable_l0(tmp_path: Path) -> None:
    source = FIXTURE_ROOT / "minimal_line.dxf"
    result = extract_ascii_dxf_l0(source, tmp_path / "l0")

    assert result.source_sha256 == (
        "1a620d5cae4d08030ee841b68a9e2412a8a5764f3455be15a7c6f0ca3dd2e9ae"
    )
    assert result.completeness_status == "complete"
    assert result.manifest_facts["publication_state"] == "staging_draft"
    assert {"schema", "runtime", "parser_adapter_version"}.isdisjoint(result.manifest_facts)
    assert result.payload_row_counts == {
        "records": 7,
        "raw_tags": 17,
        "entities": 1,
        "geometry_elements": 2,
        "xdata_tags": 0,
        "relationships": 0,
    }

    line = pq.read_table(result.payload_paths["entities"][0]).to_pylist()[0]
    assert line["entity_type"] == "LINE"
    assert line["typed_geometry_supported"] is True
    assert line["parse_disposition"] == "typed"
    geometry = pq.read_table(result.payload_paths["geometry_elements"][0]).to_pylist()
    assert [(row["geometry_component_kind"], row["x"], row["y"]) for row in geometry] == [
        ("line_start", 0.0, 0.0),
        ("line_end", 1000.0, 0.0),
    ]


def test_xdata_order_and_unsupported_entity_remain_visible(tmp_path: Path) -> None:
    source = b"""0
SECTION
2
ENTITIES
0
CIRCLE
5
AA
8
Gas
10
1.0
20
2.0
40
3.0
1001
GasApp
1000
DN100
1000

0
ENDSEC
0
EOF
"""
    result = extract_ascii_dxf_l0(source, tmp_path / "l0", source_name="gas.dxf")

    entity = pq.read_table(result.payload_paths["entities"][0]).to_pylist()[0]
    assert entity["entity_type"] == "CIRCLE"
    assert entity["typed_geometry_supported"] is False
    assert entity["unsupported_reason"] == "typed_geometry_not_implemented"
    assert entity["parse_disposition"] == "raw_only"
    entity_record = next(
        row
        for row in pq.read_table(result.payload_paths["records"][0]).to_pylist()
        if row["record_type"] == "CIRCLE"
    )
    assert entity_record["parse_disposition"] == "raw_only"
    assert result.manifest_facts["typed_geometry"]["unsupported_entity_type_counts"] == {
        "CIRCLE": 1
    }

    xdata = pq.read_table(result.payload_paths["xdata_tags"][0]).to_pylist()
    assert [(row["group_code"], row["raw_lexeme"]) for row in xdata] == [
        (1001, "GasApp"),
        (1000, "DN100"),
        (1000, ""),
    ]
    assert [row["tag_ordinal"] for row in xdata] == [0, 1, 2]


@pytest.mark.parametrize(
    ("source_name", "payload", "error_type", "error_code"),
    [
        (
            "drawing.dwg",
            b"AC1027binary",
            UnsupportedCadSourceError,
            "UNSUPPORTED_SOURCE_FORMAT",
        ),
        (
            "drawing.dxf",
            b"AutoCAD Binary DXF\r\n\x1a\x00",
            UnsupportedCadSourceError,
            "UNSUPPORTED_SOURCE_FORMAT",
        ),
        (
            "drawing.dxf",
            b"0\nSECTION\n2\nOBJECTS\n0\nIMAGEDEF\n5\nA1\n0\nENDSEC\n0\nEOF\n",
            ExternalCadDependencyError,
            "EXTERNAL_DEPENDENCY_UNSUPPORTED",
        ),
    ],
)
def test_phase_one_rejects_unsupported_source_without_publishing_staging(
    tmp_path: Path,
    source_name: str,
    payload: bytes,
    error_type: type[CadL0ExtractionError],
    error_code: str,
) -> None:
    target = tmp_path / "l0"

    with pytest.raises(error_type) as raised:
        extract_ascii_dxf_l0(payload, target, source_name=source_name)

    assert raised.value.code == error_code
    assert not target.exists()


def test_truncated_pair_prevents_l0_staging(tmp_path: Path) -> None:
    source = b"0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n"
    target = tmp_path / "l0"

    with pytest.raises(CadL0ExtractionError) as raised:
        extract_ascii_dxf_l0(source, target, source_name="broken.dxf")

    assert raised.value.code == "MALFORMED_ASCII_DXF"
    assert not target.exists()


@pytest.mark.parametrize(
    "entity_records",
    [
        # Declares two LWPOLYLINE vertices but supplies one complete x/y pair.
        "0\nLWPOLYLINE\n90\n2\n10\n0\n20\n0\n",
        # A legacy POLYLINE sequence is incomplete until its source SEQEND is present.
        "0\nPOLYLINE\n10\n0\n20\n0\n30\n0\n70\n0\n"
        "0\nVERTEX\n10\n1\n20\n1\n30\n0\n70\n0\n",
    ],
)
def test_incomplete_polyline_sources_do_not_stage_l0(
    tmp_path: Path, entity_records: str
) -> None:
    source = (
        "0\nSECTION\n2\nENTITIES\n"
        + entity_records
        + "0\nENDSEC\n0\nEOF\n"
    ).encode()
    target = tmp_path / "l0"

    with pytest.raises(CadL0ExtractionError) as raised:
        extract_ascii_dxf_l0(source, target, source_name="broken.dxf")

    assert raised.value.code == "MALFORMED_ASCII_DXF"
    assert not target.exists()
