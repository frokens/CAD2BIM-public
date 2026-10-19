"""One BEND Family with detailed layout and a finite-port default node."""

from .shared_solver import (
    nominal_node, with_construction,
    realize as shared_realize, standard_model as shared_standard_model,
)


def realize(ctx):
    return with_construction(shared_realize(ctx, max_turn_degrees=160, layout_fallback=nominal_node))


def standard_model(ctx):
    return with_construction(shared_standard_model(ctx, max_turn_degrees=160, layout_fallback=nominal_node))
