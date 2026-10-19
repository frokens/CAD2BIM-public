"""Shared schema-selected reference projection for Direct and Runner providers.

Only declared reference fields are traversed. Arbitrary data, source and pagination
cursors remain owned by their existing contracts.
"""

import json

from CAD2BIM_backend.application.reference_projection import project_reference_fields


REFERENCE_TOOL_NAMES = frozenset({
    "ifc_geometry_resource_list", "ifc_geometry_resource_info", "ifc_geometry_resource_write",
    "ifc_geometry_resource_source_read", "ifc_geometry_preview", "ifc_geometry_query_python",
    "ifc_product_family_list", "ifc_product_family_info", "ifc_product_family_write",
    "ifc_authoring_publish", "ifc_product_realization_preview", "code_list", "code_read",
    "operation_list", "operation_get", "ifc_products_apply", "ifc_products_read", "ifc_products_edit",
})
from CAD2BIM_backend.application.reference_projection import reference_schema_projection


def result_operation_alias(result):
    """Retain a returned write's original recovery identity if output projection fails."""
    payload = result.structured_content
    if not isinstance(payload, dict):
        for block in result.content:
            if block.type == "text":
                try:
                    payload = json.loads(block.text)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    break
    if isinstance(payload, dict):
        operation = payload.get("operation", payload)
        if isinstance(operation, dict) and isinstance(operation.get("operation_alias"), str):
            return operation["operation_alias"]
    return None
