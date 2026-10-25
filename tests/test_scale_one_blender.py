# SPDX-License-Identifier: GPL-3.0-or-later
"""Scale-1 alignment regressions; run with --background --factory-startup."""

import importlib.util
from itertools import combinations, product
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("selection_to_pivot", ROOT / "__init__.py")
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
addon.register()
TOLERANCE = 3e-4


def close(actual, expected):
    assert (actual - expected).length < TOLERANCE, (tuple(actual), tuple(expected))


def coordinates(obj, world=False):
    vertices = bmesh.from_edit_mesh(obj.data).verts if obj.data.is_editmode else obj.data.vertices
    return [obj.matrix_world @ v.co if world else v.co.copy() for v in vertices]


def unit_points(obj, keep_mirror=False):
    scale, delta = obj.scale.copy(), obj.delta_scale.copy()
    obj.scale = tuple(-1 if value < 0 and keep_mirror else 1 for value in scale)
    obj.delta_scale = tuple(-1 if value < 0 and keep_mirror else 1 for value in delta)
    bpy.context.view_layer.update()
    result = coordinates(obj, world=True)
    obj.scale, obj.delta_scale = scale, delta
    bpy.context.view_layer.update()
    return result


def same_shape(actual, expected):
    # Shape AND orientation after clearing scale, allowing the moved origin's offset.
    for a, e in zip(actual, expected):
        close(a - actual[0], e - expected[0])


def rigid_mesh(actual, expected):
    for i, j in combinations(range(len(actual)), 2):
        assert abs((actual[i] - actual[j]).length - (expected[i] - expected[j]).length) < TOLERANCE


def create_scene(scale, linked_scale=None, parented=False, delta=False):
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.name = "Scale One Test"
    obj.location = (3, -2, 1)
    obj.rotation_euler = (0.2, -0.4, 0.3)
    obj.scale = scale
    if delta:
        obj.delta_scale = (0.7, 1.3, 1.1)
        obj.delta_rotation_euler = (0.1, 0.2, -0.1)
        obj.delta_location = (0.3, -0.2, 0.1)
    if parented:
        parent = bpy.data.objects.new("Scale One Parent", None)
        bpy.context.collection.objects.link(parent)
        parent.rotation_euler = (-0.3, 0.1, 0.2)
        parent.scale = (1.5, 0.8, 2)
        obj.parent = parent
    users = [obj]
    if linked_scale is not None:
        linked = obj.copy()
        bpy.context.collection.objects.link(linked)
        linked.location = (-4, 2, 0)
        linked.rotation_euler = (-0.1, 0.5, 0.2)
        linked.scale = linked_scale
        linked.select_set(True)
        users.append(linked)
    child = bpy.data.objects.new("Scale One Child", None)
    bpy.context.collection.objects.link(child)
    child.parent = obj
    child.location = (1, 2, 3)
    bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(obj.data)
    for v in bm.verts:
        # A diagonal in XY exposes noncommuting rotation and scale.
        v.select_set(v.co.z < 0 and v.co.x == v.co.y)
    bmesh.update_edit_mesh(obj.data)
    return users, child


class Reporter:
    axis = 'X'
    negative = False

    def __init__(self):
        self.messages = []

    def report(self, level, message):
        self.messages.append((level, message))


