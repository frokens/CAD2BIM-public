"""Shared mechanisms for controlled local worker adapters.

Imports stay lazy so a sandboxed child can load the small protocol module
without importing parent-only process supervision dependencies.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "DarwinSeatbeltSandbox",
    "HostWorkerCapacity",
    "MAX_CONTROL_BYTES",
    "MAX_STATUS_BYTES",
    "DEFAULT_BLOCKED_IO_ATTRIBUTES",
    "PythonUserCodePreflightError",
    "PythonUserCodeValidationError",
    "RestrictedImportGuard",
    "WorkerCapacityBusy",
    "WorkerCapacityUnavailable",
    "WorkerLaunchSpec",
    "WorkerProcessBudget",
    "WorkerProcessObservations",
    "WorkerProcessSupervisor",
    "WorkerProtocolError",
    "WorkerSupervisorError",
    "WorkerTerminal",
    "compile_entry",
    "compile_user_module",
    "diagnostic_json",
    "install_readonly_audit_hook",
    "open_binary_fd",
    "read_json_frame",
    "runtime_code_diagnostic",
    "safe_builtins",
    "write_json_frame",
]


_EXPORT_MODULES = {
    "DarwinSeatbeltSandbox": ".darwin_seatbelt",
    "HostWorkerCapacity": ".capacity",
    "MAX_CONTROL_BYTES": ".protocol",
    "MAX_STATUS_BYTES": ".protocol",
    "DEFAULT_BLOCKED_IO_ATTRIBUTES": ".python_user_code",
    "PythonUserCodePreflightError": ".python_user_code",
    "PythonUserCodeValidationError": ".python_user_code",
    "RestrictedImportGuard": ".python_user_code",
    "WorkerCapacityBusy": ".capacity",
    "WorkerCapacityUnavailable": ".capacity",
    "WorkerLaunchSpec": ".supervisor",
    "WorkerProcessBudget": ".supervisor",
    "WorkerProcessObservations": ".supervisor",
    "WorkerProcessSupervisor": ".supervisor",
    "WorkerProtocolError": ".protocol",
    "WorkerSupervisorError": ".supervisor",
    "WorkerTerminal": ".supervisor",
    "compile_entry": ".python_user_code",
    "compile_user_module": ".python_user_code",
    "diagnostic_json": ".python_user_code",
    "install_readonly_audit_hook": ".python_user_code",
    "open_binary_fd": ".protocol",
    "read_json_frame": ".protocol",
    "runtime_code_diagnostic": ".python_user_code",
    "safe_builtins": ".python_user_code",
    "write_json_frame": ".protocol",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    return getattr(import_module(module_name, __name__), name)
