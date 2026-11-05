"""AI 3D Studio's Blender script. Runs *inside* Blender, never in the backend process.

Started only by :mod:`ai3d_engine.blender` as::

    blender --background --factory-startup --python-exit-code 1 \
        --python ai3d_blender.py -- <command> <request.json> <result.json>

The request is a JSON object written by the engine (file paths and numbers,
never code); the result is written as JSON. Commands:

``inspect``
    Open a GLB or FBX file in an empty scene and report what is in it:
    triangles, size (in glTF's Y-up axes), image textures that hold pixels.
``export_fbx``
    Import a GLB and write an FBX with its textures embedded.
``import_fbx``
    Import an FBX (Blender converts its units to metres and its axes) and write
    it as a GLB with its textures.
``bake``
    Import a (textured) GLB, make a reduced copy, give it new UVs and bake the
    original's base colour onto it (Cycles, selected-to-active); write it as GLB.
``remesh``
    Import a GLB and rebuild its surface: QuadriFlow (an even quad layout) or a
    voxel remesh. Measures how far the new surface is from the original, carries
    vertex colours over, and on request bakes base colour, normal and ambient-
    occlusion maps onto new UVs; write it as GLB.

Blender's coordinate system is Z-up; the glTF importer/exporter convert from/to
glTF's Y-up (x, y, z)_gltf = (x, z, -y)_blender. Blender's unit is the metre.
"""

import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

import bmesh  # type: ignore[import-not-found]
import bpy  # type: ignore[import-not-found]
import numpy as np
from mathutils.bvhtree import BVHTree  # type: ignore[import-not-found]

COMMANDS = ("inspect", "export_fbx", "import_fbx", "bake", "remesh")


class RequestError(Exception):
    pass


# -- scene helpers ----------------------------------------------------------------------


def _empty_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0


def _import(path: Path) -> list[Any]:
    """Import a GLB/glTF or FBX file; return the mesh objects it created."""
    before = set(bpy.data.objects)
    suffix = path.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(path))
    else:
        raise RequestError(f"Unsupported file type: {suffix}")
    return [o for o in bpy.data.objects if o not in before and o.type == "MESH"]


def _triangles(obj: Any) -> int:
    mesh = obj.data
    totals = np.zeros(len(mesh.polygons), dtype=np.int64)
    mesh.polygons.foreach_get("loop_total", totals)
    return int((totals - 2).sum())


