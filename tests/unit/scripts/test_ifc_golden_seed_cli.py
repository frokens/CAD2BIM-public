from __future__ import annotations

from pathlib import Path

import pytest

from scripts.maintenance import export_run_fixture
from scripts.maintenance import restore_run_fixture
from scripts.maintenance import verify_run_fixture


SEED = "1" * 64
RUN_ID = f"run_{'2' * 32}"
SECRET = "do-not-print-this-secret"
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_export_accepts_only_public_identity_and_frontier_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    source_root = tmp_path / "source"
    fixture_root = tmp_path / "fixtures"
    source_root.mkdir()
    fixture_root.mkdir()
    monkeypatch.setenv(
        "CAD2BIM_I0_SOURCE_DATABASE_URL",
        f"postgresql://fixture:{SECRET}@127.0.0.1/source_db",
    )
    monkeypatch.setenv("CAD2BIM_I0_SOURCE_STORAGE_ROOT", str(source_root))
    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(fixture_root))

    async def export(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(export_run_fixture, "_load_exporter", lambda: export)

    assert (
        export_run_fixture.main(
            [
                "--source-run-id",
                RUN_ID,
                "--catalog-frontier",
                "18",
                "--operation-frontier",
                "11",
            ]
        )
        == 0
    )

    assert captured["source_run_id"] == RUN_ID
    assert captured["catalog_frontier"] == 18
    assert captured["operation_frontier"] == 11
    assert SECRET in str(captured["source_database_url"])
    assert captured["source_storage_root"] == source_root.resolve()
    assert captured["seed_root"] == fixture_root.resolve()


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (export_run_fixture, ["--database-url", f"postgresql://user:{SECRET}@db/x"]),
        (restore_run_fixture, ["--force"]),
        (restore_run_fixture, ["--drop"]),
        (restore_run_fixture, ["--clean"]),
    ],
)
def test_forbidden_or_unknown_arguments_are_rejected_without_echo(
    module: object,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert module.main(argv) == 2  # type: ignore[attr-defined]

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "I0_CLI_ARGUMENT_INVALID: command arguments are invalid\n"
    assert SECRET not in captured.err


def test_verify_resolves_digest_below_env_fixture_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified: list[Path] = []
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(fixture_root))
    monkeypatch.setattr(verify_run_fixture, "_load_verifier", lambda: verified.append)

    assert verify_run_fixture.main(["--seed", SEED]) == 0

    assert verified == [(fixture_root / SEED).resolve()]


def test_restore_requires_exact_database_confirmation_before_materializer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def restore(**_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setenv(
        "CAD2BIM_I0_TARGET_DATABASE_URL",
        f"postgresql://fixture:{SECRET}@127.0.0.1/isolated_copy",
    )
    monkeypatch.setenv("CAD2BIM_I0_TARGET_STORAGE_ROOT", str(tmp_path / "target"))
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(fixture_root))

    async def async_restore(**kwargs: object) -> None:
        restore(**kwargs)

    monkeypatch.setattr(restore_run_fixture, "_load_restorer", lambda: async_restore)

    assert (
        restore_run_fixture.main(
            [
                "--seed",
                SEED,
                "--target-session-name",
                "g0-copy-1",
                "--confirm-target-database",
                "wrong_database",
            ]
        )
        == 2
    )

    assert called is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "I0_CLI_TARGET_CONFIRMATION_MISMATCH: "
        "target database confirmation does not match\n"
    )
    assert SECRET not in captured.err


