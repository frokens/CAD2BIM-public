from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

import pytest

from CAD2BIM_backend.domain.ifc_model import (
    EncodedIfcRevision,
    IFC_TARGET_SCHEMA,
    IfcModelValidationError,
)
from CAD2BIM_backend.infrastructure.ifc.codec import (
    MAX_IFC_REVISION_BYTES,
    IfcOpenShellModelCodec,
)


IDENTITY_SEED = "tenant-a/run-a/model"
PROJECT_GLOBAL_ID = "1xvJu1wbjMywJMjRBpLw$1"
PROJECT_NAME = "Gas Network Project"
HEADER_TIMESTAMP = datetime(2026, 8, 30, 12, 34, 56, tzinfo=timezone.utc)


def test_durable_ifc_budget_covers_the_hash_pinned_golden_candidate() -> None:
    assert MAX_IFC_REVISION_BYTES >= 241_857_160


def _revision1() -> EncodedIfcRevision:
    return IfcOpenShellModelCodec().create_revision1(
        project_global_id=PROJECT_GLOBAL_ID,
        project_name=PROJECT_NAME,
        header_timestamp=HEADER_TIMESTAMP,
    )


def test_project_global_id_and_revision1_bytes_are_stable() -> None:
    codec = IfcOpenShellModelCodec()

    assert codec.stable_project_global_id(IDENTITY_SEED) == PROJECT_GLOBAL_ID
    assert codec.stable_project_global_id(IDENTITY_SEED) == PROJECT_GLOBAL_ID
    assert codec.stable_project_global_id("tenant-a/run-b/model") != PROJECT_GLOBAL_ID

    first = codec.create_revision1(
        project_global_id=PROJECT_GLOBAL_ID,
        project_name=PROJECT_NAME,
        header_timestamp=HEADER_TIMESTAMP,
    )
    same_instant = codec.create_revision1(
        project_global_id=PROJECT_GLOBAL_ID,
        project_name=PROJECT_NAME,
        header_timestamp=HEADER_TIMESTAMP.astimezone(timezone(timedelta(hours=8))),
    )

    assert first.content == same_instant.content
    assert first.inspection.sha256 == same_instant.inspection.sha256


def test_revision1_is_strict_ifc4x3_add2_and_contains_only_one_project() -> None:
    codec = IfcOpenShellModelCodec()
    encoded = _revision1()
    inspection = codec.inspect_revision1(
        encoded.content,
        expected_project_global_id=PROJECT_GLOBAL_ID,
        expected_project_name=PROJECT_NAME,
    )
    model = codec.private_copy(encoded.content).model

    assert inspection.schema_identifier == IFC_TARGET_SCHEMA
    assert inspection.project_global_id == PROJECT_GLOBAL_ID
    assert inspection.project_name == PROJECT_NAME
    assert len(model.by_type("IfcProject")) == 1
    assert model.by_type("IfcProject")[0].Name == PROJECT_NAME
    assert not model.by_type("IfcSite")
    assert not model.by_type("IfcUnitAssignment")
    assert not model.by_type("IfcGeometricRepresentationContext")
    assert not model.by_type("IfcProduct")
    assert model.by_type("IfcProject")[0].UnitsInContext is None
    assert inspection.geometry_basis_configured is False
    assert inspection.element_count == 0


@pytest.mark.parametrize("prefix,unit_scale", ((None, 1.0), ("MILLI", .001)))
def test_revision_inspection_records_actual_body_context_and_unit(prefix, unit_scale) -> None:
    from CAD2BIM_backend.infrastructure.ifc.geometry_basis import IfcOpenShellGeometryBasisCompiler
    from CAD2BIM_backend.domain.ifc_product_compilation import preflight_product_body_facts
    from tests.unit.backend.test_ifc_geometry_basis_compiler import _local_basis_command

    codec = IfcOpenShellModelCodec()
    configured = IfcOpenShellGeometryBasisCompiler(codec).apply(_revision1().content,
                                                               _local_basis_command(prefix=prefix))
    inspection = codec.inspect(configured.content)
    facts = inspection.product_body_contexts
    assert len(facts) == 1
    assert facts[0].model_dump() == {
        "context_type": "Model", "target_view": "MODEL_VIEW",
        "parent_entity_type": "IfcGeometricRepresentationContext",
        "parent_context_type": "Model", "parent_coordinate_space_dimension": 3,
    }
    assert inspection.project_length_unit_m == unit_scale
    body = preflight_product_body_facts(facts, inspection.geometry_basis, inspection.project_length_unit_m)
    assert body.project_length_unit_m == unit_scale


