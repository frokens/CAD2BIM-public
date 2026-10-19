# VALVE mapped upper

The source is the closed 12-solid / 64-face BREP identified in
`provenance.json`, in metres and its original local frame. Two original-edge
Filling faces close the source upper and lower stem shells while preserving
their source boundary curves. These approximate the nominal X-axis
`R=0.165 m` saddle; they are not a certified manufactured joint. Its local
+Z is the upright axis. This Geometry has no gas ports and must not be used
to create a lumen or a pipe fitting.

The VALVE Family maps this complete upper once, using uniform instance scale
`lower_middle_outer_diameter_m / 0.33 m`. Housing, wheel, stem and bottom cap
scale together. The `0.33 m` baseline belongs to the source's matching lower
middle outside diameter. The original BREP remains the canonical source;
scaled inspection shapes are instance products, not alternative source assets.

The existing trusted `GeometryResourceService.import_mapped_asset` is the
formal Backend intake for exact BREP bytes and provenance documents. It
normalizes and reopens the geometry, stores its canonical BREP/source bundle,
and publishes an immutable mapped-model version after exact verification.
The Direct source-first create API cannot take the BREP path or raw bytes, so
there is no parallel Python `build(ctx)` resource here. IFC output should use
the original source representation and apply this instance scale once through
the mapped item's mapping target.
