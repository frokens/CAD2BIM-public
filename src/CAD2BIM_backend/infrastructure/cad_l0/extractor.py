from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from CAD2BIM_backend.infrastructure.cad_l0.schemas import PAYLOAD_SCHEMAS


DOCUMENT_KEY = "doc_0001"

_TYPED_GEOMETRY_TYPES = frozenset(
    {"LINE", "POINT", "LWPOLYLINE", "POLYLINE", "VERTEX", "SEQEND"}
)
_EXTERNAL_ENTITY_TYPES = frozenset(
    {
        "IMAGE",
        "IMAGEDEF",
        "IMAGEDEF_REACTOR",
        "PDFUNDERLAY",
        "PDFDEFINITION",
        "DWFUNDERLAY",
        "DWFDEFINITION",
        "DGNUNDERLAY",
        "DGNDEFINITION",
    }
)


class CadL0ExtractionError(RuntimeError):
    """The source could not be turned into a trustworthy L0 staging result."""

    code = "MALFORMED_ASCII_DXF"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnsupportedCadSourceError(CadL0ExtractionError):
    """The source uses a CAD format or external dependency outside phase one."""

    code = "UNSUPPORTED_SOURCE_FORMAT"


class ExternalCadDependencyError(CadL0ExtractionError):
    """The textual DXF references an external resource not supported in phase one."""

    code = "EXTERNAL_DEPENDENCY_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class CadL0StagingResult:
    staging_dir: Path
    manifest_draft_path: Path
    description_path: Path
    parse_issues_path: Path
    payload_paths: dict[str, tuple[Path, ...]]
    payload_row_counts: dict[str, int]
    manifest_facts: dict[str, Any]
    description_markdown: str
    source_sha256: str
    completeness_status: str


@dataclass(frozen=True, slots=True)
class _SourceLine:
    content: bytes
    byte_start: int
    byte_end: int


@dataclass(frozen=True, slots=True)
class _Tag:
    ordinal: int
    group_code: int
    raw_code_lexeme: str
    raw_value: bytes
    decoded_value: str
    byte_start: int
    byte_end: int

    @property
    def raw_tag_key(self) -> str:
        return f"tag_{self.ordinal:010d}"


@dataclass(frozen=True, slots=True)
class _Record:
    ordinal: int
    section: str
    record_type: str
    tags: tuple[_Tag, ...]

    @property
    def record_key(self) -> str:
        return f"rec_{self.ordinal:08d}"


@dataclass(slots=True)
class _PendingRelationship:
    relation_kind: str
    subject_key: str
    target: str


class _ParquetSink:
    def __init__(self, root: Path, payload_name: str, schema: pa.Schema) -> None:
        self.payload_name = payload_name
        self.schema = schema
        self.path = root / "payloads" / payload_name / "part-00000.parquet"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._writer: pq.ParquetWriter | None = None
        self._rows: list[dict[str, Any]] = []
        self.row_count = 0

    def add(self, row: dict[str, Any]) -> None:
        self._rows.append(row)
        if len(self._rows) >= 10_000:
            self.flush()

    def flush(self) -> None:
        if not self._rows:
            return
        table = pa.Table.from_pylist(self._rows, schema=self.schema)
        if self._writer is None:
            self._writer = pq.ParquetWriter(self.path, self.schema, compression="zstd")
        self._writer.write_table(table)
        self.row_count += len(self._rows)
        self._rows.clear()

    def close(self) -> None:
        self.flush()
        if self._writer is None:
            # Empty logical payloads still have a queryable, explicitly typed member.
            pq.write_table(
                pa.Table.from_pylist([], schema=self.schema), self.path, compression="zstd"
            )
        else:
            self._writer.close()


