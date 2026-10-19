"""Neutral defense-in-depth policy for controlled Python user code."""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from types import CodeType
from typing import Any, Collection, Mapping


DEFAULT_BLOCKED_IO_ATTRIBUTES = frozenset(
    {
        "AzureFileSystem",
        "FileSystem",
        "GcsFileSystem",
        "HadoopFileSystem",
        "LocalFileSystem",
        "NativeFile",
        "OSFile",
        "ParquetDataset",
        "ParquetFile",
        "S3FileSystem",
        "SubTreeFileSystem",
        "connect_input_stream",
        "connect_output_stream",
        "download",
        "from_csv_auto",
        "from_uri",
        "genfromtxt",
        "input_stream",
        "load",
        "loadtxt",
        "memory_map",
        "memmap",
        "open_input_file",
        "open_input_stream",
        "open_output_stream",
        "output_stream",
        "read_csv",
        "read_excel",
        "read_feather",
        "read_fwf",
        "read_html",
        "read_ipc",
        "read_json",
        "read_metadata",
        "read_orc",
        "read_pandas",
        "read_parquet",
        "read_pickle",
        "read_sas",
        "read_schema",
        "read_sql",
        "read_sql_query",
        "read_sql_table",
        "read_stata",
        "read_table",
        "read_xml",
        "save",
        "savetxt",
        "savez",
        "savez_compressed",
        "scan_csv",
        "scan_ipc",
        "scan_ndjson",
        "scan_parquet",
        "to_csv",
        "to_excel",
        "to_feather",
        "to_html",
        "to_ipc",
        "to_json",
        "to_orc",
        "to_parquet",
        "to_pickle",
        "to_sql",
        "to_stata",
        "to_xml",
        "write_csv",
        "write_dataset",
        "write_feather",
        "write_ipc",
        "write_json",
        "write_metadata",
        "write_ndjson",
        "write_parquet",
        "write_table",
    }
)

_BLOCKED_CALLS = frozenset({"__import__", "breakpoint", "compile", "eval", "exec", "input", "open"})
_MAX_CODE_DIAGNOSTIC_BYTES = 1_600
_SAFE_BUILTIN_NAMES = (
    "ArithmeticError",
    "AssertionError",
    "BaseException",
    "Exception",
    "IndexError",
    "KeyError",
    "NotImplementedError",
    "RuntimeError",
    "StopIteration",
    "TypeError",
    "ValueError",
    "__build_class__",
    "abs",
    "all",
    "any",
    "ascii",
    "bin",
    "bool",
    "bytearray",
    "bytes",
    "callable",
    "chr",
    "complex",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "hash",
    "hex",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "memoryview",
    "min",
    "next",
    "object",
    "oct",
    "ord",
    "pow",
    "print",
    "property",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "super",
    "tuple",
    "type",
    "zip",
)


class PythonUserCodePreflightError(RuntimeError):
    pass


class PythonUserCodeValidationError(PythonUserCodePreflightError):
    def __init__(self, diagnostic: dict[str, Any]) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic_json(diagnostic))


