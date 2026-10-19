"""Runner entry shapes over the existing identity-free public authoring contracts.

Only this adapter owns the request wrapper and source-member spelling. Domain
validation and all JSON document decoders remain shared with the Direct owner.
"""

from typing import Annotated, ClassVar
import json

from pydantic import Field, TypeAdapter

from CAD2BIM_backend import internal_authoring_contracts as internal
from CAD2BIM_mcp.direct.ifc.geometry import authoring_contracts as geometry_write
from CAD2BIM_mcp.direct.ifc.geometry import contracts as geometry
from CAD2BIM_mcp.direct.ifc.product_family import contracts as family
from CAD2BIM_mcp.direct.ifc.product_family import write_contracts as family_write
from CAD2BIM_mcp.direct.ifc.product_family import preview_contracts as family_preview
from CAD2BIM_mcp.direct.ifc.preview_cases import parse_cases, parse_render


class GeometrySourceFile(geometry_write.IfcGeometryAuthoringSourceFile):
    source_path: Annotated[str, geometry_write.IfcGeometryAuthoringSourceFile.model_fields["source_path"], Field(alias="path")]


class GeometryLiteralEdit(geometry_write.IfcGeometryLiteralEditChange):
    source_path: Annotated[str, geometry_write.IfcGeometryLiteralEditChange.model_fields["source_path"], Field(alias="path")]


class GeometryAddMember(geometry_write.IfcGeometryAddMemberChange):
    source_path: Annotated[str, geometry_write.IfcGeometryAddMemberChange.model_fields["source_path"], Field(alias="path")]


class GeometryReplaceMember(geometry_write.IfcGeometryReplaceMemberChange):
    source_path: Annotated[str, geometry_write.IfcGeometryReplaceMemberChange.model_fields["source_path"], Field(alias="path")]


class GeometryDeleteMember(geometry_write.IfcGeometryDeleteMemberChange):
    source_path: Annotated[str, geometry_write.IfcGeometryDeleteMemberChange.model_fields["source_path"], Field(alias="path")]


GeometrySourceChange = Annotated[GeometryLiteralEdit | GeometryAddMember | GeometryReplaceMember | GeometryDeleteMember, Field(discriminator="change_kind")]


class GeometryMappedSource(geometry_write.IfcGeometryMappedDraftSource):
    source_files: Annotated[tuple[GeometrySourceFile, ...], geometry_write.IfcGeometryMappedDraftSource.model_fields["source_files"]]


class GeometryFunctionSource(geometry_write.IfcGeometryFunctionDraftSource):
    source_files: Annotated[tuple[GeometrySourceFile, ...], geometry_write.IfcGeometryFunctionDraftSource.model_fields["source_files"]]


class GeometryCreateInput(geometry_write.IfcGeometryDraftCreateInput):
    source: Annotated[GeometryMappedSource | GeometryFunctionSource, Field(discriminator="source_kind")]


class GeometryUpdateInput(geometry_write.IfcGeometryDraftUpdateInput):
    source_changes: Annotated[tuple[GeometrySourceChange, ...], geometry_write.IfcGeometryDraftUpdateInput.model_fields["source_changes"]]


GeometryWriteInput = GeometryCreateInput | geometry_write.IfcGeometryDraftPublishedCreateInput | GeometryUpdateInput
GeometryListInput = geometry.IfcGeometryResourceListFirstInput | geometry.IfcGeometryResourceListCursorInput
GeometryInfoInput = geometry.IfcGeometryResourceInfoInput
GeometryPreviewInput = Annotated[geometry.IfcGeometrySinglePreviewInput | geometry.IfcGeometryCasesPreviewInput | geometry.IfcGeometryInspectionPreviewInput | geometry.IfcFamilyStandardModelPreviewInput, Field(discriminator="mode")]
GeometryQueryInput = Annotated[geometry.IfcGeometryQueryFreshInput | geometry.IfcGeometryQueryRepairInput | geometry.IfcGeometryQueryCursorInput, Field(discriminator="mode")]


class GeometrySourceFullInput(geometry.IfcGeometryResourceSourceFullInput):
    source_path: Annotated[str, geometry.IfcGeometryResourceSourceFullInput.model_fields["source_path"], Field(alias="path")]


class GeometrySourceLinesInput(geometry.IfcGeometryResourceSourceLinesInput):
    source_path: Annotated[str, geometry.IfcGeometryResourceSourceLinesInput.model_fields["source_path"], Field(alias="path")]


class GeometrySourceSearchInput(geometry.IfcGeometryResourceSourceSearchInput):
    source_path: Annotated[str, geometry.IfcGeometryResourceSourceSearchInput.model_fields["source_path"], Field(alias="path")]


GeometrySourceReadInput = GeometrySourceFullInput | GeometrySourceLinesInput | GeometrySourceSearchInput
FamilyListInput = family.IfcProductFamilyListFirstInput | family.IfcProductFamilyListCursorInput
FamilyInfoInput = family.IfcProductFamilyInfoInput


class FamilySourceFile(family_write.IfcProductFamilyAuthoringSourceFile):
    source_path: Annotated[str, family_write.IfcProductFamilyAuthoringSourceFile.model_fields["source_path"], Field(alias="path")]


