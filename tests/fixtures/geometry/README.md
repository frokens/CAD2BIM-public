# Trimmed extrusion preview regression

`trimmed-extrusion-face.brep` is a single valid, metre-scale OCCT extrusion face
extracted from an agent-authored three-port pipe fitting. It contains no source
entity identifiers, attributes or global location coordinates. The original full canonical geometry SHA-256 is
`46e3601183141ecc829b282169fea3029f6970005a5629d6409c092513cacbad`.

OCCT's default relative deflection 0.001 and angular deflection 0.1 produced
triangles spanning approximately 0.40 m away from this face and about 12.6%
excess mesh area. The regression checks actual triangle area and surface
distance, including location and reversed orientation. It tests derived display
geometry; it does not establish the original fitting's material continuity.
