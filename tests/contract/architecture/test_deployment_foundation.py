from __future__ import annotations

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _yaml(path: str) -> dict:
    return yaml.safe_load(
        (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
    )


def test_foundation_compose_is_production_and_has_no_fake_model_factory() -> None:
    compose = _yaml("deploy/compose.foundation.yaml")
    rendered = (REPOSITORY_ROOT / "deploy/compose.foundation.yaml").read_text(
        encoding="utf-8"
    )

    assert compose["x-cad2bim-service"]["environment"]["CAD2BIM_ENV"] == "production"
    assert compose["x-cad2bim-service"]["platform"] == (
        "${CAD2BIM_DOCKER_PLATFORM:-linux/amd64}"
    )
    assert set(compose["services"]) == {"postgres", "migrate"}
    assert "CAD2BIM_MODEL_FACTORY" not in rendered
    assert "tests." not in rendered


def test_production_docker_target_does_not_contain_release_fixtures() -> None:
    dockerfile = (REPOSITORY_ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock README.md alembic.ini ./" in dockerfile
    assert "uv sync --frozen --no-editable" in dockerfile
    assert "python -m pip install ." not in dockerfile
    assert "COPY tests" not in dockerfile
    assert [
        line for line in dockerfile.splitlines() if line.startswith("FROM ")
    ] == [
        "FROM python:3.11-slim-bookworm AS runtime",
        "FROM runtime AS production",
    ]


def test_foundation_postgres_keeps_production_durability_configuration() -> None:
    compose = _yaml("deploy/compose.foundation.yaml")
    postgres = compose["services"]["postgres"]

    assert "command" not in postgres
