"""Explicit startup configuration for the local Direct MCP profile."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.identity import ConfiguredLocalSubject
from CAD2BIM_backend.infrastructure.data_transform.python_worker_environment import (
    PythonWorkerEnvironmentAttestation,
    load_attestation,
)


_M2A_MAX_OUTPUT_PARQUET_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DirectMcpConfig:
    """Configuration that cannot be supplied by an MCP tool call."""

    subject: ConfiguredLocalSubject
    database_url: str = field(repr=False)
    data_query_cursor_key: str = field(repr=False)
    python_worker_executable: Path = field(repr=False)
    python_worker_attestation: PythonWorkerEnvironmentAttestation = field(repr=False)
    storage_root: Path = Path("storage/v2")
    max_upload_bytes: int = 100 * 1024 * 1024
    ifc_docs_proxy: str | None = field(default=None, repr=False)
    host_python_max_concurrent_workers: int = 1

    def __post_init__(self) -> None:
        if (type(self.host_python_max_concurrent_workers) is not int
                or not 1 <= self.host_python_max_concurrent_workers <= 128):
            raise ValueError("Host Python worker capacity must be between 1 and 128")

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "DirectMcpConfig":
        values = dict(os.environ if environ is None else environ)
        if values.get("CAD2BIM_ENV_FILE", "").strip():
            raise ValueError("Direct MCP does not read CAD2BIM_ENV_FILE")

        raw_subject = values.get("CAD2BIM_DIRECT_SUBJECT", "").strip()
        if not raw_subject:
            raise ValueError("CAD2BIM_DIRECT_SUBJECT is required")
        database_url = values.get("CAD2BIM_DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError("CAD2BIM_DATABASE_URL is required for Direct MCP")
        data_query_cursor_key = values.get("CAD2BIM_DATA_QUERY_CURSOR_KEY", "").strip()
        if not data_query_cursor_key:
            raise ValueError("CAD2BIM_DATA_QUERY_CURSOR_KEY is required for Direct MCP")
        raw_python_worker = values.get("CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE", "").strip()
        if not raw_python_worker:
            raise ValueError("CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE is required for Direct MCP")
        python_worker_executable = Path(raw_python_worker).expanduser()
        if not python_worker_executable.is_absolute():
            raise ValueError("CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE must be an absolute path")
        if not python_worker_executable.is_file() or not os.access(
            python_worker_executable, os.X_OK
        ):
            raise ValueError(
                "CAD2BIM_DATA_PYTHON_WORKER_EXECUTABLE must be an existing executable file"
            )
        try:
            python_worker_attestation = load_attestation(python_worker_executable)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

        max_upload_bytes = int(values.get("CAD2BIM_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
        if max_upload_bytes < _M2A_MAX_OUTPUT_PARQUET_BYTES:
            raise ValueError("CAD2BIM_MAX_UPLOAD_BYTES must be at least 104857600 bytes for M2a")
        storage_root = Path(values.get("CAD2BIM_STORAGE_ROOT", "storage/v2")).expanduser()
        ifc_docs_proxy = values.get("CAD2BIM_IFC_DOCS_PROXY", "").strip() or None
        if ifc_docs_proxy is not None:
            try:
                parsed_proxy = urlsplit(ifc_docs_proxy)
                _ = parsed_proxy.port
            except ValueError:
                raise ValueError("CAD2BIM_IFC_DOCS_PROXY must be a valid HTTP(S) proxy URL") from None
            if (parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.hostname
                    or parsed_proxy.query or parsed_proxy.fragment or parsed_proxy.path not in {"", "/"}):
                raise ValueError("CAD2BIM_IFC_DOCS_PROXY must be a valid HTTP(S) proxy URL")
        return cls(
            subject=ConfiguredLocalSubject(subject=raw_subject),
            database_url=database_url,
            data_query_cursor_key=data_query_cursor_key,
            python_worker_executable=python_worker_executable,
            python_worker_attestation=python_worker_attestation,
            storage_root=storage_root,
            max_upload_bytes=max_upload_bytes,
            ifc_docs_proxy=ifc_docs_proxy,
            host_python_max_concurrent_workers=int(
                values.get("CAD2BIM_HOST_PYTHON_MAX_CONCURRENT_WORKERS", "1")
            ),
        )

    def backend_settings(self) -> Settings:
        """Build backend settings directly, without consulting dotenv files."""

        return Settings(
            environment="production",
            database_url=self.database_url,
            storage_root=self.storage_root,
            max_upload_bytes=self.max_upload_bytes,
        )


__all__ = ["DirectMcpConfig"]
