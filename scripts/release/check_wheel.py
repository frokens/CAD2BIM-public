"""Build and inspect the CAD2BIM wheel without resolving network dependencies."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_ENTRYPOINTS = {
    "cad2bim-mcp-direct": "CAD2BIM_mcp.direct.entry:cli",
    "cad2bim-mcp-runner": "CAD2BIM_mcp.runner.server:cli",
    "cad2bim-checkpointer-setup": ("CAD2BIM_backend.infrastructure.langgraph.setup:cli"),
    "cad2bim-db-runtime-roles": ("CAD2BIM_backend.infrastructure.postgres.runtime_roles:cli"),
    "cad2bim-system-geometry-seed": ("CAD2BIM_backend.infrastructure.geometry_seed.cli:cli"),
    "cad2bim-chatgpt": "CAD2BIM_backend.infrastructure.chatgpt_cli:cli",
    "cad2bim-component": "CAD2BIM_backend.infrastructure.component_demo:cli",
}
REQUIRED_DIRECT_MEMBERS = (
    "CAD2BIM_mcp/tool_docs/__init__.py",
    "CAD2BIM_mcp/tool_docs/ifc_geometry.py",
    "CAD2BIM_mcp/tool_docs/ifc_product_family.py",
    "CAD2BIM_mcp/tool_docs/preview.py",
    "CAD2BIM_mcp/direct/__init__.py",
    "CAD2BIM_mcp/direct/application.py",
    "CAD2BIM_mcp/direct/application_support.py",
    "CAD2BIM_mcp/direct/bootstrap.py",
    "CAD2BIM_mcp/direct/config.py",
    "CAD2BIM_mcp/direct/contract_base.py",
    "CAD2BIM_mcp/direct/directory_budget.py",
    "CAD2BIM_mcp/direct/entry.py",
    "CAD2BIM_mcp/direct/errors.py",
    "CAD2BIM_mcp/direct/invocation.py",
    "CAD2BIM_mcp/direct/protocol.py",
    "CAD2BIM_mcp/direct/provider_schema.py",
    "CAD2BIM_mcp/direct/server.py",
    "CAD2BIM_mcp/direct/server_support.py",
    "CAD2BIM_mcp/direct/source_view.py",
    "CAD2BIM_mcp/direct/tool_support.py",
)
DIRECT_CAPABILITY_FAMILIES = (
    "session",
    "catalog",
    "operation",
    "code",
    "query",
    "transform",
    "cad",
)
DIRECT_RESPONSE_CONTRACT_FAMILIES = tuple(
    family for family in DIRECT_CAPABILITY_FAMILIES if family != "query"
)
REQUIRED_NESTED_IFC_MEMBERS = (
    "CAD2BIM_mcp/direct/ifc/__init__.py",
    "CAD2BIM_mcp/direct/ifc/model/__init__.py",
    "CAD2BIM_mcp/direct/ifc/model/application.py",
    "CAD2BIM_mcp/direct/ifc/model/contracts.py",
    "CAD2BIM_mcp/direct/ifc/model/tools.py",
    "CAD2BIM_mcp/direct/ifc/official_docs/__init__.py",
    "CAD2BIM_mcp/direct/ifc/official_docs/application.py",
    "CAD2BIM_mcp/direct/ifc/official_docs/tools.py",
    "CAD2BIM_mcp/direct/ifc/geometry/__init__.py",
    "CAD2BIM_mcp/direct/ifc/geometry/application.py",
    "CAD2BIM_mcp/direct/ifc/geometry/authoring_application.py",
    "CAD2BIM_mcp/direct/ifc/geometry/authoring_contracts.py",
    "CAD2BIM_mcp/direct/ifc/geometry/authoring_tools.py",
    "CAD2BIM_mcp/direct/ifc/geometry/contracts.py",
    "CAD2BIM_mcp/direct/ifc/geometry/tools.py",
    "CAD2BIM_mcp/direct/ifc/query/__init__.py",
    "CAD2BIM_mcp/direct/ifc/query/application.py",
    "CAD2BIM_mcp/direct/ifc/query/contracts.py",
    "CAD2BIM_mcp/direct/ifc/query/tools.py",
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
)
REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS = (
    "CAD2BIM_backend/application/exact_artifact_reader.py",
    "CAD2BIM_backend/application/geometry_authoring_service.py",
    "CAD2BIM_backend/application/geometry_catalog_service.py",
    "CAD2BIM_backend/application/geometry_preview_service.py",
    "CAD2BIM_backend/application/geometry_query_service.py",
    "CAD2BIM_backend/application/geometry_source_service.py",
    "CAD2BIM_backend/application/system_geometry_seed_service.py",
    "CAD2BIM_backend/application/text_source_view.py",
    "CAD2BIM_backend/domain/geometry_catalog.py",
    "CAD2BIM_backend/domain/geometry_authoring.py",
    "CAD2BIM_backend/domain/geometry_query.py",
    "CAD2BIM_backend/domain/geometry_runtime.py",
    "CAD2BIM_backend/domain/validation_diagnostics.py",
    "CAD2BIM_backend/domain/geometry_frame.py",
    "CAD2BIM_backend/domain/geometry_interface.py",
    "CAD2BIM_backend/domain/system_geometry_seed.py",
    "CAD2BIM_backend/domain/text_source.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/__init__.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/adapter.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/build_child.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/build_context.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/codec.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/codec_execution.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/codec_execution_protocol.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/codec_child.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/user_geometry.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/query_child.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/render.py",
    "CAD2BIM_backend/infrastructure/geometry_runtime/straight_profile.py",
    "CAD2BIM_backend/infrastructure/geometry_seed/__init__.py",
    "CAD2BIM_backend/infrastructure/geometry_seed/cli.py",
    "CAD2BIM_backend/infrastructure/geometry_seed/loader.py",
    "CAD2BIM_backend/infrastructure/geometry_seed/resources/__init__.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260902_0067_geometry_query_attempts.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260902_0068_geometry_authoring_operations.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260903_0070_geometry_resource_write_tool_name.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260905_0075_geometry_query_targets.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260905_0077_geometry_interface_annotations.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260906_0078_system_curator_schema_readiness.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260907_0079_opaque_reference_index.py",
    "CAD2BIM_backend/infrastructure/postgres/repository_reference_index.py",
    "CAD2BIM_backend/application/opaque_reference_service.py",
    "CAD2BIM_backend/ports/reference_index.py",
    "CAD2BIM_mcp/direct/references.py",
    "CAD2BIM_backend/infrastructure/postgres/repository_geometry_authoring.py",
    "CAD2BIM_backend/infrastructure/postgres/repository_geometry_query.py",
    "CAD2BIM_backend/infrastructure/source_text/__init__.py",
    "CAD2BIM_backend/infrastructure/source_text/geometry_authoring.py",
    "CAD2BIM_backend/infrastructure/source_text/source_bundle.py",
    "CAD2BIM_backend/infrastructure/source_text/source_patch.py",
    "CAD2BIM_backend/infrastructure/source_text/source_bundle_patch.py",
    "CAD2BIM_backend/infrastructure/worker_runtime/__init__.py",
    "CAD2BIM_backend/infrastructure/worker_runtime/capacity.py",
    "CAD2BIM_backend/infrastructure/worker_runtime/darwin_seatbelt.py",
    "CAD2BIM_backend/infrastructure/worker_runtime/protocol.py",
    "CAD2BIM_backend/infrastructure/worker_runtime/python_user_code.py",
    "CAD2BIM_backend/infrastructure/worker_runtime/supervisor.py",
    "CAD2BIM_backend/ports/geometry_authoring.py",
    "CAD2BIM_backend/ports/geometry_queries.py",
    "CAD2BIM_backend/ports/geometry_runtime.py",
)
REQUIRED_PACKAGE_DATA_MEMBERS = (
    "CAD2BIM_backend/infrastructure/geometry_seed/resources/manifest.json",
    "CAD2BIM_backend/infrastructure/chatgpt_auth/main.ts",
    "CAD2BIM_backend/infrastructure/chatgpt_auth/package.json",
    "CAD2BIM_backend/infrastructure/chatgpt_auth/package-lock.json",
    "CAD2BIM_backend/infrastructure/chatgpt_auth/vendor/LICENSE",
    "CAD2BIM_backend/infrastructure/langgraph/component_graph.py",
    "CAD2BIM_backend/infrastructure/langgraph/role_graph.py",
    "CAD2BIM_backend/application/product_apply_service.py",
    "CAD2BIM_backend/application/product_edit_service.py",
    "CAD2BIM_backend/application/product_edit_report.py",
    "CAD2BIM_backend/application/product_batch_geometry.py",
    "CAD2BIM_backend/application/product_realization_report.py",
    "CAD2BIM_backend/application/product_report.py",
    "CAD2BIM_backend/domain/product_edit.py",
    "CAD2BIM_backend/infrastructure/ifc/product_edit.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20261017_0113_ifc_products_edit.py",
    "CAD2BIM_mcp/direct/ifc/products/tools.py",
)
REQUIRED_IFC_QUERY_BACKEND_MEMBERS = (
    "CAD2BIM_backend/application/exact_artifact_reader.py",
    "CAD2BIM_backend/application/ifc_query_service.py",
    "CAD2BIM_backend/application/ifc_revision_reader.py",
    "CAD2BIM_backend/domain/ifc_query.py",
    "CAD2BIM_backend/ports/arrow_inputs.py",
    "CAD2BIM_backend/infrastructure/ifc_query/__init__.py",
    "CAD2BIM_backend/infrastructure/ifc_query/adapter.py",
    "CAD2BIM_backend/infrastructure/ifc_query/child.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260903_0069_ifc_query_attempts.py",
    "CAD2BIM_backend/infrastructure/postgres/repository_ifc_query.py",
    "CAD2BIM_backend/infrastructure/source_text/source_patch_adapter.py",
    "CAD2BIM_backend/ports/ifc_queries.py",
    "CAD2BIM_backend/ports/ifc_query_worker.py",
    "CAD2BIM_backend/ports/ifc_revision_reader.py",
    "CAD2BIM_backend/ports/source_patch.py",
)
REQUIRED_IFC_REVISION_RUNTIME_BACKEND_MEMBERS = (
    "CAD2BIM_backend/application/ifc_write_coordinator.py",
    "CAD2BIM_backend/infrastructure/ifc_revision_runtime/__init__.py",
    "CAD2BIM_backend/infrastructure/ifc_revision_runtime/adapter.py",
    "CAD2BIM_backend/infrastructure/ifc_revision_runtime/child.py",
    "CAD2BIM_backend/infrastructure/ifc_revision_runtime/protocol.py",
    "CAD2BIM_backend/ports/ifc_revision_runtime.py",
)
REQUIRED_DATA_QUERY_ATTEMPT_BACKEND_MEMBERS = (
    "CAD2BIM_backend/application/data_query_attempt_service.py",
    "CAD2BIM_backend/application/data_query_deadline.py",
    "CAD2BIM_backend/domain/data_query_attempt.py",
    "CAD2BIM_backend/infrastructure/memory/data_query_attempt_repository.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260904_0071_data_query_attempts.py",
    "CAD2BIM_backend/infrastructure/postgres/repository_data_query_attempt.py",
    "CAD2BIM_backend/ports/data_query_attempts.py",
)
REQUIRED_DATA_TRANSFORM_BACKEND_MEMBERS = (
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260904_0072_unify_data_transform_entry.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260928_0093_data_transform_operation.py",
)
REQUIRED_RUNNER_DOMAIN_MEMBERS = (
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260928_0092_feedback_delivery.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260928_0094_authoring_execution_claim.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260928_0095_ifc_execution_claim.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260928_0096_ifc_candidate_selection.py",
    "CAD2BIM_backend/api.py",
    "CAD2BIM_backend/bootstrap.py",
    "CAD2BIM_backend/data_application_resources.py",
    "CAD2BIM_backend/ifc_application_resources.py",
    "CAD2BIM_backend/internal_authoring_api.py",
    "CAD2BIM_backend/internal_authoring_contracts.py",
    "CAD2BIM_backend/internal_data_api.py",
    "CAD2BIM_backend/internal_domain_api.py",
    "CAD2BIM_backend/internal_ifc_api.py",
    "CAD2BIM_backend/infrastructure/mcp_domain_client.py",
    "CAD2BIM_backend/infrastructure/runner_domain_tokens.py",
    "CAD2BIM_mcp/runner/__init__.py",
    "CAD2BIM_mcp/runner/authoring_tools.py",
    "CAD2BIM_mcp/runner/backend_client.py",
    "CAD2BIM_mcp/runner/data_contracts.py",
    "CAD2BIM_mcp/runner/data_tools.py",
    "CAD2BIM_mcp/runner/domain_errors.py",
    "CAD2BIM_mcp/runner/ifc_contracts.py",
    "CAD2BIM_mcp/runner/ifc_tools.py",
    "CAD2BIM_mcp/runner/resources.py",
    "CAD2BIM_mcp/runner/server.py",
    "CAD2BIM_mcp/runner/references.py",
    "CAD2BIM_mcp/runner/tool_projection.py",
)
REQUIRED_PRODUCT_FAMILY_BACKEND_MEMBERS = (
    "CAD2BIM_backend/application/catalog_scope.py",
    "CAD2BIM_backend/application/product_family_authoring_service.py",
    "CAD2BIM_backend/application/product_family_asset_service.py",
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
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260920_0087_ephemeral_authoring_validation.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260920_0088_retire_ifc_products_apply.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260924_0089_product_family_repair_definition.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260927_0090_family_binding_options.py",
    "CAD2BIM_backend/infrastructure/postgres/migrations/versions/20260927_0091_geometry_source_assets.py",
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
)
FORBIDDEN_FLAT_IFC_MEMBERS = (
    "CAD2BIM_mcp/direct/ifc/application.py",
    "CAD2BIM_mcp/direct/ifc/contracts.py",
    "CAD2BIM_mcp/direct/ifc/tools.py",
)
FORBIDDEN_PREFIXES = (
    "CAD2BIM_agent/",
    "CAD2BIM_backend/api/",
    "CAD2BIM_backend/operations/",
    "CAD2BIM_backend/runner/",
    "CAD2BIM_backend/infrastructure/ifc_runtime/",
    "CAD2BIM_backend/infrastructure/mcp/",
    "CAD2BIM_backend/infrastructure/models/",
    "CAD2BIM_backend/infrastructure/react_runtime/",
    "CAD2BIM_backend/infrastructure/workflow_runtime/",
    "CAD2BIM_backend/infrastructure/workspace_exec/",
    "CAD2BIM_backend/context_resources/",
    "CAD2BIM_backend/domain/ifc_plan/",
    "CAD2BIM_mcp/cad_tools/",
    "CAD2BIM_mcp/descriptions/",
    "CAD2BIM_mcp/doc_tools/",
    "CAD2BIM_mcp/ifc_tools/",
)
FORBIDDEN_ROOT_MEMBERS = (
    "CAD2BIM_backend/application/ifc_products_service.py",
    "CAD2BIM_backend/domain/ifc_products.py",
    "CAD2BIM_backend/infrastructure/ifc/products.py",
    "CAD2BIM_backend/application/product_realization_host_service.py",
    "CAD2BIM_backend/main.py",
    "CAD2BIM_backend/mcp_service_auth.py",
    "CAD2BIM_backend/mcp_task_context_example.py",
    "CAD2BIM_backend/security.py",
    "CAD2BIM_backend/domain/component_authoring.py",
    "CAD2BIM_backend/domain/component_candidate.py",
    "CAD2BIM_backend/domain/component_catalog.py",
    "CAD2BIM_backend/domain/component_family.py",
    "CAD2BIM_backend/domain/component_source_authority.py",
    "CAD2BIM_backend/domain/component_types.py",
    "CAD2BIM_backend/domain/feedback_events.py",
    "CAD2BIM_backend/domain/ifc_generation.py",
    "CAD2BIM_backend/domain/operation_protocol.py",
    "CAD2BIM_backend/domain/resource_contracts.py",
    "CAD2BIM_backend/domain/session_savepoints.py",
    "CAD2BIM_backend/domain/stage_handoff.py",
    "CAD2BIM_backend/domain/task_tree.py",
    "CAD2BIM_backend/domain/task_tree_compiler.py",
    "CAD2BIM_backend/domain/task_tree_contracts.py",
    "CAD2BIM_backend/domain/user_savepoints.py",
    "CAD2BIM_backend/domain/writer_fence.py",
    "CAD2BIM_backend/ports/repositories.py",
    "CAD2BIM_mcp/http_entry.py",
    "CAD2BIM_mcp/internal_session_api.py",
    "CAD2BIM_mcp/main.py",
    "CAD2BIM_mcp/workspace_input_registry.py",
    "CAD2BIM_mcp/workspace_paths.py",
    "CAD2BIM_mcp/workspace_service.py",
    "CAD2BIM_mcp/write_receipts.py",
    "CAD2BIM_mcp/writer_fence.py",
)
_BUILD_CONTEXT_PATHS = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "alembic.ini",
    "src",
)
_IGNORED_BUILD_CONTEXT_PATTERNS = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    "*.egg-info",
    "node_modules",
)


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True, timeout=180)


def _copy_build_context(repository_root: Path, destination: Path) -> None:
    """Copy only wheel inputs so ignored build output cannot contaminate the package."""

    destination.mkdir()
    for relative_path in _BUILD_CONTEXT_PATHS:
        source = repository_root / relative_path
        target = destination / relative_path
        if source.is_dir():
            shutil.copytree(source, target, ignore=_IGNORED_BUILD_CONTEXT_PATTERNS)
        else:
            shutil.copy2(source, target)


def _missing_direct_members(members: tuple[str, ...]) -> tuple[str, ...]:
    required = REQUIRED_DIRECT_MEMBERS
    required += tuple(
        member
        for family in DIRECT_CAPABILITY_FAMILIES
        for member in (
            f"CAD2BIM_mcp/direct/{family}/__init__.py",
            f"CAD2BIM_mcp/direct/{family}/application.py",
            f"CAD2BIM_mcp/direct/{family}/tools.py",
        )
    )
    required += tuple(
        f"CAD2BIM_mcp/direct/{family}/contracts.py" for family in DIRECT_RESPONSE_CONTRACT_FAMILIES
    )
    required += REQUIRED_NESTED_IFC_MEMBERS
    return tuple(member for member in required if member not in members)


def _missing_geometry_backend_members(members: tuple[str, ...]) -> tuple[str, ...]:
    required = (
        *REQUIRED_GEOMETRY_CAPABILITY_BACKEND_MEMBERS,
        *REQUIRED_PACKAGE_DATA_MEMBERS,
    )
    return tuple(member for member in required if member not in members)


def _missing_ifc_query_backend_members(members: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(member for member in REQUIRED_IFC_QUERY_BACKEND_MEMBERS if member not in members)


def _missing_ifc_revision_runtime_backend_members(
    members: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        member for member in REQUIRED_IFC_REVISION_RUNTIME_BACKEND_MEMBERS if member not in members
    )


def _missing_data_query_attempt_backend_members(members: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        member for member in REQUIRED_DATA_QUERY_ATTEMPT_BACKEND_MEMBERS if member not in members
    )


def _missing_data_transform_backend_members(members: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        member for member in REQUIRED_DATA_TRANSFORM_BACKEND_MEMBERS if member not in members
    )


def _missing_runner_domain_members(members: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(member for member in REQUIRED_RUNNER_DOMAIN_MEMBERS if member not in members)


def _missing_product_family_backend_members(members: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        member for member in REQUIRED_PRODUCT_FAMILY_BACKEND_MEMBERS if member not in members
    )


def verify_wheel(*, repository_root: Path = REPOSITORY_ROOT) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="cad2bim-wheel-") as temporary_directory:
        temporary = Path(temporary_directory)
        build_context = temporary / "source"
        wheel_directory = temporary / "wheel"
        install_directory = temporary / "installed"
        _copy_build_context(repository_root, build_context)
        wheel_directory.mkdir()
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("uv is required for an explicitly offline wheel build")
        _run(
            [
                uv,
                "build",
                "--wheel",
                "--offline",
                "--no-progress",
                "--out-dir",
                str(wheel_directory),
                str(build_context),
            ],
            cwd=temporary,
        )
        wheels = tuple(wheel_directory.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one wheel, found {len(wheels)}")
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as archive:
            members = tuple(archive.namelist())
        forbidden = tuple(
            member
            for member in members
            if member in FORBIDDEN_ROOT_MEMBERS
            or member in FORBIDDEN_FLAT_IFC_MEMBERS
            or member.startswith(FORBIDDEN_PREFIXES)
            or member.startswith("CAD2BIM_mcp/direct_")
            or (
                member.startswith("CAD2BIM_mcp/")
                and member not in {"CAD2BIM_mcp/__init__.py", "CAD2BIM_mcp/reference_projection.py"}
                and not member.startswith("CAD2BIM_mcp/direct/")
                and not member.startswith("CAD2BIM_mcp/tool_docs/")
                and not member.startswith("CAD2BIM_mcp/runner/")
            )
        )
        if forbidden:
            raise RuntimeError("wheel contains removed product modules: " + ", ".join(forbidden))
        missing_direct = _missing_direct_members(members)
        if missing_direct:
            raise RuntimeError(
                "wheel is missing the current Direct structure: " + ", ".join(missing_direct)
            )
        missing_geometry_backend = _missing_geometry_backend_members(members)
        if missing_geometry_backend:
            raise RuntimeError(
                "wheel is missing the current geometry backend/runtime structure: "
                + ", ".join(missing_geometry_backend)
            )
        missing_ifc_query_backend = _missing_ifc_query_backend_members(members)
        if missing_ifc_query_backend:
            raise RuntimeError(
                "wheel is missing the current IFC query backend/runtime structure: "
                + ", ".join(missing_ifc_query_backend)
            )
        missing_ifc_revision_runtime_backend = _missing_ifc_revision_runtime_backend_members(
            members
        )
        if missing_ifc_revision_runtime_backend:
            raise RuntimeError(
                "wheel is missing the current IFC revision runtime structure: "
                + ", ".join(missing_ifc_revision_runtime_backend)
            )
        missing_data_query_attempt_backend = _missing_data_query_attempt_backend_members(members)
        if missing_data_query_attempt_backend:
            raise RuntimeError(
                "wheel is missing the current Data Query Attempt backend structure: "
                + ", ".join(missing_data_query_attempt_backend)
            )
        missing_data_transform_backend = _missing_data_transform_backend_members(members)
        if missing_data_transform_backend:
            raise RuntimeError(
                "wheel is missing the current Data Transform backend structure: "
                + ", ".join(missing_data_transform_backend)
            )
        missing_runner_domain = _missing_runner_domain_members(members)
        if missing_runner_domain:
            raise RuntimeError(
                "wheel is missing the current Runner domain structure: "
                + ", ".join(missing_runner_domain)
            )
        missing_product_family_backend = _missing_product_family_backend_members(members)
        if missing_product_family_backend:
            raise RuntimeError(
                "wheel is missing the current ProductFamily catalog structure: "
                + ", ".join(missing_product_family_backend)
            )
        if any(member.startswith("tests/") for member in members):
            raise RuntimeError("wheel unexpectedly contains repository tests")

        install_directory.mkdir()
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(install_directory)
        probe = "\n".join(
            (
                "import asyncio, importlib, importlib.metadata, json, sys",
                "from pathlib import Path",
                f"sys.path = [item for item in sys.path if Path(item).resolve() != Path({str(repository_root / 'src')!r}).resolve()]",
                "backend = importlib.import_module('CAD2BIM_backend')",
                f"assert tuple(Path(item).resolve() for item in backend.__path__) == (Path({str(install_directory / 'CAD2BIM_backend')!r}).resolve(),)",
                "from CAD2BIM_backend.application.product_family_authoring_service import ProductFamilyAuthoringService",
                "from CAD2BIM_backend.bootstrap import open_backend_runtime",
                "from CAD2BIM_backend.domain.product_family import PRODUCT_FAMILY_PROGRAM_ENTRY",
                "from CAD2BIM_backend.domain.product_family_authoring import FamilyDraftInlineCreateCommand",
                "from CAD2BIM_backend.domain.product_family_authoring_contracts import ProductFamilyAuthoringVersionDraft",
                "from CAD2BIM_backend.domain.geometry_runtime import GeometryBuildFixedRequest, GeometryValidateFunctionRequest, GeometryRenderRequest",
                "from CAD2BIM_backend.domain.product_realization import ProductRealizationRequest",
                "from CAD2BIM_backend.infrastructure.geometry_runtime.adapter import OcpCadQueryGeometryRuntime",
                "from CAD2BIM_backend.infrastructure.product_realization_runtime import OcpCadQueryProductRealizationRuntime",
                "from CAD2BIM_backend.infrastructure.source_text import pack_source_bundle",
                "from CAD2BIM_backend.ports.product_family_authoring import ProductFamilyAuthoringRepository",
                "from CAD2BIM_backend.ports.product_realization_runtime import ProductRealizationRuntimePort",
                "dist = importlib.metadata.distribution('cad2bim')",
                "scripts = {e.name: e.value for e in dist.entry_points if e.group == 'console_scripts'}",
                f"expected = {REQUIRED_ENTRYPOINTS!r}",
                "assert scripts == expected, scripts",
                "for value in expected.values(): importlib.import_module(value.partition(':')[0])",
                "assert PRODUCT_FAMILY_PROGRAM_ENTRY == 'main.py:realize'",
                "assert all((open_backend_runtime, ProductFamilyAuthoringService, FamilyDraftInlineCreateCommand, ProductFamilyAuthoringVersionDraft, ProductFamilyAuthoringRepository, ProductRealizationRequest, OcpCadQueryProductRealizationRuntime, ProductRealizationRuntimePort))",
                "async def probe_geometry_runtime():",
                "    source = pack_source_bundle({'main.py': b\"import cadquery as cq\\n\\ndef build(ctx):\\n    return cq.Workplane('XY').box(ctx.param('length'), 2.0, 3.0)\\n\", 'interfaces.py': b\"def evaluate(ctx):\\n    return None\\n\"})",
                "    runtime = OcpCadQueryGeometryRuntime(capacity_root=Path.cwd() / 'probe-capacity')",
                "    try:",
                "        fixed = await runtime.build_fixed(GeometryBuildFixedRequest(source_bundle=source, build_parameters={'length': 1.0}))",
                "        function = await runtime.validate_function(GeometryValidateFunctionRequest(source_bundle=source, parameter_schema={'type': 'object', 'properties': {'length': {'type': 'number', 'minimum': 0.1}}, 'required': ['length'], 'additionalProperties': False}, sample_parameters=({'length': 1.0}, {'length': 4.0})))",
                "        assert fixed.geometry.summary.volume_m3 == 6.0",
                "        assert function.sample_executions[1].geometry_summary.volume_m3 == 24.0",
                "        assert await runtime.reopen(fixed.geometry) == fixed.geometry.summary",
                "        preview = await runtime.render(GeometryRenderRequest(geometry=fixed.geometry))",
                "        assert preview.payload.startswith(b'\\x89PNG\\r\\n\\x1a\\n')",
                "    finally:",
                "        await runtime.close()",
                "asyncio.run(probe_geometry_runtime())",
                "print(json.dumps({'entrypoints': sorted(expected), 'geometry_worker': 'passed', 'product_family_authoring_abi': 'passed', 'product_realization_abi': 'passed', 'status': 'passed'}))",
            )
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(install_directory)
        _run([sys.executable, "-c", probe], cwd=temporary, env=environment)
        return {
            "status": "passed",
            "wheel_name": wheel.name,
            "member_count": len(members),
            "entrypoints": sorted(REQUIRED_ENTRYPOINTS),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    report = verify_wheel()
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
