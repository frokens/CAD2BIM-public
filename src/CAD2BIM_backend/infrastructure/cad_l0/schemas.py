from __future__ import annotations

import pyarrow as pa


RECORDS_SCHEMA = pa.schema(
    [
        ("document_key", pa.string()),
        ("record_key", pa.string()),
        ("record_domain", pa.string()),
        ("section", pa.string()),
        ("record_type", pa.string()),
        ("source_ordinal", pa.int64()),
        ("handle", pa.string()),
        ("owner_handle", pa.string()),
        ("first_raw_tag_ordinal", pa.int64()),
        ("last_raw_tag_ordinal", pa.int64()),
        ("parse_disposition", pa.string()),
    ]
)

RAW_TAGS_SCHEMA = pa.schema(
    [
        ("document_key", pa.string()),
        ("record_key", pa.string()),
        ("raw_tag_key", pa.string()),
        ("global_tag_ordinal", pa.int64()),
        ("record_tag_ordinal", pa.int32()),
        ("section", pa.string()),
        ("group_code", pa.int32()),
        ("raw_code_lexeme", pa.string()),
        ("raw_value_bytes", pa.binary()),
        ("value_kind", pa.string()),
        ("string_value", pa.string()),
        ("int_value", pa.int64()),
        ("float_value", pa.float64()),
        ("binary_value", pa.binary()),
        ("byte_start", pa.int64()),
        ("byte_end", pa.int64()),
    ]
)

ENTITIES_SCHEMA = pa.schema(
    [
        ("document_key", pa.string()),
        ("record_key", pa.string()),
        ("entity_key", pa.string()),
        ("entity_scope", pa.string()),
        ("entity_type", pa.string()),
        ("section", pa.string()),
        ("handle", pa.string()),
        ("owner_handle", pa.string()),
        ("layer", pa.string()),
        ("layout", pa.string()),
        ("linetype", pa.string()),
        ("color_index", pa.int32()),
        ("true_color", pa.int64()),
        ("lineweight", pa.int32()),
        ("transparency", pa.int64()),
        ("visible", pa.bool_()),
        ("thickness", pa.float64()),
        ("extrusion_x", pa.float64()),
        ("extrusion_y", pa.float64()),
        ("extrusion_z", pa.float64()),
        ("typed_geometry_supported", pa.bool_()),
        ("unsupported_reason", pa.string()),
        ("parse_disposition", pa.string()),
    ]
)

GEOMETRY_ELEMENTS_SCHEMA = pa.schema(
    [
        ("document_key", pa.string()),
        ("geometry_element_key", pa.string()),
        ("entity_key", pa.string()),
        ("geometry_component_kind", pa.string()),
        ("component_path", pa.string()),
        ("ordinal", pa.int32()),
        ("x", pa.float64()),
        ("y", pa.float64()),
        ("z", pa.float64()),
        ("bulge", pa.float64()),
        ("start_width", pa.float64()),
        ("end_width", pa.float64()),
        ("constant_width", pa.float64()),
        ("elevation", pa.float64()),
        ("angle_degrees", pa.float64()),
        ("closed", pa.bool_()),
        ("flags", pa.int32()),
    ]
)

XDATA_TAGS_SCHEMA = pa.schema(
    [
        ("document_key", pa.string()),
        ("xdata_tag_key", pa.string()),
        ("entity_key", pa.string()),
        ("appid", pa.string()),
        ("appid_group_ordinal", pa.int32()),
        ("tag_ordinal", pa.int32()),
        ("is_appid_marker", pa.bool_()),
        ("group_code", pa.int32()),
        ("raw_lexeme", pa.string()),
        ("typed_value_kind", pa.string()),
        ("string_value", pa.string()),
        ("int_value", pa.int64()),
        ("float_value", pa.float64()),
        ("binary_value", pa.binary()),
        ("source_raw_tag_key", pa.string()),
    ]
)

RELATIONSHIPS_SCHEMA = pa.schema(
    [
        ("document_key", pa.string()),
        ("relationship_key", pa.string()),
        ("relation_kind", pa.string()),
        ("subject_key", pa.string()),
        ("target_handle_or_raw_target", pa.string()),
        ("resolved_target_key", pa.string()),
        ("resolution_status", pa.string()),
    ]
)

PAYLOAD_SCHEMAS: dict[str, pa.Schema] = {
    "records": RECORDS_SCHEMA,
    "raw_tags": RAW_TAGS_SCHEMA,
    "entities": ENTITIES_SCHEMA,
    "geometry_elements": GEOMETRY_ELEMENTS_SCHEMA,
    "xdata_tags": XDATA_TAGS_SCHEMA,
    "relationships": RELATIONSHIPS_SCHEMA,
}
