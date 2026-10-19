# REGULATOR mapped body

The designated authoring source is the later repaired copy of the complete
Golden-selected regulator template. The original Golden saved IFC used the
earlier template; the repaired copy did not retroactively change that result.
`provenance.json` fixes the exact IFC and source-preserving local OCCT sewing
candidate. The BREP is in metres and reopens as 13 valid closed solids with 66
unchanged analytic faces. Multiple solids are retained as the source's distinct
parts; no union, mesh reconstruction, added cap, or source IFC rewrite occurs.

The body is upright in local +Z. Its two **geometric** leg ends are full disks
at local `(-0.8937003968253989, 0, 0)` and
`(+0.8937003968253989, 0, 0)` metres. Each has outward normal `-Z` and outside
diameter `0.15 m`. Their source IfcPlane reference direction is local `-Y`.
Circular contours do not uniquely determine roll, so `-Y` is a declared
source-based installation frame, not a measured pressure flow direction.
The source has no semantic IFC ports or proven manufacturer high/low identity.
The Family must retain the separately authorized high/low source roles and
record which geometric leg each role uses.

Both ends are source **solid disks**, each with one outer circle and no inner
ring. This body can meet a shared BEND v3 annular end geometrically in center,
plane, OD, opposed normals and declared full frame. Such contact does not prove
body through-flow, sealing or a manufactured joint. The two bend pieces must
still demonstrate their own real openings and bores.

This is one fixed `mapped_model` BREP. Its only variable is one backend mapped
instance `uniform_scale` for the complete body; body, end centers, outside
diameters and frame all transform together exactly once. A separate half-scale
BREP in the diagnostic directory is a readback witness, not a second source
asset. The Family never scales either shared BEND instance or either external
pipe OD.

The asset intake path is `GeometryResourceService.import_mapped_asset` in the
task Scope, followed by `GeometryAuthoringService.annotate_interfaces` on the
same root. An exact version and its annotated interfaces must be read back
before a Family binds to the body.
