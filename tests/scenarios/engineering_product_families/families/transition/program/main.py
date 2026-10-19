"""TRANSITION identity adapter over the shared finite two-port solver."""

from .shared_solver import (nominal_node, with_construction,
    realize as shared_realize, standard_model as shared_standard_model)


def _as_transition(proposal):
    if proposal["status"] == "SOLVED":
        proposal["components"][0]["component_key"] = "transition"
        proposal["fragments"][0]["fragment_key"] = "transition_body"
    else:
        for item in proposal.get("violations", []):
            if item["message"] == "BEND supports true turns up to 150 degrees":
                item["message"] = "TRANSITION supports true turns up to 150 degrees"
            elif item["message"] == "Same-direction sources are outside the BEND turn domain":
                item["message"] = "Same-direction sources are outside the TRANSITION turn domain"
    return with_construction(proposal)


def realize(ctx):
    return _as_transition(shared_realize(ctx, layout_fallback=nominal_node))


def standard_model(ctx):
    return _as_transition(shared_standard_model(ctx, layout_fallback=nominal_node,
                                               diameter_a=0.3, diameter_b=0.05))
