from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Mapping

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.cad_measurement_service import CadMeasurementService
from CAD2BIM_backend.application.data_runtime_service import (
    DataRuntimeService,
    DirectScopeResolver,
    DirectSessionService,
)
from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
from CAD2BIM_backend.domain.cad_measurement import (
    CAD_MEASUREMENT_COMPUTE_BUDGET,
    CadComputeMeasurementsCommand,
    CadToleranceSpec,
)
from CAD2BIM_backend.domain.data_runtime import DataPackageKind, ValidatedSource
from CAD2BIM_backend.domain.data_transform import OutputPackageDeclaration
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import InMemoryIdentityDirectory
from CAD2BIM_backend.infrastructure.memory.state import MemoryState
from CAD2BIM_backend.infrastructure.storage.local import LocalStorage
from CAD2BIM_backend.infrastructure.cad_measurement import (
    MeasurementTolerances,
    PointScope,
    compute_measurements,
)
from CAD2BIM_backend.infrastructure.cad_measurement.supervised_worker import (
    CadMeasurementWorkerComputeResult,
)


pytestmark = pytest.mark.integration
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "cad"
SECOND_CAD = b"""0
SECTION
2
ENTITIES
0
LWPOLYLINE
5
B1
8
GasB
90
3
70
0
10
0
20
0
10
1
20
0
10
2
20
0
0
ENDSEC
0
EOF
"""


class _InProcessMeasurementWorker:
    """Exercise service provenance without depending on subprocess supervision."""

    async def compute(
        self,
        tables: Mapping[str, pa.Table],
        command: CadComputeMeasurementsCommand,
        *,
        deadline: float,
    ) -> CadMeasurementWorkerComputeResult:
        del deadline
        tolerances = command.tolerance_spec
        bundle = compute_measurements(
            entities=tables["entities"].to_pylist(),
            geometry_elements=tables["geometry_elements"].to_pylist(),
            relationships=tables["relationships"].to_pylist(),
            path_layers=command.path_layers,
            point_scopes=tuple(
                PointScope(
                    scope_key=item.scope_key,
                    point_layers=item.point_layers,
                    search_radius_m=item.search_radius_m,
                )
                for item in command.point_scopes
            ),
            source_unit_to_m=command.source_unit_to_m,
            tolerances=MeasurementTolerances(
                endpoint_quantization_m=tolerances.endpoint_quantization_m,
                endpoint_snap_candidate_m=tolerances.endpoint_snap_candidate_m,
                endpoint_on_path_candidate_m=tolerances.endpoint_on_path_candidate_m,
                host_endpoint_guard_m=tolerances.host_endpoint_guard_m,
                point_target_uniqueness_margin_m=(tolerances.point_target_uniqueness_margin_m),
                straight_turn_tolerance_deg=tolerances.straight_turn_tolerance_deg,
            ),
            candidate_budget=CAD_MEASUREMENT_COMPUTE_BUDGET.candidate_count,
        )
        tables = bundle.to_arrow_tables()
        workspace = Path(tempfile.mkdtemp(prefix="cad2bim-m3-test-worker-"))
        output = workspace / "outputs"
        output.mkdir()
        parquet_paths = {}
        parquet_bytes = {}
        for name, table in tables.items():
            path = output / f"{name}.parquet"
            pq.write_table(table, path, compression="zstd")
            parquet_paths[name] = path
            parquet_bytes[name] = path.stat().st_size
        return CadMeasurementWorkerComputeResult(
            started_at=datetime.now(timezone.utc),
            tables=tables,
            parquet_paths=parquet_paths,
            parquet_bytes=parquet_bytes,
            _workspace=workspace,
        )


def _description(name: str) -> str:
    return f"""# {name}
## Summary
Deterministic measurements for one exact test CAD source.
## Intended use
Verify that M3 processes multiple CAD packages in separate calls.
## Contents
The fixed ten M3 Parquet payloads.
## Method
Run the server-owned M3 geometry kernel over one exact source_l0 package.
## Scope and completeness
Contains only the path layer selected in this call.
## Known limitations
These are geometry facts, not topology or IFC decisions.
"""


