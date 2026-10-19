# CONNECTOR Family

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

This Family has one `IfcPipeFitting` component and one exact
`connector_body_geometry` detailed binding or the independent `sphere_geometry`.
Use `invocation.external_constraints` entries for `side_a` and `side_b`, each
with `condition: {kind: finite_segment, start_m, end_m, outer_diameter_m,
direction_relation: same}`. `none` permits unconstrained direction. The whole
ordered segment is the allowed range, with outward direction start→end;
no real pipe object is required. `public_parameters` is empty. `standard_model`
internally supplies opposing 4 m segments with 0.2 m and 0.08 m OD and accepts
no caller targets. `product_origin` is
independent of the axis intersection and shifts the local fragment once.

The Family computes the true turn as `atan2(|u_a × u_b|, -u_a · u_b)`.
It accepts 0° through 150° and preserves distinguishable positive angles.
The 0° branch chooses one S-profile sleeve length inside both finite windows,
preferring `1.54 × max(OD_A, OD_B)` when feasible. It recomputes every S
station when that length changes, then places the two ports at the minimum
valid station of the side-specific finite interval. An empty physical layout
interval selects the declared node below; insufficient BREP land resolution is
numeric capacity uncertainty. The curved branch preserves the baseline
`f_i=1.1D_i`, `f=max(f_i)`, `e_i`, `B`, circular tangent and radius relations,
and two complete annular collars. It does not add a compensation piece.

In detailed construction the one geometry resource consumes solved local parameters and returns
two named circular external interfaces. It makes a continuous hollow
solid. The curved branch intentionally repairs the Golden IFC's two filled
terminal pieces and its 1.1× port OD while retaining the core, leg/collar
silhouette and stations. Its arc is a true circle; the saved Golden IFC
used a 12-point polyline. The bore ratio 0.86 is a selected design assumption,
not a measured external pipe wall. The S shoulders are tangent circular
biarcs and make no C2 continuity claim.

An invalid OD, axis or finite window returns a structured
infeasible/ambiguous proposal before geometry. 160° is outside the declared
0°–150° domain. The caller must not supply a precomputed angle, center,
length, x station, bend scale, radius, or compensation tube.

The fixed `connector` component and `connector_body` fragment also support
`construction=default`. Only the three declared layout misses
`minimum_body_exceeds_windows`, `body_outside_both_windows`, and
`curvature_transition_window_empty` select this node. Detailed results report
`construction=detailed, default_reason=none`. Solved ports outside their actual
axes/windows, unsupported input/domain, numeric capacity, Geometry and Codec
failures remain failures; none is caught and replaced with a node.

Detailed construction uses `ports` plus a closed tagged `shape` object. Straight
shape has `mode`, `frame_x`, `body_outer_diameter_m`, `sleeve_length_m`; curved
shape has `mode`, `frame_x`, `local_center_m`, `body_outer_diameter_m`,
`turn_radian`, `bend_scale_m`, `arc_radius_m`. The sphere binding receives
`diameter_m` and complete `interfaces`; unselected candidate parameters are absent.

The node is a complete sphere centered at the authoritative common intersection
or collinear side-A station. Each external port is its own current source A,
with its own A→B axis, orthogonal reference and circular source OD. Equal
positions do not merge interface identities. The sphere diameter is the maximum
source/interface OD; this single-fragment fitting has no separate body attachment
interface. Detailed 1.1/1.2 OD envelopes are not interface diameters. Nominal
profiles do not prove sphere openings, a bore, material continuity or installation
clearance. The fragment selects the independent `sphere_geometry` for default;
the detailed Geometry has no sphere dispatch or delegated sphere bundle member.
Its fragment translates the station relative to product_origin once;
standard_model uses the station as zero regardless of product_origin.

The detailed Geometry uses a local anchor and intrinsic body axes. The Family keeps the original numeric solution and inverse-transforms its points, axes and interface references; the existing `fragment.local_transform` restores the full installation pose. Relative bend/branch directions, dimensions and port identities remain shape inputs. Interface references retain their original world roll and may therefore still distinguish otherwise identical round bodies.
