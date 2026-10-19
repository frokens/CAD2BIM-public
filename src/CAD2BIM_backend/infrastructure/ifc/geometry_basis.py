"""Strict IfcOpenShell compiler for typed project geometry-basis changes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from CAD2BIM_backend.domain.ifc_geometry_basis import (
    ConfigureIfcGeometryBasisCommand,
    CreateParentContextOperation,
    CreateSubcontextOperation,
    IfcGeometryBasisBusinessError,
    IfcGeometryBasisCompilationError,
    IfcGeometryBasisIntegrityError,
    IfcGeometryBasisNoChangeError,
    IfcGeometryBasisSummary,
    IfcLengthUnit,
    IfcLocalCoordinateReferenceChange,
    IfcLocalCoordinateReferenceSummary,
    IfcPlaneAngleUnit,
    IfcProductBodyContextFacts,
    IfcProjectedCrsCoordinateReferenceChange,
    IfcProjectedCrsCoordinateReferenceSummary,
    IfcRepresentationContextAlreadyExistsError,
    IfcRepresentationContextCatalog,
    IfcRepresentationContextIncompatibleError,
    IfcRepresentationContextNotFoundError,
    IfcRepresentationContextSummary,
    IfcSiUnitSummary,
    IfcWorldCoordinateSystem2D,
    IfcWorldCoordinateSystem3D,
    MAX_IFC_REPRESENTATION_CONTEXTS,
    UpdateParentContextOperation,
    UpdateSubcontextOperation,
)
from CAD2BIM_backend.domain.ifc_model import EncodedIfcRevision
from CAD2BIM_backend.infrastructure.ifc.codec import IfcOpenShellModelCodec


_PARENT_CLASS = "IfcGeometricRepresentationContext"
_SUBCONTEXT_CLASS = "IfcGeometricRepresentationSubContext"


@dataclass(frozen=True, slots=True)
class _ContextState:
    kind: str
    context_type: str
    dimension: int
    parent_identifier: str | None = None
    target_view: str | None = None
    user_defined_target_view: str | None = None


class IfcOpenShellGeometryBasisCompiler:
    """Compile explicit unit/CRS/context choices on one private IFC copy."""

    def __init__(self, codec: IfcOpenShellModelCodec | None = None) -> None:
        self._codec = codec or IfcOpenShellModelCodec()

    def apply(
        self,
        base_content: bytes,
        command: ConfigureIfcGeometryBasisCommand,
    ) -> EncodedIfcRevision:
        if not isinstance(command, ConfigureIfcGeometryBasisCommand):
            raise TypeError("command must be ConfigureIfcGeometryBasisCommand")
        if (
            command.project_unit_change is None
            and command.coordinate_reference_change is None
            and not command.context_operations
        ):
            raise IfcGeometryBasisNoChangeError("geometry basis command is empty")

        private = self._codec.private_copy(base_content)
        model = private.model
        try:
            project = _require_project(model)
            _inspect_project_units(model, project)
            existing_contexts = _require_context_entities(model, project)
            current_coordinate_reference = _require_coordinate_reference(
                model,
                existing_contexts,
                allow_absent=True,
            )
            _preflight_context_operations(
                existing_contexts,
                command,
                current_coordinate_reference=current_coordinate_reference,
            )

            if command.project_unit_change is not None:
                _apply_project_unit_change(model, project, command.project_unit_change)
            _apply_context_operations(model, project, command)
            if command.coordinate_reference_change is not None:
                _apply_coordinate_reference_change(
                    model,
                    project,
                    command.coordinate_reference_change,
                )

            content = private.to_bytes()
            if content == base_content:
                raise IfcGeometryBasisNoChangeError(
                    "geometry basis command does not change the exact IFC revision"
                )
            inspection = self._codec.inspect(content)
            self.inspect_contexts(content)
            self.inspect_geometry_basis(content)
            return EncodedIfcRevision(content=content, inspection=inspection)
        except (IfcGeometryBasisBusinessError, IfcGeometryBasisIntegrityError):
            raise
        except Exception as exc:
            raise IfcGeometryBasisCompilationError(
                "typed geometry basis could not be compiled"
            ) from exc

    @staticmethod
    def inspect_product_body_contexts_from_model(model: Any) -> tuple[IfcProductBodyContextFacts, ...]:
        facts = []
        for context in model.by_type("IfcGeometricRepresentationSubContext"):
            if context.ContextIdentifier != "Body":
                continue
            parent = context.ParentContext
            facts.append(IfcProductBodyContextFacts(
                context_type=context.ContextType,
                target_view=context.TargetView,
                parent_entity_type=parent.is_a() if parent is not None else None,
                parent_context_type=getattr(parent, "ContextType", None),
                parent_coordinate_space_dimension=getattr(parent, "CoordinateSpaceDimension", None),
            ))
        return tuple(facts)

    def inspect_geometry_basis(
        self,
        content: bytes,
    ) -> IfcGeometryBasisSummary | None:
        return self.inspect_geometry_basis_from_model(self._codec.private_copy(content).model)

    def inspect_geometry_basis_from_model(self, model: Any) -> IfcGeometryBasisSummary | None:
        try:
            project = _require_project(model)
            units = _inspect_project_units(model, project)
            contexts = _require_context_entities(model, project)
            model_contexts = [
                entity
                for identifier, entity in contexts.items()
                if _is_exact_parent(entity)
                and entity.ContextType == "Model"
                and entity.CoordinateSpaceDimension == 3
            ]
            if units is None and not contexts:
                _require_coordinate_reference(model, contexts, allow_absent=True)
                return None
            if (
                units is None
                or "LENGTHUNIT" not in units
                or "PLANEANGLEUNIT" not in units
                or len(model_contexts) != 1
            ):
                _require_coordinate_reference(model, contexts, allow_absent=True)
                return None
            model_context = model_contexts[0]
            coordinate_reference = _require_coordinate_reference(
                model,
                contexts,
                allow_absent=True,
            )
            return IfcGeometryBasisSummary(
                length_unit=_unit_summary(units["LENGTHUNIT"]),
                plane_angle_unit=_unit_summary(units["PLANEANGLEUNIT"]),
                precision=float(model_context.Precision),
                coordinate_reference=coordinate_reference,
                main_context_identifier=str(model_context.ContextIdentifier),
                main_context_type="Model",
                coordinate_space_dimension=3,
            )
        except IfcGeometryBasisIntegrityError:
            raise
        except Exception as exc:
            raise IfcGeometryBasisIntegrityError(
                "IFC geometry basis could not be inspected"
            ) from exc

    def inspect_contexts(
        self,
        content: bytes,
    ) -> IfcRepresentationContextCatalog:
        return self.inspect_contexts_from_model(self._codec.private_copy(content).model)

    def inspect_contexts_from_model(self, model: Any) -> IfcRepresentationContextCatalog:
        try:
            project = _require_project(model)
            contexts = _require_context_entities(model, project)
            summaries: list[IfcRepresentationContextSummary] = []
            representations = tuple(model.by_type("IfcRepresentation"))
            for identifier in sorted(contexts):
                context = contexts[identifier]
                representation_count = sum(
                    1
                    for representation in representations
                    if representation.ContextOfItems == context
                )
                if _is_exact_parent(context):
                    summaries.append(
                        IfcRepresentationContextSummary(
                            context_identifier=identifier,
                            kind="parent_context",
                            context_type=str(context.ContextType),
                            coordinate_space_dimension=int(context.CoordinateSpaceDimension),
                            representation_count=representation_count,
                            precision=float(context.Precision),
                            world_coordinate_system=_read_world_coordinate_system(
                                context.WorldCoordinateSystem
                            ),
                            true_north=_read_optional_direction(
                                context.TrueNorth,
                                expected_dimension=2,
                            ),
                        )
                    )
                else:
                    parent = context.ParentContext
                    summaries.append(
                        IfcRepresentationContextSummary(
                            context_identifier=identifier,
                            kind="subcontext",
                            context_type=str(context.ContextType),
                            coordinate_space_dimension=int(context.CoordinateSpaceDimension),
                            parent_context_identifier=str(parent.ContextIdentifier),
                            target_view=str(context.TargetView),
                            target_scale=(
                                None if context.TargetScale is None else float(context.TargetScale)
                            ),
                            user_defined_target_view=(
                                None
                                if context.UserDefinedTargetView is None
                                else str(context.UserDefinedTargetView)
                            ),
                            representation_count=representation_count,
                        )
                    )
            return IfcRepresentationContextCatalog(contexts=tuple(summaries))
        except IfcGeometryBasisIntegrityError:
            raise
        except Exception as exc:
            raise IfcGeometryBasisIntegrityError(
                "representation contexts could not be inspected"
            ) from exc


def _require_project(model: Any) -> Any:
    projects = tuple(model.by_type("IfcProject"))
    if len(projects) != 1:
        raise IfcGeometryBasisIntegrityError("IFC revision must contain exactly one IfcProject")
    return projects[0]


def _inspect_project_units(model: Any, project: Any) -> dict[str, Any] | None:
    assignments = tuple(model.by_type("IfcUnitAssignment"))
    assignment = project.UnitsInContext
    if assignment is None:
        if assignments:
            raise IfcGeometryBasisIntegrityError(
                "IfcUnitAssignment must be referenced directly by IfcProject"
            )
        return None
    if len(assignments) != 1 or assignments[0] != assignment:
        raise IfcGeometryBasisIntegrityError(
            "IfcProject must reference the unique IfcUnitAssignment"
        )
    by_type: dict[str, Any] = {}
    for unit in tuple(assignment.Units or ()):
        unit_type = str(getattr(unit, "UnitType", "") or "")
        if unit_type in by_type:
            raise IfcGeometryBasisIntegrityError(
                f"IfcUnitAssignment contains duplicate {unit_type or 'unknown'}"
            )
        by_type[unit_type] = unit
    for unit_type, expected_name in (
        ("LENGTHUNIT", "METRE"),
        ("PLANEANGLEUNIT", "RADIAN"),
    ):
        unit = by_type.get(unit_type)
        if unit is None:
            continue
        if str(unit.is_a()) != "IfcSIUnit":
            raise IfcGeometryBasisIntegrityError(f"{unit_type} must be represented by IfcSIUnit")
        if str(unit.Name) != expected_name:
            raise IfcGeometryBasisIntegrityError(f"{unit_type} must use {expected_name}")
    return by_type


def _require_context_entities(model: Any, project: Any) -> dict[str, Any]:
    entities = tuple(model.by_type(_PARENT_CLASS))
    if len(entities) > MAX_IFC_REPRESENTATION_CONTEXTS:
        raise IfcGeometryBasisIntegrityError(
            "IFC representation context catalog exceeds the bounded limit"
        )
    contexts: dict[str, Any] = {}
    parents: list[Any] = []
    for entity in entities:
        identifier = str(getattr(entity, "ContextIdentifier", "") or "").strip()
        if not identifier:
            raise IfcGeometryBasisIntegrityError(
                "every representation context requires ContextIdentifier"
            )
        if identifier in contexts:
            raise IfcGeometryBasisIntegrityError(
                "ContextIdentifier must be unique across the model"
            )
        contexts[identifier] = entity
        if _is_exact_parent(entity):
            parents.append(entity)

    project_parents = tuple(project.RepresentationContexts or ())
    if set(parents) != set(project_parents) or any(
        not _is_exact_parent(entity) for entity in project_parents
    ):
        raise IfcGeometryBasisIntegrityError(
            "IfcProject must directly reference every and only parent context"
        )
    dimensions: set[int] = set()
    for parent in parents:
        context_type = str(parent.ContextType or "")
        dimension = int(parent.CoordinateSpaceDimension)
        if context_type not in {"Model", "Plan"}:
            raise IfcGeometryBasisIntegrityError("unsupported parent ContextType")
        if (context_type, dimension) not in {("Model", 3), ("Plan", 2)}:
            raise IfcGeometryBasisIntegrityError(
                "parent ContextType and dimension are incompatible"
            )
        if dimension in dimensions:
            raise IfcGeometryBasisIntegrityError(
                "IFC profile permits at most one parent per dimension"
            )
        dimensions.add(dimension)
        if parent.Precision is None or float(parent.Precision) <= 0.0:
            raise IfcGeometryBasisIntegrityError("parent context requires precision")
        _read_world_coordinate_system(parent.WorldCoordinateSystem)
        _read_optional_direction(parent.TrueNorth, expected_dimension=2)

    for entity in entities:
        if _is_exact_parent(entity):
            continue
        if str(entity.is_a()) != _SUBCONTEXT_CLASS:
            raise IfcGeometryBasisIntegrityError("unsupported representation context subtype")
        parent = entity.ParentContext
        if parent not in parents:
            raise IfcGeometryBasisIntegrityError(
                "subcontext must reference a project parent context"
            )
        _validate_subcontext_compatibility(
            str(entity.ContextIdentifier),
            str(entity.ContextType),
            str(parent.ContextType),
            int(parent.CoordinateSpaceDimension),
            str(entity.TargetView),
            integrity=True,
        )
        if (
            str(entity.TargetView) == "USERDEFINED"
            and not str(entity.UserDefinedTargetView or "").strip()
        ):
            raise IfcGeometryBasisIntegrityError(
                "USERDEFINED subcontext requires UserDefinedTargetView"
            )
        if str(entity.TargetView) != "USERDEFINED" and entity.UserDefinedTargetView is not None:
            raise IfcGeometryBasisIntegrityError(
                "UserDefinedTargetView is only valid for USERDEFINED"
            )
    return contexts


def _preflight_context_operations(
    existing: dict[str, Any],
    command: ConfigureIfcGeometryBasisCommand,
    *,
    current_coordinate_reference: (
        IfcLocalCoordinateReferenceSummary | IfcProjectedCrsCoordinateReferenceSummary
    ),
) -> None:
    states = {identifier: _state_from_entity(entity) for identifier, entity in existing.items()}
    created: set[str] = set()
    for operation in _ordered_context_operations(command):
        identifier = operation.context_identifier
        if isinstance(operation, (CreateParentContextOperation, CreateSubcontextOperation)):
            if identifier in states or identifier in created:
                raise IfcRepresentationContextAlreadyExistsError(identifier)
            created.add(identifier)
            if isinstance(operation, CreateParentContextOperation):
                states[identifier] = _ContextState(
                    "parent_context",
                    operation.context_type,
                    operation.coordinate_space_dimension,
                )
            else:
                parent = states.get(operation.parent_context_identifier)
                if parent is None:
                    raise IfcRepresentationContextNotFoundError(operation.parent_context_identifier)
                if parent.kind != "parent_context":
                    raise IfcRepresentationContextIncompatibleError(
                        operation.parent_context_identifier,
                        "subcontext parent must be a parent context",
                    )
                states[identifier] = _ContextState(
                    "subcontext",
                    operation.context_type,
                    parent.dimension,
                    operation.parent_context_identifier,
                    operation.target_view,
                    operation.user_defined_target_view,
                )
            continue

        current = states.get(identifier)
        if current is None:
            raise IfcRepresentationContextNotFoundError(identifier)
        if isinstance(operation, UpdateParentContextOperation):
            if current.kind != "parent_context":
                raise IfcRepresentationContextIncompatibleError(
                    identifier, "update_parent_context selected a subcontext"
                )
            dimension = operation.coordinate_space_dimension or current.dimension
            if (
                operation.world_coordinate_system is not None
                and (2 if operation.world_coordinate_system.kind == "2d" else 3) != dimension
            ):
                raise IfcRepresentationContextIncompatibleError(
                    identifier,
                    "WorldCoordinateSystem dimension must match the parent context",
                )
            states[identifier] = _ContextState(
                current.kind,
                operation.context_type or current.context_type,
                dimension,
            )
        else:
            if current.kind != "subcontext":
                raise IfcRepresentationContextIncompatibleError(
                    identifier, "update_subcontext selected a parent context"
                )
            parent_identifier = (
                operation.parent_context_identifier
                if "parent_context_identifier" in operation.model_fields_set
                else current.parent_identifier
            )
            parent = states.get(parent_identifier or "")
            if parent is None:
                raise IfcRepresentationContextNotFoundError(parent_identifier or "")
            if parent.kind != "parent_context":
                raise IfcRepresentationContextIncompatibleError(
                    parent_identifier or "", "subcontext parent must be a parent context"
                )
            states[identifier] = _ContextState(
                current.kind,
                operation.context_type or current.context_type,
                parent.dimension,
                parent_identifier,
                operation.target_view or current.target_view,
                (
                    operation.user_defined_target_view
                    if "user_defined_target_view" in operation.model_fields_set
                    else current.user_defined_target_view
                ),
            )

    parent_dimensions: set[int] = set()
    if len(states) > MAX_IFC_REPRESENTATION_CONTEXTS:
        identifier = next(reversed(states))
        raise IfcRepresentationContextIncompatibleError(
            identifier,
            f"model permits at most {MAX_IFC_REPRESENTATION_CONTEXTS} representation contexts",
        )
    for identifier, state in states.items():
        if state.kind == "parent_context":
            if (state.context_type, state.dimension) not in {("Model", 3), ("Plan", 2)}:
                raise IfcRepresentationContextIncompatibleError(
                    identifier, "parent ContextType and dimension do not match"
                )
            if state.dimension in parent_dimensions:
                raise IfcRepresentationContextIncompatibleError(
                    identifier, "only one parent context per dimension is allowed"
                )
            parent_dimensions.add(state.dimension)
            continue
        parent = states.get(state.parent_identifier or "")
        if parent is None or parent.kind != "parent_context":
            raise IfcRepresentationContextNotFoundError(state.parent_identifier or "")
        _validate_subcontext_compatibility(
            identifier,
            state.context_type,
            parent.context_type,
            parent.dimension,
            state.target_view or "",
            integrity=False,
        )
        if (state.target_view == "USERDEFINED") != (state.user_defined_target_view is not None):
            raise IfcRepresentationContextIncompatibleError(
                identifier,
                "USERDEFINED target view and UserDefinedTargetView must be set together",
            )

    coordinate = command.coordinate_reference_change
    source_identifier: str | None = None
    if isinstance(coordinate, IfcProjectedCrsCoordinateReferenceChange):
        source_identifier = coordinate.source_context_identifier
    elif coordinate is None and isinstance(
        current_coordinate_reference,
        IfcProjectedCrsCoordinateReferenceSummary,
    ):
        source_identifier = current_coordinate_reference.source_context_identifier
    if source_identifier is not None:
        context = states.get(source_identifier)
        if context is None:
            raise IfcRepresentationContextNotFoundError(source_identifier)
        if context.kind != "parent_context" or (
            context.context_type,
            context.dimension,
        ) != ("Model", 3):
            raise IfcRepresentationContextIncompatibleError(
                source_identifier,
                "projected CRS requires the Model/3D parent context",
            )


def _apply_project_unit_change(model: Any, project: Any, change: Any) -> None:
    existing = _inspect_project_units(model, project)
    if existing is None:
        units = []
        if change.length_unit is not None:
            units.append(_create_si_unit(model, "LENGTHUNIT", change.length_unit))
        if change.plane_angle_unit is not None:
            units.append(_create_si_unit(model, "PLANEANGLEUNIT", change.plane_angle_unit))
        project.UnitsInContext = model.create_entity("IfcUnitAssignment", Units=units)
        return
    if change.length_unit is not None:
        length = existing.get("LENGTHUNIT")
        if length is None:
            length = _create_si_unit(model, "LENGTHUNIT", change.length_unit)
            project.UnitsInContext.Units = tuple(project.UnitsInContext.Units) + (length,)
        else:
            _update_si_unit(length, change.length_unit)
    if change.plane_angle_unit is not None:
        angle = existing.get("PLANEANGLEUNIT")
        if angle is None:
            angle = _create_si_unit(model, "PLANEANGLEUNIT", change.plane_angle_unit)
            project.UnitsInContext.Units = tuple(project.UnitsInContext.Units) + (angle,)
        else:
            _update_si_unit(angle, change.plane_angle_unit)


def _create_si_unit(model: Any, unit_type: str, unit: IfcLengthUnit | IfcPlaneAngleUnit) -> Any:
    return model.create_entity(
        "IfcSIUnit",
        UnitType=unit_type,
        Prefix=unit.prefix,
        Name=unit.name,
    )


def _update_si_unit(entity: Any, unit: IfcLengthUnit | IfcPlaneAngleUnit) -> None:
    entity.Prefix = unit.prefix
    entity.Name = unit.name


def _apply_context_operations(
    model: Any, project: Any, command: ConfigureIfcGeometryBasisCommand
) -> None:
    contexts = _require_context_entities(model, project)
    for operation in _ordered_context_operations(command):
        if isinstance(operation, CreateParentContextOperation):
            context = model.create_entity(
                _PARENT_CLASS,
                ContextIdentifier=operation.context_identifier,
                ContextType=operation.context_type,
                CoordinateSpaceDimension=operation.coordinate_space_dimension,
                Precision=operation.precision,
                WorldCoordinateSystem=_create_world_coordinate_system(
                    model, operation.world_coordinate_system
                ),
                TrueNorth=_create_optional_direction(model, operation.true_north),
            )
            project.RepresentationContexts = tuple(project.RepresentationContexts or ()) + (
                context,
            )
            contexts[operation.context_identifier] = context
        elif isinstance(operation, UpdateParentContextOperation):
            context = contexts[operation.context_identifier]
            _update_parent_context(model, context, operation)
        elif isinstance(operation, CreateSubcontextOperation):
            context = model.create_entity(
                _SUBCONTEXT_CLASS,
                ContextIdentifier=operation.context_identifier,
                ContextType=operation.context_type,
                ParentContext=contexts[operation.parent_context_identifier],
                TargetScale=operation.target_scale,
                TargetView=operation.target_view,
                UserDefinedTargetView=operation.user_defined_target_view,
            )
            contexts[operation.context_identifier] = context
        else:
            context = contexts[operation.context_identifier]
            _update_subcontext(context, contexts, operation)


def _update_parent_context(
    model: Any, context: Any, operation: UpdateParentContextOperation
) -> None:
    fields = operation.model_fields_set
    if "context_type" in fields:
        context.ContextType = operation.context_type
    if "coordinate_space_dimension" in fields:
        context.CoordinateSpaceDimension = operation.coordinate_space_dimension
    if "precision" in fields:
        context.Precision = operation.precision
    if "world_coordinate_system" in fields:
        old = context.WorldCoordinateSystem
        context.WorldCoordinateSystem = _create_world_coordinate_system(
            model, operation.world_coordinate_system
        )
        _remove_placement_tree_if_unreferenced(model, old)
    if "true_north" in fields:
        old = context.TrueNorth
        context.TrueNorth = _create_optional_direction(model, operation.true_north)
        _remove_if_unreferenced(model, old)


def _update_subcontext(
    context: Any,
    contexts: dict[str, Any],
    operation: UpdateSubcontextOperation,
) -> None:
    fields = operation.model_fields_set
    if "context_type" in fields:
        context.ContextType = operation.context_type
    if "parent_context_identifier" in fields:
        context.ParentContext = contexts[operation.parent_context_identifier]
    if "target_view" in fields:
        context.TargetView = operation.target_view
    if "target_scale" in fields:
        context.TargetScale = operation.target_scale
    if "user_defined_target_view" in fields:
        context.UserDefinedTargetView = operation.user_defined_target_view


def _apply_coordinate_reference_change(model: Any, project: Any, change: Any) -> None:
    contexts = _require_context_entities(model, project)
    if isinstance(change, IfcLocalCoordinateReferenceChange):
        _remove_coordinate_reference(model)
        return
    source_context = contexts[change.source_context_identifier]
    operations = tuple(model.by_type("IfcCoordinateOperation"))
    if len(operations) > 1:
        raise IfcGeometryBasisIntegrityError("IFC profile permits at most one coordinate operation")
    if operations:
        operation = operations[0]
        if str(operation.is_a()) != "IfcMapConversion":
            raise IfcGeometryBasisIntegrityError("unsupported coordinate operation")
        target = operation.TargetCRS
        if str(target.is_a()) != "IfcProjectedCRS":
            raise IfcGeometryBasisIntegrityError("IfcMapConversion must target IfcProjectedCRS")
    else:
        target = model.create_entity("IfcProjectedCRS", Name=change.crs_name)
        operation = model.create_entity(
            "IfcMapConversion",
            SourceCRS=source_context,
            TargetCRS=target,
            Eastings=change.eastings,
            Northings=change.northings,
            OrthogonalHeight=change.orthogonal_height,
            XAxisAbscissa=change.x_axis_abscissa,
            XAxisOrdinate=change.x_axis_ordinate,
            Scale=change.scale,
        )
    target.Name = change.crs_name
    target.Description = change.description
    target.GeodeticDatum = change.geodetic_datum
    target.VerticalDatum = change.vertical_datum
    target.MapProjection = change.map_projection
    target.MapZone = change.map_zone
    _apply_projected_map_unit(model, project, target, change.map_unit)
    operation.SourceCRS = source_context
    operation.TargetCRS = target
    operation.Eastings = change.eastings
    operation.Northings = change.northings
    operation.OrthogonalHeight = change.orthogonal_height
    operation.XAxisAbscissa = change.x_axis_abscissa
    operation.XAxisOrdinate = change.x_axis_ordinate
    operation.Scale = change.scale


def _ordered_context_operations(
    command: ConfigureIfcGeometryBasisCommand,
) -> tuple[
    CreateParentContextOperation
    | CreateSubcontextOperation
    | UpdateParentContextOperation
    | UpdateSubcontextOperation,
    ...,
]:
    """Apply the requested final graph without making array order a dependency contract."""

    operations = command.context_operations
    return (
        *(item for item in operations if isinstance(item, CreateParentContextOperation)),
        *(item for item in operations if isinstance(item, CreateSubcontextOperation)),
        *(
            item
            for item in operations
            if isinstance(item, (UpdateParentContextOperation, UpdateSubcontextOperation))
        ),
    )


def _apply_projected_map_unit(
    model: Any,
    project: Any,
    target: Any,
    unit: IfcLengthUnit | None,
) -> None:
    """Keep MapUnit independent from project units without churning equal entities."""

    old_map_unit = target.MapUnit
    if unit is None:
        target.MapUnit = None
        _remove_if_unreferenced(model, old_map_unit)
        return

    project_units = _inspect_project_units(model, project)
    project_length_unit = (
        None if project_units is None else project_units.get("LENGTHUNIT")
    )
    if old_map_unit is None or old_map_unit == project_length_unit:
        target.MapUnit = _create_si_unit(model, "LENGTHUNIT", unit)
        _remove_if_unreferenced(model, old_map_unit)
        return
    _update_si_unit(old_map_unit, unit)


def _remove_coordinate_reference(model: Any) -> None:
    operations = tuple(model.by_type("IfcCoordinateOperation"))
    targets = [getattr(operation, "TargetCRS", None) for operation in operations]
    map_units = [
        getattr(target, "MapUnit", None)
        for target in targets
        if target is not None
    ]
    for operation in operations:
        model.remove(operation)
    for target in targets:
        _remove_if_unreferenced(model, target)
    for map_unit in map_units:
        _remove_if_unreferenced(model, map_unit)
    projected = tuple(model.by_type("IfcProjectedCRS"))
    if projected:
        raise IfcGeometryBasisIntegrityError("local mode cannot retain an orphan IfcProjectedCRS")


def _require_coordinate_reference(
    model: Any,
    contexts: dict[str, Any],
    *,
    allow_absent: bool,
) -> IfcLocalCoordinateReferenceSummary | IfcProjectedCrsCoordinateReferenceSummary:
    operations = tuple(model.by_type("IfcCoordinateOperation"))
    projected = tuple(model.by_type("IfcProjectedCRS"))
    if not operations:
        if projected:
            raise IfcGeometryBasisIntegrityError("IfcProjectedCRS must be used by IfcMapConversion")
        if allow_absent:
            return IfcLocalCoordinateReferenceSummary()
        raise IfcGeometryBasisIntegrityError("coordinate reference is missing")
    if len(operations) != 1 or len(projected) != 1:
        raise IfcGeometryBasisIntegrityError(
            "projected mode requires one MapConversion and one ProjectedCRS"
        )
    operation = operations[0]
    target = projected[0]
    if str(operation.is_a()) != "IfcMapConversion" or operation.TargetCRS != target:
        raise IfcGeometryBasisIntegrityError("unsupported coordinate reference structure")
    source = operation.SourceCRS
    identifier = str(getattr(source, "ContextIdentifier", "") or "")
    if contexts.get(identifier) != source or not _is_exact_parent(source):
        raise IfcGeometryBasisIntegrityError(
            "MapConversion SourceCRS must be a project parent context"
        )
    if str(source.ContextType) != "Model" or int(source.CoordinateSpaceDimension) != 3:
        raise IfcGeometryBasisIntegrityError("MapConversion SourceCRS must be Model/3D")
    map_unit = target.MapUnit
    map_unit_summary = None if map_unit is None else _unit_summary(map_unit)
    if map_unit_summary is not None and map_unit_summary.name != "METRE":
        raise IfcGeometryBasisIntegrityError("ProjectedCRS MapUnit must be METRE")
    project = _require_project(model)
    project_units = tuple(
        () if project.UnitsInContext is None else project.UnitsInContext.Units or ()
    )
    if map_unit is not None and map_unit in project_units:
        raise IfcGeometryBasisIntegrityError(
            "ProjectedCRS MapUnit must be independent from project units"
        )
    return IfcProjectedCrsCoordinateReferenceSummary(
        source_context_identifier=identifier,
        crs_name=str(target.Name),
        description=_optional_string(target.Description),
        geodetic_datum=_optional_string(target.GeodeticDatum),
        vertical_datum=_optional_string(target.VerticalDatum),
        map_projection=_optional_string(target.MapProjection),
        map_zone=_optional_string(target.MapZone),
        map_unit=map_unit_summary,
        eastings=float(operation.Eastings),
        northings=float(operation.Northings),
        orthogonal_height=float(operation.OrthogonalHeight),
        x_axis_abscissa=float(operation.XAxisAbscissa),
        x_axis_ordinate=float(operation.XAxisOrdinate),
        scale=float(operation.Scale),
    )


def _state_from_entity(entity: Any) -> _ContextState:
    if _is_exact_parent(entity):
        return _ContextState(
            "parent_context",
            str(entity.ContextType),
            int(entity.CoordinateSpaceDimension),
        )
    parent = entity.ParentContext
    return _ContextState(
        "subcontext",
        str(entity.ContextType),
        int(parent.CoordinateSpaceDimension),
        str(parent.ContextIdentifier),
        str(entity.TargetView),
        _optional_string(entity.UserDefinedTargetView),
    )


def _validate_subcontext_compatibility(
    identifier: str,
    context_type: str,
    parent_type: str,
    parent_dimension: int,
    target_view: str,
    *,
    integrity: bool,
) -> None:
    reason = None
    if context_type != parent_type:
        reason = "SubContext ContextType must match ParentContext"
    elif target_view in {"MODEL_VIEW", "GRAPH_VIEW", "ELEVATION_VIEW", "SECTION_VIEW"} and (
        parent_type,
        parent_dimension,
    ) != ("Model", 3):
        reason = f"{target_view} requires a Model/3D parent"
    elif target_view in {"PLAN_VIEW", "REFLECTED_PLAN_VIEW", "SKETCH_VIEW"} and (
        parent_type,
        parent_dimension,
    ) != ("Plan", 2):
        reason = f"{target_view} requires a Plan/2D parent"
    if reason is None:
        return
    if integrity:
        raise IfcGeometryBasisIntegrityError(
            f"representation context {identifier!r} is incompatible: {reason}"
        )
    raise IfcRepresentationContextIncompatibleError(identifier, reason)


def _create_world_coordinate_system(model: Any, world: Any) -> Any:
    if isinstance(world, IfcWorldCoordinateSystem2D):
        return model.create_entity(
            "IfcAxis2Placement2D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=world.origin),
            RefDirection=model.create_entity("IfcDirection", DirectionRatios=world.ref_direction),
        )
    if isinstance(world, IfcWorldCoordinateSystem3D):
        return model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=world.origin),
            Axis=model.create_entity("IfcDirection", DirectionRatios=world.axis),
            RefDirection=model.create_entity("IfcDirection", DirectionRatios=world.ref_direction),
        )
    raise IfcGeometryBasisCompilationError("unsupported world coordinate system")


def _read_world_coordinate_system(world: Any) -> Any:
    if world is None:
        raise IfcGeometryBasisIntegrityError("parent context requires WorldCoordinateSystem")
    kind = str(world.is_a())
    if kind == "IfcAxis2Placement2D":
        return IfcWorldCoordinateSystem2D(
            origin=_coordinates(world.Location, 2),
            ref_direction=_direction(world.RefDirection, 2),
        )
    if kind == "IfcAxis2Placement3D":
        return IfcWorldCoordinateSystem3D(
            origin=_coordinates(world.Location, 3),
            axis=_direction(world.Axis, 3),
            ref_direction=_direction(world.RefDirection, 3),
        )
    raise IfcGeometryBasisIntegrityError("unsupported WorldCoordinateSystem placement")


def _create_optional_direction(model: Any, values: tuple[float, float] | None) -> Any | None:
    if values is None:
        return None
    return model.create_entity("IfcDirection", DirectionRatios=values)


def _read_optional_direction(
    entity: Any | None,
    *,
    expected_dimension: int,
) -> tuple[float, ...] | None:
    if entity is None:
        return None
    return _direction(entity, expected_dimension)


def _coordinates(entity: Any, dimension: int) -> tuple[float, ...]:
    if entity is None or str(entity.is_a()) != "IfcCartesianPoint":
        raise IfcGeometryBasisIntegrityError("placement Location must be IfcCartesianPoint")
    values = tuple(float(value) for value in entity.Coordinates)
    if len(values) != dimension:
        raise IfcGeometryBasisIntegrityError("placement Location dimension is invalid")
    return values


def _direction(entity: Any, dimension: int) -> tuple[float, ...]:
    if entity is None or str(entity.is_a()) != "IfcDirection":
        raise IfcGeometryBasisIntegrityError("placement direction is missing")
    values = tuple(float(value) for value in entity.DirectionRatios)
    if len(values) != dimension or not any(value != 0.0 for value in values):
        raise IfcGeometryBasisIntegrityError("placement direction is invalid")
    return values


def _unit_summary(entity: Any) -> IfcSiUnitSummary:
    if str(entity.is_a()) != "IfcSIUnit":
        raise IfcGeometryBasisIntegrityError("unit must be IfcSIUnit")
    return IfcSiUnitSummary(
        name=str(entity.Name),
        prefix=_optional_string(entity.Prefix),
    )


def _is_exact_parent(entity: Any) -> bool:
    return str(entity.is_a()) == _PARENT_CLASS


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _remove_if_unreferenced(model: Any, entity: Any | None) -> None:
    if entity is not None and not tuple(model.get_inverse(entity)):
        model.remove(entity)


def _remove_placement_tree_if_unreferenced(model: Any, placement: Any | None) -> None:
    if placement is None or tuple(model.get_inverse(placement)):
        return
    children = [
        getattr(placement, "Location", None),
        getattr(placement, "Axis", None),
        getattr(placement, "RefDirection", None),
    ]
    model.remove(placement)
    for child in children:
        _remove_if_unreferenced(model, child)


__all__ = ["IfcOpenShellGeometryBasisCompiler"]
