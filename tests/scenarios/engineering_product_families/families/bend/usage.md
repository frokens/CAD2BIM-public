# BEND finite two-port authoring

The fragment declares `binding_options`; proposals supply
`binding_key` and parameters. Sphere parameters are `diameter_m` and `interfaces`.

After the existing owner establishes the common connection, numerically
indistinguishable opposing axes share one construction axis through that fixed
point. The four endpoint residuals and full-window direction drift must fit
`128 * machine_epsilon * max(world_coordinate_scale, finite_source_scale)`,
and this arithmetic budget must stay within one eighth of the existing 1e-6 m
position tolerance. This is not the Data 1 mm normalization budget or an angle
cutoff. Source endpoints, fixed stations and ODs are unchanged; all solved
and nominal port axes, frames and shape parameters consume the same axis
expression. Original finite-source checks still apply. A distinguishable
angle or offset follows the original solver and failure rules; declining this
axis expression never selects a node.

The Family returns `construction=detailed`, `default_reason=none` for a
solved detailed layout. Only three declared `INFEASIBLE/no_finite_fit`
outcomes select `construction=default`: `minimum_body_exceeds_windows`,
`body_outside_both_windows`, and `curvature_transition_window_empty`.
Their original detailed-failure reasons are retained; they do not establish
physical installation. Input, unsupported axes/domain, numeric-capacity and
solved-port finite-window failures remain failures. Geometry and Codec
errors are never caught.

Default retains `bend`, `bend_body` and two circular
interfaces. The fragment selects `sphere_geometry`; detailed output selects
`curved_two_port_geometry`. Its full sphere uses the authoritative intersection (side-A station
for opposite collinear sources), not the ports' average, and maximum source OD.
Independent ports remain at their own A points with the axis expression above,
orthogonal reference and original OD; coincident positions do not merge ports.
`standard_model` uses that station as local zero, independently of product_origin;
fragment placement applies it once. Source/fixed-condition and connection checks
still apply. Nominal sections prove neither openings, passage nor installation
clearance. The equations below describe detailed construction.

This Family accepts two **finite** 3D installation targets through `invocation.external_constraints`, one for each `interface_key` (`side_a`, `side_b`). Each `condition` has `kind: finite_segment`, ordered `start_m` and `end_m`, positive `outer_diameter_m`, and `direction_relation: same` (or `none` when direction is unconstrained). The whole segment is the allowed range. The outward unit direction is start→end; the targets need not be real pipe objects. `public_parameters` is empty. `product_origin` remains independent. Missing OD or a zero-length segment fails.

The two infinite axes must meet in 3D. The Program computes their intersection and checks both the line-intersection residual and each final port's perpendicular distance from its actual source axis against the formal `1e-6 m` position tolerance. This business tolerance does not increase with source-segment length; if coordinate arithmetic cannot resolve it, the result is `numeric_capacity`. A skew pair or parallel offset pair is unsupported; the solver never averages closest points. For exactly opposite collinear directions, endpoint A of `side_a` is the fixed reference station because the lines have no unique intersection. A same-direction pair is outside the turn domain. The true turn is `atan2(|ua × ub|, -ua · ub)` in `[0°, 160°]`. A distinguishable positive angle below 2° stays positive. BEND selects this domain internally in both entrypoints; it is not a caller parameter.

For a nonzero turn, equal A endpoints give the exact intersection; unequal endpoints use the intersection calculation without snapping. The solver projects both finite segments onto their outward axes and intersects the admissible body scales. It keeps the preferred scale when feasible, otherwise projects it onto that closed interval. The circular arc leaves room for each end stub and annular transition, with body radius at least `0.51` times body OD. An empty physical interval returns `no_finite_fit` before Geometry. If only the supported radius limit (`radius/body_OD≤1e8`) prevents construction, the result is `AMBIGUOUS/numeric_capacity`. The selected ports and leg lengths feed the existing short-neck, capped-ring and annular-cone Geometry; this construction does not claim a C2 meridian or a slope-at-most-one cone.

At 0°, the solver does not evaluate cotangent. Equal OD is a direct annular tube. Unequal OD has `0.30Da` and `0.30Db` end stubs. The frozen numeric minimum between them remains `max(0.10 max(Da,Db),1.875|Da-Db|/2)`, although the current Geometry uses one linear annular cone across the full selected length between stubs. This minimum preserves the prior finite-window selection; it is not a C2 property or the cone's necessary minimum. The cone can lengthen as one continuous fitting; the minimum is not a fixed body length. Let the signed source windows be `[la,ha]` and `[lb,hb]`. The admissible fitting length intersects `[stub_a+minimum_transition+stub_b,∞)` with `[la+lb,ha+hb]`; the solver selects the smallest legal intrinsic length L. Side A's exposed port is station `x` along `-ua` from the role reference and side B's is `x+L`. For that L it intersects `[-ha,-la]` and `[lb-L,hb-L]`, then chooses the smallest legal x independently of the product origin. Equal OD may likewise use a longer direct tube when source windows require it. No protected remainder or separate compensation pipe is invented.

`realize` selects ports in world coordinates and factors the fitting frame into its fragment placement. The product origin and fragment placement together recover those same selected world ports. `standard_model` supplies product-owned opposing 4 m target segments at zero with 0.02 m OD on both sides, then uses the same solver. It accepts no caller external conditions; product_origin only places the standard model. Each port reference projects world Z into its normal plane, using world X at degeneracy; it differs from the bend-plane normal. Both entrypoints return one `IfcPipeFitting` and one `bend_body` bound to exact `curved_two_port_geometry`, which other published Families may also bind. Solved `ports` supplies both independent frames and the target ODs; `shape` supplies only the selected straight/curved fields. These are the fixed Geometry inputs.

`INFEASIBLE/source_*` means missing or invalid source data; `INFEASIBLE/no_finite_fit` proves the finite windows and shape constraints do not overlap; `INFEASIBLE/unsupported_axes` or `unsupported_domain` describes an excluded layout. `AMBIGUOUS/numeric_capacity` marks floating-point resolution or supported-modeling capacity, not a proof of geometric impossibility. Unsolved proposals contain no component or fragment. The smallest designed radial/axial feature must exceed `10 ×` OCCT's `1e-7 m` confusion value; source and local coordinates must also resolve the fitting OD within the declared relative precision guard, and a radius above `1e8` times body OD is outside this modeling capacity. These are technical limits, not old product-size limits.

Detailed shape coordinates use a resource-local frame. Overall translation and rotation are carried by the existing fragment `local_transform`; solved dimensions, relative directions and the original actual port roll are preserved. Local interface references are transformed with the frame rather than regenerated from local Z. The nominal sphere path keeps its existing translation-only placement because rotating a sphere only rewrites its independent interface metadata.