def test_body_fact_inspection_preserves_unsupported_view_and_absent_context() -> None:
    from CAD2BIM_backend.infrastructure.ifc.geometry_basis import IfcOpenShellGeometryBasisCompiler
    from CAD2BIM_backend.domain.ifc_geometry_basis import IfcRepresentationContextIncompatibleError, IfcGeometryBasisIntegrityError
    from CAD2BIM_backend.domain.ifc_product_compilation import preflight_product_body_facts
    from tests.unit.backend.test_ifc_geometry_basis_compiler import _local_basis_command

    codec = IfcOpenShellModelCodec()
    initial = _revision1()
    assert initial.inspection.product_body_contexts == ()
    assert initial.inspection.project_length_unit_m is None
    configured = IfcOpenShellGeometryBasisCompiler(codec).apply(initial.content, _local_basis_command())
    model = codec.private_copy(configured.content).model
    body = model.by_type("IfcGeometricRepresentationSubContext")[0]
    body.TargetView = "PLAN_VIEW"
    with pytest.raises(IfcGeometryBasisIntegrityError, match="Plan/2D"):
        codec.inspect(model.to_string().encode("utf-8"))
    body.TargetView = "GRAPH_VIEW"
    inspection = codec.inspect(model.to_string().encode("utf-8"))
    assert inspection.product_body_contexts[0].target_view == "GRAPH_VIEW"
    with pytest.raises(IfcRepresentationContextIncompatibleError):
        preflight_product_body_facts(inspection.product_body_contexts, inspection.geometry_basis,
                                     inspection.project_length_unit_m)


def test_private_copies_do_not_share_mutable_ifcopenshell_state() -> None:
    codec = IfcOpenShellModelCodec()
    exact_content = _revision1().content
    first = codec.private_copy(exact_content)
    second = codec.private_copy(exact_content)

    first.model.by_type("IfcProject")[0].Name = "Private mutation"

    assert first.model is not second.model
    assert codec.inspect(first.to_bytes()).project_name == "Private mutation"
    assert codec.inspect(second.to_bytes()).project_name == PROJECT_NAME
    assert second.to_bytes() == exact_content


def test_saved_file_copy_preserves_exact_inspection(tmp_path: Path) -> None:
    codec = IfcOpenShellModelCodec()
    encoded = _revision1()
    saved = tmp_path / "candidate.ifc"
    saved.write_bytes(encoded.content)
    with saved.open("rb") as stream:
        private = codec.private_copy_from_saved_file(
            encoded.content, path=saved, descriptor=stream.fileno(),
        )
    assert codec.inspect_from_model(encoded.content, private.model) == encoded.inspection


def test_source_fd_copy_preserves_unlinked_bytes_and_descriptor(tmp_path: Path) -> None:
    codec = IfcOpenShellModelCodec()
    encoded = _revision1()
    source = tmp_path / "source.ifc"
    source.write_bytes(encoded.content)
    with source.open("rb") as stream:
        identity = os.fstat(stream.fileno())
        source.unlink()
        assert stream.read() == encoded.content
        private = codec.private_copy_from_source_fd(encoded.content, descriptor=stream.fileno())
        assert os.fstat(stream.fileno()).st_ino == identity.st_ino
    # The parsed model owns its native reader and remains usable after the
    # command's original descriptor is closed.
    assert codec.inspect_from_model(encoded.content, private.model) == encoded.inspection


@pytest.mark.parametrize("invalid", ("writable", "directory", "closed", "size"))
def test_source_fd_copy_rejects_invalid_capability(tmp_path: Path, monkeypatch, invalid: str) -> None:
    from CAD2BIM_backend.infrastructure.ifc import codec as codec_module

    content = b"ISO-10303-21;"
    source = tmp_path / "source.ifc"
    source.write_bytes(content)
    descriptor = os.open(tmp_path if invalid == "directory" else source,
                         os.O_RDWR if invalid == "writable" else os.O_RDONLY)
    if invalid == "closed":
        os.close(descriptor)
    elif invalid == "size":
        content += b"\n"
    monkeypatch.setattr(codec_module.ifcopenshell, "open",
                        lambda *args, **kwargs: pytest.fail("invalid descriptor reached native parser"))
    try:
        with pytest.raises(IfcModelValidationError, match="exact IFC source"):
            IfcOpenShellModelCodec().private_copy_from_source_fd(content, descriptor=descriptor)
    finally:
        if invalid != "closed":
            os.close(descriptor)


