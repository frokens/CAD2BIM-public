"""One controlled execution path for registration and nested product geometry."""
from __future__ import annotations

from CAD2BIM_backend.infrastructure.geometry_runtime.build_context import GeometryBuildContext
from CAD2BIM_backend.infrastructure.geometry_runtime.modeling_capabilities import ModelingArena
from CAD2BIM_backend.infrastructure.worker_runtime.timing import timed_phase


def compile_geometry_interfaces(content, package_name):
    from CAD2BIM_backend.domain.geometry_runtime import MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS, MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES, MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES
    from CAD2BIM_backend.infrastructure.worker_runtime.python_bundle import compile_python_bundle
    return compile_python_bundle(content, package_name=package_name, entry_name="evaluate", entry_path="interfaces.py",
        reachable_only=True, allowed_import_roots={"math"}, controlled_import_roots={"math"},
        profile_label="Geometry interfaces", maximum_members=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBERS,
        maximum_member_bytes=MAX_GEOMETRY_AUTHORING_SOURCE_MEMBER_BYTES,
        maximum_total_bytes=MAX_GEOMETRY_AUTHORING_SOURCE_TOTAL_BYTES,
        reject_private_attributes=True, controlled_modeling=True)


def execute_geometry_build(program, parameters, geometries=None, *, allow_interfaces=True,
                           capture_construction=False):
    from CAD2BIM_backend.infrastructure.geometry_runtime.user_geometry import coerce_user_shape
    host = GeometryBuildContext(parameters, geometries, allow_interfaces=allow_interfaces)
    arena = ModelingArena(profile="geometry", **({"capture_construction": True} if capture_construction else {}))
    try:
        with timed_phase("load_geometry_program"):
            entry = program.load_entry(arena.import_module, execution_globals=arena.globals())
        with timed_phase("geometry_build"):
            authored = entry(arena.author_context(host))
            # Host chooses the registered object, never a user-overridable
            # shape()/serialization method. All arenas expire on every exit.
            try:
                shape = coerce_user_shape(arena._object(authored), entry_name="build")
            except TypeError as exc:
                # The closed return ABI uses fixed, value-free TypeError text.
                # Preserve the actionable one-shape requirement for authors.
                arena.reject(f"modeling output is not one valid registered shape: {exc}")
            except BaseException as exc:
                arena.reject(f"modeling output is not one valid registered shape: {type(exc).__name__}")
        arena.ensure_active()
        if capture_construction:
            with timed_phase("geometry_construction_detach"):
                return shape, host.interfaces(), arena.construction(authored, shape)
        return shape, host.interfaces()
    finally:
        arena.close()


def execute_geometry_interfaces(program, parameters):
    host = GeometryBuildContext(parameters)
    arena = ModelingArena(profile="geometry_interfaces")
    try:
        entry = program.load_entry(arena.import_module, execution_globals=arena.globals())
        result = entry(arena.author_context(host))
        arena.ensure_active()
        if result is not None:
            raise ValueError("interfaces.py:evaluate(ctx) must return None")
        return host.interfaces()
    finally:
        arena.close()


def execute_family_program(program, host, *, entry_name=None):
    arena = ModelingArena(profile="family_numeric")
    try:
        entry = program.load_entry(arena.import_module, execution_globals=arena.globals(), entry_name=entry_name)
        author_context = arena.author_context(host, family=True)
        with timed_phase("family_numeric_entry"):
            result = entry(author_context)
        arena.ensure_active()
        return arena.snapshot(result)
    finally:
        arena.close()


__all__ = ["execute_geometry_build", "execute_geometry_interfaces", "execute_family_program"]
