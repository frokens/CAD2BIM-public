# Thin trimmed face display regression

This 3,053-byte BREP is an actual valid face extracted from an independently
rebuilt, immutable native Geometry result. Its source canonical SHA-256 is
`77e17e1e240cc17c2c8f4306f82e07b501272f87005768bc386b75602da0d5d8`;
the zero-based face index is 18. The fixture SHA-256 is
`ba4f90bf7c69810a5fba79d284efa804d1adca02c36d26d5bf085625c35a1b63`.

The native face area is approximately `2.478682224474063e-7` square metres.
OCCT's initial and one retried display meshes are safely renderable but have
large relative area differences. The real Codec regression requires a warning
PNG instead of treating the 5% display heuristic as canonical invalidity.
It does not certify this display approximation or any containing product.
