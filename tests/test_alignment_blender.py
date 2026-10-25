# SPDX-License-Identifier: GPL-3.0-or-later
"""Run in a separate Blender process with --background --factory-startup."""

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

TOLERANCE = 2e-4
CUSTOM_KEY = "_selection_to_pivot_test_property"


def close(actual, expected):
    assert (actual - expected).length < TOLERANCE, (tuple(actual), tuple(expected))


def create_object(points, custom_state=False):
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    # Uniform scale retains world-space invariance. Non-uniform scale now
    # preserves the scale-1 shape instead (covered by test_scale_one_blender.py).
    world = Matrix.LocRotScale(
        Vector((3, -2, 5)), Euler((0.3, -0.5, 0.7)).to_quaternion(), Vector((1.5, 1.5, 1.5)))
    mesh = bpy.data.meshes.new("Alignment Test")
    mesh.from_pydata([world.inverted() @ point for point in points], [], [])
    obj = bpy.data.objects.new("Alignment Test", mesh)
    bpy.context.collection.objects.link(obj)
    obj.matrix_world = world
    if custom_state:
        # Unrelated object metadata must not affect alignment or be modified.
        obj[CUSTOM_KEY] = "unchanged"
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    return obj


def world_points(obj):
    return [obj.matrix_world @ vertex.co for vertex in bmesh.from_edit_mesh(obj.data).verts]


def check_operation(obj, move_origin, axis, negative, expected_direction, expected_origin):
    before = world_points(obj)
    result = bpy.ops.object.selection_to_pivot(axis=axis, negative=negative, origin=move_origin)
    assert result == {'FINISHED'}, result
    assert obj.mode == 'EDIT'
    direction = -expected_direction if negative else expected_direction
    close(obj.matrix_world.to_3x3().col["XYZ".index(axis)].normalized(), direction)
    close(obj.matrix_world.translation, expected_origin)
    for actual, expected in zip(world_points(obj), before):
        close(actual, expected)
    if CUSTOM_KEY in obj:
        assert obj[CUSTOM_KEY] == "unchanged"


count = 0
center = Vector((6, -6, 8))
line = Vector((1, 2, 3)).normalized()
z = Vector((0, 0, 1))
selections = (
    ("single", [center]),
    ("coincident", [center, center]),
    ("coincident", [center, center, center]),
    ("line", [center - line * 2, center + line * 2]),
    ("symmetric", [center + Vector(offset) for offset in
                   ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0))]),
    ("symmetric", [center + Vector((x, y, z))
                   for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]),
)

try:
    # Switch axes repeatedly on the same object: no saved axis may stay locked.
    # Includes first-click alignment with no prior Set Origin, and old scenes.
    for custom_state in (False, True):
        for move_origin in (False, True):
            for kind, points in selections:
                obj = create_object(points, custom_state)
                origin = obj.matrix_world.translation.copy()
                expected = (center - origin).normalized() if kind in ("single", "coincident", "symmetric") and not move_origin else (
                    line if kind == "line" else z)
                for axis, negative in (("X", False), ("Y", True), ("Z", False),
                                       ("X", True), ("Y", False), ("Z", True), ("Z", True)):
                    check_operation(obj, move_origin, axis, negative, expected,
                                    center if move_origin else origin)
                    count += 1

    # Former failure: after setting X on a line, aligning Y/Z to the same line
    # must succeed without projecting perpendicular to X or preserving X.
    obj = create_object([center - line * 2, center + line * 2])
    check_operation(obj, True, "X", False, line, center)
    for axis in "YZX":
        check_operation(obj, False, axis, False, line, center)
        count += 1

    # Following Set Origin, select one different point and aim each chosen axis
    # directly from the current origin to that point, including negative vectors.
    obj = create_object([center - line * 2, center + line * 2])
    check_operation(obj, True, "Z", False, line, center)
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    for vertex in bm.verts:
        vertex.select_set(vertex.index == 0)
    bmesh.update_edit_mesh(obj.data)
    for axis in "XYZ":
        check_operation(obj, False, axis, False, -line, center)
        count += 1

    # Points or a symmetric selection centered at the origin: do nothing.
    # Exercise exact coincidence and rounding after a real origin operation.
    for recenter, vertex_count in ((False, 1), (True, 1), (False, 2), (True, 2),
                                  (False, 3), (True, 3), (False, 4), (True, 4)):
        offsets = ([Vector(offset) for offset in
                    ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0))]
                   if vertex_count == 4 else [Vector()] * vertex_count)
        obj = create_object([center + offset for offset in offsets])
        if recenter:
            check_operation(obj, True, "X", False, z, center)
        else:
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            for vertex, offset in zip(bm.verts, offsets):
                vertex.co = offset
            bmesh.update_edit_mesh(obj.data)
        world_before = obj.matrix_world.copy()
        bm = bmesh.from_edit_mesh(obj.data)
        vertices_before = [vertex.co.copy() for vertex in bm.verts]
        selection_before = [vertex.select for vertex in bm.verts]
        for axis in "XYZ":
            for negative in (False, True):
                result = bpy.ops.object.selection_to_pivot(
                    axis=axis, negative=negative, origin=False)
                assert result == {'CANCELLED'}, result
                assert obj.mode == 'EDIT'
                assert obj.matrix_world == world_before
                bm = bmesh.from_edit_mesh(obj.data)
                assert [vertex.co for vertex in bm.verts] == vertices_before
                assert [vertex.select for vertex in bm.verts] == selection_before
                count += 1

    # A nearby but distinct point must still be aimed at, not ignored.
    origin = Vector((3, -2, 5))
    point = origin + Vector((0.001, 0, 0))
    _, direction, _ = addon.selection_direction([point], direction_origin=origin)
    close(direction, Vector((1, 0, 0)))

    print(f"PASS: {count} operator alignment checks (axes, signs, origin, geometry, custom properties)")
finally:
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    addon.unregister()
