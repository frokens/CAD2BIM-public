"""Bounded in-memory imports for exact multi-module Python source bundles."""

from __future__ import annotations

import importlib.util
import ast
import types
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Collection

from CAD2BIM_backend.infrastructure.source_text.source_bundle import (
    SourceBundle,
    SourceBundleError,
    inspect_source_bundle,
)
from CAD2BIM_backend.infrastructure.worker_runtime.python_user_code import (
    PythonUserCodeValidationError,
    RestrictedImportGuard,
    compile_user_module,
    compile_modeling_module,
    safe_builtins,
)


class PythonBundleValidationError(RuntimeError):
    """An exact source bundle cannot be compiled under a bounded profile."""

    def __init__(self, message: str, *, diagnostic: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


@dataclass(frozen=True, slots=True)
class CompiledPythonBundle:
    package_name: str
    entry_name: str
    source: SourceBundle
    compiled: dict[str, Any]
    sources: dict[str, str]
    entry_names: tuple[str, ...]
    controlled_modeling: bool = False
    entry_path: str = "main.py"

    def load_entry(self, import_guard: RestrictedImportGuard, *, execution_globals: dict[str, Any] | None = None, entry_name: str | None = None) -> Any:
        entry_name = self.entry_name if entry_name is None else entry_name
        if entry_name not in self.entry_names:
            raise PythonBundleValidationError("requested entry was not validated in this bundle")
        if self.controlled_modeling != (execution_globals is not None):
            raise PythonBundleValidationError("bundle and execution capability profiles disagree")
        importer = _MemoryModuleImporter(
            package_name=self.package_name,
            compiled=self.compiled,
            external=import_guard,
            execution_globals=execution_globals,
        )
        module = importer.load(_module_name(self.package_name, self.entry_path))
        entry = getattr(module, entry_name, None)
        if not callable(entry):
            raise PythonBundleValidationError(
                f"{entry_name}(ctx) entry point is unavailable"
            )
        return entry


def compile_python_bundle(
    content: bytes,
    *,
    package_name: str,
    entry_name: str,
    required_entry_names: Collection[str] | None = None,
    allowed_import_roots: Collection[str],
    profile_label: str,
    maximum_members: int,
    maximum_member_bytes: int,
    maximum_total_bytes: int,
    reject_private_attributes: bool = False,
    controlled_modeling: bool = False,
    controlled_import_roots: Collection[str] = ("math", "cadquery"),
    entry_path: str = "main.py",
    reachable_only: bool = False,
) -> CompiledPythonBundle:
    """Compile one text-only bundle without materializing user modules on disk."""

    if not package_name.isidentifier() or not entry_name.isidentifier():
        raise ValueError("Python bundle package and entry names must be identifiers")
    entry_names = tuple(required_entry_names) if required_entry_names is not None else (entry_name,)
    if (not entry_names or entry_name not in entry_names
        or any(not isinstance(name, str) or not name.isidentifier() for name in entry_names)
        or len(set(entry_names)) != len(entry_names)):
        raise ValueError("Python bundle entries must be unique identifiers including the default entry")
    try:
        bundle = inspect_source_bundle(content)
    except SourceBundleError as exc:
        raise PythonBundleValidationError(f"{profile_label} source bundle is invalid") from exc
    if not 1 <= len(bundle.members) <= maximum_members:
        raise PythonBundleValidationError(
            f"{profile_label} accepts 1..{maximum_members} Python files"
        )
    if not any(member.path == entry_path for member in bundle.members):
        raise PythonBundleValidationError(f"{profile_label} requires {entry_path}")
    if sum(member.size_bytes for member in bundle.members) > maximum_total_bytes:
        raise PythonBundleValidationError(f"{profile_label} source exceeds its total byte budget")

    compiled: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for member in bundle.members:
        if (
            not member.path.endswith(".py")
            or member.size_bytes > maximum_member_bytes
            or not member.text_available
        ):
            raise PythonBundleValidationError(
                f"{profile_label} accepts only bounded UTF-8 Python files"
            )
        _module_name(package_name, member.path)
        source = bundle.read_text(member.path)
        sources[member.path] = source
    reachable = _reachable_module_paths(package_name, sources, entry_path) if reachable_only else set(sources)
    for path, source in sources.items():
        if path not in reachable:
            continue
        module_name = _module_name(package_name, path)
        # The controlled compiler already rejects every private attribute in its
        # one AST pass; do not parse the same module a second time for that rule.
        if reject_private_attributes and not controlled_modeling:
            _reject_private_attribute_access(source, path, profile_label)
        try:
            if controlled_modeling:
                compiled[module_name] = compile_modeling_module(
                    source, filename=f"<cad2bim-bundle:{path}>",
                    required_entry_names=entry_names if path == entry_path else (),
                    allowed_import_roots=controlled_import_roots,
                )
                continue
            compiled[module_name] = compile_user_module(
                source,
                filename=f"<cad2bim-bundle:{path}>",
                required_entry_names=entry_names if path == entry_path else (),
                allowed_import_roots=allowed_import_roots,
                allowed_context_io_attributes=(),
                profile_label=profile_label,
                allow_relative_imports=True,
            )
        except PythonUserCodeValidationError as exc:
            diagnostic = dict(exc.diagnostic)
            diagnostic["source_path"] = path
            raise PythonBundleValidationError(
                f"{profile_label} source is invalid",
                diagnostic=diagnostic,
            ) from exc
        except Exception as exc:
            raise PythonBundleValidationError(str(exc)) from exc
    return CompiledPythonBundle(
        package_name=package_name,
        entry_name=entry_name,
        source=bundle,
        compiled=compiled,
        sources=sources,
        entry_names=entry_names,
        controlled_modeling=controlled_modeling,
        entry_path=entry_path,
    )


def _reachable_module_paths(package_name, sources, entry_path):
    """Resolve static relative helper imports before applying the numeric profile."""
    modules = {_module_name(package_name, path): path for path in sources}
    pending, reachable = [entry_path], set()
    while pending:
        path = pending.pop()
        if path in reachable:
            continue
        if path == "main.py":
            raise PythonBundleValidationError("Geometry interface helpers cannot import main.py")
        reachable.add(path)
        module = _module_name(package_name, path)
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        try:
            tree = ast.parse(sources[path])
        except SyntaxError as exc:
            raise PythonBundleValidationError("interface source is invalid", diagnostic={"source_path": path, "line": exc.lineno}) from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            try:
                target = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            except (ValueError, ImportError) as exc:
                raise PythonBundleValidationError("relative import escapes the source bundle") from exc
            if target != package_name and not target.startswith(package_name + "."):
                raise PythonBundleValidationError("relative import escapes the source bundle")
            candidates = [target, *(target + "." + alias.name for alias in node.names)]
            for candidate in candidates:
                if candidate in modules:
                    pending.append(modules[candidate])
    return reachable


def _reject_private_attribute_access(source: str, path: str, profile_label: str) -> None:
    try:
        tree = ast.parse(source, filename=f"<cad2bim-bundle:{path}>", mode="exec")
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise PythonBundleValidationError(
                f"{profile_label} cannot access private runtime attributes",
                diagnostic={"source_path": path},
            )


def _module_name(package_name: str, path: str) -> str:
    source_path = PurePosixPath(path)
    parts = list(source_path.parts)
    filename = parts.pop()
    stem = filename[:-3]
    if stem == "__init__":
        if not parts:
            raise PythonBundleValidationError("root __init__.py is not supported")
    else:
        parts.append(stem)
    if any(not part.isidentifier() or part.startswith("_") for part in parts):
        raise PythonBundleValidationError(
            "Python module paths must use public identifier components"
        )
    return ".".join((package_name, *parts))


class _MemoryModuleImporter:
    def __init__(
        self,
        *,
        package_name: str,
        compiled: dict[str, Any],
        external: RestrictedImportGuard,
        execution_globals: dict[str, Any] | None = None,
    ) -> None:
        self._package_name = package_name
        self._compiled = compiled
        self._external = external
        self._globals = dict(execution_globals or {})
        self._register_module = self._globals.pop("_register_module_", None)
        self._builtins = dict(self._globals.pop("__builtins__")) if execution_globals is not None else safe_builtins(external)
        self._modules: dict[str, types.ModuleType] = {}
        root = types.ModuleType(package_name)
        root.__package__ = package_name
        root.__path__ = []
        self._modules[package_name] = root
        if self._register_module is not None:
            self._register_module(root)
        self._builtins["__import__"] = self

    def __call__(
        self,
        name: str,
        globals_: dict[str, Any] | None = None,
        locals_: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if level:
            package = str((globals_ or {}).get("__package__", ""))
            try:
                target = importlib.util.resolve_name("." * level + name, package)
            except (ImportError, ValueError) as exc:
                raise ImportError("relative import escapes the source bundle") from exc
            if not (target == self._package_name or target.startswith(self._package_name + ".")):
                raise ImportError("relative import escapes the source bundle")
            module = self.load(target)
            for requested in fromlist:
                child = f"{target}.{requested}"
                if requested != "*" and child in self._compiled:
                    self.load(child)
            return module
        if name == self._package_name or name.startswith(self._package_name + "."):
            module = self.load(name)
            return module if fromlist else self._modules[self._package_name]
        return self._external(name, globals_, locals_, fromlist, level)

    def load(self, name: str) -> types.ModuleType:
        existing = self._modules.get(name)
        if existing is not None:
            return existing
        code = self._compiled.get(name)
        if code is None:
            raise ImportError("relative module is not present in the source bundle")
        package = name.rpartition(".")[0]
        parent = self.load(package)
        module = types.ModuleType(name)
        module.__package__ = name if name in self._package_names else package
        if name in self._package_names:
            module.__path__ = []
        module.__dict__["__builtins__"] = self._builtins
        module.__dict__.update(self._globals)
        if self._register_module is not None:
            self._register_module(module)
        self._modules[name] = module
        setattr(parent, name.rsplit(".", 1)[1], module)
        try:
            exec(code, module.__dict__)
        except BaseException:
            self._modules.pop(name, None)
            raise
        return module

    @property
    def _package_names(self) -> frozenset[str]:
        return frozenset(
            name
            for name in self._compiled
            if any(candidate.startswith(name + ".") for candidate in self._compiled)
        )


__all__ = [
    "CompiledPythonBundle",
    "PythonBundleValidationError",
    "compile_python_bundle",
]
