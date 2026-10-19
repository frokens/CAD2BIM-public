from __future__ import annotations

import ast
import importlib
from pathlib import Path
from unittest.mock import Mock

from CAD2BIM_mcp.direct.geometry_preview_contracts import (
    IfcPreviewDisplayQualityWarningResult,
)
from CAD2BIM_mcp.direct.server import create_direct_mcp
from CAD2BIM_mcp.tool_docs.preview import (
    PREVIEW_DISPLAY_QUALITY_WARNING_DESCRIPTION,
)


_TOOL_DOC_MODULES = (
    "cad",
    "catalog",
    "code",
    "common",
    "data_query",
    "data_transform",
    "ifc_geometry",
    "ifc_model",
    "ifc_official_docs",
    "ifc_product_family",
    "ifc_products",
    "ifc_query",
    "ifc_spatial",
    "operation",
    "preview",
    "session",
    "source",
)


def _tool_doc_description_values() -> set[str]:
    values: set[str] = set()
    for module_name in _TOOL_DOC_MODULES:
        module = importlib.import_module(f"CAD2BIM_mcp.tool_docs.{module_name}")
        values.update(
            value
            for name, value in vars(module).items()
            if name.endswith("_DESCRIPTION") and isinstance(value, str)
        )
    return values


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _root_name(value: ast.expr) -> str | None:
    while isinstance(value, ast.Attribute):
        value = value.value
    return value.id if isinstance(value, ast.Name) else None


def test_mcp_static_provider_help_is_owned_by_tool_docs_modules() -> None:
    package_root = Path(__file__).resolve().parents[3] / "src" / "CAD2BIM_mcp"
    direct_root = package_root / "direct"
    tool_docs_root = package_root / "tool_docs"

    assert {path.stem for path in tool_docs_root.glob("*.py")} == {
        "__init__",
        *_TOOL_DOC_MODULES,
    }

    for path in direct_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_aliases: set[str] = set()
        imported_descriptions: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if node.module == "CAD2BIM_mcp.tool_docs":
                module_aliases.update(alias.asname or alias.name for alias in node.names)
            elif node.module.startswith("CAD2BIM_mcp.tool_docs."):
                imported_descriptions.update(alias.asname or alias.name for alias in node.names)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = _call_name(node)
            is_field = call_name == "Field"
            is_provider_surface = call_name in {
                "tool",
                "resource",
                "from_function",
                "register_provider_tool",
                "register_provider_document",
                "_register",
                "ResourceLink",
            }
            if not (is_field or is_provider_surface):
                continue
            description = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "description"),
                None,
            )
            if description is None:
                continue
            if (
                path.name == "tool_support.py"
                and call_name in {"from_function", "resource"}
                and isinstance(description, ast.Name)
                and description.id == "description"
            ):
                continue
            if isinstance(description, ast.Name):
                assert description.id in imported_descriptions, (path, description.lineno)
            else:
                assert _root_name(description) in module_aliases, (path, description.lineno)

        if path.name.endswith(("contracts.py", "inputs.py")):
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    assert ast.get_docstring(node, clean=False) is None, (path, node.lineno)


def test_preview_display_warning_schema_help_is_owned_by_tool_docs() -> None:
    assert (
        IfcPreviewDisplayQualityWarningResult.model_json_schema()["description"]
        == PREVIEW_DISPLAY_QUALITY_WARNING_DESCRIPTION
    )


async def test_registered_tool_and_resource_descriptions_come_from_tool_docs() -> None:
    descriptions = _tool_doc_description_values()
    server = create_direct_mcp(Mock())

    tools = await server.list_tools()
    resource_templates = await server.list_resource_templates()
    resources = await server.list_resources()

    assert len(tools) == 42
    assert "cad_import_source" not in {tool.name for tool in tools}
    assert sum(tool.name.startswith("ifc_") for tool in tools) == 24
    assert len(resource_templates) == 0
    from CAD2BIM_mcp.tool_docs.common import REFERENCE_USAGE_NAVIGATION, MULTIMODAL_RESULT_USAGE_DESCRIPTION, COMMON_TYPES_NAVIGATION
    assert all(
        tool.description.removesuffix(MULTIMODAL_RESULT_USAGE_DESCRIPTION.format(tool_name=tool.name))
        .removesuffix(REFERENCE_USAGE_NAVIGATION) in descriptions
        for tool in tools
    )
    assert {str(resource.uri): resource.name for resource in resources} == {
        "cad2bim://contracts/common/types": "common_types",
        "cad2bim://contracts/common/usage": "common_usage",
        "cad2bim://contracts/common/authoring": "common_authoring",
        "cad2bim://contracts/common/preview": "common_preview",
        "cad2bim://contracts/common/preview_types": "common_preview_types",
        "cad2bim://contracts/ifc_geometry_preview/tutorial": "ifc_geometry_preview_tutorial",
        "cad2bim://contracts/ifc_product_realization_preview/tutorial": "ifc_product_realization_preview_tutorial",
        "cad2bim://contracts/data_query_python/program_api": "data_query_python_program_api",
        "cad2bim://contracts/data_derive/duckdb_sql": "data_derive_duckdb_sql",
        "cad2bim://contracts/data_derive/python_arrow": "data_derive_python_arrow",
        "cad2bim://contracts/data_derive/repair": "data_derive_repair",
        "cad2bim://contracts/ifc_geometry_preview/result": "ifc_geometry_preview_result",
        "cad2bim://contracts/ifc_geometry_preview/cases_json": "ifc_geometry_preview_cases_json",
        "cad2bim://contracts/ifc_geometry_preview/render_json": "ifc_geometry_preview_render_json",
        "cad2bim://contracts/ifc_geometry_query_python/program_api": "ifc_geometry_query_python_program_api",
        "cad2bim://contracts/ifc_geometry_resource_write/program_api": "ifc_geometry_resource_write_program_api",
        "cad2bim://contracts/ifc_product_family_write/definition_json": "ifc_product_family_write_definition_json",
        "cad2bim://contracts/ifc_product_family_write/program_api": "ifc_product_family_write_program_api",
        "cad2bim://contracts/ifc_product_family_write/source_changes_json": "ifc_product_family_write_source_changes_json",
        "cad2bim://contracts/ifc_product_family_write/tutorial": "ifc_product_family_write_tutorial",
        "cad2bim://contracts/ifc_product_realization_preview/result": "ifc_product_realization_preview_result",
        "cad2bim://contracts/ifc_product_realization_preview/cases_json": "ifc_product_realization_preview_cases_json",
        "cad2bim://contracts/ifc_product_realization_preview/render_json": "ifc_product_realization_preview_render_json",
        "cad2bim://contracts/ifc_product_realization_preview/self_geometry": "ifc_product_realization_preview_self_geometry",
        "cad2bim://contracts/ifc_product_realization_preview/standard_model": "ifc_product_realization_preview_standard_model",
        "cad2bim://contracts/ifc_query_python/program_api": "ifc_query_python_program_api",
        "cad2bim://contracts/ifc_products_apply/input": "ifc_products_apply_input",
        "cad2bim://contracts/ifc_products_edit/input": "ifc_products_edit_input",
        "cad2bim://contracts/ifc_products_read/usage": "ifc_products_read_usage",
    }
    assert all(resource.description.removesuffix(COMMON_TYPES_NAVIGATION) in descriptions
               for resource in [*resource_templates, *resources])
