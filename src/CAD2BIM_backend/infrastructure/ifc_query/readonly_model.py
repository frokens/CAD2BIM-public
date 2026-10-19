"""Shared private IFC loading, dangerous-capability checks and mutation audit."""
from __future__ import annotations
import ast
import hashlib
from typing import Any

_MUTATOR_ATTRIBUTES = frozenset(
    {
        "add",
        "assign_header_from",
        "batch",
        "begin_transaction",
        "create_entity",
        "discard_transaction",
        "end_transaction",
        "redo",
        "remove",
        "set_history_size",
        "unbatch",
        "undo",
        "write",
    }
)

_LOW_LEVEL_ATTRIBUTES = frozenset(
    {
        "file",
        "from_pointer",
        "future",
        "history",
        "history_size",
        "ifcopenshell_wrapper",
        "storage",
        "to_delete",
        "transaction",
        "wrapped_data",
    }
)

class _ChildError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str,
        execution_started: bool,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.phase = phase
        self.execution_started = execution_started
        self.diagnostic = diagnostic
        super().__init__(message)

def _validate_source(source: str) -> None:
    try:
        tree = ast.parse(source, filename="<cad2bim-query>", mode="exec")
    except SyntaxError:
        return
    model_names = {n.targets[0].id for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
        and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == "model"}
    def model_receiver(value):
        return (isinstance(value, ast.Name) and value.id in model_names) or (
            isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "model")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            operation = "delete" if isinstance(node.ctx, ast.Del) else "assign"
            raise _ChildError(
                "IFC_QUERY_MUTATION_FORBIDDEN",
                "IFC query source cannot assign to or delete attributes; local Python "
                "container indexing is allowed and IFC index mutations are audited at runtime",
                phase="code_validation",
                execution_started=False,
                diagnostic={
                    "line": node.lineno,
                    "column": node.col_offset + 1,
                    "end_line": node.end_lineno or node.lineno,
                    "end_column": (node.end_col_offset or node.col_offset) + 1,
                    "ast_node": type(node).__name__,
                    "operation": operation,
                },
            )
        if isinstance(node, ast.Attribute):
            normalized = node.attr.lower()
            if node.attr.startswith("_"):
                raise _ChildError(
                    "IFC_QUERY_SOURCE_INVALID",
                    "IFC query source cannot access private runtime attributes",
                    phase="code_validation",
                    execution_started=False,
                )
            if node.attr == "open":
                raise _ChildError(
                    "IFC_QUERY_SOURCE_INVALID",
                    "IFC query source cannot reopen filesystem IFC content",
                    phase="code_validation",
                    execution_started=False,
                )
            if (
                (node.attr in _MUTATOR_ATTRIBUTES and (node.attr not in {"add", "remove"} or model_receiver(node.value)))
                or node.attr in _LOW_LEVEL_ATTRIBUTES
                or normalized.startswith("createifc")
            ):
                raise _ChildError(
                    "IFC_QUERY_MUTATION_FORBIDDEN",
                    "IFC query source references a forbidden mutation or low-level attribute; "
                    "ordinary local container add/remove remain allowed",
                    phase="code_validation",
                    execution_started=False,
                )
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or "", *(alias.name for alias in node.names)]
            if (node.module or "").split(".", 1)[0] == "ifcopenshell" and any(
                alias.name == "open" for alias in node.names
            ):
                raise _ChildError(
                    "IFC_QUERY_SOURCE_INVALID",
                    "IFC query source cannot import a filesystem IFC opener",
                    phase="code_validation",
                    execution_started=False,
                )
        else:
            names = []
        if any("ifcopenshell_wrapper" in name.split(".") for name in names):
            raise _ChildError(
                "IFC_QUERY_MUTATION_FORBIDDEN",
                "IFC query source requests a low-level IFC mutation capability",
                phase="code_validation",
                execution_started=False,
            )

