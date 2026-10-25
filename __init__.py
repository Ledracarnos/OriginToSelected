# SPDX-License-Identifier: GPL-3.0-or-later

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector


AXES = "XYZ"


def selected_points(context, space=None):
    """Combine live selections from all meshes in Edit Mode in world space.

    Shared meshes are read at each edited object's placement, not just once per
    datablock. Unselected and hidden geometry never contributes. An optional
    common space transform lets alignment ignore the active object's scale.
    """
    context.view_layer.update()
    points = []
    normal = Vector((0.0, 0.0, 0.0))
    weight = 0.0
    has_faces = False
    objects = sorted((obj for obj in context.objects_in_mode if obj.type == 'MESH'),
                     key=lambda obj: (obj != context.object, obj.name))
    for obj in objects:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.normal_update()
        bm.verts.index_update()
        vertices = {v for v in bm.verts if v.select and not v.hide}
        faces = [f for f in bm.faces if f.select and not f.hide]
        for edge in bm.edges:
            if edge.select and not edge.hide:
                vertices.update(edge.verts)
        for face in faces:
            vertices.update(face.verts)
        if not vertices:
            continue
        world = obj.matrix_world.copy()
        if space is not None:
            world = space @ world
        points.extend(world @ v.co for v in sorted(vertices, key=lambda v: v.index))
        has_faces |= bool(faces)
        if faces:
            linear = world.to_3x3()
            try:
                normal_matrix = linear.inverted().transposed()
            except ValueError:
                raise ValueError("Non-invertible transform: check for zero scale.") from None
            # The determinant also accounts for area differences between objects.
            area_scale = abs(linear.determinant())
            for face in faces:
                contribution = (normal_matrix @ face.normal) * face.calc_area() * area_scale
                normal += contribution
                weight += contribution.length
    if weight and normal.length > weight * 1e-6:
        normal.normalize()
    else:
        normal = None
    face_mode = context.scene.tool_settings.mesh_select_mode[2]
    return points, has_faces and face_mode, normal


def selection_direction(points, has_faces=False, face_normal=None, reference=None, invert=False,
                        direction_origin=None):
    if not points:
        raise ValueError("Select at least one vertex.")
    array = np.array([tuple(p) for p in points], dtype=np.float64)
    # Subtract a reference before taking the mean to limit cancellation for CAD
    # coordinates far from the world origin.
    relative = array - array[0]
    mean = relative.mean(axis=0)
    center = Vector(array[0] + mean)
    centered = relative - mean
    size = float(np.linalg.norm(centered, axis=1).max())
    if len(points) == 1 or size <= 1e-12:
        # Coincident vertices describe the same point, regardless of their count.
        direction = None
        method = "Single point"
    elif len(points) == 2:
        # No PCA, approximation or eigenvector sign ambiguity for two points.
        direction = (points[1] - points[0]).normalized()
        method = "Two points: exact direction"
    else:
        scaled = centered / size
        values, vectors = np.linalg.eigh(scaled.T @ scaled / len(points))
        planar = values[0] < values[2] * 1e-6 and values[1] > values[2] * 1e-8
        if has_faces and planar:
            direction = face_normal.copy() if face_normal is not None else Vector(vectors[:, 0])
            method = "Planar selection normal"
        else:
            if values[2] - values[1] < values[2] * 1e-4:
                # A symmetric point distribution has no unique principal axis.
                direction = None
                method = "Symmetric selection"
            else:
                direction = Vector(vectors[:, 2])
                method = "Principal direction (PCA)"
        if direction is not None:
            direction.normalize()
    if direction is None:
        if direction_origin is None:
            direction = Vector((0.0, 0.0, 1.0))
            method += ": world Z"
        else:
            direction = center - direction_origin
            # Mesh coordinates and mathutils matrices use single precision.
            # Recentering can leave rounding noise: never aim at that noise.
            magnitude = max(1.0, *(abs(value) for value in center),
                            *(abs(value) for value in direction_origin))
            tolerance = 4 * np.finfo(np.float32).eps * magnitude
            if direction.length <= tolerance:
                return center, None, method + " at origin: no alignment"
            direction.normalize()
            method += ": direction from origin"
        # This direction has a meaningful sense: do not apply the PCA sign rule.
        if invert:
            direction.negate()
        return center, direction, method
    # Keep the closest sense to the current requested axis, with a deterministic
    # world-space tie-break. Without a reference, signed buttons have a stable
    # polarity: clicking -X twice must not toggle the axis back to +X.
    dot = direction.dot(reference) if reference is not None else 0.0
    if dot < -1e-7:
        direction.negate()
    elif abs(dot) <= 1e-7:
        largest = max(range(3), key=lambda i: abs(direction[i]))
        if direction[largest] < 0.0:
            direction.negate()
    if invert:
        direction.negate()
    return center, direction, method


