from __future__ import annotations

from pathlib import Path
from inspect import getsource
from CAD2BIM_backend.ifc_application_resources import create_authoring_application_resources

from CAD2BIM_backend.infrastructure.postgres.models import Base
from CAD2BIM_backend.infrastructure.postgres.repository import (
    PostgresControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.postgres.repository_geometry_authoring import (
    PostgresGeometryAuthoringRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.postgres.repository_geometry_resource import (
    PostgresGeometryResourceRepositoryMixin,
)
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.ports.geometry_resources import GeometryResourceRepository
from CAD2BIM_backend.ports.geometry_authoring import GeometryAuthoringRepository
from CAD2BIM_backend.ports.storage import StoragePort


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
BACKEND_ROOT = SOURCE_ROOT / "CAD2BIM_backend"
DIRECT_ROOT = SOURCE_ROOT / "CAD2BIM_mcp" / "direct"
MIGRATION_ROOT = BACKEND_ROOT / "infrastructure" / "postgres" / "migrations" / "versions"
LEGACY_PRODUCT_NAMES = (
    "RunComponentViewModel",
    "ComponentCatalogEventModel",
    "ComponentPublicationReceiptModel",
    "ComponentPromotionModel",
    '"run_component_views"',
    '"component_catalog_events"',
    '"component_publication_receipts"',
    '"component_promotions"',
)


def _active_python_files() -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(BACKEND_ROOT.rglob("*.py"))
        if MIGRATION_ROOT not in path.parents and "__pycache__" not in path.parts
    )


def test_geometry_resource_uses_the_existing_repository_and_storage_compositions() -> None:
    assert issubclass(
        PostgresControlPlaneRepository,
        PostgresGeometryResourceRepositoryMixin,
    )
    assert GeometryResourceRepository.__name__ == "GeometryResourceRepository"
    assert StoragePort.__name__ == "StoragePort"
    assert LocalStorage.__name__ == "LocalStorage"

    production_text = "\n".join(path.read_text(encoding="utf-8") for path in _active_python_files())
    assert "class GeometryStoragePort" not in production_text
    assert "class GeometryLocalStorage" not in production_text
    assert "class GeometryDatabase" not in production_text
    assert "class GeometryUnitOfWork" not in production_text
    bootstrap_text = (DIRECT_ROOT / "bootstrap.py").read_text(encoding="utf-8")
    assert bootstrap_text.count("create_authoring_application_resources(") == 1
    assert getsource(create_authoring_application_resources).count("GeometryResourceService(") == 1


def test_geometry_authoring_extends_the_same_repository_runtime_and_storage() -> None:
    assert issubclass(
        PostgresControlPlaneRepository,
        PostgresGeometryAuthoringRepositoryMixin,
    )
    assert GeometryAuthoringRepository.__name__ == "GeometryAuthoringRepository"

    production_text = "\n".join(path.read_text(encoding="utf-8") for path in _active_python_files())
    for forbidden in (
        "class GeometryAuthoringRoot",
        "class GeometryAuthoringBuildAttempt",
        "class GeometryAuthoringCandidate",
        "class GeometryAuthoringStorage",
        "class GeometryAuthoringDatabase",
        "class GeometryAuthoringUnitOfWork",
        "class GeometryAuthoringRuntime",
        '"generated_model"',
    ):
        assert forbidden not in production_text

    bootstrap_text = (DIRECT_ROOT / "bootstrap.py").read_text(encoding="utf-8")
    authoring_application_text = (
        BACKEND_ROOT / "application" / "geometry_authoring_service.py"
    ).read_text(encoding="utf-8")
    assert "CAD2BIM_backend.infrastructure" not in authoring_application_text
    composition = getsource(create_authoring_application_resources)
    assert bootstrap_text.count("create_authoring_application_resources(") == 1
    assert composition.count("OcpCadQueryGeometryRuntime(") == 1
    assert composition.count("GeometryAuthoringService(") == 1
    assert composition.count("SourceTextGeometryAuthoringSource()") == 1


def test_geometry_authoring_direct_and_worker_import_boundaries_are_one_way() -> None:
    direct_files = tuple((DIRECT_ROOT / "ifc" / "geometry").glob("authoring_*.py"))
    assert len(direct_files) == 3
    direct_text = "\n".join(path.read_text(encoding="utf-8") for path in direct_files)
    for forbidden in (
        "sqlalchemy",
        "CAD2BIM_backend.infrastructure.postgres",
        "CAD2BIM_backend.infrastructure.storage",
        "CAD2BIM_backend.infrastructure.source_text",
        "import cadquery",
        "import OCP",
    ):
        assert forbidden not in direct_text

    worker_text = (
        BACKEND_ROOT / "infrastructure" / "geometry_runtime" / "build_child.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "CAD2BIM_mcp",
        "CAD2BIM_backend.infrastructure.postgres",
        "CAD2BIM_backend.infrastructure.storage",
    ):
        assert forbidden not in worker_text


def test_legacy_product_core_has_no_active_mapping_or_consumer() -> None:
    production_text = "\n".join(path.read_text(encoding="utf-8") for path in _active_python_files())
    for name in LEGACY_PRODUCT_NAMES:
        assert name not in production_text

    migration_text = "\n".join(
        path.read_text(encoding="utf-8") for path in MIGRATION_ROOT.glob("*.py")
    )
    for table_name in (
        "run_component_views",
        "component_catalog_events",
        "component_publication_receipts",
        "component_promotions",
    ):
        assert table_name in migration_text


def test_geometry_resource_files_remain_separate_from_product_family_surface() -> None:
    assert {
        "product_families",
        "product_family_versions",
        "product_family_geometry_bindings",
        "product_family_version_assets",
    } <= set(Base.metadata.tables)
    geometry_files = (
        BACKEND_ROOT / "domain" / "geometry_resource.py",
        BACKEND_ROOT / "application" / "geometry_resource_service.py",
        BACKEND_ROOT / "ports" / "geometry_resources.py",
        BACKEND_ROOT / "infrastructure" / "postgres" / "repository_geometry_resource.py",
    )
    assert all(path.is_file() for path in geometry_files)
    assert all(
        "productfamily" not in path.read_text(encoding="utf-8").lower() for path in geometry_files
    )

    direct_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in DIRECT_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    assert "ifc_geometry_resource_list" in direct_text
    assert "ifc_geometry_resource_info" in direct_text
    assert "ifc_product_family_list" in direct_text
    assert "ifc_product_family_info" in direct_text


def test_geometry_resource_production_files_use_stable_capability_names() -> None:
    forbidden_fragments = ("b3_01", "b301", "i0b3", "phase_b3", "temporary_", "new_")
    paths = (
        BACKEND_ROOT / "domain" / "geometry_resource.py",
        BACKEND_ROOT / "application" / "geometry_resource_service.py",
        BACKEND_ROOT / "ports" / "geometry_resources.py",
        BACKEND_ROOT / "infrastructure" / "postgres" / "repository_geometry_resource.py",
        BACKEND_ROOT / "domain" / "geometry_authoring.py",
        BACKEND_ROOT / "application" / "geometry_authoring_service.py",
        BACKEND_ROOT / "ports" / "geometry_authoring.py",
        BACKEND_ROOT / "infrastructure" / "postgres" / "repository_geometry_authoring.py",
        BACKEND_ROOT / "infrastructure" / "geometry_runtime" / "build_child.py",
        BACKEND_ROOT / "infrastructure" / "source_text" / "geometry_authoring.py",
        BACKEND_ROOT / "infrastructure" / "source_text" / "source_bundle_patch.py",
    )
    for path in paths:
        lowered = path.name.lower()
        assert all(fragment not in lowered for fragment in forbidden_fragments)
