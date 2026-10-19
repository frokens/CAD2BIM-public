# CONNECTOR body modeling function

Bind this source as one `modeling_function` to `connector_body_geometry`.
Parameters are `ports` and a closed tagged `shape`. Each of `ports.side_a` and
`ports.side_b` contains `origin_m`, `axis`, `reference_direction` and
`outer_diameter_m`. Straight shape contains `mode`, `frame_x`,
`body_outer_diameter_m`, `sleeve_length_m`; curved shape contains `mode`,
`frame_x`, `local_center_m`, `body_outer_diameter_m`, `turn_radian`,
`bend_scale_m`, `arc_radius_m`.
No project-world origin is applied by this function.

Straight mode uses the final selected S sleeve's land (0.08L), shoulders
(0.16L), central span (0.52L), radial addition (0.10 maximum OD),
two shallow grooves and 0.86×OD bore. Descending OD is built in ascending
order then rotated rigidly; external side A/B identities remain fixed.

Curved mode models the final baseline's complete bent core, both leg
transitions and both annular collars. Core/leg/collar dimensions use the
baseline 1.1× OD and B/e/R relations. Filled end pieces are rebuilt with
0.86×OD continuous bore and annular caps at the actual source OD; all pieces
are joined into one closed solid. The source IFC used a polyline core, while
this resource uses the solved true circular arc. These are explicit geometric
repairs and re-expression, not a verbatim copy of the IFC representation.

Both detailed branches reject inconsistent solved parameters through shared
native-free `parameters.py:prepare(ctx)`. `interfaces.py:evaluate(ctx)` defines
the two named interfaces from supplied local port coordinates and profiles.
Shape-only `main.py:build(ctx)` retains the actual end-face and radius checks. The Family chooses the
finite-window placement; this function consumes that local placement without
using the product world origin.

Nominal connection nodes use the independent `connection_sphere` resource
selected by the Family. This Geometry builds only the selected detailed sleeve
or curved fitting and propagates construction errors.

All positions and vectors are in the fragment-local coordinate frame. Interface references must be unit and perpendicular to their port axes; they are not required to follow a world-Z/X convention in that local frame. The Family transforms the original references, so its placement restores the same world interface frame.