def alignment_basis(direction, index, world):
    """Right-handed basis, preserving the existing twist as far as possible."""
    next_index = (index + 1) % 3
    other = world.to_3x3().col[next_index].normalized()
    other = other - direction * other.dot(direction)
    if other.length < 1e-6:
        seed = min((Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))),
                   key=lambda v: abs(v.dot(direction)))
        other = seed - direction * seed.dot(direction)
    other.normalize()
    axes = [None, None, None]
    axes[index] = direction
    axes[next_index] = other
    # Cyclic orders XYZ, YZX and ZXY are ALL right-handed (no special Y case).
    axes[(index + 2) % 3] = direction.cross(other).normalized()
    return Matrix(axes).transposed()


def effective_scale(obj):
    return Vector(tuple(obj.scale[i] * obj.delta_scale[i] for i in range(3)))


def scale_free_world(obj):
    """Remove scale magnitudes (including delta), retaining mirror signs.

    Keep the parent attachment intact, including inherited scale or shear.
    No Blender properties are changed while computing this reference frame.
    A pure mirror is an isometry: it must not be treated as unequal stretching.
    """
    scale = effective_scale(obj)
    return obj.matrix_world @ Matrix.Diagonal((*[1.0 / abs(value) for value in scale], 1.0))


def pivot_users(context, obj):
    return [obj] + [other for other in context.selected_objects
                    if other != obj and other.type == 'MESH' and other.data == obj.data]


def scale_affects_alignment(obj):
    scale = [abs(value) for value in effective_scale(obj)]
    return max(scale) - min(scale) > max(scale) * 1e-5


def selection_alignment(context, obj, index, negative, move_origin):
    """Compute a rigid local pivot change in the active object's uniform-scale frame."""
    context.view_layer.update()
    unit_world = scale_free_world(obj)
    points, has_faces, normal = selected_points(
        context, unit_world @ obj.matrix_world.inverted())
    center, direction, _method = selection_direction(
        points, has_faces, normal, invert=negative,
        direction_origin=None if move_origin else unit_world.translation.copy())
    if direction is None:
        return None, None
    local_direction = (unit_world.to_3x3().inverted() @ direction).normalized()
    rotation = alignment_basis(local_direction, index, Matrix.Identity(3))
    origin = unit_world.inverted() @ center if move_origin else Vector((0, 0, 0))
    return origin, rotation


def report_scale_effect(operator, users):
    if any(scale_affects_alignment(user) for user in users):
        operator.report({'WARNING'},
                        "Object has non-uniform scale, Selection to Pivot may be unpredictable")


def validate_object(context):
    obj = context.object
    if not obj or obj.type != 'MESH' or obj.mode != 'EDIT':
        raise ValueError("Enter Edit Mode on a mesh object.")
    validate_pivot_object(obj)
    return obj


def validate_parenting(obj):
    if not obj.parent:
        return
    if obj.parent_type not in {'OBJECT', 'BONE', 'VERTEX', 'VERTEX_3'}:
        raise ValueError("Unsupported parenting type.")
    if obj.parent_type == 'BONE':
        if obj.parent.type != 'ARMATURE' or obj.parent_bone not in obj.parent.pose.bones:
            raise ValueError("The parent bone is missing or invalid.")


def effective_parent_matrix(obj):
    """Use Blender's computed bone or vertex attachment.

    With world = attachment @ parent_inverse @ basis, recovering the attachment
    avoids approximating bone offsets or evaluated vertex-parent frames.
    The view layer must be up to date; child constraints are not compensated.
    """
    if obj.parent_type in {'BONE', 'VERTEX', 'VERTEX_3'}:
        return (obj.matrix_world @ obj.matrix_basis.inverted()
                @ obj.matrix_parent_inverse.inverted())
    return obj.parent.matrix_world.copy()


def validate_pivot_object(obj):
    """Check technical prerequisites, not the user's animation/modifier choices."""
    if not obj.is_editable or not obj.data.is_editable:
        raise ValueError("The object and its mesh must be editable.")
    validate_parenting(obj)
    for child in obj.children:
        validate_parenting(child)
    try:
        obj.matrix_world.inverted()
        obj.matrix_basis.inverted()
    except ValueError:
        raise ValueError("Non-invertible transform: check for zero scale.") from None
    return obj