def _world_points(obj: Any) -> np.ndarray:
    mesh = obj.data
    co = np.zeros(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", co)
    points = co.reshape(-1, 3)
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def _bounds(objects: list[Any]) -> tuple[np.ndarray, np.ndarray]:
    points = [_world_points(o) for o in objects if len(o.data.vertices)]
    if not points:
        return np.zeros(3), np.zeros(3)
    stacked = np.concatenate(points)
    return stacked.min(axis=0), stacked.max(axis=0)


def _to_gltf_axes(vector: np.ndarray) -> list[float]:
    """Blender (x, y, z) Z-up → glTF (x, z, -y) Y-up; sizes are taken as absolute."""
    return [float(vector[0]), float(vector[2]), float(vector[1])]


def _image_textures(objects: list[Any]) -> list[dict[str, Any]]:
    """Image texture nodes in the objects' materials whose image actually has pixels."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for obj in objects:
        for slot in obj.material_slots:
            material = slot.material
            if material is None or not material.use_nodes or material.node_tree is None:
                continue
            for node in material.node_tree.nodes:
                image = getattr(node, "image", None) if node.type == "TEX_IMAGE" else None
                if image is None or image.name in seen:
                    continue
                seen.add(image.name)
                width, height = (int(v) for v in image.size)
                linked = any(link.is_valid for link in node.outputs["Color"].links)
                found.append(
                    {
                        "name": image.name,
                        "width": width,
                        "height": height,
                        "has_pixels": width > 0 and height > 0,
                        "packed": image.packed_file is not None,
                        "linked": linked,
                    }
                )
    return found


def _has_colours(obj: Any) -> bool:
    """True when the mesh has a colour attribute whose values actually vary."""
    attributes = getattr(obj.data, "color_attributes", None)
    if not attributes:
        return False
    data = attributes[0].data
    values = np.empty(len(data) * 4, dtype=np.float32)
    data.foreach_get("color", values)
    return bool(len(values)) and float(values.reshape(-1, 4)[:, :3].std()) > 0.002


def _facts(objects: list[Any]) -> dict[str, Any]:
    lo, hi = _bounds(objects)
    images = _image_textures(objects)
    textured_objects = 0
    for obj in objects:
        own = _image_textures([obj])
        if any(i["has_pixels"] and i["linked"] and i["packed"] for i in own):
            textured_objects += 1
    return {
        "objects": len(objects),
        "triangles": sum(_triangles(o) for o in objects),
        "size": _to_gltf_axes(hi - lo),
        "images": images,
        "textured": any(i["has_pixels"] and i["linked"] for i in images),
        # Meshes whose material shows an image that is stored inside the file.
        "textured_objects": textured_objects,
        # Meshes with per-vertex colours (e.g. from local AI generation).
        "coloured_objects": sum(1 for o in objects if _has_colours(o)),
    }


def _select_only(objects: list[Any], active: Any | None = None) -> None:
    for obj in bpy.context.scene.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = active if active is not None else objects[0]


def _path(request: dict[str, Any], key: str) -> Path:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise RequestError(f"Missing path: {key}")
    return Path(value)


# -- commands ---------------------------------------------------------------------------


def inspect(request: dict[str, Any]) -> dict[str, Any]:
    source = _path(request, "input")
    if not source.is_file():
        raise RequestError("The file does not exist.")
    _empty_scene()
    objects = _import(source)
    if not objects:
        raise RequestError("The file contains no meshes.")
    return _facts(objects)


def _direct_base_colour_textures(objects: list[Any]) -> list[dict[str, Any]]:
    """Connect base-colour textures straight to the shader, as FBX expects.

    glTF multiplies the base-colour texture by ``baseColorFactor``; Blender's
    glTF importer models that with a Multiply node, which the FBX exporter does
    not follow (it would write the material without its texture). FBX has no
    such tint, so the texture is connected directly and the factor that is
    dropped is reported back.
    """
    dropped: list[dict[str, Any]] = []
    for material in {s.material for o in objects for s in o.material_slots if s.material}:
        if not material.use_nodes or material.node_tree is None:
            continue
        tree = material.node_tree
        for bsdf in (n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"):
            base = bsdf.inputs["Base Color"]
            if not base.links or base.links[0].from_node.type != "MIX":
                continue
            mix = base.links[0].from_node
            textures = [
                link.from_node
                for socket in mix.inputs
                if socket.enabled
                for link in socket.links
                if link.from_node.type == "TEX_IMAGE"
            ]
            if len(textures) != 1:
                continue
            factor = next(
                (
                    [round(float(c), 4) for c in socket.default_value[:3]]
                    for socket in mix.inputs
                    if socket.enabled and not socket.links and socket.type == "RGBA"
                ),
                None,
            )
            tree.links.new(textures[0].outputs["Color"], base)
            if factor is not None and any(abs(c - 1.0) > 1e-3 for c in factor):
                dropped.append({"material": material.name, "factor": factor})
    return dropped


def _name_packed_images() -> None:
    """Give every packed image its own file name.

    Images from a GLB are packed with an empty path, and the FBX exporter embeds
    each path only once — without distinct names only the first texture would be
    stored. Nothing is written to these paths (embedded textures are not copied).
    """
    extensions = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
    for index, image in enumerate(bpy.data.images):
        if image.packed_file is None:
            continue
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", image.name).strip("_") or "texture"
        extension = extensions.get(image.file_format, "png")
        image.filepath_raw = f"{index:03d}_{stem}.{extension}"


def export_fbx(request: dict[str, Any]) -> dict[str, Any]:
    source, dest = _path(request, "input"), _path(request, "output")
    _empty_scene()
    objects = _import(source)
    if not objects:
        raise RequestError("The model contains no meshes.")
    dropped = _direct_base_colour_textures(objects)
    _name_packed_images()
    bpy.ops.export_scene.fbx(
        filepath=str(dest),
        use_selection=False,
        object_types={"MESH", "EMPTY"},
        # Scene unit is the metre (scale_length 1). "FBX Units Scale" declares the file
        # unit as the metre (UnitScaleFactor 100, i.e. 100 cm) and writes coordinates in
        # metres with object scale 1, instead of centimetres plus a ×100 object scale.
        apply_unit_scale=True,
        apply_scale_options="FBX_SCALE_UNITS",
        global_scale=1.0,
        axis_forward="-Z",
        axis_up="Y",
        use_mesh_modifiers=True,
        mesh_smooth_type="FACE",
        use_tspace=False,
        add_leaf_bones=False,
        bake_anim=False,
        path_mode="COPY",
        embed_textures=True,
        colors_type="SRGB",  # per-vertex colours, stored as sRGB like other FBX tools expect
    )
    return {"source": _facts(objects), "dropped_colour_factors": dropped}


def import_fbx(request: dict[str, Any]) -> dict[str, Any]:
    source, dest = _path(request, "input"), _path(request, "output")
    if source.suffix.lower() != ".fbx":
        raise RequestError("Only FBX files are converted here.")
    _empty_scene()
    objects = _import(source)
    if not objects:
        raise RequestError("The FBX file contains no meshes.")
    facts = _facts(objects)
    bpy.ops.export_scene.gltf(
        filepath=str(dest),
        export_format="GLB",
        use_selection=False,
        export_yup=True,
        export_apply=True,
        export_texcoords=True,
        export_normals=True,
        export_materials="EXPORT",
        export_image_format="AUTO",
        export_animations=False,
    )
    return {"source": facts}


def _max_deviation(high: list[Any], low: Any, limit: int = 50_000) -> float:
    """Largest distance from (a sample of) the original's vertices to the reduced surface."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    tree = BVHTree.FromObject(low, depsgraph)
    points = np.concatenate([_world_points(o) for o in high])
    if len(points) > limit:
        points = points[np.random.default_rng(0).choice(len(points), limit, replace=False)]
    # low has identity transform (applied), so world == local
    worst = 0.0
    for point in points:
        hit = tree.find_nearest(point.tolist())
        if hit[0] is not None:
            worst = max(worst, float(hit[3]))
    return worst


def _emit_base_colour(objects: list[Any]) -> dict[str, Any]:
    """Make the objects' materials emit their base colour, for an EMIT bake.

    Whatever feeds the Principled BSDF's Base Color (texture, tint multiply, plain
    colour) is routed into an Emission shader on the material output. Returns
    which materials were converted, which could not be (no Principled BSDF), and
    the first material's metallic/roughness values for the new material.
    """
    converted: list[str] = []
    unsupported: list[str] = []
    metallic: float | None = None
    roughness: float | None = None
    for material in {s.material for o in objects for s in o.material_slots if s.material}:
        tree = material.node_tree if material.use_nodes else None
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None) if tree else None
        output = (
            next(
                (n for n in tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None
            )
            if tree
            else None
        )
        if tree is None or bsdf is None or output is None:
            unsupported.append(material.name)
            continue
        if metallic is None and not bsdf.inputs["Metallic"].links:
            metallic = float(bsdf.inputs["Metallic"].default_value)
        if roughness is None and not bsdf.inputs["Roughness"].links:
            roughness = float(bsdf.inputs["Roughness"].default_value)
        emission = tree.nodes.new("ShaderNodeEmission")
        emission.inputs["Strength"].default_value = 1.0
        base = bsdf.inputs["Base Color"]
        if base.links:
            tree.links.new(base.links[0].from_socket, emission.inputs["Color"])
        else:
            emission.inputs["Color"].default_value = base.default_value
        tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
        converted.append(material.name)
    return {
        "converted": sorted(converted),
        "unsupported": sorted(unsupported),
        "metallic": 0.0 if metallic is None else metallic,
        "roughness": 0.6 if roughness is None else roughness,
    }


def _reduced_copy(high: list[Any], diagonal: float) -> Any:
    """One joined copy of all meshes: world transform applied, no materials or UVs,
    texture seams merged (so reduction treats the surface as connected)."""
    copies = []
    for obj in high:
        copy = obj.copy()
        copy.data = obj.data.copy()
        bpy.context.scene.collection.objects.link(copy)
        copies.append(copy)
    _select_only(copies)
    if len(copies) > 1:
        bpy.ops.object.join()
    low = bpy.context.view_layer.objects.active
    low.name = "Model"
    world = low.matrix_world.copy()
    low.parent = None
    low.matrix_world = world
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    low.data.materials.clear()
    for layer in list(low.data.uv_layers):
        low.data.uv_layers.remove(layer)
    mesh = bmesh.new()
    mesh.from_mesh(low.data)
    bmesh.ops.remove_doubles(mesh, verts=mesh.verts, dist=diagonal * 1e-7)
    mesh.to_mesh(low.data)
    mesh.free()
    return low


def _reduce(low: Any, target: int) -> None:
    """Collapse decimation to about ``target`` triangles."""
    _select_only([low])
    modifier = low.modifiers.new("Reduce", "DECIMATE")
    modifier.decimate_type = "COLLAPSE"
    modifier.ratio = min(1.0, target / max(_triangles(low), 1))
    modifier.use_collapse_triangulate = True
    bpy.ops.object.modifier_apply(modifier=modifier.name)


def _unwrap(low: Any) -> None:
    _select_only([low])
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(
        angle_limit=math.radians(66.0), island_margin=0.003, area_weight=0.0, correct_aspect=True
    )
    bpy.ops.object.mode_set(mode="OBJECT")


def _bake_target(low: Any, size: int, surface: dict[str, Any]) -> tuple[Any, Any]:
    """New image + material on the reduced mesh; returns (image, material).

    The image is the active node (the bake target) but is linked to the shader
    only after baking, so it is not also an input while it is being written.
    """
    image = bpy.data.images.new("BaseColor", width=size, height=size, alpha=False)
    image.colorspace_settings.name = "sRGB"
    material = bpy.data.materials.new("Baked")
    material.use_nodes = True
    nodes = material.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")
    bsdf.inputs["Metallic"].default_value = surface["metallic"]
    bsdf.inputs["Roughness"].default_value = surface["roughness"]
    tex = nodes.new("ShaderNodeTexImage")
    tex.name = "BaseColorTexture"
    tex.image = image
    nodes.active = tex
    low.data.materials.append(material)
    low.data.polygons.foreach_set("material_index", np.zeros(len(low.data.polygons), np.int32))
    return image, material


def bake(request: dict[str, Any]) -> dict[str, Any]:
    source, dest = _path(request, "input"), _path(request, "output")
    texture_path = _path(request, "texture")
    target = int(request["target_triangles"])
    size = int(request["texture_size"])
    samples = int(request.get("samples", 4))
    margin = int(request.get("margin", 16))
    if target < 4 or not 64 <= size <= 16384:
        raise RequestError("Invalid target or texture size.")
    started = time.monotonic()
    _empty_scene()
    high = _import(source)
    if not high:
        raise RequestError("The model contains no meshes.")
    before = _facts(high)
    if not before["textured"]:
        raise RequestError("The model has no image texture to bake from.")
    diagonal = float(np.linalg.norm(before["size"])) or 1.0

    low = _reduced_copy(high, diagonal)
    triangles_in = _triangles(low)
    _reduce(low, target)
    triangles_out = _triangles(low)
    if triangles_out < 4:
        raise RequestError(f"Reduction left only {triangles_out} triangles.")
    reduced_at = time.monotonic()
    _unwrap(low)
    uv_at = time.monotonic()
    surface = _emit_base_colour(high)
    image, material = _bake_target(low, size, surface)

    # Bake the original's base colour (as emission: no lighting, independent of
    # metallic/transmission) from the original onto the reduced copy. Rays start
    # just outside the copy (cage extrusion) and reach the original within twice that.
    deviation = _max_deviation(high, low)
    extrusion = deviation * 1.5 + diagonal * 1e-3
    ray = extrusion * 2.0
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = samples
    scene.render.bake.margin = margin
    scene.render.bake.margin_type = "EXTEND"
    _select_only([*high, low], active=low)
    bpy.ops.object.bake(
        type="EMIT",
        use_selected_to_active=True,
        cage_extrusion=extrusion,
        max_ray_distance=ray,
        margin=margin,
        target="IMAGE_TEXTURES",
        use_clear=True,
    )
    baked_at = time.monotonic()
    nodes = material.node_tree.nodes
    material.node_tree.links.new(
        nodes["BaseColorTexture"].outputs["Color"], nodes["Principled BSDF"].inputs["Base Color"]
    )
    image.filepath_raw = str(texture_path)
    image.file_format = "PNG"
    image.save()

    _select_only([low])
    bpy.ops.export_scene.gltf(
        filepath=str(dest),
        export_format="GLB",
        use_selection=True,
        export_yup=True,
        export_apply=True,
        export_texcoords=True,
        export_normals=True,
        export_materials="EXPORT",
        export_image_format="AUTO",
    )
    return {
        "triangles_before": before["triangles"],
        "triangles_merged": triangles_in,
        "triangles_after": triangles_out,
        "texture_size": size,
        "samples": samples,
        "max_deviation": deviation,
        "cage_extrusion": extrusion,
        "max_ray_distance": ray,
        "materials_baked": surface["converted"],
        "materials_unsupported": surface["unsupported"],
        "metallic": surface["metallic"],
        "roughness": surface["roughness"],
        "seconds": {
            "reduce": round(reduced_at - started, 3),
            "uv": round(uv_at - reduced_at, 3),
            "bake": round(baked_at - uv_at, 3),
            "total": round(time.monotonic() - started, 3),
        },
    }


# -- remesh ----------------------------------------------------------------------------

# QuadriFlow refuses meshes with edges shorter than 1e-4 Blender units, which models a
# unit or so across (AI output, unscaled scans) have plenty of; it runs on a copy scaled
# to this diagonal and is scaled back afterwards.
_REMESH_DIAGONAL = 1000.0


def _surface_samples(obj: Any, count: int, seed: int) -> np.ndarray:
    """``count`` points spread over the surface by area (world space)."""
    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    mesh.transform(obj.matrix_world)
    bmesh.ops.triangulate(mesh, faces=mesh.faces)
    corners = np.array([[list(v.co) for v in f.verts] for f in mesh.faces], dtype=np.float64)
    mesh.free()
    if len(corners) == 0:
        return np.zeros((0, 3))
    area = np.linalg.norm(
        np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1
    )
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(corners), count, p=area / area.sum())
    r1, r2 = rng.random(count), rng.random(count)
    s = np.sqrt(r1)
    tri = corners[pick]
    return (
        (1 - s)[:, None] * tri[:, 0]
        + (s * (1 - r2))[:, None] * tri[:, 1]
        + (s * r2)[:, None] * tri[:, 2]
    )


