# RISER body Geometry candidate

The `modeling_function` receives Family-local bottom/top points, a unit axis and frame X direction, actual axial height, outside diameter, and sourced section. It builds one straight body and evaluates `bottom` and `top` numerically; the independent shape checks retain the planar-end obligations. The Geometry function does not choose a source height, installation origin, host, neighboring pipe, or IFC connection.

`section.known_wall_thickness_m=[]` means the wall and bore are unknown. The valid capped OD solid is an **occupancy envelope**. Its planar ends are geometric boundaries for modeling and interface checks; they do not assert a physical cap, a filled bore, or a known wall thickness. `[t]` with `0<t<OD/2` creates one hollow annular material solid and real annular end faces. No inner diameter is inferred from OD when the wall is unknown.

`local_top_m` must agree with `local_bottom_m + height_m × axis_unit`; `frame_x` is perpendicular to the axis. The shared pure `parameters.py:prepare(ctx)` checks these rules. `interfaces.py:evaluate(ctx)` uses the declared bottom and top endpoints, with outward axes negative and positive `axis_unit`, and the numeric Family reference. Shape-only `main.py:build(ctx)` extrudes exactly `height_m`, requires one valid solid, and retains each actual planar terminal face's area and wire checks. Independent BREP inspection can compare the actual end faces and outward normals with the evaluated named datums.

The Schema closes all parameter keys and permits positive finite modeled quantities within runtime numeric and document limits. CADQuery/OCCT may reject representable but too small or otherwise difficult features. Such a build diagnostic is a technical limit, not a new business minimum height or OD. This candidate is not registered and does not prove an IFC or network connection.

The current SYSTEM Family supplies the canonical local body from `[0,0,0]` to `[0,0,H]` with axis `+Z` and reference `+X`. Its existing fragment placement carries the installation translation and rotation.