class PivotApplicationError(RuntimeError):
    """A real API error after writes began; keep the partial result undoable."""


def transform_mesh(mesh, correction, normals):
    mesh.transform(correction, shape_keys=True)
    if normals is not None:
        normal_transform = np.array(correction.to_3x3().inverted(), dtype=np.float64)
        adjusted = normals.astype(np.float64) @ normal_transform
        lengths = np.linalg.norm(adjusted, axis=1)
        adjusted /= np.maximum(lengths[:, None], 1e-30)
        mesh.normals_split_custom_set(adjusted.tolist())
    mesh.update()


def apply_pivot(context, obj, origin, rotation):
    """Apply a rigid local pivot change, then restore each user's own scale.

    Other meshes keep their own pivots. Linked copies stay shared: selected
    copies receive the same rigid mesh correction and retain their scale values.
    Their visible stretched shape can change; their scale-1 shape cannot.
    Unselected copies keep their transforms and receive the shared mesh change.

    Parented objects can store the exact affine transform in their parent inverse:
    world = parent_attachment @ parent_inverse @ basis
    parent_inverse = inverse(parent_attachment) @ desired_world @ inverse(basis)
    """
    mesh = obj.data
    saved_selection = list(context.selected_objects)
    users = pivot_users(context, obj)
    for user in users:
        validate_pivot_object(user)
    user_set = set(users)
    # Preserve ordinary children, but do not compensate an unselected instance
    # of the shared mesh through its parent relationship.
    children = {child for user in users for child in user.children
                if child not in user_set and not (child.type == 'MESH' and child.data == mesh)}
    for child in children:
        if not child.is_editable:
            raise ValueError("A child object is not editable.")
    states = {item: (item.matrix_world.copy(), item.matrix_basis.copy(),
                     item.matrix_parent_inverse.copy()) for item in [*users, *children]}
    scales = {user: user.scale.copy() for user in users}
    unit_targets = {}
    targets = {}
    for user in users:
        unit_target = scale_free_world(user) @ rotation.to_4x4()
        # Place the origin at the selection's current (scaled) position.
        unit_target.translation = states[user][0] @ origin
        scale = effective_scale(user)
        reflection = Matrix.Diagonal((*[(-1.0 if value < 0 else 1.0) for value in scale], 1.0))
        # unit_target includes the mirror. Remove it only from the matrix used
        # to assign a root's proper rotation: the signed scale channels restore
        # it afterwards. This keeps uniform mirrored geometry exactly in place.
        unit_targets[user] = unit_target @ reflection
        targets[user] = unit_target @ Matrix.Diagonal((*[abs(value) for value in scale], 1.0))
    targets.update((child, states[child][0]) for child in children)
    # Rotation + translation only: never bake scale or shear into the base mesh.
    correction = rotation.transposed().to_4x4() @ Matrix.Translation(-origin)
    normals = None
    writes_started = False
    try:
        # Flush BMesh once. Do not retain BMesh references across this mode switch.
        bpy.ops.object.mode_set(mode='OBJECT')
        if mesh.has_custom_normals:
            normals = np.empty(len(mesh.loops) * 3, dtype=np.float32)
            mesh.corner_normals.foreach_get("vector", normals)
            normals = normals.reshape((-1, 3))
        def parent_depth(item):
            depth = 0
            while item.parent:
                depth += 1
                item = item.parent
            return depth

        # Vertex attachments depend on the corrected mesh coordinates. Evaluate
        # them before restoring child transforms, not after compensation.
        writes_started = True
        transform_mesh(mesh, correction, normals)
        context.view_layer.update()

        # Selected copies can be each other's ancestors: apply all compensation
        # parent-first, using each parent's updated world matrix and attachment.
        for item in sorted(targets, key=parent_depth):
            target = targets[item]
            if item.parent:
                item.matrix_parent_inverse = (effective_parent_matrix(item).inverted()
                                             @ target @ item.matrix_basis.inverted())
            else:
                # Assign the proper rotation first, avoiding Blender's ambiguous
                # decomposition of negative scales, then restore exact channels.
                item.matrix_world = unit_targets[item]
                item.scale = scales[item]
            context.view_layer.update()

    except Exception as exc:
        # Never undo the user's action automatically. A genuine API failure
        # after writes is reported as an incomplete, manually undoable action.
        if writes_started:
            raise PivotApplicationError(str(exc)) from exc
        raise
    finally:
        for other in context.selected_objects:
            other.select_set(False)
        for other in saved_selection:
            other.select_set(True)
        context.view_layer.objects.active = obj
        if obj.mode != 'EDIT':
            bpy.ops.object.mode_set(mode='EDIT')
    return users


