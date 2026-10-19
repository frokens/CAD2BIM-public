from __future__ import annotations

from pathlib import Path

from scripts.release.check_wheel import (
    FORBIDDEN_FLAT_IFC_MEMBERS,
    REQUIRED_DATA_QUERY_ATTEMPT_BACKEND_MEMBERS,
    REQUIRED_DATA_TRANSFORM_BACKEND_MEMBERS,
    REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS,
    REQUIRED_IFC_QUERY_BACKEND_MEMBERS,
    REQUIRED_IFC_REVISION_RUNTIME_BACKEND_MEMBERS,
    REQUIRED_NESTED_IFC_MEMBERS,
    REQUIRED_PACKAGE_DATA_MEMBERS,
    REQUIRED_PRODUCT_FAMILY_BACKEND_MEMBERS,
    REQUIRED_RUNNER_DOMAIN_MEMBERS,
    _BUILD_CONTEXT_PATHS,
    _copy_build_context,
    _missing_data_query_attempt_backend_members,
    _missing_data_transform_backend_members,
    _missing_geometry_backend_members,
    _missing_ifc_query_backend_members,
    _missing_ifc_revision_runtime_backend_members,
    _missing_product_family_backend_members,
    _missing_runner_domain_members,
    _missing_direct_members,
)


def test_wheel_contract_requires_runner_entry_and_new_domain_members() -> None:
    assert _missing_runner_domain_members(REQUIRED_RUNNER_DOMAIN_MEMBERS) == ()
    assert "CAD2BIM_mcp/runner/domain_errors.py" in REQUIRED_RUNNER_DOMAIN_MEMBERS
    without_runner_server = tuple(
        member for member in REQUIRED_RUNNER_DOMAIN_MEMBERS
        if member != "CAD2BIM_mcp/runner/server.py"
    )
    assert _missing_runner_domain_members(without_runner_server) == (
        "CAD2BIM_mcp/runner/server.py",
    )