def _distances(points: np.ndarray, target: Any) -> np.ndarray:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    tree = BVHTree.FromObject(target, depsgraph)
    out = np.empty(len(points))
    for i, point in enumerate(points):
        hit = tree.find_nearest(point.tolist())
        out[i] = hit[3] if hit[0] is not None else np.inf
    return out


def _fidelity(source: Any, result: Any, diagonal: float, count: int = 20_000) -> dict[str, float]:
    """Distances between the two surfaces, both ways, as fractions of the diagonal."""
    d = np.concatenate(
        [
            _distances(_surface_samples(source, count, 1), result),
            _distances(_surface_samples(result, count, 2), source),
        ]
    ) / max(diagonal, 1e-12)
    return {
        "mean": float(d.mean()),
        "p95": float(np.percentile(d, 95)),
        "max": float(d.max()),
    }


def _scaled(obj: Any, factor: float) -> None:
    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    bmesh.ops.scale(mesh, vec=(factor, factor, factor), verts=mesh.verts)
    mesh.to_mesh(obj.data)
    mesh.free()


def _duplicate(obj: Any, name: str) -> Any:
    copy = obj.copy()
    copy.data = obj.data.copy()
    copy.name = name
    bpy.context.scene.collection.objects.link(copy)
    return copy


def _colour_attribute(obj: Any) -> tuple[str, str] | None:
    """Name and domain (POINT or CORNER) of the mesh's first colour attribute."""
    attributes = getattr(obj.data, "color_attributes", None)
    if not attributes or not len(attributes):
        return None
    return attributes[0].name, attributes[0].domain


