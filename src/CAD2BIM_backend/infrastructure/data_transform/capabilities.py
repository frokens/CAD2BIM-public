"""Single source of truth for the controlled-local Python/Arrow runtime."""

from __future__ import annotations

from dataclasses import dataclass


PYTHON_ARROW_APPROVED_LIBRARIES: tuple[str, ...] = (
    "duckdb",
    "pyarrow",
    "pandas",
    "numpy",
    "scipy",
    "shapely",
    "networkx",
    "polars",
    "pyproj",
    "rapidfuzz",
    "pydantic",
    "jsonschema",
)

# These modules provide ordinary in-memory computation without exposing host I/O,
# dynamic loading, process creation, or concurrency primitives to transformation code.
PYTHON_ARROW_SAFE_STDLIB: tuple[str, ...] = (
    "array",
    "bisect",
    "collections",
    "copy",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "fractions",
    "functools",
    "heapq",
    "itertools",
    "json",
    "math",
    "operator",
    "re",
    "statistics",
    "string",
    "typing",
)


@dataclass(frozen=True, slots=True)
class TransformRuntimeCapabilities:
    """Discoverable facts for Direct controlled-local scheme A only."""

    engine: str = "python_arrow"
    isolation_profile: str = "controlled_local_scheme_a_darwin_seatbelt"
    os_isolation_enforced: bool = True
    approved_local_platforms: tuple[str, ...] = ("darwin",)
    libraries: tuple[str, ...] = PYTHON_ARROW_APPROVED_LIBRARIES
    safe_stdlib: tuple[str, ...] = PYTHON_ARROW_SAFE_STDLIB
    arrow_only_write_table: bool = True
    json_enabled: bool = True
    pip_install_enabled: bool = False
    network_enabled: bool = False
    host_paths_enabled: bool = False


PYTHON_ARROW_RUNTIME_CAPABILITIES = TransformRuntimeCapabilities()


__all__ = [
    "PYTHON_ARROW_APPROVED_LIBRARIES",
    "PYTHON_ARROW_RUNTIME_CAPABILITIES",
    "PYTHON_ARROW_SAFE_STDLIB",
    "TransformRuntimeCapabilities",
]
