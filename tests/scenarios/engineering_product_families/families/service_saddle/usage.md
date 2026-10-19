# SERVICE_SADDLE Family

Every fragment declares closed `binding_options`; the proposal names its selected
`binding_key`. Only that option's parameters are supplied. Sphere options use
`diameter_m` and independent `interfaces` (key, origin, full frame, and circular
diameter; datum entries have no profile). Selection precedes Geometry execution.

This Family creates one `IfcPipeFitting` with one GAS
branch port and a separate profile-less `host_datum` attachment interface.
The datum is an external HOST relation, not a pair of invented mainline GAS
ends. The host source itself is never modeled, cut, moved or scaled here.

Supply the real finite host `A/B` and host OD in `host_*` product parameters;
these describe the profile-less host attachment geometry. Supply the GAS
branch as one `external_constraints` item keyed `gas_branch`, whose condition
uses `kind=finite_segment`, `start_m`, `end_m`, `outer_diameter_m`, and
`direction_relation=same`. Start and end retain the original branch A/B
order; its outward direction is A→B. The solved branch OD feeds the Geometry
mapping and actual GAS interface checks. Host datum has no GAS profile
requirement. `standard_model` constructs the original nominal branch target
internally, reads the retained host geometry product parameters, and uses the
same numeric solver. Its declared standard product configuration supplies the
original nominal host endpoints and OD.
Coordinates and dimensions are metres. An unknown source value stays pending
upstream; it must not be substituted by zero to invoke this Family. The
independent product origin `O` is removed once from a `self_geometry` fragment
translation. In `standard_model`, the nominal local geometry is independent
of O and the root receives O once. The local branch and collar remain
centered at the solved junction.

The Family intersects the two 3D axes and checks that the junction lies in
the host's finite segment. For detailed construction, local X is the oriented host axis; local Z is
the branch's perpendicular component; local Y completes a right-handed frame.
For local branch `d=(dx,0,q)`, `q=0` is rejected before Geometry. For `q>0`,
the retained conservative reference boss/collar envelope reserves
`E=(.58 Dh |dx| + .72 Db)/q` on each host side. The collar half-length is
`max(.75 Dh,E+numeric seam margin)` while it fits the finite host half-window;
`.75 Dh` is a preference. The branch's real port station is
`(Dh/2+2 Db)/q` from the host-axis junction. Only the actual port must lie
in the finite branch source window; the junction may lie on its
extension. The source station is `t_port=branch_station+port_station`.
Small positive `q` is classified from those windows and arithmetic capacity,
without a hard minimum physical angle. `AMBIGUOUS` marks unresolved numeric
capacity rather than claiming a physical impossibility.

The Family chooses `construction=detailed|node` before Geometry runs; callers
do not choose an expression. Once the source intersection and finite HOST C
are legal, a detailed `host_window` envelope failure or `branch_window` port
failure selects `sphere_geometry` with the corresponding `default_reason`.
Detailed construction selects `service_saddle_geometry`, reporting
`default_reason=none`; its solved `shape` carries collar half-length and branch
port station. Node output instead carries `sphere_diameter_m` and
`sphere_interfaces`, without detailed collar or direction parameters.

The node is a nominal sphere centered at C with diameter `max(Dh,Db)`.
Its HOST datum retains C, its original full frame and no profile. Its GAS
interface retains the original finite Branch A, A→B direction, full-frame
reference and Db. C need not be inside the finite branch, and neither the GAS
origin nor its nominal section must lie on the sphere. This does not establish
a connecting neck, bore, seal, material occupancy or installation. Existing
source station and interface checks still apply to the independently evaluated interfaces.
Invalid source/OD/length, parallel or skew axes, C outside the finite host,
numeric/native capacity and seam-resolution failures stay errors. Geometry
and Codec failures propagate; they never trigger another expression.

The node fragment uses world-parallel axes and translation C−O (C in
`standard_model`), with GAS local origin A−C. The HOST radial axis and host
reference are supplied explicitly. This preserves the same world frames
without rotating A out and back at a strict finite-window endpoint.

The GAS frame uses the projection of world Z onto the physical branch's
normal plane; if that projection is numerically degenerate, it uses world X.
The Family converts this full-frame reference into fragment-local coordinates
for Geometry. A round port's visual appearance does not establish its roll.

The nominal appearance geometry keeps the selected collar (outer radius
`.58 Dh`, host bearing radius `.5 Dh`), a cylindrical root (`.65 Db`), straight
neck (`.5 Db`) and small flange. Tapered transitions and decorative fillets
are omitted. The branch retains its actual annular end with recess radius
`.43 Db` and depth `.5 Db/q`; the recess is intentionally blind. This is an
appearance and placement representation, not a communicating flow passage,
manufacturing profile or certified wall/seal design. The host clearance is
real and the host pipe remains unmodified. The solver retains its previous
conservative `.72 Db` envelope, collar selection, port station and interface
checks; the visual revision does not change detailed/node selection.
Downstream host-wall treatment, gas connection and persistent IFC changes
belong to the installation workflow. Interface success alone does not prove
that material has been bored through either the saddle or the source host.