def diagnostic_json(diagnostic: dict[str, Any]) -> str:
    """Serialize one actionable diagnostic without risking outer error truncation."""

    bounded = dict(diagnostic)
    for field, limit in (("message", 400), ("source_line", 640), ("caret", 320)):
        value = bounded.get(field)
        if not isinstance(value, str):
            continue
        bounded[field], truncated = _truncate_utf8(value, limit)
        if truncated:
            bounded[f"{field}_truncated"] = True
    rendered = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    while len(rendered.encode("utf-8")) > _MAX_CODE_DIAGNOSTIC_BYTES:
        candidates = [
            field
            for field in ("source_line", "caret", "message")
            if isinstance(bounded.get(field), str) and len(str(bounded[field]).encode("utf-8")) > 32
        ]
        if not candidates:
            break
        field = max(candidates, key=lambda item: len(str(bounded[item]).encode("utf-8")))
        current = str(bounded[field])
        bounded[field], _ = _truncate_utf8(
            current,
            max(32, len(current.encode("utf-8")) // 2),
        )
        bounded[f"{field}_truncated"] = True
        rendered = json.dumps(
            bounded,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return rendered


def runtime_code_diagnostic(
    code: str,
    entry_name: str,
    exc: BaseException,
    *,
    virtual_sources: Mapping[str, str] | None = None,
    virtual_filename_prefix: str | None = None,
) -> dict[str, Any]:
    """Return the innermost user-code frame without exposing a host path."""

    expected_filename = f"<cad2bim-{entry_name}>"
    selected = None
    selected_source = code
    selected_source_path: str | None = None
    traceback = exc.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename
        if filename == expected_filename:
            selected = traceback
            selected_source = code
            selected_source_path = None
        elif (
            virtual_sources is not None
            and virtual_filename_prefix is not None
            and filename.startswith(virtual_filename_prefix)
            and filename.endswith(">")
        ):
            source_path = filename[len(virtual_filename_prefix) : -1]
            source = virtual_sources.get(source_path)
            if source is not None:
                selected = traceback
                selected_source = source
                selected_source_path = source_path
        traceback = traceback.tb_next
    line = selected.tb_lineno if selected is not None else None
    source_line, _caret = _source_location(
        selected_source,
        line=line,
        column=None,
        end_column=None,
    )
    diagnostic: dict[str, Any] = {
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "line": line,
        "source_line": source_line,
        "code_sha256": hashlib.sha256(selected_source.encode("utf-8")).hexdigest(),
    }
    if selected is not None:
        diagnostic["function"] = selected.tb_frame.f_code.co_name
    if selected_source_path is not None:
        diagnostic["source_path"] = selected_source_path
    return diagnostic


def compile_entry(
    source: str,
    entry_name: str,
    *,
    allowed_import_roots: Collection[str],
    blocked_io_attributes: Collection[str] = DEFAULT_BLOCKED_IO_ATTRIBUTES,
    allowed_context_io_attributes: Collection[str] = ("write_table", "write_json"),
    profile_label: str = "Transformation",
    allow_relative_imports: bool = False,
) -> CodeType:
    """Validate and compile one exact ``entry_name(ctx)`` source unit."""

    if not entry_name or not entry_name.isidentifier():
        raise PythonUserCodePreflightError("Python worker mode is invalid")
    return compile_user_module(
        source,
        filename=f"<cad2bim-{entry_name}>",
        required_entry_name=entry_name,
        allowed_import_roots=allowed_import_roots,
        blocked_io_attributes=blocked_io_attributes,
        allowed_context_io_attributes=allowed_context_io_attributes,
        profile_label=profile_label,
        allow_relative_imports=allow_relative_imports,
    )


def _required_entries(name: str | None, names: Collection[str] | None) -> tuple[str, ...]:
    if name is not None and names is not None:
        raise PythonUserCodePreflightError("Python worker entry declarations are ambiguous")
    entries = tuple(names) if names is not None else (() if name is None else (name,))
    if (any(not isinstance(entry, str) or not entry.isidentifier() for entry in entries)
        or len(set(entries)) != len(entries)):
        raise PythonUserCodePreflightError("Python worker entries must be unique identifiers")
    return entries


def compile_user_module(
    source: str,
    *,
    filename: str,
    allowed_import_roots: Collection[str],
    required_entry_name: str | None = None,
    required_entry_names: Collection[str] | None = None,
    blocked_io_attributes: Collection[str] = DEFAULT_BLOCKED_IO_ATTRIBUTES,
    allowed_context_io_attributes: Collection[str] = (),
    profile_label: str = "Python",
    allow_relative_imports: bool = False,
) -> CodeType:
    """Validate one controlled module, optionally requiring ``entry(ctx)``."""

    required_entries = _required_entries(required_entry_name, required_entry_names)
    allowed_roots = frozenset(allowed_import_roots)
    blocked_attributes = frozenset(blocked_io_attributes)
    context_io_attributes = frozenset(allowed_context_io_attributes)
    try:
        tree = ast.parse(source, filename=filename, mode="exec")
    except SyntaxError as exc:
        raise PythonUserCodeValidationError(
            _code_diagnostic(
                source,
                exception_type=type(exc).__name__,
                message=exc.msg,
                line=exc.lineno,
                column=exc.offset,
                end_line=exc.end_lineno,
                end_column=exc.end_offset,
            )
        ) from None

    for required_entry_name in required_entries:
        entries = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == required_entry_name
        ]
        if len(entries) != 1 or not isinstance(entries[0], ast.FunctionDef):
            node = entries[0] if entries else None
            raise _entry_error(
                source,
                f"Source must define one top-level def {required_entry_name}(ctx)",
                node,
            )
        entry = entries[0]
        arguments = entry.args
        if (
            len(arguments.posonlyargs) + len(arguments.args) != 1
            or arguments.vararg is not None
            or arguments.kwarg is not None
            or arguments.kwonlyargs
            or arguments.defaults
            or arguments.kw_defaults
            or entry.decorator_list
        ):
            raise _entry_error(
                source,
                f"{required_entry_name} must have the exact signature {required_entry_name}(ctx)",
                entry,
            )
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is not None and _literal_expression(value):
                continue
        raise PythonUserCodePreflightError(
            f"{profile_label} top level may contain imports, functions, and literal constants only"
        )
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.level:
                if not allow_relative_imports:
                    raise _import_error(
                        source, node,
                        f"{profile_label} imports an unavailable module: relative imports "
                        "are unavailable in this execution profile",
                    )
                if any(alias.name == "*" for alias in node.names):
                    raise PythonUserCodePreflightError("Wildcard imports are not allowed")
                continue
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            unavailable = next(
                (name for name in names if name.split(".", 1)[0] not in allowed_roots), None
            )
            if unavailable is not None:
                hint = (
                    " For a private source-bundle member use a relative import, "
                    "for example: from .helpers import make_shape."
                    if allow_relative_imports else ""
                )
                raise _import_error(
                    source, node,
                    f"{profile_label} imports an unavailable module: {unavailable[:120]}." + hint,
                )
            if any(alias.name == "*" for alias in node.names):
                raise PythonUserCodePreflightError("Wildcard imports are not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise PythonUserCodePreflightError(
                f"{profile_label} requests a blocked runtime capability"
            )
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALLS:
                raise PythonUserCodePreflightError(
                    f"{profile_label} requests a blocked runtime capability"
                )
            if (
                isinstance(node.func, ast.Name)
                and node.func.id in required_entries
            ):
                raise PythonUserCodePreflightError(
                    f"The Host is the only caller of {node.func.id}(ctx)"
                )
            if isinstance(node.func, ast.Attribute) and node.func.attr in blocked_attributes:
                is_context_submission = (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "ctx"
                    and node.func.attr in context_io_attributes
                )
                if not is_context_submission:
                    raise PythonUserCodePreflightError(
                        f"{profile_label} requests a path-based or external I/O capability"
                    )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise PythonUserCodePreflightError(
                f"{profile_label} cannot mutate module or closure state"
            )
    return compile(tree, filename, "exec", dont_inherit=True)


def compile_modeling_module(
    source: str, *, filename: str, required_entry_name: str | None = None,
    required_entry_names: Collection[str] | None = None,
    allowed_import_roots: Collection[str] = ("math", "cadquery"),
) -> CodeType:
    """Positive Python syntax profile; capabilities are supplied by the modeling owner.

    RestrictedPython supplies the bytecode rewriting, not the object policy or
    OS isolation. Other workers continue to use ``compile_user_module``.
    """
    from RestrictedPython import compile_restricted_exec
    from RestrictedPython.transformer import RestrictingNodeTransformer

    required_entries = _required_entries(required_entry_name, required_entry_names)
    allowed_import_roots = frozenset(allowed_import_roots)
    if not allowed_import_roots.issubset({"math", "cadquery"}):
        raise ValueError("controlled modeling imports must be an audited subset")
    allowed = {
        ast.Module, ast.Import, ast.ImportFrom, ast.alias, ast.FunctionDef,
        ast.arguments, ast.arg, ast.Lambda, ast.Return, ast.Assign, ast.AugAssign,
        ast.Expr, ast.Pass, ast.If, ast.IfExp, ast.For, ast.While, ast.Break,
        ast.Continue, ast.Try, ast.ExceptHandler, ast.Raise, ast.Assert,
        ast.Name, ast.Constant, ast.Attribute, ast.Subscript, ast.Slice,
        ast.List, ast.Tuple, ast.Dict, ast.Set, ast.ListComp, ast.DictComp,
        ast.SetComp, ast.GeneratorExp, ast.comprehension, ast.Call, ast.keyword,
        ast.Starred, ast.UnaryOp, ast.BinOp, ast.BoolOp, ast.Compare,
        ast.JoinedStr, ast.FormattedValue, ast.Load, ast.Store, ast.Del,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq,
        ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn,
    }
    reserved = {
        "_getattr_", "_getitem_", "_getiter_", "_write_", "_apply_",
        "_iter_unpack_sequence_", "_unpack_sequence_", "_inplacevar_",
        "_print_", "_print", "printed", "__builtins__", "__import__",
    }

    def reject(node: ast.AST, message: str) -> None:
        raise PythonUserCodeValidationError(_code_diagnostic(
            source, exception_type="ModelingCapabilityError", message=message,
            line=getattr(node, "lineno", None), column=getattr(node, "col_offset", 0) + 1,
            end_line=getattr(node, "end_lineno", None), end_column=getattr(node, "end_col_offset", None),
        ))

    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise PythonUserCodeValidationError(_code_diagnostic(
            source, exception_type="SyntaxError", message=exc.msg, line=exc.lineno,
            column=exc.offset, end_line=exc.end_lineno, end_column=exc.end_offset,
        )) from None
    # Explicit relative imports do not consume __all__. Preserve this common
    # inert export declaration in source provenance without executing it or
    # granting access to other reserved Python/module names.
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "__all__":
            if not isinstance(node.value, (ast.List, ast.Tuple)) or any(
                not isinstance(item, ast.Constant) or type(item.value) is not str or not item.value.isidentifier()
                for item in node.value.elts
            ):
                reject(node, "__all__ must be an inert list of exported helper names")
            continue
        body.append(node)
    tree.body = body
    for node in ast.walk(tree):
        if type(node) not in allowed:
            reject(node, f"unsupported modeling syntax: {type(node).__name__}")
        if isinstance(node, ast.Constant) and type(node.value) not in {type(None), bool, int, float, str}:
            reject(node, "modeling literals must be null, boolean, number or text")
        if isinstance(node, ast.Name) and (node.id in reserved or "__" in node.id or (node.id != "_" and node.id.startswith("_") and node.id.endswith("_"))):
            reject(node, "reserved execution name is unavailable")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or not isinstance(node.ctx, ast.Load):
                reject(node, "private attributes and attribute writes are unavailable; use declared local operations")
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            if isinstance(node, ast.FunctionDef) and (node.decorator_list or node.returns is not None):
                reject(node, "modeling helpers do not accept decorators or executable annotations")
            args = node.args
            all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            all_args += [a for a in (args.vararg, args.kwarg) if a is not None]
            if any(a.annotation is not None for a in all_args):
                reject(node, "modeling helpers do not accept executable annotations")
            for value in [*args.defaults, *(v for v in args.kw_defaults if v is not None)]:
                if not _literal_expression(value):
                    reject(value, "helper defaults must be literal local values")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            relative = isinstance(node, ast.ImportFrom) and node.level > 0
            if any(n == "*" or (n.startswith("_") and not relative) or "__" in n for n in names):
                reject(node, "private or wildcard imports are unavailable")
            if isinstance(node, ast.Import):
                if any(n not in allowed_import_roots for n in names):
                    reject(node, "only imports declared by this controlled execution profile are available")
            elif not node.level and node.module not in allowed_import_roots:
                reject(node, "native modules are unavailable; use a relative helper or declared capability")
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets) and _literal_expression(node.value):
            continue
        reject(node, "module loading permits imports, helpers and literal local constants only")
    for required_entry_name in required_entries:
        entries = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == required_entry_name]
        if len(entries) != 1:
            reject(tree, f"define exactly one {required_entry_name}(ctx)")
        args = entries[0].args
        if len(args.posonlyargs) + len(args.args) != 1 or args.vararg or args.kwarg or args.kwonlyargs or args.defaults:
            reject(entries[0], f"entry must have signature {required_entry_name}(ctx)")

    class ModelingTransformer(RestrictingNodeTransformer):
        def check_name(self, node, name, allow_magic_methods=False):
            # Existing recipes use private local helper names. They are not
            # private *attributes*. Never permit an injected guard to shadow.
            if name is not None and (name in reserved or "__" in name or (name != "_" and name.startswith("_") and name.endswith("_"))):
                reject(node, "reserved execution name is unavailable")
            else:
                super().check_name(node, name.lstrip("_") if name and name != "_" else name,
                                   allow_magic_methods=False)

    result = compile_restricted_exec(tree, filename=filename, policy=ModelingTransformer)
    if result.errors or result.code is None:
        reject(tree, "; ".join(result.errors))
    return result.code


