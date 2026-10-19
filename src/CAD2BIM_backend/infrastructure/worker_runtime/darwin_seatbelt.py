"""Darwin Seatbelt launcher shared by controlled local worker adapters."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


DARWIN_SANDBOX_EXECUTABLE = Path("/usr/bin/sandbox-exec")

_SYSTEM_PROFILE_READ_DENIES: tuple[str, ...] = (
    "/private/etc/master.passwd",
    "/private/etc/passwd",
    "/private/etc/protocols",
    "/private/etc/services",
)
_SYSTEM_PROFILE_WRITE_DENIES: tuple[str, ...] = (
    "/cores",
    "/dev/dtracehelper",
    "/dev/null",
    "/dev/zero",
)


def _sbpl_string(value: str | Path) -> str:
    return json.dumps(os.fspath(value), ensure_ascii=False)


class DarwinSeatbeltSandbox:
    """Build one fail-closed Seatbelt launcher for a trusted Python runtime."""

    def __init__(
        self,
        python_executable: str,
        *,
        trusted_source_files: tuple[Path, ...] = (),
        trusted_source_directories: tuple[Path, ...] = (),
        trusted_source_metadata_root: Path | None = None,
        allow_scratch_writes: bool = True,
        sandbox_executable: Path = DARWIN_SANDBOX_EXECUTABLE,
        platform: str = sys.platform,
    ) -> None:
        if platform != "darwin":
            raise RuntimeError(
                "Controlled-local Python requires an approved OS isolator; "
                "only Darwin Seatbelt is currently approved"
            )
        if not (sandbox_executable.is_file() and os.access(sandbox_executable, os.X_OK)):
            raise RuntimeError("Darwin Seatbelt sandbox-exec is unavailable")

        declared = Path(python_executable)
        resolved = declared.resolve(strict=True)
        self._sandbox_executable = sandbox_executable
        self._allow_scratch_writes = allow_scratch_writes
        self._python_executable = str(declared)
        self._resolved_python_executable = resolved
        self._runtime_roots = tuple(
            dict.fromkeys((declared.parent.parent.absolute(), resolved.parent.parent))
        )
        self._trusted_sources = tuple(path.resolve(strict=True) for path in trusted_source_files)
        self._trusted_source_directories = tuple(
            path.resolve(strict=True) for path in trusted_source_directories
        )
        self._trusted_source_metadata_root = (
            trusted_source_metadata_root.resolve(strict=True)
            if trusted_source_metadata_root is not None
            else None
        )

    def command(
        self, *, scratch: Path, child_args: tuple[str, ...],
        metadata_read_fds: tuple[int, ...] = (),
    ) -> tuple[str, ...]:
        """Return the sandbox-exec command for one private call scratch directory."""

        return (
            str(self._sandbox_executable),
            "-p",
            self.profile(scratch=scratch, metadata_read_fds=metadata_read_fds),
            self._python_executable,
            *child_args,
        )

    def profile(self, *, scratch: Path, metadata_read_fds: tuple[int, ...] = ()) -> str:
        """Render the minimal deterministic SBPL profile for one worker call."""

        scratch = scratch.resolve(strict=True)
        runtime_filters = " ".join(
            f"(subpath {_sbpl_string(root)})" for root in self._runtime_roots
        )
        source_literals = " ".join(
            f"(literal {_sbpl_string(path)})" for path in self._trusted_sources
        )
        source_directory_literals = " ".join(
            f"(literal {_sbpl_string(path)})" for path in self._trusted_source_directories
        )
        system_read_denies = " ".join(
            f"(literal {_sbpl_string(path)})" for path in _SYSTEM_PROFILE_READ_DENIES
        )
        system_write_denies = " ".join(
            f"(subpath {_sbpl_string(path)})"
            if path == "/cores"
            else f"(literal {_sbpl_string(path)})"
            for path in _SYSTEM_PROFILE_WRITE_DENIES
        )
        lines = [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process-exec (require-all "
            f"(literal {_sbpl_string(self._resolved_python_executable)}) "
            "(process-attribute is-platform-binary)))",
            "(deny process-fork)",
            f"(allow file-read* {runtime_filters})",
        ]
        for descriptor in metadata_read_fds:
            if not isinstance(descriptor, int) or isinstance(descriptor, bool) or descriptor < 0:
                raise ValueError("metadata descriptor must be a non-negative integer")
            # Native readers may stat an already inherited descriptor. This
            # grants no data or directory access beyond that exact capability.
            lines.append(
                f'(allow file-read-metadata (literal "/dev/fd/{descriptor}"))'
            )
        if self._trusted_sources:
            if self._trusted_source_metadata_root is None:
                raise RuntimeError("trusted source files require a metadata root")
            lines.extend(
                (
                    "(allow file-read-metadata "
                    f"(subpath {_sbpl_string(self._trusted_source_metadata_root)}))",
                    f"(allow file-read* {source_directory_literals})",
                    f"(allow file-read* {source_literals})",
                )
            )
        lines.extend(
            (
                f"(allow file-read* {'file-write* ' if self._allow_scratch_writes else ''}(subpath {_sbpl_string(scratch)}))",
                "(deny network*)",
                f"(deny file-read* {system_read_denies})",
                f"(deny file-write* {system_write_denies})",
            )
        )
        return "\n".join(lines)


__all__ = ["DARWIN_SANDBOX_EXECUTABLE", "DarwinSeatbeltSandbox"]
