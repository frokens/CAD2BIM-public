# Shared curved two-port Geometry

The input has two required fields: `ports` and `shape`. `ports.side_a` and
`ports.side_b` each contain `origin_m`, `axis`, `reference_direction`, and
`outer_diameter_m`. These are independent local connection frames and ODs;
the existing interface owner validates their orthonormality. `shape` is a
closed tagged object: `straight` requires only `mode` and `frame_x`; `curved`
requires `mode`, `frame_x`, `local_center_m`, `body_outer_diameter_m`,
`turn_radian`, `bend_scale_m`, and `arc_radius_m`.
No branch consumes parameters from another branch. Nominal connection nodes
use the independent `connection_sphere` resource selected by the Family.
This function constructs the selected detailed shape and propagates failures.

This revision accepts true turns through 160 degrees. Each Family still
enforces its own domain; this resource range does not expand other published
Family contracts or change their immutable Geometry bindings.

This Geometry resource accepts the complete numeric shape from a finite-segment Family solver. `mode=curved` uses a true circular centerline arc with `R=0.9B cot(δ/2)`, matching tangent straight legs, and a single fused annular solid. The body OD is `1.1 max(Da,Db)`; each exposed end has its own actual source OD. From each exposed port toward the unchanged arc tangent, the leg has a short cylindrical neck to `0.085D`, a capped rectangular ring from `0.085D` to `0.185D` with outer radius `min(1.18 × D/2, body OD/2)`, then one annular cone from the port radius at `0.185D` to the core radius at the tangent. The cone consumes the full remaining leg length; the ring shoulder does not set its starting radius. The bore radius is `0.86` of the local outer radius on the neck and cone, and stays at `0.86 × D/2` under the ring. This is the fitting's design section, not an inference about the unknown source-pipe wall or bore. The radial direction is the bend-plane normal, perpendicular to both source axes, so an arbitrary 3D turn does not distort the circular leg sections. The frozen Family's `0.30D` and 1.875 occupancy bounds still govern the selected ports; they are not a claim that the new cone is C2 smooth.

`mode=straight` uses a direct annular tube for equal OD or one linear annular cone for unequal OD. It has no decorative collar. Its design radial wall is `0.07 min(Da,Db)`, constant through the reducer; the assumption belongs only to the fitting. The unequal-OD cone occupies the full solved length between the two unchanged `0.30D` end stubs. The Family retains its prior minimum-length bound, including the 1.875 coefficient, so an input that was feasible before selects the same total length and port stations. This bound is a preserved numeric compatibility condition, not the cone's minimum geometric length. A longer cone remains one continuous fitting, with no extra cylindrical compensation piece. Equal OD permits a longer single direct tube. The open passage and both end bores remain unchanged.

The shared pure `parameters.py:prepare(ctx)` verifies the supplied mode, angle, B/R relation, body OD, true port equations, direct length, shape radial frame, and both supplied unit port references perpendicular to their local axes. The Family transforms the already chosen references into the Geometry frame; this Geometry does not choose a new local-Z roll. `interfaces.py:evaluate(ctx)` defines exactly `side_a` and `side_b` from the Geometry's local port parameters. Shape-only `main.py:build(ctx)` checks the actual fused BREP is one valid solid and retains actual circular edge radii and annular face area checks against the requested ODs and design bore. Independent saved BREP inspection can compare those physical ends and outward normals with the evaluated named datums; a numerical interface PASS does not assert a physical measurement.

The caller binds `curved_two_port_geometry` once per Family version. BEND owns its Family solver; other Families may bind a selected exact Geometry version under their own contracts. `validation_samples.json` contains solved B1–B3, a 1.5° non-Z placed example, the shared 0° unequal-OD X1 witness, and a longer intrinsic reducer witness. Sample validation checks this Geometry resource but does not replace independent exact-version Preview, physical entity inspection, downstream business acceptance, or IFC execution.
