from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.exc import OperationalError
from sqlalchemy.dialects import postgresql

from CAD2BIM_backend.domain.control_plane import ConflictError
from CAD2BIM_backend.infrastructure.postgres.models import Base
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.repository_product_family_authoring import (
    PostgresProductFamilyAuthoringRepositoryMixin,
)
from CAD2BIM_backend.ports.product_family_authoring import (
    ProductFamilyAuthoringCommitOutcomeUnknownError,
    ProductFamilyAuthoringRepository,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner", "entry"), [("geometry", "find"), ("product_family", "find"), ("product_family", "require"), ("product_family", "replay")])
async def test_authoring_operation_entry_queries_require_current_contract(owner, entry):
    from CAD2BIM_backend.infrastructure.postgres.repository_geometry_authoring import (
        PostgresGeometryAuthoringRepositoryMixin,
    )

    statements = []
    session = SimpleNamespace()

    async def scalar(statement):
        statements.append(statement)
        return None

    session.scalar = scalar
    repository_type = (PostgresGeometryAuthoringRepositoryMixin if owner == "geometry"
                       else PostgresProductFamilyAuthoringRepositoryMixin)
    repository = repository_type(lambda: session)
    scope = SimpleNamespace(owner_id="owner", run_id="run",
                            identity=SimpleNamespace(tenant_id="tenant", task_id="task"))
    operation = SimpleNamespace(owner_id="owner", tenant_id="tenant", task_id="task", run_id="run",
        idempotency_key="key", tool_name="tool", request_fingerprint="a" * 64,
        repair_parent_operation_id="parent", target_geometry_resource_id="geometry",
        target_product_family_id="family", base_product_family_version_id=None)
    if entry == "find":
        assert await getattr(repository, f"_find_{owner}_operation_model")(session, scope, "op") is None
    elif entry == "require":
        with pytest.raises(ConflictError):
            await getattr(repository, f"_require_{owner}_operation_model")(session, scope, "op", lock=False)
    elif entry == "replay":
        assert await getattr(repository, f"_find_{owner}_replay")(session, operation) is None
    else:
        with pytest.raises(ConflictError):
            await getattr(repository, f"_require_{owner}_repair_parent")(session, operation)
    assert len(statements) == (2 if entry == "replay" else 1)
    for statement in statements:
        compiled = statement.compile(dialect=postgresql.dialect())
        assert "authoring_draft_write" in compiled.params.values()
        assert "system_copy" in compiled.params.values()
        assert "run" in compiled.params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["exact_source", "source_list"])
async def test_family_program_sources_use_exact_asset_and_pagination_without_format_generations(monkeypatch, entry):
    from unittest.mock import AsyncMock
    import CAD2BIM_backend.infrastructure.postgres.repository_product_family as module

    statements = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(all=lambda: [], one_or_none=lambda: None)

    repository = module.PostgresProductFamilyRepositoryMixin(lambda: Session())
    monkeypatch.setattr(repository, "_require_product_family_context", lambda _context: None)
    monkeypatch.setattr(module, "set_tenant_execution_context", AsyncMock())
    if entry == "exact_source":
        assert await repository.get_product_family_program_source(object(), family_version_id="exact") is None
    else:
        page = await repository.list_product_family_program_sources(object(), before_created_at=None,
            before_family_version_id=None, limit=3)
        assert page.sources == () and page.rejections == () and page.complete
    assert len(statements) == 1
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "realization_source_bundle" in compiled.params.values()
    assert "abi_profile" not in sql
    assert "standard_model" not in compiled.params.values()
    if entry == "source_list":
        assert 4 in compiled.params.values()  # One lookahead row, not part of the scan.


def _render(direction: str) -> str:
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    if direction == "upgrade":
        command.upgrade(config, "20260904_0073:20260904_0074", sql=True)
    else:
        command.downgrade(config, "20260904_0074:20260904_0073", sql=True)
    return " ".join(output.getvalue().split())


def _catalog_models(bundle, *, invalid=False):
    family = SimpleNamespace(family_id=bundle.family.family_id,
        owner_scope=bundle.family.owner_scope.scope_kind.name,
        owner_tenant_id=bundle.family.owner_scope.tenant_id,
        owner_task_id=bundle.family.owner_scope.task_id, created_at=bundle.family.created_at)
    values = bundle.version.model_dump()
    values["family_contract_json"] = values.pop("family_contract")
    if invalid:
        values["family_contract_json"] = dict(values["family_contract_json"], retired_contract="private-input-not-for-diagnostic")
    return family, SimpleNamespace(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["family", "source"])