def extract_ascii_dxf_l0(
    source: bytes | Path,
    staging_dir: Path,
    *,
    source_name: str | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> CadL0StagingResult:
    """Extract one exact textual DXF into deterministic, queryable L0 members.

    The function only stages files. Artifact and Data Package publication remain
    application-service responsibilities, so this module has no database or MCP coupling.
    """

    check_cancelled = check_cancelled or (lambda: None)
    check_cancelled()
    source_bytes, resolved_name = _read_source(source, source_name)
    _reject_unsupported_container(source_bytes, resolved_name)
    decoded_encoding = _select_text_encoding(source_bytes)

    staging_dir = Path(staging_dir)
    if staging_dir.exists():
        raise FileExistsError(f"staging target already exists: {staging_dir}")
    staging_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = staging_dir.parent / f".{staging_dir.name}.tmp-{uuid.uuid4().hex}"
    temporary_dir.mkdir()

    try:
        extractor = _AsciiDxfExtractor(
            source_bytes=source_bytes,
            source_name=resolved_name,
            output_dir=temporary_dir,
            decoded_encoding=decoded_encoding,
            check_cancelled=check_cancelled,
        )
        extractor.run()
        os.replace(temporary_dir, staging_dir)
    except BaseException:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise

    manifest = json.loads((staging_dir / "manifest.json").read_text(encoding="utf-8"))
    description = (staging_dir / "description.md").read_text(encoding="utf-8")
    return CadL0StagingResult(
        staging_dir=staging_dir,
        manifest_draft_path=staging_dir / "manifest.json",
        description_path=staging_dir / "description.md",
        parse_issues_path=staging_dir / "diagnostics" / "parse_issues.json",
        payload_paths={
            name: (staging_dir / "payloads" / name / "part-00000.parquet",)
            for name in PAYLOAD_SCHEMAS
        },
        payload_row_counts={
            name: details["row_count"] for name, details in manifest["payloads"].items()
        },
        manifest_facts=manifest,
        description_markdown=description,
        source_sha256=manifest["source"]["sha256"],
        completeness_status=manifest["completeness"]["status"],
    )


class _AsciiDxfExtractor:
    def __init__(
        self,
        *,
        source_bytes: bytes,
        source_name: str,
        output_dir: Path,
        decoded_encoding: str,
        check_cancelled: Callable[[], None],
    ) -> None:
        self.check_cancelled = check_cancelled
        self.source_bytes = source_bytes
        self.source_name = source_name
        self.output_dir = output_dir
        self.decoded_encoding = decoded_encoding
        self.sinks = {
            name: _ParquetSink(output_dir, name, schema)
            for name, schema in PAYLOAD_SCHEMAS.items()
        }
        self.parse_issues: list[dict[str, Any]] = []
        self.entity_type_counts: Counter[str] = Counter()
        self.unsupported_entity_counts: Counter[str] = Counter()
        self.observed_sections: list[str] = []
        self.handle_to_key: dict[str, str] = {}
        self.pending_relationships: list[_PendingRelationship] = []
        self.relationship_ordinal = 0
        self.entity_ordinal = 0
        self.geometry_ordinal_by_entity: defaultdict[str, int] = defaultdict(int)
        self.active_polyline_key: str | None = None
        self.active_polyline_typed_supported = True
        self.open_section: str | None = None
        self.seen_eof = False
        self.dxf_version: str | None = None
        self.declared_codepage: str | None = None

    def run(self) -> None:
        for record in _records_from_bytes(
            self.source_bytes, self.parse_issues, encoding=self.decoded_encoding,
            check_cancelled=self.check_cancelled,
        ):
            self.check_cancelled()
            self._consume_record(record)
        if self.open_section is not None:
            self._add_record_issue("missing_endsec", None)
        if not self.seen_eof:
            self._add_record_issue("missing_eof", None)
        if self.active_polyline_key is not None:
            self._add_record_issue("polyline_sequence_missing_seqend", None)
        blocking_count = sum(item["blocking"] for item in self.parse_issues)
        if blocking_count:
            self._write_diagnostics()
            raise CadL0ExtractionError(
                f"DXF has {blocking_count} blocking parse issue(s); L0 was not staged"
            )
        self._write_relationships()
        for sink in self.sinks.values():
            sink.close()

        if not self.sinks["records"].row_count:
            raise CadL0ExtractionError("DXF contains no parseable records")

        self._write_diagnostics()
        self._write_manifest()
        self._write_description()

    def _consume_record(self, record: _Record) -> None:
        self._validate_record_structure(record)
        if record.section and record.section not in self.observed_sections:
            self.observed_sections.append(record.section)
        record_domain = _record_domain(record)
        values = _tags_by_code(record.tags)
        handle = _first_handle(values, 5)
        owner_handle = _first_handle(values, 330)
        record_row = {
            "document_key": DOCUMENT_KEY,
            "record_key": record.record_key,
            "record_domain": record_domain,
            "section": record.section,
            "record_type": record.record_type,
            "source_ordinal": record.ordinal,
            "handle": handle,
            "owner_handle": owner_handle,
            "first_raw_tag_ordinal": record.tags[0].ordinal,
            "last_raw_tag_ordinal": record.tags[-1].ordinal,
            "parse_disposition": "parsed",
        }
        if handle:
            self.handle_to_key.setdefault(handle, record.record_key)

        for record_tag_ordinal, tag in enumerate(record.tags):
            self.sinks["raw_tags"].add(
                _raw_tag_row(tag, record=record, record_tag_ordinal=record_tag_ordinal)
            )

        if record.section == "HEADER" and record.record_type == "$ACADVER":
            self.dxf_version = _first_value(values, 1)
        elif record.section == "HEADER" and record.record_type == "$DWGCODEPAGE":
            self.declared_codepage = _first_value(values, 3)

        if record.record_type == "BLOCK":
            flags = _first_int(values, 70) or 0
            if flags & 4 or flags & 8:
                raise ExternalCadDependencyError("phase one rejects DXF XREF dependencies")
        if record.record_type in _EXTERNAL_ENTITY_TYPES:
            raise ExternalCadDependencyError(
                f"phase one rejects DXF {record.record_type} dependencies"
            )

        subject_key = record.record_key
        entity_key: str | None = None
        if record_domain in {"entity", "entity_child"}:
            entity_key, typed_supported = self._write_entity(record, values, record_domain)
            record_row["parse_disposition"] = "typed" if typed_supported else "raw_only"
            subject_key = entity_key
            if typed_supported:
                self._write_geometry(record, entity_key, values)
            self._write_xdata(record, entity_key)
            self._track_polyline_sequence(record, entity_key, typed_supported=typed_supported)
        self.sinks["records"].add(record_row)
        self._capture_handle_relationships(record, subject_key)

    def _validate_record_structure(self, record: _Record) -> None:
        if self.seen_eof:
            self._add_record_issue("record_after_eof", record)
        if record.record_type == "SECTION":
            if self.open_section is not None:
                self._add_record_issue("nested_section", record)
            if not record.section:
                self._add_record_issue("section_name_missing", record)
            self.open_section = record.section or None
        elif record.record_type == "ENDSEC":
            if self.open_section is None:
                self._add_record_issue("endsec_without_section", record)
            self.open_section = None
        elif record.record_type == "EOF":
            if self.open_section is not None:
                self._add_record_issue("eof_before_endsec", record)
            self.seen_eof = True

    def _add_record_issue(self, code: str, record: _Record | None) -> None:
        self.parse_issues.append(
            {
                "code": code,
                "blocking": True,
                "byte_start": record.tags[0].byte_start if record else len(self.source_bytes),
                "byte_end": record.tags[-1].byte_end if record else len(self.source_bytes),
            }
        )

    def _write_entity(
        self,
        record: _Record,
        values: dict[int, list[_Tag]],
        record_domain: str,
    ) -> tuple[str, bool]:
        # `entrec_` is a package-local join key, deliberately not a Catalog entity alias.
        entity_key = f"entrec_{self.entity_ordinal:08d}"
        self.entity_ordinal += 1
        entity_type = record.record_type
        supported = entity_type in _TYPED_GEOMETRY_TYPES
        unsupported_reason: str | None = None
        if entity_type == "POLYLINE" and ((_first_int(values, 70) or 0) & (16 | 64)):
            supported = False
            unsupported_reason = "mesh_or_polyface_projection_not_implemented"
        elif entity_type == "VERTEX" and not self.active_polyline_typed_supported:
            supported = False
            unsupported_reason = "parent_polyline_projection_not_implemented"
        if supported and entity_type == "LWPOLYLINE" and not _valid_lwpolyline_count(values):
            supported = False
            unsupported_reason = "declared_vertex_count_mismatch"
            self._add_record_issue("lwpolyline_vertex_count_mismatch", record)
        elif supported and not _has_required_geometry(entity_type, values):
            supported = False
            unsupported_reason = "required_geometry_tag_missing"
            self._add_record_issue("required_geometry_tag_missing", record)
        self.entity_type_counts[entity_type] += 1
        if not supported:
            self.unsupported_entity_counts[entity_type] += 1

        handle = _first_handle(values, 5)
        if handle:
            self.handle_to_key[handle] = entity_key
        extrusion = _point(values, 210, 220, 230, default=(0.0, 0.0, 1.0))
        self.sinks["entities"].add(
            {
                "document_key": DOCUMENT_KEY,
                "record_key": record.record_key,
                "entity_key": entity_key,
                "entity_scope": _entity_scope(record, values, record_domain),
                "entity_type": entity_type,
                "section": record.section,
                "handle": handle,
                "owner_handle": _first_handle(values, 330),
                "layer": _first_value(values, 8),
                "layout": _first_value(values, 410),
                "linetype": _first_value(values, 6),
                "color_index": _first_int(values, 62),
                "true_color": _first_int(values, 420),
                "lineweight": _first_int(values, 370),
                "transparency": _first_int(values, 440),
                "visible": (_first_int(values, 60) or 0) == 0,
                "thickness": _first_float(values, 39),
                "extrusion_x": extrusion[0],
                "extrusion_y": extrusion[1],
                "extrusion_z": extrusion[2],
                "typed_geometry_supported": supported,
                "unsupported_reason": unsupported_reason
                if unsupported_reason is not None
                else (None if supported else "typed_geometry_not_implemented"),
                "parse_disposition": "typed" if supported else "raw_only",
            }
        )
        return entity_key, supported

    def _write_geometry(
        self, record: _Record, entity_key: str, values: dict[int, list[_Tag]]
    ) -> None:
        entity_type = record.record_type
        if entity_type == "LINE":
            self._add_point_geometry(entity_key, "line_start", "start", values, 10, 20, 30)
            self._add_point_geometry(entity_key, "line_end", "end", values, 11, 21, 31)
        elif entity_type == "POINT":
            self._add_point_geometry(entity_key, "point_location", "location", values, 10, 20, 30)
        elif entity_type == "LWPOLYLINE":
            self._write_lwpolyline_vertices(entity_key, record.tags, values)
        elif entity_type in {"POLYLINE", "VERTEX"}:
            x_tag = _first_tag(values, 10)
            if x_tag is not None:
                y_tag = _first_tag(values, 20)
                z_tag = _first_tag(values, 30)
                kind = "polyline_origin" if entity_type == "POLYLINE" else "polyline_vertex"
                self._add_geometry_row(
                    entity_key,
                    kind=kind,
                    path="origin" if entity_type == "POLYLINE" else "vertex",
                    x=_tag_float(x_tag),
                    y=_tag_float(y_tag, default=0.0),
                    z=_tag_float(z_tag, default=0.0),
                    bulge=_first_float(values, 42),
                    start_width=_first_float(values, 40),
                    end_width=_first_float(values, 41),
                    closed=bool((_first_int(values, 70) or 0) & 1)
                    if entity_type == "POLYLINE"
                    else None,
                    flags=_first_int(values, 70),
                    values=values,
                )

    def _add_point_geometry(
        self,
        entity_key: str,
        kind: str,
        path: str,
        values: dict[int, list[_Tag]],
        x_code: int,
        y_code: int,
        z_code: int,
    ) -> None:
        x_tag = _first_tag(values, x_code)
        if x_tag is None:
            return
        y_tag = _first_tag(values, y_code)
        z_tag = _first_tag(values, z_code)
        self._add_geometry_row(
            entity_key,
            kind=kind,
            path=path,
            x=_tag_float(x_tag),
            y=_tag_float(y_tag),
            z=_tag_float(z_tag, default=0.0),
            values=values,
        )

    def _write_lwpolyline_vertices(
        self, entity_key: str, tags: Sequence[_Tag], values: dict[int, list[_Tag]]
    ) -> None:
        vertices: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for tag in tags:
            if tag.group_code == 10:
                current = {"x": _tag_float(tag)}
                vertices.append(current)
            elif current is not None and tag.group_code in {20, 40, 41, 42}:
                names = {20: "y", 40: "start_width", 41: "end_width", 42: "bulge"}
                current[names[tag.group_code]] = _tag_float(tag)
        elevation = _first_float(values, 38) or 0.0
        flags = _first_int(values, 70) or 0
        for index, vertex in enumerate(vertices):
            self._add_geometry_row(
                entity_key,
                kind="polyline_vertex",
                path=f"vertices[{index}]",
                x=vertex["x"],
                y=vertex.get("y", 0.0),
                z=elevation,
                bulge=vertex.get("bulge"),
                start_width=vertex.get("start_width"),
                end_width=vertex.get("end_width"),
                constant_width=_first_float(values, 43),
                elevation=elevation,
                closed=bool(flags & 1),
                flags=flags,
                values=values,
            )

    def _add_geometry_row(
        self,
        entity_key: str,
        *,
        kind: str,
        path: str,
        x: float | None,
        y: float | None,
        z: float | None,
        values: dict[int, list[_Tag]],
        bulge: float | None = None,
        start_width: float | None = None,
        end_width: float | None = None,
        constant_width: float | None = None,
        elevation: float | None = None,
        angle_degrees: float | None = None,
        closed: bool | None = None,
        flags: int | None = None,
    ) -> None:
        ordinal = self.geometry_ordinal_by_entity[entity_key]
        self.geometry_ordinal_by_entity[entity_key] += 1
        self.sinks["geometry_elements"].add(
            {
                "document_key": DOCUMENT_KEY,
                "geometry_element_key": f"{entity_key}_geom_{ordinal:05d}",
                "entity_key": entity_key,
                "geometry_component_kind": kind,
                "component_path": path,
                "ordinal": ordinal,
                "x": x,
                "y": y,
                "z": z,
                "bulge": bulge,
                "start_width": start_width,
                "end_width": end_width,
                "constant_width": constant_width,
                "elevation": elevation,
                "angle_degrees": angle_degrees
                if angle_degrees is not None
                else _first_float(values, 50),
                "closed": closed,
                "flags": flags,
            }
        )

    def _write_xdata(self, record: _Record, entity_key: str) -> None:
        appid: str | None = None
        appid_group_ordinal = -1
        tag_ordinal = 0
        for tag in record.tags:
            if tag.group_code == 1001:
                appid = tag.decoded_value
                appid_group_ordinal += 1
                tag_ordinal = 0
            elif appid is None or not 1000 <= tag.group_code <= 1071:
                continue
            typed = _typed_value(tag)
            self.sinks["xdata_tags"].add(
                {
                    "document_key": DOCUMENT_KEY,
                    "xdata_tag_key": (
                        f"{entity_key}_xdata_{appid_group_ordinal:04d}_{tag_ordinal:05d}"
                    ),
                    "entity_key": entity_key,
                    "appid": appid,
                    "appid_group_ordinal": appid_group_ordinal,
                    "tag_ordinal": tag_ordinal,
                    "is_appid_marker": tag.group_code == 1001,
                    "group_code": tag.group_code,
                    "raw_lexeme": tag.decoded_value,
                    "typed_value_kind": typed["kind"],
                    "string_value": typed["string"],
                    "int_value": typed["int"],
                    "float_value": typed["float"],
                    "binary_value": typed["binary"],
                    "source_raw_tag_key": tag.raw_tag_key,
                }
            )
            tag_ordinal += 1

    def _track_polyline_sequence(
        self, record: _Record, entity_key: str, *, typed_supported: bool
    ) -> None:
        if record.record_type == "POLYLINE":
            if self.active_polyline_key is not None:
                self._add_record_issue("polyline_sequence_missing_seqend", record)
            self.active_polyline_key = entity_key
            self.active_polyline_typed_supported = typed_supported
        elif record.record_type == "VERTEX" and self.active_polyline_key:
            self.pending_relationships.append(
                _PendingRelationship(
                    relation_kind="polyline_vertex",
                    subject_key=self.active_polyline_key,
                    target=entity_key,
                )
            )
        elif record.record_type == "SEQEND" and self.active_polyline_key:
            self.pending_relationships.append(
                _PendingRelationship(
                    relation_kind="polyline_sequence_end",
                    subject_key=self.active_polyline_key,
                    target=entity_key,
                )
            )
            self.active_polyline_key = None
            self.active_polyline_typed_supported = True

    def _capture_handle_relationships(self, record: _Record, subject_key: str) -> None:
        for tag in record.tags:
            code = tag.group_code
            if code == 330:
                kind = "owner"
            elif 320 <= code <= 369 or 390 <= code <= 399 or code in {480, 481, 1005}:
                kind = "xdata_handle_reference" if code == 1005 else "handle_reference"
            else:
                continue
            self.pending_relationships.append(
                _PendingRelationship(
                    relation_kind=kind,
                    subject_key=subject_key,
                    target=_normalize_handle(tag.decoded_value),
                )
            )

    def _write_relationships(self) -> None:
        for pending in self.pending_relationships:
            self.check_cancelled()
            resolved = self.handle_to_key.get(pending.target)
            # POLYLINE child links already carry package-local targets rather than handles.
            if pending.target.startswith("entrec_"):
                resolved = pending.target
            self.sinks["relationships"].add(
                {
                    "document_key": DOCUMENT_KEY,
                    "relationship_key": f"rel_{self.relationship_ordinal:09d}",
                    "relation_kind": pending.relation_kind,
                    "subject_key": pending.subject_key,
                    "target_handle_or_raw_target": pending.target,
                    "resolved_target_key": resolved,
                    "resolution_status": "resolved" if resolved else "unresolved",
                }
            )
            self.relationship_ordinal += 1

    def _write_diagnostics(self) -> None:
        diagnostics = self.output_dir / "diagnostics"
        diagnostics.mkdir()
        payload = {
            "schema": "cad-l0-parse-issues/v1",
            "source_name": self.source_name,
            "issue_count": len(self.parse_issues),
            "blocking_issue_count": sum(item["blocking"] for item in self.parse_issues),
            "issues": self.parse_issues,
        }
        (diagnostics / "parse_issues.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _write_manifest(self) -> None:
        blocking_count = sum(item["blocking"] for item in self.parse_issues)
        payloads = {}
        for name, sink in self.sinks.items():
            payloads[name] = {
                "format": "parquet",
                "row_count": sink.row_count,
                "members": [f"payloads/{name}/part-00000.parquet"],
                "arrow_schema": str(sink.schema),
            }
        manifest = {
            "publication_state": "staging_draft",
            "publication_note": (
                "The application service must bind exact Artifact versions "
                "and publish the final manifest."
            ),
            "observed_sections": self.observed_sections,
            "source": {
                "name": self.source_name,
                "format": "ascii_dxf",
                "byte_size": len(self.source_bytes),
                "sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                "dxf_version": self.dxf_version,
                "declared_codepage": self.declared_codepage,
                "decoded_encoding": self.decoded_encoding,
            },
            "payloads": payloads,
            "completeness": {
                "status": "complete",
                "successfully_parsed_group_pairs": self.sinks["raw_tags"].row_count,
                "parse_issue_count": len(self.parse_issues),
                "blocking_parse_issue_count": blocking_count,
                "all_parsed_tags_have_record": True,
            },
            "typed_geometry": {
                "supported_entity_types": sorted(_TYPED_GEOMETRY_TYPES),
                "entity_type_counts": dict(sorted(self.entity_type_counts.items())),
                "unsupported_entity_type_counts": dict(
                    sorted(self.unsupported_entity_counts.items())
                ),
            },
            "external_dependencies": [],
            "staging_diagnostics": {
                "path": "diagnostics/parse_issues.json",
                "publish_as_package_member": False,
            },
        }
        (self.output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _write_description(self) -> None:
        unsupported = self.unsupported_entity_counts
        unsupported_text = (
            ", ".join(f"{kind} ({count})" for kind, count in sorted(unsupported.items()))
            if unsupported
            else "None."
        )
        rows = "\n".join(
            f"- `{name}`: {sink.row_count:,} rows" for name, sink in self.sinks.items()
        )
        description = f"""# {self.source_name} — CAD L0 Source Package

## Summary

Deterministic source-fact extraction for the exact `{self.source_name}` byte version.

## Intended use

Query exact CAD records, tags, entities, geometry, XDATA, and source relationships
before deriving IFC modeling decisions.

## Contents

{rows}

## Method

Textual DXF group-code/value pairs are scanned in source order. Query tables use
explicit Arrow schemas and package-local stable keys. No business meaning, topology,
unit conversion, or IFC decision is inferred.

## Scope and completeness

Typed geometry covers LINE, POINT, LWPOLYLINE, POLYLINE, VERTEX, and SEQEND.
The exact source bytes remain authoritative in the source Artifact.

## Known limitations

Unsupported typed entity projections: {unsupported_text}
DWG, binary DXF, XREF, IMAGE, and UNDERLAY dependencies are rejected in phase one
rather than converted or ignored.
"""
        (self.output_dir / "description.md").write_text(description, encoding="utf-8")


def _read_source(source: bytes | Path, source_name: str | None) -> tuple[bytes, str]:
    if isinstance(source, bytes):
        return source, source_name or "source.dxf"
    path = Path(source)
    return path.read_bytes(), source_name or path.name


def _reject_unsupported_container(source: bytes, source_name: str) -> None:
    if source_name.lower().endswith(".dwg") or (
        source.startswith(b"AC10") and not source.lstrip().startswith(b"0")
    ):
        raise UnsupportedCadSourceError("DWG is not supported in phase one")
    if source.startswith(b"AutoCAD Binary DXF\r\n\x1a\x00"):
        raise UnsupportedCadSourceError("binary DXF is not supported in phase one")
    if not source_name.lower().endswith(".dxf"):
        raise UnsupportedCadSourceError("phase one accepts .dxf sources only")


def _select_text_encoding(source: bytes) -> str:
    try:
        source.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    declared = _scan_declared_codepage(source)
    encoding = {"ANSI_936": "gb18030"}.get(declared or "")
    if encoding is None:
        raise UnsupportedCadSourceError(
            "textual DXF encoding is unsupported in phase one"
        )
    try:
        source.decode(encoding)
    except UnicodeDecodeError as exc:
        raise UnsupportedCadSourceError(
            "textual DXF bytes do not match the declared codepage"
        ) from exc
    return encoding


def _scan_declared_codepage(source: bytes) -> str | None:
    lines = iter(_source_lines(source))
    waiting_for_value = False
    while True:
        try:
            code_line = next(lines)
            value_line = next(lines)
        except StopIteration:
            return None
        try:
            code = int(code_line.content.decode("ascii").strip())
            value = value_line.content.decode("ascii").strip()
        except (UnicodeDecodeError, ValueError):
            continue
        if waiting_for_value:
            return value if code == 3 else None
        waiting_for_value = code == 9 and value == "$DWGCODEPAGE"


def _source_lines(source: bytes) -> Iterator[_SourceLine]:
    offset = 0
    for physical_line in io.BytesIO(source):
        if physical_line.endswith(b"\r\n"):
            content = physical_line[:-2]
        elif physical_line.endswith((b"\r", b"\n")):
            content = physical_line[:-1]
        else:
            content = physical_line
        end = offset + len(physical_line)
        yield _SourceLine(content=content, byte_start=offset, byte_end=end)
        offset = end
    if offset < len(source):
        yield _SourceLine(content=source[offset:], byte_start=offset, byte_end=len(source))


def _parsed_tags(
    source: bytes, issues: list[dict[str, Any]], *, encoding: str,
    check_cancelled: Callable[[], None] = lambda: None,
) -> Iterator[_Tag]:
    iterator = iter(_source_lines(source))
    ordinal = 0
    while True:
        if ordinal % 1024 == 0:
            check_cancelled()
        try:
            code_line = next(iterator)
        except StopIteration:
            return
        try:
            value_line = next(iterator)
        except StopIteration:
            issues.append(
                {
                    "code": "truncated_group_pair",
                    "blocking": True,
                    "byte_start": code_line.byte_start,
                    "byte_end": code_line.byte_end,
                }
            )
            return
        try:
            raw_code_lexeme = code_line.content.decode("ascii")
            group_code = int(raw_code_lexeme.strip())
        except (UnicodeDecodeError, ValueError):
            issues.append(
                {
                    "code": "invalid_group_code",
                    "blocking": True,
                    "byte_start": code_line.byte_start,
                    "byte_end": value_line.byte_end,
                }
            )
            continue
        try:
            decoded_value = value_line.content.decode(encoding)
        except UnicodeDecodeError:
            # The top-level strict check normally catches this; retain an exact location if reused.
            issues.append(
                {
                    "code": "invalid_utf8_value",
                    "blocking": True,
                    "byte_start": value_line.byte_start,
                    "byte_end": value_line.byte_end,
                }
            )
            continue
        tag = _Tag(
            ordinal=ordinal,
            group_code=group_code,
            raw_code_lexeme=raw_code_lexeme,
            raw_value=value_line.content,
            decoded_value=decoded_value,
            byte_start=code_line.byte_start,
            byte_end=value_line.byte_end,
        )
        if _typed_value(tag)["kind"] == "unparsed":
            issues.append(
                {
                    "code": "invalid_typed_group_value",
                    "blocking": True,
                    "byte_start": tag.byte_start,
                    "byte_end": tag.byte_end,
                }
            )
        yield tag
        ordinal += 1


def _records_from_bytes(
    source: bytes, issues: list[dict[str, Any]], *, encoding: str,
    check_cancelled: Callable[[], None] = lambda: None,
) -> Iterator[_Record]:
    current_tags: list[_Tag] = []
    current_section = ""
    record_ordinal = 0

    def is_boundary(tag: _Tag) -> bool:
        if tag.group_code == 0:
            return True
        if tag.group_code != 9:
            return False
        if current_section == "HEADER":
            return True
        return bool(
            current_tags
            and current_tags[0].group_code == 0
            and current_tags[0].decoded_value.strip().upper() == "SECTION"
            and any(
                item.group_code == 2 and item.decoded_value.strip().upper() == "HEADER"
                for item in current_tags
            )
        )

    def finish() -> _Record | None:
        nonlocal current_section, record_ordinal
        if not current_tags:
            return None
        first = current_tags[0]
        record_type = first.decoded_value.strip() if first.group_code in {0, 9} else "UNATTACHED"
        section = current_section
        if record_type.upper() == "SECTION":
            section_tag = next((tag for tag in current_tags if tag.group_code == 2), None)
            section = section_tag.decoded_value.strip().upper() if section_tag else ""
        record = _Record(
            ordinal=record_ordinal,
            section=section,
            record_type=record_type.upper(),
            tags=tuple(current_tags),
        )
        record_ordinal += 1
        if record.record_type == "SECTION":
            current_section = record.section
        elif record.record_type == "ENDSEC":
            current_section = ""
        return record

    for tag in _parsed_tags(source, issues, encoding=encoding, check_cancelled=check_cancelled):
        if current_tags and is_boundary(tag):
            record = finish()
            if record is not None:
                yield record
            current_tags = []
        current_tags.append(tag)
    record = finish()
    if record is not None:
        yield record


def _record_domain(record: _Record) -> str:
    record_type = record.record_type
    section = record.section
    if record_type == "SECTION":
        return "section"
    if record_type == "ENDSEC":
        return "end_section"
    if record_type == "EOF":
        return "end_file"
    if section == "HEADER":
        return "header_variable"
    if section == "CLASSES":
        return "class"
    if section == "TABLES":
        return "table_container" if record_type in {"TABLE", "ENDTAB"} else "table_record"
    if section == "BLOCKS" and record_type == "BLOCK":
        return "block_begin"
    if section == "BLOCKS" and record_type == "ENDBLK":
        return "block_end"
    if section in {"ENTITIES", "BLOCKS"}:
        return "entity_child" if record_type in {"VERTEX", "SEQEND", "ATTRIB"} else "entity"
    if section == "OBJECTS":
        return "object"
    if section == "ACDSDATA":
        return "acds_schema" if record_type == "ACDSSCHEMA" else "acds_record"
    return "record"


def _entity_scope(
    record: _Record, values: dict[int, list[_Tag]], record_domain: str
) -> str:
    if record_domain == "entity_child":
        return {"VERTEX": "child_vertex", "ATTRIB": "attribute", "SEQEND": "sequence_end"}.get(
            record.record_type, "entity_child"
        )
    if record.section == "BLOCKS":
        return "block_member"
    if (_first_int(values, 67) or 0) == 1:
        return "top_level_paperspace"
    return "top_level_modelspace"


def _raw_tag_row(tag: _Tag, *, record: _Record, record_tag_ordinal: int) -> dict[str, Any]:
    typed = _typed_value(tag)
    return {
        "document_key": DOCUMENT_KEY,
        "record_key": record.record_key,
        "raw_tag_key": tag.raw_tag_key,
        "global_tag_ordinal": tag.ordinal,
        "record_tag_ordinal": record_tag_ordinal,
        "section": record.section,
        "group_code": tag.group_code,
        "raw_code_lexeme": tag.raw_code_lexeme,
        "raw_value_bytes": tag.raw_value,
        "value_kind": typed["kind"],
        "string_value": typed["string"],
        "int_value": typed["int"],
        "float_value": typed["float"],
        "binary_value": typed["binary"],
        "byte_start": tag.byte_start,
        "byte_end": tag.byte_end,
    }


def _typed_value(tag: _Tag) -> dict[str, Any]:
    code = tag.group_code
    value = tag.decoded_value
    result = {"kind": "string", "string": value, "int": None, "float": None, "binary": None}
    try:
        if (
            10 <= code <= 59
            or 110 <= code <= 149
            or 210 <= code <= 239
            or 460 <= code <= 469
            or 1010 <= code <= 1059
        ):
            result.update(kind="float", string=None, float=float(value.strip()))
        elif (
            60 <= code <= 79
            or 90 <= code <= 99
            or 160 <= code <= 179
            or 270 <= code <= 299
            or 370 <= code <= 389
            or 400 <= code <= 409
            or 420 <= code <= 429
            or 440 <= code <= 459
            or 1060 <= code <= 1071
        ):
            result.update(kind="int", string=None, int=int(value.strip()))
        elif 310 <= code <= 319 or code == 1004:
            result.update(kind="binary", string=None, binary=bytes.fromhex(value.strip()))
    except ValueError:
        # The pair itself is valid and exact bytes remain present; the typed projection is absent.
        result = {"kind": "unparsed", "string": value, "int": None, "float": None, "binary": None}
    return result


def _tags_by_code(tags: Iterable[_Tag]) -> dict[int, list[_Tag]]:
    result: defaultdict[int, list[_Tag]] = defaultdict(list)
    for tag in tags:
        result[tag.group_code].append(tag)
    return dict(result)


def _first_tag(values: dict[int, list[_Tag]], code: int) -> _Tag | None:
    tags = values.get(code)
    return tags[0] if tags else None


def _first_value(values: dict[int, list[_Tag]], code: int) -> str | None:
    tag = _first_tag(values, code)
    return tag.decoded_value if tag else None


def _normalize_handle(value: str) -> str:
    return value.strip().upper()


def _first_handle(values: dict[int, list[_Tag]], code: int) -> str | None:
    value = _first_value(values, code)
    return _normalize_handle(value) if value is not None else None


def _has_required_geometry(entity_type: str, values: dict[int, list[_Tag]]) -> bool:
    if entity_type == "LINE":
        return all(values.get(code) for code in (10, 20, 11, 21))
    if entity_type == "POINT":
        return all(values.get(code) for code in (10, 20))
    if entity_type == "LWPOLYLINE":
        return bool(values.get(10)) and bool(values.get(20))
    if entity_type in {"POLYLINE", "VERTEX"}:
        return all(values.get(code) for code in (10, 20, 30))
    return True


def _valid_lwpolyline_count(values: dict[int, list[_Tag]]) -> bool:
    declared_count = _first_int(values, 90)
    x_count = len(values.get(10, []))
    y_count = len(values.get(20, []))
    return declared_count is not None and declared_count == x_count == y_count


def _first_int(values: dict[int, list[_Tag]], code: int) -> int | None:
    value = _first_value(values, code)
    try:
        return int(value.strip()) if value is not None else None
    except ValueError:
        return None


def _first_float(values: dict[int, list[_Tag]], code: int) -> float | None:
    value = _first_value(values, code)
    try:
        return float(value.strip()) if value is not None else None
    except ValueError:
        return None


def _tag_float(tag: _Tag | None, *, default: float | None = None) -> float | None:
    if tag is None:
        return default
    try:
        return float(tag.decoded_value.strip())
    except ValueError:
        return default


def _point(
    values: dict[int, list[_Tag]],
    x_code: int,
    y_code: int,
    z_code: int,
    *,
    default: tuple[float, float, float],
) -> tuple[float, float, float]:
    x = _first_float(values, x_code)
    y = _first_float(values, y_code)
    z = _first_float(values, z_code)
    return (
        default[0] if x is None else x,
        default[1] if y is None else y,
        default[2] if z is None else z,
    )