def test_restore_passes_secret_configuration_only_to_materializer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv(
        "CAD2BIM_I0_TARGET_DATABASE_URL",
        f"postgresql://fixture:{SECRET}@127.0.0.1/isolated_copy",
    )
    monkeypatch.setenv("CAD2BIM_I0_TARGET_STORAGE_ROOT", str(tmp_path / "target"))
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(fixture_root))

    async def restore(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(restore_run_fixture, "_load_restorer", lambda: restore)

    assert (
        restore_run_fixture.main(
            [
                "--seed",
                SEED,
                "--target-session-name",
                "g0-copy-1",
                "--confirm-target-database",
                "isolated_copy",
            ]
        )
        == 0
    )

    assert captured["seed_root"] == (fixture_root / SEED).resolve()
    assert captured["target_session_name"] == "g0-copy-1"
    assert SECRET in str(captured["target_database_url"])
    assert captured["target_storage_root"] == (tmp_path / "target").resolve()
    assert SECRET not in capsys.readouterr().out


def test_unexpected_errors_emit_only_stable_code_and_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(fixture_root))

    def fail(_path: Path) -> None:
        raise RuntimeError(f"failure included {SECRET}")

    monkeypatch.setattr(verify_run_fixture, "_load_verifier", lambda: fail)

    assert verify_run_fixture.main(["--seed", SEED]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "I0_CLI_VERIFY_FAILED: Run fixture verification failed\n"
    assert SECRET not in captured.err


def test_database_and_storage_configuration_have_no_cli_flags() -> None:
    export_options = export_run_fixture._parser().format_help()
    restore_options = restore_run_fixture._parser().format_help()

    assert "--database-url" not in export_options + restore_options
    assert "--storage-root" not in export_options + restore_options
    assert "--force" not in restore_options
    assert "--drop" not in restore_options
    assert "--clean" not in restore_options


def test_source_storage_rejects_symlink_without_calling_exporter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_source = tmp_path / "real-source"
    source_link = tmp_path / "source-link"
    fixture_root = tmp_path / "fixtures"
    real_source.mkdir()
    source_link.symlink_to(real_source, target_is_directory=True)
    fixture_root.mkdir()
    monkeypatch.setenv(
        "CAD2BIM_I0_SOURCE_DATABASE_URL",
        "postgresql://fixture:password@127.0.0.1/source_db",
    )
    monkeypatch.setenv("CAD2BIM_I0_SOURCE_STORAGE_ROOT", str(source_link))
    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(fixture_root))
    called = False

    def exporter(**_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(export_run_fixture, "_load_exporter", lambda: exporter)

    assert (
        export_run_fixture.main(
            [
                "--source-run-id",
                RUN_ID,
                "--catalog-frontier",
                "1",
                "--operation-frontier",
                "1",
            ]
        )
        == 2
    )
    assert called is False


def test_restore_rejects_preexisting_target_without_calling_materializer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixtures"
    target_root = tmp_path / "target"
    fixture_root.mkdir()
    target_root.mkdir()
    monkeypatch.setenv(
        "CAD2BIM_I0_TARGET_DATABASE_URL",
        "postgresql://fixture:password@127.0.0.1/isolated_copy",
    )
    monkeypatch.setenv("CAD2BIM_I0_TARGET_STORAGE_ROOT", str(target_root))
    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(fixture_root))
    called = False

    def restorer(**_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(restore_run_fixture, "_load_restorer", lambda: restorer)

    assert (
        restore_run_fixture.main(
            [
                "--seed",
                SEED,
                "--target-session-name",
                "g0-copy-1",
                "--confirm-target-database",
                "isolated_copy",
            ]
        )
        == 2
    )
    assert called is False


def test_fixture_root_inside_git_repository_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def verifier(_path: Path) -> None:
        nonlocal called
        called = True

    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(PROJECT_ROOT))
    monkeypatch.setattr(verify_run_fixture, "_load_verifier", lambda: verifier)

    assert verify_run_fixture.main(["--seed", SEED]) == 2
    assert called is False


def test_restore_rejects_symlink_target_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixtures"
    real_parent = tmp_path / "real-parent"
    parent_link = tmp_path / "parent-link"
    fixture_root.mkdir()
    real_parent.mkdir()
    parent_link.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.setenv(
        "CAD2BIM_I0_TARGET_DATABASE_URL",
        "postgresql://fixture:password@127.0.0.1/isolated_copy",
    )
    monkeypatch.setenv("CAD2BIM_I0_TARGET_STORAGE_ROOT", str(parent_link / "target"))
    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(fixture_root))
    called = False

    def restorer(**_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(restore_run_fixture, "_load_restorer", lambda: restorer)

    assert (
        restore_run_fixture.main(
            [
                "--seed",
                SEED,
                "--target-session-name",
                "g0-copy-1",
                "--confirm-target-database",
                "isolated_copy",
            ]
        )
        == 2
    )
    assert called is False


def test_session_name_is_nfkc_normalized() -> None:
    from scripts.maintenance._ifc_golden_seed_cli import session_name

    assert session_name("  Ｇ０ copy  ") == "G0 copy"


@pytest.mark.parametrize("database", ["postgres", "template0", "template1"])
def test_restore_rejects_postgres_system_databases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: str,
) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    monkeypatch.setenv(
        "CAD2BIM_I0_TARGET_DATABASE_URL",
        f"postgresql://fixture:password@127.0.0.1/{database}",
    )
    monkeypatch.setenv("CAD2BIM_I0_TARGET_STORAGE_ROOT", str(tmp_path / "target"))
    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(fixture_root))

    assert (
        restore_run_fixture.main(
            [
                "--seed",
                SEED,
                "--target-session-name",
                "g0-copy-1",
                "--confirm-target-database",
                database,
            ]
        )
        == 2
    )


def test_fixture_root_rejects_an_intermediate_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    linked_parent = tmp_path / "linked-parent"
    fixture_root = real_parent / "fixtures"
    fixture_root.mkdir(parents=True)
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.setenv("CAD2BIM_I0_FIXTURE_ROOT", str(linked_parent / "fixtures"))

    assert verify_run_fixture.main(["--seed", SEED]) == 2