def _serialized_digest(model: Any) -> str:
    serialized = model.to_string()
    digest = hashlib.sha256()
    # Encode consecutive Unicode slices to avoid another whole-model bytes copy.
    # UTF-8 encodes each code point independently, preserving the exact byte hash.
    for offset in range(0, len(serialized), 1024 * 1024):
        digest.update(serialized[offset:offset + 1024 * 1024].encode("utf-8"))
    return digest.hexdigest()


def load_private_model(ifc_bytes: bytes, expected_sha256: str) -> Any:
    from CAD2BIM_backend.ports.ifc_query_worker import MAX_IFC_INPUT_BYTES
    import ifcopenshell

    if (
        not ifc_bytes or len(ifc_bytes) > MAX_IFC_INPUT_BYTES
        or hashlib.sha256(ifc_bytes).hexdigest() != expected_sha256
    ):
        raise _ChildError(
            "IFC_QUERY_EXACT_INPUT_INVALID", "Private IFC bytes do not match the exact input",
            phase="input_validation", execution_started=False,
        )
    try:
        return ifcopenshell.file.from_string(ifc_bytes.decode("utf-8"))
    except (UnicodeError, RuntimeError, ValueError) as exc:
        raise _ChildError(
            "IFC_QUERY_EXACT_INPUT_INVALID", "Private IFC bytes could not be parsed",
            phase="input_validation", execution_started=False,
        ) from exc


class PrivateModelAudit:
    """Audit the actual private model, including mutations that were later reversed."""

    def __init__(self, model: Any) -> None:
        self.model = model
        self.before = _serialized_digest(model)
        model.begin_transaction()
        self.transaction = model.transaction
        if self.transaction is None:
            raise _ChildError(
                "IFC_QUERY_MUTATION_DETECTED", "Private IFC transaction could not be started",
                phase="mutation_audit", execution_started=False,
            )
        self.end_transaction = model.end_transaction
        self.undo = model.undo
        self.forbidden_control = False
        self.recorded_mutations = 0

        def tracked_store(original):
            def store(*args: Any, **kwargs: Any) -> Any:
                # Keep the count outside the exposed transaction operation list:
                # clearing that list must not hide a changed-then-restored model.
                self.recorded_mutations += 1
                return original(*args, **kwargs)
            return store

        for name in ("store_create", "store_edit", "store_delete"):
            setattr(self.transaction, name, tracked_store(getattr(self.transaction, name)))

        def deny_control(*args: Any, **kwargs: Any) -> None:
            self.forbidden_control = True
            raise PermissionError("Private IFC transaction control and export are forbidden")

        for name in (
            "begin_transaction", "end_transaction", "discard_transaction", "undo", "redo",
            "set_history_size", "batch", "unbatch", "assign_header_from", "write",
        ):
            if hasattr(model, name):
                setattr(model, name, deny_control)

    def finish(self) -> dict[str, Any]:
        same_transaction = self.model.transaction is self.transaction
        count = max(len(self.transaction.operations), self.recorded_mutations)
        control_changed = (
            self.forbidden_control or not same_transaction
            or bool(getattr(self.transaction, "is_batched", False))
            or bool(getattr(self.transaction, "batch_delete_ids", ()))
            or bool(getattr(self.transaction, "batch_inverses", ()))
        )
        try:
            after = _serialized_digest(self.model)
        except BaseException:
            after = None
        if same_transaction:
            self.end_transaction()
            if count:
                self.undo()
        if count or control_changed or after != self.before:
            raise _ChildError(
                "IFC_QUERY_MUTATION_DETECTED",
                "Private IFC mutations were rolled back and outputs discarded" if count and same_transaction
                else "Private IFC mutation or transaction control was detected; outputs discarded",
                phase="execution", execution_started=True,
                diagnostic={"transaction_operation_count": count, "transaction_control_changed": control_changed},
            )
        return {
            "private_ifc_sha256_before": self.before,
            "private_ifc_sha256_after": after,
            "transaction_operation_count": count,
        }
