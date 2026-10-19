"""HTTP client for the Backend's protected Runner State and domain entries."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import httpx

from CAD2BIM_backend.domain.runtime_execution import (
    DATA_PYTHON_EXECUTION,
    DATA_SQL_EXECUTION,
    GEOMETRY_AUTHORING_EXECUTION,
    GEOMETRY_PREVIEW_EXECUTION,
    GEOMETRY_QUERY_EXECUTION,
    IFC_QUERY_EXECUTION,
    IFC_REVISION_EXECUTION,
    IFC_REVISION_INVOCATION_EXECUTION,
    PRODUCT_AUTHORING_EXECUTION,
    PRODUCT_PREVIEW_EXECUTION,
    RUNTIME_RESPONSE_MARGIN_SECONDS,
)


_DATA_TOOL_NAMES = frozenset({
    "data_package_list", "data_describe_package", "data_get_package_description",
    "data_query_rows", "data_query_sql", "data_query_python", "data_read_json",
    "data_derive", "code_list", "code_read", "operation_list", "operation_get",
})
_AUTHORING_TOOL_NAMES = frozenset({
    "ifc_geometry_resource_list", "ifc_geometry_resource_info",
    "ifc_geometry_resource_source_read", "ifc_geometry_resource_write",
    "ifc_geometry_preview", "ifc_geometry_query_python", "ifc_product_family_list",
    "ifc_product_family_info", "ifc_product_family_write",
    "ifc_product_realization_preview", "ifc_authoring_publish",
})
_IFC_ENDPOINTS = frozenset({
    "/internal/domain/ifc/model/initialize",
    "/internal/domain/ifc/model/info",
    "/internal/domain/ifc/model/geometry-basis/configure",
    "/internal/domain/ifc/model/representation-contexts/list",
    "/internal/domain/ifc/project/update",
    "/internal/domain/ifc/spatial-structure/list",
    "/internal/domain/ifc/spatial-structure/apply",
    "/internal/domain/ifc/products/apply",
    "/internal/domain/ifc/products/edit",
    "/internal/domain/ifc/products/read",
    "/internal/domain/ifc/query/python",
})


class BackendStateReadError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"backend State read failed ({status_code})")
        self.status_code = status_code


class BackendDomainError(RuntimeError):
    def __init__(self, status_code: int, detail: object) -> None:
        code = detail.get("code") if isinstance(detail, dict) else None
        super().__init__(f"backend domain call failed ({status_code}, {code or 'unknown'})")
        self.status_code = status_code
        self.detail = detail


class RunnerStateBackendClient:
    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        if not base_url:
            raise ValueError("backend State HTTP URL is required")
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(
        self, path: str, *, access_token: str, read_binding: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        # Headers belong to this invocation; never mutate a shared client's identity.
        response = await self._client.post(
            path,
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-CAD2BIM-State-Binding": read_binding,
            },
        )
        if response.status_code >= 400:
            raise BackendStateReadError(response.status_code)
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("backend State response is not an object")
        return payload

    async def list_resources(
        self, *, access_token: str, read_binding: str
    ) -> tuple[dict[str, Any], ...]:
        payload = await self._post(
            "/internal/state/resources/list",
            access_token=access_token,
            read_binding=read_binding,
            body={},
        )
        resources = payload.get("resources")
        if not isinstance(resources, list) or any(not isinstance(item, dict) for item in resources):
            raise RuntimeError("backend State directory is malformed")
        return tuple(resources)

    async def authorize(self, *, access_token: str, read_binding: str) -> None:
        payload = await self._post(
            "/internal/state/authorize",
            access_token=access_token,
            read_binding=read_binding,
            body={},
        )
        if payload.get("authorized") is not True:
            raise RuntimeError("backend State authorization response is malformed")

    async def read_resource(
        self, *, access_token: str, read_binding: str, uri: str
    ) -> tuple[dict[str, Any], str]:
        payload = await self._post(
            "/internal/state/resources/read",
            access_token=access_token,
            read_binding=read_binding,
            body={"uri": uri},
        )
        resource = payload.get("resource")
        text = payload.get("text")
        if not isinstance(resource, dict) or not isinstance(text, str):
            raise RuntimeError("backend State Resource is malformed")
        return resource, text

    async def _post_domain(
        self, path: str, *, access_token: str, body: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        response = await self._client.post(
            path,
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout if timeout is not None else self._client.timeout,
        )
        try:
            payload = response.json()
        except ValueError:
            raise BackendDomainError(response.status_code, None) from None
        if response.status_code >= 400:
            raise BackendDomainError(
                response.status_code,
                payload.get("detail") if isinstance(payload, dict) else None,
            )
        if not isinstance(payload, dict):
            raise BackendDomainError(502, None)
        return payload

    async def authorize_domain(self, *, access_token: str) -> None:
        payload = await self._post_domain(
            "/internal/domain/authorize", access_token=access_token, body={}
        )
        if payload.get("authorized") is not True:
            raise RuntimeError("backend domain authorization response is malformed")

    async def translate_references(
        self, *, access_token: str, direction: Literal["expand", "shorten"],
        items: Sequence[dict[str, Any]],
    ) -> tuple[str, ...]:
        payload = await self._post_domain(
            "/internal/domain/references/translate", access_token=access_token,
            body={"direction": direction, "items": list(items)},
        )
        values = payload.get("values")
        if (not isinstance(values, list) or len(values) != len(items)
                or any(not isinstance(value, str) for value in values)):
            raise BackendDomainError(502, None)
        return tuple(values)

    async def call_data_tool(
        self, *, access_token: str, tool_name: str, body: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if tool_name not in _DATA_TOOL_NAMES:
            raise ValueError("unknown Runner Data tool")
        if timeout is None:
            policy = (
                DATA_SQL_EXECUTION if tool_name == "data_query_sql" else
                DATA_PYTHON_EXECUTION if tool_name in {"data_query_python", "data_derive"} else
                None
            )
            if policy is not None:
                timeout = policy.total_seconds + RUNTIME_RESPONSE_MARGIN_SECONDS
        return await self._post_domain(
            f"/internal/domain/data/{tool_name}",
            access_token=access_token, body=body, timeout=timeout,
        )

    async def call_ifc_domain(
        self, *, access_token: str, endpoint: str, body: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if endpoint not in _IFC_ENDPOINTS:
            raise ValueError("unknown Runner IFC endpoint")
        if timeout is None:
            policy = (
                IFC_QUERY_EXECUTION if endpoint.endswith("/query/python") else
                IFC_REVISION_EXECUTION if endpoint.endswith((
                    "/model/info", "/model/representation-contexts/list", "/spatial-structure/list", "/products/read",
                )) else
                IFC_REVISION_INVOCATION_EXECUTION if endpoint.endswith((
                    "/model/initialize", "/model/geometry-basis/configure",
                    "/project/update", "/spatial-structure/apply", "/products/apply", "/products/edit",
                )) else None
            )
            if policy is not None:
                timeout = policy.total_seconds + RUNTIME_RESPONSE_MARGIN_SECONDS
        return await self._post_domain(
            endpoint, access_token=access_token, body=body, timeout=timeout,
        )

    async def call_authoring(
        self, *, access_token: str, tool_name: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        if tool_name not in _AUTHORING_TOOL_NAMES:
            raise ValueError("unknown Runner Authoring tool")
        policy = {
            "ifc_geometry_resource_write": GEOMETRY_AUTHORING_EXECUTION,
            "ifc_geometry_preview": GEOMETRY_PREVIEW_EXECUTION,
            "ifc_geometry_query_python": GEOMETRY_QUERY_EXECUTION,
            "ifc_product_family_write": PRODUCT_AUTHORING_EXECUTION,
            "ifc_product_realization_preview": PRODUCT_PREVIEW_EXECUTION,
            "ifc_authoring_publish": (GEOMETRY_AUTHORING_EXECUTION
                if body.get("root_kind") == "geometry" else PRODUCT_AUTHORING_EXECUTION),
        }.get(tool_name)
        return await self._post_domain(
            f"/internal/domain/authoring/{tool_name}",
            access_token=access_token, body=body,
            timeout=(policy.total_seconds + RUNTIME_RESPONSE_MARGIN_SECONDS)
            if policy is not None else None,
        )

__all__ = ["BackendDomainError", "BackendStateReadError", "RunnerStateBackendClient"]
