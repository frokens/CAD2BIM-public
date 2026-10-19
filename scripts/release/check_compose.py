"""Validate the deferred Docker Compose configuration without starting Docker."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "deploy" / "compose.foundation.yaml"
_CONFIGURATION_PREFIXES = ("CAD2BIM_", "DEEPSEEK_", "KIMI_", "AUTHING_")


def _compose_command(env_file: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        "cad2bim-config-check",
        "--env-file",
        str(env_file),
        "-f",
        str(COMPOSE_FILE),
        "config",
        "--quiet",
    ]


def _subprocess_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(_CONFIGURATION_PREFIXES)
    }


def _write_placeholder_environment(directory: Path) -> Path:
    """Provide interpolation values without reading the developer's environment file."""

    env_file = directory / "cad2bim-compose-config.env"
    values = {
        "CAD2BIM_ENV_FILE": str(env_file),
        "CAD2BIM_IMAGE_TAG": "deferred-config-check",
        "CAD2BIM_POSTGRES_PASSWORD": "compose-config-placeholder",
        "CAD2BIM_API_POSTGRES_PASSWORD": "compose-config-placeholder",
        "CAD2BIM_RUNNER_POSTGRES_PASSWORD": "compose-config-placeholder",
        "CAD2BIM_MCP_POSTGRES_PASSWORD": "compose-config-placeholder",
    }
    env_file.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return env_file


def check_compose_config() -> dict[str, object]:
    """Parse the provisional Compose file; never build images or start containers."""

    if shutil.which("docker") is None:
        raise RuntimeError("docker command is unavailable")
    with tempfile.TemporaryDirectory(prefix="cad2bim-compose-config-") as raw_directory:
        env_file = _write_placeholder_environment(Path(raw_directory))
        subprocess.run(
            _compose_command(env_file),
            cwd=REPOSITORY_ROOT,
            check=True,
            timeout=30,
            env=_subprocess_environment(),
        )
    return {
        "status": "passed",
        "deployment_deferred": True,
        "images_built": False,
        "containers_started": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(check_compose_config(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