def _command(package_alias: str, path_layer: str, name: str) -> CadComputeMeasurementsCommand:
    return CadComputeMeasurementsCommand(
        package_alias=package_alias,
        source_unit_to_m=1.0,
        path_layers=(path_layer,),
        point_scopes=(),
        tolerance_spec=CadToleranceSpec(
            endpoint_quantization_m=0.0001,
            endpoint_snap_candidate_m=0.01,
            endpoint_on_path_candidate_m=0.01,
            host_endpoint_guard_m=0.01,
            point_target_uniqueness_margin_m=0.005,
            straight_turn_tolerance_deg=0.5,
        ),
        output_package=OutputPackageDeclaration(
            name=name,
            description_markdown=_description(name),
        ),
    )


async def test_m3_multiple_cad_packages_are_computed_in_separate_provenance_bound_calls(
    tmp_path: Path,
) -> None:
    repository = InMemoryControlPlaneRepository(MemoryState())
    identities = InMemoryIdentityDirectory()
    artifacts = ArtifactService(repository, LocalStorage(tmp_path / "storage"))
    runtime = DataRuntimeService(repository, artifacts)
    sessions = DirectSessionService(repository, identities, "m3-multi-cad-user")
    resolver = DirectScopeResolver(repository, identities, "m3-multi-cad-user")
    opened = await sessions.open_direct_session("M3 separate CAD calls")
    scope = await resolver.resolve(
        opened.binding.direct_session_alias,
        command_id="command_m3_multi_cad",
    )

    first_source = FIXTURE_ROOT / "minimal_line.dxf"
    first = await runtime.import_cad_source(
        scope,
        ValidatedSource(
            filename=first_source.name,
            media_type="application/dxf",
            content=first_source.read_bytes(),
            source_identity="test-fixture:cad-a/minimal_line.dxf",
        ),
    )
    second = await runtime.import_cad_source(
        scope,
        ValidatedSource(
            filename="second_polyline.dxf",
            media_type="application/dxf",
            content=SECOND_CAD,
            source_identity="test-fixture:cad-b/second_polyline.dxf",
        ),
    )
    service = CadMeasurementService(
        repository,
        artifacts,
        ExactPayloadResolver(repository, artifacts),
        worker=_InProcessMeasurementWorker(),
    )

    first_result = await service.compute(
        scope,
        _command(
            first.published.package.catalog_entry.alias,
            "0",
            "M3 CAD A measurements",
        ),
    )
    second_result = await service.compute(
        scope,
        _command(
            second.published.package.catalog_entry.alias,
            "GasB",
            "M3 CAD B measurements",
        ),
    )

    first_published = first_result.published
    second_published = second_result.published
    assert first_published.package.package.package_kind is DataPackageKind.DERIVED
    assert second_published.package.package.package_kind is DataPackageKind.DERIVED
    assert first_published.package.package.package_id != second_published.package.package.package_id

    first_detail = first_published.operation.operation.detail
    second_detail = second_published.operation.operation.detail
    assert first_detail is not None
    assert second_detail is not None
    assert {item.package_id for item in first_detail.inputs} == {
        first.published.package.package.package_id
    }
    assert {item.package_id for item in second_detail.inputs} == {
        second.published.package.package.package_id
    }
    assert {item.package_id for item in first_detail.inputs}.isdisjoint(
        {item.package_id for item in second_detail.inputs}
    )

    first_members = {
        member.logical_payload_name: member
        for member in first_result.output_members
        if member.logical_payload_name is not None
    }
    second_members = {
        member.logical_payload_name: member
        for member in second_result.output_members
        if member.logical_payload_name is not None
    }
    assert first_members["atomic_edges"].row_count == 1
    assert second_members["atomic_edges"].row_count == 2
    assert tuple(first_members) == tuple(second_members)
