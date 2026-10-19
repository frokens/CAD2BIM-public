# CROSS body Geometry candidate

One analytic OCCT solid is formed from a full spherical outer envelope and
four tangent Bezier-transition necks, minus a connected inner envelope with
four open bores. All lengths are metres. Each supplied `side_*_local_port_m`
lies on its own outward ray from `local_center_m`; its OD and internally
solved wall thickness produce one true planar annular end face. The Geometry
evaluates four named interfaces from the supplied endpoints, outward rays,
source-derived reference frames and ODs in `interfaces.py:evaluate(ctx)`.
The native-free `parameters.py:prepare(ctx)` rules are shared with Shape-only
`main.py:build(ctx)`. The Family carries the chamber frame in fragment placement; the realization root then places the product. References must be unit directions perpendicular to their own local axes. The Geometry preserves the supplied roll rather than deriving a new reference from local Z.

The builder checks cap separation on both envelopes, one valid closed
material solid, one connected bore tool, and four true ring faces. Saved
BREP readback checks the center and four bore paths as void, the real face
normals and source windows, closed shell, and self interference. This
function does not construct or translate IFC objects.