def test_source_fd_native_failure_does_not_fall_back(tmp_path: Path, monkeypatch) -> None:
    from CAD2BIM_backend.infrastructure.ifc import codec as codec_module

    content = b"ISO-10303-21;"
    source = tmp_path / "source.ifc"
    source.write_bytes(content)
    def fail_native(*args, **kwargs):
        raise RuntimeError("native failure")
    monkeypatch.setattr(codec_module.ifcopenshell, "open", fail_native)
    monkeypatch.setattr(codec_module.ifcopenshell.file, "from_string",
                        lambda *args: pytest.fail("native failure fell back to from_string"))
    with source.open("rb") as stream, pytest.raises(IfcModelValidationError) as caught:
        IfcOpenShellModelCodec().private_copy_from_source_fd(content, descriptor=stream.fileno())
    assert str(caught.value.__cause__) == "native failure"


@pytest.mark.parametrize("mismatch", ("same_length_bytes", "other_path", "symlink", "missing_path"))
def test_saved_file_copy_rejects_bytes_or_path_not_bound_to_descriptor(
    tmp_path: Path, mismatch: str,
) -> None:
    content = _revision1().content
    saved = tmp_path / "candidate.ifc"
    saved.write_bytes(content)
    selected = saved
    if mismatch == "same_length_bytes":
        changed = content.replace(b"Gas Network Project", b"Gas Network ProjecX")
        assert len(changed) == len(content) and changed != content
        saved.write_bytes(changed)
    elif mismatch == "other_path":
        selected = tmp_path / "other.ifc"
        selected.write_bytes(content)
    elif mismatch == "symlink":
        selected = tmp_path / "linked.ifc"
        selected.symlink_to(saved)
    else:
        selected = tmp_path / "missing.ifc"
    with saved.open("rb") as stream, pytest.raises(IfcModelValidationError):
        IfcOpenShellModelCodec().private_copy_from_saved_file(
            content, path=selected, descriptor=stream.fileno(),
        )


def test_revision1_validation_rejects_changed_project_name() -> None:
    codec = IfcOpenShellModelCodec()
    private = codec.private_copy(_revision1().content)
    private.model.by_type("IfcProject")[0].Name = "Changed Project"

    with pytest.raises(IfcModelValidationError, match="IfcProject Name changed"):
        codec.inspect_revision1(
            private.to_bytes(),
            expected_project_global_id=PROJECT_GLOBAL_ID,
            expected_project_name=PROJECT_NAME,
        )


def test_element_count_excludes_non_element_ifc_products() -> None:
    codec = IfcOpenShellModelCodec()
    private = codec.private_copy(_revision1().content)
    private.model.create_entity("IfcSite", GlobalId="0AAAAAAAAAAAAAAAAAAAAA")
    private.model.create_entity("IfcDistributionPort", GlobalId="0BBBBBBBBBBBBBBBBBBBBB")
    private.model.create_entity("IfcAnnotation", GlobalId="0CCCCCCCCCCCCCCCCCCCCC")
    private.model.create_entity("IfcPipeSegment", GlobalId="0DDDDDDDDDDDDDDDDDDDDD")

    inspection = codec.inspect(private.to_bytes())

    assert len(private.model.by_type("IfcProduct")) == 4
    assert inspection.site_count == 1
    assert inspection.element_count == 1


@pytest.mark.parametrize(
    "content",
    (
        b"not-an-ifc-spf-payload",
        b"ISO-10303-21;\n\xff",
    ),
)
def test_codec_rejects_damaged_spf_bytes(content: bytes, tmp_path: Path) -> None:
    with pytest.raises(IfcModelValidationError):
        IfcOpenShellModelCodec().inspect(content)
    saved = tmp_path / "candidate.ifc"
    saved.write_bytes(content)
    with saved.open("rb") as stream, pytest.raises(IfcModelValidationError):
        IfcOpenShellModelCodec().private_copy_from_saved_file(
            content, path=saved, descriptor=stream.fileno(),
        )
    with saved.open("rb") as stream, pytest.raises(IfcModelValidationError):
        IfcOpenShellModelCodec().private_copy_from_source_fd(content, descriptor=stream.fileno())


def test_codec_rejects_valid_spf_with_wrong_schema() -> None:
    content = _revision1().content.replace(b"IFC4X3_ADD2", b"IFC4")

    with pytest.raises(IfcModelValidationError, match="IFC4X3_ADD2"):
        IfcOpenShellModelCodec().inspect(content)
