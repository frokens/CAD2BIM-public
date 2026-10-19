from __future__ import annotations

import pyarrow as pa


def _required(name: str, data_type: pa.DataType) -> pa.Field:
    return pa.field(name, data_type, nullable=False)


def _optional(name: str, data_type: pa.DataType) -> pa.Field:
    return pa.field(name, data_type, nullable=True)


S, F, INT, B = pa.string(), pa.float64(), pa.int64(), pa.bool_()

ATOMIC_EDGES_SCHEMA = pa.schema(
    [
        _required("atomic_edge_key", S),
        _required("document_key", S),
        _required("source_entity_key", S),
        _required("source_entity_type", S),
        _required("source_layer", S),
        _required("segment_ordinal", INT),
        _optional("source_start_geometry_element_key", S),
        _optional("source_end_geometry_element_key", S),
        _required("start_endpoint_key", S),
        _required("end_endpoint_key", S),
        *[_required(f"start_{axis}_m", F) for axis in "xyz"],
        *[_required(f"end_{axis}_m", F) for axis in "xyz"],
        *[_required(f"unit_{axis}", F) for axis in "xyz"],
        _required("length_3d_m", F),
        _required("length_xy_m", F),
        _required("is_closing_segment", B),
        _required("would_collapse_under_quantization", B),
    ]
)

ENDPOINTS_SCHEMA = pa.schema(
    [
        _required("endpoint_key", S),
        _required("atomic_edge_key", S),
        _required("document_key", S),
        _required("source_entity_key", S),
        _optional("source_geometry_element_key", S),
        _required("side", S),
        *[_required(f"raw_{axis}", F) for axis in "xyz"],
        *[_required(f"{axis}_m", F) for axis in "xyz"],
        *[_required(f"inward_tangent_{axis}", F) for axis in "xyz"],
        *[_required(f"quantized_i{axis}", INT) for axis in "xyz"],
        _required("quantized_endpoint_key", S),
        *[_required(f"quantized_{axis}_m", F) for axis in "xyz"],
        _required("quantization_deviation_3d_m", F),
    ]
)

ENDPOINT_PAIRS_SCHEMA = pa.schema(
    [
        *[
            _required(name, S)
            for name in (
                "endpoint_pair_key",
                "endpoint_a_key",
                "endpoint_b_key",
                "atomic_edge_a_key",
                "atomic_edge_b_key",
                "source_entity_a_key",
                "source_entity_b_key",
                "quantized_endpoint_a_key",
                "quantized_endpoint_b_key",
            )
        ],
        *[_required(f"delta_{axis}_m", F) for axis in "xyz"],
        _required("distance_xy_m", F),
        _required("distance_3d_m", F),
        _required("same_quantized_endpoint", B),
        _required("same_atomic_edge", B),
        _required("same_source_entity", B),
    ]
)

ENDPOINT_ON_PATH_SCHEMA = pa.schema(
    [
        *[
            _required(name, S)
            for name in (
                "endpoint_on_path_key",
                "branch_endpoint_key",
                "branch_atomic_edge_key",
                "branch_source_entity_key",
                "host_atomic_edge_key",
                "host_source_entity_key",
            )
        ],
        *[_required(f"projection_{axis}_m", F) for axis in "xyz"],
        _required("host_parameter", F),
        _required("distance_3d_m", F),
        _required("distance_xy_m", F),
        _required("signed_z_difference_m", F),
        _required("distance_to_host_start_m", F),
        _required("distance_to_host_end_m", F),
        _required("passes_host_endpoint_guard", B),
        *[_required(f"branch_tangent_{axis}", F) for axis in "xyz"],
        *[_required(f"host_tangent_{axis}", F) for axis in "xyz"],
        _required("same_source_entity", B),
        _required("source_edges_adjacent", B),
    ]
)

PLANAR_INTERSECTIONS_SCHEMA = pa.schema(
    [
        *[
            _required(name, S)
            for name in (
                "planar_intersection_key",
                "atomic_edge_a_key",
                "atomic_edge_b_key",
                "source_entity_a_key",
                "source_entity_b_key",
                "intersection_kind",
            )
        ],
        *[
            _required(name, F)
            for name in (
                "xy_start_x_m",
                "xy_start_y_m",
                "xy_end_x_m",
                "xy_end_y_m",
                "a_parameter_start",
                "a_parameter_end",
                "b_parameter_start",
                "b_parameter_end",
                "a_z_start_m",
                "a_z_end_m",
                "b_z_start_m",
                "b_z_end_m",
                "signed_z_gap_start_m",
                "signed_z_gap_end_m",
                "absolute_z_gap_start_m",
                "absolute_z_gap_end_m",
            )
        ],
        _required("same_source_entity", B),
        _required("source_edges_adjacent", B),
    ]
)

