"""Require executable Geometry assets without runtime fingerprint evidence.

Revision ID: 20260927_0091
Revises: 20260927_0090
"""

from importlib import import_module


revision = "20260927_0091"
down_revision = "20260927_0090"
branch_labels = None
depends_on = None

_OLD = "asset_role IN ('source_bundle','runtime_manifest','parameter_schema')) <> 3"
_CURRENT = "asset_role IN ('source_bundle','parameter_schema')) <> 2"


def _replace(old: str, new: str) -> None:
    guards = import_module(
        "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
        "20260920_0087_ephemeral_authoring_validation"
    )
    guards._replace_function("cad2bim_guard_geometry_formal_version", old, new)


def upgrade() -> None:
    _replace(_OLD, _CURRENT)


def downgrade() -> None:
    _replace(_CURRENT, _OLD)
