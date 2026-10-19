"""Project already-checked realization observations; lifecycle stays with each caller."""
from CAD2BIM_backend.domain.canonical import canonical_json

def record_realization(rows, index, contract, result, *, add_check):
    product = rows["products"][index]
    product["solve_status"] = result.proposal.status.value
    product["normalized_parameters_json"] = canonical_json(result.normalized_public_parameters)
    if result.proposal.status.value != "SOLVED":
        code = "FAMILY_RESULT_CONTRACT_REJECTED" if result.proposal.status.value == "CONTRACT_REJECTED" else f"FAMILY_{result.proposal.status.value}"
        product.update(error_code=code, error_phase="solve")
        add_check(rows, phase="solve", check_kind="family_result", status="FAIL", input_row_index=index,
                  product_key=product["product_key"], error_code=code, error_origin="FamilyProgram",
                  diagnostic_json=canonical_json(result.proposal.model_dump(mode="json")),
                  recommended_action="Inspect the Family result or its declared support domain")
        return
    product["solution_json"] = canonical_json(result.proposal.internal_parameters)
    world = {item.component_key: item.project_world_transform for item in result.component_project_world_transforms}
    for component in contract.components:
        rows["components"].append({"product_key": product["product_key"], "component_key": component.component_key,
            "parent_component_key": component.parent_component_key, "ifc_class": component.ifc_class.value,
            "project_world_transform_json": canonical_json(world[component.component_key].model_dump(mode="json")),
            "build_status": "NOT_EXECUTED", "commit_status": "NOT_COMMITTED"})
    declared = {item.interface_key: item for item in contract.interfaces}
    for interface in result.interface_project_world_frames:
        definition, frame = declared[interface.interface_key], interface.project_world_frame
        rows["interfaces"].append({"product_key": product["product_key"], "interface_key": interface.interface_key,
            "component_key": definition.owner_component_key, "fragment_key": interface.source.fragment_key,
            "model_interface_key": interface.source.model_interface_key, "usage": definition.interface_kind.value,
            **dict(zip(("origin_x_m", "origin_y_m", "origin_z_m"), frame.translation_m)),
            **dict(zip(("axis_x", "axis_y", "axis_z"), (row[2] for row in frame.rotation))),
            "reference_direction_json": canonical_json([row[0] for row in frame.rotation]),
            "profile_json": canonical_json(interface.profile.model_dump(mode="json")) if interface.profile is not None else None,
            "commit_status": "NOT_COMMITTED"})
    for diagnostic in result.frame_diagnostics:
        evaluation = diagnostic.evaluation
        add_check(rows, phase="interface", check_kind=diagnostic.check_kind,
            status="PASS" if evaluation.satisfied else "FAIL", input_row_index=index,
            product_key=product["product_key"], interface_key=diagnostic.interface_key,
            peer_interface_key=diagnostic.peer_interface_key, metric="origin_distance_m",
            residual=evaluation.origin_distance_m, tolerance=evaluation.position_tolerance_m, unit="METRE",
            diagnostic_json=canonical_json(diagnostic.model_dump(mode="json")))
    checked = set()
    for check in result.external_condition_checks:
        checked.add(check.interface_key)
        evaluation = check.evaluation
        add_check(rows, phase="interface", check_kind="external_condition",
            status="PASS" if evaluation.satisfied else "FAIL", input_row_index=index,
            product_key=product["product_key"], interface_key=check.interface_key, metric="position_residual_m",
            residual=evaluation.position_residual_m,
            tolerance=evaluation.condition.position_tolerance_m if evaluation.position_residual_m is not None else None,
            unit="METRE" if evaluation.position_residual_m is not None else None,
            actual_json=canonical_json(evaluation.model_dump(mode="json")),
            expected_json=canonical_json(evaluation.condition.model_dump(mode="json")))
    for interface in contract.interfaces:
        if interface.interface_kind.value == "external" and interface.interface_key not in checked:
            add_check(rows, phase="interface", check_kind="external_condition", status="NOT_COMPARED",
                      input_row_index=index, product_key=product["product_key"], interface_key=interface.interface_key)
    if result.frame_check_status != "PASS":
        product.update(error_code="INTERFACE_CONDITION_FAILED", error_phase="interface")
