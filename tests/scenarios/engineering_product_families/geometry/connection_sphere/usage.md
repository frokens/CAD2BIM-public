# Connection sphere

This independent Geometry constructs one complete sphere at local zero with
positive `diameter_m`. The Family selects this expression before construction;
this function does not choose a product layout or recover a failed build.

`interfaces` contains 1–64 named interfaces. Each closed object has `kind`, `key`,
`origin_m`, `axis`, and `reference_direction`. A `circular` interface additionally
has its own positive `diameter_m`; a `datum` has no profile. Keys use lowercase
letters, digits and underscores, begin with a letter, and have at most 64
characters. The existing interface owner checks unique keys, finite rigid frames,
profiles, count and serialized size. Invalid inputs and construction errors
propagate through the existing execution boundary.

Interface positions, full frames and circular ODs are independent of the sphere
diameter and of one another. They may coincide or be offset from the sphere
center. Family fragment placement transforms the sphere and all interfaces
together. Nominal circular sections are declarations, not measured sphere faces
or openings. The shape does not establish installed envelope, bore, sealing,
trim interval or material continuity. Source connection and finite station
eligibility remain with the existing Family and station owners.
