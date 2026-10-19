from __future__ import annotations

from pathlib import Path

import pytest

from scripts.maintenance import recover_m4_round6_database as cli
from tests.process_support.ifc_golden_seed.recovery import (
    M4RecoveryError,
    RELATIONAL_ROOT_SHA256,
    SOURCE_MANIFEST_SHA256,
)


def _arguments(source: Path, work: Path) -> list[str]:
    return [
        "--source-pgdata-copy",
        str(source),
        "--work-root",
        str(work),
        "--confirm-source-manifest",
        SOURCE_MANIFEST_SHA256,
        "--confirm-relational-root",
        RELATIONAL_ROOT_SHA256,
    ]


def test_cli_passes_only_confirmed_paths_to_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Path] = {}

    def recover(**values: Path) -> None:
        captured.update(values)

    source = tmp_path / "source"
    original = tmp_path / "original"
    original.mkdir()
    monkeypatch.setenv(cli.ORIGINAL_PGDATA_ENV, str(original))
    work = tmp_path / "work"
    arguments = cli._parser().parse_args(_arguments(source, work))
    cli.run(arguments, recovery=recover)

    assert captured == {
        "original_pgdata": original,
        "source_pgdata_copy": source,
        "work_root": work,
    }
    assert "source" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("option", "value", "expected_code"),
    [
        (
            "--confirm-source-manifest",
            "0" * 64,
            "I0_CLI_M4_SOURCE_CONFIRMATION_MISMATCH",
        ),
        (
            "--confirm-relational-root",
            "0" * 64,
            "I0_CLI_M4_ROOT_CONFIRMATION_MISMATCH",
        ),
    ],
)
def test_confirmation_mismatch_rejects_before_recovery(
    option: str,
    value: str,
    expected_code: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def recover(**_values: Path) -> None:
        nonlocal called
        called = True

    argv = _arguments(tmp_path / "source", tmp_path / "work")
    argv[argv.index(option) + 1] = value
    arguments = cli._parser().parse_args(argv)
    with pytest.raises(cli.CliRuleError) as captured:
        cli.run(arguments, recovery=recover)

    assert called is False
    assert captured.value.rule_code == expected_code
    assert capsys.readouterr().out == ""


def test_recovery_failure_emits_only_stable_safe_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "do-not-print-recovery-secret"
    original = tmp_path / "original"
    original.mkdir()
    monkeypatch.setenv(cli.ORIGINAL_PGDATA_ENV, str(original))

    def fail(**_values: Path) -> None:
        raise M4RecoveryError("I0_M4_TEST_REJECTED", f"unsafe {secret}")

    monkeypatch.setattr(cli, "_load_recovery", lambda: fail)

    assert cli.main(_arguments(tmp_path / secret, tmp_path / "work")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "I0_M4_TEST_REJECTED: M4 database recovery was rejected\n"
    assert secret not in captured.err


def test_unknown_database_or_destructive_flags_are_not_accepted() -> None:
    help_text = cli._parser().format_help()

    assert "--database-url" not in help_text
    assert "--drop" not in help_text
    assert "--force" not in help_text
    assert "--clean" not in help_text
