from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.application import data_runtime_service as service_module
from CAD2BIM_backend.application.data_query_service import DataQueryService
from CAD2BIM_backend.domain import data_runtime as domain_module
from CAD2BIM_backend.domain import invocation as invocation_module
from CAD2BIM_backend.domain.control_plane import ConflictError, ExecutionMode
from CAD2BIM_backend.domain.data_runtime import ArtifactVersionRef
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import (
    InMemoryIdentityDirectory,
)
from CAD2BIM_backend.infrastructure.postgres.repository_data_runtime import (
    PostgresDataRuntimeRepositoryMixin,
)


def _identity(*, task_id: str | None = "task_shared") -> TenantExecutionContext:
    return TenantExecutionContext(
        tenant_id="tenant_shared",
        principal_id="principal_shared",
        task_id=task_id,
        actor_kind=TenantActorKind.USER,
        command_id="command_shared_boundary",
    )


def _parameters(callable_object) -> tuple[str, ...]:
    return tuple(inspect.signature(callable_object).parameters)


def test_invocation_scope_requires_exact_task_run_owner_mode_and_frontier() -> None:
    InvocationScope = invocation_module.InvocationScope

    direct = InvocationScope(
        identity=_identity(),
        owner_id="owner_shared",
        run_id="run_shared",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=None,
    )
    graph = direct.model_copy(update={"execution_mode": ExecutionMode.GRAPH, "catalog_frontier": 7})

    assert direct.owner_id == "owner_shared"
    assert direct.identity.task_id == "task_shared"
    assert direct.run_id == "run_shared"
    assert direct.execution_mode is ExecutionMode.DIRECT_MCP
    assert direct.catalog_frontier is None
    assert graph.execution_mode is ExecutionMode.GRAPH
    assert graph.catalog_frontier == 7
    assert set(InvocationScope.model_fields) == {
        "identity",
        "owner_id",
        "run_id",
        "execution_mode",
        "catalog_frontier",
    }

    for missing in (
        "owner_id",
        "run_id",
        "execution_mode",
        "catalog_frontier",
    ):
        values = {
            "identity": _identity(),
            "owner_id": "owner_shared",
            "run_id": "run_shared",
            "execution_mode": ExecutionMode.DIRECT_MCP,
            "catalog_frontier": None,
        }
        values.pop(missing)
        with pytest.raises(ValidationError):
            InvocationScope(**values)

    with pytest.raises(ValidationError, match="task_id"):
        InvocationScope(
            identity=_identity(task_id=None),
            owner_id="owner_shared",
            run_id="run_shared",
            execution_mode=ExecutionMode.DIRECT_MCP,
            catalog_frontier=None,
        )


def test_data_runtime_service_is_entry_neutral_and_every_business_method_takes_scope() -> None:
    DataRuntimeService = service_module.DataRuntimeService

    assert _parameters(DataRuntimeService) == ("repository", "artifacts")
    assert _parameters(DataRuntimeService.import_cad_source)[:3] == (
        "self",
        "scope",
        "source",
    )
    assert _parameters(DataRuntimeService.get_catalog) == ("self", "scope")
    assert _parameters(DataRuntimeService.get_package_description) == (
        "self",
        "scope",
        "package_alias",
    )
    assert not hasattr(DataRuntimeService, "open_direct_session")


def test_data_query_service_is_entry_neutral_and_every_business_method_takes_scope() -> None:
    assert _parameters(DataQueryService) == (
        "repository",
        "artifacts",
        "cursor_codec",
        "policy",
        "clock",
    )
    for method_name in (
        "describe_package",
        "query_rows",
        "query_entities",
        "get_entity",
        "query_xdata",
    ):
        parameters = _parameters(getattr(DataQueryService, method_name))
        assert parameters[:2] == ("self", "scope")
        assert not ({"owner_id", "tenant_id", "principal_id", "task_id", "run_id"} & set(parameters))

    assert not hasattr(DataQueryService, "open_direct_session")


def test_repository_shared_methods_take_scope_instead_of_loose_identity_fields() -> None:
    repository_type = type(InMemoryControlPlaneRepository())
    expected_prefixes = {
        "publish_import_result": ("self", "scope", "package", "members", "operation"),
        "record_failed_operation": ("self", "scope", "operation"),
        "get_package": ("self", "scope", "package_id"),
        "get_operation": ("self", "scope", "operation_id"),
            "get_operation_replay": (
                "self",
                "scope",
                "tool_names",
                "request_fingerprint",
            ),
        "list_catalog": ("self", "scope", "resource_kind"),
    }

    for method_name, expected in expected_prefixes.items():
        actual = _parameters(getattr(repository_type, method_name))
        assert actual[: len(expected)] == expected
        assert not ({"owner_id", "tenant_id", "task_id", "run_id"} & set(actual))


