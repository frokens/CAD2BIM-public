from __future__ import annotations


import base64
import hashlib
import io
import struct
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from CAD2BIM_backend.application.artifact_service import ArtifactDownload
from CAD2BIM_backend.application.geometry_catalog_service import (
    GeometryCatalogService,
)
from CAD2BIM_backend.application.geometry_resource_service import GeometryResourceService
from CAD2BIM_backend.application.geometry_source_service import GeometrySourceService
from CAD2BIM_backend.application.text_source_view import TextSourceViewError, TextSourceViewService
from CAD2BIM_backend.domain.canonical import canonical_json, sha256_digest
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ExecutionMode
from CAD2BIM_backend.domain.geometry_catalog import (
    GeometryCatalogListQuery,
    GeometryCatalogReferenceError,
    GeometrySourceBundleError,
    GeometrySourceNotTextError,
)
from CAD2BIM_backend.domain.geometry_interface import (
    GeometryModelInterface,
)
from CAD2BIM_backend.domain.geometry_resource import (
    GeometryResourceAssetRole,
    GeometryResourceKind,
    GeometryResourcePage,
    GeometryResourceRecord,
    GeometryResourceScope,
    GeometryResourceScopeKind,
    GeometryResourceVersionAssetRecord,
    GeometryResourceVersionBundle,
    GeometryResourceVersionRecord,
    geometry_resource_content_digest,
)
from CAD2BIM_backend.domain.geometry_runtime import (
    GeometryPreview,
    GeometryPreviewInterfacesOverlayRequest,
    GeometryResolvedOrthographicCamera,
    GeometryResolvedPreviewPanel,
)
from CAD2BIM_backend.domain.identity import TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.domain.invocation import InvocationScope
from CAD2BIM_backend.domain.text_source import TextSourceView
from CAD2BIM_backend.infrastructure.data_query.cursor_codec import CursorCodec
from CAD2BIM_backend.infrastructure.source_text import source_bundle as source_bundle_codec
from CAD2BIM_backend.infrastructure.source_text.source_bundle import (
    MAX_SOURCE_BUNDLE_BYTES,
    MAX_SOURCE_BUNDLE_MEMBER_BYTES,
    MAX_SOURCE_BUNDLE_TOTAL_BYTES,
    SourceBundleError,
    inspect_source_bundle,
    pack_source_bundle,
)
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage


NOW = datetime(2026, 9, 2, tzinfo=UTC)


def _codec() -> CursorCodec:
    key = base64.urlsafe_b64encode(b"g" * 32).rstrip(b"=").decode()
    return CursorCodec(key)


def _context(
    *,
    task_id: str = "task-1",
    principal_id: str = "principal-1",
) -> TenantExecutionContext:
    return TenantExecutionContext(
        tenant_id="tenant-1",
        principal_id=principal_id,
        task_id=task_id,
        actor_kind=TenantActorKind.USER,
        command_id="command-current",
    )


def _scope(
    *,
    task_id: str = "task-1",
    run_id: str = "run-1",
    principal_id: str = "principal-1",
) -> InvocationScope:
    return InvocationScope(
        identity=_context(task_id=task_id, principal_id=principal_id),
        owner_id="tenant-1",
        run_id=run_id,
        execution_mode=ExecutionMode.DIRECT_MCP,
        catalog_frontier=0,
    )


