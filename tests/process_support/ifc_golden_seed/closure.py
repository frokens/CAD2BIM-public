"""Pure reachability and scope checks for an IFC Golden Seed export."""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class SeedClosureError(ValueError):
    """The selected snapshot is incomplete, out of scope, or beyond its frontier."""


@dataclass(frozen=True, slots=True)
class SeedExportScope:
    tenant_id: str
    principal_id: str
    owner_id: str
    task_id: str
    run_id: str

    @classmethod
    def normalized(
        cls,
        *,
        tenant_id: str,
        principal_id: str,
        owner_id: str,
        task_id: str,
        run_id: str,
    ) -> "SeedExportScope":
        values = {
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "owner_id": owner_id,
            "task_id": task_id,
            "run_id": run_id,
        }
        normalized: dict[str, str] = {}
        for field, value in values.items():
            if not isinstance(value, str):
                raise SeedClosureError(f"{field} must be a string")
            clean = unicodedata.normalize("NFKC", value).strip()
            if not clean or "\x00" in clean or len(clean) > 255:
                raise SeedClosureError(f"{field} is empty or invalid")
            normalized[field] = clean
        return cls(**normalized)


def _rows(values: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(dict(value) for value in values)


@dataclass(frozen=True, slots=True)
class SeedSnapshot:
    catalog_entries: tuple[dict[str, Any], ...]
    packages: tuple[dict[str, Any], ...]
    package_members: tuple[dict[str, Any], ...]
    operations: tuple[dict[str, Any], ...]
    operation_inputs: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]
    artifact_versions: tuple[dict[str, Any], ...]
    ifc_models: tuple[dict[str, Any], ...] = ()
    ifc_revisions: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_rows(
        cls,
        *,
        catalog_entries: Iterable[Mapping[str, Any]],
        packages: Iterable[Mapping[str, Any]],
        package_members: Iterable[Mapping[str, Any]],
        operations: Iterable[Mapping[str, Any]],
        operation_inputs: Iterable[Mapping[str, Any]],
        artifacts: Iterable[Mapping[str, Any]],
        artifact_versions: Iterable[Mapping[str, Any]],
        ifc_models: Iterable[Mapping[str, Any]] = (),
        ifc_revisions: Iterable[Mapping[str, Any]] = (),
    ) -> "SeedSnapshot":
        return cls(
            catalog_entries=_rows(catalog_entries),
            packages=_rows(packages),
            package_members=_rows(package_members),
            operations=_rows(operations),
            operation_inputs=_rows(operation_inputs),
            artifacts=_rows(artifacts),
            artifact_versions=_rows(artifact_versions),
            ifc_models=_rows(ifc_models),
            ifc_revisions=_rows(ifc_revisions),
        )