def _transfer_colours(source: Any, target: Any, name: str, domain: str) -> None:
    """Vertex colours from the nearest point of the original surface (interpolated).

    glTF files from different tools store them per vertex (POINT) or per face
    corner (CORNER); the transfer uses the same domain as the original.
    """
    target.data.color_attributes.new(name=name, type="BYTE_COLOR", domain=domain)
    _select_only([target])
    modifier = target.modifiers.new("Colours", "DATA_TRANSFER")
    modifier.object = source
    if domain == "POINT":
        modifier.use_vert_data = True
        modifier.data_types_verts = {"COLOR_VERTEX"}
        modifier.vert_mapping = "POLYINTERP_NEAREST"
        modifier.layers_vcol_vert_select_src = "ALL"
        modifier.layers_vcol_vert_select_dst = "NAME"
    else:
        modifier.use_loop_data = True
        modifier.data_types_loops = {"COLOR_CORNER"}
        modifier.loop_mapping = "POLYINTERP_NEAREST"
        modifier.layers_vcol_loop_select_src = "ALL"
        modifier.layers_vcol_loop_select_dst = "NAME"
    bpy.ops.object.modifier_apply(modifier=modifier.name)


def _new_image(name: str, size: int, colour: bool) -> Any:
    image = bpy.data.images.new(name, width=size, height=size, alpha=False)
    image.colorspace_settings.name = "sRGB" if colour else "Non-Color"
    return image


