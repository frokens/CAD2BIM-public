# Engineering ProductFamily candidate sources

This tree contains local source candidates and focused engineering checks. It is
not a publication registry or a backup of exact published versions. The selected
Family/Geometry versions and immutable source Artifacts in the database and
storage outside this repository remain authoritative for an existing run.

- `families/` contains twelve categories of formal Family definitions, programs,
  Preview case collections and usage. `definition.json` is the formal contract;
  `preview_cases.json` holds `cases.items` and the nominal case selector separately.
  Each item follows the current Family Preview case contract. This is a complete
  local collection, not one tool request: callers select at most 16 cases per
  Preview call, and publication may use multiple retained reports. The SADDLE
  collection keeps all 19 representatives. All twelve categories have current
  candidate manifests pointing to the actual Family and Geometry source files.
  `tests/unit/scenarios/family_numeric_fixtures.py::current_numeric_request`
  consumes these manifests for current Domain inputs; the twelve representative
  self-geometry cases in `test_product_realization_runtime.py` reuse that helper
  and the existing supervised Runtime. A manifest does not assert registration
  or a completed runtime validation.
- `geometry/` contains current modeling-function source, parameter schemas,
  validation samples and usage, plus three mapped-model intake plans and
  provenance records. `assets/mapped_geometry/` holds the exact three BREP
  source files named by those plans. A manifest or intake plan describes a
  candidate; it does not confer registration or execution eligibility.
- `shared/finite_two_port_solver.py` is a source-bundle member used by the
  Families that declare it. `inputs/` contains pure source-fact projections;
  published input Packages are separate immutable data.
- `registration/candidate_requests.py` builds local Geometry request inputs
  for focused BEND and CONNECTOR tests. The caller supplies its own Direct
  session alias; this helper does not select a Scope or publish anything.
- `validation/real_engineering/` contains the Stage 3 exact-input replay and
  occupancy checks. It requires an explicit published projection and selection.

The old single-binding manifests, registration configuration, and one-off
input/render/publication checks are retained under `internal/archive/` as
process evidence. They are not current executable entry points. Freezing this
source tree does not recreate the external database, storage, or exact version
selection required to reproduce a published product run.
