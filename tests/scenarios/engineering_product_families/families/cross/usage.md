# CROSS Family

Use four standard `external_constraints`, keyed `side_a` through `side_d`.
Each `condition` supplies `kind=finite_segment`, world-space `start_m` and
`end_m`, `outer_diameter_m`, and `direction_relation=same`. Supply
`product_origin` separately. Start retains A toward the product and end
retains B toward the continuing pipe; the outward direction is A→B.
There are no public installation parameter groups. Solved ODs feed the
existing Geometry mappings and actual-interface profile checks.

The pure numeric Program first verifies one common three-dimensional line
intersection. It computes a finite legal chamber-radius interval from all
four source windows, inner and outer cap separation, and both branches of
the nonmonotonic neck-length inequality. Each takeoff must be strictly beyond
its outer and inner transition end; the remaining straight neck has only a
numeric representability margin, not a fixed physical minimum. It chooses the legal radius nearest
`0.68 max(OD)`, then each legal takeoff nearest `1.5 OD`. It returns one
`cross_body` fragment with four model-owned external interfaces.
`INFEASIBLE` records a proven source/shape conflict; `AMBIGUOUS` records
unresolved arithmetic or insufficient numeric resolution. Neither status
requests Geometry construction.

The design derives its own port wall
`min(0.04 OD_i, 0.032 max(OD))` and chamber wall and seam
`0.04 max(OD)`. Those dimensions describe this fitting's bore; they do
not assert a match to an external pipe's unknown wall thickness. The
`standard_model` entry creates the original nominal targets internally and
returns the nominal local shape through the same solver; `realize`
uses the separately supplied world `product_origin` as the local datum.

Detailed shape coordinates use a resource-local frame. Overall translation and rotation are carried by the existing fragment `local_transform`; solved dimensions, relative directions and the original actual port roll are preserved. Local interface references are transformed with the frame rather than regenerated from local Z.