def test_validated_source_requires_exact_bytes_xor_exact_artifact_version() -> None:
    ValidatedSource = getattr(domain_module, "ValidatedSource")
    artifact = ArtifactVersionRef(
        artifact_id="artifact_source",
        version_id="version_source",
        sha256="a" * 64,
    )
    common = {
        "filename": "plant.dxf",
        "media_type": "application/dxf",
        "source_identity": "approved-root-1:drawings/plant.dxf",
        "adapter_metadata": {"root_relative_path": "drawings/plant.dxf"},
    }

    byte_source = ValidatedSource(**common, content=b"0\nEOF\n")
    artifact_source = ValidatedSource(**common, artifact_version=artifact)

    assert byte_source.content == b"0\nEOF\n"
    assert byte_source.artifact_version is None
    assert artifact_source.content is None
    assert artifact_source.artifact_version == artifact
    assert "root_relative_path" not in ValidatedSource.model_fields
    with pytest.raises(ValidationError):
        ValidatedSource(**common)
    with pytest.raises(ValidationError):
        ValidatedSource(**common, content=b"0\nEOF\n", artifact_version=artifact)


async def test_direct_session_service_and_resolver_return_only_direct_scope() -> None:
    DirectSessionService = getattr(service_module, "DirectSessionService")
    DirectScopeResolver = getattr(service_module, "DirectScopeResolver")
    repository = InMemoryControlPlaneRepository()
    identities = InMemoryIdentityDirectory()
    sessions = DirectSessionService(repository, identities, "configured-local-user")
    resolver = DirectScopeResolver(repository, identities, "configured-local-user")

    opened = await sessions.open_direct_session("Shared scope")
    scope = await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id="command_resolve_direct",
    )

    assert scope.identity.tenant_id == opened.binding.tenant_id
    assert scope.identity.principal_id == opened.binding.principal_id
    assert scope.identity.task_id == opened.binding.task_id
    assert scope.owner_id == opened.binding.owner_id
    assert scope.run_id == opened.binding.run_id
    assert scope.execution_mode is ExecutionMode.DIRECT_MCP
    assert scope.catalog_frontier is None


async def test_postgres_scope_validation_joins_task_principal_binding() -> None:
    class EmptySession:
        statement = None

        async def scalar(self, statement):
            self.statement = statement
            return None

    session = EmptySession()
    scope = invocation_module.InvocationScope(
        identity=_identity(),
        owner_id="owner_shared",
        run_id="run_shared",
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=None,
    )

    with pytest.raises(ConflictError, match="authoritative Run"):
        await PostgresDataRuntimeRepositoryMixin._require_data_run(
            session,
            scope,
            lock=False,
        )

    assert session.statement is not None
    sql = str(session.statement)
    assert "task_tenant_bindings" in sql
    assert "task_tenant_bindings.principal_id" in sql


@pytest.mark.parametrize("mismatch", ("tenant", "principal", "task", "run", "mode"))
async def test_direct_scope_resolver_rejects_binding_or_authoritative_run_mismatch(
    mismatch: str,
) -> None:
    DirectSessionService = getattr(service_module, "DirectSessionService")
    DirectScopeResolver = getattr(service_module, "DirectScopeResolver")
    repository = InMemoryControlPlaneRepository()
    identities = InMemoryIdentityDirectory()
    sessions = DirectSessionService(repository, identities, "configured-local-user")
    resolver = DirectScopeResolver(repository, identities, "configured-local-user")
    opened = await sessions.open_direct_session("Rejected scope")
    binding = opened.binding

    if mismatch == "mode":
        repository._state.runs[binding.run_id] = repository._state.runs[binding.run_id].model_copy(
            update={"execution_mode": ExecutionMode.GRAPH}
        )
    else:
        changes = {
            "tenant": {"tenant_id": "tenant_mismatch"},
            "principal": {"principal_id": "principal_mismatch"},
            "task": {"task_id": "task_mismatch"},
            "run": {"run_id": "run_mismatch"},
        }[mismatch]
        key = next(
            key
            for key, candidate in repository._state.direct_sessions.items()
            if candidate.session_id == binding.session_id
        )
        repository._state.direct_sessions[key] = binding.model_copy(update=changes)

    with pytest.raises(
        service_module.DataRuntimeApplicationError,
        match="(?i)direct|session|scope|binding|mode",
    ):
        await resolver.resolve(
            binding.direct_session_alias,
            command_id="command_reject_mismatch",
        )
