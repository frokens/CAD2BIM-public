# SERVICE_SADDLE Geometry

`interfaces.py:evaluate(ctx)` and `main.py:build(ctx)` share the pure numeric
`parameters.py:prepare(ctx)` rules for host and branch ODs, local unit branch direction,
full-frame local GAS reference and the closed solved `shape` object.
`shape.mode=detailed` carries collar half-length and branch port station.
It constructs a single appearance-oriented
OCCT saddle in local metres from a collar, cylindrical root and neck,
register and flange contour. Every branch solid and its visible recess follow the true
local oblique axis. The host bearing cylinder is subtracted from this saddle
product only; no host pipe is included in the returned shape.

The collar exterior is initially a solid cylinder. A simplified coaxial stepped root, neck,
register and flange outline is revolved once: the root radius is `.65 Db`,
and the neck radius is `.5 Db`. This deliberately approximates the reference
exterior, replacing its tapered root and small conical transition with simple
cylindrical surfaces. Decorative collar-edge and neck-seam fillets are omitted.
The host bearing radius, collar length, flange envelope, total port station
and actual annular port retain their dimensions. The visible port has inner
radius `.43 Db` and a blind recess of axial depth `.5 Db / q`, where `q` is
the branch direction component perpendicular to the host. The recess is
formed in the same revolved profile. The exterior bodies are joined before
one subtraction creates the cylindrical host opening. The original
valid-solid and actual annular-port checks remain.

The branch recess deliberately does not communicate with the host opening.
This is an approximate exterior/installation representation, not a fluid-path
model, manufacturing profile, certified wall/seal design or proof of gas
connectivity. A downstream flow or fabrication task must not infer an internal
passage from the named interface or the visible annular port.

`host_datum` is a profile-less attachment at the local host/branch axis
intersection. `gas_branch` is calculated as branch direction times the metre
port station before the shape's millimetre construction and metre conversion,
with outward axis equal to the real branch direction, the passed reference
direction and OD `Db`. The shape must be one valid closed solid. A Geometry
build alone does not establish installation, host cutting, port connection,
pressure rating or IFC writing.

Nominal nodes use the independent `connection_sphere` resource selected by the
Family. This function constructs only the detailed saddle; construction errors
propagate and unknown modes fail.