count = 0
try:
    assert addon.VIEW3D_PT_selection_to_pivot.bl_category == "Selection to Pivot"
    panel_users, _ = create_scene((1, 1, 1))
    assert addon.VIEW3D_PT_selection_to_pivot.poll(bpy.context)
    bpy.ops.object.mode_set(mode='OBJECT')
    assert not addon.VIEW3D_PT_selection_to_pivot.poll(bpy.context)

    # Cover all mirror parities, not only three negative axes (which commute
    # with any rotation and would hide errors in the reflection handling).
    for scale in ((1, 2, 1), (-1, 2, 1), *product((-2, 2), repeat=3)):
        for move_origin in (False, True):
            for axis in "XYZ":
                for negative in (False, True):
                    users, child = create_scene(scale)
                    obj = users[0]
                    original_mesh = coordinates(obj)
                    original_unit = unit_points(obj)
                    original_mirrored_unit = unit_points(obj, keep_mirror=True)
                    original_world = coordinates(obj, world=True)
                    original_scale = obj.scale.copy()
                    child_before = child.matrix_world.copy()
                    origin_before = obj.matrix_world.translation.copy()
                    bm = bmesh.from_edit_mesh(obj.data)
                    selection = [v.index for v in bm.verts if v.select]
                    expected_origin = sum((original_world[i] for i in selection), Vector()) / len(selection)
                    expected_direction = (original_mirrored_unit[selection[1]]
                                          - original_mirrored_unit[selection[0]]).normalized()
                    largest = max(range(3), key=lambda i: abs(expected_direction[i]))
                    if expected_direction[largest] < 0:
                        expected_direction.negate()
                    if negative:
                        expected_direction.negate()
                    assert bpy.ops.object.selection_to_pivot(
                        axis=axis, negative=negative, origin=move_origin) == {'FINISHED'}
                    assert obj.scale == original_scale
                    close(obj.matrix_world.translation, expected_origin if move_origin else origin_before)
                    rigid_mesh(coordinates(obj), original_mesh)
                    same_shape(unit_points(obj, keep_mirror=True), original_mirrored_unit)
                    rigid_mesh(unit_points(obj), original_unit)
                    close(obj.matrix_world.to_3x3().col["XYZ".index(axis)].normalized(), expected_direction)
                    for actual, expected in zip(child.matrix_world, child_before):
                        close(actual, expected)
                    assert [v.index for v in bmesh.from_edit_mesh(obj.data).verts if v.select] == selection
                    assert obj.mode == 'EDIT'
                    if len(set(map(abs, scale))) == 1:
                        for actual, expected in zip(coordinates(obj, world=True), original_world):
                            close(actual, expected)
                    # Exercise Blender's actual Alt+S operation, not just a simulated matrix.
                    bpy.ops.object.mode_set(mode='OBJECT')
                    bpy.ops.object.scale_clear()
                    bpy.context.view_layer.update()
                    # Clearing a mirror can change orientation, but cannot
                    # introduce a deformation into the original base shape.
                    rigid_mesh(coordinates(obj, world=True), original_unit)
                    if all(value > 0 for value in scale):
                        same_shape(coordinates(obj, world=True), original_unit)
                    count += 1

    # Different scales and rotations on linked users: no shear is baked into the
    # shared mesh, and both objects recover their own original shape at scale 1.
    for parented in (False, True):
        for delta in (False, True):
            for move_origin in (False, True):
                users, child = create_scene((1, 2, 1), (2, 1, 3), parented, delta)
                before = [unit_points(user) for user in users]
                scales = [(user.scale.copy(), user.delta_scale.copy()) for user in users]
                mesh_before = coordinates(users[0])
                # Repeated changes must not accumulate a mesh deformation.
                for axis in "XYZX":
                    assert bpy.ops.object.selection_to_pivot(
                        axis=axis, negative=True, origin=move_origin) == {'FINISHED'}
                    assert users[0].data == users[1].data
                    rigid_mesh(coordinates(users[0]), mesh_before)
                    for user, expected, (scale, delta_scale) in zip(users, before, scales):
                        assert user.scale == scale
                        assert user.delta_scale == delta_scale
                        same_shape(unit_points(user), expected)
                    count += 1

    # Linked users with different mirror signs retain their world geometry,
    # including inherited shear and negative delta scale. No new parents or
    # independent meshes are needed to store the reflection.
    for parented in (False, True):
        for delta_mirror in (False, True):
            for move_origin in (False, True):
                users, child = create_scene((-1, 1, 1), (2, -2, -2), parented)
                if delta_mirror:
                    for user in users:
                        user.delta_scale = (1, -1, 1)
                        user.delta_rotation_euler = (0.2, -0.1, 0.3)
                    bpy.context.view_layer.update()
                before = [coordinates(user, world=True) for user in users]
                scales = [(user.scale.copy(), user.delta_scale.copy()) for user in users]
                child_before = child.matrix_world.copy()
                for axis, negative in (("X", False), ("Y", True), ("Z", False), ("Z", False)):
                    assert bpy.ops.object.selection_to_pivot(
                        axis=axis, negative=negative, origin=move_origin) == {'FINISHED'}
                    assert users[0].data == users[1].data
                    for user, expected, (scale, delta_scale) in zip(users, before, scales):
                        assert user.scale == scale and user.delta_scale == delta_scale
                        assert not addon.scale_affects_alignment(user)
                        for actual, point in zip(coordinates(user, world=True), expected):
                            close(actual, point)
                    for actual, expected in zip(child.matrix_world, child_before):
                        close(actual, expected)
                    count += 1

    # Warnings depend on magnitudes, including on selected linked copies.
    # Mirroring alone must trigger neither the panel label nor the report.
    for operator_class in (addon.OBJECT_OT_selection_to_pivot,):
        for scale, linked_scale, warn in (((1, 1, 1), None, False),
                                          ((-1, 1, 1), None, False),
                                          ((1, -1, -1), None, False),
                                          ((1, 1, 1), (-2, 2, 2), False),
                                          ((1, 2, 1), None, True),
                                          ((-1, 2, 1), None, True),
                                          ((1, 1, 1), (2, 1, 1), True)):
            users, _child = create_scene(scale, linked_scale)
            assert any(addon.scale_affects_alignment(user) for user in users) == warn
            reporter = Reporter()
            reporter.origin = True
            assert operator_class.execute(reporter, bpy.context) == {'FINISHED'}
            assert bool(reporter.messages) == warn
            if warn:
                level, message = reporter.messages[0]
                assert level == {'WARNING'}
                assert message == (
                    "Object has non-uniform scale, Selection to Pivot may be unpredictable"
                )
            count += 1

    print(f"PASS: {count} scale-1 checks (Alt+S, linked meshes, parents, delta/negative scales, warnings)")
finally:
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    addon.unregister()
