from io import StringIO

from alembic import command
from alembic.config import Config


def test_standard_assets_extend_existing_rows_and_preserve_immutable_downgrade():
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/cad2bim")
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260907_0079:20260909_0080", sql=True)
    upgrade = output.getvalue()
    assert "standard_model_geometry" in upgrade
    assert "standard_model_manifest" in upgrade
    assert "CREATE OR REPLACE FUNCTION cad2bim_guard_product_family_version_complete" in upgrade
    assert "ck_product_family_version_assets_asset_role_valid" in upgrade
    assert "ck_product_family_version_assets_ck_" not in upgrade
    assert "CREATE TABLE" not in upgrade
    assert "CREATE POLICY" not in upgrade
    output.seek(0)
    output.truncate()
    command.downgrade(config, "20260909_0080:20260907_0079", sql=True)
    downgrade = output.getvalue()
    assert "Cannot downgrade while immutable standard model versions exist" in downgrade
    assert "DELETE FROM" not in downgrade


def test_ephemeral_authoring_migration_emits_current_guards_without_rewriting_history():
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:secret@example.invalid/cad2bim")
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, "20260920_0086:20260920_0087", sql=True)
    sql = output.getvalue()
    assert "product_family_authoring_draft_v2" in sql
    assert "geometry_authoring_input_v2" in sql
    assert "cad2bim_family_formal_repair_identity" not in sql
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "scope = 'SYSTEM' THEN RETURN NEW" not in sql
    assert "asset_role NOT IN ('manifest', 'validation_report')" in sql
    assert "New Family versions cannot retain validation reports" in sql
    assert "'{\"valid\": true}'::jsonb" in sql
    assert "valid\"NULL" not in sql
    assert "UPDATE product_family_versions" not in sql
    assert "UPDATE critical_operations" not in sql
    assert "DELETE FROM" not in sql
    output.seek(0)
    output.truncate()
    command.downgrade(config, "20260920_0087:20260920_0086", sql=True)
    assert "Cannot downgrade retained formal authoring facts" in output.getvalue()


def test_ephemeral_guard_patches_match_the_retained_function_sources(monkeypatch):
    from importlib import import_module
    import re

    prefix = "CAD2BIM_backend.infrastructure.postgres.migrations.versions."
    modules = {key: import_module(prefix + name) for key, name in {
        "standard": "20260909_0080_family_standard_model_assets",
        "repair": "20260905_0076_product_family_repair_fence",
        "annotation": "20260905_0077_geometry_interface_annotations",
        "usage": "20260913_0085_version_usage_instructions",
        "current": "20260920_0087_ephemeral_authoring_validation",
    }.items()}
    definitions = {}
    checking_current = False

    class Operations:
        def execute(self, statement):
            if checking_current:
                assert not statement._bindparams  # New SQL literals must not become binds.
            source = str(statement)
            match = re.search(r"CREATE(?: OR REPLACE)? FUNCTION (\w+)\(", source)
            if match:
                definitions[match[1]] = source

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    for module in modules.values():
        monkeypatch.setattr(module, "op", Operations())
    modules["standard"]._guard(standard=True)
    modules["repair"]._install_guard(ignore_attempt_identity=True)
    modules["annotation"]._install_guard(annotation=True)
    modules["usage"].upgrade()
    annotation_name = "cad2bim_guard_geometry_authoring_operation"
    definitions[annotation_name] = definitions[annotation_name].replace(
        modules["usage"]._GEOMETRY_ANNOTATION_OLD_RESULT_GUARD,
        modules["usage"]._GEOMETRY_ANNOTATION_USAGE_RESULT_GUARD,
    )
    for name in ("cad2bim_guard_product_family_current_version", "cad2bim_guard_product_family_current_operation"):
        definitions[name] = definitions[name].replace("product_family_contract_v2", "product_family_contract_v3").replace("product_realization_v2", "product_realization_v3")

    def replace(name, old, new):
        assert old in definitions[name], (name, old)
        definitions[name] = definitions[name].replace(old, new)

    monkeypatch.setattr(modules["current"], "_replace_function", replace)
    checking_current = True
    modules["current"].upgrade()
    assert "validation_scenarios" in definitions["cad2bim_guard_product_family_current_operation"]
    assert "validation_profile" in definitions[annotation_name]
    modules["current"].downgrade()
