# VALVE v3 Family

Each fixed fragment declares one `binding_options` entry. Proposals explicitly
select `valve_upper_geometry` and `curved_two_port_geometry`; the original
numerical solve, upper pose and scale rules remain. A short finite window
remains infeasible. This Family has no node-selection branch.

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

Supply `invocation.external_constraints` entries for `side_a` and `side_b`, each
with `condition: {kind: finite_segment, start_m, end_m, outer_diameter_m,
direction_relation: same}`. `none` permits unconstrained direction. The complete
ordered segment is the permitted range and outward start→end direction; no real
pipe object is required. `public_parameters` is empty and `product_origin` is
separate. `standard_model` internally supplies opposing 4 m segments with
0.3 m OD on both sides, accepting no caller targets.

This `semantic_assembly` has a physical `IfcValve` root owning one
mapped upper and exactly one `IfcPipeFitting` child owning one continuous
current `curved_two_port` lower, with complete `ports` and tagged `shape` parameters. The lower binding is the shared BEND exact version;
this Family does not create or modify a private bend. Both external gas ports
come from the lower model interfaces, using each actual source OD. The upper
has no ports and no gas bore role. The complete upper housing, wheel, stem and
bottom cap share one mapped instance scale. The Family derives that dimensionless
scale as `middle_outer_diameter_m / 0.33 m`: the shared lower has
`middle_outer_diameter_m=max(side_a_OD, side_b_OD)` for a straight run and
`1.1*max(side_a_OD, side_b_OD)` for a bend. The `.33 m` baseline is the source
upper's matching lower middle OD, with a nominal `.165 m` outside radius.
The fixed source BREP is not rewritten for different ODs. The lower function
fragment stays at scale one.

Input fields and finite-line validation are the same as the shared BEND v3
solver. `realize` solves the lower relative to the supplied product origin;
`standard_model` uses its product-owned nominal targets as the shared solver does.
At positive turn, the upper origin is the actual 3D arc midpoint. At zero
turn, it is the midpoint between the true lower ports. The upper stays upright
in world +Z and rotates only in yaw to the midpoint tangent's horizontal
projection. If that projection vanishes, declared source directions provide
a stable yaw. If neither has a horizontal bearing,
the result is `AMBIGUOUS/upper_yaw` with no product geometry. Root placement
comes from the request once; the upper fragment transform is local to it.

Material overlap between the two components at the visual joint is allowed.
It cannot justify changing the lower's B, original gas ports, or lumen. This
visual joint is an appearance assembly, not a proved manufactured mating
surface. Instance geometry may be materialized temporarily for geometry
inspection; downstream IFC mapping must apply the scale once to the original
source representation.

Detailed shape coordinates use a resource-local frame. Overall translation and rotation are carried by the existing fragment `local_transform`; solved dimensions, relative directions and the original actual port roll are preserved. Local interface references are transformed with the frame rather than regenerated from local Z.