@dataclass(frozen=True, slots=True)
class ArtifactReachability:
    artifact_id: str
    version_id: str
    reverse_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SeedClosure:
    catalog_frontier: int
    operation_frontier: int
    catalog_entries: tuple[dict[str, Any], ...]
    packages: tuple[dict[str, Any], ...]
    package_members: tuple[dict[str, Any], ...]
    operations: tuple[dict[str, Any], ...]
    operation_inputs: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]
    artifact_versions: tuple[dict[str, Any], ...]
    ifc_models: tuple[dict[str, Any], ...]
    ifc_revisions: tuple[dict[str, Any], ...]
    artifact_reachability: tuple[ArtifactReachability, ...]

    def database_document(self, scope: SeedExportScope) -> dict[str, Any]:
        return _json_value(
            {
                "scope": {
                    "tenant_id": scope.tenant_id,
                    "owner_id": scope.owner_id,
                    "task_id": scope.task_id,
                    "run_id": scope.run_id,
                },
                "catalog_frontier": self.catalog_frontier,
                "operation_frontier": self.operation_frontier,
                "catalog_entries": self.catalog_entries,
                "packages": self.packages,
                "package_members": self.package_members,
                "operations": self.operations,
                "operation_inputs": self.operation_inputs,
                "artifacts": self.artifacts,
                "artifact_versions": self.artifact_versions,
                "ifc_models": self.ifc_models,
                "ifc_revisions": self.ifc_revisions,
            }
        )


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _json_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _index(rows: Iterable[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = row.get(key)
        if not isinstance(identity, str) or not identity:
            raise SeedClosureError(f"{label} has no valid {key}")
        if identity in result:
            raise SeedClosureError(f"duplicate {label} {key}")
        result[identity] = row
    return result


def _exact_version_index(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("artifact_id"), row.get("version_id"))
        if not all(isinstance(item, str) and item for item in key):
            raise SeedClosureError("artifact version has an invalid exact identity")
        if key in result:
            raise SeedClosureError("duplicate exact artifact version")
        result[key] = row
    return result


def _require_scope(row: Mapping[str, Any], scope: SeedExportScope, label: str) -> None:
    expected = {
        "tenant_id": scope.tenant_id,
        "owner_id": scope.owner_id,
        "task_id": scope.task_id,
    }
    for field, value in expected.items():
        if field in row and row[field] != value:
            raise SeedClosureError(f"{label} escaped the normalized scope")
    if "run_id" in row and row["run_id"] not in {None, scope.run_id}:
        raise SeedClosureError(f"{label} escaped the selected Run")
    if "created_by_run_id" in row and row["created_by_run_id"] != scope.run_id:
        raise SeedClosureError(f"{label} escaped the selected Run")


def _sort(rows: Iterable[dict[str, Any]], *keys: str) -> tuple[dict[str, Any], ...]:
    return tuple(sorted(rows, key=lambda row: tuple(row.get(key) for key in keys)))


def build_export_closure(
    snapshot: SeedSnapshot,
    scope: SeedExportScope,
    *,
    catalog_frontier: int | None = None,
    operation_frontier: int | None = None,
    include_ifc: bool = True,
) -> SeedClosure:
    """Select the exact frontier and prove all package/operation/artifact references close."""

    all_groups = (
        ("catalog entry", snapshot.catalog_entries),
        ("package", snapshot.packages),
        ("package member", snapshot.package_members),
        ("operation", snapshot.operations),
        ("operation input", snapshot.operation_inputs),
        ("artifact", snapshot.artifacts),
        ("artifact version", snapshot.artifact_versions),
        ("IFC model", snapshot.ifc_models),
        ("IFC revision", snapshot.ifc_revisions),
    )
    for label, rows in all_groups:
        for row in rows:
            _require_scope(row, scope, label)

    for row in snapshot.catalog_entries:
        if (
            not isinstance(row.get("alias"), str)
            or not row["alias"]
            or isinstance(row.get("ordinal"), bool)
            or not isinstance(row.get("ordinal"), int)
            or row["ordinal"] < 1
            or isinstance(row.get("catalog_revision"), bool)
            or not isinstance(row.get("catalog_revision"), int)
            or row["catalog_revision"] < 1
        ):
            raise SeedClosureError("catalog entry identity or frontier data is invalid")
    maximum = max(
        (int(row["catalog_revision"]) for row in snapshot.catalog_entries), default=0
    )
    frontier = maximum if catalog_frontier is None else catalog_frontier
    if isinstance(frontier, bool) or not isinstance(frontier, int) or frontier < 1:
        raise SeedClosureError("catalog frontier must be a positive integer")
    if frontier > maximum:
        raise SeedClosureError("catalog frontier is beyond the consistent snapshot")

    visible_catalog = tuple(
        row for row in snapshot.catalog_entries if int(row["catalog_revision"]) <= frontier
    )
    revisions = {int(row["catalog_revision"]) for row in visible_catalog}
    if revisions != set(range(1, frontier + 1)):
        raise SeedClosureError("catalog revisions are not closed through the frontier")
    aliases = [row.get("alias") for row in visible_catalog]
    ordinals = [
        (row.get("resource_kind"), row.get("ordinal")) for row in visible_catalog
    ]
    if len(aliases) != len(set(aliases)) or len(ordinals) != len(set(ordinals)):
        raise SeedClosureError("catalog aliases or per-kind ordinals are not unique")
    for kind in ("package", "operation"):
        kind_ordinals = sorted(
            int(row["ordinal"])
            for row in visible_catalog
            if row.get("resource_kind") == kind
        )
        if kind_ordinals != list(range(1, len(kind_ordinals) + 1)):
            raise SeedClosureError("catalog ordinals are not contiguous")
    package_ids = {
        row["package_id"] for row in visible_catalog if row.get("resource_kind") == "package"
    }
    operation_ids = {
        row["operation_id"]
        for row in visible_catalog
        if row.get("resource_kind") == "operation"
    }
    if None in package_ids or None in operation_ids:
        raise SeedClosureError("catalog resource binding is incomplete")

    packages_by_id = _index(snapshot.packages, "package_id", "package")
    members_by_id = _index(snapshot.package_members, "member_id", "package member")
    operations_by_id = _index(snapshot.operations, "operation_id", "operation")
    artifacts_by_id = _index(snapshot.artifacts, "artifact_id", "artifact")
    versions_by_exact = _exact_version_index(snapshot.artifact_versions)
    revisions_by_id = _index(snapshot.ifc_revisions, "revision_id", "IFC revision")
    models_by_id = _index(snapshot.ifc_models, "ifc_model_id", "IFC model")

    missing_packages = package_ids - packages_by_id.keys()
    missing_operations = operation_ids - operations_by_id.keys()
    if missing_packages or missing_operations:
        raise SeedClosureError("catalog references a missing package or operation")

    selected_revision_ids: set[str] = set()
    if include_ifc:
        for model in snapshot.ifc_models:
            if model.get("run_id") == scope.run_id:
                current = model.get("current_revision_id")
                if not isinstance(current, str) or current not in revisions_by_id:
                    raise SeedClosureError("IFC CURRENT references a missing revision")
                selected_revision_ids.update(
                    revision["revision_id"]
                    for revision in snapshot.ifc_revisions
                    if revision.get("ifc_model_id") == model["ifc_model_id"]
                )

    inputs_by_operation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot.operation_inputs:
        inputs_by_operation[row.get("operation_id")].append(row)

    changed = True
    while changed:
        before = (len(package_ids), len(operation_ids), len(selected_revision_ids))
        for operation_id in tuple(operation_ids):
            operation = operations_by_id.get(operation_id)
            if operation is None:
                raise SeedClosureError("operation closure references a missing operation")
            if operation.get("result_package_id") is not None:
                package_ids.add(operation["result_package_id"])
            if operation.get("repair_parent_operation_id") is not None:
                operation_ids.add(operation["repair_parent_operation_id"])
            if operation.get("base_ifc_revision_id") is not None:
                selected_revision_ids.add(operation["base_ifc_revision_id"])
            for binding in inputs_by_operation.get(operation_id, ()):
                package_ids.add(binding["package_id"])
        for revision_id in tuple(selected_revision_ids):
            revision = revisions_by_id.get(revision_id)
            if revision is None:
                raise SeedClosureError("operation closure references a missing IFC revision")
            operation_ids.add(revision["operation_id"])
        changed = before != (len(package_ids), len(operation_ids), len(selected_revision_ids))

    visible_package_ids = {
        row.get("package_id") for row in visible_catalog if row.get("resource_kind") == "package"
    }
    visible_operation_ids = {
        row.get("operation_id")
        for row in visible_catalog
        if row.get("resource_kind") == "operation"
    }
    if package_ids - visible_package_ids or operation_ids - visible_operation_ids:
        raise SeedClosureError("a reachable resource lies beyond the catalog frontier")

    selected_packages = [packages_by_id[identity] for identity in package_ids]
    selected_members = [
        row for row in snapshot.package_members if row.get("package_id") in package_ids
    ]
    member_names = [(row.get("package_id"), row.get("member_name")) for row in selected_members]
    logical_names = [
        (row.get("package_id"), row.get("logical_payload_name"))
        for row in selected_members
        if row.get("logical_payload_name") is not None
    ]
    if len(member_names) != len(set(member_names)) or len(logical_names) != len(
        set(logical_names)
    ):
        raise SeedClosureError("package member or logical payload names are not unique")
    selected_operations = [operations_by_id[identity] for identity in operation_ids]
    if any(
        operation.get("status") in {"prepared", "indeterminate"}
        for operation in selected_operations
    ):
        raise SeedClosureError("non-terminal or indeterminate operations cannot be exported")
    selected_inputs = [
        row for row in snapshot.operation_inputs if row.get("operation_id") in operation_ids
    ]
    for binding in selected_inputs:
        member = members_by_id.get(binding.get("member_id"))
        if member is None or member.get("package_id") != binding.get("package_id"):
            raise SeedClosureError("operation input does not bind an exact package member")

    selected_revisions = [revisions_by_id[identity] for identity in selected_revision_ids]
    selected_model_ids = {row["ifc_model_id"] for row in selected_revisions}
    selected_models = [models_by_id[identity] for identity in selected_model_ids]
    for model in selected_models:
        model_revisions = sorted(
            (
                revision
                for revision in selected_revisions
                if revision["ifc_model_id"] == model["ifc_model_id"]
            ),
            key=lambda revision: int(revision["revision_no"]),
        )
        revision_numbers = [int(revision["revision_no"]) for revision in model_revisions]
        current = revisions_by_id.get(model.get("current_revision_id"))
        if (
            model.get("status") != "active"
            or current is None
            or revision_numbers != list(range(1, len(revision_numbers) + 1))
            or current.get("revision_no") != revision_numbers[-1]
            or model.get("write_epoch") != revision_numbers[-1]
        ):
            raise SeedClosureError("IFC revision history is not contiguous through active CURRENT")
        for revision in model_revisions:
            operation = operations_by_id.get(revision["operation_id"])
            if operation is None or operation.get("status") != "succeeded":
                raise SeedClosureError("IFC revision is not backed by a succeeded operation")

    references: dict[tuple[str, str], set[str]] = defaultdict(set)

    def add_artifact(artifact_id: Any, version_id: Any, reference: str) -> None:
        if artifact_id is None and version_id is None:
            return
        exact = (artifact_id, version_id)
        if not all(isinstance(item, str) and item for item in exact):
            raise SeedClosureError("artifact reference is not an exact version")
        if exact not in versions_by_exact or artifact_id not in artifacts_by_id:
            raise SeedClosureError("artifact closure references a missing exact version")
        references[exact].add(reference)

    for member in selected_members:
        add_artifact(
            member.get("artifact_id"),
            member.get("artifact_version_id"),
            f"package_member:{member['member_id']}",
        )
        exact_version = versions_by_exact[
            (member["artifact_id"], member["artifact_version_id"])
        ]
        if member.get("artifact_sha256") != exact_version.get("sha256"):
            raise SeedClosureError("package member digest disagrees with its exact artifact")
    for operation in selected_operations:
        if operation.get("tool_name") == "ifc_trusted_baseline_load":
            detail = operation.get("operation_detail_json")
            source = detail.get("source_asset") if isinstance(detail, Mapping) else None
            if (not isinstance(source, Mapping)
                    or not source.get("artifact_id") or not source.get("version_id")):
                raise SeedClosureError("artifact reference is not an exact version")
            add_artifact(source["artifact_id"], source["version_id"],
                         f"operation:{operation['operation_id']}:source_asset")
            exact_version = versions_by_exact[(source["artifact_id"], source["version_id"])]
            if source.get("sha256") != exact_version.get("sha256"):
                raise SeedClosureError("operation artifact digest disagrees with its exact version")
        add_artifact(
            operation.get("code_artifact_id"),
            operation.get("code_artifact_version_id"),
            f"operation:{operation['operation_id']}:code",
        )
        add_artifact(
            operation.get("description_artifact_id"),
            operation.get("description_artifact_version_id"),
            f"operation:{operation['operation_id']}:description",
        )
        for prefix in ("code", "description"):
            artifact_id = operation.get(f"{prefix}_artifact_id")
            version_id = operation.get(f"{prefix}_artifact_version_id")
            if artifact_id is not None:
                exact_version = versions_by_exact[(artifact_id, version_id)]
                if operation.get(f"{prefix}_artifact_sha256") != exact_version.get("sha256"):
                    raise SeedClosureError(
                        "operation artifact digest disagrees with its exact version"
                    )
    for revision in selected_revisions:
        add_artifact(
            revision.get("artifact_id"),
            revision.get("artifact_version_id"),
            f"ifc_revision:{revision['revision_id']}",
        )

    for artifact_id in {identity[0] for identity in references}:
        artifact = artifacts_by_id[artifact_id]
        current_version_id = artifact.get("current_version_id")
        if not isinstance(current_version_id, str):
            raise SeedClosureError("reachable artifact has no exact CURRENT version")
        add_artifact(
            artifact_id,
            current_version_id,
            f"artifact:{artifact_id}:current",
        )

    selected_versions = [versions_by_exact[identity] for identity in references]
    selected_artifact_ids = {row["artifact_id"] for row in selected_versions}
    selected_artifacts = [artifacts_by_id[identity] for identity in selected_artifact_ids]
    reachability = tuple(
        ArtifactReachability(identity[0], identity[1], tuple(sorted(reverse_refs)))
        for identity, reverse_refs in sorted(references.items())
    )
    derived_operation_frontier = max(
        (
            int(row["ordinal"])
            for row in visible_catalog
            if row.get("resource_kind") == "operation"
        ),
        default=0,
    )
    if operation_frontier is not None:
        if (
            isinstance(operation_frontier, bool)
            or not isinstance(operation_frontier, int)
            or operation_frontier < 0
            or operation_frontier != derived_operation_frontier
        ):
            raise SeedClosureError("operation frontier does not match the catalog snapshot")
    return SeedClosure(
        catalog_frontier=frontier,
        operation_frontier=derived_operation_frontier,
        catalog_entries=_sort(visible_catalog, "catalog_revision", "resource_kind", "ordinal"),
        packages=_sort(selected_packages, "package_id"),
        package_members=_sort(selected_members, "package_id", "member_name", "member_id"),
        operations=_sort(selected_operations, "operation_id"),
        operation_inputs=_sort(selected_inputs, "operation_id", "ordinal"),
        artifacts=_sort(selected_artifacts, "artifact_id"),
        artifact_versions=_sort(selected_versions, "artifact_id", "version_no"),
        ifc_models=_sort(selected_models, "ifc_model_id"),
        ifc_revisions=_sort(selected_revisions, "ifc_model_id", "revision_no"),
        artifact_reachability=reachability,
    )


__all__ = [
    "ArtifactReachability",
    "SeedClosure",
    "SeedClosureError",
    "SeedExportScope",
    "SeedSnapshot",
    "build_export_closure",
]