@pytest.mark.parametrize("all_rejected", [False, True])
async def test_family_discovery_filters_only_current_contract_errors_and_uses_scan_boundary(monkeypatch, entry, all_rejected):
    from unittest.mock import AsyncMock
    from datetime import timedelta
    import CAD2BIM_backend.infrastructure.postgres.repository_product_family as module
    from CAD2BIM_backend.domain.product_family import ProductFamilyListQuery
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyProgramSource
    from tests.unit.backend.test_product_family_catalog_service import _family_bundle

    newer = _family_bundle(version_no=2)
    older = _family_bundle()
    newer_family = newer.family.model_copy(update={"family_id": "family-new", "created_at": newer.family.created_at + timedelta(seconds=2)})
    newer = newer.model_copy(update={"family": newer_family, "version": newer.version.model_copy(update={"family_id": newer_family.family_id})})
    rows = (_catalog_models(newer, invalid=all_rejected), _catalog_models(older, invalid=True), _catalog_models(_family_bundle(version_no=3)))
    by_id = {version.family_version_id: (family, version) for family, version in rows}
    statements = []

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(all=lambda: rows)

    repository = module.PostgresProductFamilyRepositoryMixin(lambda: Session())
    monkeypatch.setattr(repository, "_require_product_family_context", lambda _context: None)
    context = object()
    bind = AsyncMock()
    monkeypatch.setattr(module, "set_tenant_execution_context", bind)

    async def load(_session, *, family_id, family_version_id):
        family, version = by_id[family_version_id]
        repository._product_family_version_record(version)  # Real current DTO validation.
        if entry == "family": return newer
        return ProductFamilyProgramSource(family=newer.family, family_version_id=newer.version.family_version_id,
            version_no=newer.version.version_no, created_at=newer.version.created_at,
            source_asset=next(asset for asset in newer.assets if asset.asset_role.value == "realization_source_bundle"))

    loader_name = "_load_product_family_bundle" if entry == "family" else "_load_product_family_program_source"
    monkeypatch.setattr(repository, loader_name, load)
    if entry == "family":
        page = await repository.list_product_families(context, query=ProductFamilyListQuery(page_size=2))
        assert len(page.entries) == (0 if all_rejected else 1)
        assert page.next_before_family_id == older.family.family_id
    else:
        page = await repository.list_product_family_program_sources(context,
            before_created_at=None, before_family_version_id=None, limit=2)
        assert len(page.sources) == (0 if all_rejected else 1)
        assert page.next_before_family_version_id == older.version.family_version_id
    assert not page.complete and len(page.rejections) == (2 if all_rejected else 1)
    assert page.rejections[-1].family_version_id == older.version.family_version_id
    issue = page.rejections[-1].issues[0]
    assert issue.field_path == "family_contract.retired_contract"
    assert "private-input" not in issue.reason
    bind.assert_awaited_once_with(bind.await_args.args[0], context)

    async def technical_failure(*_args, **_kwargs): raise OSError("database transport unavailable")
    monkeypatch.setattr(repository, loader_name, technical_failure)
    with pytest.raises(OSError, match="transport"):
        if entry == "family":
            await repository.list_product_families(context, query=ProductFamilyListQuery(page_size=2))
        else:
            await repository.list_product_family_program_sources(context,
                before_created_at=None, before_family_version_id=None, limit=2)


