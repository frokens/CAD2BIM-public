from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from CAD2BIM_backend.infrastructure.postgres.models import Base


IDENTITY_TABLES = {
    "principals",
    "tenants",
    "tenant_principals",
    "task_tenant_bindings",
    "identity_migration_quarantine",
}
LEGACY_OWNER_TABLES = {
    name for name, table in Base.metadata.tables.items() if "owner_id" in table.c
}


def _unique_columns(table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_identity_and_personal_tenant_tables_are_part_of_canonical_metadata() -> None:
    assert IDENTITY_TABLES <= set(Base.metadata.tables)
    assert {"issuer", "subject", "status", "legacy_owner_id"} <= set(
        Base.metadata.tables["principals"].c.keys()
    )
    assert ("issuer", "subject") in _unique_columns("principals")
    assert {"tenant_id", "principal_id", "access"} <= set(
        Base.metadata.tables["tenant_principals"].c.keys()
    )
    assert ("principal_id",) in _unique_columns("tenant_principals")
    assert {"tenant_id", "task_id", "principal_id", "legacy_owner_id"} <= set(
        Base.metadata.tables["task_tenant_bindings"].c.keys()
    )


def _foreign_keys(table_name: str) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    return {
        (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def test_every_legacy_owner_table_has_one_required_tenant_column() -> None:
    assert len(LEGACY_OWNER_TABLES) == 31
    for table_name in LEGACY_OWNER_TABLES:
        tenant_id = Base.metadata.tables[table_name].c.tenant_id
        assert tenant_id.type.length == 80
        assert tenant_id.nullable is False


def test_tenant_scope_is_bound_from_user_to_task_and_all_task_resources() -> None:
    assert ("owner_id", "tenant_id") in _unique_columns("users")
    assert ("tenant_id", "task_id") in _unique_columns("tasks")
    assert (
        ("owner_id", "tenant_id"),
        ("users.owner_id", "users.tenant_id"),
    ) in _foreign_keys("tasks")

    task_scoped_tables = LEGACY_OWNER_TABLES - {"users", "tasks"}
    for table_name in task_scoped_tables:
        assert (
            ("tenant_id", "task_id"),
            ("tasks.tenant_id", "tasks.task_id"),
        ) in _foreign_keys(table_name)
    assert (
        ("tenant_id", "task_id"),
        ("tasks.tenant_id", "tasks.task_id"),
    ) in _foreign_keys("task_tenant_bindings")


def test_identity_quarantine_records_unprovable_legacy_owners_without_claims() -> None:
    quarantine = Base.metadata.tables["identity_migration_quarantine"]
    assert set(quarantine.c.keys()) == {
        "legacy_owner_id",
        "reason_code",
        "task_count",
        "created_at",
    }
    assert "claims" not in quarantine.c
    assert "token" not in quarantine.c
