# TRANSITION finite two-port fitting

Every fragment declares closed `binding_options`; the proposal names its selected
`binding_key`. Only that option's parameters are supplied. Sphere options use
`diameter_m` and independent `interfaces` (key, origin, full frame, and circular
diameter; datum entries have no profile). Selection precedes Geometry execution.

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

This Family realizes one `IfcPipeFitting` with a detailed continuous body or a declared nominal node, and two external model interfaces, `side_a` and `side_b`. Supply one `invocation.external_constraints` entry per interface with `condition: {kind: finite_segment, start_m, end_m, outer_diameter_m, direction_relation: same}`. `none` permits unconstrained direction. The ordered endpoints define the complete allowed range and outward start→end direction; no real pipe object is required. `public_parameters` is empty and `product_origin` is independent. Missing OD, non-finite coordinates, and a zero-length segment are rejected. `standard_model` internally supplies opposing 4 m segments with 0.3 m and 0.05 m OD, accepting no caller targets.

The Family solves the axes from the two supplied source segments. They must intersect within the formal `1e-6 m` position tolerance, and both selected ports must lie on their own finite source windows. It does not average skew axes or invent a connecting pipe. The supported true turn is 0° through 150°, subject to finite fit, section, curvature, and modeling-resolution constraints. A numerically distinguishable positive turn remains positive. The solved `ports` object preserves each target OD in its corresponding independent port frame. The detailed solved `shape` contains only the selected straight or curved fields. The fixed fragment selects `curved_two_port_geometry` for detail or independent `sphere_geometry` for default.

At 0°, equal OD is a direct annular tube. Unequal OD uses the side-specific `0.30 × OD` straight end stubs and one linear annular cone between them. The Family keeps its prior numeric occupancy bound `max(0.10 × max(ODa, ODb), 1.875 × |ODa - ODb| / 2)` for selecting the same total length and port stations; the `1.875` term is a preserved selection rule, not a necessary geometric minimum for the new cone. The intrinsic length can grow to fit the source windows. The solver chooses the smallest legal fitting length, then the smallest legal local station in the intersection of the two signed finite windows. It chooses this station from the source geometry, independent of `product_origin`. A 6:1 OD ratio is feasible when the actual windows and section constraints permit it; no ratio or fixed product-size limit is imposed by this Family.

For a positive turn, the shared finite two-port solver selects a bend scale inside both source windows and a true circular arc with enough radius for a valid body. The bound Geometry forms each leg from a short cylindrical neck, a capped rectangular ring, and one annular cone to the original arc tangent; it keeps a continuous bore and both source-sized external ports. The Family's frozen occupancy checks still govern the selected stations, without implying C2 smoothness in this profile. `realize` factors the world-selected ports into a local fitting frame and the existing fragment placement; the product and fragment transforms recover the selected world ports. `standard_model` uses the computed intersection or collinear role-reference station as its local zero, so changing only `product_origin` leaves standard geometry unchanged. The Family's `transition_body` fragment binds one exact `curved_two_port_geometry` modeling resource. Its detailed interface centers, outward normals, references and each side's own OD are evaluated numerically before shape construction; the detailed build additionally measures the material end faces.

An empty physical layout interval selects `construction=default` with one of
`minimum_body_exceeds_windows`, `body_outside_both_windows`, or
`curvature_transition_window_empty`. The fixed `transition_body` fragment uses
the independent `sphere_geometry` binding with `diameter_m` and complete
`interfaces`. Detailed results report `construction=detailed, default_reason=none`.
The complete sphere is centered at the authoritative common intersection or
collinear side-A station. Each port stays at its own current source A with its
own outward A→B axis, orthogonal reference and circular OD. The display diameter
is max(source/interface ODs); there is no separate body attachment interface in
this single-fragment fitting. The detailed body envelope is not an interface OD.
Independent coincident ports remain distinct identities. Nominal sphere profiles
do not prove openings, through-flow or material contact. Fragment placement is
station minus product_origin; standard_model places that station at local zero.

`INFEASIBLE/no_finite_fit` remains a failure when an already solved port falls
outside its finite window. `INFEASIBLE/source_*`, `unsupported_axes`, and
`unsupported_domain` retain their source/domain failures. `AMBIGUOUS/numeric_capacity`
retains coordinate or modeling-resolution uncertainty. Geometry, interface and
Codec failures are never caught to select a default. Unsolved proposals contain
no component or fragment and must not invoke Geometry. Synthetic scenarios do
not establish real CAD coverage, network connections or IFC acceptance.

Detailed shape coordinates use a resource-local frame. Overall translation and rotation are carried by the existing fragment `local_transform`; solved dimensions, relative directions and the original actual port roll are preserved. Local interface references are transformed with the frame rather than regenerated from local Z. The nominal sphere path keeps its existing translation-only placement because rotating a sphere only rewrites its independent interface metadata.
