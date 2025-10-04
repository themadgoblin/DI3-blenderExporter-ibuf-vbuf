import bpy
import bmesh
import struct
import os

print("hello blender")

bl_info = {
    "name": "Export IBUF/VBUF",
    "description": "Triangulate active mesh and export ibuf/vbuf with specified types",
    "author": "assistant",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "category": "Import-Export",
}

def f32_to_u32_bits(f):
    """Reinterpret a Python float (float32) as uint32 IEEE-754 bits."""
    # Clamp to float32 before packing to keep parity with Blender's float precision
    return struct.unpack("<I", struct.pack("<f", float(f)))[0]

class OBJECT_OT_export_ibuf_vbuf(bpy.types.Operator):
    """Export selected object's mesh to .ibuf (uint16 triplets) and .vbuf (xyz as uint32 bits, uv as float16)."""
    bl_idname = "object.export_ibuf_vbuf"
    bl_label = "Export IBUF/VBUF"
    bl_options = {'REGISTER', 'UNDO'}

    output_dir: bpy.props.StringProperty(
        name="Output directory",
        description="Folder where object.ibuf and object.vbuf will be saved",
        default="",
        subtype='DIR_PATH'
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, "output_dir")

    def invoke(self, context, event):
        # Offer a tiny dialog to pick an output directory (bonus)
        return context.window_manager.invoke_props_dialog(self, width=500)

    def execute(self, context):
        obj = context.object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Please select a Mesh object.")
            return {'CANCELLED'}

        me = obj.data

        # Ensure we're in OBJECT mode for safe mesh edits
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        # Check triangle status & triangulate if needed
        needs_triangulate = any(p.loop_total != 3 for p in me.polygons)
        if needs_triangulate:
            self.report({'WARNING'}, "Selected object has non-tri faces. Triangulating the whole mesh…")
            bm = bmesh.new()
            bm.from_mesh(me)
            bmesh.ops.triangulate(
                bm,
                faces=bm.faces[:],
                quad_method='BEAUTY',
                ngon_method='BEAUTY'
            )
            bm.to_mesh(me)
            bm.free()
            me.update()
        else:
            self.report({'INFO'}, "Mesh is already triangles.")

        # Recompute pointers
        me = obj.data
        me.calc_loop_triangles()

        # Access active UV layer (if any)
        uv_layer = me.uv_layers.active
        has_uv = uv_layer is not None
        uv_data = uv_layer.data if has_uv else None

        # Build vbuf/ibuf in memory:
        #
        # vbuf: per (vertex,uv) unique key because UV seams require duplication.
        # layout per vertex: x:uint32, y:uint32, z:uint32, u:float16, v:float16
        # ibuf: per triangle, 3x uint16 indices into vbuf
        vbuf_bytes = bytearray()
        ibuf_bytes = bytearray()
        vmap = {}  # key: (vert_index, u, v) -> vbuf_index
        next_vid = 0

        # Pre-fetch vertex coords (object/local space)
        verts = me.vertices
        loops = me.loops

        # Helper to add/get a vbuf entry
        def get_or_add_v(vi, uv_tuple):
            nonlocal next_vid, vbuf_bytes
            key = (vi, uv_tuple[0], uv_tuple[1])
            if key in vmap:
                return vmap[key]
            co = verts[vi].co  # Vector(x,y,z)
            # Convert xyz to uint32 bit-patterns
            x_u32 = f32_to_u32_bits(co.x)
            y_u32 = f32_to_u32_bits(co.y)
            z_u32 = f32_to_u32_bits(co.z)
            # Pack xyz (as uint32) and uv (float16)
            vbuf_bytes += struct.pack("<III", x_u32, y_u32, z_u32)
            # float16 support ('e') requires Python 3.6+. Blender 4.x uses 3.10/3.11, so it's available.
            vbuf_bytes += struct.pack("<e", float(uv_tuple[0]))
            vbuf_bytes += struct.pack("<e", float(uv_tuple[1]))
            vmap[key] = next_vid
            next_vid += 1
            return vmap[key]

        # Iterate loop triangles to ensure we truly have triangles
        # Each loop tri provides three loop indices -> vertex + uv per corner
        for lt in me.loop_triangles:
            tri_indices = []
            for loop_idx in lt.loops:
                vi = loops[loop_idx].vertex_index
                if has_uv:
                    uv = uv_data[loop_idx].uv
                    u, v = float(uv.x), float(uv.y)
                else:
                    u, v = 0.0, 0.0
                vid = get_or_add_v(vi, (u, v))
                tri_indices.append(vid)

            # Validate 16-bit index requirement
            for vid in tri_indices:
                if vid > 0xFFFF:
                    self.report({'ERROR'}, f"vbuf requires > 65536 entries ({vid+1}). "
                                           f"Cannot store indices as 2 bytes. Aborting.")
                    return {'CANCELLED'}

            # Append indices as uint16
            ibuf_bytes += struct.pack("<HHH", tri_indices[0], tri_indices[1], tri_indices[2])

        # Inform we are about to save
        self.report({'INFO'}, f"Preparing to save object: {obj.name}")

        # Resolve output directory
        if self.output_dir:
            out_dir = bpy.path.abspath(self.output_dir)
        else:
            # Default to current blend’s folder
            out_dir = bpy.path.abspath("//")

        os.makedirs(out_dir, exist_ok=True)

        ibuf_path = os.path.join(out_dir, "object.ibuf")
        vbuf_path = os.path.join(out_dir, "object.vbuf")

        # Write binary files
        try:
            with open(ibuf_path, "wb") as f:
                f.write(ibuf_bytes)
            with open(vbuf_path, "wb") as f:
                f.write(vbuf_bytes)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to save: {e}")
            return {'CANCELLED'}

        # Done
        tris = len(ibuf_bytes) // (2 * 3)
        self.report({'INFO'}, f"Saved object to:\n{ibuf_path}\n{vbuf_path}\n"
                              f"Vertices in vbuf: {len(vbuf_bytes) // (4*3 + 2*2)} | Triangles: {tris}")
        return {'FINISHED'}


# Registration helpers
classes = (OBJECT_OT_export_ibuf_vbuf,)

def menu_func(self, context):
    self.layout.operator(OBJECT_OT_export_ibuf_vbuf.bl_idname, icon='EXPORT')

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.VIEW3D_MT_object.append(menu_func)

def unregister():
    bpy.types.VIEW3D_MT_object.remove(menu_func)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
