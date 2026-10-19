from __future__ import annotations

import json
from pathlib import Path

from CAD2BIM_backend.domain.run_events import RunEvent


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPOSITORY_ROOT / "docs/reference/runtime-schemas/run-event.schema.json"


def test_checked_in_run_event_schema_matches_domain_model() -> None:
    checked_in = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert checked_in == RunEvent.model_json_schema()
    assert "schema_version" not in checked_in["properties"]
    assert checked_in["additionalProperties"] is False