POINT_TO_PATH_SCHEMA = pa.schema(
    [
        *[
            _required(name, S)
            for name in (
                "point_to_path_key",
                "scope_key",
                "point_entity_key",
                "point_geometry_element_key",
                "host_atomic_edge_key",
                "host_source_entity_key",
            )
        ],
        *[_required(f"point_{axis}_m", F) for axis in "xyz"],
        *[_required(f"projection_{axis}_m", F) for axis in "xyz"],
        *[
            _required(name, F)
            for name in (
                "host_parameter",
                "distance_3d_m",
                "distance_xy_m",
                "signed_z_difference_m",
                "distance_to_host_start_m",
                "distance_to_host_end_m",
            )
        ],
        _required("nearest_host_endpoint_side", S),
        *[_required(f"host_tangent_{axis}", F) for axis in "xyz"],
        _required("host_start_quantized_endpoint_key", S),
        _required("host_end_quantized_endpoint_key", S),
        _optional("candidate_quantized_endpoint_key", S),
        _required("target_kind", S),
        _required("target_key", S),
        _required("raw_candidate_rank", INT),
        _required("distinct_target_rank", INT),
        _required("distance_gap_to_best_m", F),
        _optional("best_to_second_distinct_target_gap_m", F),
        _required("within_uniqueness_margin", B),
        _required("distinct_target_count_within_margin", INT),
    ]
)

CURVE_TURNS_SCHEMA = pa.schema(
    [
        _required("curve_turn_key", S),
        _required("document_key", S),
        _required("source_entity_key", S),
        _required("vertex_ordinal", INT),
        *[_required(f"vertex_{axis}_m", F) for axis in "xyz"],
        _required("incoming_atomic_edge_key", S),
        _required("outgoing_atomic_edge_key", S),
        *[_required(f"incoming_tangent_{axis}", F) for axis in "xyz"],
        *[_required(f"outgoing_tangent_{axis}", F) for axis in "xyz"],
        _required("dot_product", F),
        *[_required(f"cross_{axis}", F) for axis in "xyz"],
        _required("deflection_3d_deg", F),
        _optional("deflection_xy_deg", F),
        _optional("signed_xy_turn_deg", F),
        _required("within_straight_tolerance", B),
        _required("is_closing_vertex", B),
    ]
)

ENTITY_BOUNDS_SCHEMA = pa.schema(
    [
        _required("document_key", S),
        _required("source_entity_key", S),
        _required("source_entity_type", S),
        _required("source_layer", S),
        _required("selected_as_path", B),
        _required("selected_as_point", B),
        _required("geometry_element_count", INT),
        *[_required(f"min_{axis}_m", F) for axis in "xyz"],
        *[_required(f"max_{axis}_m", F) for axis in "xyz"],
        *[_required(f"center_{axis}_m", F) for axis in "xyz"],
        *[_required(f"size_{axis}_m", F) for axis in "xyz"],
    ]
)

LOCAL_FRAMES_SCHEMA = pa.schema(
    [
        _required("frame_key", S),
        _required("atomic_edge_key", S),
        *[_required(f"origin_{axis}_m", F) for axis in "xyz"],
        *[_required(f"x_axis_{axis}", F) for axis in "xyz"],
        *[_required(f"y_axis_{axis}", F) for axis in "xyz"],
        *[_required(f"z_axis_{axis}", F) for axis in "xyz"],
    ]
)

TOLERANCE_DIAGNOSTICS_SCHEMA = pa.schema(
    [
        _required("diagnostic_key", S),
        _required("row_kind", S),
        _required("diagnostic_code", S),
        _required("severity", S),
        _required("relation_family", S),
        _optional("scope_key", S),
        _optional("count", INT),
        _optional("sample_ordinal", INT),
        _optional("source_entity_key", S),
        _optional("atomic_edge_key", S),
        _optional("measured_value", F),
        _optional("threshold_name", S),
        _optional("threshold_value", F),
    ]
)

MEASUREMENT_PAYLOAD_SCHEMAS: dict[str, pa.Schema] = {
    "atomic_edges": ATOMIC_EDGES_SCHEMA,
    "endpoints": ENDPOINTS_SCHEMA,
    "endpoint_pairs": ENDPOINT_PAIRS_SCHEMA,
    "endpoint_on_path": ENDPOINT_ON_PATH_SCHEMA,
    "planar_intersections": PLANAR_INTERSECTIONS_SCHEMA,
    "point_to_path": POINT_TO_PATH_SCHEMA,
    "curve_turns": CURVE_TURNS_SCHEMA,
    "entity_bounds": ENTITY_BOUNDS_SCHEMA,
    "local_frames": LOCAL_FRAMES_SCHEMA,
    "tolerance_diagnostics": TOLERANCE_DIAGNOSTICS_SCHEMA,
}

MEASUREMENT_LOGICAL_KEYS: dict[str, tuple[str, ...]] = {
    "atomic_edges": ("atomic_edge_key",),
    "endpoints": ("endpoint_key",),
    "endpoint_pairs": ("endpoint_pair_key",),
    "endpoint_on_path": ("endpoint_on_path_key",),
    "planar_intersections": ("planar_intersection_key",),
    "point_to_path": ("point_to_path_key",),
    "curve_turns": ("curve_turn_key",),
    "entity_bounds": ("source_entity_key",),
    "local_frames": ("frame_key",),
    "tolerance_diagnostics": ("diagnostic_key",),
}
