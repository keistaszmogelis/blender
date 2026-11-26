"""Import SketchUp files through Trimble's standalone Windows SDK."""

bl_info = {
    "name": "SketchUp (.skp)",
    "version": (0, 1, 2),
    "blender": (4, 2, 0),
    "location": "File > Import",
    "category": "Import-Export",
}

import ctypes as C
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector


class Ref(C.Structure):
    _fields_ = [("ptr", C.c_void_p)]


class Point(C.Structure):
    _fields_ = [(axis, C.c_double) for axis in "xyz"]

    def tuple(self):
        return self.x, self.y, self.z


class Color(C.Structure):
    _fields_ = [(channel, C.c_ubyte) for channel in ("red", "green", "blue", "alpha")]


class Transform(C.Structure):
    _fields_ = [("values", C.c_double * 16)]


class SDK:
    def __init__(self, path):
        if sys.platform != "win32":
            raise RuntimeError("This add-on currently requires Windows x64.")
        path = Path(path)
        if not path.is_file():
            raise RuntimeError("Select SketchUpAPI.dll from the SketchUp Desktop SDK in Add-on Preferences.")
        with os.add_dll_directory(str(path.parent)):
            self.dll = C.CDLL(str(path))
        self.dll.SUInitialize.argtypes = []
        self.dll.SUInitialize.restype = None
        self.dll.SUTerminate.argtypes = []
        self.dll.SUTerminate.restype = None

    def call(self, name, *args, optional=False):
        function = getattr(self.dll, name)
        function.argtypes = [type(arg) for arg in args]
        function.restype = C.c_int
        result = self.invoke(function, *args)
        if result == 9 and optional:
            return False
        if result:
            raise RuntimeError(f"{name} returned SketchUp error {result}")
        return True

    @staticmethod
    def invoke(function, *args):
        try:
            return function(*args)
        except OSError as error:
            raise RuntimeError(f"SketchUp SDK call {function.__name__} failed: {error}") from error

    def value(self, name, ref, kind=Ref, optional=False):
        output = kind()
        if self.call(name, ref, C.pointer(output), optional=optional):
            return output
        return None

    def array(self, name, ref, count, kind=Ref):
        if not count:
            return []
        output = (kind * count)()
        written = C.c_size_t()
        self.call(name, ref, C.c_size_t(count), C.cast(output, C.POINTER(kind)), C.pointer(written))
        return list(output[:written.value])

    def items(self, prefix, suffix, ref):
        count = self.value(f"{prefix}GetNum{suffix}", ref, C.c_size_t)
        return self.array(f"{prefix}Get{suffix}", ref, count.value)

    def string(self, name, ref):
        string = Ref()
        self.call("SUStringCreate", C.pointer(string))
        try:
            self.call(name, ref, C.pointer(string))
            length = self.value("SUStringGetUTF8Length", string, C.c_size_t).value
            buffer = C.create_string_buffer(length + 1)
            written = C.c_size_t()
            self.call("SUStringGetUTF8", string, C.c_size_t(length + 1), C.cast(buffer, C.POINTER(C.c_char)), C.pointer(written))
            return buffer.value.decode("utf-8", errors="replace")
        finally:
            self.call("SUStringRelease", C.pointer(string))


INCH = 0.0254
IDENTITY = Matrix.Identity(4)


def position(point):
    return Vector(point.tuple()) * INCH


def component_matrix(transform):
    values = transform.values
    matrix = Matrix(tuple(tuple(values[column * 4 + row] for column in range(4)) for row in range(4)))
    # SketchUp's homogeneous scale can be stored in the final element.
    scale = matrix[3][3] or 1.0
    matrix = matrix * (1.0 / scale)
    matrix.translation *= INCH
    return matrix


def uv(coordinate):
    q = coordinate.z
    return (coordinate.x / q, coordinate.y / q) if abs(q) > 1e-12 else (0.0, 0.0)


def linear(channel):
    channel /= 255.0
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


class Geometry:
    def __init__(self):
        self.vertices = []
        self.edges = set()
        self.faces = []
        self.corners = []
        self.materials = []
        self.sharp = set()