def _preview_artifact_service(png: bytes) -> AsyncMock:
    artifacts = AsyncMock()
    published: dict[str, ArtifactRecord] = {}
    contents: dict[tuple[str, str], bytes] = {}

    async def find_by_idempotency_key(
        owner_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> ArtifactRecord | None:
        artifact = published.get(idempotency_key)
        if artifact is not None:
            assert artifact.owner_id == owner_id
            assert artifact.task_id == task_id
        return artifact

    async def upload_bytes(**kwargs) -> ArtifactRecord:
        content = kwargs["content"]
        artifact = ArtifactRecord(
            artifact_id=f"artifact-preview-{len(published) + 1}",
            version_id=f"artifact-version-preview-{len(published) + 1}",
            owner_id=kwargs["owner_id"],
            task_id=kwargs["task_id"],
            kind=kwargs["kind"],
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            idempotency_key=kwargs["idempotency_key"],
            created_at=NOW,
            run_id=kwargs["run_id"],
            version_metadata=kwargs["version_metadata"],
        )
        published[artifact.idempotency_key] = artifact
        contents[(artifact.artifact_id, artifact.version_id)] = content
        return artifact

    async def download_version(
        owner_id: str,
        artifact_id: str,
        version_id: str,
        *,
        run_id: str | None = None,
    ) -> ArtifactDownload:
        artifact = next(
            item
            for item in published.values()
            if item.artifact_id == artifact_id and item.version_id == version_id
        )
        assert artifact.owner_id == owner_id
        assert artifact.run_id == run_id
        return ArtifactDownload(
            artifact=artifact,
            content=contents[(artifact.artifact_id, artifact.version_id)],
        )

    artifacts.find_by_idempotency_key.side_effect = find_by_idempotency_key
    artifacts.upload_bytes.side_effect = upload_bytes
    artifacts.download_version.side_effect = download_version
    artifacts.published = published
    artifacts.contents = contents
    return artifacts


def _resolved_panel(
    panel_key: str = "legacy",
    *,
    x_px: int = 0,
    y_px: int = 0,
    width_px: int = 640,
    height_px: int = 480,
    visible_overlay_ids: tuple[str, ...] = (),
) -> GeometryResolvedPreviewPanel:
    return GeometryResolvedPreviewPanel(
        panel_key=panel_key,
        x_px=x_px,
        y_px=y_px,
        width_px=width_px,
        height_px=height_px,
        camera=GeometryResolvedOrthographicCamera(
            target_m=(0.5, 0.5, 0.5),
            direction=(0.0, 0.0, -1.0),
            up=(0.0, 1.0, 0.0),
            right=(1.0, 0.0, 0.0),
            horizontal_extent_m=width_px / height_px,
            vertical_extent_m=1.0,
        ),
        visible_overlay_ids=visible_overlay_ids,
    )


def _rendered_for_request(request, png: bytes) -> GeometryPreview:
    presentation = request.presentation
    if presentation is None:
        panels = (_resolved_panel(),)
    else:
        panel_count = len(presentation.panels)
        columns, rows = (1, 1) if panel_count == 1 else (2, 1) if panel_count == 2 else (2, 2)
        visible = (
            tuple(item.overlay_id for item in request.scene.interface_overlays)
            if isinstance(presentation.overlay, GeometryPreviewInterfacesOverlayRequest)
            else ()
        )
        panels = tuple(
            _resolved_panel(
                panel.panel_key,
                x_px=(index % columns) * (request.width_px // columns),
                y_px=(index // columns) * (request.height_px // rows),
                width_px=request.width_px // columns,
                height_px=request.height_px // rows,
                visible_overlay_ids=visible,
            )
            for index, panel in enumerate(presentation.panels)
        )
    return GeometryPreview(
        view=request.view if presentation is None else None,
        width_px=request.width_px,
        height_px=request.height_px,
        payload=png,
        sha256=hashlib.sha256(png).hexdigest(),
        resolved_panels=panels,
    )


def _bundle(
    *,
    source_zip: bytes | None = None,
    geometry: bytes = b"canonical-brep",
    interfaces: tuple[GeometryModelInterface, ...] = (),
    version_id: str = "geometry-version-1",
    version_no: int = 1,
) -> GeometryResourceVersionBundle:
    resource = GeometryResourceRecord(
        resource_id="geometry-resource-1",
        resource_kind=GeometryResourceKind.MAPPED_MODEL,
        owner_scope=GeometryResourceScope(scope_kind=GeometryResourceScopeKind.SYSTEM),
        display_name="Neutral mapped shape",
        created_at=NOW,
    )
    manifest = {"schema": "geometry_resource_manifest_v1"}
    if interfaces:
        manifest["interfaces"] = [item.model_dump(mode="json") for item in interfaces]
    manifest_bytes = canonical_json(manifest).encode()
    specifications = [
        (
            GeometryResourceAssetRole.CANONICAL_GEOMETRY,
            "occt_brep_v1",
            "model.brep",
            "application/octet-stream",
            geometry,
        ),
        (
            GeometryResourceAssetRole.MANIFEST,
            "json",
            "manifest.json",
            "application/json",
            manifest_bytes,
        ),
        (
            GeometryResourceAssetRole.VALIDATION_REPORT,
            "json",
            "validation.json",
            "application/json",
            b"{}",
        ),
    ]
    if source_zip is not None:
        specifications.append(
            (
                GeometryResourceAssetRole.SOURCE_BUNDLE,
                "zip",
                "source.zip",
                "application/zip",
                source_zip,
            )
        )
    assets = tuple(
        GeometryResourceVersionAssetRecord(
            resource_version_id=version_id,
            asset_role=role,
            asset_format=asset_format,
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=NOW,
        )
        for role, asset_format, filename, content_type, content in specifications
    )
    manifest_digest = sha256_digest(manifest)
    version = GeometryResourceVersionRecord(
        resource_version_id=version_id,
        resource_id=resource.resource_id,
        version_no=version_no,
        origin_kind="neutral_fixture",
        content_digest=geometry_resource_content_digest(
            resource_kind=resource.resource_kind,
            manifest_digest=manifest_digest,
            assets=assets,
        ),
        manifest_digest=manifest_digest,
        manifest=manifest,
        created_at=NOW,
    )
    return GeometryResourceVersionBundle(resource=resource, version=version, assets=assets)


def _source_zip(entries: tuple[tuple[str, str], ...]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, text in entries:
            archive.writestr(path, text.encode("utf-8"))
    return output.getvalue()


def _modeling_function_bundle(
    source_zip: bytes,
    parameter_schema: dict[str, object],
    *,
    preview_png: bytes | None = None,
) -> tuple[GeometryResourceVersionBundle, dict[GeometryResourceAssetRole, bytes]]:
    resource = GeometryResourceRecord(
        resource_id="geometry-function-1",
        resource_kind=GeometryResourceKind.MODELING_FUNCTION,
        owner_scope=GeometryResourceScope(
            scope_kind=GeometryResourceScopeKind.TASK,
            tenant_id="tenant-1",
            task_id="task-1",
        ),
        display_name="Parametric valve body",
        created_at=NOW,
    )
    version_id = "geometry-function-version-1"
    manifest = {"schema": "geometry_function_manifest_v1"}
    contents = {
        GeometryResourceAssetRole.SOURCE_BUNDLE: source_zip,
        GeometryResourceAssetRole.RUNTIME_MANIFEST: canonical_json(manifest).encode(),
        GeometryResourceAssetRole.PARAMETER_SCHEMA: canonical_json(parameter_schema).encode(),
        GeometryResourceAssetRole.VALIDATION_REPORT: b'{"valid":true}',
    }
    if preview_png is not None:
        contents[GeometryResourceAssetRole.PREVIEW] = preview_png
    formats = {
        GeometryResourceAssetRole.SOURCE_BUNDLE: (
            "source_bundle_v1",
            "source.zip",
            "application/zip",
        ),
        GeometryResourceAssetRole.RUNTIME_MANIFEST: (
            "json",
            "runtime_manifest.json",
            "application/json",
        ),
        GeometryResourceAssetRole.PARAMETER_SCHEMA: (
            "json_schema",
            "parameters.schema.json",
            "application/schema+json",
        ),
        GeometryResourceAssetRole.VALIDATION_REPORT: (
            "json",
            "validation.json",
            "application/json",
        ),
        GeometryResourceAssetRole.PREVIEW: (
            "png",
            "preview.png",
            "image/png",
        ),
    }
    assets = tuple(
        GeometryResourceVersionAssetRecord(
            resource_version_id=version_id,
            asset_role=role,
            asset_format=formats[role][0],
            filename=formats[role][1],
            content_type=formats[role][2],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=NOW,
        )
        for role, content in contents.items()
    )
    manifest_digest = sha256_digest(manifest)
    version = GeometryResourceVersionRecord(
        resource_version_id=version_id,
        resource_id=resource.resource_id,
        version_no=1,
        origin_kind="agent_generated",
        content_digest=geometry_resource_content_digest(
            resource_kind=resource.resource_kind,
            manifest_digest=manifest_digest,
            assets=assets,
        ),
        manifest_digest=manifest_digest,
        manifest=manifest,
        created_at=NOW,
    )
    return GeometryResourceVersionBundle(
        resource=resource,
        version=version,
        assets=assets,
    ), contents


def _with_declared_uncompressed_size(content: bytes, size: int) -> bytes:
    """Forge only central-directory metadata; the codec rejects before decompression."""

    forged = bytearray(content)
    central = forged.index(b"PK\x01\x02")
    struct.pack_into("<I", forged, central + 24, size)
    return bytes(forged)


def test_text_source_view_preserves_lines_and_literal_match_positions() -> None:
    service = TextSourceViewService()
    source = "alpha\r\n\tbeta alpha\r\n"

    lines = service.view(
        source,
        TextSourceView(view_mode="lines", start_line=2, line_count=1),
    )
    assert lines.view_mode == "lines"
    assert lines.lines[0].text == "\tbeta alpha\r\n"

    search = service.view(
        source,
        TextSourceView(view_mode="search", text="alpha", context_lines=0),
    )
    assert search.view_mode == "search"
    assert [(match.line, match.column) for match in search.matches] == [(1, 1), (2, 7)]

    with pytest.raises(TextSourceViewError, match="exceeds total_lines"):
        service.view(source, TextSourceView(view_mode="lines", start_line=3, line_count=1))


@pytest.mark.asyncio
async def test_catalog_issues_scope_bound_opaque_refs_and_rereads_exact_version() -> None:
    bundle = _bundle()
    resources = AsyncMock()
    resources.list_ready_resources.return_value = GeometryResourcePage(
        resources=(bundle.resource,),
        complete=True,
    )
    resources.get_latest_version.return_value = bundle
    service = GeometryCatalogService(resources, _codec())

    listed = await service.list(_context(), GeometryCatalogListQuery(), None)

    card = listed.resources[0]
    assert card.resource_root_ref.startswith("cur.")
    assert card.latest_version.resource_version_ref.startswith("cur.")
    assert "geometry-resource-1" not in card.resource_root_ref
    resources.get_exact_version.return_value = bundle

    resolved = await service.resolve_exact_version(
        _context(), card.latest_version.resource_version_ref
    )
    assert resolved == bundle
    resources.get_exact_version.assert_awaited_with(
        _context(),
        resource_id=bundle.resource.resource_id,
        resource_version_id=bundle.version.resource_version_id,
    )

    resources.get_exact_version.reset_mock()
    with pytest.raises(GeometryCatalogReferenceError) as invalid_reference:
        await service.resolve_exact_version(
            _context(task_id="task-2"), card.latest_version.resource_version_ref
        )
    assert "resource_alias" in str(invalid_reference.value)
    assert "resource_version_alias" in str(invalid_reference.value)
    assert "write, list, or info response" in str(invalid_reference.value)
    resources.get_exact_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_cursor_authenticates_filters_and_server_boundary() -> None:
    bundle = _bundle()
    resources = AsyncMock()
    resources.list_ready_resources.side_effect = (
        GeometryResourcePage(
            resources=(bundle.resource,),
            complete=False,
            next_before_created_at=bundle.resource.created_at,
            next_before_resource_id=bundle.resource.resource_id,
        ),
        GeometryResourcePage(resources=(), complete=True),
    )
    resources.get_latest_version.return_value = bundle
    service = GeometryCatalogService(resources, _codec())
    query = GeometryCatalogListQuery(
        resource_kinds=(GeometryResourceKind.MAPPED_MODEL,),
        page_size=1,
    )

    first = await service.list(_context(), query, None)
    assert first.next_cursor is not None
    second = await service.list(_context(), None, first.next_cursor)
    assert second.resources == ()
    continued = resources.list_ready_resources.await_args_list[1].kwargs["query"]
    assert continued.resource_kinds == query.resource_kinds
    assert continued.page_size == 1
    assert continued.before_resource_id == bundle.resource.resource_id


@pytest.mark.asyncio
async def test_source_bundle_lists_members_then_reads_one_exact_view() -> None:
    content = pack_source_bundle({"README.md": b"neutral\n", "src/main.py": b"x = 1\nprint(x)\n"})
    bundle = _bundle(source_zip=content)
    source_asset = next(
        item for item in bundle.assets if item.asset_role is GeometryResourceAssetRole.SOURCE_BUNDLE
    )
    catalog = AsyncMock()
    catalog.resolve_exact_version.return_value = bundle
    resources = AsyncMock()
    resources.read_exact_asset.return_value = (source_asset, content)
    service = GeometrySourceService(catalog, resources)

    listing = await service.read_source(_context(), "cur.version", None, None)
    assert [member.source_path for member in listing.members] == ["README.md", "src/main.py"]
    assert listing.view is None

    selected = await service.read_source(
        _context(),
        "cur.version",
        "src/main.py",
        TextSourceView(view_mode="search", text="print", context_lines=0),
    )
    assert selected.selected_path == "src/main.py"
    assert selected.view is not None and selected.view.view_mode == "search"
    assert selected.view.matches[0].line == 2


@pytest.mark.asyncio
async def test_source_bundle_exact_read_reuses_one_verified_storage_scan_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = pack_source_bundle({"src/main.py": b"x = 1\n"})
    bundle = _bundle(source_zip=content)
    source_asset = next(
        item for item in bundle.assets if item.asset_role is GeometryResourceAssetRole.SOURCE_BUNDLE
    )
    repository = AsyncMock()
    repository.get_exact_geometry_resource_version.return_value = bundle
    initial_storage = LocalStorage(tmp_path)
    key = GeometryResourceService.resource_storage_key(bundle, source_asset)
    await initial_storage.write_bytes(
        key,
        content,
        filename=source_asset.filename,
        content_type=source_asset.content_type,
    )
    restarted_storage = LocalStorage(tmp_path)
    original_read_chunk = restarted_storage._read_chunk
    observed_bytes = 0

    def read_chunk(descriptor: int, size: int) -> bytes:
        nonlocal observed_bytes
        chunk = original_read_chunk(descriptor, size)
        observed_bytes += len(chunk)
        return chunk

    monkeypatch.setattr(restarted_storage, "_read_chunk", read_chunk)
    catalog = AsyncMock()
    catalog.resolve_exact_version.return_value = bundle
    service = GeometrySourceService(
        catalog,
        GeometryResourceService(repository, restarted_storage),
    )

    listing = await service.read_source(_context(), "cur.version", None, None)

    assert [member.source_path for member in listing.members] == ["src/main.py"]
    assert listing.bundle_sha256 == source_asset.sha256
    assert observed_bytes == len(content)


@pytest.mark.asyncio
async def test_source_bundle_rejects_noncanonical_member_order() -> None:
    content = _source_zip((("z.py", "z\n"), ("a.py", "a\n")))
    bundle = _bundle(source_zip=content)
    source_asset = next(
        item for item in bundle.assets if item.asset_role is GeometryResourceAssetRole.SOURCE_BUNDLE
    )
    catalog = AsyncMock()
    catalog.resolve_exact_version.return_value = bundle
    resources = AsyncMock()
    resources.read_exact_asset.return_value = (source_asset, content)

    with pytest.raises(GeometrySourceBundleError, match="unique and sorted"):
        await GeometrySourceService(catalog, resources).read_source(
            _context(), "cur.version", None, None
        )


def test_source_bundle_pack_is_deterministic_and_round_trips_binary_members() -> None:
    first = pack_source_bundle(
        {"src/main.py": b"print('ok')\n", "raw/source.ifc": b"ISO-10303-21;\x00\xff"}
    )
    second = pack_source_bundle(
        {"raw/source.ifc": b"ISO-10303-21;\x00\xff", "src/main.py": b"print('ok')\n"}
    )

    assert first == second
    bundle = inspect_source_bundle(first)
    assert [member.path for member in bundle.members] == ["raw/source.ifc", "src/main.py"]
    assert [member.text_available for member in bundle.members] == [False, True]
    assert bundle.read("raw/source.ifc") == b"ISO-10303-21;\x00\xff"


def test_source_bundle_large_asset_budgets_are_coherent() -> None:
    assert MAX_SOURCE_BUNDLE_MEMBER_BYTES == 64 * 1024 * 1024
    assert MAX_SOURCE_BUNDLE_TOTAL_BYTES == 64 * 1024 * 1024
    assert MAX_SOURCE_BUNDLE_BYTES == 65 * 1024 * 1024
    assert MAX_SOURCE_BUNDLE_BYTES > MAX_SOURCE_BUNDLE_TOTAL_BYTES


def test_source_bundle_rejects_declared_member_just_over_limit(monkeypatch) -> None:
    content = pack_source_bundle({"raw/source.ifc": b"small"})
    forged = _with_declared_uncompressed_size(
        content,
        MAX_SOURCE_BUNDLE_MEMBER_BYTES + 1,
    )
    monkeypatch.setattr(
        source_bundle_codec,
        "MAX_SOURCE_BUNDLE_TOTAL_BYTES",
        MAX_SOURCE_BUNDLE_MEMBER_BYTES + 2,
    )

    with pytest.raises(SourceBundleError, match="unsupported member"):
        inspect_source_bundle(forged)


def test_source_bundle_rejects_declared_total_just_over_limit(monkeypatch) -> None:
    content = pack_source_bundle({"raw/source.ifc": b"small"})
    forged = _with_declared_uncompressed_size(
        content,
        MAX_SOURCE_BUNDLE_TOTAL_BYTES + 1,
    )
    monkeypatch.setattr(
        source_bundle_codec,
        "MAX_SOURCE_BUNDLE_MEMBER_BYTES",
        MAX_SOURCE_BUNDLE_TOTAL_BYTES + 2,
    )

    with pytest.raises(SourceBundleError, match="total member byte budget"):
        inspect_source_bundle(forged)


def test_source_bundle_rejects_zip_envelope_just_over_limit(monkeypatch) -> None:
    content = pack_source_bundle({"src/main.py": b"pass\n"})
    monkeypatch.setattr(source_bundle_codec, "MAX_SOURCE_BUNDLE_BYTES", len(content) - 1)

    with pytest.raises(SourceBundleError, match="ZIP exceeds"):
        inspect_source_bundle(content)


@pytest.mark.asyncio
async def test_source_bundle_lists_binary_ifc_but_rejects_text_view() -> None:
    content = pack_source_bundle(
        {"raw/source.ifc": b"ISO-10303-21;\x00\xff", "src/main.py": b"pass\n"}
    )
    bundle = _bundle(source_zip=content)
    source_asset = next(
        item for item in bundle.assets if item.asset_role is GeometryResourceAssetRole.SOURCE_BUNDLE
    )
    catalog = AsyncMock()
    catalog.resolve_exact_version.return_value = bundle
    resources = AsyncMock()
    resources.read_exact_asset.return_value = (source_asset, content)
    service = GeometrySourceService(catalog, resources)

    listing = await service.read_source(_context(), "cur.version", None, None)
    assert listing.members[0].source_path == "raw/source.ifc"
    assert listing.members[0].text_available is False

    with pytest.raises(GeometrySourceNotTextError) as caught:
        await service.read_source(
            _context(),
            "cur.version",
            "raw/source.ifc",
            TextSourceView(view_mode="full"),
        )
    assert caught.value.code == "GEOMETRY_SOURCE_NOT_TEXT"