def _occlusion_output(tree: Any) -> Any:
    """The node group the glTF exporter reads the occlusion texture from."""
    group = bpy.data.node_groups.get("glTF Material Output")
    if group is None:
        group = bpy.data.node_groups.new("glTF Material Output", "ShaderNodeTree")
        group.interface.new_socket("Occlusion", in_out="INPUT", socket_type="NodeSocketFloat")
    node = tree.nodes.new("ShaderNodeGroup")
    node.node_tree = group
    return node


def _rebuild(low: Any, request: dict[str, Any], diagonal: float) -> None:
    """QuadriFlow or voxel remesh, on a copy scaled to _REMESH_DIAGONAL and back."""
    factor = _REMESH_DIAGONAL / diagonal
    _scaled(low, factor)
    _select_only([low])
    if request["method"] == "quadriflow":
        target = int(request["target_faces"])
        if not 100 <= target <= 2_000_000:
            raise RequestError("Invalid target face count.")
        outcome = bpy.ops.object.quadriflow_remesh(
            target_faces=target,
            use_mesh_symmetry=False,
            use_preserve_sharp=False,
            use_preserve_boundary=True,
            seed=0,
        )
        if "FINISHED" not in outcome:
            raise RequestError(
                "QuadriFlow could not rebuild this surface (it needs a connected, "
                "consistently oriented, manifold mesh)."
            )
    else:
        resolution = int(request["resolution"])
        if not 16 <= resolution <= 2000:
            raise RequestError("Invalid voxel resolution.")
        low.data.remesh_voxel_size = _REMESH_DIAGONAL / resolution
        bpy.ops.object.voxel_remesh()
    _scaled(low, 1.0 / factor)


