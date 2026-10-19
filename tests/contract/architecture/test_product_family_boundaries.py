from __future__ import annotations

import ast
import inspect
from pathlib import Path
from inspect import getsource
from CAD2BIM_backend.ifc_application_resources import create_authoring_application_resources

from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres.repository_product_family import (
    PostgresProductFamilyRepositoryMixin,
)
from CAD2BIM_backend.ports.product_families import ProductFamilyRepository
from CAD2BIM_backend.ports.storage import ProductFamilyVersionStorageKey, StoragePort


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
BACKEND_ROOT = SOURCE_ROOT / "CAD2BIM_backend"
DIRECT_ROOT = SOURCE_ROOT / "CAD2BIM_mcp" / "direct"
MIGRATION_ROOT = BACKEND_ROOT / "infrastructure" / "postgres" / "migrations" / "versions"


def _active_backend_files() -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(BACKEND_ROOT.rglob("*.py"))
        if MIGRATION_ROOT not in path.parents and "__pycache__" not in path.parts
    )


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return tuple(names)


def test_product_family_extends_the_one_repository_and_composition_root() -> None:
    assert issubclass(PostgresControlPlaneRepository, PostgresProductFamilyRepositoryMixin)
    assert ProductFamilyRepository.__name__ == "ProductFamilyRepository"
    assert StoragePort.__name__ == "StoragePort"
    assert ProductFamilyVersionStorageKey.__name__ == "ProductFamilyVersionStorageKey"
    assert PostgresControlPlaneRepository.__mro__.count(PostgresProductFamilyRepositoryMixin) == 1

    bootstrap = (DIRECT_ROOT / "bootstrap.py").read_text(encoding="utf-8")
    composition = getsource(create_authoring_application_resources)
    assert bootstrap.count("create_authoring_application_resources(") == 1
    assert composition.count("ProductFamilyCatalogService(") == 1
    assert composition.count("ProductFamilyAssetService(storage, source_bundles)") == 1
    assert composition.count("SourceTextGeometryAuthoringSource()") == 1
    assert "ProductFamilyStorage" not in bootstrap


def test_repository_composition_root_has_no_shadowed_private_capability_helpers() -> None:
    owners: dict[str, list[str]] = {}
    for base in PostgresControlPlaneRepository.__mro__[1:]:
        if not base.__module__.startswith(
            "CAD2BIM_backend.infrastructure.postgres.repository"
        ):
            continue
        for name, value in base.__dict__.items():
            if name.startswith("__") or not name.startswith("_"):
                continue
            if not (
                inspect.isfunction(value)
                or isinstance(value, (staticmethod, classmethod))
            ):
                continue
            owners.setdefault(name, []).append(base.__name__)
    assert {name: classes for name, classes in owners.items() if len(classes) > 1} == {}


def test_product_family_layers_and_direct_adapter_keep_one_way_imports() -> None:
    backend_paths = (
        BACKEND_ROOT / "domain" / "product_family.py",
        BACKEND_ROOT / "domain" / "product_family_catalog.py",
        BACKEND_ROOT / "application" / "product_family_asset_service.py",
        BACKEND_ROOT / "application" / "product_family_catalog_service.py",
        BACKEND_ROOT / "ports" / "product_families.py",
    )
    assert all(path.is_file() for path in backend_paths)
    for path in backend_paths:
        imports = _imports(path)
        assert not any(name == "CAD2BIM_mcp" or name.startswith("CAD2BIM_mcp.") for name in imports)
    for path in backend_paths[:2]:
        assert not any(
            name == "CAD2BIM_backend.infrastructure"
            or name.startswith("CAD2BIM_backend.infrastructure.")
            for name in _imports(path)
        )
    for path in backend_paths[2:4]:
        assert not any(
            name == "CAD2BIM_backend.infrastructure"
            or name.startswith("CAD2BIM_backend.infrastructure.")
            for name in _imports(path)
        )

    direct_family = DIRECT_ROOT / "ifc" / "product_family"
    direct_paths = tuple(direct_family.glob("*.py"))
    assert {path.name for path in direct_paths} >= {
        "__init__.py",
        "application.py",
        "contracts.py",
        "tools.py",
    }
    direct_text = "\n".join(path.read_text(encoding="utf-8") for path in direct_paths)
    for forbidden in (
        "sqlalchemy",
        "CAD2BIM_backend.infrastructure.postgres",
        "CAD2BIM_backend.infrastructure.storage",
    ):
        assert forbidden not in direct_text


