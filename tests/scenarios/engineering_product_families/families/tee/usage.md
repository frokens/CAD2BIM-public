# TEE Family

`self_geometry` supplies three `external_constraints`, keyed `side_a`, `side_b`,
and `side_c`. Each uses `condition.kind=finite_segment`, `start_m`, `end_m`,
`outer_diameter_m`, and `direction_relation=same`. Start remains the original
A toward the fitting; end remains B toward the continuing pipe. Only the
three `side_*_role` selections remain public product parameters. The solved
ODs feed the existing Geometry bindings and actual-interface profile checks.
`standard_model` creates the original nominal targets internally and uses the
same numeric solver; callers supply no external installation targets.

The `tee` component has three circular interfaces. Its `tee_body` fragment declares
`binding_options`; the proposal selects a `binding_key`.
Sphere parameters are `diameter_m` and `interfaces` (key, origin, frame and OD).
Detailed success declares
`construction=detailed, default_reason=none`. Three declared detailed misses
select `construction=default`: `main_or_branch_window_empty` preserves the
`INFEASIBLE/no_finite_fit` empty finite interval; `main_port_clearance_unavailable`
preserves `INFEASIBLE/port_overlap`; `joint_scale_layout_not_found` preserves
the `AMBIGUOUS/joint_scale_resolution` search that could not certify a candidate.
The latter does not claim that detailed construction is impossible. A candidate
that loses its clearance in arithmetic remains an ambiguity, not a default.
Missing/invalid sources, role ties, absent common intersection, tangent-branch
topology, unsupported domain and numeric-capacity errors remain failures.
Source, Geometry and Codec exceptions propagate.

Default sphere center is the already resolved common intersection, and its
diameter is the maximum of the three source ODs. Each independent nominal port
is at its own current source A with the unchanged side identity and OD.
Its outward frame uses the same numeric axis expression as the solver.
Coincident A points keep separate identities. Nominal sections support finite-source
and connection checks, but prove no openings, communicating material or installation space.
`standard_model` expresses the default relative to the authoritative station
and ignores the installation origin. The fragment selects `tee_body_geometry`
with solved `ports` and tagged `shape` for detail, or `sphere_geometry` with
`sphere_diameter_m` and `sphere_interfaces` for default. The unselected
candidate parameters are absent. The physical layout below describes detailed attempts.

Each side `a`, `b`, `c` contains finite world `A/B`, its OD and an explicit role. Each segment has
A toward the product and B toward the continuing pipe; its outward unit
direction is A→B. All measurements are metres. `product_origin` is
independent of the junction and is subtracted once for local placement.

Set the roles to `main_a`, `main_b`, `branch` when the relation is known.
These values preserve source provenance. If that known pair makes the branch
tangent to a main ray under the existing branch-direction check, the same
unique largest-angle selector below chooses the geometric through pair. A
valid known curved pair retains its original selection. This changes only
`shape.main_a_key/main_b_key/branch_key`; source side keys, OD, finite windows,
and returned `side_a/side_b/side_c` interface identities remain attached to
their original sources. Without a unique alternative, the original known-role
selection and its tangent-branch rejection remain in force.
Otherwise set all three to `independent`; the unique pair with the largest
outward-ray angle becomes main. A tie within 1e-7 radians returns
`AMBIGUOUS`, without an input-order or OD tie break. The program checks a
single real 3D axis intersection. It never averages skew closest points.

The finite-source helper may express the selected main pair with a common axis
through the unchanged intersection, directed between the two B endpoints.
All four endpoints and full-window direction drift must fit
`128*EPS*max(world_coordinate_scale, finite_length_or_OD_scale)` and that budget
must fit `1e-6/8` metres. This arithmetic representation preserves original A/B,
OD, side identities, fixed station and the independent branch axis. Accepted
main axes are strictly opposite across bounds, clearance, construction and
output frames. Finite-source and installation checks remain authoritative;
rejection follows the original curved/capacity path.

The main solves `B` inside both finite source
windows. Its turn is `atan2(|ua × ub|, -ua · ub)` in 0–150 degrees; 0 has
an explicit straight branch. For `fi=1.1Di`, `fmain=max(fa,fb)`, each main
end allowance is the two-port `ei + 0.3fmain`, and preferred B is
`fmain(0.4δ+0.2)+0.3fmain`. Actual ports are `C+(B+ei)ui`. The branch has
preferred `k=1.4fmain`, allowance
`2.1fmax(fmax−1.1Dbranch)/(fmax+Dbranch)+0.45fmax`, and port `C+(k+e)u`.
The program intersects each physical scale's finite window and the
`fmain/2` swept-core curvature bound, then searches their joint B/k domain
for distinct physical end openings. The numeric clearance model includes
the source collar envelope and both finite cores. Main collars are checked
against the finite branch neck, and the branch collar against the finite
straight or curved main core. A nominal B/k pair with overlapping ports is
not proof that the finite domain is empty. A feasible nominal pair returns
immediately; otherwise deterministic boundary candidates and bounded finite
samples return the first certified pair. An empty per-scale interval or an
unresolved joint-domain search records its detailed reason and uses the
declared default above. No caller-provided crossing,
angle, scale, pose or port is used.

For a source main turn of at most 0.01 degrees, the Family may select the
explicit `near_zero` Geometry mode if
`(max(B+ea,B+eb)+fmain/2+B)*sin(turn/2)` is at most
`min(50 micrometres, 0.0002 * smaller main OD)`. This is a conservative
admission estimate for this TEE recipe, not a whole-body measured error bound
or the Data 1 mm normalization budget. The physical main then uses a 0-degree
angle-bisector axis; the three returned interface datums keep the original
source positions, outward axes, identities and ODs. `shape.mode=near_zero` is
retained in the solved proposal so consumers can identify that its frame is a
logical connection datum. The Geometry separately measures and bounds actual
end-face center, normal and radii before returning one valid closed hollow
solid.

Candidate clearance checks cover complete main-port disks, the finite branch
neck, tapers, collars and terminal voids. A finite capsule encloses the curved
neck and its supported straight alternative. A coincident closest point or
failed certificate remains detailed ambiguity, not proof of collision or
infeasibility. Geometry may use a bounded ruled loft for a small curved main,
or a source-aligned cone when its alignment error fits
`min(50 micrometres, 0.0002 * smallest source OD)`; otherwise it retains the
circular main and Bézier branch. These internal choices preserve all three
actual end ports and their nominal connection data. `straight` and `near_zero`
use a straight main and branch from C. The OCCT body joins one exterior and
cuts one three-way void. End annuli use designed 0.86 OD bores; source-pipe
wall thickness is unknown. Full main and branch path probes are needed to
claim a through bore.

The detailed Geometry uses a local anchor and intrinsic body axes. The Family keeps the original numeric solution and inverse-transforms its points, axes and interface references; the existing `fragment.local_transform` restores the full installation pose. Relative bend/branch directions, dimensions and port identities remain shape inputs. Interface references retain their original world roll and may therefore still distinguish otherwise identical round bodies.