def test_copy_build_context_excludes_stale_build_output(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for relative_path in _BUILD_CONTEXT_PATHS:
        source = repository / relative_path
        if relative_path == "src":
            source.mkdir()
            (source / "package.py").write_text("VALUE = 1\n", encoding="utf-8")
        else:
            source.write_text(f"{relative_path}\n", encoding="utf-8")

    stale_module = repository / "build/lib/CAD2BIM_backend/auth.py"
    stale_module.parent.mkdir(parents=True)
    stale_module.write_text("STALE = True\n", encoding="utf-8")
    stale_metadata = repository / "src/cad2bim.egg-info/SOURCES.txt"
    stale_metadata.parent.mkdir(parents=True)
    stale_metadata.write_text("build/lib/CAD2BIM_backend/auth.py\n", encoding="utf-8")
    frontend_dependency = repository / "src/BIM2CAD_frontend/node_modules/package/index.js"
    frontend_dependency.parent.mkdir(parents=True)
    frontend_dependency.write_text("export default 'not a wheel input'\n", encoding="utf-8")

    destination = tmp_path / "context"
    _copy_build_context(repository, destination)

    assert (destination / "src/package.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (destination / "build").exists()
    assert not (destination / "src/cad2bim.egg-info").exists()
    assert not (destination / "src/BIM2CAD_frontend/node_modules").exists()
    assert stale_module.is_file()


def test_wheel_contract_requires_nested_ifc_families_not_retired_flat_modules() -> None:
    members = tuple(REQUIRED_NESTED_IFC_MEMBERS)

    missing = _missing_direct_members(members)

    assert not set(REQUIRED_NESTED_IFC_MEMBERS) & set(missing)
    assert not set(FORBIDDEN_FLAT_IFC_MEMBERS) & set(missing)


def test_wheel_contract_detects_missing_nested_ifc_member() -> None:
    members = tuple(
        member for member in REQUIRED_NESTED_IFC_MEMBERS if not member.endswith("model/tools.py")
    )

    missing = _missing_direct_members(members)

    assert "CAD2BIM_mcp/direct/ifc/model/tools.py" in missing


def test_wheel_contract_requires_geometry_runtime_authoring_and_seed_manifest() -> None:
    members = (
        *REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS,
        *REQUIRED_PACKAGE_DATA_MEMBERS,
    )

    assert _missing_geometry_backend_members(members) == ()
    assert (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/"
        "20260903_0070_geometry_resource_write_tool_name.py"
        in REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS
    )
    assert (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/"
        "20260905_0075_geometry_query_targets.py" in REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS
    )
    assert (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/"
        "20260905_0077_geometry_interface_annotations.py"
        in REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS
    )
    assert (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/"
        "20260906_0078_system_curator_schema_readiness.py"
        in REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS
    )


def test_wheel_contract_detects_missing_geometry_seed_manifest() -> None:
    members = tuple(REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS)

    assert _missing_geometry_backend_members(members) == REQUIRED_PACKAGE_DATA_MEMBERS


def test_wheel_contract_detects_missing_geometry_query_target_migration() -> None:
    members = tuple(
        member
        for member in REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS
        if not member.endswith("20260905_0075_geometry_query_targets.py")
    )

    assert _missing_geometry_backend_members(members) == (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/"
        "20260905_0075_geometry_query_targets.py",
        *REQUIRED_PACKAGE_DATA_MEMBERS,
    )


def test_wheel_contract_detects_missing_geometry_interface_annotation_migration() -> None:
    members = tuple(
        member
        for member in REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS
        if not member.endswith("20260905_0077_geometry_interface_annotations.py")
    )

    assert _missing_geometry_backend_members(members) == (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/"
        "20260905_0077_geometry_interface_annotations.py",
        *REQUIRED_PACKAGE_DATA_MEMBERS,
    )


def test_wheel_contract_detects_missing_system_curator_readiness_migration() -> None:
    members = tuple(
        member
        for member in REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS
        if not member.endswith("20260906_0078_system_curator_schema_readiness.py")
    )

    assert _missing_geometry_backend_members(members) == (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/"
        "20260906_0078_system_curator_schema_readiness.py",
        *REQUIRED_PACKAGE_DATA_MEMBERS,
    )


def test_wheel_contract_requires_ifc_query_runtime_and_migration() -> None:
    assert _missing_ifc_query_backend_members(REQUIRED_IFC_QUERY_BACKEND_MEMBERS) == ()
    assert {
        "CAD2BIM_backend/application/ifc_query_service.py",
        "CAD2BIM_backend/application/ifc_revision_reader.py",
        "CAD2BIM_backend/application/exact_artifact_reader.py",
        "CAD2BIM_backend/ports/arrow_inputs.py",
        "CAD2BIM_backend/infrastructure/ifc_query/adapter.py",
        "CAD2BIM_backend/infrastructure/ifc_query/child.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260903_0069_ifc_query_attempts.py",
        "CAD2BIM_backend/infrastructure/postgres/repository_ifc_query.py",
        "CAD2BIM_backend/infrastructure/source_text/source_patch_adapter.py",
    } <= set(REQUIRED_IFC_QUERY_BACKEND_MEMBERS)
    assert {
        "CAD2BIM_mcp/direct/ifc/query/__init__.py",
        "CAD2BIM_mcp/direct/ifc/query/application.py",
        "CAD2BIM_mcp/direct/ifc/query/contracts.py",
        "CAD2BIM_mcp/direct/ifc/query/tools.py",
    } <= set(REQUIRED_NESTED_IFC_MEMBERS)


def test_wheel_contract_detects_missing_ifc_query_migration() -> None:
    members = tuple(
        member
        for member in REQUIRED_IFC_QUERY_BACKEND_MEMBERS
        if not member.endswith("20260903_0069_ifc_query_attempts.py")
    )

    assert _missing_ifc_query_backend_members(members) == (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260903_0069_ifc_query_attempts.py",
    )


def test_wheel_contract_requires_ifc_revision_runtime() -> None:
    assert (
        _missing_ifc_revision_runtime_backend_members(REQUIRED_IFC_REVISION_RUNTIME_BACKEND_MEMBERS)
        == ()
    )
    members = tuple(
        member
        for member in REQUIRED_IFC_REVISION_RUNTIME_BACKEND_MEMBERS
        if not member.endswith("ifc_revision_runtime/child.py")
    )
    assert _missing_ifc_revision_runtime_backend_members(members) == (
        "CAD2BIM_backend/infrastructure/ifc_revision_runtime/child.py",
    )


def test_wheel_contract_requires_data_query_attempt_runtime_and_migration() -> None:
    assert (
        _missing_data_query_attempt_backend_members(REQUIRED_DATA_QUERY_ATTEMPT_BACKEND_MEMBERS)
        == ()
    )
    assert {
        "CAD2BIM_backend/application/data_query_attempt_service.py",
        "CAD2BIM_backend/application/data_query_deadline.py",
        "CAD2BIM_backend/domain/data_query_attempt.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260904_0071_data_query_attempts.py",
        "CAD2BIM_backend/infrastructure/postgres/repository_data_query_attempt.py",
        "CAD2BIM_backend/ports/data_query_attempts.py",
    } <= set(REQUIRED_DATA_QUERY_ATTEMPT_BACKEND_MEMBERS)


def test_wheel_contract_detects_missing_data_query_attempt_migration() -> None:
    members = tuple(
        member
        for member in REQUIRED_DATA_QUERY_ATTEMPT_BACKEND_MEMBERS
        if not member.endswith("20260904_0071_data_query_attempts.py")
    )

    assert _missing_data_query_attempt_backend_members(members) == (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260904_0071_data_query_attempts.py",
    )


def test_wheel_contract_requires_unified_data_transform_migration() -> None:
    assert _missing_data_transform_backend_members(REQUIRED_DATA_TRANSFORM_BACKEND_MEMBERS) == ()
    assert _missing_data_transform_backend_members(()) == (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260904_0072_unify_data_transform_entry.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260928_0093_data_transform_operation.py",
    )


def test_wheel_contract_requires_product_family_catalog_and_migration() -> None:
    assert _missing_product_family_backend_members(REQUIRED_PRODUCT_FAMILY_BACKEND_MEMBERS) == ()
    assert {
        "CAD2BIM_backend/application/product_family_authoring_service.py",
        "CAD2BIM_backend/application/product_family_catalog_service.py",
        "CAD2BIM_backend/application/product_realization_input_service.py",
        "CAD2BIM_backend/application/product_realization_preview_service.py",
        "CAD2BIM_backend/application/validation_support.py",
        "CAD2BIM_backend/domain/product_family.py",
        "CAD2BIM_backend/domain/product_family_authoring.py",
        "CAD2BIM_backend/domain/product_family_authoring_contracts.py",
        "CAD2BIM_backend/domain/product_family_catalog.py",
        "CAD2BIM_backend/domain/product_realization.py",
        "CAD2BIM_backend/domain/product_realization_host.py",
        "CAD2BIM_backend/domain/product_realization_preview.py",
        "CAD2BIM_backend/domain/resource_scope.py",
        "CAD2BIM_backend/domain/version_usage.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260904_0073_product_family_catalog.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260904_0074_product_family_authoring.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260905_0076_product_family_repair_fence.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260909_0080_family_standard_model_assets.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260909_0082_family_self_geometry_contract.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260909_0083_family_current_write_guard.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260910_0084_geometry_resource_inputs.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260913_0085_version_usage_instructions.py",
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260920_0086_family_numeric_realization.py",
        "CAD2BIM_backend/infrastructure/postgres/repository_product_family_authoring.py",
        "CAD2BIM_backend/infrastructure/postgres/repository_product_family.py",
        "CAD2BIM_backend/infrastructure/ifc/product_realization_host.py",
        "CAD2BIM_backend/infrastructure/product_realization_runtime/__init__.py",
        "CAD2BIM_backend/infrastructure/product_realization_runtime/adapter.py",
        "CAD2BIM_backend/infrastructure/product_realization_runtime/child.py",
        "CAD2BIM_backend/infrastructure/product_realization_runtime/protocol.py",
        "CAD2BIM_backend/infrastructure/worker_runtime/python_bundle.py",
        "CAD2BIM_backend/ports/product_family_authoring.py",
        "CAD2BIM_backend/ports/product_families.py",
        "CAD2BIM_backend/ports/product_realization_host.py",
        "CAD2BIM_backend/ports/product_realization_runtime.py",
    } <= set(REQUIRED_PRODUCT_FAMILY_BACKEND_MEMBERS)
    assert {
        "CAD2BIM_mcp/direct/ifc/product_family/__init__.py",
        "CAD2BIM_mcp/direct/ifc/product_family/application.py",
        "CAD2BIM_mcp/direct/ifc/product_family/contracts.py",
        "CAD2BIM_mcp/direct/ifc/product_family/preview_application.py",
        "CAD2BIM_mcp/direct/ifc/product_family/preview_contracts.py",
        "CAD2BIM_mcp/direct/ifc/product_family/preview_tools.py",
        "CAD2BIM_mcp/direct/ifc/product_family/tools.py",
        "CAD2BIM_mcp/direct/ifc/product_family/write_application.py",
        "CAD2BIM_mcp/direct/ifc/product_family/write_contracts.py",
        "CAD2BIM_mcp/direct/ifc/product_family/write_tools.py",
    } <= set(REQUIRED_NESTED_IFC_MEMBERS)


def test_wheel_contract_detects_missing_product_family_migration() -> None:
    members = tuple(
        member
        for member in REQUIRED_PRODUCT_FAMILY_BACKEND_MEMBERS
        if not member.endswith("20260904_0073_product_family_catalog.py")
    )

    assert _missing_product_family_backend_members(members) == (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260904_0073_product_family_catalog.py",
    )


def test_wheel_contract_detects_missing_product_family_repair_fence_migration() -> None:
    members = tuple(
        member
        for member in REQUIRED_PRODUCT_FAMILY_BACKEND_MEMBERS
        if not member.endswith("20260905_0076_product_family_repair_fence.py")
    )

    assert _missing_product_family_backend_members(members) == (
        "CAD2BIM_backend/infrastructure/postgres/migrations/versions/"
        "20260905_0076_product_family_repair_fence.py",
    )


def test_wheel_contract_detects_missing_product_realization_runtime_member() -> None:
    members = tuple(
        member
        for member in REQUIRED_PRODUCT_FAMILY_BACKEND_MEMBERS
        if not member.endswith("product_realization_runtime/child.py")
    )

    assert _missing_product_family_backend_members(members) == (
        "CAD2BIM_backend/infrastructure/product_realization_runtime/child.py",
    )
