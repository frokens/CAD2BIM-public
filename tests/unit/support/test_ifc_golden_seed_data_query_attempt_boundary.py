from __future__ import annotations

from CAD2BIM_backend.infrastructure.postgres.models import (
    DataQueryAttemptModel,
    DataQueryInputModel,
)
from tests.process_support.ifc_golden_seed import materializer


def test_data_query_attempts_are_target_empty_guards_not_seed_business_rows() -> None:
    assert DataQueryAttemptModel in materializer._EMPTY_TARGET_MODELS
    assert DataQueryInputModel in materializer._EMPTY_TARGET_MODELS
    assert "data_query_attempts" not in materializer._DATABASE_ROWS_FIELDS
    assert "data_query_inputs" not in materializer._DATABASE_ROWS_FIELDS
    assert "data_query_attempts" not in materializer._ROW_COLLECTIONS
    assert "data_query_inputs" not in materializer._ROW_COLLECTIONS
