"""Finite target failure must finish before GAS_METER has a Geometry proposal."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest


FAMILY = Path(__file__).resolve().parents[2] / "scenarios/engineering_product_families/families/gas_meter"


def test_too_short_targets_keep_finite_window_failure_and_no_geometry_fragments():
    spec = importlib.util.spec_from_file_location("meter_external_owner", FAMILY / "program/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    case = next(item for item in json.loads((FAMILY / "preview_cases.json").read_text())["cases"]["items"]
                if item["case_key"] == "true_short_window")
    invocation = copy.deepcopy(case["invocation"])
    before = copy.deepcopy(invocation)

    class Context:
        def param(self, key):
            raise AssertionError("GAS_METER has no public installation parameters")

        def product_origin(self):
            return invocation["product_origin"]

        def external_constraints(self):
            return invocation["external_constraints"]

    ctx = Context()
    left, right = module._source(ctx, "left"), module._source(ctx, "right")
    available = right["b"][0] - left["b"][0] - 2 * module._HALF_BODY_M
    minimum = module._minimum_length(left["od"]) + module._minimum_length(right["od"])
    assert available == pytest.approx(.02)
    assert minimum == pytest.approx(.0928)
    assert minimum > available
    result = module.realize(ctx)
    assert result["status"] == "INFEASIBLE"
    assert result["violations"][0]["violation_key"] == "finite_window"
    assert "fragments" not in result
    assert invocation == before
