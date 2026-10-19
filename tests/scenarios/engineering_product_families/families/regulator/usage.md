# REGULATOR Family

Each fragment selects one declared `binding_key` and supplies only its parameters.
Sphere inputs are `diameter_m` and independent `interfaces` (key, origin, frame,
circular diameter; datums have no profile). Selection precedes Geometry.

## Inputs and root placement

Supply `external_constraints` for `high_external` and `low_external`, plus
`product_origin`. Each condition is `finite_segment` with `start_m`, `end_m`,
positive `outer_diameter_m` and `direction_relation: "same"`; its full interval
is available for placement. High/low identify pressure sides. The segment
starts toward the product and ends toward the continuing pipe; its outward
direction is start→end. Callers need not pre-solve bend centers, body placement,
angles, planes, height or scale. Existing authoritative connection stations may
be supplied through optional `node_stations`; missing source facts are not zeroes.

`standard_model` constructs its original nominal targets internally and runs
the same solver, with no caller external constraints. Backend places that
Family-local construction once at `product_origin`. `self_geometry` uses the
supplied source coordinates and expresses solved pieces relative to that origin.

## Fixed assembly and Geometry

The graph is an `IfcElementAssembly` with one body `IfcValve` and two separate
`IfcPipeFitting` adapters. The root owns no geometry; body and adapters each own
one fragment. Real body output selects `regulator_body_asset` directly with
`uniform_scale=body_uniform_scale` in `[0.4,1]` and no Geometry parameters.
Runtime scales the mapped body and its two actual disk interfaces together.
Function fragments use scale one and receive already sized parameters.
Both internal joins
require opposed normals, full-frame coincidence and equal OD. The two external
interfaces remain distinct and retain their respective source ODs.

High pairs with the body's local left (-X) disk, low with right (+X); this
assembly convention does not infer pressure flow from the source IFC. The body
may translate, change height and yaw while upright. The mapped binding reuses
the exact BREP recorded in `geometry/regulator_body/provenance.json` and its
actual interfaces. Node construction selects `sphere_geometry` with nominal
internal ports in the same fragment.

Detailed adapters select the same exact `curved_two_port` Geometry.
Detailed outer ends match their finite sources and ODs; inner ends match the
body's `0.15 × scale` m disks and local -Y reference datum. Bends may have different angles,
radii, planes and poses. The detailed design domain is 0°..150°, with 0° using
the shared straight branch. No extra compensation segment or private bend
Geometry is introduced. Compact/node adapters use spheres with complete nominal
ports, checked through the same interface chain.

## Internal three-level selection

The Program tries detailed body and bends at scale one, then uses its bounded
scale search over `[0.4,1]`. Only when no detailed layout is found, it tries a
real body with spherical adapters, again at scale one before the same bounded
scale search. A remaining layout miss enters `node`. Callers provide facts, not
a construction selector. The scale is linear for the body shape, port spacing,
body disks and adapter inner ports; source axes, finite windows and external ODs
stay unchanged. The bounded search finds witnesses but does not certify every
scale in a continuous interval infeasible. Invalid/degenerate input, numerical
capacity or placement precision errors, author exceptions, Geometry failures and
Codec failures are never converted to symbolic success. Only the selected
proposal is constructed.

## Compact construction

Compact retains the real upright body at the selected scale. It solves finite source
stations whose horizontal separation equals `1.7874007936507978 × scale` m.
Each body disk lies vertically above its source station. Heading
follows those stations; common disk height in metres is
`max(P_high.z,P_low.z)+0.5*max(high_OD,low_OD,0.15 × scale)`.
Different source heights may give different vertical adapter distances. This is
a REG symbolic layout rule, not a real connecting tube or a clearance certificate.

Finite-window candidates reuse the numeric root calculation. Opposed collinear
sources retain their direction order and use an interior feasible interval when
positive continuing PIPE residuals are available. Detailed candidates obey the
same ordering: inverse-order candidates are discarded while selection continues.
A sole boundary solution without positive residual is a layout miss.

## Node construction and station authority

Node replaces the entire assembly's material outline, including the body, with
symbolic spheres. `node_stations` is `unavailable` by default, or `confirmed` with
independent `high_m` and `low_m` points. These are connection facts, not selectors.
Ordinary source projection supplies confirmed points only after both exact
Host/port associations resolve. The serial permission owner may supply a common
interior station selected from both current permissions. Family checks every
confirmed point against its finite source axis/window. Missing authority returns
`connection_station_unresolved`; contradictory fixed points return
`node_station_not_permitted`. Rewritten serial A/B never implicitly authorize
their A endpoints as node stations.

Coincident node stations retain both external identities, axes, references and
ODs. Each internal body port uses OD 0.15 m at its corresponding station, with
normal -Z and reference -Y; the adapter matches it with opposed normal.
The body sphere lies midway between stations and uses the largest external or
body OD; each adapter uses its largest port OD. Display size does not move ports
or cut planes. Four components, three fragments and both joins stay complete.

Exact-source and shared cut-interval checks apply to every level. Detailed and
compact serial results require `t<h<l<b`; a confirmed zero-span node may use
`t<h=l<b`, retaining two positive PIPE sections and all whole-group checks.
A source-end node has valid interfaces and zero-length adjustment; it is not
skipped or missing.

## Parameters, receipts and evidence limits

Detailed adapters supply `ports` and tagged `shape` (`straight` or `curved`).
Each sphere option instead receives `diameter_m` and `interfaces`, preserving
each origin, orthogonal frame, key and circular OD. Solved candidate parameters
are prefixed `body_`, `high_` or `low_`; unused candidate parameters are absent.
Mapped body output records `body_uniform_scale`; a node body has no mapped scale.
`construction` records the selected
level. `default_reason` is `none`,
`detailed_layout_not_found`, `detailed_turn_unsupported`,
`detailed_and_compact_layout_not_found`, or
`detailed_turn_unsupported_and_compact_not_found`.

Family `SOLVED` is a numerical proposal. Backend validates parameters and poses,
builds the three selected pieces and checks their independently evaluated numerical interfaces in
world coordinates. An unsolved proposal builds no Geometry. Detailed contact
is a solid body disk against an annular bend end: matching frames and OD do not
prove through flow, sealing or manufacturing contact. Compact/node ports prove
nominal coincidence only. Port checks and cut intervals do not certify openings,
continuous material, BREP clearance or physical installation feasibility; those
claims require independent evidence.

Detailed shape coordinates use a resource-local frame. Overall translation and rotation are carried by the existing fragment `local_transform`; solved dimensions, relative directions and the original actual port roll are preserved. Local interface references are transformed with the frame rather than regenerated from local Z. The nominal sphere path keeps its existing translation-only placement because rotating a sphere only rewrites its independent interface metadata.
