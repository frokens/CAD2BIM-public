"""Stage complete report assets privately for the existing IFC transaction owner."""
from datetime import datetime, timezone
from uuid import uuid4
import pyarrow.parquet as pq
import pyarrow as pa
from CAD2BIM_backend.domain.canonical import canonical_json
from CAD2BIM_backend.domain.control_plane import ArtifactRecord
from CAD2BIM_backend.domain.data_runtime import ArrowColumnRecord, ArtifactVersionRef, DataPackageKind, DataPackageMemberRecord, DataPackageRecord, PackageMemberKind
from CAD2BIM_backend.domain.ifc_model import IfcRunIdentity, PreparedIfcRevision
from CAD2BIM_backend.domain.product_report import ProductReportDiagnostic, ProductReportDiagnostics
from CAD2BIM_backend.ports.storage import StorageKey, StoragePort


async def read_product_report_diagnostics(queries, scope, package, *, edit=False):
    """Read original checks through the existing bounded Data query owner."""
    from CAD2BIM_backend.domain.data_query import DataQueryRowsRequest

    select = ["check_index", "input_row_index", "product_key", "phase", "error_code", "status",
              "field_path", "message", "recommended_action", "metric", "residual", "tolerance", "unit"]
    if edit:
        select.extend(("target_global_id", "related_global_id"))
    pages = []
    for status in ("FAIL", "WARN"):
        pages.append(await queries.query_rows(scope, DataQueryRowsRequest.model_validate({"query": {
            "package_alias": package.catalog_entry.alias, "payload_name": "checks", "select": select,
            "filters": [{"field": "status", "op": "eq", "value": status}]}})))
    items = []
    for page in pages:
        for row in page.rows:
            values = row.values
            items.append(ProductReportDiagnostic(
                check_index=values["check_index"], input_row_index=values.get("input_row_index"),
                product_key=values.get("product_key"), target_global_id=values.get("target_global_id"),
                related_global_id=values.get("related_global_id"), phase=values["phase"],
                code=values.get("error_code"), severity="error" if values["status"] == "FAIL" else "warning",
                field_path=values.get("field_path"),
                message=values.get("message") or values.get("error_code") or "Recorded check",
                recommended_action=values.get("recommended_action") or "Inspect the original checks report",
                metric=values.get("metric"), residual=values.get("residual"),
                tolerance=values.get("tolerance"), unit=values.get("unit")))
    return ProductReportDiagnostics(items=tuple(items), complete=all(page.page.complete for page in pages))


async def stage_product_report(
    storage: StoragePort, scope: IfcRunIdentity, prepared: PreparedIfcRevision,
    *, summary, rows, schemas, keys, description, name, manifest_fields, asset_kind, package_summary,
):
    """Upload complete private bytes; no Artifact, Package or Catalog is registered here."""
    now = datetime.now(timezone.utc)
    package_id = f"data_package_{uuid4().hex}"
    members, artifacts = [], []

    async def stage(name, kind, media, content, *, payload_name=None, table=None):
        artifact_id, version_id = f"artifact_{uuid4().hex}", f"artifact_version_{uuid4().hex}"
        key = StorageKey(scope.owner_id, scope.task_id, artifact_id, 1)

        async def chunks():
            yield content

        written = await storage.write(key, filename=name.rsplit("/", 1)[-1], content_type=media, chunks=chunks())
        stored = await storage.stat(key)
        if stored != written:
            raise ValueError("private report changed while staging")
        artifact = ArtifactRecord(
            artifact_id=artifact_id, version_id=version_id, owner_id=scope.owner_id,
            task_id=scope.task_id, run_id=scope.run_id, kind=asset_kind,
            filename=stored.filename, content_type=media, size_bytes=stored.size_bytes,
            sha256=stored.sha256, idempotency_key=f"product-report-asset:{artifact_id}", created_at=now,
        )
        artifacts.append(artifact)
        members.append(DataPackageMemberRecord(
            member_id=f"package_member_{uuid4().hex}", owner_id=scope.owner_id,
            tenant_id=scope.tenant_id, task_id=scope.task_id, package_id=package_id,
            member_name=name, member_kind=kind,
            artifact=ArtifactVersionRef(artifact_id=artifact_id, version_id=version_id, sha256=stored.sha256),
            media_type=media, created_at=now, logical_payload_name=payload_name,
            row_count=table.num_rows if table is not None else None,
            logical_key_fields=keys[payload_name] if table is not None else (),
            arrow_schema=tuple(ArrowColumnRecord(name=field.name, type=str(field.type), nullable=field.nullable)
                               for field in table.schema) if table is not None else (),
            logical_byte_count=table.nbytes if table is not None else None,
            payload_description=f"{name} {payload_name} results" if table is not None else None,
        ))

    for payload_name, schema in schemas.items():
        table = pa.Table.from_pylist(rows[payload_name], schema=schema)
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink, compression="zstd")
        await stage(f"payloads/{payload_name}/part-00000.parquet", PackageMemberKind.PAYLOAD,
                    "application/vnd.apache.parquet", sink.getvalue().to_pybytes(), payload_name=payload_name, table=table)
    await stage("description.md", PackageMemberKind.DESCRIPTION, "text/markdown", description.encode())
    manifest = {
        "publication_state": "published", "package_id": package_id, "package_kind": "derived",
        "producer_operation_id": prepared.operation_id,
        **manifest_fields,
        "summary": summary.model_dump(mode="json"),
        "payloads": {member.logical_payload_name: member.model_dump(mode="json")
                     for member in members if member.member_kind is PackageMemberKind.PAYLOAD},
    }
    await stage("manifest.json", PackageMemberKind.MANIFEST, "application/json", canonical_json(manifest).encode())
    return (DataPackageRecord(
            package_id=package_id, owner_id=scope.owner_id, tenant_id=scope.tenant_id,
            task_id=scope.task_id, created_by_run_id=scope.run_id, name=name,
            package_kind=DataPackageKind.DERIVED, description_title=name,
            summary=package_summary,
            section_titles=tuple(item.title() for item in schemas),
            idempotency_key=f"product-report:{package_id}", request_fingerprint=prepared.request_digest,
            created_at=now,
        ), tuple(members), tuple(artifacts))
