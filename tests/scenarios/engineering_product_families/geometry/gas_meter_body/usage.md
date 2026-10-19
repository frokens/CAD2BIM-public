# GAS_METER middle-port mapped body

This mapped body uses the **selected Family-adjusted body Geometry v4**, not Golden07 or the abandoned z=0 BREP. The exact original canonical BREP SHA is `19f60010…`, 628211 bytes. It independently reopens as 96 valid solids and 704 faces; all of them form the one `IfcFlowMeter` body. The main housing is fixed at 400×260×360 mm, centered at local z=.14 m, and never scales.

The published v4 `design_datum` is `(0,0,.14)` m. `left_attachment` and `right_attachment` are `(-.2,0,.14)` and `(.2,0,.14)` m, with outward `-X` and `+X`, both referenced to `+Z`. These sit at the **middle of the housing**. They are profile-less internal assembly frames. The creation source's older z=0 interface calls were intentionally replaced by v4's public annotations; that creation source does not override the selected v4 contract.

The numeric Family maps this local datum onto its solved product datum. A body fragment at yaw θ therefore has translation `C−O−(0,0,.14)` relative to product origin O, while the two middle attachment frames map to `C±.2u`. The two gas ends belong only to the separate connector function. Neither the source body nor the attachment annotations prove an internal gas passage, seal or metrology behavior.

For a different TASK Scope, import these exact body bytes and establish a new mapped resource identity. A historical resource alias from another TASK cannot be revised or bound across Scope.