class Importer:
    def __init__(self, sdk, collection, directory, mode, threshold):
        self.sdk = sdk
        self.collection = collection
        self.directory = Path(directory)
        self.mode = mode
        self.threshold = threshold
        self.materials = {}
        self.texture_scales = {}
        self.objects = []
        self.meshes = []
        self.images = []
        self.new_materials = []

    def material(self, ref):
        if ref is None:
            return None
        sdk = self.sdk
        if ref.ptr in self.materials:
            return self.materials[ref.ptr]
        material = bpy.data.materials.new(sdk.string("SUMaterialGetName", ref) or "SketchUp Material")
        self.new_materials.append(material)
        material.use_nodes = True
        nodes = material.node_tree.nodes
        shader = nodes.get("Principled BSDF")
        color = sdk.value("SUMaterialGetColor", ref, Color)
        opacity = sdk.value("SUMaterialGetOpacity", ref, C.c_double).value
        rgba = (linear(color.red), linear(color.green), linear(color.blue), opacity)
        material.diffuse_color = rgba
        shader.inputs["Base Color"].default_value = rgba
        shader.inputs["Alpha"].default_value = opacity
        texture = sdk.value("SUMaterialGetTexture", ref, optional=True)
        if texture:
            width, height = C.c_size_t(), C.c_size_t()
            u_scale, v_scale = C.c_double(), C.c_double()
            sdk.call("SUTextureGetDimensions", texture, C.pointer(width), C.pointer(height),
                     C.pointer(u_scale), C.pointer(v_scale))
            self.texture_scales[ref.ptr] = (u_scale.value, v_scale.value)
            filename = sdk.string("SUTextureGetFileName", texture)
            extension = Path(filename).suffix.lower()
            if extension not in (".jpg", ".jpeg", ".png", ".bmp", ".tga", ".tif", ".tiff", ".gif", ".webp"):
                extension = ".png"
            path = self.directory / f"texture-{len(self.materials)}{extension}"
            sdk.call("SUTextureWriteToFile", texture, C.c_char_p(str(path).encode("utf-8")))
            image = bpy.data.images.load(str(path), check_existing=False)
            self.images.append(image)
            image.name = Path(filename).name or path.name
            image.pack()
            node = nodes.new("ShaderNodeTexImage")
            node.image = image
            node.location = (-320, 300)
            material.node_tree.links.new(node.outputs["Color"], shader.inputs["Base Color"])
        self.materials[ref.ptr] = material
        return material

    def face_data(self, face, inherited):
        sdk = self.sdk
        front = sdk.value("SUFaceGetFrontMaterial", face, optional=True)
        back = sdk.value("SUFaceGetBackMaterial", face, optional=True)
        chosen = front or back or inherited
        material = self.material(chosen)
        texture_scale = self.texture_scales.get(chosen.ptr) if chosen is not None else None
        reverse = front is None and back is not None
        helper = Ref()
        sdk.call("SUMeshHelperCreate", C.pointer(helper), face)
        try:
            size = sdk.value("SUMeshHelperGetNumVertices", helper, C.c_size_t).value
            points = sdk.array("SUMeshHelperGetVertices", helper, size, Point)
            normals = sdk.array("SUMeshHelperGetNormals", helper, size, Point)
            coords = []
            if texture_scale:
                side = "Back" if reverse else "Front"
                coords = sdk.array(f"SUMeshHelperGet{side}STQCoords", helper, size, Point)
            lookup = {p.tuple(): i for i, p in enumerate(points)}
            hole_count = sdk.value("SUFaceGetNumInnerLoops", face, C.c_size_t).value
            if hole_count:
                count = sdk.value("SUMeshHelperGetNumTriangles", helper, C.c_size_t).value
                indices = sdk.array("SUMeshHelperGetVertexIndices", helper, count * 3, C.c_size_t)
                polygons = [[points[index] for index in indices[i:i + 3]] for i in range(0, len(indices), 3)]
            else:
                outer = sdk.value("SUFaceGetOuterLoop", face)
                polygons = [[sdk.value("SUVertexGetPosition", vertex, Point)
                             for vertex in sdk.items("SULoop", "Vertices", outer)]]
            normal = sdk.value("SUFaceGetNormal", face, Point).tuple()
            corners = []
            for polygon in polygons:
                if hole_count:
                    a, b, c = (Vector(point.tuple()) for point in polygon)
                    if (b - a).cross(c - a).dot(Vector(normal)) < 0:
                        polygon.reverse()
                data = []
                for point in polygon:
                    index = lookup.get(point.tuple())
                    point_normal = normals[index].tuple() if index is not None and index < len(normals) else normal
                    texcoord = uv(coords[index]) if index is not None and index < len(coords) else (0.0, 0.0)
                    if texture_scale:
                        texcoord = tuple(value * scale for value, scale in zip(texcoord, texture_scale))
                    data.append((point, point_normal, texcoord))
                if reverse:
                    data = [(point, tuple(-v for v in normal), texcoord)
                            for point, normal, texcoord in reversed(data)]
                corners.append(data)
            return material, corners
        finally:
            sdk.call("SUMeshHelperRelease", C.pointer(helper))

    def append_entities(self, geometry, entities, transform, inherited):
        sdk = self.sdk
        vertices = {}
        normal_matrix = transform.to_3x3().inverted_safe().transposed()
        mirrored = transform.to_3x3().determinant() < 0

        def vertex_index(point):
            key = point.tuple()
            if key not in vertices:
                vertices[key] = len(geometry.vertices)
                geometry.vertices.append(transform @ position(point))
            return vertices[key]

        def edge_key(edge):
            start = sdk.value("SUEdgeGetStartVertex", edge)
            end = sdk.value("SUEdgeGetEndVertex", edge)
            endpoints = [vertex_index(sdk.value("SUVertexGetPosition", vertex, Point))
                         for vertex in (start, end)]
            return tuple(sorted(endpoints))

        for edge in sdk.items("SUEntities", "Edges", entities):
            key = edge_key(edge)
            if key[0] != key[1]:
                geometry.edges.add(key)

        for face in sdk.items("SUEntities", "Faces", entities):
            material, polygons = self.face_data(face, inherited)
            smooth = False
            for edge in sdk.items("SUFace", "Edges", face):
                edge_smooth = sdk.value("SUEdgeGetSmooth", edge, C.c_bool).value
                smooth |= edge_smooth
                if not edge_smooth:
                    geometry.sharp.add(edge_key(edge))
            for polygon in polygons:
                if mirrored:
                    polygon.reverse()
                indices = [vertex_index(point) for point, _, _ in polygon]
                if len(set(indices)) != len(indices) or len(indices) < 3:
                    continue
                geometry.faces.append(indices)
                geometry.materials.append((material, smooth))
                geometry.corners.extend((normal_matrix @ Vector(normal), texcoord)
                                        for _, normal, texcoord in polygon)

    def children(self, entities):
        sdk = self.sdk
        for group in sdk.items("SUEntities", "Groups", entities):
            yield (sdk.string("SUGroupGetName", group) or "Group",
                   sdk.value("SUGroupGetEntities", group),
                   component_matrix(sdk.value("SUGroupGetTransform", group, Transform)),
                   self.instance_material("SUGroupToDrawingElement", group))
        for instance in sdk.items("SUEntities", "Instances", entities):
            definition = sdk.value("SUComponentInstanceGetDefinition", instance)
            name = sdk.string("SUComponentInstanceGetName", instance)
            yield (name or sdk.string("SUComponentDefinitionGetName", definition) or "Component",
                   sdk.value("SUComponentDefinitionGetEntities", definition),
                   component_matrix(sdk.value("SUComponentInstanceGetTransform", instance, Transform)),
                   self.instance_material("SUComponentInstanceToDrawingElement", instance))

    def instance_material(self, conversion, instance):
        function = getattr(self.sdk.dll, conversion)
        function.argtypes = [Ref]
        function.restype = Ref
        return self.sdk.value("SUDrawingElementGetMaterial", self.sdk.invoke(function, instance), optional=True)

    def object_from_geometry(self, name, geometry, parent, transform):
        mesh = bpy.data.meshes.new(name)
        self.meshes.append(mesh)
        # from_pydata expects unique edges, including those it derives from polygons.
        polygon_edges = {tuple(sorted((face[index - 1], vertex)))
                         for face in geometry.faces for index, vertex in enumerate(face)}
        mesh.from_pydata(geometry.vertices, sorted(geometry.edges - polygon_edges),
                         geometry.faces, shade_flat=False)
        mesh.update()
        slots = {}
        if geometry.faces:
            layer = mesh.uv_layers.new(name="UVMap")
            layer.uv.foreach_set("vector", [v for _, pair in geometry.corners for v in pair])
        for polygon, (material, smooth) in zip(mesh.polygons, geometry.materials):
            polygon.use_smooth = smooth
            if material not in slots:
                slots[material] = len(mesh.materials)
                mesh.materials.append(material)
            polygon.material_index = slots[material]
        for edge in mesh.edges:
            edge.use_edge_sharp = tuple(sorted(edge.vertices)) in geometry.sharp
        if geometry.corners:
            mesh.normals_split_custom_set([normal.normalized() for normal, _ in geometry.corners])
        obj = bpy.data.objects.new(name, mesh)
        self.objects.append(obj)
        self.collection.objects.link(obj)
        obj.parent = parent
        obj.matrix_basis = transform
        return obj

    def collect(self, geometry, entities, transform, inherited, children):
        self.append_entities(geometry, entities, transform, inherited)
        for _, child_entities, child_matrix, material in children:
            self.collect(geometry, child_entities, transform @ child_matrix,
                         material or inherited, self.children(child_entities))

    def component(self, name, entities, transform, inherited, parent):
        children = list(self.children(entities))
        location, rotation, scale = transform.decompose()
        object_transform = Matrix.LocRotScale(location, rotation, scale)
        residual = object_transform.inverted_safe() @ transform
        geometry = Geometry()
        join = self.mode == "JOIN" or (self.mode == "AUTO" and len(children) >= self.threshold)
        if join:
            self.collect(geometry, entities, residual, inherited, children)
        else:
            self.append_entities(geometry, entities, residual, inherited)
        obj = self.object_from_geometry(name, geometry, parent, object_transform)
        if not join:
            for child_name, child_entities, matrix, material in children:
                self.component(child_name, child_entities, residual @ matrix, material or inherited, obj)

    def run(self, entities):
        geometry = Geometry()
        self.append_entities(geometry, entities, IDENTITY, None)
        if geometry.faces or geometry.edges:
            self.object_from_geometry("Model Geometry", geometry, None, IDENTITY)
        for name, child_entities, matrix, material in self.children(entities):
            self.component(name, child_entities, matrix, material, None)

    def rollback(self):
        for datablocks, items in ((bpy.data.objects, self.objects), (bpy.data.meshes, self.meshes),
                                  (bpy.data.materials, self.new_materials), (bpy.data.images, self.images)):
            for item in items:
                datablocks.remove(item, do_unlink=True)


class SKPPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    sdk_path: StringProperty(name="SketchUpAPI.dll", subtype="FILE_PATH")

    def draw(self, context):
        self.layout.prop(self, "sdk_path")


class ImportSKP(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.skp"
    bl_label = "Import SketchUp"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".skp"
    filter_glob: StringProperty(default="*.skp", options={"HIDDEN"})
    nesting: EnumProperty(
        name="Nested Components",
        items=[
            ("HIERARCHY", "Hierarchy", "Create separate child mesh objects"),
            ("JOIN", "Join", "Join descendants into each top-level component mesh"),
            ("AUTO", "Automatic", "Join a component's subtree when its direct child count reaches the threshold"),
        ],
        default="AUTO",
    )
    threshold: IntProperty(name="Join Threshold", default=10, min=1)

    def draw(self, context):
        self.layout.prop(self, "nesting")
        row = self.layout.row()
        row.enabled = self.nesting == "AUTO"
        row.prop(self, "threshold")

    def execute(self, context):
        try:
            preferences = context.preferences.addons[__package__].preferences
            sdk_path = bpy.path.abspath(preferences.sdk_path) if preferences.sdk_path else ""
            sdk = SDK(sdk_path)
            sdk.invoke(sdk.dll.SUInitialize)
            try:
                model, status = Ref(), C.c_int()
                sdk.call("SUModelCreateFromFileWithStatus", C.pointer(model),
                         C.c_char_p(self.filepath.encode("utf-8")), C.pointer(status))
                try:
                    if status.value:
                        self.report({"WARNING"}, "This file is newer than the installed SketchUp SDK. Some data may be missing.")
                    collection = bpy.data.collections.new(Path(self.filepath).stem)
                    context.collection.children.link(collection)
                    with TemporaryDirectory(prefix="blender-skp-") as directory:
                        importer = Importer(sdk, collection, directory, self.nesting, self.threshold)
                        try:
                            importer.run(sdk.value("SUModelGetEntities", model))
                        except Exception:
                            importer.rollback()
                            bpy.data.collections.remove(collection)
                            raise
                finally:
                    sdk.call("SUModelRelease", C.pointer(model))
            finally:
                sdk.invoke(sdk.dll.SUTerminate)
        except (RuntimeError, OSError, AttributeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


def menu_import(self, context):
    self.layout.operator(ImportSKP.bl_idname, text="SketchUp (.skp)")


def register():
    bpy.utils.register_class(SKPPreferences)
    bpy.utils.register_class(ImportSKP)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    bpy.utils.unregister_class(ImportSKP)
    bpy.utils.unregister_class(SKPPreferences)