def _bake_maps(
    high: list[Any], low: Any, maps: set[str], settings: dict[str, Any], extrusion: float
) -> list[str]:
    """Bake the requested maps onto new UVs of ``low``; returns what was baked."""
    _unwrap(low)
    surface = _emit_base_colour(high) if "base" in maps else None
    material = bpy.data.materials.new("Remeshed")
    material.use_nodes = True
    tree = material.node_tree
    bsdf = tree.nodes.get("Principled BSDF")
    if surface:
        bsdf.inputs["Metallic"].default_value = surface["metallic"]
        bsdf.inputs["Roughness"].default_value = surface["roughness"]
    low.data.materials.clear()
    low.data.materials.append(material)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = settings["samples"]
    scene.render.bake.margin = settings["margin"]
    scene.render.bake.margin_type = "EXTEND"
    images: dict[str, Any] = {}
    for kind in ("base", "normal", "occlusion"):
        if kind not in maps:
            continue
        node = tree.nodes.new("ShaderNodeTexImage")
        node.image = _new_image(kind, settings["size"], colour=kind == "base")
        tree.nodes.active = node
        if kind == "occlusion":
            _select_only([low])
            bpy.ops.object.bake(type="AO", margin=settings["margin"], target="IMAGE_TEXTURES")
        else:
            _select_only([*high, low], active=low)
            bpy.ops.object.bake(
                type="EMIT" if kind == "base" else "NORMAL",
                use_selected_to_active=True,
                cage_extrusion=extrusion,
                max_ray_distance=extrusion * 2.0,
                normal_space="TANGENT",
                margin=settings["margin"],
                target="IMAGE_TEXTURES",
                use_clear=True,
            )
        node.image.filepath_raw = str(settings["folder"] / f"{settings['stem']}-{kind}.png")
        node.image.file_format = "PNG"
        node.image.save()
        images[kind] = node
    links = tree.links
    if "base" in images:
        links.new(images["base"].outputs["Color"], bsdf.inputs["Base Color"])
    if "normal" in images:
        normal_map = tree.nodes.new("ShaderNodeNormalMap")
        links.new(images["normal"].outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])
    if "occlusion" in images:
        split = tree.nodes.new("ShaderNodeSeparateColor")
        links.new(images["occlusion"].outputs["Color"], split.inputs["Color"])
        links.new(split.outputs["Red"], _occlusion_output(tree).inputs["Occlusion"])
    return list(images)