@pytest.mark.asyncio
async def test_scoped_family_metadata_does_not_decode_retired_contract_or_query_assets(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    import CAD2BIM_backend.infrastructure.postgres.repository_product_family as module
    from tests.unit.backend.test_product_family_catalog_service import _family_bundle
    bundle = _family_bundle()
    row = _catalog_models(bundle, invalid=True)
    statements = []

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(one_or_none=lambda: row)

    repository = module.PostgresProductFamilyRepositoryMixin(lambda: Session())
    monkeypatch.setattr(repository, "_require_product_family_context", lambda _context: None)
    bind = AsyncMock()
    monkeypatch.setattr(module, "set_tenant_execution_context", bind)
    decode = Mock(side_effect=AssertionError("metadata must not parse a Family contract"))
    monkeypatch.setattr(repository, "_product_family_version_record", decode)
    context = object()
    metadata = await repository.get_product_family_version_metadata(context,
        family_id=bundle.family.family_id, family_version_id=bundle.version.family_version_id)
    assert metadata.family_version_id == bundle.version.family_version_id
    decode.assert_not_called()
    assert len(statements) == 1
    compiled = statements[0].compile(dialect=postgresql.dialect())
    assert bundle.family.family_id in compiled.params.values() and bundle.version.family_version_id in compiled.params.values()
    assert "product_family_version_assets" not in str(compiled)
    bind.assert_awaited_once_with(bind.await_args.args[0], context)


def test_family_contract_diagnostics_do_not_expose_custom_validator_input(monkeypatch):
    from pydantic import ValidationError
    import CAD2BIM_backend.infrastructure.postgres.repository_product_family as module
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyStoredContractError
    from tests.unit.backend.test_product_family_catalog_service import _family_bundle
    _, version = _catalog_models(_family_bundle())
    failure = ValidationError.from_exception_data("ProductFamilyContract", [{
        "type": "value_error", "loc": ("standard_model", "public_parameters"),
        "input": {"private": "input-value-must-stay-private"},
        "ctx": {"error": ValueError("wrapped validator has input-value-must-stay-private")},
    }])
    def invalid(_value): raise failure
    monkeypatch.setattr(module.ProductFamilyContract, "model_validate", invalid)
    with pytest.raises(ProductFamilyStoredContractError) as caught:
        module.PostgresProductFamilyRepositoryMixin._product_family_version_record(version)
    issue = caught.value.issues[0]
    assert issue.field_path == "family_contract.standard_model.public_parameters"
    assert issue.reason == "Current Family contract value failed validation"
    assert "private" not in issue.model_dump_json()


def test_family_digest_corruption_is_not_a_structural_rejection():
    from pydantic import ValidationError
    from CAD2BIM_backend.infrastructure.postgres.repository_product_family import PostgresProductFamilyRepositoryMixin
    from tests.unit.backend.test_product_family_catalog_service import _family_bundle
    _, version = _catalog_models(_family_bundle())
    version.contract_digest = "0" * 64
    with pytest.raises(ValidationError, match="contract_digest"):
        PostgresProductFamilyRepositoryMixin._product_family_version_record(version)


@pytest.mark.asyncio
async def test_exact_parent_identity_does_not_depend_on_parseable_structure_kind(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    import CAD2BIM_backend.infrastructure.postgres.repository_product_family as module
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyStoredContractError
    from tests.unit.backend.test_product_family_catalog_service import _family_bundle
    bundle = _family_bundle()
    _family, version = _catalog_models(bundle)
    version.family_contract_json["product_structure_kind"] = "former_structure_not_current"
    statements = []

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def scalar(self, statement):
            statements.append(statement)
            return version.family_version_id

    repository = module.PostgresProductFamilyRepositoryMixin(lambda: Session())
    monkeypatch.setattr(repository, "_require_product_family_context", lambda _context: None)
    bind = AsyncMock()
    monkeypatch.setattr(module, "set_tenant_execution_context", bind)
    metadata = Mock(side_effect=AssertionError("parent identity must not read summary or structure kind"))
    monkeypatch.setattr(repository, "_product_family_version_metadata", metadata)
    context = object()
    identity = await repository.get_product_family_version_identity(context,
        family_id=bundle.family.family_id, family_version_id=version.family_version_id)
    assert identity == version.family_version_id
    metadata.assert_not_called()
    statement = statements[0]
    assert tuple(column.key for column in statement.selected_columns) == ("family_version_id",)
    compiled = statement.compile(dialect=postgresql.dialect())
    assert bundle.family.family_id in compiled.params.values()
    assert version.family_version_id in compiled.params.values()
    assert "JOIN product_families" in str(compiled)
    assert "family_contract_json" not in str(compiled) and "product_family_version_assets" not in str(compiled)
    bind.assert_awaited_once_with(bind.await_args.args[0], context)
    # Identity is not current-contract acceptance: the exact DTO still rejects this raw parent.
    with pytest.raises(ProductFamilyStoredContractError):
        repository._product_family_version_record(version)


def test_family_contract_union_tag_diagnostic_keeps_expected_tags_without_offending_input():
    from pydantic import ValidationError
    from CAD2BIM_backend.domain.product_family import ProductFamilyContract
    from CAD2BIM_backend.domain.product_family_catalog import ProductFamilyStoredContractError
    from CAD2BIM_backend.infrastructure.postgres.repository_product_family import PostgresProductFamilyRepositoryMixin
    from tests.unit.backend.test_product_family_catalog_service import _family_bundle
    _, version = _catalog_models(_family_bundle())
    private_tag = "offending_parameter_kind_unique_private_marker"
    version.family_contract_json["public_parameters"] = [{"parameter_key": "probe", "parameter_kind": private_tag}]
    with pytest.raises(ValidationError) as original:
        ProductFamilyContract.model_validate(version.family_contract_json)
    error = original.value.errors(include_input=False)[0]
    assert error["type"] == "union_tag_invalid" and private_tag in error["msg"]
    with pytest.raises(ProductFamilyStoredContractError) as caught:
        PostgresProductFamilyRepositoryMixin._product_family_version_record(version)
    issue = caught.value.issues[0]
    assert issue.field_path == "family_contract.public_parameters.0"
    assert issue.reason == "Expected one of: " + error["ctx"]["expected_tags"]
    assert private_tag not in issue.model_dump_json()


def _render_repair_fence(direction: str) -> str:
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:secret@example.invalid/cad2bim",
    )
    output = StringIO()
    config.output_buffer = output
    if direction == "upgrade":
        command.upgrade(config, "20260905_0075:20260905_0076", sql=True)
    else:
        command.downgrade(config, "20260905_0076:20260905_0075", sql=True)
    return " ".join(output.getvalue().split())


def test_product_family_authoring_orm_extends_the_single_operation_table() -> None:
    operations = Base.metadata.tables["critical_operations"]
    family_versions = Base.metadata.tables["product_family_versions"]
    assert "abi_profile" not in family_versions.c
    assert operations.c.target_product_family_id.nullable is True
    assert operations.c.base_product_family_version_id.nullable is True
    assert operations.c.result_product_family_version_id.nullable is True

    foreign_keys = {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in operations.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        ("target_product_family_id", "base_product_family_version_id"),
        (
            "product_family_versions.family_id",
            "product_family_versions.family_version_id",
        ),
    ) in foreign_keys
    assert (
        ("target_product_family_id", "result_product_family_version_id"),
        (
            "product_family_versions.family_id",
            "product_family_versions.family_version_id",
        ),
    ) in foreign_keys
    assert not any(
        tuple(constraint.column_keys) == ("target_product_family_id",)
        for constraint in operations.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )
    assert not any(
        tuple(constraint.columns.keys()) == ("result_product_family_version_id",)
        for constraint in operations.constraints
        if isinstance(constraint, UniqueConstraint)
    )
    checks = " ".join(
        str(constraint.sqltext)
        for constraint in operations.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "product_family_write" in checks
    assert "ifc_product_family_write" in checks
    assert "authoring_draft_write" in checks
    assert "authoring_publish" in checks
    assert "operation_detail_json -> 'version_draft' IS NOT NULL" in checks
    assert "jsonb_typeof(operation_detail_json -> 'version_draft') = 'object'" in checks
    assert "operation_detail_json #>> '{version_draft,abi_profile}'" not in checks
    assert "COALESCE((operation_kind = 'product_family_write'" in checks
    assert "tool_name <> 'ifc_model_initialize'" not in checks
    for ifc_write_tool in (
        "ifc_model_initialize",
        "ifc_model_configure_geometry_basis",
        "ifc_project_update",
        "ifc_spatial_structure_apply",
    ):
        assert ifc_write_tool in checks
    assert "result_product_family_version_id IS NOT NULL" in checks
    fingerprint = next(
        index
        for index in operations.indexes
        if isinstance(index, Index) and index.name == "uq_critical_operations_run_tool_fingerprint"
    )
    assert "product_family_write" in str(fingerprint.dialect_options["postgresql"]["where"])


def test_0074_sql_is_task_only_atomic_and_does_not_create_a_parallel_platform() -> None:
    upgrade = _render("upgrade")
    assert "ADD COLUMN abi_profile VARCHAR(64)" in upgrade
    assert "ALTER COLUMN abi_profile SET NOT NULL" in upgrade
    assert "ck_product_family_versions_abi_profile_valid" in upgrade
    assert "Nonempty ProductFamily ABI digest migration requires online" in upgrade
    assert "ADD COLUMN target_product_family_id VARCHAR(80)" in upgrade
    assert "fk_critical_operations_exact_product_family_base_version" in upgrade
    assert "fk_critical_operations_exact_product_family_result_version" in upgrade
    assert "UNIQUE (result_product_family_version_id)" in upgrade
    assert "ProductFamily Operation must be inserted prepared" in upgrade
    assert "product_family_authoring_source" in upgrade
    assert "operation_detail_json -> 'version_draft' IS NOT NULL" in upgrade
    assert "COALESCE((operation_kind = 'product_family_write'" in upgrade
    assert (
        "parent.error_phase IN ( 'code_validation', 'execution', 'output_validation' )" in upgrade
    )
    assert (
        "parent.operation_detail_json -> 'version_draft' = "
        "NEW.operation_detail_json -> 'version_draft'" in upgrade
    )
    assert "product_families_runtime_insert" in upgrade
    assert "owner_scope = 'TASK'" in upgrade
    assert "cad2bim_runner_runtime, cad2bim_mcp_runtime" in upgrade
    assert "GRANT INSERT ON TABLE product_families" in upgrade
    assert "CREATE TABLE product_family_operations" not in upgrade
    assert "CREATE TABLE realization_program" not in upgrade
    assert "current_product_family" not in upgrade
    assert "UPDATE CRITICAL_OPERATIONS" not in upgrade.upper()
    assert "DELETE FROM CRITICAL_OPERATIONS" not in upgrade.upper()
    assert "tool_name <> 'ifc_model_initialize'" not in upgrade
    for historical_tool_name in (
        "cad_import_source",
        "data_transform",
        "data_repair_transform",
        "cad_compute_measurements",
        "ifc_model_initialize",
        "ifc_model_configure_geometry_basis",
        "ifc_project_update",
        "ifc_spatial_structure_apply",
        "ifc_geometry_resource_create",
        "ifc_geometry_resource_repair",
        "ifc_geometry_resource_write",
    ):
        assert historical_tool_name in upgrade

    downgrade = _render("downgrade")
    assert "ProductFamily authoring facts are not empty" in downgrade
    assert "DROP COLUMN abi_profile" in downgrade
    assert "DROP COLUMN target_product_family_id" in downgrade
    assert (
        "product_family_write"
        not in downgrade.split("ADD CONSTRAINT ck_critical_operations_operation_kind_valid")[-1]
    )


def test_0076_repair_fence_ignores_only_attempt_specific_draft_fields() -> None:
    upgrade = _render_repair_fence("upgrade")
    assert (
        "CREATE OR REPLACE FUNCTION cad2bim_guard_product_family_authoring_operation()" in upgrade
    )
    assert (
        "(parent.operation_detail_json -> 'version_draft') "
        "- 'family_version_id' - 'created_at'" in upgrade
    )
    assert (
        "(NEW.operation_detail_json -> 'version_draft') "
        "- 'family_version_id' - 'created_at'" in upgrade
    )
    assert "UPDATE CRITICAL_OPERATIONS" not in upgrade.upper()

    downgrade = _render_repair_fence("downgrade")
    assert (
        "parent.operation_detail_json -> 'version_draft' = "
        "NEW.operation_detail_json -> 'version_draft'" in downgrade
    )
    assert "- 'family_version_id' - 'created_at'" not in downgrade


def test_product_family_authoring_uses_the_existing_composition_root_and_port() -> None:
    assert issubclass(
        PostgresControlPlaneRepository,
        PostgresProductFamilyAuthoringRepositoryMixin,
    )
    assert ProductFamilyAuthoringRepository.__name__ == "ProductFamilyAuthoringRepository"
    assert (
        PostgresControlPlaneRepository.__mro__.count(PostgresProductFamilyAuthoringRepositoryMixin)
        == 1
    )


@pytest.mark.asyncio
async def test_system_copy_commit_preserves_an_unknown_database_outcome() -> None:
    class _UnavailableSession:
        async def __aenter__(self):
            raise OperationalError("COMMIT", {}, ConnectionError("response lost"))

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    repository = PostgresProductFamilyAuthoringRepositoryMixin(_UnavailableSession)
    with pytest.raises(ProductFamilyAuthoringCommitOutcomeUnknownError):
        await repository.commit_product_family_authoring_success(
            SimpleNamespace(),
            SimpleNamespace(operation=SimpleNamespace()),
            execution_claim=SimpleNamespace(),
        )
