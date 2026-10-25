# SPDX-License-Identifier: GPL-3.0-or-later
"""Bone-parent integration tests; run with --background --factory-startup."""

import importlib.util
from pathlib import Path

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("selection_to_pivot", ROOT / "__init__.py")
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
addon.register()


def close(actual, expected):
    assert (actual - expected).length < 5e-4, (tuple(actual), tuple(expected))


def positions(obj):
    vertices = bmesh.from_edit_mesh(obj.data).verts if obj.data.is_editmode else obj.data.vertices
    return [obj.matrix_world @ v.co for v in vertices]


def unit_positions(obj):
    # Observe Blender with scale cleared rather than duplicating the addon's math.
    scale = obj.scale.copy()
    obj.scale = (1, 1, 1)
    bpy.context.view_layer.update()
    result = positions(obj)
    obj.scale = scale
    bpy.context.view_layer.update()
    return result


def same_shape(actual, expected):
    for a, e in zip(actual, expected):
        close(a - actual[0], e - expected[0])


def set_pose(rig, amount):
    for index, bone in enumerate(rig.pose.bones):
        bone.rotation_mode = 'XYZ'
        bone.rotation_euler = (amount * (index + 1), -amount * 0.3, amount * 0.5)
        bone.location = (amount * 0.2, 0, 0)
    bpy.context.view_layer.update()


def create_scene(relative, nonuniform):
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    root = bpy.data.objects.new("Rig Root Test", None)
    bpy.context.collection.objects.link(root)
    root.matrix_world = Matrix.LocRotScale(
        Vector((2, -1, 3)), Euler((0.3, 0.2, -0.4)).to_quaternion(),
        Vector((1.5, 0.8, 2)) if nonuniform else Vector((1, 1, 1)))
    rig = bpy.data.objects.new("Bone Parent Test", bpy.data.armatures.new("Bone Parent Test"))
    bpy.context.collection.objects.link(rig)
    rig.parent = root
    rig.location = (1, 2, -1)
    rig.rotation_euler = (0.1, -0.2, 0.3)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    first = rig.data.edit_bones.new("Attachment")
    first.head = (0.2, 0.3, 0.4)
    first.tail = (0.4, 2, 1.3)
    second = rig.data.edit_bones.new("Other Attachment")
    second.head = first.tail
    second.tail = (2, 3, 2)
    second.parent = first
    bpy.ops.object.mode_set(mode='OBJECT')
    for bone in rig.data.bones:
        bone.use_relative_parent = relative
    set_pose(rig, 0.4)

    mesh = bpy.data.meshes.new("Bone Child Test")
    mesh.from_pydata([(1, 2, 0.5), (2, 4, 1.5), (-1, 0.5, 3)], [], [])
    users = []
    references = []
    for index, bone_name in enumerate(("Attachment", "Other Attachment")):
        obj = bpy.data.objects.new("Bone Child Test", mesh)
        bpy.context.collection.objects.link(obj)
        obj.parent = rig
        obj.parent_type = 'BONE'
        obj.parent_bone = bone_name
        obj.location = (1 + index, -0.5, 0.3)
        obj.rotation_euler = (0.2, -0.4, 0.3)
        obj.scale = (1.2, 0.7, 1.4)
        obj.delta_location = (0.1, 0.2, -0.1)
        obj.delta_rotation_euler = (0.1, 0.05, -0.1)
        obj.matrix_parent_inverse = Matrix.Translation((0.2, -0.1, 0.4))
        # Independent, unselected reference retains the original mesh and pivot.
        reference = obj.copy()
        reference.data = mesh.copy()
        bpy.context.collection.objects.link(reference)
        references.append(reference)
        users.append(obj)

    child = bpy.data.objects.new("Ordinary Child Test", None)
    bpy.context.collection.objects.link(child)
    child.parent = users[0]
    child.location = (1, 2, 3)
    bpy.ops.object.select_all(action='DESELECT')
    for obj in users:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = users[0]
    bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(mesh)
    bm.verts.ensure_lookup_table()
    for vertex in bm.verts:
        vertex.select_set(vertex.index < 2)
    bmesh.update_edit_mesh(mesh)
    return rig, users, references, child


count = 0
try:
    for relative in (False, True):
        for nonuniform in (False, True):
            for move_origin in (False, True):
                for axis in "XYZ":
                    for negative in (False, True):
                        rig, users, references, child = create_scene(relative, nonuniform)
                        active = users[0]
                        old_origin = active.matrix_world.translation.copy()
                        points, _, _ = addon.selected_points(bpy.context)
                        center = (points[0] + points[1]) * 0.5
                        unit_points = unit_positions(active)
                        direction = (unit_points[1] - unit_points[0]).normalized()
                        largest = max(range(3), key=lambda i: abs(direction[i]))
                        if direction[largest] < 0:
                            direction.negate()
                        if negative:
                            direction.negate()
                        child_before = child.matrix_world.copy()
                        rig_before = rig.matrix_world.copy()
                        bone_before = [bone.matrix.copy() for bone in rig.pose.bones]
                        basis_before = [obj.matrix_basis.copy() for obj in users]
                        assert bpy.ops.object.selection_to_pivot(
                            axis=axis, negative=negative, origin=move_origin) == {'FINISHED'}
                        close(active.matrix_world.translation, center if move_origin else old_origin)
                        close(active.matrix_world.to_3x3().col["XYZ".index(axis)].normalized(), direction)
                        assert active.mode == 'EDIT'
                        assert users[0].data == users[1].data
                        assert rig.matrix_world == rig_before
                        assert [bone.matrix for bone in rig.pose.bones] == bone_before
                        for index, obj in enumerate(users):
                            assert obj.parent == rig and obj.parent_type == 'BONE'
                            assert obj.matrix_basis == basis_before[index]
                            same_shape(unit_positions(obj), unit_positions(references[index]))
                        for actual, expected in zip(child.matrix_world, child_before):
                            close(actual, expected)
                        # The scale-1 shape must survive subsequent bone motion,
                        # not just the pose where the pivot was changed.
                        for amount in (0.0, -0.6, 0.9):
                            set_pose(rig, amount)
                            for obj, reference in zip(users, references):
                                same_shape(unit_positions(obj), unit_positions(reference))
                        count += 1
    print(f"PASS: {count} bone-parent checks (pose, relative parenting, scale, linked copies, children)")
finally:
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    addon.unregister()