def tag_full_pivot_undo():
    """Include Object IDs in the outer operator's single undo step.

    Blender 5.2's Edit Mesh undo stores mesh data, not object transforms,
    parent inverses or custom properties. Merely using UNDO (or UNDO_GROUPED)
    therefore restores compensated vertices under the wrong object matrix.

    Creating/removing an ID invalidates Blender's memfile undo cache. Blender
    then includes a hidden global snapshot with the outer Edit Mesh undo step,
    restoring the entire pivot, children and mesh links together. The empty
    marker never enters the scene and no datablock is left behind. Do NOT add
    manual undo pushes or merge successive clicks into one undo group.
    """
    marker = bpy.data.meshes.new(".Selection to Pivot Undo")
    bpy.data.meshes.remove(marker)


def report_incomplete_pivot(operator, context, error):
    tag_full_pivot_undo()
    message = "Operation incomplete: " + str(error)
    if context.preferences.edit.use_global_undo:
        message += ". Use Ctrl+Z to undo."
    operator.report({'WARNING'}, message)
    # FINISHED is essential: Blender must record the partial writes for undo.
    return {'FINISHED'}


class OBJECT_OT_selection_to_pivot(bpy.types.Operator):
    bl_idname = "object.selection_to_pivot"
    bl_label = "Align Axis to Selection"
    bl_description = "Align an axis to the selection, optionally moving the origin"
    bl_options = {'REGISTER', 'UNDO'}

    axis: bpy.props.EnumProperty(name="Axis", items=[(x, x, "") for x in AXES], default='X')
    negative: bpy.props.BoolProperty(name="Negative", default=False)
    origin: bpy.props.BoolProperty(name="Origin", default=True)

    def execute(self, context):
        try:
            obj = validate_object(context)
            index = AXES.index(self.axis)
            origin, rotation = selection_alignment(
                context, obj, index, self.negative, self.origin)
            if rotation is None:
                # No direction to aim at: leave the pivot and undo history alone.
                return {'CANCELLED'}
            users = apply_pivot(context, obj, origin, rotation)
            tag_full_pivot_undo()
            report_scale_effect(self, users)
        except PivotApplicationError as exc:
            return report_incomplete_pivot(self, context, exc)
        except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class OBJECT_OT_selection_to_pivot_scale_warning(bpy.types.Operator):
    bl_idname = "object.selection_to_pivot_scale_warning"
    bl_label = "Non-uniform scale"
    bl_description = "Object has non-uniform scale, Selection to Pivot may be unpredictable"

    def execute(self, context):
        return {'CANCELLED'}


class VIEW3D_PT_selection_to_pivot(bpy.types.Panel):
    bl_label = "Selection to Pivot"
    bl_idname = "VIEW3D_PT_selection_to_pivot"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Selection to Pivot"

    @classmethod
    def poll(cls, context):
        obj = context.object
        return bool(obj and obj.type == 'MESH' and obj.mode == 'EDIT')

    def draw(self, context):
        layout = self.layout
        obj = context.object
        if any(scale_affects_alignment(user) for user in pivot_users(context, obj)):
            row = layout.row()
            row.enabled = False
            row.operator(OBJECT_OT_selection_to_pivot_scale_warning.bl_idname,
                         text="Non-uniform scale", icon='ERROR')
        for move_origin in (True, False):
            if not move_origin:
                layout.separator()
            column = layout.column(align=True)
            column.label(text="Align Origin Axis to Selection" if move_origin
                         else "Align Axis to Selection")
            for axis in AXES:
                row = column.row(align=True)
                for negative in (False, True):
                    button = row.operator(OBJECT_OT_selection_to_pivot.bl_idname,
                                          text=("-" if negative else "") + axis,
                                          translate=False)
                    button.axis = axis
                    button.negative = negative
                    button.origin = move_origin


CLASSES = (OBJECT_OT_selection_to_pivot,
           OBJECT_OT_selection_to_pivot_scale_warning, VIEW3D_PT_selection_to_pivot)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
