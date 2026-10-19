# Public test material

This public snapshot retains source-level tests and reusable standalone Geometry/ProductFamily definitions.
It does not include the original real-network drawing, its golden tables, private native agent runs,
or their screenshots and outputs. Tests whose only input was that private drawing are not distributed.
Drawing-specific regression functions and their private helper constants are omitted as well; the
public subset is not presented as a replay of the private acceptance suite.

- `fixtures/cad/minimal_line.dxf` is a synthetic one-line document.
- Geometry/BREP regressions use standalone local-coordinate objects without project association.
- `fixtures/geometry_runtime/pipe_finite_miter_cases.json` uses anonymous case identifiers and independent local origins; its relative path geometry, section and expected statuses are preserved.
- Synthetic credential/path strings in rejection tests exercise redaction and isolation. They are not active access credentials.
- Engineering resource definitions are examples of current resource contracts, not a preinstalled customer or SYSTEM database.

Start with the focused provider tests in the root README. Database integration and real geometry tests
have additional prerequisites and should run only against an isolated environment. Private-dataset
results have not been re-labelled as public reproducible results.
