"""Provision fixed non-superuser PostgreSQL login roles after Alembic upgrade."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy import text

from CAD2BIM_backend.ports.identity import IdentityAccessDenied


@dataclass(frozen=True, slots=True)
class RuntimeLoginRole:
    login_role: str
    privilege_role: str
    password_env_name: str


RUNTIME_LOGIN_ROLES = (
    RuntimeLoginRole(
        login_role="cad2bim_api_login",
        privilege_role="cad2bim_api_runtime",
        password_env_name="CAD2BIM_API_POSTGRES_PASSWORD",
    ),
    RuntimeLoginRole(
        login_role="cad2bim_runner_login",
        privilege_role="cad2bim_runner_runtime",
        password_env_name="CAD2BIM_RUNNER_POSTGRES_PASSWORD",
    ),
    RuntimeLoginRole(
        login_role="cad2bim_mcp_login",
        privilege_role="cad2bim_mcp_runtime",
        password_env_name="CAD2BIM_MCP_POSTGRES_PASSWORD",
    ),
)
SYSTEM_CURATOR_LOGIN_ROLE = RuntimeLoginRole(
    login_role="cad2bim_system_curator_login",
    privilege_role="cad2bim_system_curator",
    password_env_name="CAD2BIM_SYSTEM_CURATOR_POSTGRES_PASSWORD",
)
_ROLE_CHOICES = dict(zip(("api", "runner", "mcp"), RUNTIME_LOGIN_ROLES, strict=True))
_ROLE_CHOICES["system-curator"] = SYSTEM_CURATOR_LOGIN_ROLE


async def require_system_curator(connection) -> None:
    """Check the connected login independently of any caller-supplied actor kind."""
    row = (await connection.execute(text("""
        SELECT role.rolsuper, role.rolbypassrls,
               pg_has_role(current_user, 'cad2bim_system_curator', 'MEMBER') AS is_member,
               pg_has_role(current_user, 'cad2bim_api_runtime', 'MEMBER') AS is_api,
               pg_has_role(current_user, 'cad2bim_mcp_runtime', 'MEMBER') AS is_mcp,
               pg_has_role(current_user, 'cad2bim_runner_runtime', 'MEMBER') AS is_runner
        FROM pg_roles AS role WHERE role.rolname = current_user
    """))).one_or_none()
    if (row is None or row.rolsuper or row.rolbypassrls or not row.is_member
        or row.is_api or row.is_mcp or row.is_runner):
        raise IdentityAccessDenied(
            "SYSTEM publication requires a non-superuser, non-BYPASSRLS login "
            "that inherits only cad2bim_system_curator among runtime roles"
        )


def _psycopg_database_url(database_url: str) -> str:
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+psycopg":
        raise ValueError("CAD2BIM_DATABASE_URL must use postgresql+psycopg://")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _selected_roles(environ: Mapping[str, str], role_names: tuple[str, ...] | None):
    if role_names is None:
        return RUNTIME_LOGIN_ROLES + (
            (SYSTEM_CURATOR_LOGIN_ROLE,)
            if SYSTEM_CURATOR_LOGIN_ROLE.password_env_name in environ else ()
        )
    if not role_names or len(set(role_names)) != len(role_names):
        raise ValueError("select at least one distinct runtime role")
    if any(name not in _ROLE_CHOICES for name in role_names):
        raise ValueError("unknown runtime role selection")
    return tuple(_ROLE_CHOICES[name] for name in role_names)


def _runtime_passwords(
    environ: Mapping[str, str], *, role_names: tuple[str, ...] | None = None,
) -> dict[str, str]:
    values: dict[str, str] = {}
    roles = _selected_roles(environ, role_names)
    for role in roles:
        password = environ.get(role.password_env_name, "")
        if len(password) < 16:
            raise RuntimeError(
                f"{role.password_env_name} must contain at least 16 characters"
            )
        if quote(password, safe="") != password:
            raise RuntimeError(
                f"{role.password_env_name} must be URL-safe ASCII"
            )
        values[role.login_role] = password
    if len(set(values.values())) != len(values):
        raise RuntimeError("runtime PostgreSQL passwords must be distinct")
    return values


def provision_runtime_login_roles(
    database_url: str,
    *,
    environ: Mapping[str, str] | None = None,
    role_names: tuple[str, ...] | None = None,
) -> None:
    """Create or rotate fixed login roles without emitting credential values."""

    source = os.environ if environ is None else environ
    roles = _selected_roles(source, role_names)
    passwords = _runtime_passwords(source, role_names=role_names)
    with psycopg.connect(_psycopg_database_url(database_url)) as connection:
        with connection.cursor() as cursor:
            for role in roles:
                cursor.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = %s)",
                    (role.login_role,),
                )
                exists = cursor.fetchone()
                if exists is None or not exists[0]:
                    cursor.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                        ).format(sql.Identifier(role.login_role))
                    )
                cursor.execute(
                    "SELECT set_config('cad2bim.runtime_role_password', %s, true)",
                    (passwords[role.login_role],),
                )
                cursor.execute(
                    sql.SQL(
                        """
                        DO $block$
                        DECLARE
                            runtime_password text := current_setting(
                                'cad2bim.runtime_role_password', true
                            );
                        BEGIN
                            IF runtime_password IS NULL OR runtime_password = '' THEN
                                RAISE EXCEPTION 'runtime role password is unavailable';
                            END IF;
                            EXECUTE format(
                                'ALTER ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB '
                                'NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
                                {},
                                runtime_password
                            );
                            PERFORM set_config(
                                'cad2bim.runtime_role_password', '', true
                            );
                        END
                        $block$
                        """
                    ).format(sql.Literal(role.login_role))
                )
                cursor.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(role.privilege_role),
                        sql.Identifier(role.login_role),
                    )
                )


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roles", nargs="+", choices=tuple(_ROLE_CHOICES),
                        help="Only create/rotate the selected login roles; defaults to the full runtime set")
    args = parser.parse_args(argv)
    database_url = os.environ.get("CAD2BIM_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("CAD2BIM_DATABASE_URL is required")
    provision_runtime_login_roles(database_url, role_names=tuple(args.roles) if args.roles else None)
    print("runtime PostgreSQL login roles provisioned")


__all__ = [
    "RUNTIME_LOGIN_ROLES",
    "SYSTEM_CURATOR_LOGIN_ROLE",
    "RuntimeLoginRole",
    "cli",
    "provision_runtime_login_roles",
    "require_system_curator",
]


if __name__ == "__main__":
    cli()