def test_product_family_has_no_parallel_program_operation_storage_or_current_platform() -> None:
    production_text = "\n".join(
        path.read_text(encoding="utf-8") for path in _active_backend_files()
    )
    for forbidden in (
        "class ProductFamilyDatabase",
        "class ProductFamilyUnitOfWork",
        "class ProductFamilyStoragePort",
        "class ProductFamilyLocalStorage",
        "class ProductFamilyOperation(",
        "class ProductFamilyOperation:",
        "class RealizationProgramRepository",
        "class RealizationProgramModel",
        '"product_family_current"',
        '"product_family_currents"',
        '"realization_programs"',
        '"realization_program_versions"',
    ):
        assert forbidden not in production_text

    models_text = (BACKEND_ROOT / "infrastructure" / "postgres" / "models.py").read_text(
        encoding="utf-8"
    )
    assert models_text.count('__tablename__ = "product_families"') == 1
    assert models_text.count('__tablename__ = "product_family_versions"') == 1
    assert models_text.count('__tablename__ = "product_family_geometry_bindings"') == 1
    assert models_text.count('__tablename__ = "product_family_version_assets"') == 1
    storage_text = (BACKEND_ROOT / "infrastructure" / "storage" / "local.py").read_text(
        encoding="utf-8"
    )
    assert storage_text.count("class LocalStorage") == 1


def test_geometry_and_product_family_remain_separate_domain_aggregates() -> None:
    geometry_paths = (
        BACKEND_ROOT / "domain" / "geometry_resource.py",
        BACKEND_ROOT / "application" / "geometry_resource_service.py",
        BACKEND_ROOT / "ports" / "geometry_resources.py",
        BACKEND_ROOT / "infrastructure" / "postgres" / "repository_geometry_resource.py",
    )
    assert all(
        "productfamily" not in path.read_text(encoding="utf-8").lower() for path in geometry_paths
    )

    product_domain = (BACKEND_ROOT / "domain" / "product_family.py").read_text(encoding="utf-8")
    assert "GeometryResourceKind" in product_domain
    assert "ProductFamilyGeometryBindingRecord" in product_domain
    assert "GeometryResourceVersionBundle" not in product_domain
    assert "GeometryResourceRepository" not in product_domain


def test_product_family_production_files_use_stable_capability_names() -> None:
    forbidden = ("b3_05", "b305", "i0b3", "phase_b3", "temporary_", "new_")
    paths = (
        BACKEND_ROOT / "domain" / "product_family.py",
        BACKEND_ROOT / "domain" / "product_family_catalog.py",
        BACKEND_ROOT / "application" / "product_family_asset_service.py",
        BACKEND_ROOT / "application" / "product_family_catalog_service.py",
        BACKEND_ROOT / "ports" / "product_families.py",
        BACKEND_ROOT / "infrastructure" / "postgres" / "repository_product_family.py",
        DIRECT_ROOT / "ifc" / "product_family" / "application.py",
        DIRECT_ROOT / "ifc" / "product_family" / "contracts.py",
        DIRECT_ROOT / "ifc" / "product_family" / "tools.py",
    )
    assert all(path.is_file() for path in paths)
    for path in paths:
        lowered = path.name.lower()
        assert all(fragment not in lowered for fragment in forbidden)
