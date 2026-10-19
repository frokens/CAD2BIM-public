# GAS_METER middle-port shared connector

One function supplies both left and right `IfcPipeFitting` instances. Each Shape-only build retains six exterior solids: a full-length nipple, rounded root shoulder, chamfered hex nut and three shallow bands. Edge treatments are capped relative to actual radius and collar length, retaining the six-part shape.

Inputs are `od_m` and solved `length_m`. Both entries share pure `parameters.py:prepare(ctx)` for finiteness, resolution and terminal-margin checks. The last band ends at `2c+.0092 m`, with `c=min(.018,max(.008,.22OD))`; the function keeps another 1.2 mm of visible nipple after that band. Native construction converts metre inputs to millimetres, builds the six solids and scales the result back to metres.

`interfaces.py:evaluate(ctx)` declares profile-less `body_joint` at local `(0,0,0)`, outward `-X`, reference `+Z`, and circular `gas_end` at `(length_m,0,0)`, outward `+X`, with diameter `od_m`. The Family places the two body-joint frames against the selected body's z=.14 m internal attachment frames with opposed direction and complete frame orientation. Named interface evaluation is independent of physical disk inspection. The nipple disks are solid and bands are decorative; through-flow, sealing and external pipe installation remain unproved.
