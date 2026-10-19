# TEE body modeling function

The candidate `modeling_function` uses binding key `tee_body_geometry`.
The Family supplies `ports` and `shape`. `ports.side_a`, `side_b`, `side_c`
each have independent `origin_m`, `axis`, `reference_direction`, and
`outer_diameter_m`. `shape` is a closed tagged object. `straight` requires
`mode`, `local_center_m`, `bend_scale_m`, `branch_neck_scale_m`, `main_a_key`,
`main_b_key`, and `branch_key`; `near_zero` adds the source `turn_radian`,
and `curved` adds `turn_radian` and `arc_radius_m`.
Unselected branch fields are not permitted. This function does not read world
origin or source pipes. Nominal nodes use the independent `connection_sphere`
resource selected by the Family. Geometry does not choose a fallback or suppress
an exception; every mode retains its physical body and actual-end checks.

The exterior is based on the Stage03 selected `BASELINE_IFC` TEE Body
created by Golden `ifc_builder.py`, rather than the independent `04_tee`
shape. The main swept core radius is `fmain/2`, branch swept root radius
`fmax/2`, and the main straight-transition exterior is the union of
Golden's two cone pairs: when `fmax>fmain`, its root is `fmax/2` while the
main swept core remains `fmain/2`. The collar radii/stations follow the
selected source. In `curved` mode the central main connects the 0.9B tangent
stations. When the branch selects the axial cone, the main turn is at most
1 degree and main sagitta plus branch-axis offset is at most 0.05 times the
main inner radius, both main surfaces use a ruled loft between exact circular
end sections. Corresponding semicircles share the main-plane normal as their
radial reference. This engineering simplification preserves the terminal
positions, directions and diameters. Other curved mains use the circular sweep,
with each circular section assembled from two analytic semicircles. The separate
`near_zero` mode creates one physically straight main about the angle-bisector
axis through C, including its outer body and bore. The branch retains its source
axis. The original sequential unions and final cut
remain unchanged. Circular end boundaries are interpreted by their physical
radii and total circumference, not by the number of edges used to represent them.
In `curved` mode the reference branch begins at the main arc midpoint M and
uses M plus the 0.7k, 0.9k and k positions as cubic Bézier control points.
The physical neck selects the original branch axis when its reference-curve
alignment bound fits `min(50 micrometres, 0.0002 * smallest source OD)`.
For midpoint distance delta from that axis, positive axial distance d0 from M
to the first control and outer root radius r, the bound is `delta * (1+r/d0)`.
The last three controls lie on the source axis, so this criterion combines
centerline offset and section tilt. The selected engineering neck is one
linear cone: its outer root radius is 0.9 times the main outer radius, increasing
to fmax/2 at k; its inner radius stays 0.86 times the outer radius. The internal
start cap projects M onto the source axis. This simpler central profile uses
an explicit radial transition; all three connection positions, directions and
ODs and the existing terminal legs stay exact.
Larger deviations retain the Bézier sweep, ending at k with the exact outward
branch-axis tangent. The curve need not pass through the intermediate controls.
In `straight` and `near_zero` modes the branch starts at C along its source axis.
Outer and void sweeps share the selected mode and centerline;
when the bounded straight approximation applies, the neck joins the same axial profile as
its terminal leg. Each set of coaxial core, cones, collar and terminal
reduction is represented by an ordered radial upper-envelope profile and
one full revolution. The profile includes interval endpoints and crossings
of overlapping linear radii, retaining radial collar steps and the larger
main transition root when the branch OD exceeds both main ODs. The straight
main combines both legs and the central core in one revolution; the curved
main uses its selected central surface and one revolution per leg. The inner profiles
retain the short extensions beyond all three ports. Outer parts and void
parts are each joined before one unified main/branch void is subtracted
last. Each terminal gets a short 1.1OD-to-OD reduction beyond
the collar, with an actual 0.86OD bore. These local repairs are required
for one valid continuous solid and three real annular source-OD ports.

Before returning, the function checks validity, solid count and shell count
once each, requires one valid closed solid, and measures each actual planar
annular end face center and radii. The three end checks reuse one scan of the completed body's planar faces and
their centers. This removes repeated topology queries without changing the
outer collar, internal passage, Boolean construction or any end check.
`interfaces.py:evaluate(ctx)` computes all
three named interface origins, axes, references and ODs from the supplied
Geometry parameters before build. Both entries share `parameters.py:prepare(ctx)`.
In explicit `near_zero`, the interface remains a **logical source connection datum**:
its origin, axis and OD retain the original source values, while the physical end face is
measured independently. The Geometry rejects a measured end-center difference
above `min(50 micrometres, 0.0002 * smaller main OD)`, a physical-face normal
difference above 0.01 degrees from the source axis, or a face normal that does
not follow its straight physical main axis. The branch face remains coincident
with its source datum within 10 nanometres. The selected mode is visible in
`proposal.internal_parameters.shape.mode` (engineering summary:
`proposal_facts.internal_parameters.shape.mode`); a frame PASS checks the logical datum and cannot
be reported as a measured physical face-normal match.

The Geometry measures and rejects out-of-budget physical ends before success.
The current typed receipt does not retain each successful face residual; use a
saved BREP readback record when the exact residual values are needed.

Admission also requires source main turn at most 0.01 degrees and the local
estimate `(max(B+ea,B+eb)+fmain/2+B)*sin(turn/2)` below the same spatial budget.
These numbers are a scoped engineering modeling default, not a source-data
normalization or a fabrication standard. The estimate covers centerline and
cross-section orientation terms used to decide whether to attempt this mode;
it is not a measured whole-surface Hausdorff bound. The actual three annular
ends, valid closed shell and communicating bore require their own build and
readback evidence. Ordinary curved ports remain exact for either central
surface; straight ports retain their exact source geometry.

All positions and vectors are in the fragment-local coordinate frame. Interface references must be unit and perpendicular to their port axes; they are not required to follow a world-Z/X convention in that local frame. The Family transforms the original references, so its placement restores the same world interface frame.
