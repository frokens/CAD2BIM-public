"""Pure request inputs for current engineering Geometry candidate tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Candidate:
    manifest_path: Path
    geometry_name: str
    scenario_count: int


BEND = Candidate(
    manifest_path=ROOT / "families/bend/manifest.json",
    geometry_name="curved two-port geometry",
    scenario_count=13,
)
CONNECTOR = Candidate(
    manifest_path=ROOT / "families/connector/manifest.json",
    geometry_name="CONNECTOR body geometry",
    scenario_count=9,
)


def source_members(files: list[dict[str, str]]) -> list[dict[str, str]]:
    """Validate current manifest paths separately from canonical bundle members."""
    assert all(set(item) == {"repo_path", "bundle_path"} for item in files)
    assert len({item["bundle_path"] for item in files}) == len(files)
    assert all((ROOT / item["repo_path"]).is_file() for item in files)
    return files


def geometry_request(
    geometry: dict, candidate: Candidate, *, direct_session_alias: str,
) -> dict:
    """Build one provider request from the candidate's current local resources."""
    return {
        "direct_session_alias": direct_session_alias,
        "mode": "create",
        "display_name": candidate.geometry_name + " / " + geometry["binding_key"],
        "usage_instructions": {"action": "set", "value": (ROOT / geometry["usage_path"]).read_text(encoding="utf-8")},
        "source": {
            "source_kind": geometry["resource_kind"],
            "source_files": [
                {"source_path": item["bundle_path"],
                 "source": (ROOT / item["repo_path"]).read_text(encoding="utf-8")}
                for item in source_members(geometry["source_files"])
            ],
            "geometry_inputs": [],
            "parameter_schema_json": json.dumps({"action": "set", "value": json.loads(
                (ROOT / geometry["parameter_schema_path"]).read_text(encoding="utf-8"))}, ensure_ascii=False),
        },
    }


def geometry_preview_request(geometry: dict, *, direct_session_alias: str, draft_ref: str, retain: bool) -> dict:
    """Prepare current candidate cases using the actual draft returned by Write."""
    samples = json.loads((ROOT / geometry["validation_samples_path"]).read_text(encoding="utf-8"))
    assert samples
    cases = {"mode": "inline", "items": [
        {"case_kind": "geometry", "case_key": f"representative_{index + 1}",
         "parameters": sample, "expectation": {"expected_status": "BUILT"}}
        for index, sample in enumerate(samples)
    ]}
    return {
        "direct_session_alias": direct_session_alias, "mode": "cases",
        "source": {"source_kind": "draft", "draft_ref": draft_ref},
        "cases_json": json.dumps(cases, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        "retain": retain,
    }