class RestrictedImportGuard:
    def __init__(
        self,
        *,
        allowed_import_roots: Collection[str],
        policy: dict[str, Any] | None = None,
        worker_label: str = "Python/Arrow worker",
    ) -> None:
        self._allowed_import_roots = frozenset(allowed_import_roots)
        self._local = threading.local()
        self._original = builtins.__import__
        self._policy = policy or {}
        self._worker_label = worker_label
        self._duckdb_hardened = False

    @property
    def active(self) -> bool:
        return bool(getattr(self._local, "depth", 0))

    @property
    def allowed_import_roots(self) -> frozenset[str]:
        return self._allowed_import_roots

    @property
    def worker_label(self) -> str:
        return self._worker_label

    def __call__(
        self,
        name: str,
        globals_: dict[str, Any] | None = None,
        locals_: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        caller = str((globals_ or {}).get("__name__", ""))
        root = name.split(".", 1)[0]
        caller_root = caller.split(".", 1)[0]
        if (
            level == 0
            and root not in self._allowed_import_roots
            and caller_root not in self._allowed_import_roots
        ):
            raise ImportError(f"module {root!r} is not available in the {self._worker_label}")
        self._local.depth = int(getattr(self._local, "depth", 0)) + 1
        try:
            module = self._original(name, globals_, locals_, fromlist, level)
            if root == "duckdb" and not self._duckdb_hardened:
                self._harden_duckdb()
            return module
        finally:
            self._local.depth -= 1

    def _harden_duckdb(self) -> None:
        module = sys.modules.get("duckdb")
        if module is None:
            raise ImportError("DuckDB could not be loaded in the controlled runtime")
        required = {"threads", "rss_bytes", "scratch_bytes"}
        if not required.issubset(self._policy):
            raise ImportError("DuckDB worker policy is incomplete")
        original_connect = module.connect
        policy = self._policy

        def safe_connect(
            database: str = ":memory:",
            *,
            read_only: bool = False,
            config: dict[str, Any] | None = None,
        ) -> Any:
            if database != ":memory:" or read_only or config:
                raise PermissionError("DuckDB accepts only the controlled in-memory connection")
            settings = {
                "enable_external_access": "false",
                "allow_community_extensions": "false",
                "allow_unsigned_extensions": "false",
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
                "python_enable_replacements": "false",
                "python_scan_all_frames": "false",
                "threads": str(policy["threads"]),
                "memory_limit": f"{policy['rss_bytes']}B",
                "temp_directory": str(Path.cwd() / "duckdb-tmp"),
                "max_temp_directory_size": f"{policy['scratch_bytes']}B",
            }
            connection = original_connect(":memory:", config=settings)
            connection.execute("SET lock_configuration=true")
            return connection

        # Importing DuckDB opens its own default connection and native pool.
        # Release it before constructing the one controlled connection; keeping
        # both pools silently exceeds the declared computation thread budget.
        module.default_connection().close()
        default = safe_connect()

        def deny_external(*_args: Any, **_kwargs: Any) -> Any:
            raise PermissionError("DuckDB external readers and extensions are disabled")

        module.connect = safe_connect
        module.default_connection = lambda: default
        module.execute = default.execute
        module.query = default.query
        module.sql = default.sql
        for name in (
            "from_csv_auto",
            "install_extension",
            "load_extension",
            "read_csv",
            "read_json",
            "read_parquet",
        ):
            if hasattr(module, name):
                setattr(module, name, deny_external)
        self._duckdb_hardened = True


def install_readonly_audit_hook(import_guard: RestrictedImportGuard) -> None:
    trusted_roots = tuple(Path(value).resolve() for value in {sys.prefix, sys.base_prefix} if value)

    def within_trusted_import_root(value: str | bytes | os.PathLike[str]) -> bool:
        try:
            path = Path(value).resolve()
        except (OSError, TypeError, ValueError):
            return False
        return any(path == root or root in path.parents for root in trusted_roots)

    def called_from_approved_library() -> bool:
        try:
            frame = sys._getframe(1)
            while frame is not None:
                module_root = str(frame.f_globals.get("__name__", "")).split(".", 1)[0]
                if module_root in import_guard.allowed_import_roots or module_root == "importlib":
                    return True
                frame = frame.f_back
        except (AttributeError, ValueError):
            return False
        return False

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            target = args[0] if args else None
            if isinstance(target, int):
                return
            mode = args[1] if len(args) > 1 else "r"
            flags = args[2] if len(args) > 2 else 0
            read_only = (
                isinstance(mode, str)
                and not any(marker in mode for marker in ("w", "a", "x", "+"))
                and (not isinstance(flags, int) or _read_only_flags(flags))
            )
            trusted_library_read = import_guard.active or called_from_approved_library()
            if trusted_library_read and read_only and within_trusted_import_root(target):
                return
            raise PermissionError(
                f"ordinary file access is disabled in the {import_guard.worker_label}"
            )
        if event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec", "os.fork")):
            raise PermissionError("network and subprocess effects are disabled")
        if event in {"os.system", "pty.spawn", "ctypes.dlopen", "ctypes.dlsym"}:
            if event.startswith("ctypes.") and import_guard.active:
                return
            raise PermissionError("dynamic process or library effects are disabled")

    sys.addaudithook(audit)


def safe_builtins(import_guard: RestrictedImportGuard) -> dict[str, Any]:
    values = {name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES}
    values["__import__"] = import_guard
    return values


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _source_location(
    code: str,
    *,
    line: int | None,
    column: int | None,
    end_column: int | None,
) -> tuple[str | None, str | None]:
    if line is None or line < 1:
        return None, None
    source_lines = code.splitlines()
    if line > len(source_lines):
        return None, None
    source_line = source_lines[line - 1]
    if column is None or column < 1:
        return source_line, None
    prefix = source_line[: max(0, column - 1)]
    caret_prefix = "".join("\t" if character == "\t" else " " for character in prefix)
    width = max(1, (end_column or column + 1) - column)
    return source_line, f"{caret_prefix}{'^' * width}"


def _code_diagnostic(
    code: str,
    *,
    exception_type: str,
    message: str,
    line: int | None,
    column: int | None,
    end_line: int | None,
    end_column: int | None,
) -> dict[str, Any]:
    source_line, caret = _source_location(
        code,
        line=line,
        column=column,
        end_column=end_column,
    )
    return {
        "exception_type": exception_type,
        "message": message,
        "line": line,
        "column": column,
        "end_line": end_line,
        "end_column": end_column,
        "source_line": source_line,
        "caret": caret,
        "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
    }


def _import_error(code: str, node: ast.AST, message: str) -> PythonUserCodeValidationError:
    return PythonUserCodeValidationError(
        _code_diagnostic(
            code,
            exception_type="ImportPolicyError",
            message=message,
            line=node.lineno,
            column=node.col_offset + 1,
            end_line=node.end_lineno,
            end_column=node.end_col_offset + 1 if node.end_col_offset is not None else None,
        )
    )


def _entry_error(
    code: str,
    message: str,
    node: ast.AST | None = None,
) -> PythonUserCodeValidationError:
    column_offset = getattr(node, "col_offset", None)
    end_column_offset = getattr(node, "end_col_offset", None)
    return PythonUserCodeValidationError(
        _code_diagnostic(
            code,
            exception_type="EntryPointError",
            message=message,
            line=getattr(node, "lineno", None),
            column=column_offset + 1 if isinstance(column_offset, int) else None,
            end_line=getattr(node, "end_lineno", None),
            end_column=end_column_offset + 1 if isinstance(end_column_offset, int) else None,
        )
    )


def _literal_expression(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_literal_expression(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is None or _literal_expression(key) for key in node.keys) and all(
            _literal_expression(value) for value in node.values
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _literal_expression(node.operand)
    return False


def _read_only_flags(flags: int) -> bool:
    blocked = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    if hasattr(os, "O_EXCL"):
        blocked |= os.O_EXCL
    return flags & blocked == 0


__all__ = [
    "DEFAULT_BLOCKED_IO_ATTRIBUTES",
    "PythonUserCodePreflightError",
    "PythonUserCodeValidationError",
    "RestrictedImportGuard",
    "compile_entry",
    "compile_user_module",
    "diagnostic_json",
    "install_readonly_audit_hook",
    "runtime_code_diagnostic",
    "safe_builtins",
]