def remesh(request: dict[str, Any]) -> dict[str, Any]:
    source_path, dest = _path(request, "input"), _path(request, "output")
    maps = set(request.get("maps", []))
    size = int(request.get("texture_size", 2048))
    if request.get("method") not in {"quadriflow", "voxel"}:
        raise RequestError("Invalid remesh method.")
    if not maps <= {"base", "normal", "occlusion"} or not 64 <= size <= 16384:
        raise RequestError("Invalid maps or texture size.")
    started = time.monotonic()
    _empty_scene()
    high = _import(source_path)
    if not high:
        raise RequestError("The model contains no meshes.")
    before = _facts(high)
    diagonal = float(np.linalg.norm(before["size"])) or 1.0
    colour = next((c for c in (_colour_attribute(o) for o in high) if c), None)

    source = _reduced_copy(high, diagonal)  # joined, world space, seams merged
    source.name = "Source"
    low = _duplicate(source, "Model")
    _rebuild(low, request, diagonal)
    remeshed_at = time.monotonic()
    faces = len(low.data.polygons)
    if faces < 4:
        raise RequestError(f"Remeshing left only {faces} faces.")
    quads = sum(1 for p in low.data.polygons if len(p.vertices) == 4)
    fidelity = _fidelity(source, low, diagonal)
    measured_at = time.monotonic()

    if colour:
        _transfer_colours(source, low, *colour)
    if before["textured"]:
        maps.add("base")  # a texture cannot survive new UVs without baking it
    baked: list[str] = []
    if maps:
        settings = {
            "size": size,
            "samples": int(request.get("samples", 4)),
            "margin": int(request.get("margin", 16)),
            "folder": dest.parent,
            "stem": dest.stem,
        }
        extrusion = fidelity["max"] * diagonal * 1.5 + diagonal * 1e-3
        baked = _bake_maps(high, low, maps, settings, extrusion)
    baked_at = time.monotonic()

    bpy.data.objects.remove(source, do_unlink=True)
    _select_only([low])
    bpy.ops.export_scene.gltf(
        filepath=str(dest),
        export_format="GLB",
        use_selection=True,
        export_yup=True,
        export_apply=True,
        export_texcoords=True,
        export_normals=True,
        export_materials="EXPORT",
        export_image_format="AUTO",
        export_vertex_color="ACTIVE" if colour else "MATERIAL",
    )
    return {
        "method": request["method"],
        "triangles_before": before["triangles"],
        "faces_after": faces,
        "quads_after": quads,
        "triangles_after": _triangles(low),
        "fidelity": fidelity,
        "colours_transferred": colour is not None,
        "maps": baked,
        "texture_size": size if baked else None,
        "seconds": {
            "remesh": round(remeshed_at - started, 3),
            "measure": round(measured_at - remeshed_at, 3),
            "bake": round(baked_at - measured_at, 3),
            "total": round(time.monotonic() - started, 3),
        },
    }


def main(argv: list[str]) -> int:
    args = argv[argv.index("--") + 1 :] if "--" in argv else []
    if len(args) != 3 or args[0] not in COMMANDS:
        print(
            "usage: ... -- {inspect|export_fbx|import_fbx|bake|remesh} <request.json> <result.json>"
        )
        return 2
    command, request_path, result_path = args[0], Path(args[1]), Path(args[2])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    handler = {
        "inspect": inspect,
        "export_fbx": export_fbx,
        "import_fbx": import_fbx,
        "bake": bake,
        "remesh": remesh,
    }[command]
    try:
        result: dict[str, Any] = {"ok": True, **handler(request)}
    except RequestError as exc:
        result = {"ok": False, "error": str(exc)}
    except RuntimeError as exc:  # Blender operators raise RuntimeError on failure
        result = {"ok": False, "error": str(exc).strip().splitlines()[-1][:500]}
    result["blender"] = bpy.app.version_string
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