class FamilyInlineSource(family_write.IfcProductFamilyInlineDraftSource):
    source_files: Annotated[tuple[FamilySourceFile, ...], family_write.IfcProductFamilyInlineDraftSource.model_fields["source_files"]]


class FamilyCreateInput(family_write.IfcProductFamilyInlineDraftCreateInput):
    source: FamilyInlineSource


class FamilyLiteralEdit(family_write.IfcProductFamilyLiteralEditChange):
    source_path: Annotated[str, family_write.IfcProductFamilyLiteralEditChange.model_fields["source_path"], Field(alias="path")]


class FamilyAddMember(family_write.IfcProductFamilyAddMemberChange):
    source_path: Annotated[str, family_write.IfcProductFamilyAddMemberChange.model_fields["source_path"], Field(alias="path")]


class FamilyReplaceMember(family_write.IfcProductFamilyReplaceMemberChange):
    source_path: Annotated[str, family_write.IfcProductFamilyReplaceMemberChange.model_fields["source_path"], Field(alias="path")]


class FamilyDeleteMember(family_write.IfcProductFamilyDeleteMemberChange):
    source_path: Annotated[str, family_write.IfcProductFamilyDeleteMemberChange.model_fields["source_path"], Field(alias="path")]


FamilySourceChange = Annotated[FamilyLiteralEdit | FamilyAddMember | FamilyReplaceMember | FamilyDeleteMember, Field(discriminator="change_kind")]


class FamilySourceChangesDocument(family_write._ProductFamilySourceChangesDocument):
    changes: Annotated[tuple[FamilySourceChange, ...], family_write._ProductFamilySourceChangesDocument.model_fields["changes"]]


class FamilyUpdateInput(family_write.IfcProductFamilyDraftUpdateInput):
    _source_changes_document_type: ClassVar[type] = FamilySourceChangesDocument


FamilyWriteInput = FamilyCreateInput | family_write.IfcProductFamilyPublishedDraftCreateInput | FamilyUpdateInput
ProductRealizationPreviewInput = Annotated[family_preview.IfcProductRealizationSolveProviderInput | family_preview.IfcProductRealizationCasesInput, Field(discriminator="mode")]
AuthoringPublishInput = internal.AuthoringPublishInput


def authoring_domain_request(name, request):
    """Deterministically project public declarations to the existing HTTP contract."""
    value = request.model_dump(mode="json", exclude_unset=True)
    if name in {"ifc_geometry_resource_write", "ifc_product_family_write"}:
        value = request.to_domain_declaration()
    elif name in {"ifc_geometry_resource_list", "ifc_product_family_list"}:
        value = {"cursor": request.cursor} if hasattr(request, "cursor") else {"query": request.model_dump(mode="json")}
    elif name in {"ifc_geometry_resource_info", "ifc_product_family_info"}:
        target = value["target"]
        if target["target_kind"] == "draft":
            value = target
        else:
            value = {**target, "target_kind": "published"}
            if "resource_version_alias" in value:
                value["resource_alias"] = value.pop("resource_version_alias")
            if "family_version_alias" in value:
                value["family_alias"] = value.pop("family_version_alias")
    elif name == "ifc_geometry_resource_source_read":
        value = {"resource_version_alias": request.resource_version_alias, "path": request.source_path,
                 "view": {key: item for key, item in value.items() if key not in {"resource_version_alias", "source_path"}}}
    elif name == "ifc_geometry_preview" and request.mode in {"single", "cases"}:
        if request.mode == "single":
            value["parameters"] = json.loads(value.pop("sample_parameters_json", "{}"))
            profile = value.pop("preview_profile_json", None)
            value["render_profile"] = None if profile is None else json.loads(profile)
        else:
            value["cases"] = parse_cases(value.pop("cases_json"), case_kind="geometry")
            value["render"] = parse_render(value.pop("render_json", None))
    elif name == "ifc_product_realization_preview":
        if request.mode == "single":
            document, profile = family_preview.parse_product_realization_invocation(
                request.attachment_kind, request.invocation_json, request.preview_profile_json)
            value["mode"] = "single"
            value.pop("attachment_kind", None)
            value.pop("invocation_json", None)
            value.pop("preview_profile_json", None)
            value["invocation"] = {"realization_kind": request.attachment_kind, **document.model_dump(mode="json")}
            value["render_profile"] = profile
        else:
            value["cases"] = parse_cases(value.pop("cases_json"), case_kind="family")
            value["render"] = parse_render(value.pop("render_json", None))
    contract = {
        "ifc_geometry_resource_list": internal.GeometryListInput, "ifc_geometry_resource_info": internal.GeometryInfoInput,
        "ifc_geometry_resource_source_read": internal.GeometrySourceReadInput, "ifc_geometry_resource_write": internal.GeometryWriteInput,
        "ifc_geometry_preview": internal.GeometryPreviewInput, "ifc_geometry_query_python": internal.GeometryQueryInput,
        "ifc_product_family_list": internal.FamilyListInput, "ifc_product_family_info": internal.FamilyInfoInput,
        "ifc_product_family_write": internal.FamilyWriteInput, "ifc_product_realization_preview": internal.ProductRealizationPreviewInput,
        "ifc_authoring_publish": internal.AuthoringPublishInput,
    }[name]
    return TypeAdapter(contract).validate_python(value)
